"""Only forward orchestration is new; provider and scientific paths have their own tests."""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from mesoforge.application import batch_forecast, forward_run
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy

TARGET = "2026-09-16T22:00:00+00:00"
SELECTION = {
    "status": "selected",
    "target_reference_time": TARGET,
    "selected_cycles": {"HRRR": "fixed-evidence"},
}


@pytest.fixture(autouse=True)
def no_storage_lock(monkeypatch):
    # Unit tests exercise orchestration only; the real advisory lock needs PostgreSQL.
    monkeypatch.setattr(forward_run, "_acquire_run_lock", nullcontext)


def make_issuer(existing=()):
    issuer = Mock()
    issuer.find_versions.return_value = tuple(existing)
    return issuer


@pytest.mark.parametrize("name", [None, "Minneapolis", ""])
def test_optional_name_does_not_change_required_coordinate_inputs(name):
    location = {"lat": 44.98859, "lon": -93.25557}
    if name is not None:
        location["name"] = name
    assert batch_forecast._coordinates(location) == (44.98859, -93.25557)


@pytest.mark.parametrize("extra", [{"name": 10}, {"station": "KMSP"}, {"county": "Hennepin"}])
def test_no_additional_geographic_configuration(extra):
    with pytest.raises(ValueError):
        batch_forecast._coordinates({"lat": 44.98859, "lon": -93.25557, **extra})


def test_optional_display_timezone_is_presentation_only():
    location = {"lat": 44.98859, "lon": -93.25557, "display_timezone": "America/Chicago"}
    assert batch_forecast._coordinates(location) == (44.98859, -93.25557)
    assert batch_forecast.location_display_timezone(location) == "America/Chicago"
    assert batch_forecast.location_display_timezone({"lat": 1.0, "lon": 2.0}) is None
    with pytest.raises(ValueError):
        batch_forecast._coordinates({**location, "display_timezone": 5})
    with pytest.raises(ValueError):
        batch_forecast.location_display_timezone({**location, "display_timezone": "Mars/Olympus"})


def test_verification_failure_does_not_stop_shared_issuance(tmp_path, monkeypatch):
    locations = [
        {"lat": 44.98859, "lon": -93.25557, "name": "Minneapolis"},
        {"lat": 95.0, "lon": -93.0},
        {"lat": 45.9, "lon": -93.0},
    ]
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": locations}))
    calls = []

    def verify(lat, lon, *, now):
        calls.append(("verify", lat))
        if lat == locations[0]["lat"]:
            raise RuntimeError("private connection details")
        return {"status": "nothing_to_verify", "downloaded_bytes": 0}

    def discover(directory):
        calls.append(("discover", directory.name))
        # Verify issuance will consume the snapshot, not this mutable input.
        config.write_text('{"locations": []}')
        return SELECTION

    def issue(snapshot, selection, directory, **kwargs):
        calls.append(("issue", directory.name))
        # Only coordinates that still need this decision window reach preparation.
        assert json.loads(snapshot.read_text()) == {"locations": [locations[0], locations[2]]}
        assert json.loads((directory.parent / "locations.json").read_text()) == {
            "locations": locations
        }
        assert selection == directory.parent / "selection/selection.json"
        assert kwargs["issuer"] is issuer
        assert callable(kwargs["forecast_report_builder"])
        return {
            "batch_run_id": "immutable-batch-id",
            "preparation": {"downloaded_bytes": 123},
            "results": [
                {
                    "index": 0,
                    "status": "ok",
                    "forecast": {"hourly_report": {}},
                    "issued": {"issued_forecast_id": "first"},
                },
                {
                    "index": 1,
                    "status": "ok",
                    "forecast": {"hourly_report": {}},
                    "issued": {"issued_forecast_id": "last"},
                },
            ],
        }

    issuer = make_issuer()
    monkeypatch.setattr(forward_run, "verify_previous", verify)
    monkeypatch.setattr(forward_run, "_discover", discover)
    monkeypatch.setattr(forward_run, "run_selected_batch", issue)
    monkeypatch.setattr(forward_run, "render_hourly_report", lambda report: "36 hourly rows")
    output = tmp_path / "run"
    result = forward_run.run_forward(config, output, issuer=issuer)
    assert calls == [
        ("verify", 44.98859),
        ("verify", 45.9),
        ("discover", "selection"),
        ("issue", "prepared"),
    ]
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    assert [row["index"] for row in result["results"]] == [0, 1, 2]
    assert result["results"][0]["verification"]["status"] == "error"
    assert result["results"][2]["verification"]["status"] == "nothing_to_verify"
    assert result["results"][2]["issued"] == {"issued_forecast_id": "last"}
    assert result["results"][1]["error"]["code"] == "invalid_location"
    assert result["summary"] == {
        "issued": 2,
        "skipped": 0,
        "failed": 1,
        "verification": result["summary"]["verification"],
    }
    assert result["issuance_indexes"] == [0, 2]
    assert result["overlap_protection"] == forward_run.OVERLAP_PROTECTION
    assert "private connection" not in json.dumps(result)
    assert json.loads((output / "result.json").read_text()) == result
    assert "first" in (output / "hourly-report.md").read_text()
    assert issuer.find_versions.call_count == 2
    for call in issuer.find_versions.call_args_list:
        assert call.kwargs["target_reference_time"] == datetime.fromisoformat(TARGET)


