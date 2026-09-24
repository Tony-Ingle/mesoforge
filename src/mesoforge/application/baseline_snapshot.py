"""Immutable, configured-domain MesoForge baselines and their independent pointer.

The artifact stores already calculated grids. Its reader only verifies, restores
metadata references and extracts the saved center; it never loads model arrays.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mesoforge.application.baseline_codec import CompactCodec
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    _publication_lock,
    _replace_pointer,
    _sha256_file,
    check_information_cutoff,
)
from mesoforge.application.spatial_coverage import CoverageRequiredError, validate_coordinate
from mesoforge.contracts.serialization import canonical_json_bytes

BASELINE_SCHEMA = "mesoforge.baseline-snapshot.v1"
POINTER_SCHEMA = "mesoforge.latest-baseline-pointer.v1"
POINTER_FILE = "latest_baseline.json"
MANIFEST_FILE = "baseline.json"
BASELINES_DIRECTORY = "baselines"


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise SnapshotError("Baseline timestamps must include a timezone")
    return instant


def _artifact_path(directory: Path, descriptor: dict[str, Any]) -> Path:
    path = (directory / str(descriptor["file"])).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise SnapshotError("Baseline artifact escapes its immutable directory")
    return path


def read_artifact(directory: Path, descriptor: dict[str, Any]) -> Any:
    path = _artifact_path(directory, descriptor)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
        raise SnapshotError(f"Baseline artifact digest differs: {path.name}")
    if descriptor.get("encoding") == "json+gzip":
        payload = gzip.decompress(payload)
    return json.loads(payload)


def write_artifact(directory: Path, filename: str, value: Any) -> dict[str, Any]:
    """Exclusive creation: completed or partially written files are never replaced."""
    raw = canonical_json_bytes(value)
    payload = gzip.compress(raw, compresslevel=6, mtime=0)
    with (directory / filename).open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "file": filename,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "uncompressed_bytes": len(raw),
        "encoding": "json+gzip",
    }


def write_manifest(directory: Path, manifest: dict[str, Any]) -> tuple[Path, str]:
    payload = canonical_json_bytes(manifest)
    path = directory / MANIFEST_FILE
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path, hashlib.sha256(payload).hexdigest()


def _order(manifest: dict[str, Any]) -> tuple[datetime, ...]:
    prepared = manifest["prepared_snapshot"]
    # Preserve the prepared reference ordering, then distinguish newer contributor
    # publications at the same reference and later analysis of the same state.
    return tuple(
        _instant(value)
        for value in (
            prepared["coverage"]["reference_time"],
            prepared["published_at"],
            manifest["analysis_cutoff"],
            manifest["built_at"],
        )
    )


def read_pointer(root: Path) -> dict[str, Any] | None:
    path = root / POINTER_FILE
    if not path.exists():
        return None
    pointer: dict[str, Any] = json.loads(path.read_bytes())
    if pointer.get("schema_version") != POINTER_SCHEMA:
        raise SnapshotError("Unsupported latest-baseline pointer")
    return pointer


def _resolve(root: Path, pointer: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    directory = (root / pointer["baseline_directory"]).resolve()
    if not directory.is_relative_to((root / BASELINES_DIRECTORY).resolve()):
        raise SnapshotError("Baseline pointer escapes the baseline collection")
    payload = (directory / MANIFEST_FILE).read_bytes()
    if hashlib.sha256(payload).hexdigest() != pointer["manifest_sha256"]:
        raise SnapshotError("Baseline manifest digest differs from its pointer")
    manifest = json.loads(payload)
    if (
        manifest.get("schema_version") != BASELINE_SCHEMA
        or manifest.get("baseline_snapshot_id") != pointer["baseline_snapshot_id"]
        or manifest.get("completeness", {}).get("status") != "complete"
    ):
        raise SnapshotError("Baseline identity/schema/completeness differs")
    return manifest, directory


def publish_latest_baseline(
    root: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    *,
    published_at: datetime,
) -> dict[str, Any]:
    """Serialize compare-and-publish across processes with the proven OS file lock."""
    candidate = {
        "schema_version": POINTER_SCHEMA,
        "baseline_snapshot_id": manifest["baseline_snapshot_id"],
        "baseline_directory": f"{BASELINES_DIRECTORY}/{manifest['baseline_snapshot_id']}",
        "manifest_sha256": manifest_sha256,
        "published_at": published_at.isoformat(),
    }
    saved, directory = _resolve(root, candidate)
    if canonical_json_bytes(saved) != canonical_json_bytes(manifest):
        raise SnapshotError("Publication manifest differs from the immutable artifact")
    if _instant(candidate["published_at"]) < _instant(manifest["completed_at"]):
        raise SnapshotError("Baseline cannot publish before completion")
    for artifact in manifest["artifacts"]:
        if _sha256_file(_artifact_path(directory, artifact)) != artifact["sha256"]:
            raise SnapshotError("Baseline artifact changed before publication")
    with _publication_lock(root, lock_file=".latest_baseline.lock"):
        current = read_pointer(root)
        if current:
            previous, _ = _resolve(root, current)
            if _order(manifest) < _order(previous):
                raise SnapshotError("Older baseline cannot replace the current baseline")
            if _order(manifest) == _order(previous):
                if manifest["baseline_snapshot_id"] == previous["baseline_snapshot_id"]:
                    return current
                raise SnapshotError("Equal-order competing baseline publication is ambiguous")
        candidate["previous_baseline_snapshot_id"] = (
            current["baseline_snapshot_id"] if current else None
        )
        _replace_pointer(root, POINTER_FILE, candidate)
    return candidate


@dataclass
class PinnedBaseline:
    pointer: dict[str, Any]
    manifest: dict[str, Any]
    directory: Path
    codec: CompactCodec

    def reference_view(self, reference_time: datetime) -> BaselineView:
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise SnapshotError("Reference time must include a timezone")
        if not any(
            _instant(value) == reference_time
            for value in self.manifest["coverage"]["reference_times"]
        ):
            raise SnapshotError(
                "Baseline does not cover this reference hour; rebuild in background"
            )
        return BaselineView(self, reference_time)


@dataclass
class BaselineView:
    pinned: PinnedBaseline
    reference_time: datetime

    def forecast(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        validate_coordinate(latitude, longitude)
        domain = next(
            (
                row
                for row in self.pinned.manifest["domains"]
                if row["latitude"] == latitude
                and row["longitude"] == longitude
                and _instant(row["reference_time"]) == self.reference_time
            ),
            None,
        )
        if domain is None:
            raise CoverageRequiredError(
                "Coordinate has no saved baseline domain; "
                "add it to the background build configuration"
            )
        encoded = read_artifact(self.pinned.directory, domain["artifact"])
        grid = self.pinned.codec.decode(encoded)
        if grid["geometry"] != domain["geometry"]:
            raise SnapshotError("Restored baseline geometry differs from declared coverage")
        forecast = extract_grid_point(grid, latitude=latitude, longitude=longitude, copy_grid=False)
        if forecast["local_grid"]["sha256"] != domain["grid_sha256"]:
            raise SnapshotError("Restored baseline grid differs from its calculated identity")
        return forecast


def load_baseline(root: Path) -> PinnedBaseline:
    """Pin once; only selected domains are inflated, and no native arrays are loaded."""
    pointer = read_pointer(root)
    if pointer is None:
        raise SnapshotError("No latest-baseline snapshot has been published")
    manifest, directory = _resolve(root, pointer)
    prepared = manifest["prepared_snapshot"]
    if _sha256_file(Path(prepared["directory"]) / "snapshot.json") != prepared["manifest_sha256"]:
        raise SnapshotError("Referenced prepared snapshot manifest changed")
    information = read_artifact(
        directory, manifest["information_cutoff"]["source_information_file"]
    )
    problems = check_information_cutoff(
        information,
        analysis_cutoff=_instant(manifest["analysis_cutoff"]),
        published_at=prepared["published_at"],
        completed_at=prepared["completed_at"],
    )
    if problems or manifest["information_cutoff"]["status"] != "proven":
        raise SnapshotError("Baseline source information has unresolved cutoff limitations")
    tables = read_artifact(directory, manifest["metadata_file"])
    return PinnedBaseline(pointer, manifest, directory, CompactCodec.from_tables(tables))
