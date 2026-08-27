"""PostgreSQL-backed implementations of the storage.interfaces protocols
(plan Section 4.9/4.10, Task 8).

SQLAlchemy ORM objects never escape this module: every method returns
plain dataclass/contract value objects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mesoforge.common.errors import Conflict, NotFound
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    strip_prefix,
)
from mesoforge.contracts.artifacts import ArtifactManifest, Availability, SourceIdentity
from mesoforge.contracts.provenance import ActivityArtifactRef, ActivityError, ActivityManifest
from mesoforge.contracts.runs import RunManifest
from mesoforge.storage.postgres.database import create_database_engine, create_session_factory
from mesoforge.storage.postgres.models import (
    ActivityInputRow,
    ActivityOutputRow,
    ActivityRow,
    ArtifactRow,
    ConfigurationSnapshotRow,
    GridRow,
    RunRow,
    RunSelectedInputRow,
    StoredObjectRow,
)


class _ConfigurationSnapshotInput(Protocol):
    configuration_snapshot_id: str
    configuration_digest: str
    canonical_json: dict[str, object]
    source_references: tuple[dict[str, object], ...]


class _StoredObjectInput(Protocol):
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


@dataclass(frozen=True)
class GridRecord:
    grid_id: str
    definition_digest: str
    canonical_json: dict[str, object]


@dataclass(frozen=True)
class ConfigurationSnapshotRecord:
    configuration_snapshot_id: str
    configuration_digest: str
    canonical_json: dict[str, object]
    source_references: tuple[dict[str, object], ...]
    created_at: datetime


@dataclass(frozen=True)
class StoredObjectRecord:
    content_digest: str
    storage_uri: str
    media_type: str
    byte_size: int


class PostgresGridRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(
        self, grid_id: str, definition_digest: str, canonical_json: dict[str, object]
    ) -> GridRecord:
        existing = self._session.get(GridRow, grid_id)
        if existing is not None:
            if existing.definition_digest != definition_digest:
                raise Conflict(
                    f"grid_id {grid_id!r} is already registered with a different definition_digest"
                )
            return GridRecord(existing.id, existing.definition_digest, existing.canonical_json)

        row = GridRow(
            id=grid_id, definition_digest=definition_digest, canonical_json=canonical_json
        )
        self._session.add(row)
        self._session.flush()
        return GridRecord(row.id, row.definition_digest, row.canonical_json)

    def get(self, grid_id: str) -> GridRecord:
        row = self._session.get(GridRow, grid_id)
        if row is None:
            raise NotFound(f"grid_id {grid_id!r} not found")
        return GridRecord(row.id, row.definition_digest, row.canonical_json)


class PostgresConfigurationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(self, snapshot: _ConfigurationSnapshotInput) -> ConfigurationSnapshotRecord:
        digest = snapshot.configuration_digest
        existing = self._session.execute(
            sa.select(ConfigurationSnapshotRow).where(ConfigurationSnapshotRow.digest == digest)
        ).scalar_one_or_none()
        if existing is not None:
            return ConfigurationSnapshotRecord(
                configuration_snapshot_id=existing.id,
                configuration_digest=existing.digest,
                canonical_json=existing.canonical_json,
                source_references=tuple(existing.source_references or []),
                created_at=existing.created_at,
            )

        snapshot_id = snapshot.configuration_snapshot_id
        canonical_json = snapshot.canonical_json
        source_references = getattr(snapshot, "source_references", ()) or ()
        row = ConfigurationSnapshotRow(
            id=snapshot_id,
            digest=digest,
            schema_version="configuration-snapshot.v1",
            canonical_json=canonical_json,
            source_references=list(source_references),
        )
        self._session.add(row)
        self._session.flush()
        return ConfigurationSnapshotRecord(
            configuration_snapshot_id=row.id,
            configuration_digest=row.digest,
            canonical_json=row.canonical_json,
            source_references=tuple(row.source_references or []),
            created_at=row.created_at,
        )

    def get(self, snapshot_id: str) -> ConfigurationSnapshotRecord:
        row = self._session.get(ConfigurationSnapshotRow, snapshot_id)
        if row is None:
            raise NotFound(f"configuration snapshot {snapshot_id!r} not found")
        return ConfigurationSnapshotRecord(
            configuration_snapshot_id=row.id,
            configuration_digest=row.digest,
            canonical_json=row.canonical_json,
            source_references=tuple(row.source_references or []),
            created_at=row.created_at,
        )


class PostgresStoredObjectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(self, stored_object: _StoredObjectInput) -> StoredObjectRecord:
        digest = stored_object.content_digest
        existing = self._session.get(StoredObjectRow, digest)
        if existing is not None:
            return StoredObjectRecord(
                existing.content_digest,
                existing.storage_uri,
                existing.media_type,
                existing.byte_size,
            )
        row = StoredObjectRow(
            content_digest=digest,
            storage_uri=stored_object.storage_uri,
            media_type=stored_object.media_type,
            byte_size=stored_object.byte_size,
        )
        self._session.add(row)
        self._session.flush()
        return StoredObjectRecord(
            row.content_digest, row.storage_uri, row.media_type, row.byte_size
        )

    def get(self, content_digest: str) -> StoredObjectRecord:
        row = self._session.get(StoredObjectRow, content_digest)
        if row is None:
            raise NotFound(f"stored object {content_digest!r} not found")
        return StoredObjectRecord(
            row.content_digest, row.storage_uri, row.media_type, row.byte_size
        )


def _artifact_row_to_manifest(row: ArtifactRow) -> ArtifactManifest:
    source_identity = None
    if row.source_authority is not None:
        source_identity = SourceIdentity(
            authority=row.source_authority,
            locator=row.source_locator,  # type: ignore[arg-type]
            revision=row.source_revision,  # type: ignore[arg-type]
        )
    return ArtifactManifest(
        artifact_id=ArtifactId(f"art_{row.id}"),
        artifact_type=row.artifact_type,
        artifact_schema_version=row.artifact_schema_version,
        media_type=row.stored_object.media_type,
        byte_size=row.stored_object.byte_size,
        content_digest=Digest(row.content_digest),
        storage_uri=row.stored_object.storage_uri,
        created_at=row.created_at,
        registered_at=row.registered_at,
        availability=Availability(
            available_at=row.available_at,
            authority=row.availability_authority,
            method=row.availability_method,
            ingested_at=row.ingested_at,
        ),
        run_id=(RunId(f"run_{row.run_id}") if row.run_id is not None else None),
        configuration_snapshot_id=ConfigurationSnapshotId(row.configuration_snapshot_id),
        configuration_digest=Digest(row.configuration_digest),
        code_revision=row.code_revision,
        environment_digest=Digest(row.environment_digest),
        quality_state=row.quality_state,
        source_registration_digest=(
            Digest(row.source_registration_digest)
            if row.source_registration_digest is not None
            else None
        ),
        source_identity=source_identity,
        attributes=row.attributes,
    )


class PostgresArtifactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, manifest: ArtifactManifest) -> ArtifactManifest:
        artifact_uuid = uuid.UUID(strip_prefix(manifest.artifact_id, "art_"))
        run_uuid = (
            uuid.UUID(strip_prefix(manifest.run_id, "run_"))
            if manifest.run_id is not None
            else None
        )
        row = ArtifactRow(
            id=artifact_uuid,
            schema_version=manifest.schema_version,
            artifact_type=manifest.artifact_type,
            artifact_schema_version=manifest.artifact_schema_version,
            content_digest=manifest.content_digest,
            source_registration_digest=manifest.source_registration_digest,
            source_authority=(
                manifest.source_identity.authority if manifest.source_identity else None
            ),
            source_locator=(manifest.source_identity.locator if manifest.source_identity else None),
            source_revision=(
                manifest.source_identity.revision if manifest.source_identity else None
            ),
            created_at=manifest.created_at,
            available_at=manifest.availability.available_at,
            availability_authority=manifest.availability.authority,
            availability_method=manifest.availability.method,
            ingested_at=manifest.availability.ingested_at,
            run_id=run_uuid,
            configuration_snapshot_id=manifest.configuration_snapshot_id,
            configuration_digest=manifest.configuration_digest,
            code_revision=manifest.code_revision,
            environment_digest=manifest.environment_digest,
            quality_state=manifest.quality_state,
            attributes=manifest.attributes,
        )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return _artifact_row_to_manifest(row)

    def add_derived(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        artifact_schema_version: str,
        content_digest: str,
        created_at: datetime,
        availability_authority: str,
        availability_method: str,
        parent_available_ats: tuple[datetime, ...],
        activity_completed_at: datetime,
        run_id: str | None,
        configuration_snapshot_id: str,
        configuration_digest: str,
        code_revision: str,
        environment_digest: str,
        quality_state: str,
        attributes: dict[str, object] | None,
    ) -> ArtifactManifest:
        """Insert a derived artifact whose ``registered_at`` and
        ``available_at`` are BOTH computed by PostgreSQL from a single
        ``transaction_timestamp()`` call within this insert statement
        (plan Section 5, step 8; Codex review t_9bb13e2b finding 3).

        ``transaction_timestamp()`` is constant for the duration of a
        PostgreSQL transaction, so referencing it twice in the same
        statement -- once directly for ``registered_at`` and once inside
        ``GREATEST(...)`` for ``available_at`` -- guarantees both derive
        from the exact same instant, and that ``available_at >=
        registered_at`` always holds by construction (the
        ``transaction_timestamp()`` term inside GREATEST can never be
        less than ``registered_at``, which equals it exactly). The
        application layer never computes or supplies either timestamp
        with ``datetime.now()``.
        """
        artifact_uuid = uuid.UUID(strip_prefix(artifact_id, "art_"))
        run_uuid = uuid.UUID(strip_prefix(run_id, "run_")) if run_id is not None else None

        available_at_expr = sa.func.greatest(
            activity_completed_at, sa.func.transaction_timestamp(), *parent_available_ats
        )
        stmt = (
            sa.insert(ArtifactRow)
            .values(
                id=artifact_uuid,
                schema_version="artifact-manifest.v1",
                artifact_type=artifact_type,
                artifact_schema_version=artifact_schema_version,
                content_digest=content_digest,
                source_registration_digest=None,
                source_authority=None,
                source_locator=None,
                source_revision=None,
                created_at=created_at,
                registered_at=sa.func.transaction_timestamp(),
                available_at=available_at_expr,
                availability_authority=availability_authority,
                availability_method=availability_method,
                ingested_at=None,
                run_id=run_uuid,
                configuration_snapshot_id=configuration_snapshot_id,
                configuration_digest=configuration_digest,
                code_revision=code_revision,
                environment_digest=environment_digest,
                quality_state=quality_state,
                attributes=attributes,
            )
            .returning(ArtifactRow)
        )
        row = self._session.execute(stmt).scalars().one()
        self._session.flush()
        self._session.refresh(row)
        return _artifact_row_to_manifest(row)

    def get(self, artifact_id: str) -> ArtifactManifest:
        row = self._session.get(ArtifactRow, uuid.UUID(strip_prefix(artifact_id, "art_")))
        if row is None:
            raise NotFound(f"artifact {artifact_id!r} not found")
        return _artifact_row_to_manifest(row)

    def get_many(self, ids: tuple[str, ...]) -> tuple[ArtifactManifest, ...]:
        return tuple(self.get(artifact_id) for artifact_id in ids)

    def find_by_source_registration_digest(self, digest: str) -> ArtifactManifest | None:
        row = self._session.execute(
            sa.select(ArtifactRow).where(ArtifactRow.source_registration_digest == digest)
        ).scalar_one_or_none()
        return _artifact_row_to_manifest(row) if row is not None else None


def _activity_row_to_manifest(row: ActivityRow) -> ActivityManifest:
    inputs = tuple(
        ActivityArtifactRef(role=i.role, artifact_id=ArtifactId(f"art_{i.artifact_id}"))
        for i in sorted(row.inputs, key=lambda i: i.ordinal)
    )
    outputs = tuple(
        ActivityArtifactRef(role=o.role, artifact_id=ArtifactId(f"art_{o.artifact_id}"))
        for o in sorted(row.outputs, key=lambda o: o.ordinal)
    )
    return ActivityManifest(
        activity_id=ActivityId(f"act_{row.id}"),
        activity_type=row.activity_type,
        activity_version=row.activity_version,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        idempotency_digest=Digest(row.idempotency_digest),
        parameters_digest=Digest(row.parameters_digest),
        configuration_snapshot_id=ConfigurationSnapshotId(row.configuration_snapshot_id),
        configuration_digest=Digest(row.configuration_digest),
        code_revision=row.code_revision,
        environment_digest=Digest(row.environment_digest),
        run_id=(RunId(f"run_{row.run_id}") if row.run_id is not None else None),
        inputs=inputs,
        outputs=outputs,
        error=(ActivityError(**row.error) if row.error is not None else None),
    )


class PostgresActivityRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_started(self, manifest: ActivityManifest) -> ActivityManifest:
        activity_uuid = uuid.UUID(strip_prefix(manifest.activity_id, "act_"))
        run_uuid = (
            uuid.UUID(strip_prefix(manifest.run_id, "run_"))
            if manifest.run_id is not None
            else None
        )
        row = ActivityRow(
            id=activity_uuid,
            schema_version=manifest.schema_version,
            activity_type=manifest.activity_type,
            activity_version=manifest.activity_version,
            status=manifest.status,
            started_at=manifest.started_at,
            idempotency_digest=manifest.idempotency_digest,
            parameters_digest=manifest.parameters_digest,
            configuration_snapshot_id=manifest.configuration_snapshot_id,
            configuration_digest=manifest.configuration_digest,
            code_revision=manifest.code_revision,
            environment_digest=manifest.environment_digest,
            run_id=run_uuid,
        )
        for ordinal, ref in enumerate(manifest.inputs):
            row.inputs.append(
                ActivityInputRow(
                    ordinal=ordinal,
                    role=ref.role,
                    artifact_id=uuid.UUID(strip_prefix(ref.artifact_id, "art_")),
                )
            )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return _activity_row_to_manifest(row)

    def finish_succeeded(
        self,
        activity_id: str,
        outputs: tuple[ActivityArtifactRef, ...],
        completed_at: datetime,
    ) -> ActivityManifest:
        row = self._session.get(ActivityRow, uuid.UUID(strip_prefix(activity_id, "act_")))
        if row is None:
            raise NotFound(f"activity {activity_id!r} not found")
        row.status = "succeeded"
        row.completed_at = completed_at
        for ordinal, ref in enumerate(outputs):
            row.outputs.append(
                ActivityOutputRow(
                    ordinal=ordinal,
                    role=ref.role,
                    artifact_id=uuid.UUID(strip_prefix(ref.artifact_id, "art_")),
                )
            )
        self._session.flush()
        self._session.refresh(row)
        return _activity_row_to_manifest(row)

    def finish_failed(
        self, activity_id: str, error: ActivityError, completed_at: datetime
    ) -> ActivityManifest:
        row = self._session.get(ActivityRow, uuid.UUID(strip_prefix(activity_id, "act_")))
        if row is None:
            raise NotFound(f"activity {activity_id!r} not found")
        row.status = "failed"
        row.completed_at = completed_at
        row.error = error.model_dump(mode="json")
        self._session.flush()
        self._session.refresh(row)
        return _activity_row_to_manifest(row)

    def find_succeeded_by_idempotency(self, digest: str) -> ActivityManifest | None:
        row = self._session.execute(
            sa.select(ActivityRow).where(
                ActivityRow.idempotency_digest == digest, ActivityRow.status == "succeeded"
            )
        ).scalar_one_or_none()
        return _activity_row_to_manifest(row) if row is not None else None

    def producer_of(self, artifact_id: str) -> ActivityManifest | None:
        artifact_uuid = uuid.UUID(strip_prefix(artifact_id, "art_"))
        output_row = self._session.execute(
            sa.select(ActivityOutputRow).where(ActivityOutputRow.artifact_id == artifact_uuid)
        ).scalar_one_or_none()
        if output_row is None:
            return None
        row = self._session.get(ActivityRow, output_row.activity_id)
        return _activity_row_to_manifest(row) if row is not None else None

    def consumers_of(self, artifact_id: str) -> tuple[ActivityManifest, ...]:
        artifact_uuid = uuid.UUID(strip_prefix(artifact_id, "art_"))
        input_rows = self._session.execute(
            sa.select(ActivityInputRow).where(ActivityInputRow.artifact_id == artifact_uuid)
        ).scalars()
        activity_ids = {row.activity_id for row in input_rows}
        rows = [self._session.get(ActivityRow, aid) for aid in activity_ids]
        return tuple(_activity_row_to_manifest(row) for row in rows if row is not None)


def _run_row_to_manifest(row: RunRow) -> RunManifest:
    # The relational run_selected_inputs join table (ordinal order
    # preserved via ORDER BY ordinal) is the ONLY source of selected
    # input references -- the legacy runs.selected_inputs JSONB column
    # and its fail-open fallback read path were removed by migration
    # 0003_drop_legacy_selection_json (Codex review t_f569c45c
    # finding 5: a run claiming selected inputs must never load from
    # unconstrained JSON). A run with zero selected inputs (an empty
    # tuple) is legitimately possible and is not a fallback condition.
    ordered_ids = tuple(
        ArtifactId(f"art_{r.artifact_id}")
        for r in sorted(row.selected_input_rows, key=lambda r: r.ordinal)
    )
    return RunManifest(
        run_id=RunId(f"run_{row.id}"),
        forecast_issue_time=row.forecast_issue_time,
        information_cutoff=row.information_cutoff,
        configuration_snapshot_id=ConfigurationSnapshotId(row.configuration_snapshot_id),
        configuration_digest=Digest(row.configuration_digest),
        code_revision=row.code_revision,
        environment_digest=Digest(row.environment_digest),
        lockfile_digest=Digest(row.lockfile_digest),
        random_seed=row.random_seed,
        selected_input_artifact_ids=ordered_ids,
        created_at=row.created_at,
    )


class PostgresRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, manifest: RunManifest) -> RunManifest:
        run_uuid = uuid.UUID(strip_prefix(manifest.run_id, "run_"))
        row = RunRow(
            id=run_uuid,
            schema_version=manifest.schema_version,
            forecast_issue_time=manifest.forecast_issue_time,
            information_cutoff=manifest.information_cutoff,
            configuration_snapshot_id=manifest.configuration_snapshot_id,
            configuration_digest=manifest.configuration_digest,
            code_revision=manifest.code_revision,
            environment_digest=manifest.environment_digest,
            lockfile_digest=manifest.lockfile_digest,
            random_seed=manifest.random_seed,
        )
        for ordinal, artifact_id in enumerate(manifest.selected_input_artifact_ids):
            row.selected_input_rows.append(
                RunSelectedInputRow(
                    ordinal=ordinal,
                    artifact_id=uuid.UUID(strip_prefix(artifact_id, "art_")),
                )
            )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return _run_row_to_manifest(row)

    def get(self, run_id: str) -> RunManifest:
        row = self._session.get(RunRow, uuid.UUID(strip_prefix(run_id, "run_")))
        if row is None:
            raise NotFound(f"run {run_id!r} not found")
        return _run_row_to_manifest(row)


class PostgresUnitOfWork:
    """Concrete storage.interfaces.UnitOfWork over one SQLAlchemy Session."""

    def __init__(self, dsn: str) -> None:
        self._engine = create_database_engine(dsn)
        self._session_factory = create_session_factory(self._engine)
        self._session: Session | None = None

    def __enter__(self) -> PostgresUnitOfWork:
        self._session = self._session_factory()
        self.grids = PostgresGridRepository(self._session)
        self.configurations = PostgresConfigurationRepository(self._session)
        self.stored_objects = PostgresStoredObjectRepository(self._session)
        self.artifacts = PostgresArtifactRepository(self._session)
        self.activities = PostgresActivityRepository(self._session)
        self.runs = PostgresRunRepository(self._session)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._session is not None
        if exc_type is not None:
            self._session.rollback()
        self._session.close()
        self._engine.dispose()

    def commit(self) -> None:
        assert self._session is not None
        try:
            self._session.commit()
        except sa.exc.IntegrityError as exc:
            self._session.rollback()
            raise Conflict(f"commit violated a database constraint: {exc}") from exc

    def rollback(self) -> None:
        assert self._session is not None
        self._session.rollback()
