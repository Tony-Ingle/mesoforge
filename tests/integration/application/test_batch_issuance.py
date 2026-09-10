"""Focused issuance checks against real PostgreSQL and S3-compatible storage."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
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
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
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
