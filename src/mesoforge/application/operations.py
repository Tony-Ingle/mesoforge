"""Operator commands for a hosted deployment; explicit, local and never served over HTTP.

``status`` answers, in one read-only report: is the guidance worker running, is
guidance current, which baseline is pinned and when was it built, which contributor
cycles and governed policies it uses, what was issued last and whether the AI desk
edited it, what failed, and when the next scheduled run is. ``migrate`` is the only
schema-changing command and names its target database explicitly. Object export and
import copy the content-addressed store through the existing S3 abstraction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesoforge.application.baseline_readiness import (
    baseline_readiness,
    mark_governance_unavailable,
)
from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.forecast_schedule import ForecastSchedule
from mesoforge.application.forecast_worker import object_store
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    coverage_for,
    derive_reference_time,
    resolve_latest_complete,
)
from mesoforge.application.runtime_log import redact
from mesoforge.application.worker_status import (
    FORECAST_STATUS,
    GUIDANCE_STATUS,
    guidance_health,
    iso,
    read_json,
    status_directory,
)
from mesoforge.common.identifiers import Digest
from mesoforge.storage.s3 import content_addressed_key

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = _ROOT / "configs/locations.json"
EXPORT_MANIFEST = "objects-manifest.json"


def _error(exc: BaseException) -> str:
    return str(redact(f"{type(exc).__name__}: {exc}"))[:1000]


def database_target(dsn: str) -> dict[str, Any]:
    """Host, port, database and user of a DSN; the password is never returned."""
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    return {
        "driver": url.drivername,
        "host": url.host,
        "port": url.port,
        "database": url.database,
        "username": url.username,
    }


def migration_status() -> dict[str, Any]:
    from mesoforge.storage.postgres.database import resolve_database_dsn
    from mesoforge.storage.postgres.schema import schema_status

    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    return {"target": database_target(dsn), **schema_status(dsn)}


def migrate(expect_database: str) -> dict[str, Any]:
    """Explicit operator upgrade to the repository head of one named database.

    The migration DSN (``MESOFORGE_ALEMBIC_DSN``, else ``MESOFORGE_DATABASE_DSN``)
    must name ``expect_database`` and the same server the workers use.
    """
    from mesoforge.storage.postgres.schema import (
        apply_runtime_grants,
        schema_status,
        upgrade_to_head,
    )

    migration_dsn = os.environ.get("MESOFORGE_ALEMBIC_DSN") or os.environ.get(
        "MESOFORGE_DATABASE_DSN"
    )
    if not migration_dsn:
        raise RuntimeError("MESOFORGE_ALEMBIC_DSN or MESOFORGE_DATABASE_DSN must be set")
    target = database_target(migration_dsn)
    worker_dsn = os.environ.get("MESOFORGE_DATABASE_DSN")
    if worker_dsn:
        worker = database_target(worker_dsn)
        if {k: worker[k] for k in ("host", "port", "database")} != {
            k: target[k] for k in ("host", "port", "database")
        }:
            raise RuntimeError("Migration and worker DSNs name different databases; refusing")
    if target["database"] != expect_database:
        raise RuntimeError(
            f"Migration target database is {target['database']!r}, not {expect_database!r}"
        )
    before = schema_status(migration_dsn)
    upgrade_to_head()
    after = schema_status(migration_dsn)
    grants = apply_runtime_grants(migration_dsn)
    return {"target": target, "before": before, "after": after, "runtime_grants": grants}


def apply_grants(expect_database: str) -> dict[str, Any]:
    """Re-apply worker grants on an already migrated database (owner credentials)."""
    from mesoforge.storage.postgres.schema import apply_runtime_grants

    dsn = os.environ.get("MESOFORGE_ALEMBIC_DSN") or os.environ.get("MESOFORGE_DATABASE_DSN")
    if not dsn:
        raise RuntimeError("MESOFORGE_ALEMBIC_DSN or MESOFORGE_DATABASE_DSN must be set")
    target = database_target(dsn)
    if target["database"] != expect_database:
        raise RuntimeError(f"Target database is {target['database']!r}, not {expect_database!r}")
    return {"target": target, "runtime_grants": apply_runtime_grants(dsn)}


def init_storage() -> dict[str, Any]:
    """The one explicit bucket-creation step; workers never create buckets."""
    store = object_store(ensure_bucket=True)
    store.check_bucket()
    return {
        "bucket": os.environ["MESOFORGE_S3_BUCKET"],
        "endpoint": os.environ["MESOFORGE_S3_ENDPOINT"],
        "status": "ready",
    }


def _stored_objects() -> list[dict[str, Any]]:
    import sqlalchemy as sa

    from mesoforge.storage.postgres.database import resolve_database_dsn

    engine = sa.create_engine(resolve_database_dsn("MESOFORGE_DATABASE_DSN"), future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT content_digest, storage_uri, media_type, byte_size "
                    "FROM stored_objects ORDER BY content_digest"
                )
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        engine.dispose()


def _object_path(directory: Path, digest: str) -> Path:
    path = directory / content_addressed_key(Digest(digest))
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Export object path escapes its backup directory")
    return path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _outside_repository(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(_ROOT):
        raise ValueError("Backups must be written outside the repository")
    return resolved


def export_objects(destination: Path) -> dict[str, Any]:
    """Copy every object the database references into a verified local tree.

    Dump PostgreSQL first: issuance writes an object before its row commits, so
    exporting after the dump covers every row the dump contains.
    """
    destination = _outside_repository(destination)
    destination.mkdir(parents=True, exist_ok=True)
    rows = _stored_objects()
    store = object_store(ensure_bucket=False)
    copied = present = total = 0
    for row in rows:
        path = _object_path(destination, row["content_digest"])
        total += int(row["byte_size"])
        if (
            path.is_file()
            and path.stat().st_size == row["byte_size"]
            and _file_digest(path) == row["content_digest"]
        ):
            present += 1
            continue
        data = store.get_verified(row["storage_uri"], Digest(row["content_digest"]))
        if len(data) != row["byte_size"]:
            raise ValueError("Stored object byte size differs from the database reference")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        copied += 1
    manifest = {
        "schema_version": "mesoforge.object-export.v1",
        "exported_at": iso(datetime.now(UTC)),
        "bucket": os.environ.get("MESOFORGE_S3_BUCKET"),
        "objects": rows,
    }
    manifest_path = destination / EXPORT_MANIFEST
    temporary_manifest = manifest_path.with_suffix(".tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest_path)
    return {
        "objects": len(rows),
        "copied": copied,
        "already_present": present,
        "bytes": total,
        "destination": str(destination),
    }


def _validated_export(source: Path) -> dict[str, Any]:
    """Read and verify the entire export before any restore writes begin."""
    manifest = json.loads((source / EXPORT_MANIFEST).read_text(encoding="utf-8"))
    bucket = os.environ.get("MESOFORGE_S3_BUCKET")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != (
        "mesoforge.object-export.v1"
    ):
        raise ValueError("Unsupported object export schema")
    if not bucket or manifest.get("bucket") != bucket:
        raise ValueError("Restore into the bucket the database rows reference")
    if not isinstance(manifest.get("objects"), list):
        raise ValueError("Object export must contain an objects list")
    seen: set[str] = set()
    for row in manifest["objects"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid object export row")
        digest = Digest(row["content_digest"])
        if digest in seen:
            raise ValueError("Duplicate object digest in export")
        seen.add(digest)
        if row.get("storage_uri") != f"s3://{bucket}/{content_addressed_key(digest)}":
            raise ValueError("Export object key differs from its canonical database reference")
        if not isinstance(row.get("media_type"), str) or not row["media_type"]:
            raise ValueError("Invalid object media type")
        if type(row.get("byte_size")) is not int or row["byte_size"] < 0:
            raise ValueError("Invalid object byte size")
        path = _object_path(source, row["content_digest"])
        if _file_digest(path) != digest:
            raise ValueError(f"Export file differs from its digest: {path}")
        if path.stat().st_size != row["byte_size"]:
            raise ValueError(f"Export file differs from its byte size: {path}")
    return manifest


def validate_export(source: Path) -> dict[str, Any]:
    """Read-only preflight used before restoring the database or any objects."""
    manifest = _validated_export(source)
    return {"objects": len(manifest["objects"]), "bucket": manifest["bucket"], "valid": True}


def check_empty_storage() -> dict[str, Any]:
    """Refuse an occupied restore target; a missing bucket is acceptable."""
    object_store(ensure_bucket=False).check_empty_bucket()
    return {"bucket": os.environ["MESOFORGE_S3_BUCKET"], "empty": True}


def import_objects(source: Path) -> dict[str, Any]:
    """Restore an export after verifying all files, rechecking each before its write."""
    manifest = _validated_export(source)
    store = object_store(ensure_bucket=False)
    restored = 0
    for row in manifest["objects"]:
        path = _object_path(source, row["content_digest"])
        data = path.read_bytes()
        if f"sha256:{hashlib.sha256(data).hexdigest()}" != row["content_digest"]:
            raise ValueError(f"Export file differs from its digest: {path}")
        stored = store.put_if_absent(Digest(row["content_digest"]), data, row["media_type"])
        if stored.storage_uri != row["storage_uri"]:
            raise ValueError("Restored object key differs from the database reference")
        restored += 1
    return {"objects": restored, "bucket": manifest["bucket"]}


def _guidance(root: Path, now: datetime) -> dict[str, Any] | None:
    try:
        pointer, manifest, _ = resolve_latest_complete(root / "guidance")
    except (SnapshotError, OSError, ValueError, KeyError):
        return None
    current = derive_reference_time(now)
    try:
        covers = bool(coverage_for(manifest, current)["usable"])
    except (KeyError, ValueError):
        covers = False
    return {
        "contributor_state_id": pointer["snapshot_id"],
        "reference_time": pointer["reference_time"],
        "published_at": pointer["published_at"],
        "last_valid_time": pointer.get("last_valid_time"),
        "contributor_cycles": {
            model: row.get("cycle")
            for model, row in manifest.get("contributors", {}).items()
            if "cycle" in row
        },
        "covers_current_hour": covers,
    }


def _latest_issuances(locations: list[Any]) -> list[dict[str, Any]]:
    from mesoforge.storage.postgres.database import resolve_database_dsn
    from mesoforge.storage.postgres.repositories import PostgresUnitOfWork

    rows = []
    with PostgresUnitOfWork(resolve_database_dsn("MESOFORGE_DATABASE_DSN")) as uow:
        for location in locations:
            try:
                lat, lon = _coordinates(location)
            except ValueError:
                continue
            records = uow.issued_forecasts.list_for_coordinate(lat, lon, limit=1)
            latest = records[0] if records else None
            rows.append(
                {
                    "location": location.get("id") or location.get("name"),
                    "issued_forecast_id": str(latest.issued_forecast_id) if latest else None,
                    "issued_at": iso(latest.issued_at) if latest else None,
                    "target_reference_time": iso(latest.target_reference_time) if latest else None,
                }
            )
    return rows


def _section(report: dict[str, Any], name: str, function: Any) -> None:
    try:
        report[name] = function()
    except Exception as exc:
        report[name] = {"status": "unavailable", "error": _error(exc)}


def _baseline_readiness(
    root: Path, locations: list[Any], *, now: datetime, governance: Any
) -> dict[str, Any]:
    revision, failure = None, None
    try:
        revision = current_code_revision(_ROOT)
    except Exception as exc:
        failure = exc
    report = baseline_readiness(
        root,
        locations,
        now=now,
        governance=governance,
        expected_code_revision=revision,
    )
    if failure is not None:
        report["ready"] = False
        report["reasons"].append(f"code_revision_unavailable: {_error(failure)}")
    return report


def status(root: Path, config: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only deployment status; each section fails independently."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    root = root.resolve()
    locations = load_locations(config)
    report: dict[str, Any] = {"checked_at": iso(now), "root": str(root)}
    _section(report, "schema", migration_status)
    worker = read_json(status_directory(root) / GUIDANCE_STATUS) or {}
    last_poll = (worker.get("polls") or {}).get("last") or {}
    report["guidance_worker"] = {
        "health": guidance_health(root, now=now),
        "state": worker.get("state"),
        "started_at": worker.get("started_at"),
        "code_revision": worker.get("code_revision"),
        "last_poll": {key: last_poll.get(key) for key in ("finished_at", "categories", "seconds")},
        "last_refresh": (worker.get("refresh") or {}).get("last_attempt"),
        "last_probe": (worker.get("refresh") or {}).get("last_probe"),
        "last_build": (worker.get("build") or {}).get("last_attempt"),
        "background": worker.get("background"),
        "interrupted": len(worker.get("interrupted") or []),
        "disk": worker.get("disk"),
        "next_poll_at": worker.get("next_poll_at"),
    }
    report["guidance"] = _guidance(root, now)
    governance, failure = None, None
    try:
        from mesoforge.application.governance import configured_governance

        governance = configured_governance()
        scopes = governance.status()["scopes"]
        report["policies"] = [
            {
                "family": row["family"],
                "scope_key": row["scope_key"],
                "active_policy_artifact_id": row["active_policy_artifact_id"],
                "registered_candidates": len(row["candidates"]),
            }
            for row in scopes
        ]
    except Exception as exc:
        governance, failure = None, exc
        report["policies"] = {"status": "unavailable", "error": _error(exc)}
    readiness = _baseline_readiness(root / "baseline", locations, now=now, governance=governance)
    if failure is not None:
        mark_governance_unavailable(readiness, failure)
    readiness.pop("pointer", None)
    report["baseline"] = readiness
    report["last_forecast_run"] = read_json(status_directory(root) / FORECAST_STATUS)
    _section(report, "latest_issuances", lambda: _latest_issuances(locations))
    report["next_run"] = ForecastSchedule.from_environment().describe(now)
    return report


def render_status(report: dict[str, Any]) -> str:
    worker = report["guidance_worker"]
    health = worker["health"]
    guidance = report.get("guidance") or {}
    readiness = report["baseline"]
    baseline = readiness.get("baseline") or {}
    schema = report.get("schema") or {}
    lines = [
        f"MesoForge status at {report['checked_at']}",
        f"Schema: {schema.get('current')} (head {schema.get('head')}, "
        f"at head: {schema.get('at_head')})",
        f"Guidance worker: {'healthy' if health['healthy'] else 'NOT healthy'} "
        f"({health.get('reason')}); state {worker.get('state')}; "
        f"code {str(worker.get('code_revision'))[:12]}",
        f"Guidance: {guidance.get('contributor_state_id', 'none')} "
        f"(reference {guidance.get('reference_time')}, covers current hour: "
        f"{guidance.get('covers_current_hour')})",
        f"Contributors: {json.dumps(guidance.get('contributor_cycles', {}))}",
        f"Baseline: {baseline.get('baseline_snapshot_id', 'none')} built "
        f"{baseline.get('built_at')} published {baseline.get('published_at')}; "
        f"ready: {readiness['ready']}"
        + (f" ({', '.join(readiness['reasons'])})" if readiness["reasons"] else ""),
        f"Blend governance: {baseline.get('blend_governance')}; governed blend policies: "
        f"{json.dumps(baseline.get('governed_blend_policies', {}))}",
    ]
    policies = report.get("policies")
    if isinstance(policies, list):
        active = [row for row in policies if row["active_policy_artifact_id"]]
        lines.append(f"Governed scopes: {len(policies)}; active policies: {len(active)}")
    last_poll = worker.get("last_poll") or {}
    if last_poll.get("categories"):
        lines.append(f"Last poll issues: {', '.join(last_poll['categories'])}")
    for label in ("last_refresh", "last_build"):
        row = worker.get(label) or {}
        if row:
            lines.append(
                f"{label.replace('_', ' ').capitalize()}: "
                f"{row.get('status') or row.get('outcome')} at {row.get('at')}"
                + (f" ({row.get('category')}: {row.get('error')})" if row.get("category") else "")
            )
    run = report.get("last_forecast_run") or {}
    if run:
        lines.append(
            f"Last forecast run: {run.get('status')} (exit {run.get('exit_code')}) "
            f"at {run.get('started_at')}"
        )
        for row in run.get("results", []):
            desk = row.get("ai_desk") or {}
            label = row["location"].get("name") or row["location"].get("id")
            lines.append(
                f"  {label}: {row.get('status')}"
                + (
                    f"; AI {desk.get('completion_reason')}, {desk.get('accepted_edits')} edits"
                    f", {desk.get('provider_calls')} calls"
                    if desk
                    else ""
                )
            )
    issuances = report.get("latest_issuances")
    if isinstance(issuances, list):
        for row in issuances:
            lines.append(
                f"Latest issuance {row['location']}: {row['issued_forecast_id']} "
                f"(reference {row['target_reference_time']})"
            )
    lines.append(f"Next scheduled run: {report['next_run']['next_run_local']}")
    return "\n".join(lines)


def _default_root() -> Path | None:
    value = os.environ.get("MESOFORGE_PROSPECTIVE_ROOT")
    return Path(value) if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "baseline", "issuances"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=_default_root())
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name == "status":
            command.add_argument("--json", action="store_true")
    commands.add_parser("migration-status")
    migrate_command = commands.add_parser("migrate", help="Explicit upgrade to repository head")
    migrate_command.add_argument(
        "--expect-database", required=True, help="Database name the migration DSN must name"
    )
    grants = commands.add_parser("apply-grants", help="Re-apply least-privilege worker grants")
    grants.add_argument("--expect-database", required=True)
    commands.add_parser("init-storage", help="Explicitly create the configured bucket")
    commands.add_parser("check-empty-storage", help="Read-only empty restore-target check")
    export = commands.add_parser("export-objects")
    export.add_argument("--destination", type=Path, required=True)
    restore = commands.add_parser("import-objects")
    restore.add_argument("--source", type=Path, required=True)
    validate = commands.add_parser("validate-export", help="Read-only complete export validation")
    validate.add_argument("--source", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "migration-status":
            result = migration_status()
            print(json.dumps(result, indent=2))
            return 0 if result["at_head"] else 1
        if args.command == "migrate":
            print(json.dumps(migrate(args.expect_database), indent=2))
            return 0
        if args.command == "apply-grants":
            print(json.dumps(apply_grants(args.expect_database), indent=2))
            return 0
        if args.command == "init-storage":
            print(json.dumps(init_storage(), indent=2))
            return 0
        if args.command == "check-empty-storage":
            print(json.dumps(check_empty_storage(), indent=2))
            return 0
        if args.command == "validate-export":
            print(json.dumps(validate_export(args.source), indent=2))
            return 0
        if args.command == "export-objects":
            print(json.dumps(export_objects(args.destination), indent=2))
            return 0
        if args.command == "import-objects":
            print(json.dumps(import_objects(args.source), indent=2))
            return 0
        if args.root is None:
            parser.error("--root or MESOFORGE_PROSPECTIVE_ROOT is required")
        if args.command == "issuances":
            rows = _latest_issuances(load_locations(args.config))
            print(json.dumps(rows, indent=2))
            return 0
        if args.command == "baseline":
            governance, failure = None, None
            try:
                from mesoforge.application.governance import configured_governance

                governance = configured_governance()
            except Exception as exc:
                failure = exc
            report = _baseline_readiness(
                args.root.resolve() / "baseline",
                load_locations(args.config),
                now=datetime.now(UTC),
                governance=governance,
            )
            if failure is not None:
                mark_governance_unavailable(report, failure)
            report.pop("pointer", None)
            print(json.dumps(report, indent=2, default=str))
            return 0 if report["ready"] else 3
        report = status(args.root, args.config)
        print(json.dumps(report, indent=2, default=str) if args.json else render_status(report))
        return 0 if report["guidance_worker"]["health"]["healthy"] else 1
    except Exception as exc:
        print(json.dumps({"error": _error(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
