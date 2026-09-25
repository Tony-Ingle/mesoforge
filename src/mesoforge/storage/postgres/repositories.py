"""PostgreSQL-backed implementations of the storage.interfaces protocols
(plan Section 4.9/4.10, Task 8).

SQLAlchemy ORM objects never escape this module: every method returns
plain dataclass/contract value objects.

Every public repository method's ID/digest parameters and return-object
fields use the typed ``common.identifiers`` classes (``Digest``,
``ArtifactId``, ``ActivityId``, ``ConfigurationSnapshotId``, ``GridId``,
``RunId``), not an unrestricted ``str`` (Codex review t_f569c45c finding
3; final re-review HIGH finding 6/t_1ecb8414: concrete adapter entry
points must construct/validate typed values themselves -- a caller that
bypasses static typing at runtime must still fail closed with
``InvalidIdentifier`` before a malformed ID/digest reaches SQL, rather
than silently becoming a query miss).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from mesoforge.common.errors import Conflict, NotFound
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    RunId,
    strip_prefix,
    validate_code_revision,
)
from mesoforge.contracts.artifacts import ArtifactManifest, Availability, SourceIdentity
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
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
    IssuedForecastRow,
    RunRow,
    RunSelectedInputRow,
    StoredObjectRow,
)


class _ConfigurationSnapshotInput(Protocol):
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    canonical_json: dict[str, object]
    source_references: tuple[dict[str, object], ...]


class _StoredObjectInput(Protocol):
    @property
    def content_digest(self) -> Digest: ...
    @property
    def storage_uri(self) -> str: ...
    @property
    def media_type(self) -> str: ...
    @property
    def byte_size(self) -> int: ...


@dataclass(frozen=True)
class GridRecord:
    grid_id: GridId
    definition_digest: Digest
    canonical_json: dict[str, object]


@dataclass(frozen=True)
class ConfigurationSnapshotRecord:
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    canonical_json: dict[str, object]
    source_references: tuple[dict[str, object], ...]
    created_at: datetime


@dataclass(frozen=True)
class StoredObjectRecord:
    content_digest: Digest
    storage_uri: str
    media_type: str
    byte_size: int


class PostgresGridRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(
        self, grid_id: GridId, definition_digest: Digest, canonical_json: dict[str, object]
    ) -> GridRecord:
        grid_id = GridId(grid_id)
        definition_digest = Digest(definition_digest)
        existing = self._session.get(GridRow, str(grid_id))
        if existing is not None:
            if existing.definition_digest != definition_digest:
                raise Conflict(
                    f"grid_id {grid_id!r} is already registered with a different definition_digest"
                )
            return GridRecord(
                GridId(existing.id), Digest(existing.definition_digest), existing.canonical_json
            )

        row = GridRow(
            id=str(grid_id),
            definition_digest=str(definition_digest),
            canonical_json=canonical_json,
        )
        self._session.add(row)
        self._session.flush()
        return GridRecord(GridId(row.id), Digest(row.definition_digest), row.canonical_json)

    def get(self, grid_id: GridId) -> GridRecord:
        grid_id = GridId(grid_id)
        row = self._session.get(GridRow, str(grid_id))
        if row is None:
            raise NotFound(f"grid_id {grid_id!r} not found")
        return GridRecord(GridId(row.id), Digest(row.definition_digest), row.canonical_json)


class PostgresConfigurationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(self, snapshot: _ConfigurationSnapshotInput) -> ConfigurationSnapshotRecord:
        digest = str(Digest(snapshot.configuration_digest))
        existing = self._session.execute(
            sa.select(ConfigurationSnapshotRow).where(ConfigurationSnapshotRow.digest == digest)
        ).scalar_one_or_none()
        if existing is not None:
            return ConfigurationSnapshotRecord(
                configuration_snapshot_id=ConfigurationSnapshotId(existing.id),
                configuration_digest=Digest(existing.digest),
                canonical_json=existing.canonical_json,
                source_references=tuple(existing.source_references or []),
                created_at=existing.created_at,
            )

        snapshot_id = str(ConfigurationSnapshotId(snapshot.configuration_snapshot_id))
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
            configuration_snapshot_id=ConfigurationSnapshotId(row.id),
            configuration_digest=Digest(row.digest),
            canonical_json=row.canonical_json,
            source_references=tuple(row.source_references or []),
            created_at=row.created_at,
        )

    def get(self, snapshot_id: ConfigurationSnapshotId) -> ConfigurationSnapshotRecord:
        snapshot_id = ConfigurationSnapshotId(snapshot_id)
        row = self._session.get(ConfigurationSnapshotRow, str(snapshot_id))
        if row is None:
            raise NotFound(f"configuration snapshot {snapshot_id!r} not found")
        return ConfigurationSnapshotRecord(
            configuration_snapshot_id=ConfigurationSnapshotId(row.id),
            configuration_digest=Digest(row.digest),
            canonical_json=row.canonical_json,
            source_references=tuple(row.source_references or []),
            created_at=row.created_at,
        )


class PostgresStoredObjectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(self, stored_object: _StoredObjectInput) -> StoredObjectRecord:
        digest = str(Digest(stored_object.content_digest))
        existing = self._session.get(StoredObjectRow, digest)
        if existing is not None:
            return StoredObjectRecord(
                Digest(existing.content_digest),
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
            Digest(row.content_digest), row.storage_uri, row.media_type, row.byte_size
        )

    def get(self, content_digest: Digest) -> StoredObjectRecord:
        content_digest = Digest(content_digest)
        row = self._session.get(StoredObjectRow, str(content_digest))
        if row is None:
            raise NotFound(f"stored object {content_digest!r} not found")
        return StoredObjectRecord(
            Digest(row.content_digest), row.storage_uri, row.media_type, row.byte_size
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

        Every ID/digest parameter is re-constructed against its typed
        ``common.identifiers`` class immediately (final re-review HIGH
        finding 6/t_1ecb8414): a caller that bypasses static typing at
        runtime with a malformed value fails closed with
        ``InvalidIdentifier`` here, before any SQL is issued.
        ``code_revision`` is a direct (non-Pydantic-mediated) parameter
        of this boundary -- like ``compute_idempotency_digest`` -- so it
        must itself call ``validate_code_revision`` rather than trust a
        caller that bypasses static typing (exhaustive-inventory
        remediation: this was the one direct ``code_revision`` boundary
        that had no runtime validation at all).
        """
        artifact_id = ArtifactId(artifact_id)
        content_digest = Digest(content_digest)
        run_id = RunId(run_id) if run_id is not None else None
        configuration_snapshot_id = ConfigurationSnapshotId(configuration_snapshot_id)
        configuration_digest = Digest(configuration_digest)
        code_revision = validate_code_revision(code_revision)
        environment_digest = Digest(environment_digest)

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
                content_digest=str(content_digest),
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
                configuration_snapshot_id=str(configuration_snapshot_id),
                configuration_digest=str(configuration_digest),
                code_revision=code_revision,
                environment_digest=str(environment_digest),
                quality_state=quality_state,
                attributes=attributes,
            )
            .returning(ArtifactRow)
        )
        row = self._session.execute(stmt).scalars().one()
        self._session.flush()
        self._session.refresh(row)
        return _artifact_row_to_manifest(row)

    def get(self, artifact_id: ArtifactId) -> ArtifactManifest:
        artifact_id = ArtifactId(artifact_id)
        row = self._session.get(ArtifactRow, uuid.UUID(strip_prefix(artifact_id, "art_")))
        if row is None:
            raise NotFound(f"artifact {artifact_id!r} not found")
        return _artifact_row_to_manifest(row)

    def get_many(self, ids: tuple[ArtifactId, ...]) -> tuple[ArtifactManifest, ...]:
        return tuple(self.get(artifact_id) for artifact_id in ids)

    def find_by_source_registration_digest(self, digest: Digest) -> ArtifactManifest | None:
        digest = Digest(digest)
        row = self._session.execute(
            sa.select(ArtifactRow).where(ArtifactRow.source_registration_digest == str(digest))
        ).scalar_one_or_none()
        return _artifact_row_to_manifest(row) if row is not None else None

    def find_real_metar_sources(
        self, *, latitude: float, longitude: float, configuration_digest: Digest
    ) -> tuple[ArtifactManifest, ...]:
        """Find retained real acquisitions without selecting a new observation revision."""
        configuration_digest = Digest(configuration_digest)
        for value, bound in ((latitude, 90), (longitude, 180)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not -bound <= value <= bound
            ):
                raise ValueError("latitude and longitude must be finite geographic coordinates")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == "aviationweather-metar-response",
                ArtifactRow.source_authority == "aviationweather.gov",
                ArtifactRow.configuration_digest == str(configuration_digest),
                ArtifactRow.attributes.contains(
                    {
                        "data_kind": "real_metar_observations",
                        "latitude": latitude,
                        "longitude": longitude,
                    }
                ),
            )
            .order_by(ArtifactRow.registered_at, ArtifactRow.id)
        )
        return tuple(_artifact_row_to_manifest(row) for row in rows)

    def find_mrms_extractions(
        self, *, product_time: datetime, limit: int = 1000
    ) -> tuple[ArtifactManifest, ...]:
        """Find one retained source hour across coordinates, including legacy extractions.

        Older extraction manifests have no query attributes. Their immutable
        payloads are checked by the caller; the bounded query does not rewrite them.
        """
        if product_time.tzinfo is None or not 1 <= limit <= 1000:
            raise ValueError("MRMS lookup requires an aware product time and bounded limit")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == "mrms-coordinate-extraction",
                sa.or_(
                    ArtifactRow.attributes.contains(
                        {"product_time": product_time.astimezone(UTC).isoformat()}
                    ),
                    ArtifactRow.attributes.is_(None),
                    sa.not_(ArtifactRow.attributes.has_key("product_time")),
                ),
            )
            .order_by(ArtifactRow.registered_at, ArtifactRow.id)
            .limit(limit)
        )
        return tuple(_artifact_row_to_manifest(row) for row in rows)

    def find_issued_temperature_verifications(
        self, *, latitude: float, longitude: float
    ) -> tuple[ArtifactManifest, ...]:
        """Find saved verification facts by their coordinate attributes, without payload reads."""
        for value, bound in ((latitude, 90), (longitude, 180)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not -bound <= value <= bound
            ):
                raise ValueError("latitude and longitude must be finite geographic coordinates")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == "issued-temperature-verification",
                ArtifactRow.attributes.contains({"latitude": latitude, "longitude": longitude}),
            )
            .order_by(ArtifactRow.registered_at, ArtifactRow.id)
        )
        return tuple(_artifact_row_to_manifest(row) for row in rows)

    def find_issued_qpf_verifications(
        self,
        *,
        latitude: float,
        longitude: float,
        start_valid_time: datetime,
        end_valid_time: datetime,
        limit: int = 5000,
    ) -> tuple[ArtifactManifest, ...]:
        """Bounded, field-specific analytical facts; never mix temperature records."""
        if not 1 <= limit <= 10001:
            raise ValueError("QPF fact query limit must be within 1..10001")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == "issued-qpf-verification",
                ArtifactRow.attributes.contains({"latitude": latitude, "longitude": longitude}),
                sa.cast(ArtifactRow.attributes["valid_time"].astext, sa.DateTime(timezone=True))
                >= start_valid_time,
                sa.cast(ArtifactRow.attributes["valid_time"].astext, sa.DateTime(timezone=True))
                < end_valid_time,
            )
            .order_by(ArtifactRow.registered_at, ArtifactRow.id)
            .limit(limit)
        )
        return tuple(_artifact_row_to_manifest(row) for row in rows)

    def find_learning_artifacts(
        self, artifact_type: str, *, attributes: dict[str, object], limit: int = 1000
    ) -> tuple[ArtifactManifest, ...]:
        """Bounded lookup of compact learning artifacts in the existing artifact catalog."""
        if (
            artifact_type
            not in {"learning-policy", "learning-overlay", "forecast-variant", "learning-binding"}
            or not 1 <= limit <= 10000
        ):
            raise ValueError("Invalid learning artifact query")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == artifact_type,
                ArtifactRow.attributes.contains(attributes),
            )
            .order_by(ArtifactRow.registered_at, ArtifactRow.id)
            .limit(limit + 1)
        )
        found = tuple(_artifact_row_to_manifest(row) for row in rows)
        if len(found) > limit:
            raise ValueError("Learning artifact query is truncated; narrow its scope")
        return found

    def find_unindexed_issued_temperature_verifications(
        self, *, limit: int
    ) -> tuple[ArtifactManifest, ...]:
        """Find saved verification facts that carry no identity attributes (any coordinate).

        Facts persisted before attributes existed can only be identified from their
        immutable payload, so callers must bound how many they read.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == "issued-temperature-verification",
                sa.or_(
                    ArtifactRow.attributes.is_(None),
                    sa.not_(ArtifactRow.attributes.has_key("issued_forecast_id")),
                ),
            )
            .order_by(ArtifactRow.registered_at, ArtifactRow.id)
            .limit(limit)
        )
        return tuple(_artifact_row_to_manifest(row) for row in rows)

    def find_station_discovery_sources(
        self, *, latitude: float, longitude: float, policy_version: str
    ) -> tuple[ArtifactManifest, ...]:
        """Find newest coordinate-scoped station metadata through existing source artifacts."""
        for value, bound in ((latitude, 90), (longitude, 180)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not -bound <= value <= bound
            ):
                raise ValueError("latitude and longitude must be finite geographic coordinates")
        rows = self._session.scalars(
            sa.select(ArtifactRow)
            .where(
                ArtifactRow.artifact_type == "aviationweather-stationinfo-response",
                ArtifactRow.source_authority == "aviationweather.gov",
                ArtifactRow.attributes.contains(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "policy_version": policy_version,
                    }
                ),
            )
            .order_by(ArtifactRow.registered_at.desc(), ArtifactRow.id.desc())
        )
        return tuple(_artifact_row_to_manifest(row) for row in rows)


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
        activity_id: ActivityId,
        outputs: tuple[ActivityArtifactRef, ...],
        completed_at: datetime,
    ) -> ActivityManifest:
        activity_id = ActivityId(activity_id)
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
        self, activity_id: ActivityId, error: ActivityError, completed_at: datetime
    ) -> ActivityManifest:
        activity_id = ActivityId(activity_id)
        row = self._session.get(ActivityRow, uuid.UUID(strip_prefix(activity_id, "act_")))
        if row is None:
            raise NotFound(f"activity {activity_id!r} not found")
        row.status = "failed"
        row.completed_at = completed_at
        row.error = error.model_dump(mode="json")
        self._session.flush()
        self._session.refresh(row)
        return _activity_row_to_manifest(row)

    def find_succeeded_by_idempotency(self, digest: Digest) -> ActivityManifest | None:
        digest = Digest(digest)
        row = self._session.execute(
            sa.select(ActivityRow).where(
                ActivityRow.idempotency_digest == str(digest), ActivityRow.status == "succeeded"
            )
        ).scalar_one_or_none()
        return _activity_row_to_manifest(row) if row is not None else None

    def producer_of(self, artifact_id: ArtifactId) -> ActivityManifest | None:
        artifact_id = ArtifactId(artifact_id)
        artifact_uuid = uuid.UUID(strip_prefix(artifact_id, "art_"))
        output_row = self._session.execute(
            sa.select(ActivityOutputRow).where(ActivityOutputRow.artifact_id == artifact_uuid)
        ).scalar_one_or_none()
        if output_row is None:
            return None
        row = self._session.get(ActivityRow, output_row.activity_id)
        return _activity_row_to_manifest(row) if row is not None else None

    def consumers_of(self, artifact_id: ArtifactId) -> tuple[ActivityManifest, ...]:
        artifact_id = ArtifactId(artifact_id)
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

    def get(self, run_id: RunId) -> RunManifest:
        run_id = RunId(run_id)
        row = self._session.get(RunRow, uuid.UUID(strip_prefix(run_id, "run_")))
        if row is None:
            raise NotFound(f"run {run_id!r} not found")
        return _run_row_to_manifest(row)


def _issued_forecast_row_to_record(row: IssuedForecastRow) -> IssuedForecastRecord:
    return IssuedForecastRecord.model_validate(
        {
            "schema_version": row.schema_version,
            "issued_forecast_id": row.issued_forecast_id,
            "batch_run_id": row.batch_run_id,
            "location_index": row.location_index,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "issued_at": row.issued_at,
            "target_reference_time": row.target_reference_time,
            "content_digest": Digest(row.content_digest),
        }
    )


class PostgresIssuedForecastRepository:
    """Append and read issued versions; migration 0004 rejects update and delete."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: IssuedForecastRecord) -> IssuedForecastRecord:
        record = IssuedForecastRecord.model_validate(record.model_dump())
        row = IssuedForecastRow(**record.model_dump())
        self._session.add(row)
        self._session.flush()
        return _issued_forecast_row_to_record(row)

    def get(self, issued_forecast_id: uuid.UUID) -> IssuedForecastRecord:
        if not isinstance(issued_forecast_id, uuid.UUID):
            raise TypeError("issued_forecast_id must be a UUID")
        row = self._session.get(IssuedForecastRow, issued_forecast_id)
        if row is None:
            raise NotFound(f"issued forecast {issued_forecast_id!r} not found")
        return _issued_forecast_row_to_record(row)

    def list_for_coordinate(
        self, latitude: float, longitude: float, *, limit: int | None = 100
    ) -> tuple[IssuedForecastRecord, ...]:
        # Explicit None permits complete history queries; existing callers stay bounded.
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
            raise ValueError("limit must be None or an integer between 1 and 1000")
        for value, bound in ((latitude, 90), (longitude, 180)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not -bound <= value <= bound
            ):
                raise ValueError("latitude and longitude must be finite geographic coordinates")
        rows = self._session.scalars(
            sa.select(IssuedForecastRow)
            .where(
                IssuedForecastRow.latitude == latitude,
                IssuedForecastRow.longitude == longitude,
            )
            .order_by(
                IssuedForecastRow.issued_at.desc(), IssuedForecastRow.issued_forecast_id.desc()
            )
            .limit(limit)
        )
        return tuple(_issued_forecast_row_to_record(row) for row in rows)


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
        self.issued_forecasts = PostgresIssuedForecastRepository(self._session)
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
