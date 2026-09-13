"""Read-only conditions over exact stored versions, using the existing storage doubles."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import local_surface_grid, weather_conditions
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting import conditions
from mesoforge.forecasting.conditions import (
    ConditionsPreviewUnavailableError,
    build_conditions_preview,
)
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWork
from tests.unit.application.test_forecast_issuance import memory_service as memory_service
from tests.unit.forecasting.test_conditions import saved_forecast
from tests.unit.test_issued_forecast_api import prepared_guidance as prepared_guidance


@pytest.fixture()
def condition_versions(memory_service):
    service, factory, objects = memory_service
    forecast = saved_forecast()["forecast"]
    records = [service.issue(forecast, batch_run_id=uuid4(), location_index=0) for _ in range(2)]
    return service, factory, objects, records


@pytest.fixture()
def preview_client(prepared_guidance, condition_versions, monkeypatch):
    service, _, _, _ = condition_versions
    monkeypatch.setattr(weather_conditions, "read_issued_forecast", service.read)
    app = api.create_app(prepared_guidance)
    forbidden = Mock(side_effect=AssertionError("Preview attempted source I/O or mutation"))
    for owner, name in (
        (PreparedPointForecast, "forecast"),
        (PreparedPointForecast, "_forecast_column"),
        (PreparedPointForecast, "from_directory"),
        (local_surface_grid, "build_local_surface_grid"),
        (ForecastIssuanceService, "issue"),
        (InMemoryObjectStore, "put_if_absent"),
        (InMemoryUnitOfWork, "commit"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    monkeypatch.setattr("xarray.open_dataset", forbidden)
    with TestClient(app) as client:
        yield client
    forbidden.assert_not_called()


def _url(identifier):
    return f"/issued-forecasts/{identifier}/conditions"


def _expected_derivation():
    return {
        "ruleset_id": conditions.RULESET_ID,
        "template_version": conditions.TEMPLATE_VERSION,
        "source_sha256": {
            "forecasting/conditions.py": hashlib.sha256(
                Path(conditions.__file__).read_bytes()
            ).hexdigest(),
            "application/weather_conditions.py": hashlib.sha256(
                Path(weather_conditions.__file__).read_bytes()
            ).hexdigest(),
        },
    }


def test_preview_reuses_exact_versions_and_center_without_writes(
    preview_client, condition_versions
):
    service, factory, objects, records = condition_versions
    before = dict(objects.objects)
    original_rows = dict(factory.issued_forecasts)
    for record in records:
        saved = json.loads(before[record.content_digest])
        response = preview_client.get(_url(record.issued_forecast_id))
        repeated = preview_client.get(_url(record.issued_forecast_id))
        assert response.status_code == repeated.status_code == 200
        assert response.content == repeated.content
        result = response.json()
        assert result == {**build_conditions_preview(saved), "derivation": _expected_derivation()}
        assert result["input"]["code_identity"] == saved["code_identity"]
        assert result["derivation"] != result["input"]["code_identity"]
        assert result["input"]["issued_forecast_id"] == str(record.issued_forecast_id)
        assert result["input"]["issued_payload_digest"] == str(record.content_digest)
        grid = saved["forecast"]["local_grid_baseline"]
        assert len(result["cells"]) == len(grid["cells"])
        assert all(len(cell["hours"]) == 36 for cell in result["cells"])
        center = next(cell for cell in result["cells"] if cell["is_forecast_point"])
        assert result["center_point"]["hours"] == center["hours"]
        assert [hour["horizon_hours"] for hour in center["hours"]] == list(range(1, 37))
        assert all(hour["rendering"]["text"] and hour["components"] for hour in center["hours"])
        assert service.read(record.issued_forecast_id) == saved
    assert objects.objects == before
    assert factory.issued_forecasts == original_rows
    assert len(factory.stored_objects) == 2
    assert factory.artifacts == factory.activities == {}


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123"])
def test_invalid_id_is_rejected_before_storage(preview_client, monkeypatch, bad_id):
    reader = Mock(side_effect=AssertionError("Invalid ID reached storage"))
    monkeypatch.setattr(weather_conditions, "read_issued_forecast", reader)
    response = preview_client.get(_url(bad_id))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_issued_forecast_id"
    assert "data_kind" not in response.json()
    reader.assert_not_called()


def test_unknown_id_returns_not_found(preview_client, condition_versions):
    _, factory, objects, _ = condition_versions
    before = dict(objects.objects)
    response = preview_client.get(_url(uuid4()))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "issued_forecast_not_found"
    assert objects.objects == before and len(factory.issued_forecasts) == 2


@pytest.mark.parametrize("failure", ["missing_grid", "unsupported_version"])
def test_saved_grid_limitations_are_explicit_without_rebuilding(
    prepared_guidance, memory_service, monkeypatch, failure
):
    service, factory, objects = memory_service
    forecast = saved_forecast()["forecast"]
    if failure == "missing_grid":
        forecast.pop("local_grid_baseline")
    else:
        grid = forecast["local_grid_baseline"]
        grid["version"] = "unsupported-test-version"
        forecast["local_grid"]["version"] = grid["version"]
        from mesoforge.contracts.serialization import canonical_json_digest

        forecast["local_grid"]["sha256"] = str(canonical_json_digest(grid))
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    monkeypatch.setattr(weather_conditions, "read_issued_forecast", service.read)
    app = api.create_app(prepared_guidance)
    forbidden = Mock(side_effect=AssertionError("Unsupported preview attempted rebuilding"))
    monkeypatch.setattr(local_surface_grid, "build_local_surface_grid", forbidden)
    monkeypatch.setattr(PreparedPointForecast, "forecast", forbidden)
    before = dict(objects.objects)
    with TestClient(app) as client:
        response = client.get(_url(record.issued_forecast_id))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conditions_preview_unavailable"
    assert response.json()["error"]["message"]
    forbidden.assert_not_called()
    assert objects.objects == before and len(factory.issued_forecasts) == 1


@pytest.mark.parametrize("failure", ["missing_metadata", "missing_object", "corrupt_object"])
def test_known_version_storage_damage_is_sanitized_and_does_not_recalculate(
    preview_client, condition_versions, failure
):
    _, factory, objects, records = condition_versions
    record = records[0]
    if failure == "missing_metadata":
        del factory.stored_objects[record.content_digest]
    elif failure == "missing_object":
        del objects.metadata[record.content_digest]
    else:
        objects.objects[record.content_digest] = b"secret corrupted payload"
    before = dict(objects.objects)
    response = preview_client.get(_url(record.issued_forecast_id))
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "conditions_preview_failed",
            "message": "Could not read and verify the saved condition-preview inputs.",
        }
    }
    assert objects.objects == before and len(factory.issued_forecasts) == 2


def test_wrong_readback_identity_and_connection_details_are_not_returned(
    preview_client, condition_versions, monkeypatch
):
    service, _, _, records = condition_versions
    first, second = records
    monkeypatch.setattr(
        weather_conditions,
        "read_issued_forecast",
        Mock(return_value=service.read(second.issued_forecast_id)),
    )
    response = preview_client.get(_url(first.issued_forecast_id))
    assert response.status_code == 500
    assert str(second.issued_forecast_id) not in response.text
    monkeypatch.setattr(
        weather_conditions, "read_issued_forecast", Mock(side_effect=RuntimeError("secret DSN"))
    )
    response = preview_client.get(_url(first.issued_forecast_id))
    assert response.status_code == 500 and "secret" not in response.text


def test_cli_reads_once_and_prints_the_same_canonical_preview_without_mutation(
    preview_client, condition_versions, monkeypatch, capsys
):
    service, factory, objects, records = condition_versions
    record = records[0]
    saved = service.read(record.issued_forecast_id)
    original = deepcopy(saved)
    reader = Mock(return_value=saved)
    monkeypatch.setattr(weather_conditions, "read_issued_forecast", reader)
    before = dict(objects.objects)
    assert weather_conditions.main(["--issued-forecast-id", str(record.issued_forecast_id)]) == 0
    captured = capsys.readouterr()
    reader.assert_called_once_with(record.issued_forecast_id)
    assert captured.err == ""
    expected = {**build_conditions_preview(original), "derivation": _expected_derivation()}
    assert captured.out.encode("utf-8") == canonical_json_bytes(expected) + b"\n"
    assert saved == original
    assert objects.objects == before and len(factory.issued_forecasts) == 2


@pytest.mark.parametrize(
    "failure,code",
    [
        (NotFound("secret object"), "issued_forecast_not_found"),
        (
            ConditionsPreviewUnavailableError("Saved grid is unavailable"),
            "conditions_preview_unavailable",
        ),
        (IntegrityError("secret object digest"), "conditions_preview_failed"),
    ],
)
def test_cli_failures_have_no_success_output_and_sanitize_storage(
    monkeypatch, capsys, failure, code
):
    monkeypatch.setattr(weather_conditions, "read_issued_forecast", Mock(side_effect=failure))
    assert weather_conditions.main(["--issued-forecast-id", str(uuid4())]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == code
    assert "secret" not in captured.err
