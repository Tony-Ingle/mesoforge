"""Validate a completed same-host recovery copy without database or provider access.

The committed backup script already checks ``pg_restore --list``. This boundary
rechecks its retained result, source hashes, every exported object, and the complete
runtime archive before publishing a compact receipt. A local recovery copy does not
protect against loss of the VPS. No backup or live artifact is deleted here.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from mesoforge.application.disk_admission import DiskPolicy
from mesoforge.application.worker_lock import single_writer, worker_lock_root
from mesoforge.application.worker_status import iso, write_json
from mesoforge.common.identifiers import Digest
from mesoforge.storage.s3 import content_addressed_key

MANIFEST_SCHEMA = "mesoforge.local-backup-manifest.v1"
RECEIPT_SCHEMA = "mesoforge.local-backup-receipt.v1"
RECEIPT_NAME = "local-backup-receipt.json"
_REQUIRED = {"postgres.dump", "runtime.tar.gz", "objects/objects-manifest.json", "images.txt"}
_OPTIONAL = (
    "daily-receipts.json",
    "deployment.json",
    "retention-plan.json",
    "backup-estimate.json",
)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ROOT = Path(__file__).resolve().parents[3]
# Small non-runtime manifests plus conservative tar/database overhead. This is a
# capacity estimate, not a claim about gzip compression or an archiving policy.
BACKUP_METADATA_RESERVE_BYTES = 1024**3


def canonical(value: Any) -> bytes:
    """Compact JSON for this operational receipt and the retention-plan binding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_file(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (
        parts.is_absolute()
        or ".." in parts.parts
        or not parts.parts
        or "\\" in relative
        or ":" in relative
    ):
        raise ValueError("Backup member escapes its recovery set")
    path = root
    for part in parts.parts:
        path = path / part
        if path.is_symlink() or path.is_junction():
            raise ValueError("Backup links are not supported")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Missing or unsafe backup member")
    return path


def _runtime_archive(path: Path, expected: dict[str, dict[str, Any]]) -> None:
    """Read all file data and the gzip trailer, with no extraction or path writes."""
    matched = set()
    with gzip.open(path, "rb") as compressed:
        with tarfile.open(fileobj=compressed, mode="r|") as archive:
            seen = set()
            for member in archive:
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in member.name
                    or ":" in member.name
                    or not (member.isfile() or member.isdir())
                    or str(name) in seen
                ):
                    raise ValueError("Unsafe runtime recovery archive member")
                seen.add(str(name))
                if member.isfile():
                    data = archive.extractfile(member)
                    if data is None:
                        raise ValueError("Unreadable runtime recovery file")
                    checksum = hashlib.sha256() if str(name) in expected else None
                    with data:
                        while block := data.read(1 << 20):
                            if checksum is not None:
                                checksum.update(block)
                    if checksum is not None:
                        target = expected[str(name)]
                        if (
                            member.size != target["bytes"]
                            or checksum.hexdigest() != target["sha256"]
                        ):
                            raise ValueError(
                                "Runtime archive differs from planned retained payload"
                            )
                        matched.add(str(name))
        # tar's end blocks can precede the gzip CRC/trailer. Reading to EOF also
        # catches a truncated/corrupt compressed stream beyond the tar end marker.
        while compressed.read(1 << 20):
            pass
    if not expected.keys() <= matched:
        raise ValueError("Runtime archive omits planned retained payloads")


def _absolute_contract_path(value: str) -> PurePosixPath | PureWindowsPath:
    parsed = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("Backup runtime mapping requires an absolute path without escape")
    return parsed


