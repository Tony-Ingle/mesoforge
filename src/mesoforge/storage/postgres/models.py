"""SQLAlchemy typed table mappings for the Phase 0 PostgreSQL schema
(plan Section 4.10).

SQLAlchemy ORM objects never escape repository adapters -- callers outside
``storage.postgres`` only ever see plain contract objects.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mesoforge.storage.postgres.database import Base


class ConfigurationSnapshotRow(Base):
    __tablename__ = "configuration_snapshots"

    id: Mapped[str] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(unique=True, nullable=False)
    schema_version: Mapped[str] = mapped_column(nullable=False)
    canonical_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_references: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.transaction_timestamp()
    )


class GridRow(Base):
    __tablename__ = "grids"

    id: Mapped[str] = mapped_column(primary_key=True)
    definition_digest: Mapped[str] = mapped_column(nullable=False)
    canonical_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.transaction_timestamp()
    )


class StoredObjectRow(Base):
    __tablename__ = "stored_objects"
    __table_args__ = (CheckConstraint("byte_size >= 0", name="ck_stored_objects_byte_size"),)

    content_digest: Mapped[str] = mapped_column(primary_key=True)
    storage_uri: Mapped[str] = mapped_column(unique=True, nullable=False)
    media_type: Mapped[str] = mapped_column(nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.transaction_timestamp()
    )


class IssuedForecastRow(Base):
    __tablename__ = "issued_forecasts"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'issued-forecast.v1'", name="ck_issued_forecasts_schema_version"
        ),
        CheckConstraint("location_index >= 0", name="ck_issued_forecasts_location_index"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_issued_forecasts_latitude"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_issued_forecasts_longitude"),
        CheckConstraint("forecast_horizon_hours IN (36, 120)", name="ck_issued_forecasts_horizon"),
        UniqueConstraint(
            "batch_run_id", "location_index", name="uq_issued_forecasts_batch_location"
        ),
        Index("ix_issued_forecasts_coordinate_issue", "latitude", "longitude", "issued_at"),
    )

    issued_forecast_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(nullable=False)
    batch_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    location_index: Mapped[int] = mapped_column(nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_reference_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    forecast_horizon_hours: Mapped[int] = mapped_column(nullable=False, server_default="36")
    forecast_payload_digest: Mapped[str | None] = mapped_column(nullable=True)
    content_digest: Mapped[str] = mapped_column(
        ForeignKey("stored_objects.content_digest"), nullable=False
    )


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(nullable=False)
    forecast_issue_time: Mapped[datetime] = mapped_column(nullable=False)
    information_cutoff: Mapped[datetime] = mapped_column(nullable=False)
    configuration_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("configuration_snapshots.id"), nullable=False
    )
    configuration_digest: Mapped[str] = mapped_column(nullable=False)
    code_revision: Mapped[str] = mapped_column(nullable=False)
    environment_digest: Mapped[str] = mapped_column(nullable=False)
    lockfile_digest: Mapped[str] = mapped_column(nullable=False)
    random_seed: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.transaction_timestamp()
    )

    selected_input_rows: Mapped[list[RunSelectedInputRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "(source_registration_digest IS NOT NULL AND source_authority IS NOT NULL "
            " AND source_locator IS NOT NULL AND source_revision IS NOT NULL) OR "
            "(source_registration_digest IS NULL AND source_authority IS NULL "
            " AND source_locator IS NULL AND source_revision IS NULL)",
            name="ck_artifacts_source_fields_all_or_nothing",
        ),
        CheckConstraint(
            "quality_state IN ('valid', 'partial', 'invalid')",
            name="ck_artifacts_quality_state",
        ),
        Index(
            "ix_artifacts_source_registration_digest_unique",
            "source_registration_digest",
            unique=True,
            postgresql_where=("source_registration_digest IS NOT NULL"),
        ),
        Index("ix_artifacts_run_id", "run_id"),
        Index("ix_artifacts_available_at", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(nullable=False)
    artifact_type: Mapped[str] = mapped_column(nullable=False)
    artifact_schema_version: Mapped[str] = mapped_column(nullable=False)
    content_digest: Mapped[str] = mapped_column(
        ForeignKey("stored_objects.content_digest"), nullable=False
    )
    source_registration_digest: Mapped[str | None] = mapped_column(nullable=True)
    source_authority: Mapped[str | None] = mapped_column(nullable=True)
    source_locator: Mapped[str | None] = mapped_column(nullable=True)
    source_revision: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.transaction_timestamp()
    )
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    availability_authority: Mapped[str] = mapped_column(nullable=False)
    availability_method: Mapped[str] = mapped_column(nullable=False)
    ingested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    configuration_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("configuration_snapshots.id"), nullable=False
    )
    configuration_digest: Mapped[str] = mapped_column(nullable=False)
    code_revision: Mapped[str] = mapped_column(nullable=False)
    environment_digest: Mapped[str] = mapped_column(nullable=False)
    quality_state: Mapped[str] = mapped_column(nullable=False)
    attributes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    stored_object: Mapped[StoredObjectRow] = relationship()


class ActivityRow(Base):
    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed')", name="ck_activities_status"
        ),
        Index(
            "ix_activities_idempotency_digest_succeeded_unique",
            "idempotency_digest",
            unique=True,
            postgresql_where="status = 'succeeded'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(nullable=False)
    activity_type: Mapped[str] = mapped_column(nullable=False)
    activity_version: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    idempotency_digest: Mapped[str] = mapped_column(nullable=False)
    parameters_digest: Mapped[str] = mapped_column(nullable=False)
    configuration_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("configuration_snapshots.id"), nullable=False
    )
    configuration_digest: Mapped[str] = mapped_column(nullable=False)
    code_revision: Mapped[str] = mapped_column(nullable=False)
    environment_digest: Mapped[str] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    inputs: Mapped[list[ActivityInputRow]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )
    outputs: Mapped[list[ActivityOutputRow]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )


class ActivityInputRow(Base):
    __tablename__ = "activity_inputs"
    __table_args__ = (UniqueConstraint("activity_id", "role"),)

    activity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("activities.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    role: Mapped[str] = mapped_column(nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id"), nullable=False
    )

    activity: Mapped[ActivityRow] = relationship(back_populates="inputs")


class ActivityOutputRow(Base):
    __tablename__ = "activity_outputs"
    __table_args__ = (UniqueConstraint("activity_id", "role"),)

    activity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("activities.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    role: Mapped[str] = mapped_column(nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id"), unique=True, nullable=False
    )

    activity: Mapped[ActivityRow] = relationship(back_populates="outputs")


class RunSelectedInputRow(Base):
    """Selected-input references for a run, with real foreign-key
    relational integrity and preserved ordinal order (plan Section 4.8;
    Codex review t_9bb13e2b finding 5: run creation must persist
    selected input references with relational integrity, not
    unconstrained JSON)."""

    __tablename__ = "run_selected_inputs"
    __table_args__ = (UniqueConstraint("run_id", "artifact_id"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id"), nullable=False
    )

    run: Mapped[RunRow] = relationship(back_populates="selected_input_rows")


_GOVERNANCE_CHAIN = "('ACTIVATED', 'ROLLED_BACK', 'EMERGENCY_ROLLED_BACK')"


class GovernanceEventRow(Base):
    """Append-only policy-governance event; migration 0005 owns the insert trigger."""

    __tablename__ = "governance_events"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'mesoforge.policy-governance-event.v1'",
            name="ck_governance_events_schema_version",
        ),
        CheckConstraint(
            "family IN ('temperature_correction', 'blend_policy', 'ai_desk_policy')",
            name="ck_governance_events_family",
        ),
        CheckConstraint(
            "event_type IN ('REGISTERED', 'ELIGIBILITY_EVALUATED', 'ACTIVATED', "
            "'ROLLED_BACK', 'EMERGENCY_ROLLED_BACK', 'RETIRED')",
            name="ck_governance_events_event_type",
        ),
        CheckConstraint("scope_seq >= 1", name="ck_governance_events_scope_seq"),
        CheckConstraint(
            "char_length(scope_key) BETWEEN 2 AND 512", name="ck_governance_events_scope_key"
        ),
        CheckConstraint(
            "(policy_artifact_id IS NULL) = (policy_content_digest IS NULL)",
            name="ck_governance_events_policy_identity",
        ),
        CheckConstraint(
            "policy_artifact_id IS NOT NULL OR event_type IN ('ROLLED_BACK', "
            "'EMERGENCY_ROLLED_BACK')",
            name="ck_governance_events_policy_required",
        ),
        CheckConstraint(
            f"previous_head_event_id IS NULL OR event_type IN {_GOVERNANCE_CHAIN}",
            name="ck_governance_events_previous_head",
        ),
        CheckConstraint(
            "(rolled_back_event_id IS NOT NULL) = "
            "(event_type IN ('ROLLED_BACK', 'EMERGENCY_ROLLED_BACK'))",
            name="ck_governance_events_rolled_back",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('eligible', 'not_eligible')",
            name="ck_governance_events_decision",
        ),
        CheckConstraint(
            "(event_type = 'ELIGIBILITY_EVALUATED') = (decision IS NOT NULL) AND "
            "(event_type <> 'ELIGIBILITY_EVALUATED' OR (evaluation_artifact_id IS NOT NULL "
            "AND information_cutoff IS NOT NULL)) AND "
            "(evaluation_artifact_id IS NULL OR event_type IN "
            "('ELIGIBILITY_EVALUATED', 'ACTIVATED')) AND "
            "(event_type <> 'ACTIVATED' OR evaluation_artifact_id IS NOT NULL)",
            name="ck_governance_events_evaluation",
        ),
        CheckConstraint(
            "family <> 'ai_desk_policy' OR event_type IN ('REGISTERED', 'RETIRED')",
            name="ck_governance_events_ai_desk",
        ),
        CheckConstraint("char_length(actor) BETWEEN 1 AND 200", name="ck_governance_events_actor"),
        CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 2000", name="ck_governance_events_reason"
        ),
        CheckConstraint(
            "char_length(code_revision) = 40", name="ck_governance_events_code_revision"
        ),
        CheckConstraint(
            "payload IS NULL OR octet_length(payload::text) <= 32768",
            name="ck_governance_events_payload_size",
        ),
        UniqueConstraint("family", "scope_key", "scope_seq", name="uq_governance_events_scope_seq"),
        UniqueConstraint("request_key", name="uq_governance_events_request_key"),
        Index(
            "ix_governance_events_registered_policy",
            "policy_artifact_id",
            unique=True,
            postgresql_where="event_type = 'REGISTERED'",
        ),
        Index(
            "ix_governance_events_retired_policy",
            "policy_artifact_id",
            unique=True,
            postgresql_where="event_type = 'RETIRED'",
        ),
        Index("ix_governance_events_scope_recorded", "family", "scope_key", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[str] = mapped_column(nullable=False)
    family: Mapped[str] = mapped_column(nullable=False)
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope_seq: Mapped[int] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    policy_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id"), nullable=True
    )
    policy_content_digest: Mapped[str | None] = mapped_column(nullable=True)
    previous_head_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("governance_events.id"), nullable=True
    )
    rolled_back_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("governance_events.id"), nullable=True
    )
    evaluation_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("artifacts.id"), nullable=True
    )
    decision: Mapped[str | None] = mapped_column(nullable=True)
    information_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    governance_policy_version: Mapped[str] = mapped_column(nullable=False)
    governance_policy_digest: Mapped[str] = mapped_column(nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    code_revision: Mapped[str] = mapped_column(nullable=False)
    environment_digest: Mapped[str] = mapped_column(nullable=False)
    request_key: Mapped[str] = mapped_column(nullable=False)
    request_digest: Mapped[str] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
