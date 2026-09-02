"""Offline Phase 2 multi-model acceptance proof (plan Task 14).

The **only** substitution is deterministic fixture *bytes* at the real
``HttpTransport`` GET/HEAD/range boundary (Codex re-review finding 1).
``Phase2ProductionProvider.discover()`` and ``acquire()`` run completely
unchanged, so candidate discovery, index retrieval/parsing, selector
matching and ambiguity handling, HEAD/Content-Length framing, byte-range
framing and GRIB2 integrity validation, and retry/deadline/cutoff policy
are all genuinely exercised. Everything after that boundary runs through
the production coordinator, production adapter composition,
ArtifactService, PostgreSQL repositories, standalone MinIO, and the
Phase 2 pure science modules.
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

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.phase2 import Phase2Coordinator, Phase2Request
from mesoforge.application.phase2_adapters import Phase2ProductionAdapters
from mesoforge.application.phase2_production import (
    Phase2ProductionProvider,
    Phase2ProductionScience,
    Phase2UnavailableProvider,
    UnavailableHttpTransport,
    build_phase2_replay_adapters,
)
from mesoforge.application.phase2_replay import (
    Phase2ReplayContractError,
    Phase2ReplayIdentityError,
    parse_run_spec,
)
from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.forecasts import validate_baseline_forecast_v2
from mesoforge.contracts.lineage_v2 import VariableLineageManifestV2
from mesoforge.contracts.verification import MatchedPairRowV2, VerificationReportV2
from mesoforge.forecasting.contributions import BlendContributionManifest
from mesoforge.guidance.canonical_v2 import validate_canonical_guidance_lineage_v2
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.validation import validate_verification_report_v2
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
    RecordingSleeper,
)
from tests.support.phase2_provider_transports import (
    FrozenSleeper,
    UnavailableTransport,
    build_gfs_transport,
    build_hrrr_transport,
    build_nbm_transport,
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
# The provider selects the current cycle, so target horizons 1..36 map
# directly onto source leads 1..36; GFS additionally acquires lead 0's
# APCP as the same-bucket previous-hour parent for lead 1.
HRRR_LEADS = HORIZONS
NBM_LEADS = HORIZONS
GFS_LEADS = (0, *HORIZONS)
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
        additional_overlay_paths=(
            ROOT / "configs/phase2-grasston.yaml",
            # Approved reduced NBM fixture grid (see the overlay's own
            # header): the exact operational projection at a fixture
            # scale, so synthetic eccodes messages stay small while the
            # grid contract is still enforced exactly.
            ROOT / "tests/fixtures/phase2-fixture-grid-overlay.yaml",
        ),
    )
    assert configuration.phase2 is not None
    assert configuration.phase2.nbm.grid_profile.profile_id == "nbm-core-conus-fixture.v1", (
        "the acceptance overlay must select the approved fixture grid profile"
    )
    snapshot = ConfigurationService(unit_of_work_factory=lambda: PostgresUnitOfWork(dsn)).register(
        configuration
    )
    return configuration.phase2, snapshot


def _request(snapshot: Any, *, run_id: str | None = None, cutoff: datetime = CUTOFF):
    return Phase2Request(
        run_id=run_id or f"run_{uuid.uuid4()}",
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
        code_revision="2" * 40,
        environment_digest="sha256:" + "3" * 64,
        lockfile_digest="sha256:" + "4" * 64,
        target_reference_time=REFERENCE,
        forecast_issue_time=ISSUE,
        information_cutoff=cutoff,
        verification_cutoff=VERIFY,
    )


def _transports(
    config: Phase2Configuration,
    available: frozenset[str],
    *,
    fault_scripts: dict[str, dict[str, list[Any]]] | None = None,
) -> dict[str, Any]:
    """Build the three provider transports.

    A model that is not ``available`` gets a transport whose every
    request 404s -- the real shape of an unpublished cycle. Production
    discovery must then omit it entirely rather than splicing a partial
    cycle, and that decision is made by production code, not by the test.
    """
    faults = fault_scripts or {}
    if "HRRR" in available:
        hrrr = build_hrrr_transport(
            config.hrrr,
            cycle=REFERENCE,
            leads=HRRR_LEADS,
            base_value=MODEL_VALUE["HRRR"],
            fault_script=faults.get("HRRR"),
        )
    else:
        hrrr = UnavailableTransport()
    if "NBM" in available:
        nbm = build_nbm_transport(
            config.nbm,
            cycle=REFERENCE,
            leads=NBM_LEADS,
            base_value=MODEL_VALUE["NBM"],
            fault_script=faults.get("NBM"),
        )
    else:
        nbm = UnavailableTransport()
    if "GFS" in available:
        gfs = build_gfs_transport(
            config.gfs,
            cycle=REFERENCE,
            leads=GFS_LEADS,
            base_value=MODEL_VALUE["GFS"],
            fault_script=faults.get("GFS"),
        )
    else:
        gfs = UnavailableTransport()
    return {"HRRR": hrrr, "NBM": nbm, "GFS": gfs}


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


def _build(service, config, available, *, fault_scripts=None):
    """Compose the production provider/science/coordinator, injecting
    only scripted transports at the network boundary."""
    transports = _transports(config, available, fault_scripts=fault_scripts)
    # The clock sits at the information cutoff: every acquisition this
    # run performs is therefore genuinely available by the cutoff, and a
    # late one would be rejected by production's own cutoff check. The
    # sleeper records backoffs without advancing that clock (see
    # FrozenSleeper) so a source-availability scenario stays a
    # source-availability scenario.
    clock = FixedClock(CUTOFF)
    sleeper = FrozenSleeper()
    provider = Phase2ProductionProvider(
        configuration=config,
        hrrr_transport=transports["HRRR"],
        nbm_transport=transports["NBM"],
        gfs_transport=transports["GFS"],
        clock=clock,
        sleeper=sleeper,
    )
    aviation = FixtureAviationWeatherTransport()
    content = JSON.serialize(_raw_metar_records())
    aviation.metar_queue = [
        FakeHttpResponse(
            status_code=200, headers={"Content-Type": "application/json"}, content=content
        )
        for _ in range(16)
    ]
    science = Phase2ProductionScience(
        configuration=config,
        aviationweather_transport=aviation,
        clock=clock,
        sleeper=sleeper,
    )
    adapters = Phase2ProductionAdapters(
        artifact_service=service, providers=provider, science=science.operations()
    )
    coordinator = Phase2Coordinator(
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
    return coordinator, provider, transports


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
    coordinator, _provider, _transports_by_model = _build(service, config, available)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
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
    coordinator, provider, transports = _build(service, config, frozenset(MODELS))
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

    # Codex re-review finding 1: the real acquisition boundary ran. The
    # provider issued genuine index GETs, HEAD requests, and ranged GETs
    # carrying exact Range headers, and every retained payload is a
    # complete GRIB2 message the range machinery validated.
    for model in MODELS:
        transport = transports[model]
        assert transport.head_calls, f"{model} never issued a HEAD for the full-object length"
        assert transport.range_headers, f"{model} never issued a ranged GET"
        assert any(url.endswith(".idx") for url, _headers in transport.get_calls), (
            f"{model} never retrieved a provider index"
        )
        assert all(header.startswith("bytes=") for header in transport.range_headers)

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

    # Replay is proved *independently* below in
    # ``test_replay_is_reproduced_from_persisted_roots_alone``: a fresh
    # provider/science/coordinator, mutated live configuration, and
    # raising transports. Here we only confirm the same-process replay
    # is idempotent and concurrency-safe.
    recorded = first.observations.responses
    physical = coordinator.replay(request, first.selected_inputs, recorded)
    assert physical.verification.report.artifact_id == first.verification.report.artifact_id
    assert (
        physical.atomic_forecast.activity.activity_id == first.atomic_forecast.activity.activity_id
    )
    assert _payload(store, physical.verification.report) == _payload(
        store, first.verification.report
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = tuple(
            pool.map(
                lambda _: coordinator.replay(request, first.selected_inputs, recorded), range(2)
            )
        )
    assert {item.verification.report.artifact_id for item in concurrent} == {
        first.verification.report.artifact_id
    }
    assert {item.atomic_forecast.activity.activity_id for item in concurrent} == {
        first.atomic_forecast.activity.activity_id
    }
    # Replay never touches the network again: no additional provider or
    # observation request was issued after the first run's acquisition.
    calls_after = {model: len(transports[model].get_calls) for model in MODELS}
    coordinator.replay(request, first.selected_inputs, recorded)
    assert {model: len(transports[model].get_calls) for model in MODELS} == calls_after

    forbidden = ("mesoforge.rrfs", "mesoforge.ai", "mesoforge.bias", "mesoforge.publication")
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules
        for prefix in forbidden
    )
    _ = provider


def _replay_coordinator(service) -> Phase2Coordinator:
    """A coordinator that can *only* replay.

    Codex review ``t_652b155e``: this is the independent proof harness.
    It shares nothing with the run that produced the artifacts -- no
    provider instance, no acquisition cache, no ``Phase2Configuration``
    at all -- and its provider/transport wiring raises on every network
    or discovery attempt. Anything it reproduces therefore came from
    persisted bytes.
    """
    adapters = build_phase2_replay_adapters(
        artifact_service=service,
        clock=FixedClock(VERIFY + timedelta(days=365)),
        sleeper=FrozenSleeper(),
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


def test_replay_is_reproduced_from_persisted_roots_alone(infrastructure, migrated_dsn, monkeypatch):
    """Codex review ``t_652b155e``: a replay must be reconstructible
    from the run's immutable persisted roots by a process that shares
    nothing with the original run.

    The replay coordinator is built with a brand-new science instance,
    no configuration object whatsoever, a provider that refuses
    discovery/acquisition, and a transport that raises on every request.
    The live configuration used for the original run is then deliberately
    mutated (different stations, matching tolerance, metric set, blend
    weights) *before* replay -- and the replay must still be physically
    and logically identical, because it never reads that object.
    """
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id=f"run_{uuid.uuid4()}")
    coordinator, _provider, _transports_by_model = _build(service, config, frozenset(MODELS))
    first = coordinator.run(request)

    # Every subsequent socket attempt is fatal: replay must be offline.
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )

    # Mutate the live configuration in ways that would visibly change
    # alignment, availability, blending, matching, and verification if
    # any stage still read it.
    mutated = config.model_copy(
        update={
            "matching_policy": config.matching_policy.model_copy(
                update={"tolerance_minutes": 1.0, "calm_threshold_m_s": 99.0}
            ),
            "metric_set": config.metric_set.model_copy(
                update={"metric_names": ("temperature_mae",)}
            ),
            "stations": tuple(
                station.model_copy(
                    update={
                        "expected_latitude": station.expected_latitude + 0.75,
                        "expected_longitude": station.expected_longitude - 0.75,
                        "expected_elevation_m": station.expected_elevation_m + 500.0,
                    }
                )
                for station in config.stations
            ),
        }
    )
    assert mutated.stations != config.stations

    replay_coordinator = _replay_coordinator(service)
    replayed = replay_coordinator.replay(
        request, first.selected_inputs, first.observations.responses
    )

    # Physical equality: the same artifact and activity identities.
    assert replayed.verification.report.artifact_id == first.verification.report.artifact_id
    assert replayed.matching.matched_pairs.artifact_id == first.matching.matched_pairs.artifact_id
    assert (
        replayed.corrected_forecast.baseline.artifact_id
        == first.corrected_forecast.baseline.artifact_id
    )
    assert replayed.alignment.aligned_guidance.artifact_id == (
        first.alignment.aligned_guidance.artifact_id
    )
    assert tuple(a.artifact_id for a in replayed.normalized.artifacts) == tuple(
        a.artifact_id for a in first.normalized.artifacts
    )
    assert (
        replayed.atomic_forecast.activity.activity_id == first.atomic_forecast.activity.activity_id
    )
    # Observations were reused, not re-fetched.
    assert tuple(r.artifact_id for r in replayed.observations.responses) == tuple(
        r.artifact_id for r in first.observations.responses
    )

    # Logical equality: byte-identical payloads at every derived stage.
    for stage_first, stage_replayed, serializer in (
        (first.alignment.aligned_guidance, replayed.alignment.aligned_guidance, JSON),
        (first.availability.report, replayed.availability.report, JSON),
        (first.corrected_forecast.baseline, replayed.corrected_forecast.baseline, NETCDF),
        (first.observations.normalized, replayed.observations.normalized, JSON),
        (first.matching.matched_pairs, replayed.matching.matched_pairs, JSON),
        (first.verification.report, replayed.verification.report, JSON),
    ):
        assert stage_first.content_digest == stage_replayed.content_digest
        if serializer is JSON:
            assert _payload(store, stage_first) == _payload(store, stage_replayed)


def test_replay_rejects_a_request_that_conflicts_with_the_persisted_run_spec(
    infrastructure, migrated_dsn
):
    """A caller-supplied request is checked against the run spec, never
    trusted: a different verification cutoff, seed, or lockfile digest
    fails closed before any stage runs."""
    service, _store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id=f"run_{uuid.uuid4()}")
    coordinator, _provider, _transports_by_model = _build(service, config, frozenset(MODELS))
    first = coordinator.run(request)
    replay_coordinator = _replay_coordinator(service)
    recorded = first.observations.responses

    for changes, expected in (
        ({"verification_cutoff": VERIFY + timedelta(hours=1)}, "verification_cutoff"),
        ({"random_seed": 7}, "random_seed"),
        ({"lockfile_digest": "sha256:" + "5" * 64}, "lockfile_digest"),
        ({"forecast_issue_time": ISSUE + timedelta(minutes=5)}, "forecast_issue_time"),
    ):
        conflicting = Phase2Request.model_validate({**request.model_dump(), **changes}, strict=True)
        with pytest.raises(Phase2ReplayIdentityError, match=expected):
            replay_coordinator.replay(conflicting, first.selected_inputs, recorded)


def test_replay_rejects_pinned_roots_and_observations_that_the_run_never_consumed(
    infrastructure, migrated_dsn
):
    """Pinned source roots and recorded observations must match the
    persisted run spec exactly; dropping or substituting either fails
    closed."""
    service, _store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id=f"run_{uuid.uuid4()}")
    coordinator, _provider, _transports_by_model = _build(service, config, frozenset(MODELS))
    first = coordinator.run(request)
    replay_coordinator = _replay_coordinator(service)
    recorded = first.observations.responses

    dropped = first.selected_inputs.model_copy(
        update={"source_roots": first.selected_inputs.source_roots[1:]}
    )
    with pytest.raises(Phase2ReplayIdentityError, match="do not match the persisted run spec"):
        replay_coordinator.replay(request, dropped, recorded)

    # Substituting the station snapshot for the run spec (a valid
    # artifact of this very run, but in the wrong role) is rejected by
    # the artifact-role contract before anything is parsed.
    swapped = first.selected_inputs.model_copy(
        update={"run_spec": first.selected_inputs.station_snapshot}
    )
    with pytest.raises(Phase2ReplayContractError):
        replay_coordinator.replay(request, swapped, recorded)

    with pytest.raises(Phase2ReplayIdentityError, match="at least one recorded observation"):
        replay_coordinator.replay(request, first.selected_inputs, ())

    # A genuine artifact of this run that is not an observation response.
    with pytest.raises(Phase2ReplayIdentityError, match="artifact type"):
        replay_coordinator.replay(
            request, first.selected_inputs, (first.selected_inputs.station_snapshot,)
        )


def test_replay_rejects_a_conflicting_live_configuration(infrastructure, migrated_dsn):
    """When a live configuration *is* wired in, it must agree with the
    run's persisted configuration identity or replay fails closed --
    a silently-different policy can never be applied to a replay."""
    service, _store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id=f"run_{uuid.uuid4()}")
    coordinator, _provider, _transports_by_model = _build(service, config, frozenset(MODELS))
    first = coordinator.run(request)

    conflicting = config.model_copy(
        update={
            "matching_policy": config.matching_policy.model_copy(update={"tolerance_minutes": 1.0})
        }
    )
    science = Phase2ProductionScience(
        configuration=conflicting,
        aviationweather_transport=UnavailableHttpTransport(),
        clock=FixedClock(CUTOFF),
        sleeper=FrozenSleeper(),
    )
    adapters = Phase2ProductionAdapters(
        artifact_service=service,
        providers=Phase2UnavailableProvider(),
        science=science.operations(),
    )
    replay_coordinator = Phase2Coordinator(
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
    with pytest.raises(Phase2ReplayIdentityError, match="conflicts with the persisted run"):
        replay_coordinator.replay(request, first.selected_inputs, first.observations.responses)


def test_persisted_run_spec_carries_every_lead_acquisition_evidence(infrastructure, migrated_dsn):
    """Normalization no longer depends on a process-local acquisition
    cache, so the run spec must itself carry every lead's endpoint,
    resolved URLs, and per-message inventory evidence."""
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id=f"run_{uuid.uuid4()}")
    coordinator, _provider, _transports_by_model = _build(service, config, frozenset(MODELS))
    result = coordinator.run(request)

    spec = parse_run_spec(
        store.get_verified(
            result.selected_inputs.run_spec.storage_uri,
            result.selected_inputs.run_spec.content_digest,
        )
    )
    # The persisted configuration reproduces the live one exactly.
    assert spec.configuration == config

    pinned_roles = {
        (root.model, root.source_lead_hours, root.field_role)
        for root in result.selected_inputs.source_roots
    }
    persisted_roles: set[tuple[str, int, str]] = set()
    for model in MODELS:
        group = spec.group(model)
        assert group.selected
        assert group.source_cycle_reference_time == REFERENCE
        # The persisted leads are exactly the leads that were actually
        # acquired for this model -- derived from the registered roots
        # rather than restated here, so the assertion cannot drift from
        # production's own acquisition policy.
        expected_leads = sorted(
            {
                root.source_lead_hours
                for root in result.selected_inputs.source_roots
                if root.model == model
            }
        )
        assert list(group.source_lead_hours) == expected_leads
        for lead in group.leads:
            assert lead.endpoint
            assert lead.resolved_index_url.startswith("http")
            assert lead.resolved_grib_url.startswith("http")
            assert lead.index_completed_at <= CUTOFF
            assert lead.grib_completed_at <= CUTOFF
            persisted_roles.add((model, lead.source_lead_hours, "index"))
            for message in lead.selected_messages:
                assert message.inventory_row
                assert message.byte_end > message.byte_start
                persisted_roles.add((model, lead.source_lead_hours, message.field_role))
    # Every registered root is described, and nothing extra is claimed.
    assert persisted_roles == pinned_roles
    # Target horizons 1..36 are covered for every selected model.
    assert set(spec.target_horizons) == set(HORIZONS)


def test_partial_cycle_rejects_whole_model_without_splicing(infrastructure, migrated_dsn):
    """A model whose mid-range lead is permanently missing (404 past its
    completion deadline) must be dropped whole. Production decides this
    from real 404 responses; the test never removes the model itself."""
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    # HRRR lead 18 is never published on either endpoint, so every HRRR
    # candidate cycle fails acquisition and HRRR is absent from the run.
    faults = {
        "HRRR": {
            "wrfsfcf18.grib2": [FakeHttpResponse(status_code=404) for _ in range(64)],
        }
    }
    coordinator, _provider, _transports = _build(
        service, config, frozenset(MODELS), fault_scripts=faults
    )
    result = coordinator.run(_request(snapshot))
    assert all(root.model != "HRRR" for root in result.selected_inputs.source_roots)
    aligned = _payload(store, result.alignment.aligned_guidance)
    assert aligned["models"] == ["NBM", "GFS"]
    report = _payload(store, result.availability.report)
    assert report["run_state"] == "degraded"
    assert all("HRRR" not in entry["models"] for entry in report["entries"])


def test_production_creates_complete_variable_lineage_for_every_model(infrastructure, migrated_dsn):
    """Codex re-review finding 3: every model's canonical artifact must
    reference a real, complete ``variable-lineage.v2`` manifest -- not a
    raw message artifact, and not a GFS-QPF-only lineage record."""
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    coordinator, _provider, _transports = _build(service, config, frozenset(MODELS))
    result = coordinator.run(_request(snapshot))

    engine = sa.create_engine(migrated_dsn)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT 'art_'||a.id::text, a.artifact_type, a.artifact_schema_version, "
                    "s.storage_uri, a.content_digest FROM artifacts a "
                    "JOIN stored_objects s ON s.content_digest = a.content_digest"
                )
            ).all()
    finally:
        engine.dispose()
    by_id = {row[0]: row for row in rows}

    seen_models: set[str] = set()
    for artifact in result.normalized.artifacts:
        dataset = _payload(store, artifact, NETCDF)
        model = str(dataset.attrs["model"])
        seen_models.add(model)
        # The grid must name this model, never another's.
        assert dataset.attrs["grid_id"] == f"phase2-{model}.v1"

        lineage_id = str(dataset.attrs["variable_lineage_manifest_id"])
        assert lineage_id in by_id, "lineage reference must resolve to a registered artifact"
        _id, artifact_type, schema_version, storage_uri, content_digest = by_id[lineage_id]
        assert artifact_type == "variable-lineage"
        assert schema_version == "variable-lineage.v2"

        manifest = VariableLineageManifestV2.model_validate_json(
            store.get_verified(storage_uri, content_digest)
        )
        assert manifest.model == model
        assert str(manifest.grid_id) == f"phase2-{model}.v1"
        assert str(manifest.configuration_snapshot_id) == str(
            dataset.attrs["configuration_snapshot_id"]
        )
        dataset_leads = {
            int(value / np.timedelta64(1, "h")) for value in dataset["source_lead_time"].values
        }
        assert set(manifest.expected_source_lead_hours) == dataset_leads
        # The lineage covers the model's source fields. NBM's canonical
        # U/V are derived from its published speed/direction pair, so
        # that pair -- not the derived components -- is what its lineage
        # names. Validate through the production contract rather than
        # restating the rule here.
        validate_canonical_guidance_lineage_v2(
            dataset,
            lineage_manifest=manifest,
            lineage_artifact_id=lineage_id,
            lineage_artifact_type=artifact_type,
            lineage_schema_version=schema_version,
        )
        # Every entry names real evidence, not a placeholder.
        for entry in manifest.entries:
            assert entry.selected_grib_artifact_ids
            assert all(str(a) in by_id for a in entry.selected_grib_artifact_ids)
            assert str(entry.index_artifact_id) in by_id
            assert entry.inventory_rows and all(row for row in entry.inventory_rows)
            assert entry.selector_expression
            assert all(end > start for start, end in entry.byte_ranges)
            assert entry.resolved_grib_url.startswith("http")
    assert seen_models == {"hrrr", "nbm", "gfs"}


def test_run_spec_and_station_snapshot_carry_complete_replay_inputs(infrastructure, migrated_dsn):
    """Codex re-review finding 5: replay must be reconstructible from
    persisted bytes, so the run spec and station snapshot must record
    every input downstream science consumes."""
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot)
    coordinator, _provider, _transports = _build(service, config, frozenset(MODELS))
    result = coordinator.run(request)

    run_spec = _payload(store, result.selected_inputs.run_spec)
    assert run_spec["schema_version"] == "phase2-run-spec.v1"
    assert run_spec["verification_cutoff"] == request.verification_cutoff.isoformat()
    assert run_spec["information_cutoff"] == request.information_cutoff.isoformat()
    assert run_spec["forecast_issue_time"] == request.forecast_issue_time.isoformat()
    assert run_spec["target_horizons"] == list(HORIZONS)
    assert run_spec["cycle_selection_policy"]["target_horizons"] == list(HORIZONS)
    assert run_spec["request_digests"]["configuration_digest"] == str(request.configuration_digest)
    assert run_spec["request_digests"]["lockfile_digest"] == str(request.lockfile_digest)
    assert run_spec["configuration_digests"]["phase2_configuration"]
    assert run_spec["configuration_digests"]["matching_policy"]

    groups = {group["model"]: group for group in run_spec["required_groups"]}
    assert set(groups) == set(MODELS)
    for model in MODELS:
        group = groups[model]
        assert group["selected"] is True
        assert group["source_cycle_reference_time"] == REFERENCE.isoformat()
        assert group["source_lead_hours"]
        assert group["endpoints"]
        assert group["canonical_variable_ids"]
        assert group["cycle_completion_deadline_minutes"] > 0

    stations = _payload(store, result.selected_inputs.station_snapshot)
    assert stations["schema_version"] == "station-catalog-snapshot.v1"
    assert stations["station_ids"] == list(STATIONS)
    by_id = {row["station_id"]: row for row in stations["stations"]}
    assert set(by_id) == set(STATIONS)
    for station in config.stations:
        row = by_id[str(station.station_id)]
        # Exact coordinates/elevation/provider identity, as consumed by
        # bilinear alignment and METAR matching.
        assert row["expected_latitude"] == station.expected_latitude
        assert row["expected_longitude"] == station.expected_longitude
        assert row["expected_elevation_m"] == station.expected_elevation_m
        assert row["provider_icao_id"] == station.provider_icao_id
        assert row["site_name"] == station.site_name
        assert row["provider_site_types"] == list(station.provider_site_types)
        assert row["provider_priority"] == station.provider_priority
    assert stations["point_extraction_policy"]["policy_id"] == "bilinear-native-grid.v1"
    assert stations["point_extraction_policy"]["allow_extrapolation"] is False


def test_information_cutoff_rejects_a_late_cycle_and_falls_back_deterministically(
    infrastructure, migrated_dsn
):
    """Codex re-review finding 4: no selected input may have an
    ``available_at`` after the request's information cutoff.

    The clock is pinned *before* the target cycle, so every acquisition
    of that cycle completes after the cutoff. Production must reject the
    whole run rather than retaining a late input.
    """
    service, _store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    # A cutoff one hour before the target reference time: the 12Z cycle
    # itself is not yet entitled to be seen at all.
    early_cutoff = REFERENCE - timedelta(hours=1)
    request = _request(snapshot, cutoff=early_cutoff)
    transports = _transports(config, frozenset(MODELS))
    clock = FixedClock(REFERENCE)
    sleeper = RecordingSleeper(clock)
    provider = Phase2ProductionProvider(
        configuration=config,
        hrrr_transport=transports["HRRR"],
        nbm_transport=transports["NBM"],
        gfs_transport=transports["GFS"],
        clock=clock,
        sleeper=sleeper,
    )
    discovery = provider.discover(request)
    retained = provider.retained_acquisitions_for(request.run_id)
    assert retained == {}, (
        "no model may be selected when every candidate cycle is after the information cutoff"
    )
    assert discovery.selection_digest is not None


def test_late_acquisition_is_rejected_even_when_the_cycle_predates_the_cutoff(
    infrastructure, migrated_dsn
):
    """The cutoff applies to the acquisition timestamps, not merely to
    the cycle reference time: a cycle that predates the cutoff but whose
    bytes only appeared afterwards is still a leak of future
    information."""
    service, _store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot)
    transports = _transports(config, frozenset(MODELS))
    # The clock (and therefore every acquisition's completed_at) sits an
    # hour past the cutoff, while the candidate cycles themselves are
    # comfortably before it.
    clock = FixedClock(CUTOFF + timedelta(hours=1))
    sleeper = RecordingSleeper(clock)
    provider = Phase2ProductionProvider(
        configuration=config,
        hrrr_transport=transports["HRRR"],
        nbm_transport=transports["NBM"],
        gfs_transport=transports["GFS"],
        clock=clock,
        sleeper=sleeper,
    )
    provider.discover(request)
    assert provider.retained_acquisitions_for(request.run_id) == {}, (
        "an acquisition completed after the information cutoff must never be retained"
    )
