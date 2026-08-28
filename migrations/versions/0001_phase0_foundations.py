"""Phase 0 foundations schema.

Revision ID: 0001_phase0_foundations
Revises:
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase0_foundations"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "configuration_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("digest", sa.Text(), nullable=False, unique=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("canonical_json", postgresql.JSONB(), nullable=False),
        sa.Column("source_references", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
    )

    op.create_table(
        "grids",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("definition_digest", sa.Text(), nullable=False),
        sa.Column("canonical_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
    )

    op.create_table(
        "stored_objects",
        sa.Column("content_digest", sa.Text(), primary_key=True),
        sa.Column("storage_uri", sa.Text(), nullable=False, unique=True),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_stored_objects_byte_size"),
    )

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("forecast_issue_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("information_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "configuration_snapshot_id",
            sa.Text(),
            sa.ForeignKey("configuration_snapshots.id"),
            nullable=False,
        ),
        sa.Column("configuration_digest", sa.Text(), nullable=False),
        sa.Column("code_revision", sa.CHAR(40), nullable=False),
        sa.Column("environment_digest", sa.Text(), nullable=False),
        sa.Column("lockfile_digest", sa.Text(), nullable=False),
        sa.Column("random_seed", sa.BigInteger(), nullable=False),
        sa.Column("selected_inputs", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("artifact_type", sa.Text(), nullable=False),
        sa.Column("artifact_schema_version", sa.Text(), nullable=False),
        sa.Column(
            "content_digest",
            sa.Text(),
            sa.ForeignKey("stored_objects.content_digest"),
            nullable=False,
        ),
        sa.Column("source_registration_digest", sa.Text(), nullable=True),
        sa.Column("source_authority", sa.Text(), nullable=True),
        sa.Column("source_locator", sa.Text(), nullable=True),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("transaction_timestamp()"),
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability_authority", sa.Text(), nullable=False),
        sa.Column("availability_method", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column(
            "configuration_snapshot_id",
            sa.Text(),
            sa.ForeignKey("configuration_snapshots.id"),
            nullable=False,
        ),
        sa.Column("configuration_digest", sa.Text(), nullable=False),
        sa.Column("code_revision", sa.CHAR(40), nullable=False),
        sa.Column("environment_digest", sa.Text(), nullable=False),
        sa.Column("quality_state", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "(source_registration_digest IS NOT NULL AND source_authority IS NOT NULL "
            " AND source_locator IS NOT NULL AND source_revision IS NOT NULL) OR "
            "(source_registration_digest IS NULL AND source_authority IS NULL "
            " AND source_locator IS NULL AND source_revision IS NULL)",
            name="ck_artifacts_source_fields_all_or_nothing",
        ),
        sa.CheckConstraint(
            "quality_state IN ('valid', 'partial', 'invalid')",
            name="ck_artifacts_quality_state",
        ),
    )
    op.create_index(
        "ix_artifacts_source_registration_digest_unique",
        "artifacts",
        ["source_registration_digest"],
        unique=True,
        postgresql_where=sa.text("source_registration_digest IS NOT NULL"),
    )
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_index("ix_artifacts_available_at", "artifacts", ["available_at"])

    op.create_table(
        "activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("activity_type", sa.Text(), nullable=False),
        sa.Column("activity_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_digest", sa.Text(), nullable=False),
        sa.Column("parameters_digest", sa.Text(), nullable=False),
        sa.Column(
            "configuration_snapshot_id",
            sa.Text(),
            sa.ForeignKey("configuration_snapshots.id"),
            nullable=False,
        ),
        sa.Column("configuration_digest", sa.Text(), nullable=False),
        sa.Column("code_revision", sa.CHAR(40), nullable=False),
        sa.Column("environment_digest", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed')", name="ck_activities_status"
        ),
    )
    op.create_index(
        "ix_activities_idempotency_digest_succeeded_unique",
        "activities",
        ["idempotency_digest"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )

    op.create_table(
        "activity_inputs",
        sa.Column(
            "activity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("activities.id"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.SmallInteger(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=False,
        ),
        sa.UniqueConstraint("activity_id", "role"),
    )
    op.create_index("ix_activity_inputs_artifact_id", "activity_inputs", ["artifact_id"])

    op.create_table(
        "activity_outputs",
        sa.Column(
            "activity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("activities.id"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.SmallInteger(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=False,
            unique=True,
        ),
        sa.UniqueConstraint("activity_id", "role"),
    )
    op.create_index("ix_activity_outputs_artifact_id", "activity_outputs", ["artifact_id"])


def downgrade() -> None:
    op.drop_table("activity_outputs")
    op.drop_table("activity_inputs")
    op.drop_index("ix_activities_idempotency_digest_succeeded_unique", table_name="activities")
    op.drop_table("activities")
    op.drop_index("ix_artifacts_available_at", table_name="artifacts")
    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_index("ix_artifacts_source_registration_digest_unique", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_table("runs")
    op.drop_table("stored_objects")
    op.drop_table("grids")
    op.drop_table("configuration_snapshots")
