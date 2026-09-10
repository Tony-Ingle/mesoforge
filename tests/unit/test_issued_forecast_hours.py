"""Offline selection of saved hours; no regeneration, observation lookup, or scoring."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWork
from tests.unit import test_issued_forecast_api as retrieval_tests

prepared_guidance = retrieval_tests.prepared_guidance
saved_versions = retrieval_tests.saved_versions

QUERY = {
    "lat": 45.8,
    "lon": -93.1,
    "start_valid_time": "2026-08-30T13:00:00Z",
    "end_valid_time": "2026-08-30T16:00:00Z",
}


def test_selection_preserves_both_versions_and_every_original_hour_field(
    prepared_guidance, saved_versions, monkeypatch
):
    service, factory, objects, records = saved_versions
    original = {
        str(r.issued_forecast_id): json.loads(objects.objects[r.content_digest]) for r in records
    }
    before = dict(objects.objects)
    monkeypatch.setattr(api, "select_issued_forecast_hours", service.select_hours)
    app = api.create_app(prepared_guidance)
    forbidden = Mock(side_effect=AssertionError("Selection attempted calculation or mutation"))
    for owner, name in (
        (PreparedPointForecast, "forecast"),
        (PreparedPointForecast, "from_directory"),
        (ForecastIssuanceService, "issue"),
        (InMemoryObjectStore, "put_if_absent"),
        (InMemoryUnitOfWork, "commit"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    with TestClient(app) as client:
        first = client.get("/issued-forecast-hours", params=QUERY)
        repeated = client.get("/issued-forecast-hours", params=QUERY)
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    payload = first.json()
    assert payload["interval_closure"] == "left_closed_right_open"
    assert payload["start_valid_time"] == QUERY["start_valid_time"]
    assert payload["end_valid_time"] == QUERY["end_valid_time"]
    assert (payload["latitude"], payload["longitude"]) == (45.8, -93.1)
    rows = payload["results"]
    assert len(rows) == 6
    assert [r["hour"]["horizon_hours"] for r in rows] == [1, 1, 2, 2, 3, 3]
    # Issuance is later than this historical window; selection must not invent a cutoff.
    assert all(row["issued"]["issued_at"] == "2026-09-10T12:00:00Z" for row in rows)
    for horizon in (1, 2, 3):
        matching = [row for row in rows if row["hour"]["horizon_hours"] == horizon]
        assert {r["issued"]["issued_forecast_id"] for r in matching} == set(original)
    for row in rows:
        saved = original[row["issued"]["issued_forecast_id"]]
        assert row["hour"] == saved["forecast"]["hours"][row["hour"]["horizon_hours"] - 1]
        assert row["hour"]["temperature"]["value"] == pytest.approx(
            282 + row["hour"]["horizon_hours"], abs=1e-10
        )
        assert row["hour"]["temperature"]["unit"] == "K"
        assert row["code_identity"] == saved["code_identity"]
        assert row["forecast_context"] == {
            k: v for k, v in saved["forecast"].items() if k != "hours"
        }
        record = next(
            r for r in records if str(r.issued_forecast_id) == row["issued"]["issued_forecast_id"]
        )
        assert row["issued"] == record.model_dump(mode="json")
    forbidden.assert_not_called()
    assert objects.objects == before
    assert len(factory.issued_forecasts) == len(factory.stored_objects) == 2


@pytest.mark.parametrize(
    "override",
    [
        {"start_valid_time": "2026-08-29T00:00:00Z", "end_valid_time": "2026-08-30T13:00:00Z"},
        {"start_valid_time": "2026-09-01T01:00:00Z", "end_valid_time": "2026-09-02T00:00:00Z"},
        {"lat": 45.80001},
        {"lat": 0, "lon": 0},
    ],
)
def test_no_matching_times_or_exact_coordinate_returns_empty(
    prepared_guidance, saved_versions, monkeypatch, override
):
    service, _, _, _ = saved_versions
    monkeypatch.setattr(api, "select_issued_forecast_hours", service.select_hours)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get("/issued-forecast-hours", params={**QUERY, **override})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_offset_times_normalize_and_subhour_boundaries_select_actual_valid_times(
    prepared_guidance, saved_versions, monkeypatch
):
    service, _, _, _ = saved_versions
    monkeypatch.setattr(api, "select_issued_forecast_hours", service.select_hours)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(
            "/issued-forecast-hours",
            params={
                **QUERY,
                "start_valid_time": "2026-08-30T08:30:00-05:00",
                "end_valid_time": "2026-08-30T09:30:00-05:00",
            },
        )
    assert response.status_code == 200
    assert response.json()["start_valid_time"] == "2026-08-30T13:30:00Z"
    assert response.json()["end_valid_time"] == "2026-08-30T14:30:00Z"
    assert [r["hour"]["valid_time"] for r in response.json()["results"]] == [
        "2026-08-30T14:00:00Z"
    ] * 2


def test_final_saved_hour_at_window_start_is_included(saved_versions):
    service, _, _, _ = saved_versions
    result = service.select_hours(
        latitude=45.8,
        longitude=-93.1,
        start_valid_time=datetime.fromisoformat("2026-09-01T00:00:00Z"),
        end_valid_time=datetime.fromisoformat("2026-09-01T01:00:00Z"),
    )
    assert len(result["results"]) == 2
    assert all(row["hour"]["horizon_hours"] == 36 for row in result["results"])
    assert all(row["hour"]["valid_time"] == "2026-09-01T00:00:00Z" for row in result["results"])


@pytest.mark.parametrize(
    "override",
    [
        {"lat": "nan"},
        {"lon": "inf"},
        {"lat": 91},
        {"lon": -181},
        {"lat": "text"},
        {"start_valid_time": "not-a-time"},
        {"end_valid_time": "2026-08-30T16:00:00"},
        {"start_valid_time": "2026-08-30T16:00:00Z"},
        {"start_valid_time": "2026-08-30T17:00:00Z"},
    ],
)
def test_invalid_queries_fail_before_storage(prepared_guidance, monkeypatch, override):
    forbidden = Mock(side_effect=AssertionError("Invalid query attempted storage"))
    monkeypatch.setattr(api, "select_issued_forecast_hours", forbidden)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get("/issued-forecast-hours", params={**QUERY, **override})
    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "invalid_issued_forecast_hour_query"
    forbidden.assert_not_called()


@pytest.mark.parametrize("missing", list(QUERY))
def test_missing_query_parameters_have_history_specific_errors(prepared_guidance, missing):
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get(
            "/issued-forecast-hours", params={k: v for k, v in QUERY.items() if k != missing}
        )
    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "invalid_issued_forecast_hour_query"


def test_missing_values_remain_explicit_in_selected_hours(saved_versions):
    service, _, objects, records = saved_versions
    forecast = json.loads(objects.objects[records[0].content_digest])["forecast"]
    forecast["hours"][0]["temperature"]["value"] = None
    forecast["hours"][0]["missing_reasons"] = ["HRRR: prepared guidance file is missing"]
    missing = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    result = service.select_hours(
        latitude=45.8,
        longitude=-93.1,
        start_valid_time=datetime.fromisoformat(QUERY["start_valid_time"]),
        end_valid_time=datetime.fromisoformat(QUERY["end_valid_time"]),
    )
    row = next(
        r
        for r in result["results"]
        if r["issued"]["issued_forecast_id"] == str(missing.issued_forecast_id)
    )
    assert row["hour"] == forecast["hours"][0]
    assert row["hour"]["temperature"] == {"value": None, "unit": "K"}
    assert [s["weight"] for s in row["hour"]["sources"]] == [0.7, 0.3]


def test_bad_stored_payload_returns_error_without_partial_matches(
    prepared_guidance, saved_versions, monkeypatch
):
    service, factory, objects, records = saved_versions
    objects.objects[records[0].content_digest] = b"corrupt"
    before = dict(objects.objects)
    monkeypatch.setattr(api, "select_issued_forecast_hours", service.select_hours)
    with TestClient(api.create_app(prepared_guidance)) as client:
        response = client.get("/issued-forecast-hours", params=QUERY)
    assert response.status_code == 500
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "issued_forecast_hour_selection_failed"
    assert objects.objects == before
    assert len(factory.issued_forecasts) == 2


def test_direct_application_query_rejects_naive_time_before_storage(saved_versions, monkeypatch):
    service, _, _, _ = saved_versions
    forbidden = Mock(side_effect=AssertionError("Invalid query attempted storage"))
    monkeypatch.setattr(service, "_uow_factory", forbidden)
    with pytest.raises(ValueError, match="timezone-aware"):
        service.select_hours(
            latitude=45.8,
            longitude=-93.1,
            start_valid_time=datetime(2026, 8, 30, 13),
            end_valid_time=datetime.fromisoformat(QUERY["end_valid_time"]),
        )
    forbidden.assert_not_called()
