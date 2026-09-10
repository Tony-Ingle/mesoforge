"""Focused issuance checks against real PostgreSQL and S3-compatible storage."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application.batch_forecast import run_batch
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.identifiers import Digest
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore, content_addressed_key
from tests.unit.application.test_batch_forecast import FIRST, LAST, OUTSIDE, write_config
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    prepare_fixture_guidance,
)

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[3]
CODE_IDENTITY = {"git_commit": "a" * 40, "working_tree_dirty": False}


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    monkeypatch.setenv("MESOFORGE_ALEMBIC_DSN", clean_postgres_dsn)
    command.upgrade(config, "head")
    return clean_postgres_dsn


@pytest.fixture()
def object_store() -> S3ArtifactObjectStore:
    # Dedicated random test bucket, following the existing integration environment.
    return S3ArtifactObjectStore(
        bucket=f"mesoforge-issuance-test-{uuid4().hex[:8]}",
        endpoint_url=os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100"),
        access_key=os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test"),
        secret_key=os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password"),
    )


@pytest.fixture()
def service(migrated_dsn: str, object_store: S3ArtifactObjectStore) -> ForecastIssuanceService:
    return ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity=CODE_IDENTITY,
        clock=lambda: datetime(2026, 9, 10, 12, tzinfo=UTC),
    )


@pytest.fixture(scope="module")
def prepared_guidance(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("stored-batch-guidance")
    prepare_fixture_guidance(directory, EXTENDED_HORIZONS)
    return directory


@pytest.fixture()
def configured_retrieval_storage(
    migrated_dsn: str, object_store: S3ArtifactObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the endpoint's production configuration path against the isolated services."""
    monkeypatch.setenv("MESOFORGE_DATABASE_DSN", migrated_dsn)
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", object_store._bucket)
    for setting, default in (
        ("ENDPOINT", "http://127.0.0.1:19100"),
        ("ACCESS_KEY", "mesoforge_test"),
        ("SECRET_KEY", "mesoforge_test_password"),
    ):
        monkeypatch.setenv(
            f"MESOFORGE_S3_{setting}", os.environ.get(f"MESOFORGE_TEST_S3_{setting}", default)
        )


def storage_inventory(dsn: str, object_store: S3ArtifactObjectStore) -> tuple:
    """Capture actual row identities and S3 object identities, sizes, and ETags."""
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as connection:
            headers = tuple(
                connection.execute(
                    sa.text("SELECT * FROM issued_forecasts ORDER BY issued_forecast_id")
                )
            )
            metadata = tuple(
                connection.execute(sa.text("SELECT * FROM stored_objects ORDER BY content_digest"))
            )
    finally:
        engine.dispose()
    objects = object_store._client.list_objects_v2(Bucket=object_store._bucket)
    assert not objects.get("IsTruncated", False)
    entries = tuple(
        sorted((item["Key"], item["ETag"], item["Size"]) for item in objects.get("Contents", []))
    )
    return headers, metadata, entries


def forbid_retrieval_writes_and_calculation(monkeypatch: pytest.MonkeyPatch) -> Mock:
    forbidden = Mock(side_effect=AssertionError("Retrieval attempted a write or calculation"))
    for owner, method in (
        (ForecastIssuanceService, "issue"),
        (PreparedPointForecast, "forecast"),
        (PostgresUnitOfWork, "commit"),
        (S3ArtifactObjectStore, "put_if_absent"),
        (S3ArtifactObjectStore, "_ensure_bucket"),
    ):
        monkeypatch.setattr(owner, method, forbidden)
    return forbidden


