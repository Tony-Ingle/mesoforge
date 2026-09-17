"""Read-only period summaries over exact stored versions, using the storage doubles."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import (
    local_surface_grid,
    weather_conditions,
    weather_periods,
    weather_transitions,
)
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.conditions import build_conditions_preview
from mesoforge.forecasting.periods import build_period_summary
from mesoforge.forecasting.transitions import build_transitions
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWork
from tests.unit.application.test_forecast_issuance import memory_service as memory_service
from tests.unit.forecasting.test_conditions import saved_forecast
from tests.unit.test_issued_forecast_api import prepared_guidance as prepared_guidance
from tests.unit.test_weather_transitions_api import evolving_forecast


@pytest.fixture()
def period_version(memory_service):
    service, factory, objects = memory_service
    saved = evolving_forecast()
    saved["forecast"]["hourly_report"] = {"display_timezone": "America/Denver"}
    record = service.issue(saved["forecast"], batch_run_id=uuid4(), location_index=0)
    return service, factory, objects, record


@pytest.fixture()
def client(prepared_guidance, period_version, monkeypatch):
    service, _, _, _ = period_version
    for module in (weather_transitions, weather_conditions):
        monkeypatch.setattr(module, "read_issued_forecast", service.read)
    app = api.create_app(prepared_guidance)
    forbidden = Mock(side_effect=AssertionError("Periods attempted source I/O or mutation"))
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
    return f"/issued-forecasts/{identifier}/conditions/periods"


def _expected(saved, zone, source):
    transitions = build_transitions(
        build_conditions_preview(saved, scope="point"),
        display_timezone=zone,
        timezone_source=source,
    )
    return {
        **build_period_summary(transitions),
        "derivation": {
            **weather_periods._derivation_identity(),
            "transitions": weather_transitions._derivation_identity(),
        },
    }


def test_default_periods_use_the_issuance_zone_and_match_the_pure_functions(client, period_version):
    service, factory, objects, record = period_version
    before = dict(objects.objects)
    saved = json.loads(before[record.content_digest])
    response = client.get(_url(record.issued_forecast_id))
    repeated = client.get(_url(record.issued_forecast_id))
    assert response.status_code == repeated.status_code == 200
    assert response.content == repeated.content
    result = response.json()
    assert result == _expected(saved, "America/Denver", "issuance_hourly_report")
    assert result["schema_version"] == "mesoforge.period-summary.v1"
    assert result["input"]["issued_forecast_id"] == str(record.issued_forecast_id)
    assert result["display_timezone"] == {
        "name": "America/Denver",
        "source": "issuance_hourly_report",
    }
    # Target 12Z Sep 11 = 06:00 MDT: hours run 07:00 Friday through 18:00 Saturday, and
    # the final 18:00 endpoint opens a one-hour partial Saturday night period.
    assert [(p["id"], p["hours"]) for p in result["periods"]] == [
        ("2026-09-11-day", 11),
        ("2026-09-11-night", 12),
        ("2026-09-12-day", 12),
        ("2026-09-12-night", 1),
    ]
    assert result["periods"][0]["partial_start"] is True
    assert result["periods"][2]["partial_end"] is False
    assert result["periods"][3]["partial_start"] is False
    assert result["periods"][3]["partial_end"] is True
    day = result["periods"][0]
    assert [ref["type"] for ref in day["transition_refs"]] == [
        "precipitation_onset",
        "precipitation_wording_onset",
        "sky_trend",
    ]
    assert [s["text"] for s in day["sentences"]] == [
        "Rain developing early Friday afternoon.",
        "Becoming mostly clear early Friday afternoon.",
    ]
    assert day["omitted_facts"][0]["reason"] == "applicability_transitions_are_structured_only"
    assert {g["track"] for g in result["periods"][1]["gaps"]} == {"sky"}
    assert result["text"] == (
        "Rain developing early Friday afternoon. Becoming mostly clear early Friday afternoon."
    )
    assert service.read(record.issued_forecast_id) == saved
    assert objects.objects == before
    assert len(factory.issued_forecasts) == len(factory.stored_objects) == 1
    assert factory.artifacts == factory.activities == {}


def test_requested_zone_changes_period_boundaries_but_not_facts(client, period_version):
    _, _, _, record = period_version
    default = client.get(_url(record.issued_forecast_id)).json()
    utc = client.get(_url(record.issued_forecast_id), params={"display_timezone": "UTC"}).json()
    assert utc["transitions"] == default["transitions"]
    assert utc["display_timezone"] == {"name": "UTC", "source": "request"}
    assert [p["id"] for p in utc["periods"]] == [
        "2026-09-11-day",
        "2026-09-11-night",
        "2026-09-12-day",
        "2026-09-12-night",
    ]
    assert utc["periods"][0]["utc_start"] == "2026-09-11T06:00:00Z"
    assert utc["periods"][0]["partial_start"] is True and utc["periods"][3]["partial_end"] is True


def test_unknown_zone_and_invalid_id_are_rejected_before_storage(client, monkeypatch):
    reader = Mock(side_effect=AssertionError("Invalid request reached storage"))
    monkeypatch.setattr(weather_transitions, "read_issued_forecast", reader)
    response = client.get(_url(uuid4()), params={"display_timezone": "Mars/Olympus"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_display_timezone"
    response = client.get(_url("not-a-uuid"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_issued_forecast_id"
    reader.assert_not_called()


def test_unknown_version_returns_not_found(client, period_version):
    _, factory, objects, _ = period_version
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
    assert response.json()["error"]["code"] == "periods_preview_unavailable"
    forbidden.assert_not_called()
    assert objects.objects == before and len(factory.issued_forecasts) == 1


def test_cli_prints_the_canonical_summary_and_rejects_unknown_zones_before_reading(
    period_version, monkeypatch, capsys
):
    service, factory, objects, record = period_version
    saved = service.read(record.issued_forecast_id)
    reader = Mock(return_value=deepcopy(saved))
    monkeypatch.setattr(weather_transitions, "read_issued_forecast", reader)
    before = dict(objects.objects)
    identifier = str(record.issued_forecast_id)
    assert weather_periods.main(["--issued-forecast-id", identifier]) == 0
    expected = _expected(saved, "America/Denver", "issuance_hourly_report")
    assert capsys.readouterr().out.encode("utf-8") == canonical_json_bytes(expected) + b"\n"
    assert (
        weather_periods.main(["--issued-forecast-id", identifier, "--display-timezone", "UTC"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["display_timezone"]["name"] == "UTC"
    assert (
        weather_periods.main(
            ["--issued-forecast-id", identifier, "--display-timezone", "Mars/Olympus"]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "invalid_display_timezone"
    assert reader.call_count == 2
    assert objects.objects == before and len(factory.issued_forecasts) == 1
