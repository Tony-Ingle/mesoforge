"""Saved-version retrieval with real readback logic and explicit offline storage doubles."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import issuance
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast, prepare_demo_files
from mesoforge.storage import s3
from tests.support.in_memory_uow import (
    InMemoryObjectStore,
    InMemoryUnitOfWork,
    InMemoryUnitOfWorkFactory,
)
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    prepare_fixture_guidance,
)


@pytest.fixture(scope="module")
def prepared_guidance(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("retrieval-guidance")
    prepare_fixture_guidance(directory, EXTENDED_HORIZONS)
    return directory


@pytest.fixture()
def saved_versions(prepared_guidance: Path):
    objects = InMemoryObjectStore()
    factory = InMemoryUnitOfWorkFactory()
    service = ForecastIssuanceService(
        objects,
        factory,
        code_identity={"git_commit": "a" * 40, "working_tree_dirty": False},
        clock=lambda: datetime(2026, 9, 10, 12, tzinfo=UTC),
    )
    forecast = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=45.8, longitude=-93.1
    )
    records = [service.issue(forecast, batch_run_id=uuid4(), location_index=0) for _ in range(2)]
    return service, factory, objects, records


def test_exact_versions_use_existing_readback_without_calculation_or_writes(
    tmp_path: Path, saved_versions, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory, objects, records = saved_versions
    original_bytes = dict(objects.objects)
    monkeypatch.setattr(api, "read_issued_forecast", service.read)
    # The calculation endpoint has different, synthetic three-hour inputs loaded;
    # retrieval must still return the saved 36-hour version's own labels and times.
    prepare_demo_files(tmp_path)
    app = api.create_app(tmp_path)
    forbidden = Mock(side_effect=AssertionError("Retrieval attempted calculation or mutation"))
    monkeypatch.setattr(PreparedPointForecast, "forecast", forbidden)
    monkeypatch.setattr(PreparedPointForecast, "from_directory", forbidden)
    monkeypatch.setattr(ForecastIssuanceService, "issue", forbidden)
    monkeypatch.setattr(InMemoryObjectStore, "put_if_absent", forbidden)
    monkeypatch.setattr(InMemoryUnitOfWork, "commit", forbidden)
    with TestClient(app) as client:
        for record in records:
            for _ in range(2):
                response = client.get(f"/issued-forecasts/{record.issued_forecast_id}")
                assert response.status_code == 200
                assert response.json() == json.loads(original_bytes[record.content_digest])
                assert len(response.json()["forecast"]["hours"]) == 36
                assert response.json()["code_identity"]["git_commit"] == "a" * 40
    forbidden.assert_not_called()
    assert objects.objects == original_bytes
    assert len(factory.issued_forecasts) == len(factory.stored_objects) == 2


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", "b80e231a-c6e6-4066-ab4c"])
def test_invalid_id_has_a_retrieval_error_before_storage_access(
    prepared_guidance: Path, monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    forbidden = Mock(side_effect=AssertionError("Invalid ID attempted storage access"))
    monkeypatch.setattr(api, "read_issued_forecast", forbidden)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(f"/issued-forecasts/{bad_id}")
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_issued_forecast_id",
            "message": "Provide an issued-forecast ID in UUID format.",
        }
    }
    forbidden.assert_not_called()


def test_unknown_version_returns_404(prepared_guidance: Path, saved_versions, monkeypatch):
    service, factory, objects, _ = saved_versions
    monkeypatch.setattr(api, "read_issued_forecast", service.read)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(f"/issued-forecasts/{uuid4()}")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "issued_forecast_not_found",
            "message": "No saved issued forecast exists for this ID.",
        }
    }
    assert len(factory.issued_forecasts) == len(objects.objects) == 2


@pytest.mark.parametrize("failure", ["missing_metadata", "missing_object", "corrupt_object"])
def test_damaged_known_version_is_a_storage_error_not_unknown_or_recalculated(
    prepared_guidance: Path, saved_versions, monkeypatch, failure: str
) -> None:
    service, factory, objects, records = saved_versions
    record = records[0]
    if failure == "missing_metadata":
        del factory.stored_objects[record.content_digest]
    elif failure == "missing_object":
        del objects.metadata[record.content_digest]
    else:
        objects.objects[record.content_digest] = b"corrupted payload"
    before = dict(objects.objects)
    monkeypatch.setattr(api, "read_issued_forecast", service.read)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(f"/issued-forecasts/{record.issued_forecast_id}")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "issued_forecast_read_failed",
            "message": "Could not read and verify the saved issued forecast.",
        }
    }
    assert objects.objects == before
    assert len(factory.issued_forecasts) == 2


def test_storage_connection_error_is_sanitized(prepared_guidance: Path, monkeypatch):
    monkeypatch.setattr(
        api,
        "read_issued_forecast",
        Mock(side_effect=RuntimeError("secret connection details must not be returned")),
    )
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(f"/issued-forecasts/{uuid4()}")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "issued_forecast_read_failed"
    assert "secret" not in response.text


def test_configured_reader_uses_only_s3_get_and_preserves_stored_identity(
    saved_versions, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory, objects, records = saved_versions
    record = records[0]
    client = Mock()
    client.get_object.return_value = {"Body": io.BytesIO(objects.objects[record.content_digest])}
    monkeypatch.setattr(s3.boto3, "client", Mock(return_value=client))
    monkeypatch.setattr(issuance, "PostgresUnitOfWork", lambda dsn: factory())
    for name, value in {
        "DATABASE_DSN": "postgresql+psycopg://offline-test",
        "S3_BUCKET": "mesoforge-test",
        "S3_ENDPOINT": "http://127.0.0.1:59010",
        "S3_ACCESS_KEY": "test-only",
        "S3_SECRET_KEY": "test-only",
    }.items():
        monkeypatch.setenv(f"MESOFORGE_{name}", value)
    saved = issuance.read_issued_forecast(record.issued_forecast_id)
    assert saved == json.loads(objects.objects[record.content_digest])
    assert [call[0] for call in client.method_calls] == ["get_object"]
    assert len(factory.issued_forecasts) == len(objects.objects) == 2


def test_prepared_forecast_does_not_resolve_issued_storage(prepared_guidance: Path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Calculation attempted issued storage"))
    monkeypatch.setattr(api, "read_issued_forecast", forbidden)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get("/forecast?lat=45.8&lon=-93.1")
    assert response.status_code == 200
    assert len(response.json()["hours"]) == 36
    assert "issued_forecast_id" not in response.json()
    forbidden.assert_not_called()
