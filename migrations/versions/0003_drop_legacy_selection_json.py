"""Backfill and remove the legacy unconstrained ``runs.selected_inputs``
JSONB column.

Codex review (t_f569c45c, finding 5) found that ``0002_run_selected_inputs``
added the relationally-integral ``run_selected_inputs`` join table but
left the legacy ``runs.selected_inputs`` JSONB column in place as an
unconstrained fallback that ``storage/postgres/repositories.py``
consulted whenever a run had no relational rows -- so a run could still
end up reporting its selected inputs from arbitrary, non-FK-checked
JSON. This migration removes that fail-open path structurally:

1. any pre-existing ``runs`` row whose ``run_selected_inputs`` join rows
   are missing/incomplete relative to its legacy
   ``selected_inputs->'artifact_ids'`` JSON is backfilled -- each
   legacy-only artifact ID is inserted into ``run_selected_inputs`` with
   its JSON array ordinal, but ONLY when the referenced artifact row
   actually exists (preserving the FK invariant; a dangling legacy ID
   that references a nonexistent artifact is intentionally left
   unmigrated rather than inserted as a broken row -- there is no
   Phase 0 production data, so this is a safety measure for any
   already-applied environment, not an expected code path);
2. the legacy ``selected_inputs`` column is dropped. After this
   migration, ``run_selected_inputs`` is the only place selected-input
   references live -- there is no unconstrained JSON to silently fall
   back to.

Revision ID: 0003_drop_legacy_selection_json
Revises: 0002_run_selected_inputs
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_drop_legacy_selection_json"
down_revision: str | None = "0002_run_selected_inputs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_runs = sa.table(
    "runs",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("selected_inputs", postgresql.JSONB()),
)
_run_selected_inputs = sa.table(
    "run_selected_inputs",
    sa.column("run_id", postgresql.UUID(as_uuid=True)),
    sa.column("ordinal", sa.SmallInteger()),
    sa.column("artifact_id", postgresql.UUID(as_uuid=True)),
)
_artifacts = sa.table("artifacts", sa.column("id", postgresql.UUID(as_uuid=True)))


def upgrade() -> None:
    connection = op.get_bind()

    rows = connection.execute(sa.select(_runs.c.id, _runs.c.selected_inputs)).fetchall()

    for run_id, selected_inputs in rows:
        legacy_ids = (selected_inputs or {}).get("artifact_ids") or []
        if not legacy_ids:
            continue

        existing_ordinals = {
            ordinal
            for (ordinal,) in connection.execute(
                sa.select(_run_selected_inputs.c.ordinal).where(
                    _run_selected_inputs.c.run_id == run_id
                )
            )
        }

        for ordinal, artifact_id in enumerate(legacy_ids):
            if ordinal in existing_ordinals:
                continue
            artifact_exists = connection.execute(
                sa.select(_artifacts.c.id).where(_artifacts.c.id == artifact_id)
            ).first()
            if artifact_exists is None:
                # Dangling legacy reference to a nonexistent artifact --
                # never insert a broken relational row; the run's
                # relationally-integral selection simply omits it.
                continue
            connection.execute(
                sa.insert(_run_selected_inputs).values(
                    run_id=run_id, ordinal=ordinal, artifact_id=artifact_id
                )
            )

    op.drop_column("runs", "selected_inputs")


def downgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("selected_inputs", postgresql.JSONB(), nullable=True),
    )
    connection = op.get_bind()
    run_ids = [row[0] for row in connection.execute(sa.select(_runs.c.id))]
    for run_id in run_ids:
        ordered_artifact_ids = [
            str(artifact_id)
            for (artifact_id,) in connection.execute(
                sa.select(_run_selected_inputs.c.artifact_id)
                .where(_run_selected_inputs.c.run_id == run_id)
                .order_by(_run_selected_inputs.c.ordinal)
            )
        ]
        connection.execute(
            sa.update(_runs)
            .where(_runs.c.id == run_id)
            .values(selected_inputs={"artifact_ids": ordered_artifact_ids})
        )
    op.alter_column("runs", "selected_inputs", nullable=False)
