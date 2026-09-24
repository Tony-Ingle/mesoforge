"""Cross-process publication ordering and failure safety without provider or DB access."""

from __future__ import annotations

import json
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mesoforge.application import prepared_snapshot as snapshots


def _manifest(root: Path, name: str, hour: int) -> tuple[dict[str, Any], str]:
    reference = datetime(2026, 9, 18, hour, tzinfo=UTC)

    def iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    manifest = {
        "schema_version": snapshots.SNAPSHOT_SCHEMA,
        "snapshot_id": name,
        "completeness": {"required_deterministic": True},
        "coverage": {
            "reference_time": iso(reference),
            "first_valid_time": iso(reference + timedelta(hours=1)),
            "last_valid_time": iso(reference + timedelta(hours=36)),
        },
    }
    directory = root / snapshots.SNAPSHOTS_DIRECTORY / name
    directory.mkdir(parents=True)
    _, digest = snapshots.write_manifest(directory, manifest)
    return manifest, digest


def _publish_in_process(
    root: Path,
    manifest: dict[str, Any],
    digest: str,
    attempted: Any,
    read: Any,
    release: Any,
) -> None:
    original_read = snapshots.read_pointer

    def paused_read(path: Path) -> dict[str, Any] | None:
        current = original_read(path)
        read.set()
        if release is not None and not release.wait(30):
            raise AssertionError("Parent did not release publication barrier")
        return current

    attempted.set()
    with patch.object(snapshots, "read_pointer", paused_read):
        try:
            snapshots.publish_latest_complete(
                root, manifest, digest, published_at=datetime(2026, 9, 18, 12, tzinfo=UTC)
            )
        except snapshots.SnapshotError as exc:
            result = {"status": "rejected", "reason": str(exc)}
        else:
            result = {"status": "published"}
    (root / f"{manifest['snapshot_id']}.result.json").write_text(json.dumps(result))


@pytest.mark.parametrize("first_hour,second_hour", [(10, 11), (11, 10)])
def test_separate_process_publishers_cannot_roll_pointer_backwards(
    tmp_path: Path, first_hour: int, second_hour: int
) -> None:
    initial, initial_digest = _manifest(tmp_path, "initial", 9)
    first_manifest, first_digest = _manifest(tmp_path, "first", first_hour)
    second_manifest, second_digest = _manifest(tmp_path, "second", second_hour)
    snapshots.publish_latest_complete(
        tmp_path, initial, initial_digest, published_at=datetime(2026, 9, 18, 12, tzinfo=UTC)
    )
    retained_manifests = {
        path: path.read_bytes() for path in tmp_path.rglob(snapshots.MANIFEST_FILE)
    }
    # Spawn explicitly even on Unix: these writers share no Python locks or globals.
    context = multiprocessing.get_context("spawn")
    first_attempt, first_read, release = (context.Event() for _ in range(3))
    second_attempt, second_read = (context.Event() for _ in range(2))
    first = context.Process(
        target=_publish_in_process,
        args=(tmp_path, first_manifest, first_digest, first_attempt, first_read, release),
    )
    second = context.Process(
        target=_publish_in_process,
        args=(tmp_path, second_manifest, second_digest, second_attempt, second_read, None),
    )
    first.start()
    try:
        assert first_read.wait(30), "First writer never reached its stale-read barrier"
        second.start()
        assert second_attempt.wait(30), "Second process never attempted publication"
        # With only atomic replace, the second writer reads the initial pointer and
        # finishes while the first is paused, then the first can roll it backwards.
        # A process lock must prevent that read until the first publication finishes.
        second_read_before_release = second_read.wait(1)
    finally:
        release.set()
        first.join(30)
        if second.pid is not None:
            second.join(30)
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert not second_read_before_release
    current, _, _ = snapshots.resolve_latest_complete(tmp_path)
    assert current["reference_time"] == "2026-09-18T11:00:00Z"
    assert current["snapshot_id"] == ("second" if second_hour == 11 else "first")
    second_result = json.loads((tmp_path / "second.result.json").read_text())
    assert second_result["status"] == ("published" if second_hour == 11 else "rejected")
    assert {path: path.read_bytes() for path in retained_manifests} == retained_manifests


@pytest.mark.parametrize("failing_operation", ["fsync", "replace"])
def test_failed_newer_publication_preserves_pointer_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_operation: str
) -> None:
    original, original_digest = _manifest(tmp_path, "original", 9)
    newer, newer_digest = _manifest(tmp_path, "newer", 10)
    published_at = datetime(2026, 9, 18, 12, tzinfo=UTC)
    snapshots.publish_latest_complete(
        tmp_path, original, original_digest, published_at=published_at
    )
    pointer_bytes = (tmp_path / snapshots.POINTER_FILE).read_bytes()
    retained_manifests = {
        path: path.read_bytes() for path in tmp_path.rglob(snapshots.MANIFEST_FILE)
    }

    def fail(*args: Any) -> None:
        raise OSError("Injected publication failure")

    with monkeypatch.context() as failure:
        failure.setattr(snapshots.os, failing_operation, fail)
        with pytest.raises(OSError, match="Injected publication failure"):
            snapshots.publish_latest_complete(
                tmp_path, newer, newer_digest, published_at=published_at
            )

    assert (tmp_path / snapshots.POINTER_FILE).read_bytes() == pointer_bytes
    assert snapshots.resolve_latest_complete(tmp_path)[0]["snapshot_id"] == "original"
    assert {path: path.read_bytes() for path in retained_manifests} == retained_manifests
    assert not list(tmp_path.glob(f".{snapshots.POINTER_FILE}.*.tmp"))
    snapshots.publish_latest_complete(tmp_path, newer, newer_digest, published_at=published_at)
    assert snapshots.resolve_latest_complete(tmp_path)[0]["snapshot_id"] == "newer"


def test_existing_v1_pointer_is_readable_without_lock_artifact(tmp_path: Path) -> None:
    manifest, digest = _manifest(tmp_path, "historical", 9)
    historical_pointer = {
        "schema_version": snapshots.POINTER_SCHEMA,
        "snapshot_id": "historical",
        "snapshot_directory": "snapshots/historical",
        "manifest_file": snapshots.MANIFEST_FILE,
        "manifest_sha256": digest,
        "reference_time": manifest["coverage"]["reference_time"],
        "first_valid_time": manifest["coverage"]["first_valid_time"],
        "last_valid_time": manifest["coverage"]["last_valid_time"],
        "published_at": "2026-09-18T09:15:00Z",
        "previous_snapshot_id": None,
    }
    (tmp_path / snapshots.POINTER_FILE).write_text(json.dumps(historical_pointer))
    pointer, saved_manifest, _ = snapshots.resolve_latest_complete(tmp_path)
    assert pointer == historical_pointer
    assert saved_manifest == manifest
    assert not (tmp_path / ".latest_complete.lock").exists()