def test_existing_version_for_the_decision_window_is_skipped_unless_reissue(tmp_path, monkeypatch):
    locations = [
        {"lat": 44.98859, "lon": -93.25557, "name": "Minneapolis"},
        {"lat": 44.9537, "lon": -93.09},
    ]
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": locations}))
    existing = SimpleNamespace(issued_forecast_id=UUID(int=7))
    issuer = Mock()
    issuer.find_versions.side_effect = lambda *, latitude, longitude, target_reference_time: (
        (existing,) if latitude == 44.98859 else ()
    )
    monkeypatch.setattr(
        forward_run, "verify_previous", lambda *a, **kw: {"status": "nothing_to_verify"}
    )
    monkeypatch.setattr(forward_run, "_discover", lambda directory: SELECTION)
    issued_snapshots = []

    def issue(snapshot, selection, directory, **kwargs):
        issued = json.loads(snapshot.read_text())["locations"]
        issued_snapshots.append(issued)
        return {
            "batch_run_id": "batch",
            "results": [
                {
                    "index": position,
                    "status": "ok",
                    "forecast": {"hourly_report": {}},
                    "issued": {"issued_forecast_id": f"new-{location['lat']}"},
                }
                for position, location in enumerate(issued)
            ],
        }

    monkeypatch.setattr(forward_run, "run_selected_batch", issue)
    monkeypatch.setattr(forward_run, "render_hourly_report", lambda report: "rows")

    guarded = forward_run.run_forward(config, tmp_path / "guarded", issuer=issuer)
    assert [row["status"] for row in guarded["results"]] == ["skipped_already_issued", "ok"]
    assert guarded["results"][0]["skipped"]["existing_issued_forecast_ids"] == [str(UUID(int=7))]
    assert guarded["results"][0]["skipped"]["target_reference_time"] == TARGET
    assert "issued" not in guarded["results"][0]
    assert guarded["results"][1]["issued"] == {"issued_forecast_id": "new-44.9537"}
    assert guarded["results"][1]["index"] == 1
    assert guarded["summary"]["issued"] == 1
    assert guarded["summary"]["skipped"] == 1
    assert guarded["summary"]["failed"] == 0
    assert issued_snapshots == [[locations[1]]]
    report = (tmp_path / "guarded" / "hourly-report.md").read_text()
    assert "Issuance skipped" in report and str(UUID(int=7)) in report

    # An explicit reissue adds a version instead of skipping.
    reissued = forward_run.run_forward(config, tmp_path / "reissued", issuer=issuer, reissue=True)
    assert [row["status"] for row in reissued["results"]] == ["ok", "ok"]
    assert reissued["reissue"] is True
    assert issued_snapshots[-1] == locations

    # Nothing to issue: preparation and issuance are never started.
    issuer.find_versions.side_effect = lambda **kw: (existing,)
    forbidden = Mock(side_effect=AssertionError("No preparation when every coordinate is covered"))
    monkeypatch.setattr(forward_run, "run_selected_batch", forbidden)
    covered = forward_run.run_forward(config, tmp_path / "covered", issuer=issuer)
    assert [row["status"] for row in covered["results"]] == ["skipped_already_issued"] * 2
    assert covered["summary"]["issued"] == 0 and covered["summary"]["skipped"] == 2
    assert covered["issuance_indexes"] == []
    forbidden.assert_not_called()


