"""Read-only evolution over exact stored versions, using the existing storage doubles."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import local_surface_grid, weather_conditions, weather_transitions
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting import cloud_cover
from mesoforge.forecasting.conditions import build_conditions_preview
from mesoforge.forecasting.transitions import build_transitions
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWork
from tests.unit.application.test_forecast_issuance import memory_service as memory_service
from tests.unit.forecasting.test_conditions import (
    POP,
    QPF,
    active_sky_field,
    center_hour,
    refresh_saved_grid,
    saved_forecast,
)
from tests.unit.test_issued_forecast_api import prepared_guidance as prepared_guidance
from tests.unit.test_weather_conditions_api import _expected_derivation as _conditions_derivation


def evolving_forecast():
    """Dry then rainy hours under cloudy then mostly clear sky; other fields unchanged."""
    saved = saved_forecast()
    for index in range(36):
        hour = center_hour(saved, index)
        fields = hour["surface"]["fields"]
        if index < 6:
            fields[POP]["value"] = 0.0
            fields[QPF]["value"] = 0.0
        if index < 12:
            percentage = 95.0 if index < 6 else 15.0
            fields[cloud_cover.CLOUD] = active_sky_field(percentage, hour["valid_time"])
    refresh_saved_grid(saved)
    return saved


@pytest.fixture()
def transition_version(memory_service):
    service, factory, objects = memory_service
    saved = evolving_forecast()
    saved["forecast"]["hourly_report"] = {"display_timezone": "America/Denver"}
    record = service.issue(saved["forecast"], batch_run_id=uuid4(), location_index=0)
    return service, factory, objects, record


@pytest.fixture()
def client(prepared_guidance, transition_version, monkeypatch):
    service, _, _, _ = transition_version
    monkeypatch.setattr(weather_transitions, "read_issued_forecast", service.read)
    monkeypatch.setattr(weather_conditions, "read_issued_forecast", service.read)
    app = api.create_app(prepared_guidance)
    forbidden = Mock(side_effect=AssertionError("Transitions attempted source I/O or mutation"))
    for owner, name in (
        (PreparedPointForecast, "forecast"),
        (PreparedPointForecast, "from_directory"),
        (local_surface_grid, "build_local_surface_grid"),
        (ForecastIssuanceService, "issue"),
        (InMemoryObjectStore, "put_if_absent"),
        (InMemoryUnitOfWork, "commit"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    monkeypatch.setattr("xarray.open_dataset", forbidden)
    with TestClient(app) as test_client:
        yield test_client
    forbidden.assert_not_called()


def _url(identifier):
    return f"/issued-forecasts/{identifier}/conditions/transitions"


def _expected_derivation():
    identity = weather_transitions._derivation_identity()
    assert identity["conditions"] == _conditions_derivation()
    return identity


def test_default_uses_the_issuance_display_zone_and_matches_the_pure_function(
    client, transition_version
):
    service, factory, objects, record = transition_version
    before = dict(objects.objects)
    saved = json.loads(before[record.content_digest])
    response = client.get(_url(record.issued_forecast_id))
    repeated = client.get(_url(record.issued_forecast_id))
    assert response.status_code == repeated.status_code == 200
    assert response.content == repeated.content
    result = response.json()
    expected = build_transitions(
        build_conditions_preview(saved, scope="point"),
        display_timezone="America/Denver",
        timezone_source="issuance_hourly_report",
    )
    assert result == {**expected, "derivation": _expected_derivation()}
    assert result["display_timezone"] == {
        "name": "America/Denver",
        "source": "issuance_hourly_report",
    }
    assert result["input"]["issued_forecast_id"] == str(record.issued_forecast_id)
    assert result["input"]["issued_payload_digest"] == str(record.content_digest)
    assert result["input"]["conditions_scope"] == "point" and result["input"]["hours"] == 36
    kinds = [(f["type"], f["window"]["start"], f["window"]["end"]) for f in result["transitions"]]
    assert kinds == [
        ("precipitation_onset", "2026-09-11T18:00:00Z", "2026-09-11T19:00:00Z"),
        ("precipitation_wording_onset", "2026-09-11T18:00:00Z", "2026-09-11T19:00:00Z"),
        ("sky_trend", "2026-09-11T18:00:00Z", "2026-09-11T19:00:00Z"),
    ]
    assert [item["text"] for item in result["rendering"]["items"]] == [
        "Rain developing between 12 PM and 1 PM Friday",
        "Becoming mostly clear between 12 PM and 1 PM Friday",
    ]
    assert {g["track"] for g in result["gaps"]} == {"sky"}
    assert all(g["hour_ref"].startswith("center_point.hours[") for g in result["gaps"])
    assert service.read(record.issued_forecast_id) == saved
    assert objects.objects == before
    assert len(factory.issued_forecasts) == len(factory.stored_objects) == 1
    assert factory.artifacts == factory.activities == {}


def test_requested_zone_overrides_the_issuance_zone_without_changing_facts(
    client, transition_version
):
    _, _, _, record = transition_version
    default = client.get(_url(record.issued_forecast_id)).json()
    utc = client.get(_url(record.issued_forecast_id), params={"display_timezone": "UTC"}).json()
    assert utc["transitions"] == default["transitions"]
    assert utc["display_timezone"] == {"name": "UTC", "source": "request"}
    assert [item["text"] for item in utc["rendering"]["items"]] == [
        "Rain developing between 6 PM and 7 PM Friday",
        "Becoming mostly clear between 6 PM and 7 PM Friday",
    ]


def test_unknown_display_zone_and_invalid_id_are_rejected_before_storage(client, monkeypatch):
    reader = Mock(side_effect=AssertionError("Invalid request reached storage"))
    monkeypatch.setattr(weather_transitions, "read_issued_forecast", reader)
    response = client.get(_url(uuid4()), params={"display_timezone": "Mars/Olympus"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_display_timezone"
    response = client.get(_url("not-a-uuid"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_issued_forecast_id"
    reader.assert_not_called()


def test_unknown_version_returns_not_found(client, transition_version):
    _, factory, objects, _ = transition_version
    before = dict(objects.objects)
    response = client.get(_url(uuid4()))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "issued_forecast_not_found"
    assert objects.objects == before and len(factory.issued_forecasts) == 1


def test_point_only_issuance_is_unavailable_not_rebuilt(
    prepared_guidance, memory_service, monkeypatch
):
    service, factory, objects = memory_service
    forecast = saved_forecast()["forecast"]
    forecast.pop("local_grid_baseline")
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    monkeypatch.setattr(weather_transitions, "read_issued_forecast", service.read)
    forbidden = Mock(side_effect=AssertionError("Unsupported preview attempted rebuilding"))
    monkeypatch.setattr(local_surface_grid, "build_local_surface_grid", forbidden)
    before = dict(objects.objects)
    with TestClient(api.create_app(prepared_guidance)) as test_client:
        response = test_client.get(_url(record.issued_forecast_id))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transitions_preview_unavailable"
    forbidden.assert_not_called()
    assert objects.objects == before and len(factory.issued_forecasts) == 1


def test_cli_prints_the_canonical_preview_and_rejects_unknown_zones_before_reading(
    transition_version, monkeypatch, capsys
):
    service, factory, objects, record = transition_version
    saved = service.read(record.issued_forecast_id)
    reader = Mock(return_value=deepcopy(saved))
    monkeypatch.setattr(weather_transitions, "read_issued_forecast", reader)
    before = dict(objects.objects)
    identifier = str(record.issued_forecast_id)
    assert weather_transitions.main(["--issued-forecast-id", identifier]) == 0
    expected = {
        **build_transitions(
            build_conditions_preview(saved, scope="point"),
            display_timezone="America/Denver",
            timezone_source="issuance_hourly_report",
        ),
        "derivation": _expected_derivation(),
    }
    assert capsys.readouterr().out.encode("utf-8") == canonical_json_bytes(expected) + b"\n"
    assert (
        weather_transitions.main(
            ["--issued-forecast-id", identifier, "--display-timezone", "America/Chicago"]
        )
        == 0
    )
    chicago = json.loads(capsys.readouterr().out)
    assert chicago["display_timezone"] == {"name": "America/Chicago", "source": "request"}
    assert (
        weather_transitions.main(
            ["--issued-forecast-id", identifier, "--display-timezone", "Mars/Olympus"]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "invalid_display_timezone"
    assert reader.call_count == 2
    assert objects.objects == before and len(factory.issued_forecasts) == 1


def test_conditions_route_is_unchanged_by_the_transitions_route(client, transition_version):
    _, _, _, record = transition_version
    conditions = client.get(f"/issued-forecasts/{record.issued_forecast_id}/conditions")
    assert conditions.status_code == 200
    assert conditions.json()["scope"]["requested"] == "point"
    assert "transitions" not in conditions.json()
    assert weather_conditions.preview_weather_conditions.__name__ == "preview_weather_conditions"