def test_two_batch_runs_keep_both_versions_and_one_off_api_does_not_issue(
    tmp_path: Path,
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
) -> None:
    config = write_config(tmp_path, [FIRST, OUTSIDE, LAST])
    first = run_batch(config, prepared_guidance, issuer=service)
    second = run_batch(config, prepared_guidance, issuer=service)
    assert first["batch_run_id"] != second["batch_run_id"]
    expected_ids: set[UUID] = set()
    for batch in (first, second):
        assert [row["status"] for row in batch["results"]] == ["ok", "error", "ok"]
        unsupported = batch["results"][1]
        assert unsupported["error"]["code"] == "unsupported_coordinate"
        assert "forecast" not in unsupported
        assert "issued" not in unsupported
        for index in (0, 2):
            row = batch["results"][index]
            expected_ids.add(UUID(row["issued"]["issued_forecast_id"]))
            assert row["issued"]["batch_run_id"] == batch["batch_run_id"]
            assert row["issued"]["location_index"] == index
            assert len(row["forecast"]["hours"]) == 36
            for horizon, hour in enumerate(row["forecast"]["hours"], start=1):
                assert hour["horizon_hours"] == horizon
                assert hour["temperature"]["value"] == pytest.approx(282 + horizon, abs=1e-6)
                assert hour["temperature"]["unit"] == "K"
                assert hour["missing_reasons"] == []
                assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]
                assert [source["cycle"] for source in hour["sources"]] == [
                    "2026-08-30T12:00:00Z",
                    "2026-08-30T06:00:00Z",
                ]
    assert len(expected_ids) == 4

    # Read with a new service/UoW, not state held in either batch invocation.
    reader = ForecastIssuanceService(
        object_store, lambda: PostgresUnitOfWork(migrated_dsn), code_identity=CODE_IDENTITY
    )
    for batch in (first, second):
        for row in (batch["results"][0], batch["results"][2]):
            saved = reader.read(UUID(row["issued"]["issued_forecast_id"]))
            assert saved["forecast"] == row["forecast"]
            assert saved["issued_at"] == "2026-09-10T12:00:00Z"
            assert saved["target_reference_time"] == "2026-08-30T12:00:00Z"
            assert saved["code_identity"] == CODE_IDENTITY

    with PostgresUnitOfWork(migrated_dsn) as uow:
        first_versions = uow.issued_forecasts.list_for_coordinate(FIRST["lat"], FIRST["lon"])
        last_versions = uow.issued_forecasts.list_for_coordinate(LAST["lat"], LAST["lon"])
        assert len(first_versions) == len(last_versions) == 2
        assert uow.issued_forecasts.list_for_coordinate(OUTSIDE["lat"], OUTSIDE["lon"]) == ()
        assert {row.issued_forecast_id for row in first_versions + last_versions} == expected_ids

    with TestClient(api.create_app(prepared_guidance)) as client:
        for _ in range(2):
            response = client.get("/forecast", params=FIRST)
            assert response.status_code == 200
            assert response.json() == first["results"][0]["forecast"]
    engine = sa.create_engine(migrated_dsn)
    try:
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM issued_forecasts")) == 4
            assert connection.scalar(sa.text("SELECT count(*) FROM stored_objects")) == 4
    finally:
        engine.dispose()


def test_persisted_missing_model_is_still_explicit_with_no_weight_change(
    prepared_guidance: Path, tmp_path: Path, service: ForecastIssuanceService
) -> None:
    directory = tmp_path / "missing-model"
    shutil.copytree(prepared_guidance, directory)
    (directory / "GFS.nc").unlink()
    batch = run_batch(write_config(tmp_path, [FIRST]), directory, issuer=service)
    result = batch["results"][0]
    assert result["status"] == "ok"
    saved = service.read(UUID(result["issued"]["issued_forecast_id"]))["forecast"]
    assert saved == result["forecast"]
    assert len(saved["hours"]) == 36
    for hour in saved["hours"]:
        assert hour["temperature"] == {"value": None, "unit": "K"}
        assert hour["missing_reasons"] == ["GFS: prepared guidance file is missing"]
        assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE issued_forecasts SET latitude = 45.9 WHERE issued_forecast_id = :identifier",
        "DELETE FROM issued_forecasts WHERE issued_forecast_id = :identifier",
    ],
)
def test_database_rejects_update_and_delete_of_issued_versions(
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    statement: str,
) -> None:
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    engine = sa.create_engine(migrated_dsn)
    try:
        with pytest.raises(sa.exc.DBAPIError, match="immutable"), engine.begin() as connection:
            connection.execute(sa.text(statement), {"identifier": record.issued_forecast_id})
    finally:
        engine.dispose()
    assert service.read(record.issued_forecast_id)["forecast"] == forecast
    with PostgresUnitOfWork(migrated_dsn) as uow:
        assert uow.issued_forecasts.get(record.issued_forecast_id) == record


def test_failed_database_commit_rolls_back_header_and_metadata_then_batch_continues(
    prepared_guidance: Path,
    tmp_path: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
) -> None:
    class FailedCommitUnitOfWork(PostgresUnitOfWork):
        def commit(self) -> None:
            raise RuntimeError("Deliberate commit failure after inserts")

    calls = 0

    def uow_factory() -> PostgresUnitOfWork:
        nonlocal calls
        calls += 1
        return (
            FailedCommitUnitOfWork(migrated_dsn) if calls == 1 else PostgresUnitOfWork(migrated_dsn)
        )

    issuer = ForecastIssuanceService(object_store, uow_factory, code_identity=CODE_IDENTITY)
    batch = run_batch(write_config(tmp_path, [FIRST, LAST]), prepared_guidance, issuer=issuer)
    failed, successful = batch["results"]
    assert failed["status"] == "error"
    assert failed["error"]["code"] == "issuance_failed"
    assert "issued" not in failed
    assert "forecast" not in failed
    assert successful["status"] == "ok"
    assert (
        issuer.read(UUID(successful["issued"]["issued_forecast_id"]))["forecast"]
        == (successful["forecast"])
    )
    engine = sa.create_engine(migrated_dsn)
    try:
        with engine.connect() as connection:
            # Bytes uploaded before a failed transaction may be unreferenced; they
            # must never appear as successful issued headers or registered metadata.
            assert connection.scalar(sa.text("SELECT count(*) FROM issued_forecasts")) == 1
            assert connection.scalar(sa.text("SELECT count(*) FROM stored_objects")) == 1
    finally:
        engine.dispose()
    with PostgresUnitOfWork(migrated_dsn) as uow:
        assert uow.issued_forecasts.list_for_coordinate(FIRST["lat"], FIRST["lon"]) == ()
        assert len(uow.issued_forecasts.list_for_coordinate(LAST["lat"], LAST["lon"])) == 1