def test_decision_window_lookup_failure_never_issues_competing_versions(tmp_path, monkeypatch):
    config = tmp_path / "locations.json"
    config.write_text('{"locations": [{"lat":45.8,"lon":-93.1}]}')
    issuer = Mock()
    issuer.find_versions.side_effect = RuntimeError("storage unavailable")
    monkeypatch.setattr(
        forward_run, "verify_previous", lambda *a, **kw: {"status": "nothing_to_verify"}
    )
    monkeypatch.setattr(forward_run, "_discover", lambda directory: SELECTION)
    issue = Mock(side_effect=AssertionError("Unknown history must never issue"))
    monkeypatch.setattr(forward_run, "run_selected_batch", issue)
    result = forward_run.run_forward(config, tmp_path / "run", issuer=issuer)
    assert result["results"][0]["error"]["code"] == "current_issuance_failed"
    assert result["summary"]["failed"] == 1
    issue.assert_not_called()


def test_per_location_display_timezone_reaches_the_report_builder(tmp_path, monkeypatch):
    locations = [
        {"lat": 44.98859, "lon": -93.25557, "display_timezone": "America/Chicago"},
        {"lat": 39.7392, "lon": -104.9903, "display_timezone": "Mars/Olympus"},
        {"lat": 45.9, "lon": -93.0},
    ]
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": locations}))
    monkeypatch.setattr(
        forward_run, "verify_previous", lambda *a, **kw: {"status": "nothing_to_verify"}
    )
    monkeypatch.setattr(forward_run, "_discover", lambda directory: SELECTION)
    zones = {}

    def build(forecast, *, display_timezone):
        zones[(forecast["latitude"], forecast["longitude"])] = display_timezone
        return {"display_timezone": display_timezone}

    monkeypatch.setattr(forward_run, "build_hourly_report", build)

    def issue(snapshot, selection, directory, **kwargs):
        builder = kwargs["forecast_report_builder"]
        results = []
        for position, location in enumerate(json.loads(snapshot.read_text())["locations"]):
            forecast = {"latitude": location["lat"], "longitude": location["lon"], "hours": []}
            forecast["hourly_report"] = builder(forecast)
            results.append(
                {
                    "index": position,
                    "status": "ok",
                    "forecast": forecast,
                    "issued": {"issued_forecast_id": f"id-{position}"},
                }
            )
        return {"batch_run_id": "batch", "results": results}

    monkeypatch.setattr(forward_run, "run_selected_batch", issue)
    monkeypatch.setattr(forward_run, "render_hourly_report", lambda report: "rows")
    result = forward_run.run_forward(
        config, tmp_path / "run", issuer=make_issuer(), display_timezone="America/Denver"
    )
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    assert result["results"][1]["error"]["code"] == "invalid_location"
    assert result["results"][0]["display_timezone"] == "America/Chicago"
    assert result["results"][2]["display_timezone"] == "America/Denver"
    assert zones == {(44.98859, -93.25557): "America/Chicago", (45.9, -93.0): "America/Denver"}
    assert result["results"][0]["forecast"]["hourly_report"] == {
        "display_timezone": "America/Chicago"
    }


def test_overlapping_run_stops_before_any_directory_or_provider_work(tmp_path, monkeypatch):
    config = tmp_path / "locations.json"
    config.write_text('{"locations": [{"lat":45.8,"lon":-93.1}]}')
    forbidden = Mock(side_effect=AssertionError("An overlapping run must do no work"))
    for name in ("_discover", "verify_previous", "run_selected_batch"):
        monkeypatch.setattr(forward_run, name, forbidden)

    @contextmanager
    def busy():
        raise AdvisoryLockBusy("held elsewhere")
        yield

    output = tmp_path / "run"
    with pytest.raises(AdvisoryLockBusy):
        forward_run.run_forward(config, output, issuer=Mock(), run_lock=busy)
    assert not output.exists()
    forbidden.assert_not_called()


