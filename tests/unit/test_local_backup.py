"""Local recovery validation with tiny real archives, no database or network calls."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mesoforge.application import local_backup as backup
from mesoforge.common.identifiers import Digest
from mesoforge.storage.s3 import content_addressed_key


def checksums(source: Path) -> None:
    names = ("postgres.dump", "runtime.tar.gz", "objects/objects-manifest.json", "images.txt")
    (source / "SHA256SUMS").write_text(
        "".join(f"{backup.file_digest(source / name)}  {name}\n" for name in names),
        encoding="utf-8",
    )


def archive(
    source: Path, member: str = "guidance/source.bin", kind: bytes = tarfile.REGTYPE
) -> None:
    with tarfile.open(source / "runtime.tar.gz", "w:gz") as output:
        info = tarfile.TarInfo(member)
        info.type = kind
        payload = b"retained native guidance bytes"
        if kind == tarfile.REGTYPE:
            info.size = len(payload)
            output.addfile(info, io.BytesIO(payload))
        else:
            info.linkname = "outside"
            output.addfile(info)


def recovery_set(tmp_path: Path) -> Path:
    source = tmp_path / "mesoforge-completed-test"
    source.mkdir()
    (source / "postgres.dump").write_bytes(b"PGDMP" + b"small fixture custom dump")
    (source / "postgres.list").write_text("; fixture pg_restore --list proof\n", encoding="utf-8")
    (source / "images.txt").write_text("mesoforge:immutable-test-revision\n", encoding="utf-8")
    archive(source)
    payload = b"an immutable issued forecast object"
    digest = Digest.of_bytes(payload)
    key = content_addressed_key(digest)
    # export_objects receives backup/objects and then uses the storage key beneath it.
    path = source / "objects" / key
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    manifest = {
        "schema_version": "mesoforge.object-export.v1",
        "exported_at": "2026-10-08T16:41:06Z",
        "bucket": "live-source-bucket",
        "objects": [
            {
                "content_digest": str(digest),
                "storage_uri": f"s3://live-source-bucket/{key}",
                "byte_size": len(payload),
                "media_type": "application/json",
            }
        ],
    }
    (source / "objects/objects-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    checksums(source)
    return source


def plan(source: Path, root: Path) -> dict[str, Any]:
    value: dict[str, Any] = {
        "runtime_root": str(root),
        "status": "dry_run",
        "schema_version": "mesoforge.guidance-retention-plan.v1",
        "candidates": [
            {
                "path": "guidance/source.bin",
                "bytes": len(b"retained native guidance bytes"),
                "sha256": hashlib.sha256(b"retained native guidance bytes").hexdigest(),
                "snapshot_id": "test-only",
            }
        ],
    }
    value["plan_sha256"] = hashlib.sha256(backup.canonical(value)).hexdigest()
    (source / "retention-plan.json").write_text(json.dumps(value), encoding="utf-8")
    (source / "deployment.json").write_text(
        json.dumps(
            {
                "runtime_archive_root": str(root),
                "runtime_root": str(root),
            }
        ),
        encoding="utf-8",
    )
    return value


def test_completed_backup_validates_actual_export_layout_and_reproducible_identity(
    tmp_path: Path,
) -> None:
    source = recovery_set(tmp_path)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    receipt = backup.validate_backup(source, now=datetime(2026, 10, 8, 17, tzinfo=UTC))
    assert receipt["schema_version"] == "mesoforge.local-backup-receipt.v1"
    assert receipt["status"] == "verified" and receipt["objects"] == 1
    assert receipt["source"] == str(source.resolve())
    assert receipt["runtime_coverage"] == "complete_retained_runtime"
    assert receipt["bytes"] == sum(len(value) for value in before.values())
    for row in receipt["files"]:
        assert row["sha256"] == hashlib.sha256(before[Path(row["path"])]).hexdigest()
    assert json.loads((source / backup.RECEIPT_NAME).read_text()) == receipt
    # Validation does not rewrite a source or duplicate its payloads.
    assert all((source / path).read_bytes() == data for path, data in before.items())
    count_after_first = len(list(source.rglob("*")))
    again = backup.validate_backup(source, now=datetime(2026, 10, 8, 18, tzinfo=UTC))
    assert again["backup_id"] == receipt["backup_id"]
    assert again["files"] == receipt["files"]
    assert again["verified_at"] != receipt["verified_at"]
    assert len(list(source.rglob("*"))) == count_after_first


def test_retention_receipt_binds_exact_plan_and_runtime_not_a_generic_old_backup(
    tmp_path: Path,
) -> None:
    source = recovery_set(tmp_path)
    retained = plan(source, tmp_path / "dedicated-runtime")
    (source / "daily-receipts.json").write_text('{"days":[]}', encoding="utf-8")
    (source / "backup-estimate.json").write_text('{"backup_bytes":1000}', encoding="utf-8")
    # Not whitelisted: credentials must not become receipt/export payloads by globbing.
    (source / "email.env").write_text("SMTP_PASSWORD=do-not-include", encoding="utf-8")
    receipt = backup.validate_backup(source)
    assert receipt["runtime_root"] == retained["runtime_root"]
    assert receipt["retention_plan_sha256"] == retained["plan_sha256"]
    paths = {row["path"] for row in receipt["files"]}
    assert {
        "retention-plan.json",
        "daily-receipts.json",
        "deployment.json",
        "backup-estimate.json",
    } <= paths
    assert "email.env" not in paths
    assert "do-not-include" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "problem",
    [
        "changed_dump",
        "invalid_dump",
        "missing_list",
        "changed_object",
        "wrong_uri",
        "duplicate_object",
        "missing_checksum",
        "naive_time",
        "bad_size",
        "duplicate_checksum",
    ],
)
def test_corrupt_local_recovery_set_never_gets_a_success_receipt(
    tmp_path: Path, problem: str
) -> None:
    source = recovery_set(tmp_path)
    manifest_path = source / "objects/objects-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if problem in {"changed_dump", "invalid_dump"}:
        (source / "postgres.dump").write_bytes(b"not a readable dump")
        if problem == "invalid_dump":
            checksums(source)
    elif problem == "missing_list":
        (source / "postgres.list").write_text("")
    elif problem == "changed_object":
        key = content_addressed_key(Digest(manifest["objects"][0]["content_digest"]))
        (source / "objects" / key).write_bytes(b"corrupt")
    elif problem == "missing_checksum":
        lines = (source / "SHA256SUMS").read_text().splitlines()
        (source / "SHA256SUMS").write_text("\n".join(lines[1:]))
    elif problem == "duplicate_checksum":
        text = (source / "SHA256SUMS").read_text()
        (source / "SHA256SUMS").write_text(text + text.splitlines()[0] + "\n")
    else:
        if problem == "wrong_uri":
            manifest["objects"][0]["storage_uri"] = "s3://wrong/key"
        elif problem == "duplicate_object":
            manifest["objects"].append(dict(manifest["objects"][0]))
        elif problem == "naive_time":
            manifest["exported_at"] = "2026-10-08T16:41:06"
        elif problem == "bad_size":
            manifest["objects"][0]["byte_size"] = True
        manifest_path.write_text(json.dumps(manifest))
        checksums(source)
    with pytest.raises(ValueError):
        backup.validate_backup(source)
    assert not (source / backup.RECEIPT_NAME).exists()


@pytest.mark.parametrize(
    ("member", "kind"),
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard", tarfile.LNKTYPE),
        ("fifo", tarfile.FIFOTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_unsafe_runtime_archive_is_not_accepted(tmp_path: Path, member: str, kind: bytes) -> None:
    source = recovery_set(tmp_path)
    archive(source, member, kind)
    checksums(source)
    with pytest.raises(ValueError, match="Unsafe runtime"):
        backup.validate_backup(source)
    assert not (source / backup.RECEIPT_NAME).exists()


def test_full_gzip_trailer_is_checked_even_with_updated_outer_checksum(tmp_path: Path) -> None:
    source = recovery_set(tmp_path)
    path = source / "runtime.tar.gz"
    data = path.read_bytes()
    path.write_bytes(data[:-4])
    checksums(source)
    with pytest.raises((EOFError, OSError, tarfile.TarError)):
        backup.validate_backup(source)
    assert not (source / backup.RECEIPT_NAME).exists()


@pytest.mark.parametrize(
    "relative", ["../outside", "/absolute", "nested/../../outside", "nested\\outside", "C:/outside"]
)
def test_backup_member_escape_is_rejected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        backup.safe_file(tmp_path, relative)


@pytest.mark.parametrize("problem", ["bad_digest", "changed_root", "relative_root", "escape_root"])
def test_invalid_retention_plan_cannot_authorize_cleanup(tmp_path: Path, problem: str) -> None:
    source = recovery_set(tmp_path)
    value = plan(source, tmp_path / "runtime")
    if problem == "bad_digest":
        value["plan_sha256"] = "0" * 64
    elif problem == "changed_root":
        value["runtime_root"] = str(tmp_path / "different-runtime")
    else:
        value["runtime_root"] = (
            "relative/runtime" if problem == "relative_root" else "/runtime/../escape"
        )
        raw = {key: v for key, v in value.items() if key != "plan_sha256"}
        value["plan_sha256"] = hashlib.sha256(backup.canonical(raw)).hexdigest()
    (source / "retention-plan.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        backup.validate_backup(source)
    assert not (source / backup.RECEIPT_NAME).exists()


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "wrong_digest",
        "wrong_size",
        "directory",
        "escape",
        "wrong_deployment",
        "missing_deployment",
    ],
)
def test_retention_receipt_requires_actual_archived_candidate_bytes(
    tmp_path: Path, problem: str
) -> None:
    source = recovery_set(tmp_path)
    value = plan(source, tmp_path / "runtime" / "minneapolis-daily")
    deployment = source / "deployment.json"
    mapping = json.loads(deployment.read_text())
    mapping["runtime_archive_root"] = str(tmp_path / "runtime")
    deployment.write_text(json.dumps(mapping))
    archive(source, "minneapolis-daily/guidance/source.bin")
    checksums(source)
    if problem == "missing":
        value["candidates"][0]["path"] = "guidance/not-backed-up.bin"
    elif problem == "wrong_digest":
        value["candidates"][0]["sha256"] = "0" * 64
    elif problem == "wrong_size":
        value["candidates"][0]["bytes"] += 1
    elif problem == "directory":
        archive(source, "minneapolis-daily/guidance/source.bin", tarfile.DIRTYPE)
        checksums(source)
    elif problem == "escape":
        value["candidates"][0]["path"] = "../other/source.bin"
    elif problem == "wrong_deployment":
        mapping["runtime_archive_root"] = str(tmp_path / "different")
        deployment.write_text(json.dumps(mapping))
    else:
        deployment.unlink()
    raw = {key: v for key, v in value.items() if key != "plan_sha256"}
    value["plan_sha256"] = hashlib.sha256(backup.canonical(raw)).hexdigest()
    (source / "retention-plan.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        backup.validate_backup(source)
    assert not (source / backup.RECEIPT_NAME).exists()


def test_retention_archive_can_cover_selected_runtime_below_full_volume(tmp_path: Path) -> None:
    source = recovery_set(tmp_path)
    value = plan(source, tmp_path / "volume" / "minneapolis-daily")
    (source / "deployment.json").write_text(
        json.dumps(
            {
                "runtime_archive_root": str(tmp_path / "volume"),
                "runtime_root": value["runtime_root"],
            }
        )
    )
    archive(source, "./minneapolis-daily/guidance/source.bin")
    checksums(source)
    receipt = backup.validate_backup(source)
    assert receipt["retention_plan_sha256"] == value["plan_sha256"]


def test_incomplete_backup_and_naive_validation_clock_are_rejected(tmp_path: Path) -> None:
    source = recovery_set(tmp_path)
    with pytest.raises(ValueError, match="aware clock"):
        backup.validate_backup(source, now=datetime(2026, 10, 8, 17))
    incomplete = source.with_name(source.name + ".incomplete")
    source.rename(incomplete)
    with pytest.raises(ValueError, match="completed"):
        backup.validate_backup(incomplete)


def test_cli_failure_redacts_exception_text_and_never_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = recovery_set(tmp_path)
    assert backup.main(["validate", "--source", str(source)]) == 0
    sensitive_marker = "not-for-output"

    def fail(path: Path) -> dict[str, Any]:
        raise RuntimeError(sensitive_marker)

    monkeypatch.setattr(backup, "validate_backup", fail)
    assert backup.main(["validate", "--source", str(source)]) == 1
    output = capsys.readouterr().out
    assert sensitive_marker not in output and '"reason": "RuntimeError"' in output
    assert (source / "postgres.dump").exists()


def verified_generation(root: Path, day: int) -> Path:
    source = recovery_set(root)
    target = source.with_name(f"mesoforge-202610{day:02}T000000Z-test")
    source.rename(target)
    manifest = target / "objects/objects-manifest.json"
    values = json.loads(manifest.read_text())
    values["exported_at"] = f"2026-10-{day:02}T00:00:00Z"
    manifest.write_text(json.dumps(values))
    checksums(target)
    backup.validate_backup(target, now=datetime(2026, 10, 15, tzinfo=UTC))
    return target


def test_prune_dry_run_keeps_two_newest_and_current_and_protects_legacy(tmp_path: Path) -> None:
    root = tmp_path / "dedicated-backups"
    root.mkdir()
    sets = [verified_generation(root, day) for day in range(1, 5)]
    legacy = root / "mesoforge-legacy"
    legacy.mkdir()
    (legacy / "postgres.dump").write_bytes(b"historical protected copy")
    incomplete = root / "mesoforge-running.incomplete"
    incomplete.mkdir()
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    planned = backup.prune_plan(root, current_backup=sets[0])
    assert planned["status"] == "dry_run"
    assert [row["name"] for row in planned["candidates"]] == [sets[1].name]
    assert all(path.read_bytes() == content for path, content in snapshot.items())
    assert len(list(root.rglob("*"))) > len(snapshot)
    result = backup.apply_prune(root, current_backup=sets[0])
    assert result["removed"] == [sets[1].name]
    assert not sets[1].exists()
    assert all(path.exists() for path in (sets[0], *sets[2:], legacy, incomplete))
    assert json.loads((root / "local-backup-prune.json").read_text())["status"] == "completed"


def test_modified_or_foreign_backup_files_are_never_pruned(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    sets = [verified_generation(root, day) for day in range(1, 5)]
    (sets[0] / "operator-notes.txt").write_text("not owned by the backup validator")
    (sets[1] / "postgres.dump").write_bytes(b"changed since receipt")
    planned = backup.prune_plan(root)
    assert planned["candidates"] == []
    assert {row["name"] for row in planned["protected"]} == {path.name for path in sets}


def test_interrupted_prune_preserves_newest_and_can_resume_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    sets = [verified_generation(root, day) for day in range(1, 5)]
    deleted = []
    real_remove = shutil.rmtree

    def interrupt(path: Path) -> None:
        # The persisted dry-run/report must exist before any removal starts.
        assert (root / "local-backup-prune.json").is_file()
        if deleted:
            raise OSError("injected interrupted cleanup")
        real_remove(path)
        deleted.append(path)

    monkeypatch.setattr(backup.shutil, "rmtree", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        backup.apply_prune(root, current_backup=sets[-1])
    receipt = json.loads((root / "local-backup-prune.json").read_text())
    assert receipt["status"] == "interrupted" and len(receipt["removed"]) == 1
    assert all(path.is_dir() for path in sets[2:])
    monkeypatch.setattr(backup.shutil, "rmtree", real_remove)
    assert len(backup.apply_prune(root, current_backup=sets[-1])["removed"]) == 1
    assert all(path.is_dir() for path in sets[2:])


@pytest.mark.parametrize("keep", [0, 1, True])
def test_backup_prune_cannot_drop_the_recovery_buffer(tmp_path: Path, keep: int) -> None:
    with pytest.raises(ValueError, match="at least two"):
        backup.prune_plan(tmp_path, keep=keep)


def test_backup_prune_rejects_runner_root_or_outside_current(tmp_path: Path) -> None:
    runner = tmp_path / "_work" / "backups"
    runner.mkdir(parents=True)
    with pytest.raises(ValueError, match="dedicated root"):
        backup.prune_plan(runner)
    root = tmp_path / "backups"
    root.mkdir()
    with pytest.raises(ValueError, match="direct unlinked child"):
        backup.prune_plan(root, current_backup=tmp_path / "elsewhere")


def test_backup_worker_lock_is_held_until_parent_pipe_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mesoforge.application.worker_lock import single_writer

    monkeypatch.delenv("MESOFORGE_WORKER_LOCK_ROOT", raising=False)

    class ParentPipe:
        def read(self) -> bytes:
            with single_writer(tmp_path) as acquired:
                assert not acquired
            return b""

    class Input:
        buffer = ParentPipe()

    monkeypatch.setattr(backup.sys, "stdin", Input())
    assert backup.main(["hold-lock", "--runtime-root", str(tmp_path)]) == 0
    assert '"status": "locked"' in capsys.readouterr().out
    with single_writer(tmp_path) as acquired:
        assert acquired
        assert backup.hold_lock(tmp_path) == 3
    assert '"status": "busy"' in capsys.readouterr().out


def test_backup_estimate_accounts_for_full_recovery_without_artifact_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "native.bin").write_bytes(b"x" * 101)
    (runtime / "prepared").mkdir()
    (runtime / "prepared" / "array.bin").write_bytes(b"x" * 201)
    monkeypatch.setattr(backup, "_database_sizes", lambda: (100, 500))
    before = set(runtime.rglob("*"))
    result = backup.estimate_backup(runtime)
    assert result["runtime_bytes"] == 302
    assert result["database_bytes"] == 100 and result["objects_bytes"] == 500
    assert result["backup_bytes"] == 306 + 500 + 200 + backup.BACKUP_METADATA_RESERVE_BYTES
    assert set(runtime.rglob("*")) == before


def test_backup_estimate_cli_refuses_projected_disk_reserve_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    destination = tmp_path / "backups"
    destination.mkdir()
    monkeypatch.setattr(backup, "_database_sizes", lambda: (100, 500))
    monkeypatch.setenv("MESOFORGE_GUIDANCE_MIN_FREE_GB", "20")
    monkeypatch.setattr(
        backup.shutil, "disk_usage", lambda path: SimpleNamespace(free=20 * 1024**3)
    )
    assert (
        backup.main(["estimate", "--runtime-root", str(runtime), "--destination", str(destination)])
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "estimated" and result["admitted"] is False
    assert list(destination.iterdir()) == [] and list(runtime.iterdir()) == []


def test_backup_database_estimate_uses_one_read_only_query_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy as sa

    from mesoforge.storage.postgres import database

    queries = []
    disposed = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, query: Any) -> Any:
            queries.append(str(query))
            return SimpleNamespace(one=lambda: (100, 200))

    engine = SimpleNamespace(connect=Connection, dispose=lambda: disposed.append(True))
    monkeypatch.setattr(sa, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(database, "resolve_database_dsn", lambda key: "unused-test-dsn")
    assert backup._database_sizes() == (100, 200)
    assert queries == [
        "SELECT pg_database_size(current_database()), "
        "COALESCE(SUM(byte_size), 0) FROM stored_objects"
    ]
    assert disposed == [True]


def test_unreadable_runtime_inventory_fails_closed_instead_of_underestimating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def inaccessible(root: Path, *, onerror: Any, followlinks: bool) -> list[Any]:
        assert root == tmp_path and followlinks is False
        onerror(PermissionError("fixture unreadable runtime directory"))
        return []

    monkeypatch.setattr(backup.os, "walk", inaccessible)
    monkeypatch.setattr(
        backup, "_database_sizes", lambda: pytest.fail("No database work after unreadable runtime")
    )
    with pytest.raises(PermissionError):
        backup.estimate_backup(tmp_path)


def test_unreadable_backup_inventory_cannot_authorize_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = recovery_set(tmp_path)
    backup.validate_backup(source)

    def inaccessible(root: Path, *, onerror: Any, followlinks: bool) -> list[Any]:
        onerror(PermissionError("fixture unreadable backup directory"))
        return []

    monkeypatch.setattr(backup.os, "walk", inaccessible)
    result = backup.prune_plan(tmp_path)
    assert result["candidates"] == []
    assert result["protected"] == [
        {"name": source.name, "reason": "legacy_incomplete_or_unverified"}
    ]