def _plan_binding(source: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if not (source / "retention-plan.json").exists():
        return {}, {}
    plan = json.loads(safe_file(source, "retention-plan.json").read_text("utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("Retained cleanup plan must be an object")
    if plan.get("schema_version") != "mesoforge.guidance-retention-plan.v1":
        raise ValueError("Unsupported retained cleanup plan")
    identity = plan.get("plan_sha256")
    if not isinstance(identity, str) or not _HEX.fullmatch(identity):
        raise ValueError("Retained cleanup plan requires its canonical identity")
    raw = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if hashlib.sha256(canonical(raw)).hexdigest() != identity:
        raise ValueError("Retained cleanup plan checksum mismatch")
    root = plan.get("runtime_root")
    if not isinstance(root, str):
        raise ValueError("Retained cleanup plan requires an absolute runtime root")
    native_root = _absolute_contract_path(root)
    deployment = json.loads(safe_file(source, "deployment.json").read_text("utf-8"))
    if not isinstance(deployment, dict) or deployment.get("runtime_root") != root:
        raise ValueError("Recovery deployment must identify the exact planned runtime root")
    archive_root = deployment.get("runtime_archive_root")
    if not isinstance(archive_root, str):
        raise ValueError("Recovery deployment must identify the runtime archive root")
    prefix = native_root.relative_to(_absolute_contract_path(archive_root)).as_posix()
    candidates = plan.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Retained cleanup plan requires its candidate list")
    expected: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Invalid cleanup candidate")
        relative = PurePosixPath(item["path"])
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or "\\" in item["path"]
            or ":" in item["path"]
            or type(item.get("bytes")) is not int
            or item["bytes"] < 0
            or not isinstance(item.get("sha256"), str)
            or not _HEX.fullmatch(item["sha256"])
        ):
            raise ValueError("Invalid cleanup candidate path or content identity")
        name = str(PurePosixPath(prefix) / relative)
        if name in expected:
            raise ValueError("Duplicate cleanup candidate")
        expected[name] = item
    return {"runtime_root": root, "retention_plan_sha256": identity}, expected


def validate_backup(
    source: Path, *, now: datetime | None = None, _write_receipt: bool = True
) -> dict[str, Any]:
    """Publish a verification receipt only after checking the complete recovery set."""
    if (
        source.name.endswith(".incomplete")
        or source.is_symlink()
        or source.is_junction()
        or not source.is_dir()
    ):
        raise ValueError("Only a completed real backup directory can be validated")
    source = source.resolve()
    checked = set()
    for line in safe_file(source, "SHA256SUMS").read_text("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None or match[2] in checked:
            raise ValueError("Invalid backup checksum manifest")
        if file_digest(safe_file(source, match[2])) != match[1]:
            raise ValueError("Backup checksum mismatch")
        checked.add(match[2])
    if not _REQUIRED <= checked:
        raise ValueError("Incomplete backup checksum coverage")
    with safe_file(source, "postgres.dump").open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise ValueError("Expected PostgreSQL custom-format dump")
    if not safe_file(source, "postgres.list").read_text("utf-8").strip():
        raise ValueError("Missing database readability proof")
    binding, archive_members = _plan_binding(source)
    _runtime_archive(safe_file(source, "runtime.tar.gz"), archive_members)
    objects = json.loads(safe_file(source, "objects/objects-manifest.json").read_text("utf-8"))
    if (
        not isinstance(objects, dict)
        or objects.get("schema_version") != "mesoforge.object-export.v1"
    ):
        raise ValueError("Unsupported object export manifest")
    if not isinstance(objects.get("bucket"), str) or not objects["bucket"]:
        raise ValueError("Object export requires its source bucket")
    if not isinstance(objects.get("objects"), list):
        raise ValueError("Object export requires its immutable reference list")
    files = _REQUIRED | {"postgres.list", "SHA256SUMS"}
    seen = set()
    for row in objects["objects"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid object export row")
        identity = Digest(row["content_digest"])
        if identity in seen:
            raise ValueError("Repeated object export digest")
        seen.add(identity)
        key = content_addressed_key(identity)
        relative = f"objects/{key}"
        path = safe_file(source, relative)
        if (
            type(row.get("byte_size")) is not int
            or path.stat().st_size != row["byte_size"]
            or file_digest(path) != identity[7:]
        ):
            raise ValueError("Object export bytes differ from immutable reference")
        if row.get("storage_uri") != f"s3://{objects['bucket']}/{key}":
            raise ValueError("Object export source identity mismatch")
        files.add(relative)
    for optional in _OPTIONAL:
        if (source / optional).exists():
            json.loads(safe_file(source, optional).read_text("utf-8"))
            files.add(optional)
    created = objects.get("exported_at")
    if not isinstance(created, str) or datetime.fromisoformat(created).utcoffset() is None:
        raise ValueError("Backup requires an aware creation time")
    verified_at = now or datetime.now(UTC)
    if verified_at.tzinfo is None or verified_at.utcoffset() is None:
        raise ValueError("Backup validation requires an aware clock")
    rows: list[dict[str, Any]] = [
        {
            "path": name,
            "sha256": file_digest(safe_file(source, name)),
            "bytes": safe_file(source, name).stat().st_size,
        }
        for name in sorted(files)
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at": created,
        "files": rows,
        "objects": len(seen),
        "runtime_coverage": "complete_retained_runtime",
        **binding,
    }
    manifest_digest = hashlib.sha256(canonical(manifest)).hexdigest()
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "verified",
        "backup_id": manifest_digest,
        "manifest_sha256": manifest_digest,
        "verified_at": iso(verified_at),
        "source": str(source),
        "created_at": created,
        "bytes": sum(row["bytes"] for row in rows),
        "files": rows,
        "objects": len(seen),
        "runtime_coverage": "complete_retained_runtime",
        **binding,
    }
    if _write_receipt:
        write_json(source / RECEIPT_NAME, receipt)
    return receipt


def _backup_root(root: Path) -> Path:
    absolute = root.absolute()
    resolved = root.resolve(strict=True)
    if (
        absolute != resolved
        or not resolved.is_dir()
        or resolved == Path(resolved.anchor)
        or resolved.is_relative_to(_ROOT)
        or any(path.is_symlink() or path.is_junction() for path in (root, *root.parents))
        or (resolved / "status").is_symlink()
        or (resolved / "status").is_junction()
        or any(
            part.lower() in {"_work", "actions-runner", "postgres", "minio", "etc"}
            or part.lower().startswith("actions.runner.")
            for part in resolved.parts
        )
    ):
        raise ValueError("Backup pruning requires an unlinked dedicated root outside the checkout")
    return resolved


def _members_strict(root: Path) -> Iterator[Path]:
    """Never silently omit unreadable directories or follow links during inventory."""

    def unreadable(error: OSError) -> None:
        raise error

    for parent, directories, files in os.walk(root, onerror=unreadable, followlinks=False):
        directories.sort()
        for name in [*directories, *sorted(files)]:
            member = Path(parent) / name
            if member.is_symlink() or member.is_junction():
                raise ValueError("Recovery inventory cannot follow filesystem links")
            mode = member.stat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError("Recovery inventory found a special filesystem member")
            yield member


def _verified_set(path: Path) -> dict[str, Any]:
    stored = json.loads(safe_file(path, RECEIPT_NAME).read_text("utf-8"))
    if not isinstance(stored, dict):
        raise ValueError("Backup receipt must be an object")
    if stored.get("schema_version") != RECEIPT_SCHEMA or stored.get("status") != "verified":
        raise ValueError("Backup does not have this validator's successful receipt")
    verified_at = datetime.fromisoformat(stored["verified_at"])
    actual = validate_backup(path, now=verified_at, _write_receipt=False)
    if stored != actual:
        raise ValueError("Backup changed since its verification receipt")
    expected = {row["path"] for row in actual["files"]} | {RECEIPT_NAME}
    found = set()
    for member in _members_strict(path):
        relative = member.relative_to(path).as_posix()
        if member.is_file():
            found.add(relative)
        elif not member.is_dir() or not any(name.startswith(relative + "/") for name in expected):
            raise ValueError("Backup contains an unlisted member")
    if found != expected:
        raise ValueError("Backup contains an unlisted file")
    return actual


def prune_plan(root: Path, *, keep: int = 2, current_backup: Path | None = None) -> dict[str, Any]:
    """Inventory only this validator's unchanged recovery sets; legacy data is protected."""
    if type(keep) is not int or keep < 2:
        raise ValueError("Retain at least two complete verified recovery sets")
    root = _backup_root(root)
    current: Path | None = None
    if current_backup is not None:
        current = current_backup.absolute()
        if current.parent != root or current.resolve() != current:
            raise ValueError("Current backup must be a direct unlinked child of the backup root")
        _verified_set(current)
    verified = []
    protected: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not path.name.startswith("mesoforge-"):
            protected.append({"name": path.name, "reason": "not_a_managed_recovery_set"})
            continue
        try:
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked backup")
            receipt = _verified_set(path)
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError):
            protected.append({"name": path.name, "reason": "legacy_incomplete_or_unverified"})
            continue
        verified.append((datetime.fromisoformat(receipt["created_at"]), path.name, receipt))
    verified.sort(key=lambda item: (item[0], item[1]), reverse=True)
    retained = {name for _, name, _ in verified[:keep]}
    if current is not None:
        retained.add(current.name)
    candidates = []
    for _, name, receipt in verified:
        if name in retained:
            protected.append({"name": name, "reason": "current_or_recent_verified_recovery"})
        else:
            candidates.append(
                {"name": name, "backup_id": receipt["backup_id"], "bytes": receipt["bytes"]}
            )
    return {
        "schema_version": "mesoforge.local-backup-prune.v1",
        "status": "dry_run",
        "root": str(root),
        "keep": keep,
        "current_backup": str(current) if current is not None else None,
        "candidates": candidates,
        "protected": protected,
        "reclaimable_bytes": sum(row["bytes"] for row in candidates),
    }


def apply_prune(root: Path, *, keep: int = 2, current_backup: Path | None = None) -> dict[str, Any]:
    """Record a dry run first, then remove only revalidated direct-child recovery sets."""
    root = _backup_root(root)
    with single_writer(root) as acquired:
        if not acquired:
            raise RuntimeError("Another backup cleanup holds the recovery-root lock")
        plan = prune_plan(root, keep=keep, current_backup=current_backup)
        report = root / "local-backup-prune.json"
        write_json(report, plan)
        result = {**plan, "status": "applying", "removed": []}
        try:
            for item in plan["candidates"]:
                path = root / item["name"]
                if (
                    path.parent != root
                    or path.resolve() != path
                    or path.is_symlink()
                    or path.is_junction()
                ):
                    raise ValueError("Backup deletion path is no longer a safe direct child")
                if _verified_set(path)["backup_id"] != item["backup_id"]:
                    raise ValueError("Backup changed after cleanup planning")
                shutil.rmtree(path)
                result["removed"].append(item["name"])
                write_json(report, result)
        except BaseException:
            result["status"] = "interrupted"
            write_json(report, result)
            raise
        result["status"] = "completed"
        write_json(report, result)
        return result


def hold_lock(root: Path) -> int:
    """Hold the existing worker lock until the supervising backup process closes stdin."""
    with single_writer(worker_lock_root(root)) as acquired:
        print(json.dumps({"status": "locked" if acquired else "busy"}), flush=True)
        if not acquired:
            return 3
        sys.stdin.buffer.read()
    return 0


def _database_sizes() -> tuple[int, int]:
    import sqlalchemy as sa

    from mesoforge.storage.postgres.database import resolve_database_dsn

    engine = sa.create_engine(resolve_database_dsn("MESOFORGE_DATABASE_DSN"), future=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT pg_database_size(current_database()), "
                    "COALESCE(SUM(byte_size), 0) FROM stored_objects"
                )
            ).one()
            database_bytes, object_bytes = int(row[0]), int(row[1])
            if database_bytes < 0 or object_bytes < 0:
                raise ValueError("Invalid database capacity inventory")
            return database_bytes, object_bytes
    finally:
        engine.dispose()


def estimate_backup(runtime_root: Path, *, destination: Path | None = None) -> dict[str, Any]:
    """Read-only full recovery estimate before expensive work or backup creation."""
    root = _backup_root(runtime_root)
    runtime_bytes = 0
    for path in _members_strict(root):
        mode = path.stat().st_mode
        if stat.S_ISREG(mode):
            runtime_bytes += path.stat().st_size
        elif not stat.S_ISDIR(mode):
            raise ValueError("Runtime capacity inventory found a special filesystem member")
    database_bytes, object_bytes = _database_sizes()
    projected = (runtime_bytes * 101 + 99) // 100 + object_bytes + 2 * database_bytes
    projected += BACKUP_METADATA_RESERVE_BYTES
    result: dict[str, Any] = {
        "status": "estimated",
        "backup_bytes": projected,
        "runtime_bytes": runtime_bytes,
        "objects_bytes": object_bytes,
        "database_bytes": database_bytes,
        "metadata_reserve_bytes": BACKUP_METADATA_RESERVE_BYTES,
    }
    if destination is not None:
        target = _backup_root(destination)
        free = shutil.disk_usage(target).free
        policy = DiskPolicy.from_environment()
        result.update(
            {
                "free_bytes": free,
                "projected_free_bytes": max(0, free - projected),
                "min_free_bytes": policy.min_free_bytes,
                "admitted": free >= projected + policy.min_free_bytes,
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--source", type=Path, required=True)
    prune = commands.add_parser("prune")
    prune.add_argument("--root", type=Path, required=True)
    prune.add_argument("--keep", type=int, default=2)
    prune.add_argument("--current-backup", type=Path)
    prune.add_argument("--apply", action="store_true")
    hold = commands.add_parser("hold-lock")
    hold.add_argument("--runtime-root", type=Path, required=True)
    estimate = commands.add_parser("estimate")
    estimate.add_argument("--runtime-root", type=Path, required=True)
    estimate.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "hold-lock":
            return hold_lock(args.runtime_root)
        if args.operation == "estimate":
            result = estimate_backup(args.runtime_root, destination=args.destination)
        elif args.operation == "prune":
            operation = apply_prune if args.apply else prune_plan
            result = operation(args.root, keep=args.keep, current_backup=args.current_backup)
        else:
            result = validate_backup(args.source)
    except Exception as exc:
        # Exception text may include paths or provider material from malformed files.
        print(json.dumps({"status": "failed", "reason": type(exc).__name__}))
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result.get("admitted") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
