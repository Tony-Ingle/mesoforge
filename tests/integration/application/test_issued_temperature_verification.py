"""Immutable one-hour verification facts through real PostgreSQL and MinIO.

Both the model messages and observations used here are generated test fixtures.
The test issuance clock models issuance before the selected hour; this is not an
operational forecast-skill claim about the retrospective preparation fixtures.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.issued_temperature_verification import (
    IssuedTemperatureVerificationService,
    configured_service,
)
from mesoforge.application.observation_preview import preview_observation_match
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_observations import (
    acquire_bundle,
    load_observation_configuration,
    prepare_bundle,
)
from mesoforge.application.station_discovery import StationDiscoveryService
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.observations.sources.stationinfo import discover_metar_stations
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore, content_addressed_key
from tests.integration.application import test_batch_issuance as issuance_tests
from tests.support.observation_preview import (
    complete_storage_inventory,
    seed_observation_preview_inputs,
)
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
    RecordingSleeper,
)
from tests.unit.application.test_batch_forecast import FIRST, LAST, OUTSIDE, write_config
from tests.unit.application.test_prepared_observations import _record, fixture_station_records

pytestmark = pytest.mark.integration

migrated_dsn = issuance_tests.migrated_dsn
object_store = issuance_tests.object_store
prepared_guidance = issuance_tests.prepared_guidance
configured_retrieval_storage = issuance_tests.configured_retrieval_storage

VALID_TIME = datetime(2026, 8, 30, 13, tzinfo=UTC)
ISSUED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)
CODE_IDENTITY = {**issuance_tests.CODE_IDENTITY, "synthetic_test_fixture": True}
JSON = CanonicalJsonSerializer()


def make_issuer(
    dsn: str, objects: S3ArtifactObjectStore, issued_at: datetime = ISSUED_AT
) -> ForecastIssuanceService:
    return ForecastIssuanceService(
        objects,
        lambda: PostgresUnitOfWork(dsn),
        code_identity=CODE_IDENTITY,
        clock=lambda: issued_at,
    )


def make_verifier(
    dsn: str, objects: S3ArtifactObjectStore, issuer: ForecastIssuanceService
) -> IssuedTemperatureVerificationService:
    return IssuedTemperatureVerificationService(
        ArtifactService(
            unit_of_work_factory=lambda: PostgresUnitOfWork(dsn),
            object_store=objects,
            idempotency_lock=PostgresIdempotencyLock(dsn),
        ),
        read_forecast=issuer.read,
        preview_match=preview_observation_match,
        code_identity=CODE_IDENTITY,
        code_revision="a" * 40,
        environment_digest=Digest.of_bytes(b"issued-temperature-verification-test-fixture"),
        clock=lambda: datetime.now(UTC),
    )


def persisted_discovery(
    dsn: str,
    objects: S3ArtifactObjectStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rename_station: bool = False,
) -> tuple[StationDiscoveryService, Mock]:
    """Generated stationinfo bytes traverse the real discovery and persistence path."""
    from mesoforge.application import automatic_verification, prepared_observations

    configuration = load_observation_configuration()
    assert configuration.phase2 is not None
    settings = configuration.phase2.aviationweather
    station_records = fixture_station_records()
    if rename_station:
        for record in station_records:
            if record["icaoId"] == "KROS":
                record["icaoId"] = "KNEW"
    payload = JSON.serialize(station_records)

    def fetch(latitude: float, longitude: float) -> Any:
        transport = FixtureAviationWeatherTransport()
        transport.stationinfo_queue.append(FakeHttpResponse(200, {"ETag": "fixture"}, payload))
        clock = FixedClock(datetime(2026, 8, 30, 17, tzinfo=UTC))
        return discover_metar_stations(
            settings,
            latitude=latitude,
            longitude=longitude,
            transport=transport,
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )

    provider = Mock(side_effect=fetch)
    factory = lambda: PostgresUnitOfWork(dsn)  # noqa: E731
    lock = PostgresIdempotencyLock(dsn)
    service = StationDiscoveryService(
        configuration=configuration,
        artifacts=ArtifactService(
            unit_of_work_factory=factory, object_store=objects, idempotency_lock=lock
        ),
        unit_of_work_factory=factory,
        idempotency_lock=lock,
        discover=provider,
        code_identity=lambda: {**CODE_IDENTITY, "git_commit": "a" * 40},
    )
    monkeypatch.setattr(
        automatic_verification, "get_station_candidates", service.get_station_candidates
    )
    monkeypatch.setattr(
        prepared_observations, "get_station_candidates", service.get_station_candidates
    )
    return service, provider


def verification_artifacts(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in inventory["tables"]["artifacts"]
        if row["artifact_type"] == "issued-temperature-verification"
    ]


def assert_forecasts_unchanged(
    before: dict[str, Any], after: dict[str, Any], objects: S3ArtifactObjectStore
) -> None:
    assert after["tables"]["issued_forecasts"] == before["tables"]["issued_forecasts"]
    assert set(before["objects"]).issubset(after["objects"])
    for row in before["tables"]["issued_forecasts"]:
        # Read the actual bytes, not only counts or application-held envelopes.
        digest = Digest(row["content_digest"])
        payload = objects._client.get_object(
            Bucket=objects._bucket, Key=content_addressed_key(digest)
        )["Body"].read()
        assert Digest.of_bytes(payload) == digest


def test_verify_persists_exact_fact_and_repeated_operation_returns_the_same_record(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    issued = issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    original = issuer.read(issued.issued_forecast_id)
    preview = preview_observation_match(issued.issued_forecast_id, VALID_TIME)
    before = complete_storage_inventory(migrated_dsn, object_store)

    # Exercise the production environment/configuration factory as well as adapters.
    response = configured_service().verify(issued.issued_forecast_id, VALID_TIME)
    assert response["status"] == "verified"
    identifier = ArtifactId(response["verification_id"])
    result = response["result"]
    assert result["schema_version"] == "issued-temperature-verification.v1"
    assert result["status"] == "verified"
    assert result["reasons"] == []
    assert result["issued_forecast_digest"] == str(issued.content_digest)
    assert result["match"] == preview
    match = result["match"]
    assert match["issued_forecast_id"] == str(issued.issued_forecast_id)
    assert match["issued_at"] == "2026-08-30T12:00:00Z"
    assert match["forecast"]["valid_time"] == "2026-08-30T13:00:00Z"
    assert match["forecast"]["temperature"]["value"] == pytest.approx(283.0, abs=1e-6)
    assert match["selected"]["station_id"] == "KROS"
    assert match["selected"]["temperature"] == {"value": 293.15, "unit": "K"}
    revision = match["selected"]["provenance"]["revision_digest"]
    assert revision == preview["selected"]["provenance"]["revision_digest"]
    assert revision.startswith("sha256:")
    # Independent arithmetic: 70% of 280 K plus 30% of 290 K is 283 K;
    # the observation is 20 C + 273.15 = 293.15 K, so the error is -10.15 K.
    assert result["temperature_error"]["value"] == pytest.approx(-10.15, abs=1e-6)
    assert result["temperature_error"]["unit"] == "K"
    assert result["temperature_error"]["definition"] == "forecast_minus_observation"
    assert result["verification_policy"]
    assert (
        result["verification_cutoff"]
        == observations.model_dump(mode="json")["availability"]["available_at"]
    )
    assert "synthetic_observation_fixture" in JSON.serialize(result).decode()

    after = complete_storage_inventory(migrated_dsn, object_store)
    assert len(verification_artifacts(after)) == 1
    assert len(after["tables"]["activities"]) == len(before["tables"]["activities"]) + 1
    assert len(after["objects"]) == len(before["objects"]) + 1
    assert_forecasts_unchanged(before, after, object_store)
    assert issuer.read(issued.issued_forecast_id) == original

    # Independent persisted-byte read proves PostgreSQL metadata and MinIO payload
    # correspond to the returned result; new service instances share no local cache.
    with PostgresUnitOfWork(migrated_dsn) as uow:
        artifact = uow.artifacts.get(identifier)
    payload = object_store.get_verified(artifact.storage_uri, artifact.content_digest)
    assert JSON.deserialize(payload) == result
    assert artifact.model_dump(mode="json") == response["artifact"]
    assert configured_service().read(identifier) == response
    assert configured_service().verify(issued.issued_forecast_id, VALID_TIME) == response
    assert complete_storage_inventory(migrated_dsn, object_store) == after


def test_concurrent_verifications_share_one_successful_activity_and_object(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    issued = issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    before = complete_storage_inventory(migrated_dsn, object_store)

    def verify(_: int) -> dict[str, Any]:
        return make_verifier(migrated_dsn, object_store, issuer).verify(
            issued.issued_forecast_id, VALID_TIME, report_reuse=True
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(verify, range(3)))
    assert sorted(response.pop("already_existing") for response in responses) == [
        False,
        True,
        True,
    ]
    assert responses[0]["status"] == "verified"
    assert responses == [responses[0]] * 3
    after = complete_storage_inventory(migrated_dsn, object_store)
    assert len(verification_artifacts(after)) == 1
    assert len(after["tables"]["activities"]) == len(before["tables"]["activities"]) + 1
    assert len(after["objects"]) == len(before["objects"]) + 1
    assert_forecasts_unchanged(before, after, object_store)


def test_window_keeps_versions_continues_after_unavailable_hours_and_reuses_saved_results(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later_time = datetime(2026, 8, 30, 15, tzinfo=UTC)
    observations = seed_observation_preview_inputs(
        migrated_dsn, object_store, VALID_TIME, extra_valid_times=(later_time,)
    )
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    issued = [issuer.issue(forecast, batch_run_id=uuid4(), location_index=0) for _ in range(2)]
    originals = {row.issued_forecast_id: issuer.read(row.issued_forecast_id) for row in issued}
    verifier = make_verifier(migrated_dsn, object_store, issuer)
    existing = verifier.verify(issued[0].issued_forecast_id, VALID_TIME)
    before = complete_storage_inventory(migrated_dsn, object_store)
    query = {
        "latitude": FIRST["lat"],
        "longitude": FIRST["lon"],
        "start_valid_time": VALID_TIME,
        "end_valid_time": datetime(2026, 8, 30, 16, tzinfo=UTC),
    }

    first = verifier.verify_window(**query)
    assert first["summary"] == {
        "verified": 3,
        "unavailable": 2,
        "ineligible": 0,
        "already_existing": 1,
        "errors": 0,
    }
    assert [row["valid_time"] for row in first["results"]] == [
        f"2026-08-30T{hour}:00:00Z" for hour in (13, 13, 14, 14, 15, 15)
    ]
    identifiers = {str(row.issued_forecast_id) for row in issued}
    for offset in (0, 2, 4):
        pair = first["results"][offset : offset + 2]
        assert {row["issued_forecast_id"] for row in pair} == identifiers
    for row in first["results"][2:4]:
        assert row["status"] == "unavailable"
        assert row["verification_id"] is None
        assert row["reasons"]
        assert row["temperature_error"]["value"] is None
    assert all(row["status"] == "verified" for row in first["results"][4:])
    persisted = {
        row["verification_id"]: verifier.read(ArtifactId(row["verification_id"]))
        for row in first["results"]
        if row["verification_id"] is not None
    }
    assert len(persisted) == 4
    assert persisted[existing["verification_id"]] == existing
    for row in first["results"]:
        if row["verification_id"] is None:
            continue
        saved = persisted[row["verification_id"]]["result"]
        assert saved["match"]["issued_forecast_id"] == row["issued_forecast_id"]
        assert saved["match"]["forecast"]["valid_time"] == row["valid_time"]
        assert saved["temperature_error"] == row["temperature_error"]
        assert saved["match"]["selected"]["provenance"]["revision_digest"].startswith("sha256:")

    after = complete_storage_inventory(migrated_dsn, object_store)
    assert len(verification_artifacts(after)) == 4
    assert len(after["tables"]["activities"]) == len(before["tables"]["activities"]) + 3
    assert len(after["objects"]) == len(before["objects"]) + 3
    assert_forecasts_unchanged(before, after, object_store)

    repeated = make_verifier(migrated_dsn, object_store, issuer).verify_window(**query)
    assert repeated["summary"] == {
        "verified": 0,
        "unavailable": 2,
        "ineligible": 0,
        "already_existing": 4,
        "errors": 0,
    }
    assert repeated["results"] == [
        {**row, "status": "already_existing"} if row["verification_id"] else row
        for row in first["results"]
    ]
    for identifier, saved in persisted.items():
        assert verifier.read(ArtifactId(identifier)) == saved
    assert {row.issued_forecast_id: issuer.read(row.issued_forecast_id) for row in issued} == (
        originals
    )
    assert complete_storage_inventory(migrated_dsn, object_store) == after


def test_two_issued_versions_for_the_same_hour_are_independently_verified(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    issued = [issuer.issue(forecast, batch_run_id=uuid4(), location_index=0) for _ in range(2)]
    before = complete_storage_inventory(migrated_dsn, object_store)
    verifier = make_verifier(migrated_dsn, object_store, issuer)
    results = [verifier.verify(row.issued_forecast_id, VALID_TIME) for row in issued]
    assert all(row["status"] == "verified" for row in results)
    assert len({row["verification_id"] for row in results}) == 2
    assert {row["result"]["match"]["issued_forecast_id"] for row in results} == {
        str(row.issued_forecast_id) for row in issued
    }
    assert len({row["result"]["issued_forecast_digest"] for row in results}) == 2
    assert (
        len(
            {row["result"]["match"]["selected"]["provenance"]["revision_digest"] for row in results}
        )
        == 1
    )
    assert [row["result"]["temperature_error"]["value"] for row in results] == pytest.approx(
        [-10.15, -10.15], abs=1e-6
    )
    after = complete_storage_inventory(migrated_dsn, object_store)
    assert len(verification_artifacts(after)) == 2
    assert_forecasts_unchanged(before, after, object_store)


@pytest.mark.parametrize("case", ["issued_after_valid_time", "no_observation", "no_dataset"])
def test_ineligible_or_unavailable_results_are_explicit_and_make_no_writes(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    issued_at = (
        datetime(2026, 8, 30, 14, tzinfo=UTC) if case == "issued_after_valid_time" else ISSUED_AT
    )
    issuer = make_issuer(migrated_dsn, object_store, issued_at)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    issued = issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    if case == "no_dataset":
        monkeypatch.delenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID")
    valid_time = datetime(2026, 8, 30, 15, tzinfo=UTC) if case == "no_observation" else VALID_TIME
    before = complete_storage_inventory(migrated_dsn, object_store)
    response = make_verifier(migrated_dsn, object_store, issuer).verify(
        issued.issued_forecast_id, valid_time
    )
    assert response["status"] == (
        "ineligible" if case == "issued_after_valid_time" else "unavailable"
    )
    assert response["verification_id"] is None
    assert response["result"]["reasons"]
    assert response["result"]["temperature_error"]["value"] is None
    assert complete_storage_inventory(migrated_dsn, object_store) == before


def test_read_rejects_an_unknown_or_non_verification_artifact_without_writes(
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    verifier = make_verifier(migrated_dsn, object_store, make_issuer(migrated_dsn, object_store))
    before = complete_storage_inventory(migrated_dsn, object_store)
    with pytest.raises(NotFound):
        verifier.read(ArtifactId.generate())
    with pytest.raises(NotFound):
        verifier.read(observations.artifact_id)
    assert complete_storage_inventory(migrated_dsn, object_store) == before


def test_saved_verification_payload_damage_fails_closed_without_rewriting_the_result(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(observations.artifact_id))
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    issued = issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    verifier = make_verifier(migrated_dsn, object_store, issuer)
    response = verifier.verify(issued.issued_forecast_id, VALID_TIME)
    assert response["status"] == "verified"
    identifier = ArtifactId(response["verification_id"])
    digest = Digest(response["artifact"]["content_digest"])
    # Deliberate corruption is confined to this fixture's dedicated random bucket.
    object_store._client.put_object(
        Bucket=object_store._bucket, Key=content_addressed_key(digest), Body=b"{}"
    )
    before = complete_storage_inventory(migrated_dsn, object_store)
    with pytest.raises(IntegrityError):
        verifier.read(identifier)
    with pytest.raises(IntegrityError):
        verifier.verify(issued.issued_forecast_id, VALID_TIME)
    assert complete_storage_inventory(migrated_dsn, object_store) == before


def test_prepared_metar_bytes_feed_existing_window_and_rebuild_without_http(
    tmp_path: Path,
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new preparation boundary uses invented provider bytes, then existing storage/logic."""
    from mesoforge.application import prepared_observations

    _, metadata_provider = persisted_discovery(migrated_dsn, object_store, monkeypatch)

    end = datetime(2026, 8, 30, 16, tzinfo=UTC)
    acquired = datetime(2026, 8, 30, 17, tzinfo=UTC)
    payload = JSON.serialize(
        [
            _record(
                obsTime=int(datetime(2026, 8, 30, hour, 10, tzinfo=UTC).timestamp()),
                reportTime=f"2026-08-30T{hour}:10:00Z",
                receiptTime=f"2026-08-30T{hour}:12:00Z",
            )
            for hour in (13, 15)
        ]
    )
    transport = FixtureAviationWeatherTransport()
    transport.metar_queue.append(FakeHttpResponse(200, {"ETag": "fixture"}, payload))
    clock = FixedClock(acquired)
    directory = tmp_path / "metar"
    # Retained acquisition and later preparation can come from different code revisions.
    monkeypatch.setattr(
        prepared_observations, "_identity", lambda: {**CODE_IDENTITY, "git_commit": "b" * 40}
    )
    acquire_bundle(
        directory,
        latitude=FIRST["lat"],
        longitude=FIRST["lon"],
        start_valid_time=VALID_TIME,
        end_valid_time=end,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
    )

    def no_http() -> None:
        pytest.fail("Preparation from retained bytes and verification must not construct HTTP")

    monkeypatch.setattr(prepared_observations, "RequestsAviationWeatherHttpTransport", no_http)
    monkeypatch.setattr(
        prepared_observations, "_identity", lambda: {**CODE_IDENTITY, "git_commit": "c" * 40}
    )
    prepared = prepare_bundle(directory)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", prepared["observations_artifact_id"])
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"],
        longitude=FIRST["lon"],
    )
    issued = [issuer.issue(forecast, batch_run_id=uuid4(), location_index=0) for _ in range(2)]
    before = complete_storage_inventory(migrated_dsn, object_store)
    verifier = make_verifier(migrated_dsn, object_store, issuer)
    query = dict(
        latitude=FIRST["lat"],
        longitude=FIRST["lon"],
        start_valid_time=VALID_TIME,
        end_valid_time=end,
    )
    first = verifier.verify_window(**query)
    assert first["summary"] == {
        "verified": 4,
        "already_existing": 0,
        "unavailable": 2,
        "ineligible": 0,
        "errors": 0,
    }
    assert {row["issued_forecast_id"] for row in first["results"]} == {
        str(row.issued_forecast_id) for row in issued
    }
    for row in first["results"]:
        if row["verification_id"] is None:
            assert row["reasons"]
            continue
        saved = verifier.read(ArtifactId(row["verification_id"]))["result"]
        selected = saved["match"]["selected"]
        assert selected["temperature"] == {"value": 293.15, "unit": "K"}
        assert selected["provenance"]["raw_artifact_id"] == prepared["raw_artifact_id"]
        assert selected["provenance"]["revision_digest"].startswith("sha256:")
        with PostgresUnitOfWork(migrated_dsn) as uow:
            raw_source = uow.artifacts.get(ArtifactId(prepared["raw_artifact_id"]))
            station_snapshot = uow.artifacts.get(
                ArtifactId(selected["provenance"]["station_snapshot_artifact_id"])
            )
        assert raw_source.code_revision == "b" * 40
        assert station_snapshot.code_revision == "c" * 40
        assert saved["temperature_error"]["value"] == pytest.approx(
            saved["match"]["forecast"]["temperature"]["value"] - 293.15
        )
    _, raw = verifier._artifacts.load_verified_payload(ArtifactId(prepared["raw_artifact_id"]))
    assert raw == payload
    after = complete_storage_inventory(migrated_dsn, object_store)
    assert_forecasts_unchanged(before, after, object_store)
    assert prepare_bundle(directory) == prepared
    repeat = verifier.verify_window(**query)
    assert repeat["summary"] == {**first["summary"], "verified": 0, "already_existing": 4}
    assert complete_storage_inventory(migrated_dsn, object_store) == after
    assert len(transport.get_calls) == 1
    metadata_provider.assert_called_once_with(FIRST["lat"], FIRST["lon"])


