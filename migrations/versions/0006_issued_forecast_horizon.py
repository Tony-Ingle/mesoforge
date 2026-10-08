"""Retain each immutable issuance's scientific duration for metadata-only selection.

Revision ID: 0006_issued_forecast_horizon
Revises: 0005_policy_governance

All pre-migration issuances were constrained to 36 hours. Adding the constant
default preserves their actual coverage without rewriting their object payloads.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_issued_forecast_horizon"
down_revision: str | None = "0005_policy_governance"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "issued_forecasts",
        sa.Column("forecast_horizon_hours", sa.Integer(), nullable=False, server_default="36"),
    )
    op.create_check_constraint(
        "ck_issued_forecasts_horizon", "issued_forecasts", "forecast_horizon_hours IN (36, 120)"
    )
    op.add_column(
        "issued_forecasts", sa.Column("forecast_payload_digest", sa.String(), nullable=True)
    )


def downgrade() -> None:
    # Older code would silently truncate long-issuance verification opportunities.
    # Refuse that downgrade; scientific history must not be deleted to force it.
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM issued_forecasts
                WHERE forecast_horizon_hours <> 36 OR forecast_payload_digest IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Cannot remove horizon metadata while long issuances exist';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_issued_forecasts_horizon", "issued_forecasts", type_="check")
    op.drop_column("issued_forecasts", "forecast_payload_digest")
    op.drop_column("issued_forecasts", "forecast_horizon_hours")
