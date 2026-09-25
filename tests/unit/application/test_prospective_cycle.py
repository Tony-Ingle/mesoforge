"""Operator composition, clock and error behavior; scientific kernels are tested elsewhere."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mesoforge.application import prospective_cycle as cycle
from mesoforge.application.batch_forecast import _coordinates, load_locations

START = datetime(2031, 11, 2, 6, 59, tzinfo=UTC)
AFTER = START + timedelta(minutes=3)
LOCATIONS = [
    {"id": "a", "lat": 44.98, "lon": -93.25, "display_timezone": "America/Chicago"},
    {"id": "b", "lat": 45.8, "lon": -93.1},
]


@pytest.fixture
def operator(tmp_path, monkeypatch):
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": LOCATIONS}))
    events = []
    prepared = {"snapshot_id": "prepared-a"}
    pointer = {"baseline_snapshot_id": "baseline-a"}
    issuer = Mock()
    issuer.find_versions.return_value = []

    def refresh(path, root):
        events.append(("refresh", load_locations(path)))
        return {"status": "published", "latest_complete": prepared}

    def build(guidance, root, locations, *, prepared_pointer):
        assert prepared_pointer is prepared
        events.append(("build", locations))
        return {"status": "published", "pointer": pointer}

    def forecast(root, locations, **kwargs):
        events.append(("forecast", kwargs))
        assert kwargs["baseline_pointer"] is pointer
        assert kwargs["issue"] and kwargs["issuer"] is issuer
        reference = kwargs["reference_time"] or cycle.derive_reference_time(kwargs["request_time"])
        return {
            "status": "ok",
            "request_time": kwargs["request_time"].isoformat(),
            "reference_time": reference.isoformat(),
            "baseline": {
                "baseline_snapshot_id": "baseline-a",
                "prepared_snapshot_id": "prepared-a",
            },
            "results": [
                {
                    "index": i,
                    "location": row,
                    "status": "ok",
                    "baseline_extraction_seconds": 1,
                    "issued": {"issued_forecast_id": f"issued-{i}"},
                }
                for i, row in enumerate(locations)
            ],
            "summary": {"ok": len(locations), "issued": len(locations), "failed": 0, "skipped": 0},
        }

    monkeypatch.setattr(cycle, "refresh_guidance", refresh)
    monkeypatch.setattr(cycle, "build_baseline", build)
    monkeypatch.setattr(cycle, "forecast_from_baseline", forecast)
    return SimpleNamespace(
        config=config, root=tmp_path / "runtime", issuer=issuer, events=events, pointer=pointer
    )


def test_now_clock_resampled_after_background_and_reference_can_cross_hour(operator):
    times = iter([START, AFTER])
    result = cycle.run_prospective_cycle(
        operator.config, operator.root, clock=lambda: next(times), issuer=operator.issuer
    )
    assert [e[0] for e in operator.events] == ["refresh", "build", "forecast"]
    assert result["started_at"] == START.isoformat()
    assert result["request_time"] == AFTER.isoformat()
    assert result["reference_time"] == "2031-11-02T07:00:00+00:00"
    assert operator.events[-1][1]["reference_time"] is None
    assert result["status"] == "completed"
    assert len(result["results"]) == 2
    saved = json.loads((Path(result["directory"]) / "result.json").read_text())
    assert saved == result


def test_aware_local_clock_is_normalized_to_utc_without_fixed_offset(operator):
    from zoneinfo import ZoneInfo

    # Both sides of the repeated Chicago fall-back hour map to distinct UTC decisions.
    for fold, expected in [(0, "06"), (1, "07")]:
        now = datetime(2031, 11, 2, 1, 30, tzinfo=ZoneInfo("America/Chicago"), fold=fold)
        result = cycle.run_prospective_cycle(
            operator.config, operator.root, clock=lambda now=now: now, issuer=operator.issuer
        )
        assert result["reference_time"] == f"2031-11-02T{expected}:00:00+00:00"


def test_naive_clock_rejected_before_background_work(operator):
    with pytest.raises(ValueError, match="timezone-aware"):
        cycle.run_prospective_cycle(
            operator.config,
            operator.root,
            clock=lambda: START.replace(tzinfo=None),
            issuer=operator.issuer,
        )
    assert operator.events == []


@pytest.mark.parametrize(
    "reference", [START, START.replace(tzinfo=None), AFTER + timedelta(days=1)]
)
def test_invalid_explicit_replay_reference_rejected(operator, reference):
    with pytest.raises(ValueError, match="Replay reference"):
        cycle.run_prospective_cycle(
            operator.config,
            operator.root,
            clock=lambda: START,
            replay_reference_time=reference,
            issuer=operator.issuer,
        )
    assert operator.events == []


def test_explicit_replay_is_marked_and_does_not_replace_actual_analysis_clock(operator):
    reference = START.replace(minute=0)
    result = cycle.run_prospective_cycle(
        operator.config,
        operator.root,
        clock=lambda: AFTER,
        replay_reference_time=reference,
        issuer=operator.issuer,
    )
    assert result["reference_time_source"] == "explicit_replay"
    assert result["reference_time"] == reference.isoformat()
    assert result["request_time"] == AFTER.isoformat()


def test_repeat_uses_existing_baseline_but_keeps_locked_issuance_boundary(operator, monkeypatch):
    operator.issuer.find_versions.return_value = [object()]
    pinned = SimpleNamespace(
        pointer=operator.pointer,
        manifest={
            "domains": [
                {
                    "latitude": row["lat"],
                    "longitude": row["lon"],
                    "reference_time": "2031-11-02T06:00:00Z",
                }
                for row in LOCATIONS
            ]
        },
        reference_view=Mock(),
    )
    monkeypatch.setattr(cycle, "load_baseline", lambda root: pinned)
    # Identity changes to a copied pointer are intentional at the repeat boundary.
    original = cycle.forecast_from_baseline

    def forecast(root, locations, **kwargs):
        assert kwargs["baseline_pointer"] == operator.pointer
        kwargs["baseline_pointer"] = operator.pointer
        return original(root, locations, **kwargs)

    monkeypatch.setattr(cycle, "forecast_from_baseline", forecast)
    result = cycle.run_prospective_cycle(
        operator.config, operator.root, clock=lambda: START, issuer=operator.issuer
    )
    assert [e[0] for e in operator.events] == ["forecast"]
    assert result["background"]["status"] == "reused_for_already_issued_window"


@pytest.mark.parametrize("phase", ["refresh", "build"])
def test_background_failure_stops_issuance_and_preserves_independent_pointers(
    operator, monkeypatch, phase
):
    (operator.root / "baseline").mkdir(parents=True)
    old = operator.root / "baseline" / "latest_baseline.json"
    old.write_text("known-good-a")

    def fail(*args, **kwargs):
        raise OSError("provider or build unavailable")

    monkeypatch.setattr(cycle, "refresh_guidance" if phase == "refresh" else "build_baseline", fail)
    result = cycle.run_prospective_cycle(
        operator.config, operator.root, clock=lambda: START, issuer=operator.issuer
    )
    assert result["status"] == "failed"
    assert all(row["issuance_outcome"] == "not_issued" for row in result["results"])
    assert not any(e[0] == "forecast" for e in operator.events)
    assert old.read_text() == "known-good-a"
    if phase == "build":
        assert result["background"]["refresh"]["status"] == "published"


def test_bad_configured_row_is_filtered_only_for_background_collection(operator, monkeypatch):
    locations = [LOCATIONS[0], {"id": "bad", "lat": 999, "lon": 0}, LOCATIONS[1]]
    operator.config.write_text(json.dumps({"locations": locations}))
    result = cycle.run_prospective_cycle(
        operator.config, operator.root, clock=lambda: START, issuer=operator.issuer
    )
    assert operator.events[:2] == [("refresh", LOCATIONS), ("build", LOCATIONS)]
    assert [row["location"] for row in result["results"]] == locations


def test_default_registry_is_exact_owner_configuration():
    locations = load_locations(cycle.DEFAULT_CONFIG)
    assert [(r["id"], r["name"], *_coordinates(r), r["display_timezone"]) for r in locations] == [
        ("minneapolis", "Minneapolis", 44.98861, -93.25553, "America/Chicago"),
        ("surley", "Surley", 44.97304, -93.20901, "America/Chicago"),
        ("grasston", "Grasston", 45.80268, -93.07952, "America/Chicago"),
    ]


def test_scheduler_cli_requires_no_date_or_interactive_input(monkeypatch, tmp_path, capsys):
    result = {
        "started_at": START.isoformat(),
        "reference_time": START.replace(minute=0).isoformat(),
        "directory": str(tmp_path),
        "results": [],
        "status": "completed",
    }
    run = Mock(return_value=result)
    monkeypatch.setattr(cycle, "run_prospective_cycle", run)
    assert cycle.main(["--root", str(tmp_path)]) == 0
    assert run.call_args.args == (cycle.DEFAULT_CONFIG, tmp_path)
    assert run.call_args.kwargs == {"replay_reference_time": None}
    assert "Cycle result: completed" in capsys.readouterr().out
