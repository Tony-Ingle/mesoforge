"""Only automatic lookback, bounded delegation, and per-version result handling."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest

from mesoforge.application import forward_verification as forward
from tests.unit.application.test_automatic_verification import selection
from tests.unit.verification import test_issued_temperature as science_tests

match = science_tests.match
NOW = datetime(2026, 9, 11, tzinfo=UTC)


def outcome(saved: dict[str, Any], status: str = "verified") -> dict[str, Any]:
    return {
        "status": "completed",
        "downloaded_bytes": 0,
        "verification": {
            "results": [
                {
                    "issued_forecast_id": row["issued"]["issued_forecast_id"],
                    "valid_time": row["hour"]["valid_time"],
                    "status": status,
                    "verification_id": "retained-result-" + row["issued"]["issued_forecast_id"],
                    "reasons": [],
                }
                for row in saved["results"]
            ]
        },
    }


def test_versions_share_bounded_requests_and_repeat_delegates_idempotent_reuse(
    match: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = selection(match, (13, 13, 14, 19, 20))
    original = deepcopy(saved)
    selector = Mock(return_value=saved)
    monkeypatch.setattr(forward, "select_issued_forecast_hours", selector)
    calls = []

    def runner(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        subset = {
            "results": [
                row
                for row in saved["results"]
                if kwargs["start_valid_time"]
                <= datetime.fromisoformat(row["hour"]["valid_time"])
                < kwargs["end_valid_time"]
            ]
        }
        return outcome(subset, "verified" if len(calls) <= 2 else "already_existing")

    monkeypatch.setattr(forward, "run_window", runner)
    first = forward.verify_previous(45.8, -93.1, now=NOW)
    repeated = forward.verify_previous(45.8, -93.1, now=NOW)
    assert first["summary"]["verified"] == 5
    assert repeated["summary"]["already_existing"] == 5
    assert repeated["summary"]["verified"] == 0
    assert first["summary"]["errors"] == repeated["summary"]["errors"] == 0
    assert len(first["preflight"]["ready_valid_times"]) == 4
    assert len({row["issued_forecast_id"] for row in first["results"]}) == 5
    assert calls[:2] == calls[2:]
    assert [(call["start_valid_time"].hour, call["end_valid_time"].hour) for call in calls[:2]] == [
        (13, 19),
        (20, 20),
    ]
    assert calls[0]["end_valid_time"].microsecond == 1
    assert selector.call_args.kwargs == {
        "latitude": 45.8,
        "longitude": -93.1,
        "start_valid_time": datetime.min.replace(tzinfo=UTC),
        "end_valid_time": NOW,
    }
    assert saved == original


@pytest.mark.parametrize("hours", [(), (12,), (16,)])
def test_no_ready_hours_do_not_enter_observation_path(
    match: dict[str, Any], monkeypatch: pytest.MonkeyPatch, hours: tuple[int, ...]
) -> None:
    monkeypatch.setattr(
        forward, "select_issued_forecast_hours", Mock(return_value=selection(match, hours))
    )
    runner = Mock(side_effect=AssertionError("No observation path should execute"))
    monkeypatch.setattr(forward, "run_window", runner)
    now = datetime(2026, 9, 10, 16, 14, tzinfo=UTC)
    result = forward.verify_previous(45.8, -93.1, now=now)
    assert result["status"] == "nothing_to_verify"
    assert result["reason"]
    assert result["downloaded_bytes"] == 0
    assert result["windows"] == []
    assert result["summary"]["ineligible"] == int(hours == (12,))
    assert result["summary"]["deferred"] == int(hours == (16,))
    runner.assert_not_called()


def test_window_failure_does_not_prevent_later_window_or_expose_connection_details(
    match: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = selection(match, (13, 13, 20))
    monkeypatch.setattr(forward, "select_issued_forecast_hours", Mock(return_value=saved))
    runner = Mock(
        side_effect=[
            RuntimeError("private connection details"),
            outcome({"results": saved["results"][2:]}),
        ]
    )
    monkeypatch.setattr(forward, "run_window", runner)
    result = forward.verify_previous(45.8, -93.1, now=NOW)
    assert result["status"] == "partial"
    assert result["summary"]["errors"] == 2
    assert result["summary"]["verified"] == 1
    assert result["windows"][0]["error"]["code"] == "verification_failed"
    assert "private connection details" not in str(result)
    assert runner.call_count == 2


def test_unavailable_window_keeps_reason_without_recounting_ineligible_hours(
    match: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = selection(match, (13, 14, 15, 20))
    saved["results"][1]["hour"]["temperature"]["value"] = None
    monkeypatch.setattr(forward, "select_issued_forecast_hours", Mock(return_value=saved))
    unavailable = {
        "status": "unavailable",
        "reason": "No usable station candidates.",
        "downloaded_bytes": 0,
        "verification": None,
    }
    runner = Mock(side_effect=[unavailable, outcome({"results": saved["results"][3:]})])
    monkeypatch.setattr(forward, "run_window", runner)
    result = forward.verify_previous(45.8, -93.1, now=NOW)
    assert result["summary"] == {
        "verified": 1,
        "unavailable": 2,
        "ineligible": 1,
        "already_existing": 0,
        "errors": 0,
        "deferred": 0,
    }
    assert result["results"][0]["reasons"] == [unavailable["reason"]]
    assert result["results"][1]["reasons"] == [
        "forecast_temperature_missing_nonfinite_or_not_kelvin"
    ]


def test_matching_margin_is_complete_at_boundary_and_downloads_are_reported(
    match: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = selection(match, (13,))
    monkeypatch.setattr(forward, "select_issued_forecast_hours", Mock(return_value=saved))
    completed = outcome(saved)
    completed["downloaded_bytes"] = 713
    runner = Mock(return_value=completed)
    monkeypatch.setattr(forward, "run_window", runner)
    at_margin = datetime(2026, 9, 10, 13, 15, tzinfo=UTC)
    deferred = forward.verify_previous(45.8, -93.1, now=at_margin - timedelta(microseconds=1))
    ready = forward.verify_previous(45.8, -93.1, now=at_margin)
    assert deferred["summary"]["deferred"] == 1
    assert ready["summary"]["verified"] == 1
    assert ready["downloaded_bytes"] == 713
    runner.assert_called_once()


def test_naive_evaluation_time_is_rejected_before_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    selector = Mock()
    monkeypatch.setattr(forward, "select_issued_forecast_hours", selector)
    with pytest.raises(ValueError, match="timezone"):
        forward.verify_previous(45.8, -93.1, now=NOW.replace(tzinfo=None))
    selector.assert_not_called()
