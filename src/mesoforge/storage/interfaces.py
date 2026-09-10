"""Storage and repository protocols (plan Section 4.9).

Behaviorally complete ``Protocol`` declarations only -- no concrete
infrastructure imports (boto3, SQLAlchemy, psycopg, Alembic, h5netcdf).
Concrete adapters (``storage.postgres``, ``storage.s3``,
``storage.netcdf``) implement these protocols; ``application`` composes
them without knowing which concrete adapter is in use.

Every ID/digest field or parameter below uses the typed
``common.identifiers`` classes (``Digest``, ``ArtifactId``, ``ActivityId``,
``ConfigurationSnapshotId``, ``GridId``, ``RunId``), not an unrestricted
``str`` (Codex review t_f569c45c finding 3; final re-review HIGH finding
6/t_1ecb8414: the earlier remediation left this file's ``StoredObject``,
``ConfigurationSnapshotLike``, ``GridDefinitionLike``,
``ArtifactObjectStore``, ``StoredObjectRepository``, and
``IdempotencyLock`` declarations string-typed). Plain ``str`` remains only
for genuinely non-identifier fields: ``storage_uri``, ``media_type``, and
``canonical_json``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    RunId,
)
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.contracts.provenance import ActivityManifest
from mesoforge.provenance.lineage import ActivityEdge, LineageGraph


class StoredObject(Protocol):
    @property
    def content_digest(self) -> Digest: ...
    @property
    def storage_uri(self) -> str: ...
    @property
    def media_type(self) -> str: ...
    @property
    def byte_size(self) -> int: ...


class ConfigurationSnapshotLike(Protocol):
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    canonical_json: dict[str, object]


class GridDefinitionLike(Protocol):
    grid_id: GridId
    definition_digest: Digest


@runtime_checkable
class DatasetSerializer(Protocol):
    def serialize(self, dataset: object) -> bytes: ...
    def deserialize(self, payload: bytes) -> object: ...


class ArtifactObjectStore(Protocol):
    def put_if_absent(
        self, content_digest: Digest, data: bytes, media_type: str
    ) -> StoredObject: ...
    def get_verified(self, storage_uri: str, expected_digest: Digest) -> bytes: ...
    def exists_verified(self, storage_uri: str, expected_digest: Digest) -> bool: ...
    def delete_unregistered(self, storage_uri: str) -> None: ...


class StoredObjectRepository(Protocol):
    def add_if_absent(self, stored_object: StoredObject) -> StoredObject: ...
    def get(self, content_digest: Digest) -> StoredObject: ...


class ArtifactRepository(Protocol):
    """Repository boundary for artifact records. ``artifact_id`` and
    ``digest`` parameters use the typed ``common.identifiers`` types
    (Codex review t_f569c45c finding 3: repository protocols must not
    expose unrestricted ``str`` for IDs/digests) so a malformed value
    is rejected at the protocol boundary rather than silently accepted
    by a concrete adapter. ``add_derived`` (final re-review HIGH finding
    6/t_1ecb8414) is fully typed here, not an untyped ``**kwargs``, so
    its signature itself proves conformance.
    """

    def add(self, manifest: ArtifactManifest) -> ArtifactManifest: ...
    def get(self, artifact_id: ArtifactId) -> ArtifactManifest: ...
    def get_many(self, ids: tuple[ArtifactId, ...]) -> tuple[ArtifactManifest, ...]: ...
    def find_by_source_registration_digest(self, digest: Digest) -> ArtifactManifest | None: ...
    def add_derived(
        self,
        *,
        artifact_id: ArtifactId,
        artifact_type: str,
        artifact_schema_version: str,
        content_digest: Digest,
        created_at: datetime,
        availability_authority: str,
        availability_method: str,
        parent_available_ats: tuple[datetime, ...],
        activity_completed_at: datetime,
        run_id: RunId | None,
        configuration_snapshot_id: ConfigurationSnapshotId,
        configuration_digest: Digest,
        code_revision: str,
        environment_digest: Digest,
        quality_state: str,
        attributes: dict[str, object] | None,
    ) -> ArtifactManifest: ...


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


class IssuedForecastRepository(Protocol):
    def add(self, record: IssuedForecastRecord) -> IssuedForecastRecord: ...
    def get(self, issued_forecast_id: UUID) -> IssuedForecastRecord: ...
    def list_for_coordinate(
        self, latitude: float, longitude: float, *, limit: int | None = 100
    ) -> tuple[IssuedForecastRecord, ...]: ...


class IssuanceUnitOfWork(Protocol):
    """Only the existing repositories needed to save one issued version."""

    @property
    def stored_objects(self) -> StoredObjectRepository: ...
    @property
    def issued_forecasts(self) -> IssuedForecastRepository: ...
    def __enter__(self) -> IssuanceUnitOfWork: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object | None: ...
    def commit(self) -> None: ...


class LineageReader(Protocol):
    def ancestors(self, artifact_id: ArtifactId) -> LineageGraph: ...
    def descendants(self, artifact_id: ArtifactId) -> LineageGraph: ...


class IdempotencyLock(Protocol):
    def acquire(self, digest: Digest) -> object: ...  # context manager


class UnitOfWork(Protocol):
    stored_objects: StoredObjectRepository
    artifacts: ArtifactRepository
    activities: ActivityRepository
    configurations: ConfigurationRepository
    grids: GridRepository
    runs: RunRepository
    issued_forecasts: IssuedForecastRepository

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
    "IssuanceUnitOfWork",
    "IssuedForecastRepository",
    "LineageGraph",
    "LineageReader",
    "RunRepository",
    "StoredObject",
    "StoredObjectRepository",
    "UnitOfWork",
]
