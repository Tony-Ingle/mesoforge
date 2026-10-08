"""Retention protects scientific lineage before preferring any cycle-count window."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application import guidance_retention as retention
from mesoforge.application.baseline_snapshot import BASELINE_SCHEMA, write_artifact
from mesoforge.application.prepared_snapshot import SNAPSHOT_SCHEMA


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "guidance/snapshots").mkdir(parents=True)
    (root / "baseline/baselines").mkdir(parents=True)

    return root


def _generation(root: Path, hour: int, *, failed: bool = False) -> Path:
    directory = root / "guidance/snapshots" / f"20261007T{hour:02d}0000Z-12345678"
    directory.mkdir()
    (directory / "source.grib2").write_bytes(b"raw meteorology")
    if failed:
        _write(directory / "failure.json", {"status": "failed"})
        _write(directory / "result.json", {"status": "failed"})
        return directory
    cycle = f"2026-10-07T{hour:02d}:00:00Z"
    contributors = {model: {"cycle": cycle} for model in ("HRRR", "GFS", "RAP", "IFS")}
    contributors["NBM"] = {"products": {"cloud": {"cycle": cycle}}}
    _write(
        directory / "snapshot.json",
        {
            "schema_version": SNAPSHOT_SCHEMA,
            "snapshot_id": directory.name,
            "contributors": contributors,
        },
    )
    _write(directory / "refresh-log.json", [{"step": "prepare", "status": "ok"}])
    return directory


def _baseline(root: Path, prepared: Path, identity: str, *refs: Path) -> None:
    directory = root / "baseline/baselines" / identity
    directory.mkdir()
    metadata = write_artifact(
        directory,
        "metadata.json.gz",
        {"source_documents": [{"path": str(path)} for path in refs], "metadata": []},
    )
    _write(
        directory / "baseline.json",
        {
            "schema_version": BASELINE_SCHEMA,
            "baseline_snapshot_id": identity,
            "prepared_snapshot": {"snapshot_id": prepared.name, "directory": str(prepared)},
            "metadata_file": metadata,
        },
    )


def _rows(plan: dict[str, Any]) -> dict[str, Any]:
    return {row["snapshot_id"]: row for row in plan["generations"]}


def test_cycle_windows_current_previous_and_all_retained_baselines(runtime: Path) -> None:
    generations = [_generation(runtime, hour) for hour in range(6)]
    _write(
        runtime / "guidance/latest_complete.json",
        {"snapshot_id": generations[-1].name, "previous_snapshot_id": generations[-2].name},
    )
    for index in range(3):
        _baseline(runtime, generations[index], f"baseline-{index}")
    _write(
        runtime / "baseline/latest_baseline.json",
        {"baseline_snapshot_id": "baseline-2", "previous_baseline_snapshot_id": "baseline-1"},
    )
    report = retention.plan_retention(runtime)
    rows = _rows(report)
    assert report["cycle_counts"] == {"HRRR": 4, "RAP": 4, "GFS": 3, "IFS": 3, "NBM": 4}
    assert sum("recent_HRRR_cycle_window" in row["reasons"] for row in rows.values()) == 4
    assert sum("recent_GFS_cycle_window" in row["reasons"] for row in rows.values()) == 3
    assert "latest_prepared" in rows[generations[-1].name]["reasons"]
    assert "previous_prepared_recovery" in rows[generations[-2].name]["reasons"]
    assert "latest_baseline_dependency" in rows[generations[2].name]["reasons"]
    assert "previous_baseline_recovery_dependency" in rows[generations[1].name]["reasons"]
    assert "retained_baseline_dependency" in rows[generations[0].name]["reasons"]
    assert report["removable_bytes"] == 0
    assert report["bounded_complete_history"] is False
    baselines = {row["baseline_snapshot_id"]: row for row in report["baselines"]}
    assert "latest_baseline" in baselines["baseline-2"]["reasons"]
    assert "previous_baseline_recovery" in baselines["baseline-1"]["reasons"]
    assert all(row["action"] == "retain" for row in baselines.values())
    assert report["retained_baseline_bytes"] == sum(row["bytes"] for row in baselines.values())
    changed = _rows(retention.plan_retention(runtime, counts=retention.CycleCounts(HRRR=1)))
    assert sum("recent_HRRR_cycle_window" in row["reasons"] for row in changed.values()) == 1
    assert all(row["action"] == "retain" for row in changed.values())


def test_obsolete_complete_generation_stays_until_permanent_reference_closure(
    runtime: Path,
) -> None:
    old = _generation(runtime, 1)
    _generation(runtime, 2)
    counts = retention.CycleCounts(1, 1, 1, 1, 1)
    reasons = _rows(retention.plan_retention(runtime, counts=counts))[old.name]["reasons"]
    assert reasons == ["permanent_artifact_reference_closure_unproven"]


def test_failed_generation_dry_run_and_apply_refusal_never_access_database_or_delete(
    runtime: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mesoforge.storage.postgres import database

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Retention must not access database credentials or delete data")

    monkeypatch.setattr(database, "resolve_database_dsn", forbidden)
    monkeypatch.setattr(Path, "unlink", forbidden)
    failed = _generation(runtime, 1, failed=True)
    _generation(runtime, 2)
    assert retention.main(["--runtime-root", str(runtime)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["operation"] == "dry_run"
    assert _rows(report)[failed.name]["action"] == "retain"
    assert report["deletion_supported"] is False
    assert report["removable_bytes"] == 0
    before = {str(path): path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    for _ in range(2):
        assert retention.main(["--runtime-root", str(runtime), "--apply"]) == 2
        result = capsys.readouterr()
        assert json.loads(result.out) == report
        assert "Nothing was deleted" in result.err
    assert {str(path): path.read_bytes() for path in runtime.rglob("*") if path.is_file()} == before


def test_failed_generation_can_have_unindexed_external_replay_dependency(
    runtime: Path, tmp_path: Path
) -> None:
    failed = _generation(runtime, 1, failed=True)
    external = tmp_path / "historical-development-case/preparation.json"
    _write(external, {"source_directory": str(failed), "raw_file": str(failed / "source.grib2")})
    report = retention.plan_retention(runtime)
    row = _rows(report)[failed.name]
    assert row["state"] == "terminal_failed"
    # An empty local reference scan is not proof of global absence. We retain
    # without traversing arbitrary operator/research/runner directories.
    assert row["reasons"] == ["permanent_artifact_reference_closure_unproven"]
    assert row["action"] == "retain"
    assert report["removable_bytes"] == 0
    assert external.is_file() and (failed / "source.grib2").is_file()


def test_pin_protects_whole_case_and_unpin_is_explicit(runtime: Path) -> None:
    failed = _generation(runtime, 1, failed=True)
    retention.pin_case(runtime, failed.name, reason="Keep severe-weather acquisition failure")
    assert "operator_case_pin" in _rows(retention.plan_retention(runtime))[failed.name]["reasons"]
    retention.pin_case(runtime, failed.name, reason="", remove=True)
    row = _rows(retention.plan_retention(runtime))[failed.name]
    assert "operator_case_pin" not in row["reasons"]
    assert row["action"] == "retain"


def test_source_reference_protects_failed_generation(runtime: Path) -> None:
    failed = _generation(runtime, 1, failed=True)
    complete = _generation(runtime, 2)
    _baseline(runtime, complete, "current", failed / "source.grib2")
    assert (
        "retained_local_metadata_dependency"
        in _rows(retention.plan_retention(runtime))[failed.name]["reasons"]
    )
    assert retention.plan_retention(runtime)["removable_bytes"] == 0


@pytest.mark.parametrize("state", ["in_flight", "malformed", "unknown_baseline"])
def test_unproven_reference_inventory_fails_closed(runtime: Path, state: str) -> None:
    failed = _generation(runtime, 1, failed=True)
    if state == "in_flight":
        incomplete = _generation(runtime, 2, failed=True)
        (incomplete / "failure.json").unlink()
        assert _rows(retention.plan_retention(runtime))[incomplete.name]["action"] == "retain"
        _write(incomplete / "preparation.json", {"directory": str(failed)})
    elif state == "malformed":
        (failed / "broken.json").write_text("{", encoding="utf-8")
    else:
        _write(runtime / "baseline/baselines/unfinished/partial.json", {})
    report = retention.plan_retention(runtime)
    assert report["removable_bytes"] == 0
    assert failed.is_dir()


def test_guidance_lock_serializes_pin_changes(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = _generation(runtime, 1, failed=True)

    @contextmanager
    def busy(_root: Path):
        yield False

    monkeypatch.setattr(retention, "single_writer", busy)
    with pytest.raises(RuntimeError, match="Guidance worker"):
        retention.pin_case(runtime, failed.name, reason="Protect case")
    assert failed.exists()


def test_cli_does_not_echo_untrusted_infrastructure_exception(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("private://credential-and-provider-error")

    monkeypatch.setattr(retention, "plan_retention", broken)
    assert retention.main(["--runtime-root", str(runtime), "--apply"]) == 2
    result = capsys.readouterr()
    assert "OSError" in result.err
    assert "credential" not in result.err
    assert not result.out


def test_links_path_escape_and_unmanaged_data_never_deleted(runtime: Path, tmp_path: Path) -> None:
    failed = _generation(runtime, 1, failed=True)
    runner = tmp_path / "actions-runner/_work"
    runner.mkdir(parents=True)
    marker = runner / "keep.txt"
    marker.write_text("protected runner", encoding="utf-8")
    with pytest.raises(ValueError, match="managed snapshot"):
        retention.pin_case(runtime, "../../actions-runner", reason="escape")
    try:
        (failed / "linked").symlink_to(runner, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    with pytest.raises(ValueError, match="links"):
        retention.plan_retention(runtime)
    assert marker.read_text(encoding="utf-8") == "protected runner"


def test_unknown_schema_and_invalid_cycle_configuration_fail_closed(runtime: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        retention.CycleCounts(GFS=0)
    failed = _generation(runtime, 1, failed=True)
    _write(failed / "snapshot.json", {"schema_version": "future-schema"})
    with pytest.raises(ValueError, match="Unsupported prepared"):
        retention.plan_retention(runtime)
    assert failed.exists()


def test_inflight_status_and_pinned_baseline_are_explicit_without_runner_traversal(
    runtime: Path, tmp_path: Path
) -> None:
    prepared = _generation(runtime, 1)
    _baseline(runtime, prepared, "protected")
    retention.pin_case(runtime, prepared.name, reason="Research case")
    _write(runtime / "status/guidance-worker.json", {"in_flight": {"phase": "build"}})
    _write(runtime / "status/forecast-worker.json", {"state": "busy"})
    runner = tmp_path / "actions-runner/_work"
    _write(runner / "keep.json", {"important": "other application"})
    before = (runner / "keep.json").read_bytes()
    report = retention.plan_retention(runtime)
    assert report["in_flight_workers"] == ["guidance-worker.json", "forecast-worker.json"]
    for row in [*report["generations"], *report["baselines"]]:
        assert "worker_in_flight_reference_closure_unproven" in row["reasons"]
        assert row["action"] == "retain"
    assert "operator_case_pin_dependency" in report["baselines"][0]["reasons"]
    assert report["removable_bytes"] == 0
    assert "actions-runner" not in json.dumps(report)
    assert (runner / "keep.json").read_bytes() == before
