"""Native cache expiration is separate from immutable forecast/evidence retention."""

from __future__ import annotations

import hashlib
import json
import tarfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application import guidance_retention as retention
from mesoforge.application.baseline_snapshot import BASELINE_SCHEMA, write_artifact
from mesoforge.application.prepared_snapshot import SNAPSHOT_SCHEMA

COUNTS = retention.CycleCounts(1, 1, 1, 1, 1, 1, 1)
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("MESOFORGE_WORKER_LOCK_ROOT", raising=False)
    root = tmp_path / "runtime"
    (root / "guidance/snapshots").mkdir(parents=True)
    (root / "baseline/baselines").mkdir(parents=True)
    return root


def _generation(root: Path, hour: int, *, failed: bool = False) -> Path:
    directory = root / "guidance/snapshots" / f"20261007T{hour:02d}0000Z-12345678"
    directory.mkdir()
    (directory / "source.grib2").write_bytes(b"raw meteorology")
    (directory / "prepared.nc").write_bytes(b"prepared meteorology")
    if failed:
        _write(directory / "failure.json", {"status": "failed"})
        _write(directory / "result.json", {"status": "failed"})
        return directory
    cycle = f"2026-10-07T{hour:02d}:00:00Z"
    contributors = {
        model: {"cycle": cycle, "status": "complete", "valid_times": [cycle]}
        for model in ("HRRR", "GFS", "RAP", "IFS")
    }
    contributors["NBM"] = {"products": {"cloud": {"cycle": cycle, "status": "complete"}}}
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
        {
            "source_documents": [
                {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in refs
            ],
            "metadata": [],
        },
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


def _receipt(root: Path, plan: dict[str, Any]) -> Path:
    path = root.parent / "verified-backup-receipt.json"
    _write(
        path,
        {
            "schema_version": retention.BACKUP_RECEIPT_SCHEMA,
            "status": "verified",
            "runtime_root": str(root),
            "backup_id": "test-recovery-copy",
            "manifest_sha256": "a" * 64,
            "retention_plan_sha256": plan["plan_sha256"],
            "verified_at": NOW.isoformat(),
        },
    )
    return path


def test_cycle_windows_and_current_recovery_protection(runtime: Path) -> None:
    generations = [_generation(runtime, hour) for hour in range(7)]
    _write(
        runtime / "guidance/latest_complete.json",
        {
            "snapshot_id": generations[-1].name,
            "previous_snapshot_id": generations[-2].name,
        },
    )
    for index in range(3):
        _baseline(
            runtime, generations[index], f"baseline-{index}", generations[index] / "snapshot.json"
        )
    _write(
        runtime / "baseline/latest_baseline.json",
        {
            "baseline_snapshot_id": "baseline-2",
            "previous_baseline_snapshot_id": "baseline-1",
        },
    )
    report = retention.plan_retention(runtime)
    rows = _rows(report)
    assert report["cycle_counts"] == {
        "HRRR": 4,
        "RAP": 4,
        "GFS": 3,
        "IFS": 3,
        "NBM": 4,
        "GEFS": 3,
        "ECMWF_ENS": 3,
    }
    assert sum("recent_HRRR_cycle_window" in row["reasons"] for row in rows.values()) == 4
    assert sum("recent_GFS_cycle_window" in row["reasons"] for row in rows.values()) == 3
    assert "CURRENT" in rows[generations[-1].name]["labels"]
    assert "RECOVERY" in rows[generations[-2].name]["labels"]
    assert "latest_baseline_dependency" in rows[generations[2].name]["reasons"]
    assert "previous_baseline_recovery_dependency" in rows[generations[1].name]["reasons"]
    # Old baseline references require JSON, not perpetually retained native arrays.
    assert rows[generations[0].name]["action"] == "expire_payloads"
    assert report["removable_bytes"] == 35
    assert report["bounded_complete_history"] is False
    assert all(row["action"] == "retain" for row in report["baselines"])


def test_usable_cycle_counts_include_native_nbm_gefs_and_ensemble(runtime: Path) -> None:
    generations = [_generation(runtime, hour) for hour in range(3)]
    for index, directory in enumerate(generations):
        manifest = json.loads((directory / "snapshot.json").read_bytes())
        descriptors = []
        for source_id in ("GEFS_6H", "ECMWF_ENS_24H"):
            source = directory / source_id
            _write(
                source / "manifest.json",
                {
                    "source_id": source_id,
                    "events": [{"source_cycle": f"2026-10-07T0{index}:00:00Z"}],
                },
            )
            descriptors.append(
                {
                    "source_id": source_id,
                    "status": "prepared",
                    "directory": str(source),
                    "manifest_sha256": retention._digest(source / "manifest.json"),
                }
            )
        manifest["evidence"] = {"probability_sources": descriptors}
        if index == 2:
            # Unavailable newer discovery must not count as a retained complete cycle.
            manifest["contributors"]["GFS"]["status"] = "unavailable"
        _write(directory / "snapshot.json", manifest)
    rows = _rows(retention.plan_retention(runtime, counts=COUNTS))
    assert "recent_GFS_cycle_window" in rows[generations[1].name]["reasons"]
    assert "recent_GFS_cycle_window" not in rows[generations[2].name]["reasons"]
    assert "recent_GEFS_cycle_window" in rows[generations[2].name]["reasons"]
    assert "recent_ECMWF_ENS_cycle_window" in rows[generations[2].name]["reasons"]
    assert "recent_NBM_cycle_window" in rows[generations[2].name]["reasons"]


def test_apply_expires_only_backed_up_payload_and_is_idempotent(runtime: Path) -> None:
    old, new = _generation(runtime, 1), _generation(runtime, 2)
    _baseline(runtime, old, "historical", old / "snapshot.json")
    immutable = {
        path: path.read_bytes()
        for path in runtime.rglob("*")
        if path.is_file() and path.suffix not in {".grib2", ".nc"}
    }
    plan = retention.plan_retention(runtime, counts=COUNTS)
    assert {row["snapshot_id"] for row in plan["candidates"]} == {old.name}
    receipt = _receipt(runtime, plan)
    result = retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert result["status"] == "complete" and result["deleted_bytes"] == 35
    assert not (old / "source.grib2").exists() and not (old / "prepared.nc").exists()
    assert (new / "source.grib2").exists()
    assert all(path.read_bytes() == data for path, data in immutable.items())
    repeated = retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert repeated["operation"] == "already_applied"
    assert len(list((runtime / "retention/transactions").glob("*.json"))) == 1
    expired = _rows(retention.plan_retention(runtime, counts=COUNTS))[old.name]
    assert expired["native_replay"] == "unavailable_after_retention"
    assert expired["expired_payload_bytes"] == 35
    with pytest.raises(ValueError, match="restore the recovery copy"):
        retention.pin_case(runtime, old.name, reason="Too late to protect original input bundle")
    (old / "source.grib2").write_bytes(b"raw meteorology")
    (old / "prepared.nc").write_bytes(b"prepared meteorology")
    retention.pin_case(runtime, old.name, reason="Exact source bundle restored from recovery copy")
    restored = _rows(retention.plan_retention(runtime, counts=COUNTS))[old.name]
    assert restored["native_replay"] == "retained"
    assert "PINNED" in restored["labels"]


@pytest.mark.parametrize(
    "mutation", ["receipt", "root", "new_payload", "pin", "metadata", "future_receipt"]
)
def test_backup_must_cover_exact_current_plan(runtime: Path, mutation: str) -> None:
    old = _generation(runtime, 1)
    _generation(runtime, 2)
    plan = retention.plan_retention(runtime, counts=COUNTS)
    receipt = _receipt(runtime, plan)
    document = json.loads(receipt.read_bytes())
    if mutation == "receipt":
        document["status"] = "pending"
    elif mutation == "root":
        document["runtime_root"] = str(runtime.parent)
    elif mutation == "future_receipt":
        document["verified_at"] = "2030-01-01T00:00:00Z"
    elif mutation == "new_payload":
        (old / "source.grib2").write_bytes(b"changed raw")
    elif mutation == "pin":
        retention.pin_case(runtime, old.name, reason="protect")
    else:
        _write(old / "new-reference.json", {"metadata": "new"})
    _write(receipt, document)
    with pytest.raises(ValueError):
        retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert (old / "source.grib2").exists()


def test_interrupted_unlink_resumes_only_original_authorized_remaining_files(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _generation(runtime, 1)
    _generation(runtime, 2)
    plan = retention.plan_retention(runtime, counts=COUNTS)
    receipt = _receipt(runtime, plan)
    unlink = Path.unlink
    first = True

    def crash_after_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal first
        unlink(path, *args, **kwargs)
        if first and path.suffix in {".nc", ".grib2"}:
            first = False
            raise OSError("simulated interruption after unlink")

    monkeypatch.setattr(Path, "unlink", crash_after_unlink)
    with pytest.raises(OSError, match="interruption"):
        retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert len([path for path in old.iterdir() if path.suffix in {".grib2", ".nc"}]) == 1
    outcome = retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert outcome["status"] == "complete" and len(outcome["deleted"]) == 2
    assert (old / "snapshot.json").exists()


def test_interrupted_cleanup_refuses_new_pin(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _generation(runtime, 1)
    _generation(runtime, 2)
    receipt = _receipt(runtime, retention.plan_retention(runtime, counts=COUNTS))

    unlink = Path.unlink

    def failure(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.suffix in {".nc", ".grib2"}:
            raise OSError("no unlink")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failure)
    with pytest.raises(OSError):
        retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    retention.pin_case(runtime, old.name, reason="important case")
    with pytest.raises(ValueError, match="Protected dependencies changed"):
        retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert (old / "source.grib2").exists()


@pytest.mark.parametrize("relative", [False, True])
def test_failed_unknown_and_cross_generation_dependencies_remain_protected(
    runtime: Path, relative: bool
) -> None:
    failed = _generation(runtime, 0, failed=True)
    old = _generation(runtime, 1)
    newer = _generation(runtime, 2)
    _write(
        newer / "external-replay.json",
        {"raw": f"../{old.name}/source.grib2" if relative else str(old / "source.grib2")},
    )
    rows = _rows(retention.plan_retention(runtime, counts=COUNTS))
    assert "UNRESOLVED" in rows[failed.name]["labels"]
    assert "retained_local_metadata_dependency" in rows[old.name]["reasons"]
    assert all(row["action"] == "retain" for row in rows.values())


@pytest.mark.parametrize(
    "state", ["busy", "in_flight", "malformed", "unknown_baseline", "unknown_file"]
)
def test_unproven_reference_inventory_fails_closed(runtime: Path, state: str) -> None:
    old = _generation(runtime, 1)
    _generation(runtime, 2)
    if state in {"busy", "in_flight"}:
        _write(
            runtime / "status/guidance-worker.json",
            {state: True} if state == "in_flight" else {"state": "busy"},
        )
    elif state == "malformed":
        (old / "broken.json").write_text("{", encoding="utf-8")
    elif state == "unknown_file":
        (old / "opaque.data").write_bytes(b"unknown retained consumer")
    else:
        _write(runtime / "baseline/baselines/unfinished/partial.json", {})
    report = retention.plan_retention(runtime, counts=COUNTS)
    assert report["removable_bytes"] == 0
    assert old.is_dir()


def test_pin_and_shared_worker_lock(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _generation(runtime, 1)
    _generation(runtime, 2)
    retention.pin_case(runtime, old.name, reason="research")
    assert "PINNED" in _rows(retention.plan_retention(runtime, counts=COUNTS))[old.name]["labels"]
    retention.pin_case(runtime, old.name, reason="", remove=True)
    receipt = _receipt(runtime, retention.plan_retention(runtime, counts=COUNTS))

    @contextmanager
    def busy(_root: Path):
        yield False

    monkeypatch.setattr(retention, "single_writer", busy)
    with pytest.raises(RuntimeError):
        retention.pin_case(runtime, old.name, reason="protect")
    with pytest.raises(RuntimeError):
        retention.apply_retention(runtime, counts=COUNTS, backup_receipt=receipt, now=NOW)
    assert (old / "source.grib2").exists()


def test_status_dry_run_has_no_writes_and_apply_needs_backup(
    runtime: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _generation(runtime, 1)
    from mesoforge.storage.postgres import database

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("read-only inventory must not access database or delete")

    monkeypatch.setattr(database, "resolve_database_dsn", forbidden)
    monkeypatch.setattr(Path, "unlink", forbidden)
    before = {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()}
    for flag in ("--status", "--dry-run"):
        assert retention.main(["--runtime-root", str(runtime), flag]) == 0
        assert json.loads(capsys.readouterr().out)["operation"] == "dry_run"
    assert retention.main(["--runtime-root", str(runtime), "--apply"]) == 2
    assert "ValueError" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in runtime.rglob("*") if path.is_file()} == before


def test_untrusted_exception_redaction(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("private://credential-and-provider-error")

    monkeypatch.setattr(retention, "plan_retention", broken)
    assert retention.main(["--runtime-root", str(runtime)]) == 2
    result = capsys.readouterr()
    assert "OSError" in result.err and "credential" not in result.err


def test_links_escape_and_runner_data_are_never_touched(runtime: Path, tmp_path: Path) -> None:
    old = _generation(runtime, 1)
    runner = tmp_path / "actions-runner/_work"
    _write(runner / "keep.json", {"important": "another application"})
    before = (runner / "keep.json").read_bytes()
    with pytest.raises(ValueError, match="managed snapshot"):
        retention.pin_case(runtime, "../../actions-runner", reason="escape")
    with pytest.raises(ValueError):
        retention._candidate_path(
            runtime, {"path": "guidance/snapshots/../../source.grib2", "snapshot_id": old.name}
        )
    with pytest.raises(ValueError, match="infrastructure"):
        retention.plan_retention(runner)
    assert (runner / "keep.json").read_bytes() == before
    try:
        (old / "linked").symlink_to(runner, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    with pytest.raises(ValueError, match="links"):
        retention.plan_retention(runtime)
    assert (runner / "keep.json").read_bytes() == before


def test_unknown_schema_and_invalid_counts_fail_closed(runtime: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        retention.CycleCounts(GEFS=0)
    old = _generation(runtime, 1)
    _write(old / "snapshot.json", {"schema_version": "future-schema"})
    with pytest.raises(ValueError, match="Unsupported prepared"):
        retention.plan_retention(runtime)


def test_verified_local_recovery_archive_authorizes_exact_native_expiry(runtime: Path) -> None:
    from mesoforge.application import local_backup
    from tests.unit.test_local_backup import checksums, recovery_set

    old = _generation(runtime, 1)
    _generation(runtime, 2)
    plan = retention.plan_retention(runtime, counts=COUNTS)
    source = recovery_set(runtime.parent)
    _write(source / "retention-plan.json", plan)
    _write(
        source / "deployment.json",
        {"runtime_root": str(runtime), "runtime_archive_root": str(runtime)},
    )
    with tarfile.open(source / "runtime.tar.gz", "w:gz") as archive:
        for path in runtime.rglob("*"):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(runtime).as_posix())
    checksums(source)
    clock = datetime(2026, 10, 9, tzinfo=UTC)
    backup = local_backup.validate_backup(source, now=clock)
    assert backup["retention_plan_sha256"] == plan["plan_sha256"]
    result = retention.apply_retention(
        runtime, counts=COUNTS, backup_receipt=source / local_backup.RECEIPT_NAME, now=clock
    )
    assert result["status"] == "complete"
    assert result["backup_id"] == backup["backup_id"]
    assert not (old / "source.grib2").exists()
