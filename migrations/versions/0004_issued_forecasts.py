"""Immutable issued coordinate forecasts linked to verified stored objects.

Revision ID: 0004_issued_forecasts
Revises: 0003_drop_legacy_selection_json
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_issued_forecasts"
down_revision: str | None = "0003_drop_legacy_selection_json"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issued_forecasts",
        sa.Column("issued_forecast_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column("batch_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("location_index", sa.Integer(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_reference_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "content_digest",
            sa.String(),
            sa.ForeignKey("stored_objects.content_digest"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version = 'issued-forecast.v1'", name="ck_issued_forecasts_schema_version"
        ),
        sa.CheckConstraint("location_index >= 0", name="ck_issued_forecasts_location_index"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_issued_forecasts_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_issued_forecasts_longitude"),
        sa.UniqueConstraint(
            "batch_run_id", "location_index", name="uq_issued_forecasts_batch_location"
        ),
    )
    op.create_index(
        "ix_issued_forecasts_coordinate_issue",
        "issued_forecasts",
        ["latitude", "longitude", "issued_at"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_issued_forecast_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'issued forecasts are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER issued_forecasts_immutable
        BEFORE UPDATE OR DELETE ON issued_forecasts
        FOR EACH ROW EXECUTE FUNCTION reject_issued_forecast_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("issued_forecasts")
    op.execute("DROP FUNCTION reject_issued_forecast_mutation()")
