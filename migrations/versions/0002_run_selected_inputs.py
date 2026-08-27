"""Run selected-input references with relational integrity.

Codex review (t_9bb13e2b, finding 5) found run creation persisted
selected input artifact IDs as unconstrained JSON on ``runs.selected_inputs``,
with no foreign-key relational integrity and no protection against a
tampered/dangling selection surviving into the database. This revision
adds ``run_selected_inputs``, a proper join table with foreign keys to
both ``runs`` and ``artifacts`` and an ordinal column that preserves
selection order (plan Section 4.8's "preserving selection order"
requirement, mirroring ``activity_inputs``/``activity_outputs``).

The legacy ``runs.selected_inputs`` JSONB column is retained (nullable
is not changed) for backward read compatibility with any already-written
rows, but new writes should populate ``run_selected_inputs`` and
repository reads reconstruct ``selected_input_artifact_ids`` from it.

Revision ID: 0002_run_selected_inputs
Revises: 0001_phase0_foundations
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_run_selected_inputs"
down_revision: str | None = "0001_phase0_foundations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_selected_inputs",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.SmallInteger(), primary_key=True),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "artifact_id"),
    )
    op.create_index("ix_run_selected_inputs_artifact_id", "run_selected_inputs", ["artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_run_selected_inputs_artifact_id", table_name="run_selected_inputs")
    op.drop_table("run_selected_inputs")
