"""Expire superseded model payloads while preserving durable forecast evidence.

Only known GRIB/index/NetCDF payloads in complete, unprotected generations may
expire after a matching verified recovery copy. Every JSON document and baseline
stays immutable: baseline readback uses these documents, not native arrays. Full
native re-preparation of expired generations is intentionally unavailable; a case
requiring that capability must be pinned. Failed/unknown bundles remain protected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mesoforge.application.baseline_snapshot import BASELINE_SCHEMA, read_artifact
from mesoforge.application.prepared_snapshot import SNAPSHOT_SCHEMA, _replace_pointer
from mesoforge.application.worker_lock import single_writer, worker_lock_root
from mesoforge.application.worker_status import FORECAST_STATUS, GUIDANCE_STATUS

_ROOT = Path(__file__).resolve().parents[3]
_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
_PINS = "guidance-retention-pins.json"
_PINS_SCHEMA = "mesoforge.guidance-retention-pins.v1"
PLAN_SCHEMA = "mesoforge.guidance-retention-plan.v1"
TRANSACTION_SCHEMA = "mesoforge.guidance-retention-transaction.v1"
BACKUP_RECEIPT_SCHEMA = "mesoforge.local-backup-receipt.v1"
_PAYLOAD_SUFFIXES = frozenset({".grib2", ".grib", ".grb2", ".nc", ".idx", ".index"})
_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class CycleCounts:
    """Distinct native cycles to protect, retaining each bundled generation in full."""

    HRRR: int = 4
    RAP: int = 4
    GFS: int = 3
    IFS: int = 3
    NBM: int = 4
    GEFS: int = 3
    ECMWF_ENS: int = 3

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError("Every retained cycle count must be a positive integer")


DEFAULT_CYCLE_COUNTS = CycleCounts()


def _root(value: Path) -> Path:
    if value.is_symlink() or value.is_junction():
        raise ValueError("Runtime root must not be a link")
    root = value.resolve(strict=True)
    if value.absolute() != root:
        raise ValueError("Runtime root must not traverse a filesystem link or parent escape")
    if root.is_relative_to(_ROOT) or root == Path(root.anchor):
        raise ValueError("Retention requires a dedicated runtime root outside the repository")
    if any(
        part.lower() in {"etc", "_work", "actions-runner", "postgres", "minio"}
        or part.lower().startswith("actions.runner.")
        for part in root.parts
    ):
        raise ValueError("Retention cannot target infrastructure or runner directories")
    # Only the hosted layout is supported. Arbitrary caches and runner directories
    # are never inferred from a path passed by the operator.
    for name in ("guidance/snapshots", "baseline/baselines"):
        path = root / name
        if not path.is_dir() or path.resolve() != path:
            raise ValueError(f"Missing or linked hosted collection: {name}")
    status = root / "status"
    if status.exists() and status.resolve() != status:
        raise ValueError("Hosted status directory must not be a link")
    return root


def _files(directory: Path) -> list[Path]:
    """Reject links, junctions and special files before traversing/deleting anything."""
    result: list[Path] = []
    pending = [directory]
    while pending:
        parent = pending.pop()
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("Retention does not traverse filesystem links")
        for path in sorted(parent.iterdir()):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Retention does not traverse filesystem links")
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                result.append(path)
            else:
                raise ValueError("Retention does not traverse special files")
    return result


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.is_junction():
        raise ValueError("Retained metadata must not be a link")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Expected retained JSON object: {path.name}")
    return value


def _pins(root: Path) -> dict[str, Any]:
    path = root / _PINS
    if not path.exists():
        return {"schema_version": _PINS_SCHEMA, "pins": {}}
    if path.is_symlink() or path.is_junction():
        raise ValueError("Retention pins must not be a link")
    value = _read(path)
    if value.get("schema_version") != _PINS_SCHEMA or not isinstance(value.get("pins"), dict):
        raise ValueError("Unsupported retention pin registry")
    return value


def pin_case(root: Path, generation_name: str, *, reason: str, remove: bool = False) -> None:
    """Pin one whole dependency bundle; no inference about scientific significance."""
    root = _root(root)
    if not _ID.fullmatch(generation_name) or (not remove and not reason.strip()):
        raise ValueError("A managed snapshot ID and an operator reason are required")
    if not (root / "guidance/snapshots" / generation_name).is_dir():
        raise ValueError("The managed snapshot does not exist")
    with single_writer(worker_lock_root(root)) as acquired:
        if not acquired:
            raise RuntimeError(
                "Guidance worker or Forecast worker is running; wait for a safe boundary first"
            )
        if not remove and generation_name in _expiration_records(root):
            raise ValueError(
                "Case native payloads expired; restore the recovery copy before pinning"
            )
        value = _pins(root)
        if remove:
            value["pins"].pop(generation_name, None)
        else:
            value["pins"][generation_name] = {
                "reason": reason,
                "pinned_at": datetime.now(UTC).isoformat(),
            }
        _replace_pointer(root, _PINS, value)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Retention timestamps require a timezone")
    return result.astimezone(UTC)


def _payload(path: Path) -> bool:
    return path.suffix in _PAYLOAD_SUFFIXES or path.name.endswith(".grib2.gz")


def _expiration_records(root: Path) -> dict[str, int]:
    """The retained audit distinguishes absent native cache from available evidence."""
    directory = root / "retention/transactions"
    if not directory.exists():
        return {}
    if directory.resolve() != directory:
        raise ValueError("Retention audit directory must not be linked")
    result: dict[str, int] = {}
    seen: set[str] = set()
    for path in _files(directory):
        if path.suffix != ".json":
            continue  # A crash may leave an atomic-write temp; never prune it here.
        transaction = _read(path)
        plan = transaction["plan"]
        if (
            transaction.get("schema_version") != TRANSACTION_SCHEMA
            or plan["root"] != str(root)
            or plan["plan_sha256"] != path.stem
            or _json_digest({k: v for k, v in plan.items() if k != "plan_sha256"})
            != plan["plan_sha256"]
        ):
            raise ValueError("Invalid retained native-payload expiry audit")
        for item in plan["candidates"]:
            candidate = _candidate_path(root, item)
            deleted = item["path"] in transaction["deleted"]
            interrupted = item["path"] == transaction["active_path"] and not candidate.exists()
            if candidate.exists() and deleted:
                if (
                    candidate.stat().st_size != item["bytes"]
                    or _digest(candidate) != item["sha256"]
                ):
                    raise ValueError(
                        "Previously expired native payload was replaced with new bytes"
                    )
                continue  # An exact operator restore can be pinned as a complete case again.
            if (deleted or interrupted) and item["path"] not in seen:
                result[item["snapshot_id"]] = result.get(item["snapshot_id"], 0) + item["bytes"]
                seen.add(item["path"])
    return result


def _cycles(manifest: dict[str, Any], model: str) -> set[str]:
    row = manifest["contributors"].get(model, {})
    rows = [row]
    if model == "NBM":
        rows.extend(row.get("products", {}).values())
    candidates = [
        row.get("cycle")
        for row in rows
        if row.get("status", "complete") in {"complete", "retained", "prepared"}
        and row.get("valid_times", [True])
    ]
    if model in {"GEFS", "ECMWF_ENS"}:
        source_id = "GEFS_6H" if model == "GEFS" else "ECMWF_ENS_24H"
        for descriptor in manifest.get("evidence", {}).get("probability_sources", []):
            if descriptor["source_id"] != source_id or descriptor.get("status") != "prepared":
                continue
            path = Path(descriptor["directory"]) / "manifest.json"
            if path.resolve() != path or _digest(path) != descriptor["manifest_sha256"]:
                raise ValueError("Probability source manifest is linked or changed")
            source = _read(path)
            if source["source_id"] != source_id:
                raise ValueError("Probability source identity mismatch")
            candidates.extend(event["source_cycle"] for event in source["events"])
    result = set()
    for value in candidates:
        if value is None:
            continue
        result.add(_instant(value).isoformat())
    return result


def plan_retention(root: Path, *, counts: CycleCounts = DEFAULT_CYCLE_COUNTS) -> dict[str, Any]:
    """Read-only inventory; malformed/unknown state fails closed instead of pruning."""
    root = _root(root)
    collection = root / "guidance/snapshots"
    generations: dict[str, dict[str, Any]] = {}
    baselines: list[dict[str, Any]] = []
    documents: list[tuple[str | None, Any]] = []
    metadata: dict[str, str] = {}
    payloads: dict[str, list[Path]] = {}
    problems: list[str] = []
    pins = _pins(root)["pins"]
    expired = _expiration_records(root)
    if (root / _PINS).exists():
        metadata[_PINS] = _digest(root / _PINS)
    # Known worker documents only: never traverse another application's or a
    # runner's state. In-flight status is conservative even after an interruption.
    in_flight_workers: list[str] = []
    for name in (GUIDANCE_STATUS, FORECAST_STATUS):
        path = root / "status" / name
        if path.exists():
            status = _read(path)
            if status.get("in_flight") or status.get("state") == "busy":
                in_flight_workers.append(name)
                documents.append((None, status))
    for directory in sorted(collection.iterdir()):
        if not directory.is_dir() or not _ID.fullmatch(directory.name):
            problems.append(f"Unrecognized entry retained: {directory.name}")
            continue
        files = _files(directory)
        payloads[directory.name] = [path for path in files if _payload(path)]
        row: dict[str, Any] = {
            "snapshot_id": directory.name,
            "bytes": sum(path.stat().st_size for path in files),
            "reasons": ["operator_case_pin"] if directory.name in pins else [],
            "cycles": {},
            "state": "incomplete_or_unknown",
        }
        generations[directory.name] = row
        manifest_path = directory / "snapshot.json"
        if manifest_path.exists():
            manifest = _read(manifest_path)
            if (
                manifest.get("schema_version") != SNAPSHOT_SCHEMA
                or manifest.get("snapshot_id") != directory.name
            ):
                raise ValueError("Unsupported prepared snapshot identity")
            row["state"] = "complete"
            row["cycles"] = {model: sorted(_cycles(manifest, model)) for model in asdict(counts)}
        elif (directory / "failure.json").exists():
            failure = _read(directory / "failure.json")
            result = (
                _read(directory / "result.json") if (directory / "result.json").exists() else {}
            )
            if failure.get("status") == "failed" and result.get("status", "failed") == "failed":
                row["state"] = "terminal_failed"
                row["reasons"].append("permanent_artifact_reference_closure_unproven")
        if row["state"] == "incomplete_or_unknown":
            row["reasons"].append("incomplete_or_unknown_never_age_pruned")
        if any(path.suffix != ".json" and not _payload(path) for path in files):
            row["reasons"].append("unknown_file_contract")
        # Even a failed generation can refer to another generation. Scan JSON
        # manifests, not multi-GB native arrays or compressed numerical grids.
        for path in files:
            if path.suffix == ".json":
                metadata[path.relative_to(root).as_posix()] = _digest(path)
                try:
                    documents.append((directory.name, json.loads(path.read_bytes())))
                except (ValueError, OSError) as exc:
                    problems.append(
                        f"Unreadable metadata retained: {path.name}: {type(exc).__name__}"
                    )

    def protect(identity: Any, reason: str) -> None:
        if identity in generations:
            generations[identity]["reasons"].append(reason)
        elif identity is not None:
            problems.append(f"Referenced generation is absent: {identity}")

    pointer_path = root / "guidance/latest_complete.json"
    if pointer_path.exists():
        metadata["guidance/latest_complete.json"] = _digest(pointer_path)
        pointer = _read(pointer_path)
        protect(pointer.get("snapshot_id"), "latest_prepared")
        protect(pointer.get("previous_snapshot_id"), "previous_prepared_recovery")
    baseline_pointer = root / "baseline/latest_baseline.json"
    current = _read(baseline_pointer) if baseline_pointer.exists() else {}
    if baseline_pointer.exists():
        metadata["baseline/latest_baseline.json"] = _digest(baseline_pointer)
    baseline_collection = root / "baseline/baselines"
    for directory in sorted(baseline_collection.iterdir()):
        if not directory.is_dir():
            problems.append(f"Unrecognized baseline entry retained: {directory.name}")
            continue
        files = _files(directory)  # Validate paths without inflating numerical grids.
        row = {
            "baseline_snapshot_id": directory.name,
            "prepared_snapshot_id": None,
            "bytes": sum(path.stat().st_size for path in files),
            "state": "incomplete_or_unknown",
            "reasons": ["permanent_artifact_reference_closure_unproven"],
            "action": "retain",
        }
        baselines.append(row)
        path = directory / "baseline.json"
        if not path.exists():
            row["reasons"].append("incomplete_or_unknown_never_age_pruned")
            problems.append(f"Incomplete baseline retained: {directory.name}")
            continue
        baseline = _read(path)
        if (
            baseline.get("schema_version") != BASELINE_SCHEMA
            or baseline.get("baseline_snapshot_id") != directory.name
        ):
            raise ValueError("Unknown baseline schema or identity prevents retention")
        identity = baseline["prepared_snapshot"]["snapshot_id"]
        row.update(prepared_snapshot_id=identity, state="complete")
        # Historical baseline readers consume JSON source documents and their
        # saved grids. They do not reread GRIB/NetCDF, so their lineage identity
        # alone does not pin the whole native working set forever.
        if identity not in generations:
            problems.append(f"Referenced generation is absent: {identity}")
        for key, reason in (
            ("baseline_snapshot_id", "latest_baseline_dependency"),
            ("previous_baseline_snapshot_id", "previous_baseline_recovery_dependency"),
        ):
            if current.get(key) == baseline.get("baseline_snapshot_id"):
                protect(identity, reason)
                row["reasons"].append(reason.removesuffix("_dependency"))
        if identity in pins:
            row["reasons"].append("operator_case_pin_dependency")
        metadata[path.relative_to(root).as_posix()] = _digest(path)
        tables = read_artifact(directory, baseline["metadata_file"])
        metadata[f"{directory.relative_to(root).as_posix()}/source_documents"] = _json_digest(
            tables["source_documents"]
        )
        for descriptor in tables["source_documents"]:
            source = Path(descriptor["path"])
            if (
                source.suffix != ".json"
                or source.resolve() != source
                or not source.is_file()
                or _digest(source) != str(descriptor.get("sha256", "")).removeprefix("sha256:")
            ):
                problems.append("Unresolved baseline source document dependency")
                documents.append((None, descriptor))

    baseline_ids = {row["baseline_snapshot_id"] for row in baselines}
    for key in ("baseline_snapshot_id", "previous_baseline_snapshot_id"):
        if current.get(key) is not None and current[key] not in baseline_ids:
            problems.append("Baseline pointer references an absent generation")

    # Cross-generation references may support retained native re-preparation.
    # Keep those bundles. Self-owned raw paths remain immutable provenance after
    # native payload expiration; every corresponding JSON document stays here.
    for owner, document in documents:
        for value in _strings(document):
            for identity in generations:
                if identity == owner:
                    continue
                directory = collection / identity
                if (
                    value == identity
                    or value == str(directory)
                    or value.startswith(str(directory) + "/")
                    or value.startswith(str(directory) + "\\")
                    or identity in value.replace("\\", "/").split("/")
                ):
                    protect(identity, "retained_local_metadata_dependency")
    for model, limit in asdict(counts).items():
        selected = sorted(
            {cycle for row in generations.values() for cycle in row["cycles"].get(model, [])},
            reverse=True,
        )[:limit]
        for row in generations.values():
            if set(selected).intersection(row["cycles"].get(model, [])):
                row["reasons"].append(f"recent_{model}_cycle_window")
    for row in generations.values():
        if problems:
            row["reasons"].append("local_reference_inventory_incomplete")
        row["reasons"] = sorted(set(row["reasons"]))
        row["outside_cycle_window"] = row["state"] == "complete" and not any(
            reason.startswith("recent_") for reason in row["reasons"]
        )
        if in_flight_workers:
            row["reasons"].append("worker_in_flight_reference_closure_unproven")
        row["action"] = "expire_payloads" if not row["reasons"] else "retain"
    rows = list(generations.values())
    for row in [*rows, *baselines]:
        if in_flight_workers:
            row["reasons"].append("worker_in_flight_reference_closure_unproven")
        row["reasons"] = sorted(set(row["reasons"]))
    candidates = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _digest(path),
            "snapshot_id": row["snapshot_id"],
        }
        for row in rows
        if row["action"] == "expire_payloads"
        for path in sorted(payloads[row["snapshot_id"]])
    ]
    for row in rows:
        row["removable_bytes"] = sum(
            entry["bytes"] for entry in candidates if entry["snapshot_id"] == row["snapshot_id"]
        )
        row["labels"] = _labels(row)
        row["expired_payload_bytes"] = expired.get(row["snapshot_id"], 0)
        row["native_replay"] = (
            "unavailable_after_retention"
            if row["expired_payload_bytes"]
            else "expires_on_apply"
            if row["removable_bytes"]
            else "retained"
        )
    for row in baselines:
        row["removable_bytes"] = 0
        row["labels"] = _labels(row)
    report = {
        "schema_version": PLAN_SCHEMA,
        "operation": "dry_run",
        "root": str(root),
        "runtime_root": str(root),
        "cycle_counts": asdict(counts),
        "generations": rows,
        "baselines": baselines,
        "in_flight_workers": in_flight_workers,
        "problems": sorted(set(problems)),
        "retained_bytes": sum(row["bytes"] - row["removable_bytes"] for row in rows),
        "retained_baseline_bytes": sum(row["bytes"] for row in baselines),
        "removable_bytes": sum(entry["bytes"] for entry in candidates),
        "candidates": candidates,
        "protection_state_sha256": _json_digest(
            {"metadata": metadata, "in_flight": in_flight_workers, "cycle_counts": asdict(counts)}
        ),
        "expired_preference_but_protected_bytes": sum(
            row["bytes"]
            for row in rows
            if row["outside_cycle_window"] and row["action"] == "retain"
        ),
        "bounded_complete_history": False,
        "deletion_supported": True,
        "limitation": "Only known superseded raw/prepared payloads expire. JSON provenance and "
        "all baselines remain for exact historical forecast/AI replay; baseline history is not "
        "bounded. Native re-preparation of expired inputs requires the verified recovery copy. "
        "Pin research cases before retention. Failed/unknown bundles and unresolved cross-"
        "generation dependencies cannot be pruned by this contract.",
    }
    report["plan_sha256"] = _json_digest(report)
    return report


def _labels(row: dict[str, Any]) -> list[str]:
    reasons = row["reasons"]
    result = ["DELETE" if row["removable_bytes"] else "KEEP"]
    for prefix, label in (
        ("operator_case_pin", "PINNED"),
        ("latest_", "CURRENT"),
        ("previous_", "RECOVERY"),
        ("worker_in_flight", "IN-FLIGHT"),
    ):
        if any(reason.startswith(prefix) for reason in reasons):
            result.append(label)
    if any(
        token in reason
        for reason in reasons
        for token in ("unproven", "unknown", "incomplete", "metadata_dependency")
    ):
        result.append("UNRESOLVED")
    return result


def _candidate_path(root: Path, item: dict[str, Any]) -> Path:
    name = item["path"]
    relative = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or "\\" in name
        or ":" in name
        or relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) < 4
        or relative.parts[:2] != ("guidance", "snapshots")
        or not _ID.fullmatch(relative.parts[2])
        or item["snapshot_id"] != relative.parts[2]
    ):
        raise ValueError("Deletion path is not a managed native payload")
    path = root.joinpath(*relative.parts)
    if not _payload(path) or path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Deletion path escaped the runtime or native payload contract")
    if path.is_symlink() or path.is_junction():
        raise ValueError("Deletion path must not be linked")
    return path


def _backup_receipt(path: Path, root: Path, *, now: datetime) -> dict[str, Any]:
    receipt = _read(path)
    if (
        receipt.get("schema_version") != BACKUP_RECEIPT_SCHEMA
        or receipt.get("status") != "verified"
        or receipt.get("runtime_root") != str(root)
        or not receipt.get("backup_id")
        or not _HEX.fullmatch(str(receipt.get("manifest_sha256", "")))
        or not _HEX.fullmatch(str(receipt.get("retention_plan_sha256", "")))
        or _instant(receipt["verified_at"]) > now
    ):
        raise ValueError("Retention requires a verified backup covering this runtime and plan")
    return receipt


def apply_retention(
    root: Path,
    *,
    backup_receipt: Path,
    counts: CycleCounts = DEFAULT_CYCLE_COUNTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Unlink only the backed-up exact plan under the shared worker OS lock.

    An append-in-place transaction status uses atomic replacement and an explicit
    pending unlink. Resume validates the same protection graph and each remaining
    digest. A crash never grants permission to delete newly created payloads.
    """
    root = _root(root)
    checked_at = _instant((now or datetime.now(UTC)).isoformat())
    receipt = _backup_receipt(backup_receipt, root, now=checked_at)
    with single_writer(worker_lock_root(root)) as acquired:
        if not acquired:
            raise RuntimeError("Guidance or Forecast work is in flight; retention refused")
        plan = plan_retention(root, counts=counts)
        transaction_root = root / "retention/transactions"
        if transaction_root.resolve() != transaction_root:
            raise ValueError("Retention audit directory must not be linked")
        transaction_path = transaction_root / f"{receipt['retention_plan_sha256']}.json"
        if transaction_path.exists():
            transaction = _read(transaction_path)
            original = transaction["plan"]
            if (
                transaction.get("schema_version") != TRANSACTION_SCHEMA
                or original["plan_sha256"] != receipt["retention_plan_sha256"]
                or _json_digest({k: v for k, v in original.items() if k != "plan_sha256"})
                != original["plan_sha256"]
                or transaction["backup_manifest_sha256"] != receipt["manifest_sha256"]
                or original["root"] != str(root)
            ):
                raise ValueError("Retention transaction differs from its verified recovery copy")
            if transaction["status"] == "complete":
                return {**transaction, "operation": "already_applied"}
        else:
            if plan["plan_sha256"] != receipt["retention_plan_sha256"]:
                raise ValueError("Runtime changed since backup; create and verify a fresh plan")
            original = plan
            transaction = {
                "schema_version": TRANSACTION_SCHEMA,
                "operation": "apply",
                "status": "in_progress",
                "created_at": checked_at.isoformat(),
                "backup_id": receipt["backup_id"],
                "backup_manifest_sha256": receipt["manifest_sha256"],
                "plan": original,
                "deleted": [],
                "active_path": None,
                "native_replay": "Expired sources require the verified recovery copy; "
                "saved baseline/issuance and immutable JSON provenance remain unchanged.",
            }
        if (
            original["protection_state_sha256"] != plan["protection_state_sha256"]
            or plan["problems"]
            or plan["in_flight_workers"]
        ):
            raise ValueError("Protected dependencies changed or are unresolved; retention refused")
        eligible = {item["path"]: item for item in plan["candidates"]}
        # Preflight the complete remaining set before making another unlink.
        for item in original["candidates"]:
            path = _candidate_path(root, item)
            if item["path"] in transaction["deleted"]:
                if path.exists():
                    raise ValueError("An expired payload reappeared; a fresh plan is required")
                continue
            if not path.exists() and transaction["active_path"] == item["path"]:
                transaction["deleted"].append(item["path"])
                transaction["active_path"] = None
                continue
            if (
                eligible.get(item["path"]) != item
                or not path.is_file()
                or path.stat().st_size != item["bytes"]
                or _digest(path) != item["sha256"]
            ):
                raise ValueError("Native payload changed or became protected; retention refused")
        transaction_root.mkdir(parents=True, exist_ok=True)

        def record() -> None:
            _replace_pointer(transaction_root, transaction_path.name, transaction)

        record()
        for item in original["candidates"]:
            if item["path"] in transaction["deleted"]:
                continue
            path = _candidate_path(root, item)
            if path.stat().st_size != item["bytes"] or _digest(path) != item["sha256"]:
                raise ValueError("Native payload changed during retention")
            transaction["active_path"] = item["path"]
            record()
            path.unlink()
            transaction["deleted"].append(item["path"])
            transaction["active_path"] = None
            record()
        transaction["status"] = "complete"
        transaction["completed_at"] = datetime.now(UTC).isoformat()
        transaction["deleted_bytes"] = sum(item["bytes"] for item in original["candidates"])
        record()
        return transaction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Read-only inventory (the default)")
    group.add_argument("--status", action="store_true", help="Alias for read-only inventory")
    group.add_argument("--apply", action="store_true", help="Apply the exact verified backup plan")
    group.add_argument("--pin", metavar="SNAPSHOT_ID")
    group.add_argument("--unpin", metavar="SNAPSHOT_ID")
    parser.add_argument("--reason", default="")
    parser.add_argument("--backup-receipt", type=Path)
    for model, count in asdict(CycleCounts()).items():
        parser.add_argument(f"--keep-{model.lower().replace('_', '-')}", type=int, default=count)
    args = parser.parse_args(argv)
    try:
        counts = CycleCounts(
            **{model: getattr(args, f"keep_{model.lower()}") for model in asdict(CycleCounts())}
        )
        if args.pin or args.unpin:
            pin_case(
                args.runtime_root,
                args.pin or args.unpin,
                reason=args.reason,
                remove=bool(args.unpin),
            )
        if args.apply:
            if args.backup_receipt is None:
                raise ValueError("--apply requires --backup-receipt")
            report = apply_retention(
                args.runtime_root, counts=counts, backup_receipt=args.backup_receipt
            )
        else:
            report = plan_retention(args.runtime_root, counts=counts)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        print(
            f"Retention refused ({type(exc).__name__}); inspect the retained transaction "
            "before retrying. No unplanned payload is eligible for deletion.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