def test_api_retrieves_each_exact_issued_version_without_writes_or_calculation(
    tmp_path: Path,
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(tmp_path, [FIRST])
    original_envelopes = {}
    for _ in range(2):
        result = run_batch(config, prepared_guidance, issuer=service)["results"][0]
        assert result["status"] == "ok"
        identifier = result["issued"]["issued_forecast_id"]
        # Compare HTTP output to the original JSON actually stored in MinIO,
        # independently of the service reader used by the endpoint.
        response = object_store._client.get_object(
            Bucket=object_store._bucket,
            Key=content_addressed_key(Digest(result["issued"]["content_digest"])),
        )
        envelope = json.loads(response["Body"].read())
        assert envelope["issued_forecast_id"] == identifier
        assert envelope["forecast"] == result["forecast"]
        assert envelope["code_identity"] == CODE_IDENTITY
        original_envelopes[identifier] = envelope
    assert len(original_envelopes) == 2
    before = storage_inventory(migrated_dsn, object_store)
    assert tuple(len(items) for items in before) == (2, 2, 2)
    forbidden = forbid_retrieval_writes_and_calculation(monkeypatch)

    with TestClient(api.create_app(prepared_guidance)) as client:
        for _ in range(2):
            for identifier, envelope in original_envelopes.items():
                response = client.get(f"/issued-forecasts/{identifier}")
                assert response.status_code == 200
                assert response.json() == envelope
    forbidden.assert_not_called()
    assert storage_inventory(migrated_dsn, object_store) == before


def test_api_retrieval_rejects_malformed_and_unknown_ids_without_writes(
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = storage_inventory(migrated_dsn, object_store)
    assert tuple(len(items) for items in before) == (0, 0, 0)
    forbidden = forbid_retrieval_writes_and_calculation(monkeypatch)
    with TestClient(api.create_app(prepared_guidance)) as client:
        for identifier, status, code in (
            ("not-a-uuid", 422, "invalid_issued_forecast_id"),
            (str(uuid4()), 404, "issued_forecast_not_found"),
        ):
            response = client.get(f"/issued-forecasts/{identifier}")
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.json()["error"]["message"]
    forbidden.assert_not_called()
    assert storage_inventory(migrated_dsn, object_store) == before


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_api_retrieval_reports_saved_payload_damage_without_recalculation(
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    key = content_addressed_key(record.content_digest)
    # Deliberately damage only this test's dedicated random bucket. A missing
    # payload is a storage failure, not an unknown issued-forecast ID.
    if damage == "corrupt":
        object_store._client.put_object(Bucket=object_store._bucket, Key=key, Body=b"{}")
    else:
        object_store._client.delete_object(Bucket=object_store._bucket, Key=key)
    before = storage_inventory(migrated_dsn, object_store)
    forbidden = forbid_retrieval_writes_and_calculation(monkeypatch)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(f"/issued-forecasts/{record.issued_forecast_id}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "issued_forecast_read_failed"
    assert response.json()["error"]["message"]
    forbidden.assert_not_called()
    assert storage_inventory(migrated_dsn, object_store) == before


def test_api_retrieval_does_not_create_a_misconfigured_missing_bucket(
    prepared_guidance: Path,
    migrated_dsn: str,
    service: ForecastIssuanceService,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    missing_bucket = f"mesoforge-absent-{uuid4().hex[:8]}"
    monkeypatch.setenv("MESOFORGE_S3_BUCKET", missing_bucket)
    before = storage_inventory(migrated_dsn, object_store)
    assert missing_bucket not in {
        item["Name"] for item in object_store._client.list_buckets()["Buckets"]
    }
    forbidden = forbid_retrieval_writes_and_calculation(monkeypatch)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(f"/issued-forecasts/{record.issued_forecast_id}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "issued_forecast_read_failed"
    forbidden.assert_not_called()
    assert missing_bucket not in {
        item["Name"] for item in object_store._client.list_buckets()["Buckets"]
    }
    assert storage_inventory(migrated_dsn, object_store) == before