def test_automatic_window_acquires_once_reuses_real_snapshot_and_skips_empty_window(
    tmp_path: Path,
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application import automatic_verification, prepared_observations

    discovery_service, metadata_provider = persisted_discovery(
        migrated_dsn, object_store, monkeypatch, rename_station=True
    )

    # Retained synthetic fixtures must never satisfy the automatic real-source lookup.
    synthetic = seed_observation_preview_inputs(migrated_dsn, object_store, VALID_TIME)
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", str(synthetic.artifact_id))
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_DIR", str(tmp_path / "observations"))
    transport = FixtureAviationWeatherTransport()
    payload = JSON.serialize(
        [
            _record(
                icaoId="KNEW",
                rawOb=f"SYNTHETIC KNEW 30{hour:02d}10Z 18005KT 10SM CLR 20/10 A3000",
                obsTime=int(datetime(2026, 8, 30, hour, 10, tzinfo=UTC).timestamp()),
                reportTime=f"2026-08-30T{hour}:10:00Z",
                receiptTime=f"2026-08-30T{hour}:12:00Z",
            )
            for hour in (13, 15)
        ]
    )
    transport.metar_queue.append(FakeHttpResponse(200, {}, payload))
    monkeypatch.setattr(
        prepared_observations, "RequestsAviationWeatherHttpTransport", lambda: transport
    )
    issuer = make_issuer(migrated_dsn, object_store)
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"],
        longitude=FIRST["lon"],
    )
    issued = [issuer.issue(forecast, batch_run_id=uuid4(), location_index=0) for _ in range(2)]
    before = complete_storage_inventory(migrated_dsn, object_store)
    query = dict(
        latitude=FIRST["lat"],
        longitude=FIRST["lon"],
        start_valid_time=VALID_TIME,
        end_valid_time=datetime(2026, 8, 30, 16, tzinfo=UTC),
    )
    first = automatic_verification.run_window(**query)
    assert first["preflight"]["query_window_start"] == "2026-08-30T12:45:00+00:00"
    assert first["preflight"]["query_window_end"] == "2026-08-30T15:15:00+00:00"
    assert first["preflight"]["station_ids"] == ["KCBG", "KJMR", "KNEW"]
    assert first["downloaded_bytes"] == len(payload)
    assert first["observations_reused"] is False
    assert first["station_discovery"]["reused"] is False
    assert first["station_discovery"]["discovery_calls"] == 1
    assert (
        first["observation_source"]["station_discovery_artifact_id"]
        == (first["station_discovery"]["discovery_artifact_id"])
    )
    assert first["verification"]["summary"] == {
        "verified": 4,
        "already_existing": 0,
        "unavailable": 2,
        "ineligible": 0,
        "errors": 0,
    }
    assert {row["issued_forecast_id"] for row in first["verification"]["results"]} == {
        str(row.issued_forecast_id) for row in issued
    }
    verifier = make_verifier(migrated_dsn, object_store, issuer)
    for row in first["verification"]["results"]:
        if row["verification_id"] is None:
            continue
        saved = verifier.read(ArtifactId(row["verification_id"]))["result"]
        selected = saved["match"]["selected"]
        assert selected["station_id"] == "KNEW"
        assert selected["temperature"] == {"value": 293.15, "unit": "K"}
        assert saved["temperature_error"]["value"] == pytest.approx(
            saved["match"]["forecast"]["temperature"]["value"] - 293.15
        )
    assert os.environ["MESOFORGE_OBSERVATIONS_ARTIFACT_ID"] == str(synthetic.artifact_id)
    after = complete_storage_inventory(migrated_dsn, object_store)
    assert_forecasts_unchanged(before, after, object_store)
    repeated = automatic_verification.run_window(**query)
    assert repeated["observations_artifact_id"] == first["observations_artifact_id"]
    assert repeated["observations_reused"] is True
    assert repeated["downloaded_bytes"] == 0
    assert repeated["station_discovery"]["reused"] is True
    assert repeated["station_discovery"]["discovery_calls"] == 0
    assert (
        repeated["station_discovery"]["content_digest"]
        == first["station_discovery"]["content_digest"]
    )
    assert repeated["verification"]["summary"] == {
        **first["verification"]["summary"],
        "verified": 0,
        "already_existing": 4,
    }
    # An overlapping narrower request reuses the same original snapshot too.
    subset = automatic_verification.run_window(
        **{**query, "end_valid_time": datetime(2026, 8, 30, 14, tzinfo=UTC)}
    )
    assert subset["observations_artifact_id"] == first["observations_artifact_id"]
    assert subset["verification"]["summary"]["already_existing"] == 2
    empty = automatic_verification.run_window(
        **{**query, "start_valid_time": ISSUED_AT, "end_valid_time": VALID_TIME}
    )
    assert empty["status"] == "nothing_to_verify"
    assert empty["downloaded_bytes"] == 0
    assert len(transport.get_calls) == 1
    assert complete_storage_inventory(migrated_dsn, object_store) == after
    metadata_provider.assert_called_once_with(FIRST["lat"], FIRST["lon"])
    cached = discovery_service.get_station_candidates(FIRST["lat"], FIRST["lon"])
    assert cached == repeated["station_discovery"]
    assert complete_storage_inventory(migrated_dsn, object_store) == after

    # Broken retained evidence fails closed, without silently reacquiring different observations.
    with PostgresUnitOfWork(migrated_dsn) as uow:
        manifest = uow.artifacts.get(ArtifactId(first["observations_artifact_id"]))
    object_store._client.put_object(
        Bucket=object_store._bucket, Key=content_addressed_key(manifest.content_digest), Body=b"{}"
    )
    with pytest.raises(IntegrityError):
        automatic_verification.run_window(**query)
    assert len(transport.get_calls) == 1


