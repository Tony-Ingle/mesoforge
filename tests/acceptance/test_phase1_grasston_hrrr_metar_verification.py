"""Offline Phase 1 proof using production science, PostgreSQL, and MinIO."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
import sqlalchemy as sa
import xarray as xr
from alembic import command
from alembic.config import Config

from mesoforge.alignment.spatial import bilinear_interpolate, project_station_point
from mesoforge.application.artifacts import (
    ArtifactService,
    InputBinding,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.phase1 import (
    ForecastArtifacts,
    HrrrLeadArtifacts,
    ObservationArtifacts,
    Phase1Coordinator,
    Phase1Request,
    StationCatalogArtifacts,
    VerificationArtifacts,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.catalog.stations import StationCatalogSnapshot, normalize_station_catalog
from mesoforge.contracts.artifacts import Availability
from mesoforge.contracts.observations import NormalizedObservation, RawMetarRecord
from mesoforge.contracts.verification import MatchedPairRow, VerificationReport
from mesoforge.forecasting.baseline import assemble_baseline_forecast
from mesoforge.guidance.decoding import decode_selected_messages
from mesoforge.guidance.normalization import (
    assemble_canonical_hrrr_dataset,
    build_lambert_conformal_crs,
    compute_bbox_halo_subset_indices,
    compute_latlon_grid,
    compute_projected_coordinates,
    rotate_wind_to_earth_relative,
)
from mesoforge.guidance.validation import validate_phase1_hrrr_guidance
from mesoforge.observations.normalization import normalize_metar_record
from mesoforge.observations.tables import build_observations_table
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.parquet import ParquetTableSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.matching import match_baseline_to_observations
from mesoforge.verification.metrics import compute_verification_report
from mesoforge.verification.tables import build_matched_pairs_table
from tests.fixtures.hrrr_grib import (
    DX_M,
    DY_M,
    FIRST_LAT_DEGREES,
    FIRST_LON_DEGREES,
    LAD_DEGREES,
    LATIN1_DEGREES,
    LATIN2_DEGREES,
    LOV_DEGREES,
    NX,
    NY,
    make_lead_grib_bytes,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
# Keep the prospective synthetic issuance later than the test runner's database
# transaction clock.  Derived station metadata is correctly unavailable until its
# registration transaction completes, so a historical cutoff would make this
# acceptance fixture fail for the same reason a real retrospective fetch must fail.
NOW = datetime(2030, 8, 28, 18, tzinfo=UTC)
LEADS = tuple(range(7))


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn):
    c = Config(str(ROOT / "alembic.ini"))
    c.set_main_option("script_location", str(ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(c, "head")
    return clean_postgres_dsn


@pytest.fixture()
def infrastructure(migrated_dsn):
    store = S3ArtifactObjectStore(
        bucket=f"mesoforge-phase1-{uuid.uuid4().hex[:8]}",
        endpoint_url=os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100"),
        access_key=os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test"),
        secret_key=os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password"),
    )
    return ArtifactService(
        unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn),
        object_store=store,
        idempotency_lock=PostgresIdempotencyLock(migrated_dsn),
    ), store


class Clock:
    def now(self):
        return NOW


class Sleeper:
    def sleep(self, seconds):
        raise AssertionError(f"unexpected sleep {seconds}")


def valid_bytes(v):
    if not isinstance(v, bytes):
        raise TypeError


def valid_map(v):
    if not isinstance(v, Mapping):
        raise TypeError


def valid_table(v):
    if not isinstance(v, pa.Table):
        raise TypeError


def valid_dataset(v):
    if not isinstance(v, xr.Dataset):
        raise TypeError


def observations(table):
    out = []
    for row in table.to_pylist():
        row["quality_flags"] = tuple(row["quality_flags"])
        out.append(NormalizedObservation.model_validate(row, strict=True))
    return out


def pairs(table):
    return [MatchedPairRow.model_validate(r, strict=True) for r in table.to_pylist()]


def station_snapshot(value):
    return StationCatalogSnapshot.model_validate_json(json.dumps(value))


def verification_report(value):
    return VerificationReport.model_validate_json(json.dumps(value))


class Ports:
    def __init__(self, req, cfg):
        self.req = req
        self.cfg = cfg
        self.js = CanonicalJsonSerializer()
        self.nc = H5NetcdfDatasetSerializer()
        self.pq = ParquetTableSerializer()

    def source(self, s, typ, loc, data, available=NOW):
        return s.register_source(
            SourceRegistrationRequest(
                source_authority="mesoforge.synthetic",
                source_locator=loc,
                source_revision="fixture.v2",
                artifact_type=typ,
                artifact_schema_version=typ + ".v1",
                media_type="application/octet-stream",
                created_at=NOW,
                availability=Availability(
                    available_at=available, authority="mesoforge.synthetic", method="fixture.v2"
                ),
                configuration_snapshot_id=self.req.configuration_snapshot_id,
                configuration_digest=self.req.configuration_digest,
                code_revision=self.req.code_revision,
                environment_digest=self.req.environment_digest,
            ),
            data,
        )

    def tx(self, s, activity, ins, typ, serializer, bindings, fn, validator, media, run=True):
        return s.execute_role_bound_transformation(
            TransformationRequest(
                activity_type=activity,
                activity_version="v1",
                inputs=tuple(
                    TransformationInputRef(role=r, artifact_id=a.artifact_id) for r, a in ins
                ),
                output_role="primary",
                output_artifact_type=typ,
                output_artifact_schema_version=typ + ".v1",
                output_media_type=media,
                configuration_snapshot_id=self.req.configuration_snapshot_id,
                configuration_digest=self.req.configuration_digest,
                code_revision=self.req.code_revision,
                environment_digest=self.req.environment_digest,
                run_id=str(self.req.run_id) if run else None,
            ),
            fn,
            serializer,
            input_bindings=bindings,
            output_validator=validator,
        ).output

    def register_station_catalog(self, request, *, artifact_service, clock, sleeper):
        raw = [
            {
                "icaoId": x.provider_icao_id,
                "lat": x.expected_latitude,
                "lon": x.expected_longitude,
                "elev": x.expected_elevation_m,
                "site": x.site_name,
                "siteType": {"METAR": True},
            }
            for x in self.cfg.phase1.stations
        ]
        response = self.source(
            artifact_service,
            "aviationweather-station-response",
            "fixture://stations",
            json.dumps(raw).encode(),
        )
        snap = self.tx(
            artifact_service,
            "stations.normalize",
            (("response", response),),
            "station-catalog-snapshot",
            self.js,
            {"response": InputBinding(json.loads, lambda x: None)},
            lambda x: normalize_station_catalog(
                raw_records=x["response"],
                domain=self.cfg.phase1.domain,
                expected_stations=self.cfg.phase1.stations,
                source_artifact_id=response.artifact_id,
                effective_from=NOW,
            ).model_dump(mode="json"),
            valid_map,
            "application/json",
            False,
        )
        return StationCatalogArtifacts(response=response, snapshot=snap)

    def register_hrrr_sources(self, request, *, artifact_service, clock, sleeper):
        out = []
        for lead in LEADS:
            idx = self.source(
                artifact_service,
                "hrrr-grib-index",
                f"fixture://f{lead}/idx",
                f"synthetic f{lead}".encode(),
            )
            grib = self.source(
                artifact_service,
                "hrrr-selected-grib",
                f"fixture://f{lead}/grib",
                make_lead_grib_bytes(
                    forecast_hour=lead,
                    temperature_k=np.full((NY, NX), 280.0 + lead),
                    eastward_wind_m_s=np.zeros((NY, NX)),
                    northward_wind_m_s=np.zeros((NY, NX)),
                    grid_relative_wind=True,
                ),
            )
            out.append(HrrrLeadArtifacts(lead_hours=lead, index=idx, selected_grib=grib))
        return tuple(out)

    def build_forecast(self, request, run, station_catalog, hrrr, *, artifact_service):
        roots = tuple((f"idx{h.lead_hours}", h.index) for h in hrrr) + tuple(
            (f"grib{h.lead_hours}", h.selected_grib) for h in hrrr
        )
        acq = self.tx(
            artifact_service,
            "hrrr.acquire",
            roots,
            "hrrr-acquisition-manifest",
            self.js,
            {r: InputBinding(lambda x: x, valid_bytes) for r, _ in roots},
            lambda x: {
                "schema_version": "hrrr-acquisition-manifest.v1",
                "byte_counts": {r: len(v) for r, v in x.items()},
            },
            valid_map,
            "application/json",
        )
        lin = self.tx(
            artifact_service,
            "hrrr.lineage",
            (("acquisition", acq),),
            "variable-lineage-manifest",
            self.js,
            {"acquisition": InputBinding(self.js.deserialize, valid_map)},
            lambda x: {
                "schema_version": "variable-lineage-manifest.v1",
                "variables": [
                    a.canonical_variable_id for a in self.cfg.phase1.hrrr.field_assertions
                ],
            },
            valid_map,
            "application/json",
        )
        gins = (("lineage", lin),) + tuple((f"grib{h.lead_hours}", h.selected_grib) for h in hrrr)
        binds = {"lineage": InputBinding(self.js.deserialize, valid_map)}
        binds.update({f"grib{h}": InputBinding(lambda x: x, valid_bytes) for h in LEADS})

        def guidance(v):
            crs = build_lambert_conformal_crs(
                lov_degrees=LOV_DEGREES,
                lad_degrees=LAD_DEGREES,
                latin1_degrees=LATIN1_DEGREES,
                latin2_degrees=LATIN2_DEGREES,
            )
            x, y = compute_projected_coordinates(
                crs,
                first_lat_degrees=FIRST_LAT_DEGREES,
                first_lon_degrees=FIRST_LON_DEGREES,
                dx_m=DX_M,
                dy_m=DY_M,
                nx=NX,
                ny=NY,
            )
            lat, lon = compute_latlon_grid(crs, x=x, y=y)
            sub = compute_bbox_halo_subset_indices(
                x=x, y=y, lat=lat, lon=lon, bbox=self.cfg.phase1.domain.bbox
            )
            ys = slice(sub.y_start, sub.y_end)
            xs = slice(sub.x_start, sub.x_end)
            ts = []
            us = []
            vs = []
            for h in LEADS:
                d = decode_selected_messages(v[f"grib{h}"], settings=self.cfg.phase1.hrrr)
                u = d["eastward_wind_10m"]
                w = d["northward_wind_10m"]
                rot = rotate_wind_to_earth_relative(
                    u_grid=u.values,
                    v_grid=w.values,
                    x=x,
                    y=y,
                    crs=crs,
                    u_relative_to_grid=bool(u.attrs["GRIB_uvRelativeToGrid"]),
                    v_relative_to_grid=bool(w.attrs["GRIB_uvRelativeToGrid"]),
                )
                ts.append(d["air_temperature_2m"].values[ys, xs])
                us.append(rot.eastward[ys, xs])
                vs.append(rot.northward[ys, xs])
            return assemble_canonical_hrrr_dataset(
                forecast_reference_time=np.datetime64(NOW.replace(tzinfo=None), "ns"),
                lead_hours=LEADS,
                x=x[xs],
                y=y[ys],
                lat=lat[ys, xs],
                lon=lon[ys, xs],
                temperature_k=np.stack(ts),
                eastward_wind_m_s=np.stack(us),
                northward_wind_m_s=np.stack(vs),
                grid_id="hrrr-conus-grasston-subset.v1",
                configuration_snapshot_id=str(self.req.configuration_snapshot_id),
                variable_lineage_manifest_id=str(lin.artifact_id),
            )

        guide = self.tx(
            artifact_service,
            "hrrr.decode-normalize-rotate",
            gins,
            "canonical-guidance",
            self.nc,
            binds,
            guidance,
            validate_phase1_hrrr_guidance,
            "application/x-netcdf",
        )

        def extract(v):
            ds = v["guidance"]
            snap = station_snapshot(v["stations"])
            crs = build_lambert_conformal_crs(
                lov_degrees=LOV_DEGREES,
                lad_degrees=LAD_DEGREES,
                latin1_degrees=LATIN1_DEGREES,
                latin2_degrees=LATIN2_DEGREES,
            )
            rows = []
            for st in snap.stations:
                sx, sy = project_station_point(crs, latitude=st.latitude, longitude=st.longitude)
                for h in LEADS:
                    for var in ("air_temperature_2m", "eastward_wind_10m", "northward_wind_10m"):
                        z = bilinear_interpolate(
                            field=ds[var].isel(lead_time=h).values,
                            x=ds.x.values,
                            y=ds.y.values,
                            station_x=sx,
                            station_y=sy,
                        )
                        rows.append(
                            {
                                "station_id": str(st.station_id),
                                "lead_hours": h,
                                "variable_id": var,
                                "value": z.value,
                                "weights": [
                                    z.weights.w00,
                                    z.weights.w01,
                                    z.weights.w10,
                                    z.weights.w11,
                                ],
                            }
                        )
            return {"schema_version": "point-extraction-report.v1", "rows": rows}

        ext = self.tx(
            artifact_service,
            "guidance.bilinear-extract",
            (("guidance", guide), ("stations", station_catalog.snapshot)),
            "point-extraction-report",
            self.js,
            {
                "guidance": InputBinding(self.nc.deserialize, validate_phase1_hrrr_guidance),
                "stations": InputBinding(self.js.deserialize, valid_map),
            },
            extract,
            valid_map,
            "application/json",
        )
        base = self.tx(
            artifact_service,
            "forecast.baseline",
            (("guidance", guide), ("stations", station_catalog.snapshot), ("extraction", ext)),
            "baseline-forecast",
            self.nc,
            {
                "guidance": InputBinding(self.nc.deserialize, validate_phase1_hrrr_guidance),
                "stations": InputBinding(self.js.deserialize, valid_map),
                "extraction": InputBinding(self.js.deserialize, valid_map),
            },
            lambda v: assemble_baseline_forecast(
                station_values={
                    (r["station_id"], r["lead_hours"], r["variable_id"]): r["value"]
                    for r in v["extraction"]["rows"]
                },
                lead_hours=LEADS,
                forecast_issue_time=np.datetime64(NOW.replace(tzinfo=None), "ns"),
                hrrr_source_reference_time=np.datetime64(NOW.replace(tzinfo=None), "ns"),
                contributor_artifact_id=str(guide.artifact_id),
                extraction_report_artifact_id=str(ext.artifact_id),
            ),
            valid_dataset,
            "application/x-netcdf",
        )
        return ForecastArtifacts(
            acquisition_manifest=acq,
            variable_lineage=lin,
            canonical_guidance=guide,
            extraction_report=ext,
            baseline=base,
        )

    def register_metar_observations(
        self, request, station_catalog, *, artifact_service, clock, sleeper
    ):
        raw = raw_metars()
        response = self.source(
            artifact_service,
            "aviationweather-metar-response",
            "fixture://metar",
            json.dumps(raw).encode(),
        )

        def norm(v):
            snap = station_snapshot(v["stations"])
            by = {s.provider_icao_id: s for s in snap.stations}
            out = []
            for i, p in enumerate(v["response"]):
                r = RawMetarRecord.model_validate_json(json.dumps(p))
                st = by[r.icao_id]
                out.append(
                    normalize_metar_record(
                        raw=r,
                        station=st,
                        station_id=st.station_id,
                        policy=self.cfg.phase1.observation_normalization_policy,
                        raw_artifact_id=response.artifact_id,
                        raw_record_index=i,
                        station_snapshot_artifact_id=station_catalog.snapshot.artifact_id,
                        ingested_at=r.receipt_time,
                        query_window_start=NOW,
                        query_window_end=NOW + timedelta(hours=6),
                    )
                )
            return build_observations_table(out)

        obs = self.tx(
            artifact_service,
            "metar.normalize",
            (("response", response), ("stations", station_catalog.snapshot)),
            "normalized-metar-observations",
            self.pq,
            {
                "response": InputBinding(json.loads, lambda x: None),
                "stations": InputBinding(self.js.deserialize, valid_map),
            },
            norm,
            valid_table,
            "application/x-parquet",
        )
        return ObservationArtifacts(responses=(response,), normalized=obs)

    def verify(
        self, request, run, forecast, observations: ObservationArtifacts, *, artifact_service
    ):
        match = self.tx(
            artifact_service,
            "verification.match",
            (("baseline", forecast.baseline), ("observations", observations.normalized)),
            "matched-pairs",
            self.pq,
            {
                "baseline": InputBinding(self.nc.deserialize, valid_dataset),
                "observations": InputBinding(self.pq.deserialize, valid_table),
            },
            lambda v: build_matched_pairs_table(
                match_baseline_to_observations(
                    baseline=v["baseline"],
                    station_ids=self.cfg.phase1.domain.station_ids,
                    lead_hours=LEADS,
                    observations=globals()["observations"](v["observations"]),
                    matching_policy=self.cfg.phase1.matching_policy,
                    verification_cutoff=self.req.verification_cutoff,
                    baseline_artifact_id=forecast.baseline.artifact_id,
                    observations_artifact_id=observations.normalized.artifact_id,
                )
            ),
            valid_table,
            "application/x-parquet",
        )
        report = self.tx(
            artifact_service,
            "verification.metrics",
            (
                ("pairs", match),
                ("baseline", forecast.baseline),
                ("guidance", forecast.canonical_guidance),
            ),
            "verification-report",
            self.js,
            {
                "pairs": InputBinding(self.pq.deserialize, valid_table),
                "baseline": InputBinding(self.nc.deserialize, valid_dataset),
                "guidance": InputBinding(self.nc.deserialize, validate_phase1_hrrr_guidance),
            },
            lambda v: compute_verification_report(
                pairs(v["pairs"]),
                metric_set=self.cfg.phase1.metric_set,
                station_ids=tuple(map(str, self.cfg.phase1.domain.station_ids)),
                lead_hours=LEADS,
                baseline_artifact_id=forecast.baseline.artifact_id,
                matched_pairs_artifact_id=match.artifact_id,
            ).model_dump(mode="json"),
            verification_report,
            "application/json",
        )
        return VerificationArtifacts(matched_pairs=match, report=report)


def raw_metars():
    sites = {
        "KCBG": (45.557, -93.264, 285.0),
        "KJMR": (45.88854, -93.269, 301.0),
        "KROS": (45.69624, -92.95427, 282.0),
    }
    out = []
    for icao, (lat, lon, elev) in sites.items():
        for h in LEADS:
            if icao == "KROS" and h == 6:
                continue
            event = NOW + timedelta(hours=h)
            temp = None if (icao, h) == ("KROS", 3) else 6.85 + h
            vrb = (icao, h) == ("KJMR", 2)
            out.append(
                {
                    "icao_id": icao,
                    "obs_time": event.isoformat(),
                    "report_time": event.isoformat(),
                    "receipt_time": (event + timedelta(minutes=1)).isoformat(),
                    "temp": temp,
                    "wdir": "VRB" if vrb else 0.0,
                    "wspd": 10.0 if vrb else 0.0,
                    "qc_field": 0.0,
                    "metar_type": "METAR",
                    "raw_ob": icao + " synthetic",
                    "lat": lat,
                    "lon": lon,
                    "elev": elev,
                }
            )
    cor = dict(out[0])
    cor["receipt_time"] = (NOW + timedelta(minutes=2)).isoformat()
    cor["raw_ob"] += " COR"
    out.append(cor)
    return out


def ancestors(dsn, root):
    q = sa.text(
        """WITH RECURSIVE a(id) AS (
        SELECT CAST(:r AS uuid) UNION
        SELECT ai.artifact_id FROM activity_outputs ao
        JOIN activity_inputs ai ON ai.activity_id=ao.activity_id
        JOIN a ON a.id=ao.artifact_id)
        SELECT 'art_'||id::text FROM a"""
    )
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as c:
            return set(c.execute(q, {"r": root.removeprefix("art_")}).scalars())
    finally:
        engine.dispose()


def succeeded_count(dsn):
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as connection:
            return connection.execute(
                sa.text("SELECT count(*) FROM activities WHERE status='succeeded'")
            ).scalar_one()
    finally:
        engine.dispose()


def test_offline_phase1_runs_real_science_with_replay_and_lineage(infrastructure, migrated_dsn):
    service, store = infrastructure
    cfg, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
    )
    snap = ConfigurationService(
        unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn)
    ).register(cfg)
    req = Phase1Request(
        run_id=f"run_{uuid.uuid4()}",
        configuration_snapshot_id=snap.configuration_snapshot_id,
        configuration_digest=snap.configuration_digest,
        code_revision="d" * 40,
        environment_digest="sha256:" + "e" * 64,
        lockfile_digest="sha256:" + "f" * 64,
        cycle=NOW,
        forecast_issue_time=NOW,
        information_cutoff=NOW,
        verification_cutoff=NOW + timedelta(hours=7),
    )
    port = Ports(req, cfg)
    coordinator = Phase1Coordinator(
        artifact_service=service,
        sources=port,
        transformations=port,
        clock=Clock(),
        sleeper=Sleeper(),
    )
    first = coordinator.run(req)
    before = succeeded_count(migrated_dsn)
    second = coordinator.run(req)
    after = succeeded_count(migrated_dsn)
    assert len(first.run.selected_input_artifact_ids) == 15
    assert first.verification.report.artifact_id == second.verification.report.artifact_id
    assert before == after
    all_artifacts = (
        first.station_catalog.response,
        first.station_catalog.snapshot,
        *(a for h in first.hrrr for a in (h.index, h.selected_grib)),
        first.forecast.acquisition_manifest,
        first.forecast.variable_lineage,
        first.forecast.canonical_guidance,
        first.forecast.extraction_report,
        first.forecast.baseline,
        *first.observations.responses,
        first.observations.normalized,
        first.verification.matched_pairs,
        first.verification.report,
    )
    for a in all_artifacts:
        assert store.get_verified(a.storage_uri, a.content_digest)
    guide = port.nc.deserialize(
        store.get_verified(
            first.forecast.canonical_guidance.storage_uri,
            first.forecast.canonical_guidance.content_digest,
        )
    )
    validate_phase1_hrrr_guidance(guide)
    base = port.nc.deserialize(
        store.get_verified(
            first.forecast.baseline.storage_uri, first.forecast.baseline.content_digest
        )
    )
    table = port.pq.deserialize(
        store.get_verified(
            first.verification.matched_pairs.storage_uri,
            first.verification.matched_pairs.content_digest,
        )
    )
    report = verification_report(
        port.js.deserialize(
            store.get_verified(
                first.verification.report.storage_uri, first.verification.report.content_digest
            )
        )
    )
    assert guide.sizes["lead_time"] == 7 and base.sizes["location"] == 3 and table.num_rows == 21
    assert first.forecast.baseline.artifact_id != first.forecast.canonical_guidance.artifact_id
    assert guide.identical(port.nc.deserialize(port.nc.serialize(guide)))
    assert base.identical(port.nc.deserialize(port.nc.serialize(base)))
    for lead in LEADS:
        np.testing.assert_allclose(guide.air_temperature_2m.isel(lead_time=lead), 280.0 + lead)
    assert np.all(guide.eastward_wind_10m.values == 0.0)
    assert np.all(guide.northward_wind_10m.values == 0.0)
    oracle = json.loads((ROOT / "tests/fixtures/phase1_expected.json").read_text())
    rows = pairs(table)
    for field, expected in oracle["statuses"].items():
        assert Counter(getattr(r, field + "_status") for r in rows) == expected
    by_key = {(str(row.station_id), row.lead_hours): row for row in rows}
    assert by_key[("station.kcbg", 0)].selected_provider_available_at == NOW + timedelta(minutes=2)
    assert by_key[("station.kjmr", 2)].wind_direction_status == (
        "wind_direction_missing_or_variable"
    )
    assert by_key[("station.kros", 3)].temperature_status == "temperature_missing"
    assert by_key[("station.kros", 6)].temperature_status == "no_report_within_tolerance"
    overall = {r.metric_name: r for r in report.rows if r.stratum_kind == "overall"}
    for name, (value, count) in oracle["overall"].items():
        assert overall[name].sample_count == count
        assert (
            overall[name].value == pytest.approx(value)
            if value is not None
            else overall[name].value is None
        ), (name, overall[name].value, value)
    obs = observations(
        port.pq.deserialize(
            store.get_verified(
                first.observations.normalized.storage_uri,
                first.observations.normalized.content_digest,
            )
        )
    )
    replay = match_baseline_to_observations(
        baseline=base,
        station_ids=cfg.phase1.domain.station_ids,
        lead_hours=LEADS,
        observations=obs,
        matching_policy=cfg.phase1.matching_policy,
        verification_cutoff=req.verification_cutoff,
        baseline_artifact_id=first.forecast.baseline.artifact_id,
        observations_artifact_id=first.observations.normalized.artifact_id,
    )
    assert build_matched_pairs_table(replay).equals(table)
    replay_report = compute_verification_report(
        replay,
        metric_set=cfg.phase1.metric_set,
        station_ids=tuple(map(str, cfg.phase1.domain.station_ids)),
        lead_hours=LEADS,
        baseline_artifact_id=first.forecast.baseline.artifact_id,
        matched_pairs_artifact_id=first.verification.matched_pairs.artifact_id,
    )
    assert replay_report == report

    report_ancestors = ancestors(migrated_dsn, first.verification.report.artifact_id)
    required_roots = {
        first.station_catalog.response.artifact_id,
        *(
            artifact.artifact_id
            for lead in first.hrrr
            for artifact in (lead.index, lead.selected_grib)
        ),
        *(artifact.artifact_id for artifact in first.observations.responses),
    }
    assert required_roots <= report_ancestors
    assert first.forecast.baseline.artifact_id in report_ancestors
    assert first.forecast.canonical_guidance.artifact_id in report_ancestors
    assert not any(
        name.startswith(("mesoforge.ai", "mesoforge.publication", "mesoforge.bias"))
        for name in sys.modules
    )
    assert compute_verification_report(
        replay,
        metric_set=cfg.phase1.metric_set,
        station_ids=tuple(map(str, cfg.phase1.domain.station_ids)),
        lead_hours=LEADS,
        baseline_artifact_id=first.forecast.baseline.artifact_id,
        matched_pairs_artifact_id=first.verification.matched_pairs.artifact_id,
    ).model_dump(mode="json") == report.model_dump(mode="json")
    lineage = ancestors(migrated_dsn, str(first.verification.report.artifact_id))
    required = {
        str(first.station_catalog.response.artifact_id),
        str(first.station_catalog.snapshot.artifact_id),
        str(first.forecast.canonical_guidance.artifact_id),
        str(first.forecast.baseline.artifact_id),
        str(first.observations.responses[0].artifact_id),
        *(str(a.artifact_id) for h in first.hrrr for a in (h.index, h.selected_grib)),
    }
    assert required <= lineage
    late = port.source(
        service, "late-source", "fixture://late", b"late", NOW + timedelta(seconds=1)
    )
    with pytest.raises(ValueError, match="cutoff"):
        service.create_run(
            run_id=f"run_{uuid.uuid4()}",
            forecast_issue_time=NOW,
            information_cutoff=NOW,
            configuration_snapshot_id=req.configuration_snapshot_id,
            configuration_digest=req.configuration_digest,
            code_revision=req.code_revision,
            environment_digest=req.environment_digest,
            lockfile_digest=req.lockfile_digest,
            random_seed=0,
            selected_input_artifact_ids=(late.artifact_id,),
        )
    assert not any(
        n == p or n.startswith(p + ".")
        for n in sys.modules
        for p in ("mesoforge.ai", "mesoforge.publication", "mesoforge.bias")
    )
