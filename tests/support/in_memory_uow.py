"""Behaviorally complete in-memory test doubles for
storage.interfaces-shaped protocols, used only by unit tests (plan
Section 4.9/Task 10: "in-memory test doubles defined under tests only").
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from mesoforge.common.errors import Conflict, IntegrityError, NotFound
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.provenance import ActivityManifest


@dataclass
class _StoredObject:
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, _StoredObject] = {}
        self.fail_next_put = False
        self.corrupt_next_get = False

    def put_if_absent(self, content_digest: str, data: bytes, media_type: str) -> _StoredObject:
        if self.fail_next_put:
            self.fail_next_put = False
            raise RuntimeError("simulated upload failure")

        key = f"objects/sha256/{content_digest[7:9]}/{content_digest[9:]}"
        storage_uri = f"s3://mesoforge-test/{key}"
        if content_digest not in self.objects:
            self.objects[content_digest] = data
            self.metadata[content_digest] = _StoredObject(
                content_digest=content_digest,
                storage_uri=storage_uri,
                media_type=media_type,
                byte_size=len(data),
            )
        return self.metadata[content_digest]

    def get_verified(self, storage_uri: str, expected_digest: str) -> bytes:
        for digest, meta in self.metadata.items():
            if meta.storage_uri == storage_uri:
                data = self.objects[digest]
                if self.corrupt_next_get:
                    self.corrupt_next_get = False
                    actual_digest = "sha256:" + "0" * 64
                else:
                    actual_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
                if actual_digest != expected_digest:
                    raise IntegrityError(
                        f"checksum mismatch for {storage_uri!r}: expected "
                        f"{expected_digest!r}, got {actual_digest!r}"
                    )
                return data
        raise NotFound(f"no object at {storage_uri!r}")


class InMemoryIdempotencyLock:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, digest: str) -> threading.Lock:
        with self._guard:
            if digest not in self._locks:
                self._locks[digest] = threading.Lock()
            return self._locks[digest]

    @contextmanager
    def acquire(self, digest: str):
        lock = self._lock_for(digest)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


class _InMemoryGridRepository:
    def __init__(self, store: dict[str, tuple[str, dict]]) -> None:
        self._store = store

    def add_if_absent(self, grid_id: str, definition_digest: str, canonical_json: dict) -> object:
        existing = self._store.get(grid_id)
        if existing is not None:
            if existing[0] != definition_digest:
                raise Conflict(
                    f"grid_id {grid_id!r} already registered with a different definition"
                )
            return existing
        self._store[grid_id] = (definition_digest, canonical_json)
        return self._store[grid_id]

    def get(self, grid_id: str) -> object:
        if grid_id not in self._store:
            raise NotFound(f"grid_id {grid_id!r} not found")
        return self._store[grid_id]


class _InMemoryStoredObjectRepository:
    def __init__(self, store: dict[str, object]) -> None:
        self._store = store

    def add_if_absent(self, stored_object: object) -> object:
        digest = stored_object.content_digest  # type: ignore[attr-defined]
        if digest not in self._store:
            self._store[digest] = stored_object
        return self._store[digest]

    def get(self, content_digest: str) -> object:
        if content_digest not in self._store:
            raise NotFound(f"stored object {content_digest!r} not found")
        return self._store[content_digest]


class _InMemoryArtifactRepository:
    def __init__(self, store: dict[str, ArtifactManifest]) -> None:
        self._store = store

    def add(self, manifest: ArtifactManifest) -> ArtifactManifest:
        self._store[manifest.artifact_id] = manifest
        return manifest

    def get(self, artifact_id: str) -> ArtifactManifest:
        if artifact_id not in self._store:
            raise NotFound(f"artifact {artifact_id!r} not found")
        return self._store[artifact_id]

    def get_many(self, ids: tuple[str, ...]) -> tuple[ArtifactManifest, ...]:
        return tuple(self.get(i) for i in ids)

    def find_by_source_registration_digest(self, digest: str) -> ArtifactManifest | None:
        for manifest in self._store.values():
            if manifest.source_registration_digest == digest:
                return manifest
        return None


class _InMemoryActivityRepository:
    def __init__(self, store: dict[str, ActivityManifest]) -> None:
        self._store = store

    def add_started(self, manifest: ActivityManifest) -> ActivityManifest:
        self._store[manifest.activity_id] = manifest
        return manifest

    def finish_succeeded(self, activity_id, outputs, completed_at) -> ActivityManifest:
        existing = self._store[activity_id]
        updated = existing.model_copy(
            update={"status": "succeeded", "completed_at": completed_at, "outputs": outputs}
        )
        self._store[activity_id] = updated
        return updated

    def finish_failed(self, activity_id, error, completed_at) -> ActivityManifest:
        existing = self._store[activity_id]
        updated = existing.model_copy(
            update={"status": "failed", "completed_at": completed_at, "error": error}
        )
        self._store[activity_id] = updated
        return updated

    def find_succeeded_by_idempotency(self, digest: str) -> ActivityManifest | None:
        for manifest in self._store.values():
            if manifest.idempotency_digest == digest and manifest.status == "succeeded":
                return manifest
        return None

    def producer_of(self, artifact_id: str) -> ActivityManifest | None:
        for manifest in self._store.values():
            for output in manifest.outputs:
                if output.artifact_id == artifact_id:
                    return manifest
        return None

    def consumers_of(self, artifact_id: str) -> tuple[ActivityManifest, ...]:
        return tuple(
            m for m in self._store.values() if any(i.artifact_id == artifact_id for i in m.inputs)
        )


class InMemoryUnitOfWork:
    def __init__(self, factory: InMemoryUnitOfWorkFactory) -> None:
        self._factory = factory
        self.grids = _InMemoryGridRepository(factory.grids)
        self.stored_objects = _InMemoryStoredObjectRepository(factory.stored_objects)
        self.artifacts = _InMemoryArtifactRepository(factory.artifacts)
        self.activities = _InMemoryActivityRepository(factory.activities)

    def __enter__(self) -> InMemoryUnitOfWork:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class InMemoryUnitOfWorkFactory:
    """Shared, process-lifetime backing dicts (simulates a persistent
    database across ``with unit_of_work_factory() as uow`` blocks)."""

    def __init__(self) -> None:
        self.grids: dict[str, tuple[str, dict]] = {}
        self.stored_objects: dict[str, object] = {}
        self.artifacts: dict[str, ArtifactManifest] = {}
        self.activities: dict[str, ActivityManifest] = {}
        self._guard = threading.Lock()

    def __call__(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self)