def test_automatic_batch_isolates_locations_and_reuses_results_without_changing_issuances(
    tmp_path: Path,
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application import automatic_verification, prepared_observations

    _, metadata_provider = persisted_discovery(migrated_dsn, object_store, monkeypatch)

    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_DIR", str(tmp_path / "observations"))
    transport = FixtureAviationWeatherTransport()
    payload = JSON.serialize(
        [
            _record(
                obsTime=int(datetime(2026, 8, 30, 13, 10, tzinfo=UTC).timestamp()),
                reportTime="2026-08-30T13:10:00Z",
                receiptTime="2026-08-30T13:12:00Z",
            )
        ]
    )
    transport.metar_queue.extend(
        [FakeHttpResponse(200, {}, payload), FakeHttpResponse(200, {}, payload)]
    )
    monkeypatch.setattr(
        prepared_observations, "RequestsAviationWeatherHttpTransport", lambda: transport
    )
    issuer = make_issuer(migrated_dsn, object_store)
    guidance = PreparedPointForecast.from_directory(prepared_guidance)
    issued = [
        issuer.issue(
            guidance.forecast(latitude=location["lat"], longitude=location["lon"]),
            batch_run_id=uuid4(),
            location_index=i,
        )
        for i, location in enumerate((FIRST, LAST))
    ]
    originals = [issuer.read(row.issued_forecast_id) for row in issued]
    before = complete_storage_inventory(migrated_dsn, object_store)
    config = write_config(tmp_path, [FIRST, OUTSIDE, LAST, {"lat": 45.75, "lon": -93.2}])
    window = dict(start_valid_time=VALID_TIME, end_valid_time=datetime(2026, 8, 30, 14, tzinfo=UTC))
    first = automatic_verification.run_batch(config, **window)
    assert first["summary"] == {"completed": 2, "errors": 1, "nothing_to_verify": 1}
    assert [row["status"] for row in first["results"]] == [
        "completed",
        "error",
        "completed",
        "nothing_to_verify",
    ]
    assert first["results"][1]["error"]["code"] == "unsupported_coordinate"
    assert first["results"][3]["result"]["downloaded_bytes"] == 0
    for position, issuance in zip((0, 2), issued, strict=True):
        row = first["results"][position]
        assert row["summary"]["verified"] == 1
        assert row["result"]["verification"]["results"][0]["issued_forecast_id"] == str(
            issuance.issued_forecast_id
        )
    assert len(transport.get_calls) == 2
    assert metadata_provider.call_count == 2
    after = complete_storage_inventory(migrated_dsn, object_store)
    assert_forecasts_unchanged(before, after, object_store)
    repeat = automatic_verification.run_batch(config, **window)
    for position in (0, 2):
        row = repeat["results"][position]
        assert row["summary"]["already_existing"] == 1
        assert row["summary"]["verified"] == 0
        assert row["result"]["downloaded_bytes"] == 0
        assert (
            row["result"]["verification"]["results"][0]["verification_id"]
            == first["results"][position]["result"]["verification"]["results"][0]["verification_id"]
        )
    assert len(transport.get_calls) == 2
    assert metadata_provider.call_count == 2
    assert [issuer.read(row.issued_forecast_id) for row in issued] == originals
    assert complete_storage_inventory(migrated_dsn, object_store) == after
