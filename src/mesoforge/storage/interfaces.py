"""Storage and repository protocols (plan Section 4.9).

Behaviorally complete ``Protocol`` declarations only -- no concrete
infrastructure imports (boto3, SQLAlchemy, psycopg, Alembic, h5netcdf).
Concrete adapters (``storage.postgres``, ``storage.s3``,
``storage.netcdf``) implement these protocols; ``application`` composes
them without knowing which concrete adapter is in use.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    RunId,
)
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.provenance import ActivityManifest
from mesoforge.provenance.lineage import ActivityEdge, LineageGraph


class StoredObject(Protocol):
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


class ConfigurationSnapshotLike(Protocol):
    configuration_snapshot_id: str
    configuration_digest: str
    canonical_json: dict[str, object]


class GridDefinitionLike(Protocol):
    grid_id: str
    definition_digest: str


@runtime_checkable
class DatasetSerializer(Protocol):
    def serialize(self, dataset: object) -> bytes: ...
    def deserialize(self, payload: bytes) -> object: ...


class ArtifactObjectStore(Protocol):
    def put_if_absent(self, content_digest: str, data: bytes, media_type: str) -> StoredObject: ...
    def get_verified(self, storage_uri: str, expected_digest: str) -> bytes: ...
    def exists_verified(self, storage_uri: str, expected_digest: str) -> bool: ...
    def delete_unregistered(self, storage_uri: str) -> None: ...


class StoredObjectRepository(Protocol):
    def add_if_absent(self, stored_object: StoredObject) -> StoredObject: ...
    def get(self, content_digest: str) -> StoredObject: ...


class ArtifactRepository(Protocol):
    """Repository boundary for artifact records. ``artifact_id`` and
    ``digest`` parameters use the typed ``common.identifiers`` types
    (Codex review t_f569c45c finding 3: repository protocols must not
    expose unrestricted ``str`` for IDs/digests) so a malformed value
    is rejected at the protocol boundary rather than silently accepted
    by a concrete adapter.
    """

    def add(self, manifest: ArtifactManifest) -> ArtifactManifest: ...
    def get(self, artifact_id: ArtifactId) -> ArtifactManifest: ...
    def get_many(self, ids: tuple[ArtifactId, ...]) -> tuple[ArtifactManifest, ...]: ...
    def find_by_source_registration_digest(self, digest: Digest) -> ArtifactManifest | None: ...
    def add_derived(self, **kwargs: object) -> ArtifactManifest: ...


class ActivityRepository(Protocol):
    def add_started(self, manifest: ActivityManifest) -> ActivityManifest: ...
    def finish_succeeded(
        self, activity_id: ActivityId, outputs: tuple[object, ...], completed_at: object
    ) -> ActivityManifest: ...
    def finish_failed(
        self, activity_id: ActivityId, error: object, completed_at: object
    ) -> ActivityManifest: ...
    def find_succeeded_by_idempotency(self, digest: Digest) -> ActivityManifest | None: ...
    def producer_of(self, artifact_id: ArtifactId) -> ActivityManifest | None: ...
    def consumers_of(self, artifact_id: ArtifactId) -> tuple[ActivityManifest, ...]: ...


class ConfigurationRepository(Protocol):
    def add_if_absent(self, snapshot: ConfigurationSnapshotLike) -> ConfigurationSnapshotLike: ...
    def get(self, snapshot_id: ConfigurationSnapshotId) -> ConfigurationSnapshotLike: ...


class GridRepository(Protocol):
    def add_if_absent(
        self, grid_id: GridId, definition_digest: Digest, canonical_json: dict[str, object]
    ) -> GridDefinitionLike: ...
    def get(self, grid_id: GridId) -> GridDefinitionLike: ...


class RunRepository(Protocol):
    def add(self, manifest: object) -> object: ...
    def get(self, run_id: RunId) -> object: ...


class LineageReader(Protocol):
    def ancestors(self, artifact_id: ArtifactId) -> LineageGraph: ...
    def descendants(self, artifact_id: ArtifactId) -> LineageGraph: ...


class IdempotencyLock(Protocol):
    def acquire(self, digest: str) -> object: ...  # context manager


class UnitOfWork(Protocol):
    stored_objects: StoredObjectRepository
    artifacts: ArtifactRepository
    activities: ActivityRepository
    configurations: ConfigurationRepository
    grids: GridRepository
    runs: RunRepository

    def __enter__(self) -> UnitOfWork: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object | None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


__all__ = [
    "ActivityEdge",
    "ActivityRepository",
    "ArtifactObjectStore",
    "ArtifactRepository",
    "ConfigurationRepository",
    "ConfigurationSnapshotLike",
    "DatasetSerializer",
    "GridDefinitionLike",
    "GridRepository",
    "IdempotencyLock",
    "LineageGraph",
    "LineageReader",
    "RunRepository",
    "StoredObject",
    "StoredObjectRepository",
    "UnitOfWork",
]
