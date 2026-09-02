"""Offline Phase 2 multi-model acceptance proof (plan Task 14).

The network boundary is deterministic fixture data.  Everything after that
boundary runs through the production coordinator, production adapter composition,
ArtifactService, PostgreSQL repositories, standalone MinIO, and the Phase 2 pure
science modules.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
)
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.phase2 import DiscoveryResult, Phase2Coordinator, Phase2Request
from mesoforge.application.phase2_adapters import Phase2ProductionAdapters
from mesoforge.application.phase2_production import (
    Phase2ProductionProvider,
    Phase2ProductionScience,
)
from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.forecasts import validate_baseline_forecast_v2
from mesoforge.contracts.verification import MatchedPairRowV2, VerificationReportV2
from mesoforge.forecasting.contributions import BlendContributionManifest
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.validation import validate_verification_report_v2
from tests.fixtures import gfs_grib, hrrr_grib, nbm_grib
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
    RecordingSleeper,
)

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[2]
REFERENCE = datetime(2030, 8, 31, 12, tzinfo=UTC)
ISSUE = REFERENCE + timedelta(minutes=30)
CUTOFF = REFERENCE + timedelta(minutes=20)
VERIFY = REFERENCE + timedelta(hours=38)
MODELS = ("HRRR", "NBM", "GFS")
STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
HORIZONS = tuple(range(1, 37))
VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "liquid_equivalent_precipitation_amount_1h",
    "probability_of_precipitation_1h",
)
MODEL_VALUE = {"HRRR": 10.0, "NBM": 20.0, "GFS": 40.0}
JSON = CanonicalJsonSerializer()
NETCDF = H5NetcdfDatasetSerializer()


def _model_from_json(model, value):
    return model.model_validate_json(json.dumps(value, separators=(",", ":")))


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


@pytest.fixture()
def infrastructure(migrated_dsn: str) -> tuple[ArtifactService, S3ArtifactObjectStore]:
    store = S3ArtifactObjectStore(
        bucket=f"mesoforge-phase2-{uuid.uuid4().hex[:8]}",
        endpoint_url=os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100"),
        access_key=os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test"),
        secret_key=os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password"),
    )
    return (
        ArtifactService(
            unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn),
            object_store=store,
            idempotency_lock=PostgresIdempotencyLock(migrated_dsn),
        ),
        store,
    )


def _configuration(dsn: str) -> tuple[Phase2Configuration, Any]:
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    assert configuration.phase2 is not None
    snapshot = ConfigurationService(unit_of_work_factory=lambda: PostgresUnitOfWork(dsn)).register(
        configuration
    )
    return configuration.phase2, snapshot


def _request(snapshot: Any, *, run_id: str | None = None) -> Phase2Request:
    return Phase2Request(
        run_id=run_id or f"run_{uuid.uuid4()}",
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
        code_revision="2" * 40,
        environment_digest="sha256:" + "3" * 64,
        lockfile_digest="sha256:" + "4" * 64,
        target_reference_time=REFERENCE,
        forecast_issue_time=ISSUE,
        information_cutoff=CUTOFF,
        verification_cutoff=VERIFY,
    )


def _source(
    service: ArtifactService,
    request: Phase2Request,
    *,
    locator: str,
    artifact_type: str,
    schema: str,
    payload: bytes,
    available_at: datetime = REFERENCE,
) -> ArtifactManifest:
    return service.register_source(
        SourceRegistrationRequest(
            source_authority="fixture.offline",
            source_locator=locator,
            source_revision="fixture.v1",
            artifact_type=artifact_type,
            artifact_schema_version=schema,
            media_type="application/octet-stream",
            created_at=REFERENCE,
            availability=Availability(
                available_at=available_at, authority="fixture.offline", method="fixture.v1"
            ),
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
        ),
        payload,
    )


class FixtureProvider(Phase2ProductionProvider):
    """Only the external-byte boundary is replaced for offline acceptance."""

    def __init__(
        self,
        service: ArtifactService,
        configuration: Phase2Configuration,
        available: frozenset[str],
        *,
        partial_model: str | None = None,
    ) -> None:
        clock = FixedClock(REFERENCE)
        sleeper = RecordingSleeper(clock)
        super().__init__(
            configuration=configuration,
            hrrr_transport=None,  # type: ignore[arg-type]
            nbm_transport=None,  # type: ignore[arg-type]
            gfs_transport=None,  # type: ignore[arg-type]
            clock=clock,
            sleeper=sleeper,
        )
        self.service = service
        self.available = available - ({partial_model} if partial_model else set())
        self.partial_model = partial_model
        self.network_calls = 0

    def discover(self, request: Phase2Request) -> DiscoveryResult:
        acquisitions = {
            model: _fixture_acquisitions(model) for model in MODELS if model in self.available
        }
        self._acquisitions_by_run[str(request.run_id)] = acquisitions
        return DiscoveryResult(
            selection_digest=Digest.of_bytes(
                json.dumps(
                    {"available": sorted(self.available), "partial": self.partial_model}
                ).encode()
            )
        )

    # Inherit production acquire(): fixture injection ends at retained source bytes.


def _selected(variable: str, payload: bytes, ordinal: int) -> SelectedMessage:
    row = IndexRow(
        ordinal + 1, ordinal * 1000, f"{ordinal + 1}:{ordinal * 1000}:fixture:{variable}"
    )
    return SelectedMessage(variable, row, 0, len(payload), payload)


def _fixture_messages(model: str, lead: int) -> tuple[SelectedMessage, ...]:
    base = MODEL_VALUE[model]
    date = REFERENCE.strftime("%Y%m%d")
    variables: list[tuple[str, bytes]] = []
    if model == "HRRR":
        shape = (hrrr_grib.NY, hrrr_grib.NX)
        args = {"forecast_hour": lead, "cycle_date": date, "cycle_hour": REFERENCE.hour}
        variables = [
            (
                "air_temperature_2m",
                hrrr_grib.make_temperature_message(
                    values_k=np.full(shape, 270 + base + lead / 10), **args
                ),
            ),
            (
                "dew_point_temperature_2m",
                hrrr_grib.make_dew_point_message(
                    values_k=np.full(shape, 268 + base + lead / 10), **args
                ),
            ),
            (
                "eastward_wind_10m",
                hrrr_grib.make_wind_message(
                    component="u", values_m_s=np.full(shape, base / 10), grid_relative=False, **args
                ),
            ),
            (
                "northward_wind_10m",
                hrrr_grib.make_wind_message(
                    component="v", values_m_s=np.full(shape, base / 20), grid_relative=False, **args
                ),
            ),
            (
                "wind_gust_10m",
                hrrr_grib.make_gust_message(values_m_s=np.full(shape, base / 10 + 5), **args),
            ),
            (
                "liquid_equivalent_precipitation_amount_1h",
                hrrr_grib.make_apcp_message(
                    values_kg_m2=np.full(shape, base / 100 + lead / 1000), **args
                ),
            ),
        ]
    elif model == "NBM":
        shape = (nbm_grib.NY, nbm_grib.NX)
        args = {"forecast_hour": lead, "cycle_date": date, "cycle_hour": REFERENCE.hour}
        for variable, value in (
            ("air_temperature_2m", 270 + base + lead / 10),
            ("dew_point_temperature_2m", 268 + base + lead / 10),
            ("wind_speed_10m", base / 10),
            ("wind_from_direction_10m", 225.0),
            ("wind_gust_10m", base / 10 + 5),
        ):
            variables.append(
                (
                    variable,
                    nbm_grib.make_instantaneous_message(
                        canonical_variable_id=variable, values=np.full(shape, value), **args
                    ),
                )
            )
        variables.extend(
            (
                (
                    "liquid_equivalent_precipitation_amount_1h",
                    nbm_grib.make_apcp_deterministic_message(
                        values_kg_m2=np.full(shape, base / 100 + lead / 1000), **args
                    ),
                ),
                (
                    "probability_of_precipitation_1h",
                    nbm_grib.make_pop01_message(
                        values_percent=np.full(shape, min(99, 20 + lead)), **args
                    ),
                ),
            )
        )
    else:
        shape = (gfs_grib.NY, gfs_grib.NX)
        args = {"forecast_hour": lead, "cycle_date": date, "cycle_hour": REFERENCE.hour}
        for variable, value in (
            ("air_temperature_2m", 270 + base + lead / 10),
            ("dew_point_temperature_2m", 268 + base + lead / 10),
            ("eastward_wind_10m", base / 10),
            ("northward_wind_10m", base / 20),
            ("wind_gust_10m", base / 10 + 5),
        ):
            variables.append(
                (
                    variable,
                    gfs_grib.make_instantaneous_message(
                        canonical_variable_id=variable,
                        values=np.full(shape, value),
                        grid_relative_wind=False,
                        **args,
                    ),
                )
            )
        bucket_start = 6 * ((lead - 1) // 6) if lead else 0
        apcp = gfs_grib.make_apcp_message(
            start_step=bucket_start,
            end_step=lead,
            values_kg_m2=np.full(shape, (lead - bucket_start) * (base / 100)),
            cycle_date=date,
            cycle_hour=REFERENCE.hour,
        )
        variables.append(("liquid_equivalent_precipitation_amount_1h", apcp))
        if lead <= 5:
            variables.append(("liquid_equivalent_precipitation_amount_1h", apcp))
    return tuple(_selected(variable, payload, i) for i, (variable, payload) in enumerate(variables))


def _fixture_acquisitions(model: str) -> tuple[Phase2LeadAcquisition, ...]:
    leads = (0, *HORIZONS) if model == "GFS" else HORIZONS
    return tuple(
        Phase2LeadAcquisition(
            model=model.lower(),
            cycle_date=REFERENCE.date(),
            cycle_hour=REFERENCE.hour,
            forecast_hour=lead,
            endpoint="fixture.offline",
            resolved_grib_url=f"fixture://{model.lower()}/{REFERENCE:%Y%m%d%H}/f{lead:03d}",
            resolved_index_url=f"fixture://{model.lower()}/{REFERENCE:%Y%m%d%H}/f{lead:03d}.idx",
            index_payload=f"fixture index {model} {lead}".encode(),
            index_attempts=(),
            index_completed_at=REFERENCE,
            selected_messages=_fixture_messages(model, lead),
            grib_attempts=(),
            grib_completed_at=REFERENCE,
            full_object_etag="fixture",
            full_object_last_modified=None,
            full_object_content_length=sum(len(m.payload) for m in _fixture_messages(model, lead)),
        )
        for lead in leads
    )


def _raw_metar_records() -> list[dict[str, object]]:
    sites = {
        "KCBG": (45.557, -93.264, 285.0),
        "KJMR": (45.88854, -93.269, 301.0),
        "KROS": (45.69624, -92.95427, 282.0),
    }
    rows = []
    for icao, (lat, lon, elev) in sites.items():
        for horizon in HORIZONS:
            if icao == "KROS" and horizon == 36:
                continue
            event = REFERENCE + timedelta(hours=horizon)
            rows.append(
                {
                    "icaoId": icao,
                    "obsTime": int(event.timestamp()),
                    "reportTime": event.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "receiptTime": (event + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "temp": 6.85 + horizon / 10,
                    "dewp": 5.0,
                    "wdir": 180.0,
                    "wspd": 10.0,
                    "wgst": 15.0,
                    "qcField": 0.0,
                    "metarType": "METAR",
                    "rawOb": f"METAR {icao} 000000Z P0001",
                    "lat": lat,
                    "lon": lon,
                    "elev": elev,
                }
            )
    return rows


def _coordinator(service, provider, config):
    aviation = FixtureAviationWeatherTransport()
    response = FakeHttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=JSON.serialize(_raw_metar_records()),
    )
    aviation.metar_queue = [
        FakeHttpResponse(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
        )
        for _ in range(4)
    ]
    clock = FixedClock(REFERENCE)
    sleeper = RecordingSleeper(clock)
    science = Phase2ProductionScience(
        configuration=config,
        provider=provider,
        aviationweather_transport=aviation,
        clock=clock,
        sleeper=sleeper,
    )
    adapters = Phase2ProductionAdapters(
        artifact_service=service, providers=provider, science=science.operations()
    )
    return Phase2Coordinator(
        artifact_service=service,
        discovery=adapters,
        acquisition=adapters,
        normalization=adapters,
        alignment=adapters,
        availability=adapters,
        forecast=adapters,
        correction=adapters,
        observations=adapters,
        matching=adapters,
        verification=adapters,
    )


def _payload(store, manifest, serializer=JSON):
    return serializer.deserialize(store.get_verified(manifest.storage_uri, manifest.content_digest))


def _ancestors(dsn: str, artifact_id: ArtifactId) -> set[str]:
    query = sa.text(
        """WITH RECURSIVE a(id) AS (
        SELECT CAST(:root AS uuid) UNION
        SELECT ai.artifact_id FROM activity_outputs ao
        JOIN activity_inputs ai ON ai.activity_id=ao.activity_id
        JOIN a ON a.id=ao.artifact_id)
        SELECT 'art_'||id::text FROM a"""
    )
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(query, {"root": str(artifact_id).removeprefix("art_")}).scalars()
            )
    finally:
        engine.dispose()


SCENARIOS = [
    (frozenset(), "complete"),
    (frozenset({"HRRR"}), "degraded"),
    (frozenset({"NBM"}), "degraded"),
    (frozenset({"GFS"}), "degraded"),
    (frozenset({"HRRR", "NBM"}), "degraded"),
    (frozenset({"HRRR", "GFS"}), "degraded"),
    (frozenset({"NBM", "GFS"}), "degraded"),
    (frozenset(MODELS), "invalid"),
]


@pytest.mark.parametrize("failed,expected_state", SCENARIOS)
def test_complete_source_failure_matrix_uses_only_approved_rows(
    infrastructure, migrated_dsn, monkeypatch, failed, expected_state
):
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot)
    available = frozenset(MODELS) - failed
    provider = FixtureProvider(service, config, available)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    coordinator = _coordinator(service, provider, config)
    if not available:
        with pytest.raises(ValueError, match="normalized guidance must not be empty"):
            coordinator.run(request)
        return
    result = coordinator.run(request)
    report = _payload(store, result.availability.report)
    assert report["run_state"] == expected_state
    assert report["models"] == [model for model in MODELS if model in available]
    entries = report["entries"]
    for entry in entries:
        if entry["variable"] == "probability_of_precipitation_1h":
            assert entry["state"] == ("complete" if "NBM" in available else "unavailable")
            assert entry["weights"] is None
            continue
        table = (
            config.blend_configuration.qpf_table
            if entry["variable"] == "liquid_equivalent_precipitation_amount_1h"
            else config.blend_configuration.scalar_vector_table
        )
        configured = table.row_for(
            available_models=tuple(model for model in MODELS if model in available),
            horizon=entry["horizon"],
        )
        # Independent literal equality: this fails if preferred weights are
        # silently renormalized instead of selecting the configured fallback row.
        assert tuple(entry["weights"]) == configured.weights
        assert entry["row_id"] == str(configured.row_id)
    baseline = _payload(store, result.corrected_forecast.baseline, NETCDF)
    validate_baseline_forecast_v2(baseline)
    if "NBM" not in available:
        assert np.isnan(baseline.probability_of_precipitation_1h.values).all()
        assert np.all(baseline.probability_of_precipitation_1h_state.values == 2)
        # A nonzero deterministic QPF proves PoP was not synthesized from QPF.
        assert np.all(baseline.liquid_equivalent_precipitation_amount_1h.values > 0)
        pairs = [
            _model_from_json(MatchedPairRowV2, row)
            for row in _payload(store, result.matching.matched_pairs)["rows"]
        ]
        for pair in pairs:
            assert pair.temperature_availability_state == "fallback"
            assert pair.wind_speed_availability_state == "fallback"
            assert pair.gust_availability_state == "fallback"
            assert pair.qpf_availability_state == "fallback"
            assert pair.pop_availability_state == "unavailable"
        verification = _model_from_json(
            VerificationReportV2, _payload(store, result.verification.report)
        )
        availability_rows = {
            (row.stratum_value, row.metric_name): row
            for row in verification.rows
            if row.stratum_kind == "by_availability_state"
        }
        for metric in ("temperature_mae", "wind_speed_mae", "gust_mae", "qpf_mae"):
            assert availability_rows[("fallback", metric)].sample_count > 0
            assert availability_rows[("unavailable", metric)].sample_count == 0
        assert availability_rows[("fallback", "pop_brier_score")].sample_count == 0
        unavailable_pop = availability_rows[("unavailable", "pop_brier_score")]
        assert unavailable_pop.sample_count == 0
        assert unavailable_pop.missing_counts["forecast_missing_or_invalid"] > 0


def test_complete_cycle_proves_configuration_lineage_replay_and_concurrency(
    infrastructure, migrated_dsn
):
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id="run_00000000-0000-0000-0000-000000000014")
    provider = FixtureProvider(service, config, frozenset(MODELS))
    coordinator = _coordinator(service, provider, config)
    first = coordinator.run(request)

    assert config.hrrr.allowed_cycle_hours == (0, 6, 12, 18)
    assert config.hrrr.max_age_hours == 6.0
    assert config.nbm.max_age_hours == 3.0
    assert config.gfs.max_age_hours == 12.0
    assert config.cycle_selection_policy.target_horizons == HORIZONS
    assert not any(
        (
            config.rrfs_enabled,
            config.ai_adjustment_enabled,
            config.learned_weights_enabled,
            config.publication_enabled,
        )
    )
    assert len(first.selected_inputs.source_roots) > 3 * 36 * 2
    assert all(
        root.artifact.availability.available_at <= CUTOFF
        for root in first.selected_inputs.source_roots
    )
    assert tuple(root.model for root in first.selected_inputs.source_roots[:36]) == ("HRRR",) * 36
    assert len(first.normalized.artifacts) == 3
    aligned = _payload(store, first.alignment.aligned_guidance)
    assert aligned["models"] == list(MODELS) and aligned["shape"] == [3, 36, 3]

    contributions = _model_from_json(
        BlendContributionManifest,
        _payload(store, first.atomic_forecast.contribution_manifest),
    )
    assert len(contributions.rows) == 7 * 3 * 36
    for row in contributions.rows:
        assert sum(item.weighted_contribution for item in row.contributors) == pytest.approx(
            row.unrounded_sum, abs=1e-12
        )
    pairs = [
        _model_from_json(MatchedPairRowV2, row)
        for row in _payload(store, first.matching.matched_pairs)["rows"]
    ]
    assert len(pairs) == 108
    missing = next(
        row
        for row in pairs
        if str(row.station_id) == "station.kros" and row.target_horizon_hours == 36
    )
    assert missing.temperature_status == "no_report_within_tolerance"
    report = _model_from_json(VerificationReportV2, _payload(store, first.verification.report))
    validate_verification_report_v2(report)
    assert report.rows

    lineage = _ancestors(migrated_dsn, first.verification.report.artifact_id)
    required = {
        str(first.selected_inputs.station_snapshot.artifact_id),
        str(first.observations.responses[0].artifact_id),
        *(str(root.artifact.artifact_id) for root in first.selected_inputs.source_roots),
    }
    assert required <= lineage
    assert all(
        store.get_verified(root.artifact.storage_uri, root.artifact.content_digest).startswith(
            b"GRIB"
        )
        for root in first.selected_inputs.source_roots
        if root.field_role.startswith("message:")
    )

    physical = coordinator.replay(request, first.selected_inputs)
    assert physical.verification.report.artifact_id == first.verification.report.artifact_id
    assert (
        physical.atomic_forecast.activity.activity_id == first.atomic_forecast.activity.activity_id
    )
    assert _payload(store, physical.verification.report) == _payload(
        store, first.verification.report
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = tuple(
            pool.map(lambda _: coordinator.replay(request, first.selected_inputs), range(2))
        )
    assert {item.verification.report.artifact_id for item in concurrent} == {
        first.verification.report.artifact_id
    }
    assert {item.atomic_forecast.activity.activity_id for item in concurrent} == {
        first.atomic_forecast.activity.activity_id
    }
    assert provider.network_calls == 0
    forbidden = ("mesoforge.rrfs", "mesoforge.ai", "mesoforge.bias", "mesoforge.publication")
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules
        for prefix in forbidden
    )


def test_partial_cycle_rejects_whole_model_without_splicing(infrastructure, migrated_dsn):
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    provider = FixtureProvider(service, config, frozenset(MODELS), partial_model="HRRR")
    result = _coordinator(service, provider, config).run(_request(snapshot))
    assert all(root.model != "HRRR" for root in result.selected_inputs.source_roots)
    aligned = _payload(store, result.alignment.aligned_guidance)
    assert aligned["models"] == ["NBM", "GFS"]
    report = _payload(store, result.availability.report)
    assert report["run_state"] == "degraded"
    assert all("HRRR" not in entry["models"] for entry in report["entries"])
