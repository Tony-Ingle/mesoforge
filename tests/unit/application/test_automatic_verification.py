"""Only the new forecast preflight, request derivation, and command boundaries."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from mesoforge.application import automatic_verification as automatic
from mesoforge.application import prepared_observations as preparation
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
)
from tests.unit.application.test_batch_forecast import FIRST, LAST, OUTSIDE, write_config
from tests.unit.application.test_prepared_observations import fixture_discovery
from tests.unit.verification import test_issued_temperature as science_tests

match = science_tests.match
NOW = datetime(2026, 9, 10, 16, 15, tzinfo=UTC)


def selection(match: dict[str, Any], hours: tuple[int, ...]) -> dict[str, Any]:
    rows = []
    for index, hour in enumerate(hours):
        forecast = deepcopy(match["forecast"])
        forecast["valid_time"] = f"2026-09-10T{hour:02}:00:00Z"
        rows.append(
            {
                "issued": {"issued_forecast_id": str(index), "issued_at": match["issued_at"]},
                "hour": forecast,
            }
        )
    return {
        "results": rows,
        "start_valid_time": "2026-09-10T12:00:00Z",
        "end_valid_time": "2026-09-10T18:00:00Z",
    }


def test_request_uses_only_ready_actual_times_and_preserves_each_issued_version(
    match: dict[str, Any],
) -> None:
    saved = selection(match, (12, 13, 13, 14, 17))
    result = automatic.derive_request(saved, now=NOW)
    assert [row["status"] for row in result["hours"]] == [
        "ineligible",
        "ready",
        "ready",
        "ready",
        "deferred",
    ]
    assert result["query_window_start"] == "2026-09-10T12:45:00+00:00"
    assert result["query_window_end"] == "2026-09-10T14:15:00+00:00"
    assert len(result["ready_valid_times"]) == 2
    assert len({row["issued_forecast_id"] for row in result["hours"]}) == 5


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            {"temperature": {"value": None, "unit": "K"}},
            "forecast_temperature_missing_nonfinite_or_not_kelvin",
        ),
        ({"missing_reasons": ["missing_model"]}, "forecast_has_explicit_missingness"),
        ({"sources": []}, "forecast_source_cycles_unavailable"),
    ],
)
def test_existing_forecast_ineligibility_prevents_observation_request(
    match: dict[str, Any],
    change: dict[str, Any],
    reason: str,
) -> None:
    saved = selection(match, (13,))
    saved["results"][0]["hour"].update(change)
    result = automatic.derive_request(saved, now=NOW)
    assert result["ready_valid_times"] == []
    assert reason in result["hours"][0]["reasons"]


def test_recent_hour_waits_for_complete_margin_but_issue_before_valid_is_not_overrestricted(
    match: dict[str, Any],
) -> None:
    saved = selection(match, (16,))
    saved["results"][0]["issued"]["issued_at"] = "2026-09-10T15:59:00Z"
    deferred = automatic.derive_request(saved, now=NOW - timedelta(seconds=1))
    assert deferred["hours"][0]["reasons"] == ["observation_matching_window_not_complete"]
    assert automatic.derive_request(saved, now=NOW)["ready_valid_times"] == [
        "2026-09-10T16:00:00+00:00"
    ]


def test_excessive_derived_span_fails_before_acquisition(match: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="six-hour"):
        automatic.derive_request(selection(match, (13, 20)), now=NOW + timedelta(days=1))


@pytest.mark.parametrize("hours", [(), (12,)])
def test_no_ready_hours_return_without_loading_configuration_or_acquiring(
    match: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hours: tuple[int, ...],
) -> None:
    monkeypatch.setattr(
        automatic, "select_issued_forecast_hours", lambda **kw: selection(match, hours)
    )

    def unexpected() -> None:
        pytest.fail("No eligible saved hours must return before observation configuration or I/O")

    monkeypatch.setattr(automatic, "load_observation_configuration", unexpected)
    monkeypatch.setattr(automatic, "get_station_candidates", unexpected)
    assert (
        automatic.main(
            [
                "--lat",
                "45.8",
                "--lon",
                "-93.1",
                "--start-valid-time",
                "2026-09-10T12:00:00Z",
                "--end-valid-time",
                "2026-09-10T13:00:00Z",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "nothing_to_verify"
    assert result["downloaded_bytes"] == 0
    assert result["verification"] is None
    assert result["reason"]


def test_one_actual_valid_hour_acquires_exact_half_hour_with_automatic_stations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FixtureAviationWeatherTransport()
    transport.metar_queue.append(FakeHttpResponse(200, {}, b"[]"))
    monkeypatch.setattr(preparation, "RequestsAviationWeatherHttpTransport", lambda: transport)
    monkeypatch.setattr(preparation, "SystemClock", lambda: FixedClock(NOW))
    discovery = Mock(side_effect=fixture_discovery)
    monkeypatch.setattr(preparation, "get_station_candidates", discovery)
    valid = datetime(2026, 9, 10, 13, tzinfo=UTC)
    metadata = preparation.acquire_for_valid_times(
        tmp_path / "single",
        latitude=45.8,
        longitude=-93.1,
        valid_times=(valid, valid),
    )
    query = parse_qs(urlsplit(transport.get_calls[0]).query)
    assert query["date"] == ["2026-09-10T13:15:00Z"]
    assert float(query["hours"][0]) == 0.5
    assert query["ids"] == ["KCBG,KJMR,KROS"]
    assert metadata["query_window_start"] == "2026-09-10T12:45:00+00:00"
    discovery.assert_called_once_with(45.8, -93.1)


def test_batch_preserves_order_and_results_past_an_unsupported_coordinate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        {
            "status": "completed",
            "downloaded_bytes": 0,
            "observations_artifact_id": name,
            "verification": {"summary": {"verified": 0, "already_existing": 2, "errors": 0}},
        }
        for name in ("first-observations", "last-observations")
    ]
    runner = Mock(side_effect=outcomes)
    monkeypatch.setattr(automatic, "run_window", runner)
    first = NOW - timedelta(hours=2)
    result = automatic.run_batch(
        write_config(tmp_path, [FIRST, OUTSIDE, LAST]), start_valid_time=first, end_valid_time=NOW
    )
    assert result["summary"] == {"completed": 2, "nothing_to_verify": 0, "errors": 1}
    assert [r["location"] for r in result["results"]] == [FIRST, OUTSIDE, LAST]
    assert [r["index"] for r in result["results"]] == [0, 1, 2]
    assert result["results"][1]["error"]["code"] == "unsupported_coordinate"
    assert result["results"][0]["result"] is outcomes[0]
    assert result["results"][2]["result"] is outcomes[1]
    assert [call.kwargs for call in runner.call_args_list] == [
        {
            "latitude": location["lat"],
            "longitude": location["lon"],
            "start_valid_time": first,
            "end_valid_time": NOW,
        }
        for location in (FIRST, LAST)
    ]


def test_batch_cli_keeps_partial_failures_separate_and_continues_to_no_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    partial = {
        "status": "completed",
        "downloaded_bytes": 0,
        "verification": {
            "summary": {"verified": 1, "errors": 1},
            "results": [{"issued_forecast_id": "original", "status": "verified"}],
        },
    }
    nothing = {"status": "nothing_to_verify", "downloaded_bytes": 0, "verification": None}
    runner = Mock(side_effect=[RuntimeError("private connection details"), partial, nothing])
    monkeypatch.setattr(automatic, "run_window", runner)
    code = automatic.main(
        [
            "--config",
            str(write_config(tmp_path, [FIRST, LAST, FIRST])),
            "--start-valid-time",
            "2026-09-10T13:00:00Z",
            "--end-valid-time",
            "2026-09-10T16:00:00Z",
        ]
    )
    output = capsys.readouterr().out
    result = json.loads(output)
    assert code == 1
    assert "private connection details" not in output
    assert result["summary"] == {"completed": 0, "nothing_to_verify": 1, "errors": 2}
    assert result["results"][0]["error"]["code"] == "verification_failed"
    assert result["results"][1]["error"]["code"] == "verification_incomplete"
    assert result["results"][1]["result"] == partial
    assert result["results"][2]["result"] == nothing
    assert runner.call_count == 3


@pytest.mark.parametrize("start", ["2026-09-10T16:00:00Z", "2026-09-10T13:00:00"])
def test_batch_rejects_invalid_global_window_even_with_empty_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: str,
) -> None:
    runner = Mock()
    monkeypatch.setattr(automatic, "run_window", runner)
    assert (
        automatic.main(
            [
                "--config",
                str(write_config(tmp_path, [])),
                "--start-valid-time",
                start,
                "--end-valid-time",
                "2026-09-10T16:00:00Z",
            ]
        )
        == 2
    )
    runner.assert_not_called()


def test_batch_reuses_config_rules_and_continues_past_invalid_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nothing = {"status": "nothing_to_verify", "downloaded_bytes": 0, "verification": None}
    runner = Mock(return_value=nothing)
    monkeypatch.setattr(automatic, "run_window", runner)
    config = write_config(tmp_path, [{**FIRST, "station_id": "KROS"}, LAST])
    # Windows-authored BOM remains supported by the shared reader.
    config.write_text(config.read_text(), encoding="utf-8-sig")
    result = automatic.run_batch(
        config, start_valid_time=NOW - timedelta(hours=1), end_valid_time=NOW
    )
    assert result["results"][0]["error"]["code"] == "invalid_location"
    assert result["results"][1]["result"] == nothing
    assert runner.call_count == 1


def test_empty_batch_cli_succeeds_without_running_any_coordinate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = Mock()
    monkeypatch.setattr(automatic, "run_window", runner)
    assert (
        automatic.main(
            [
                "--config",
                str(write_config(tmp_path, [])),
                "--start-valid-time",
                "2026-09-10T13:00:00Z",
                "--end-valid-time",
                "2026-09-10T16:00:00Z",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["results"] == []
    runner.assert_not_called()


@pytest.mark.parametrize("missing_elevation", [False, True])
def test_eligible_hours_without_usable_discovered_metadata_remain_unavailable(
    match: dict[str, Any], monkeypatch: pytest.MonkeyPatch, missing_elevation: bool
) -> None:
    monkeypatch.setattr(
        automatic, "select_issued_forecast_hours", lambda **kw: selection(match, (13,))
    )
    discovered = fixture_discovery()
    if missing_elevation:
        for candidate in discovered["candidates"]:
            candidate["elevation_m"] = None
    else:
        discovered["candidates"] = []
    monkeypatch.setattr(automatic, "get_station_candidates", lambda *args: discovered)
    acquire = Mock(side_effect=AssertionError("Unusable metadata must not request METAR data"))
    monkeypatch.setattr(automatic, "acquire_for_valid_times", acquire)
    result = automatic.run_window(
        latitude=45.8,
        longitude=-93.1,
        start_valid_time=datetime(2026, 9, 10, 13, tzinfo=UTC),
        end_valid_time=datetime(2026, 9, 10, 14, tzinfo=UTC),
    )
    assert result["status"] == "unavailable"
    assert result["reason"]
    assert result["downloaded_bytes"] == 0
    assert result["verification"] is None
    assert result["preflight"]["station_ids"] == []
    assert result["station_discovery"] == discovered
    assert len(result["station_metadata_exclusions"]) == (3 if missing_elevation else 0)
    assert all(row["reason"] for row in result["station_metadata_exclusions"])
    acquire.assert_not_called()
