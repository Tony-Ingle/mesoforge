"""Conservative operator retention for the hosted guidance collection.

Cycle counts are working-set preferences, not authorization to break immutable
lineage. Both completed and failed generations remain protected until permanent
artifact reference closure can be proved. A failed refresh may already contain
prepared data consumed by an external development/replay artifact. This module
therefore inventories and pins cases but deliberately offers no destructive cleanup
or claim of bounded steady-state scientific history.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesoforge.application.baseline_snapshot import BASELINE_SCHEMA, read_artifact
from mesoforge.application.guidance_worker import single_writer
from mesoforge.application.prepared_snapshot import SNAPSHOT_SCHEMA, _replace_pointer

_ROOT = Path(__file__).resolve().parents[3]
_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
_PINS = "guidance-retention-pins.json"
_PINS_SCHEMA = "mesoforge.guidance-retention-pins.v1"
_REFUSAL = (
    "Retention refused: immutable artifact filesystem dependencies are not fully indexed; "
    "no complete or failed generation can be proven disposable. Nothing was deleted."
)


@dataclass(frozen=True)
class CycleCounts:
    """Distinct native cycles to protect, retaining each bundled generation in full."""

    HRRR: int = 4
    RAP: int = 4
    GFS: int = 3
    IFS: int = 3
    NBM: int = 4

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError("Every retained cycle count must be a positive integer")


DEFAULT_CYCLE_COUNTS = CycleCounts()


def _root(value: Path) -> Path:
    if value.is_symlink() or value.is_junction():
        raise ValueError("Runtime root must not be a link")
    root = value.resolve(strict=True)
    if root.is_relative_to(_ROOT) or root == Path(root.anchor):
        raise ValueError("Retention requires a dedicated runtime root outside the repository")
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
    with single_writer(root) as acquired:
        if not acquired:
            raise RuntimeError("Guidance worker is running; stop it at a safe boundary first")
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


def _cycles(manifest: dict[str, Any], model: str) -> set[str]:
    row = manifest["contributors"].get(model, {})
    candidates = (
        [product.get("cycle") for product in row.get("products", {}).values()]
        if model == "NBM"
        else [row.get("cycle")]
    )
    result = set()
    for value in candidates:
        if value is None:
            continue
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("A retained cycle lacks a timezone")
        result.add(instant.astimezone(UTC).isoformat())
    return result


def plan_retention(root: Path, *, counts: CycleCounts = DEFAULT_CYCLE_COUNTS) -> dict[str, Any]:
    """Read-only inventory; malformed/unknown state fails closed instead of pruning."""
    root = _root(root)
    collection = root / "guidance/snapshots"
    generations: dict[str, dict[str, Any]] = {}
    documents: list[tuple[str | None, Any]] = []
    problems: list[str] = []
    pins = _pins(root)["pins"]
    for directory in sorted(collection.iterdir()):
        if not directory.is_dir() or not _ID.fullmatch(directory.name):
            problems.append(f"Unrecognized entry retained: {directory.name}")
            continue
        files = _files(directory)
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
            row["reasons"].append("permanent_artifact_reference_closure_unproven")
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
        # Even a failed generation can refer to another generation. Scan JSON
        # manifests, not multi-GB native arrays or compressed numerical grids.
        for path in files:
            if path.suffix == ".json":
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
        pointer = _read(pointer_path)
        protect(pointer.get("snapshot_id"), "latest_prepared")
        protect(pointer.get("previous_snapshot_id"), "previous_prepared_recovery")
        documents.append((None, pointer))
    baseline_pointer = root / "baseline/latest_baseline.json"
    current = _read(baseline_pointer) if baseline_pointer.exists() else {}
    baseline_collection = root / "baseline/baselines"
    for directory in sorted(baseline_collection.iterdir()):
        if not directory.is_dir():
            problems.append(f"Unrecognized baseline entry retained: {directory.name}")
            continue
        _files(directory)  # Validate paths without inflating numerical grids.
        path = directory / "baseline.json"
        if not path.exists():
            problems.append(f"Incomplete baseline retained: {directory.name}")
            continue
        baseline = _read(path)
        if baseline.get("schema_version") != BASELINE_SCHEMA:
            raise ValueError("Unknown baseline schema prevents retention")
        identity = baseline["prepared_snapshot"]["snapshot_id"]
        protect(identity, "retained_baseline_dependency")
        for key, reason in (
            ("baseline_snapshot_id", "latest_baseline_dependency"),
            ("previous_baseline_snapshot_id", "previous_baseline_recovery_dependency"),
        ):
            if current.get(key) == baseline.get("baseline_snapshot_id"):
                protect(identity, reason)
        documents.append((None, baseline))
        tables = read_artifact(directory, baseline["metadata_file"])
        documents.append((None, {"source_documents": tables["source_documents"]}))

    # Every explicit local generation dependency is protected, even from an
    # otherwise failed or incomplete generation. IDs in lineage are references too.
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
        row["action"] = "retain"
    rows = list(generations.values())
    return {
        "operation": "dry_run",
        "root": str(root),
        "cycle_counts": asdict(counts),
        "generations": rows,
        "problems": sorted(set(problems)),
        "retained_bytes": sum(row["bytes"] for row in rows if row["action"] == "retain"),
        "removable_bytes": 0,
        "expired_preference_but_protected_bytes": sum(
            row["bytes"] for row in rows if row["outside_cycle_window"]
        ),
        "bounded_complete_history": False,
        "deletion_supported": False,
        "limitation": "Complete and failed generations remain protected: permanent artifact "
        "references, including external development/replay consumers, are not fully indexed. "
        "Baseline readback also requires retained prepared manifests and source documents.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Read-only inventory (the default)")
    group.add_argument(
        "--apply", action="store_true", help="Refused until dependency closure is proven"
    )
    group.add_argument("--pin", metavar="SNAPSHOT_ID")
    group.add_argument("--unpin", metavar="SNAPSHOT_ID")
    parser.add_argument("--reason", default="")
    for model, count in asdict(CycleCounts()).items():
        parser.add_argument(f"--keep-{model.lower()}", type=int, default=count)
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
        report = plan_retention(args.runtime_root, counts=counts)
        print(json.dumps(report, indent=2, allow_nan=False))
        if args.apply:
            print(_REFUSAL, file=sys.stderr)
            return 2
        return 0
    except Exception as exc:
        print(
            f"Retention refused ({type(exc).__name__}): inventory or pin operation failed; "
            "no weather data was deleted.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