def test_cli_reports_overlap_with_a_distinct_exit_code(tmp_path, monkeypatch, capsys):
    config = tmp_path / "locations.json"
    config.write_text('{"locations": [{"lat":45.8,"lon":-93.1}]}')

    @contextmanager
    def busy():
        raise AdvisoryLockBusy("held elsewhere")
        yield

    monkeypatch.setattr(forward_run, "_acquire_run_lock", busy)
    code = forward_run.main(["--config", str(config), "--output-dir", str(tmp_path / "run")])
    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.err)["error"]["code"] == "forward_run_overlap"
    assert captured.out == ""
    assert not (tmp_path / "run").exists()


def test_lock_is_held_across_verification_and_issuance(tmp_path, monkeypatch):
    config = tmp_path / "locations.json"
    config.write_text('{"locations": [{"lat":45.8,"lon":-93.1}]}')
    events = []

    @contextmanager
    def lock():
        events.append("lock")
        yield
        events.append("unlock")

    monkeypatch.setattr(
        forward_run, "verify_previous", lambda *a, **kw: events.append("verify") or {"status": "x"}
    )
    monkeypatch.setattr(forward_run, "_discover", lambda d: events.append("discover") or SELECTION)
    monkeypatch.setattr(
        forward_run,
        "run_selected_batch",
        lambda *a, **kw: (
            events.append("issue")
            or {
                "results": [
                    {
                        "index": 0,
                        "status": "ok",
                        "forecast": {"hourly_report": {}},
                        "issued": {"issued_forecast_id": "x"},
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(forward_run, "render_hourly_report", lambda report: "rows")
    forward_run.run_forward(config, tmp_path / "run", issuer=make_issuer(), run_lock=lock)
    assert events == ["lock", "verify", "discover", "issue", "unlock"]


@pytest.mark.parametrize("failure", ["unavailable", "raises"])
def test_current_set_failure_keeps_completed_verification(tmp_path, monkeypatch, failure):
    config = tmp_path / "locations.json"
    config.write_text('{"locations": [{"lat":45.8,"lon":-93.1}]}')
    verification = {"status": "completed", "summary": {"verified": 2, "already_existing": 1}}
    monkeypatch.setattr(forward_run, "verify_previous", lambda *a, **kw: deepcopy(verification))
    discovery = Mock(return_value={"status": "unavailable", "reason": "RAP incomplete"})
    if failure == "raises":
        discovery.side_effect = ValueError("Provider object changed")
    monkeypatch.setattr(forward_run, "_discover", discovery)
    issue = Mock(side_effect=AssertionError("Unavailable model set must never issue"))
    monkeypatch.setattr(forward_run, "run_selected_batch", issue)
    result = forward_run.run_forward(config, tmp_path / "run", issuer=make_issuer())
    assert result["results"][0]["verification"] == verification
    assert result["results"][0]["error"]["code"] == "current_issuance_failed"
    issue.assert_not_called()


@pytest.mark.parametrize(
    "locations", [[], [{"lat": 95.0, "lon": -93.0}], [{"lat": False, "lon": 0}]]
)
def test_no_valid_locations_performs_no_provider_or_observation_calls(
    tmp_path, monkeypatch, locations
):
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": locations}))
    forbidden = Mock(side_effect=AssertionError("No work should require data"))
    for name in ("_discover", "verify_previous", "run_selected_batch"):
        monkeypatch.setattr(forward_run, name, forbidden)
    result = forward_run.run_forward(config, tmp_path / "run", issuer=Mock())
    assert result["summary"]["issued"] == 0
    forbidden.assert_not_called()


def test_run_never_overwrites_existing_directory(tmp_path, monkeypatch):
    config = tmp_path / "locations.json"
    config.write_text('{"locations": []}')
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "result.json"
    marker.write_text("previous run")
    forbidden = Mock(side_effect=AssertionError("No repeated data work"))
    monkeypatch.setattr(forward_run, "_discover", forbidden)
    with pytest.raises(FileExistsError):
        forward_run.run_forward(config, output, issuer=Mock())
    assert marker.read_text() == "previous run"
    forbidden.assert_not_called()
