"""Only forward orchestration is new; provider and scientific paths have their own tests."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from mesoforge.application import batch_forecast, forward_run


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
        return {"status": "selected", "selected_cycles": {"HRRR": "fixed-evidence"}}

    def issue(snapshot, selection, directory, **kwargs):
        calls.append(("issue", directory.name))
        assert json.loads(snapshot.read_text()) == {"locations": locations}
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
                {"index": 1, "status": "error", "error": {"code": "unsupported_coordinate"}},
                {
                    "index": 2,
                    "status": "ok",
                    "forecast": {"hourly_report": {}},
                    "issued": {"issued_forecast_id": "last"},
                },
            ],
        }

    issuer = Mock()
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
    assert result["results"][0]["verification"]["status"] == "error"
    assert result["results"][2]["verification"]["status"] == "nothing_to_verify"
    assert result["summary"]["issued"] == 2 and result["summary"]["failed"] == 1
    assert "private connection" not in json.dumps(result)
    assert json.loads((output / "result.json").read_text()) == result
    assert "first" in (output / "hourly-report.md").read_text()


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
    result = forward_run.run_forward(config, tmp_path / "run", issuer=Mock())
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
