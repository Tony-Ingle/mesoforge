"""Offline Phase 1 proof using production science, PostgreSQL, and MinIO.

Exercises the real ``Phase1ProductionAdapters`` (the sole
``Phase1SourcePort``/``Phase1TransformationPort`` implementation --
plan Section 4.3, Codex review t_09a43c6c finding 4) with deterministic
in-process fixture HTTP transports standing in for HRRR and
AviationWeather.gov, never a parallel test-local port implementation.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from mesoforge.application.artifacts import ArtifactService, SourceRegistrationRequest
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.phase1 import Phase1Coordinator, Phase1Request
from mesoforge.application.phase1_adapters import Phase1ProductionAdapters
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.contracts.artifacts import Availability
from mesoforge.contracts.observations import NormalizedObservation
from mesoforge.contracts.verification import MatchedPairRow, VerificationReport
from mesoforge.guidance.validation import validate_phase1_hrrr_guidance
from mesoforge.observations.acquisition import RequestRateLimiter
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.parquet import ParquetTableSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.matching import match_baseline_to_observations
from mesoforge.verification.metrics import compute_verification_report
from mesoforge.verification.tables import build_matched_pairs_table
from tests.fixtures.hrrr_grib import NX, NY
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
    FixtureHrrrTransport,
    RecordingSleeper,
    build_hrrr_lead_fixture,
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


def observations(table):
    out = []
    for row in table.to_pylist():
        row["quality_flags"] = tuple(row["quality_flags"])
        out.append(NormalizedObservation.model_validate(row, strict=True))
    return out


def pairs(table):
    return [MatchedPairRow.model_validate(r, strict=True) for r in table.to_pylist()]


def verification_report(value):
    return VerificationReport.model_validate_json(json.dumps(value))


def raw_stationinfo_records(stations):
    return [
        {
            "icaoId": station.provider_icao_id,
            "lat": station.expected_latitude,
            "lon": station.expected_longitude,
            "elev": station.expected_elevation_m,
            "site": station.site_name,
            "siteType": {"METAR": True},
        }
        for station in stations
    ]


def raw_metar_records_camel_case():
    """Provider-shaped (camelCase) METAR JSON records, exercised through
    the real ``parse_raw_metar_response``/``RawMetarRecord`` boundary
    (Codex review t_09a43c6c finding 6) rather than a test-only snake_case
    shape. ``obsTime`` uses the provider's real epoch-seconds encoding;
    ``reportTime``/``receiptTime`` use its ISO-8601-with-Z encoding."""
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
                    "icaoId": icao,
                    "obsTime": int(event.timestamp()),
                    "reportTime": event.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "receiptTime": (event + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "temp": temp,
                    "wdir": "VRB" if vrb else 0.0,
                    "wspd": 10.0 if vrb else 0.0,
                    "qcField": 0.0,
                    "metarType": "METAR",
                    "rawOb": icao + " synthetic",
                    "lat": lat,
                    "lon": lon,
                    "elev": elev,
                }
            )
    cor = dict(out[0])
    cor["receiptTime"] = (NOW + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cor["rawOb"] += " COR"
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
    assert cfg.phase1 is not None
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

    cycle_date_str = NOW.strftime("%Y%m%d")
    cycle_hour = NOW.hour
    hrrr_leads = {
        lead: build_hrrr_lead_fixture(
            forecast_hour=lead,
            temperature_k=np.full((NY, NX), 280.0 + lead),
            eastward_wind_m_s=np.zeros((NY, NX)),
            northward_wind_m_s=np.zeros((NY, NX)),
            cycle_date=cycle_date_str,
            cycle_hour=cycle_hour,
            grid_relative_wind=True,
        )
        for lead in LEADS
    }
    hrrr_transport = FixtureHrrrTransport(
        settings=cfg.phase1.hrrr, leads=hrrr_leads, cycle_date=cycle_date_str, cycle_hour=cycle_hour
    )

    aviationweather_transport = FixtureAviationWeatherTransport()
    # The production adapters re-acquire from the network on every
    # coordinator.run() call (only artifact *registration* is
    # idempotent-by-digest, not acquisition itself) -- this test calls
    # coordinator.run() twice to prove replay/idempotency, so two
    # scripted responses are queued for each AviationWeather endpoint.
    aviationweather_transport.stationinfo_queue = [
        FakeHttpResponse(
            status_code=200,
            content=json.dumps(raw_stationinfo_records(cfg.phase1.stations)).encode(),
        )
        for _ in range(2)
    ]
    aviationweather_transport.metar_queue = [
        FakeHttpResponse(
            status_code=200, content=json.dumps(raw_metar_records_camel_case()).encode()
        )
        for _ in range(2)
    ]

    adapters = Phase1ProductionAdapters(
        configuration=cfg.phase1,
        hrrr_transport=hrrr_transport,
        aviationweather_transport=aviationweather_transport,
        aviationweather_rate_limiter=RequestRateLimiter(
            min_interval_seconds=cfg.phase1.aviationweather.min_request_interval_seconds
        ),
    )
    clock = FixedClock(NOW)
    sleeper = RecordingSleeper(clock)
    coordinator = Phase1Coordinator(
        artifact_service=service,
        sources=adapters,
        transformations=adapters,
        clock=clock,
        sleeper=sleeper,
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

    netcdf = H5NetcdfDatasetSerializer()
    parquet = ParquetTableSerializer()
    canonical_json = CanonicalJsonSerializer()

    guide = netcdf.deserialize(
        store.get_verified(
            first.forecast.canonical_guidance.storage_uri,
            first.forecast.canonical_guidance.content_digest,
        )
    )
    validate_phase1_hrrr_guidance(guide)
    base = netcdf.deserialize(
        store.get_verified(
            first.forecast.baseline.storage_uri, first.forecast.baseline.content_digest
        )
    )
    table = parquet.deserialize(
        store.get_verified(
            first.verification.matched_pairs.storage_uri,
            first.verification.matched_pairs.content_digest,
        )
    )
    report = verification_report(
        canonical_json.deserialize(
            store.get_verified(
                first.verification.report.storage_uri, first.verification.report.content_digest
            )
        )
    )
    assert guide.sizes["lead_time"] == 7 and base.sizes["location"] == 3 and table.num_rows == 21
    assert first.forecast.baseline.artifact_id != first.forecast.canonical_guidance.artifact_id
    assert guide.identical(netcdf.deserialize(netcdf.serialize(guide)))
    assert base.identical(netcdf.deserialize(netcdf.serialize(base)))
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
        parquet.deserialize(
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
    late = service.register_source(
        SourceRegistrationRequest(
            source_authority="mesoforge.synthetic",
            source_locator="fixture://late",
            source_revision="fixture.v2",
            artifact_type="late-source",
            artifact_schema_version="late-source.v1",
            media_type="application/octet-stream",
            created_at=NOW,
            availability=Availability(
                available_at=NOW + timedelta(seconds=1),
                authority="mesoforge.synthetic",
                method="fixture.v2",
            ),
            configuration_snapshot_id=req.configuration_snapshot_id,
            configuration_digest=req.configuration_digest,
            code_revision=req.code_revision,
            environment_digest=req.environment_digest,
        ),
        b"late",
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
