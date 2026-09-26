"""Append-only policy-governance events with database-enforced lifecycle invariants.

Effective intervals are derived: a chain event (ACTIVATED, ROLLED_BACK,
EMERGENCY_ROLLED_BACK) is in force from its recorded_at until the next chain event of
the same scope. recorded_at is stamped from the database clock after the family's
transaction-scoped advisory lock, so readers holding the shared lock observe every
event recorded at or before their decision time.

Revision ID: 0005_policy_governance
Revises: 0004_issued_forecasts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_policy_governance"
down_revision: str | None = "0004_issued_forecasts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_CHAIN = "('ACTIVATED', 'ROLLED_BACK', 'EMERGENCY_ROLLED_BACK')"


def upgrade() -> None:
    op.create_table(
        "governance_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column("family", sa.String(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("scope_seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column(
            "policy_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=True,
        ),
        sa.Column("policy_content_digest", sa.String(), nullable=True),
        sa.Column(
            "previous_head_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("governance_events.id"),
            nullable=True,
        ),
        sa.Column(
            "rolled_back_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("governance_events.id"),
            nullable=True,
        ),
        sa.Column(
            "evaluation_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id"),
            nullable=True,
        ),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("information_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("governance_policy_version", sa.String(), nullable=False),
        sa.Column("governance_policy_digest", sa.String(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("code_revision", sa.String(), nullable=False),
        sa.Column("environment_digest", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("request_digest", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "schema_version = 'mesoforge.policy-governance-event.v1'",
            name="ck_governance_events_schema_version",
        ),
        sa.CheckConstraint(
            "family IN ('temperature_correction', 'blend_policy', 'ai_desk_policy')",
            name="ck_governance_events_family",
        ),
        sa.CheckConstraint(
            "event_type IN ('REGISTERED', 'ELIGIBILITY_EVALUATED', 'ACTIVATED', "
            "'ROLLED_BACK', 'EMERGENCY_ROLLED_BACK', 'RETIRED')",
            name="ck_governance_events_event_type",
        ),
        sa.CheckConstraint("scope_seq >= 1", name="ck_governance_events_scope_seq"),
        sa.CheckConstraint(
            "char_length(scope_key) BETWEEN 2 AND 512", name="ck_governance_events_scope_key"
        ),
        sa.CheckConstraint(
            "(policy_artifact_id IS NULL) = (policy_content_digest IS NULL)",
            name="ck_governance_events_policy_identity",
        ),
        sa.CheckConstraint(
            "policy_artifact_id IS NOT NULL OR event_type IN ('ROLLED_BACK', "
            "'EMERGENCY_ROLLED_BACK')",
            name="ck_governance_events_policy_required",
        ),
        sa.CheckConstraint(
            f"previous_head_event_id IS NULL OR event_type IN {_CHAIN}",
            name="ck_governance_events_previous_head",
        ),
        sa.CheckConstraint(
            "(rolled_back_event_id IS NOT NULL) = "
            "(event_type IN ('ROLLED_BACK', 'EMERGENCY_ROLLED_BACK'))",
            name="ck_governance_events_rolled_back",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('eligible', 'not_eligible')",
            name="ck_governance_events_decision",
        ),
        sa.CheckConstraint(
            "(event_type = 'ELIGIBILITY_EVALUATED') = (decision IS NOT NULL) AND "
            "(event_type <> 'ELIGIBILITY_EVALUATED' OR (evaluation_artifact_id IS NOT NULL "
            "AND information_cutoff IS NOT NULL)) AND "
            "(evaluation_artifact_id IS NULL OR event_type IN "
            "('ELIGIBILITY_EVALUATED', 'ACTIVATED')) AND "
            "(event_type <> 'ACTIVATED' OR evaluation_artifact_id IS NOT NULL)",
            name="ck_governance_events_evaluation",
        ),
        sa.CheckConstraint(
            "family <> 'ai_desk_policy' OR event_type IN ('REGISTERED', 'RETIRED')",
            name="ck_governance_events_ai_desk",
        ),
        sa.CheckConstraint(
            "char_length(actor) BETWEEN 1 AND 200", name="ck_governance_events_actor"
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 2000", name="ck_governance_events_reason"
        ),
        sa.CheckConstraint(
            "char_length(code_revision) = 40", name="ck_governance_events_code_revision"
        ),
        sa.CheckConstraint(
            "payload IS NULL OR octet_length(payload::text) <= 32768",
            name="ck_governance_events_payload_size",
        ),
        sa.UniqueConstraint(
            "family", "scope_key", "scope_seq", name="uq_governance_events_scope_seq"
        ),
        sa.UniqueConstraint("request_key", name="uq_governance_events_request_key"),
    )
    op.create_index(
        "ix_governance_events_registered_policy",
        "governance_events",
        ["policy_artifact_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'REGISTERED'"),
    )
    op.create_index(
        "ix_governance_events_retired_policy",
        "governance_events",
        ["policy_artifact_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'RETIRED'"),
    )
    op.create_index(
        "ix_governance_events_scope_recorded",
        "governance_events",
        ["family", "scope_key", "recorded_at"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION governance_lock_key(family text) RETURNS bigint
        LANGUAGE sql IMMUTABLE AS $$
            SELECT hashtextextended('mesoforge.policy-governance.v1:' || family, 0)
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION governance_event_before_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            last_seq integer;
            last_at timestamptz;
            head governance_events%ROWTYPE;
            has_head boolean;
            prior_policy uuid;
            is_registered boolean;
            is_retired boolean;
            ref_id uuid;
            ref_digest text;
            ref_registered timestamptz;
            ref_available timestamptz;
        BEGIN
            PERFORM pg_advisory_xact_lock(governance_lock_key(NEW.family));
            NEW.recorded_at := clock_timestamp();
            SELECT max(scope_seq), max(recorded_at) INTO last_seq, last_at
              FROM governance_events
             WHERE family = NEW.family AND scope_key = NEW.scope_key;
            IF NEW.scope_seq <> coalesce(last_seq, 0) + 1 THEN
                RAISE EXCEPTION 'governance:scope_seq_conflict' USING ERRCODE = '23000';
            END IF;
            IF last_at IS NOT NULL AND NEW.recorded_at <= last_at THEN
                RAISE EXCEPTION 'governance:clock_regression' USING ERRCODE = '23000';
            END IF;
            SELECT * INTO head FROM governance_events
             WHERE family = NEW.family AND scope_key = NEW.scope_key
               AND event_type IN ('ACTIVATED', 'ROLLED_BACK', 'EMERGENCY_ROLLED_BACK')
             ORDER BY scope_seq DESC LIMIT 1;
            has_head := FOUND;
            IF NEW.event_type IN ('ACTIVATED', 'ROLLED_BACK', 'EMERGENCY_ROLLED_BACK')
                    AND NEW.previous_head_event_id IS DISTINCT FROM
                    (CASE WHEN has_head THEN head.id END) THEN
                RAISE EXCEPTION 'governance:stale_head' USING ERRCODE = '23000';
            END IF;
            is_registered := NEW.policy_artifact_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM governance_events
                 WHERE family = NEW.family AND scope_key = NEW.scope_key
                   AND event_type = 'REGISTERED'
                   AND policy_artifact_id = NEW.policy_artifact_id);
            is_retired := NEW.policy_artifact_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM governance_events
                 WHERE family = NEW.family AND scope_key = NEW.scope_key
                   AND event_type = 'RETIRED'
                   AND policy_artifact_id = NEW.policy_artifact_id);
            IF NEW.event_type = 'REGISTERED' AND is_registered THEN
                RAISE EXCEPTION 'governance:already_registered' USING ERRCODE = '23000';
            ELSIF NEW.event_type = 'ELIGIBILITY_EVALUATED'
                    AND (NOT is_registered OR is_retired) THEN
                RAISE EXCEPTION 'governance:candidate_not_registered_or_retired'
                    USING ERRCODE = '23000';
            ELSIF NEW.event_type = 'ACTIVATED' AND NOT is_registered THEN
                RAISE EXCEPTION 'governance:candidate_not_registered' USING ERRCODE = '23000';
            ELSIF NEW.event_type = 'ACTIVATED' AND is_retired THEN
                RAISE EXCEPTION 'governance:candidate_retired' USING ERRCODE = '23000';
            ELSIF NEW.event_type = 'ROLLED_BACK' THEN
                IF NOT has_head OR NEW.rolled_back_event_id IS DISTINCT FROM head.id THEN
                    RAISE EXCEPTION 'governance:rollback_head_mismatch' USING ERRCODE = '23000';
                END IF;
                IF NEW.policy_artifact_id IS NOT NULL AND (is_retired OR NOT EXISTS (
                        SELECT 1 FROM governance_events
                         WHERE family = NEW.family AND scope_key = NEW.scope_key
                           AND event_type = 'ACTIVATED'
                           AND policy_artifact_id = NEW.policy_artifact_id)) THEN
                    RAISE EXCEPTION 'governance:rollback_target_unsafe' USING ERRCODE = '23000';
                END IF;
            ELSIF NEW.event_type = 'EMERGENCY_ROLLED_BACK' THEN
                IF NOT has_head OR head.event_type <> 'ACTIVATED'
                        OR NEW.rolled_back_event_id IS DISTINCT FROM head.id THEN
                    RAISE EXCEPTION 'governance:emergency_target_unsafe'
                        USING ERRCODE = '23000';
                END IF;
                SELECT policy_artifact_id INTO prior_policy FROM governance_events
                 WHERE id = head.previous_head_event_id;
                IF NEW.policy_artifact_id IS DISTINCT FROM prior_policy OR is_retired THEN
                    RAISE EXCEPTION 'governance:emergency_target_unsafe'
                        USING ERRCODE = '23000';
                END IF;
            ELSIF NEW.event_type = 'RETIRED' THEN
                IF NOT is_registered THEN
                    RAISE EXCEPTION 'governance:candidate_not_registered'
                        USING ERRCODE = '23000';
                END IF;
                IF has_head AND head.policy_artifact_id IS NOT DISTINCT FROM
                        NEW.policy_artifact_id THEN
                    RAISE EXCEPTION 'governance:retire_active_policy' USING ERRCODE = '23000';
                END IF;
            END IF;
            FOREACH ref_id IN ARRAY ARRAY[NEW.policy_artifact_id, NEW.evaluation_artifact_id]
            LOOP
                IF ref_id IS NOT NULL THEN
                    SELECT content_digest, registered_at, available_at
                      INTO ref_digest, ref_registered, ref_available
                      FROM artifacts WHERE id = ref_id;
                    IF NOT FOUND OR ref_registered > NEW.recorded_at
                            OR ref_available > NEW.recorded_at THEN
                        RAISE EXCEPTION 'governance:artifact_not_available'
                            USING ERRCODE = '23000';
                    END IF;
                    IF ref_id = NEW.policy_artifact_id
                            AND ref_digest <> NEW.policy_content_digest THEN
                        RAISE EXCEPTION 'governance:policy_digest_mismatch'
                            USING ERRCODE = '23000';
                    END IF;
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_governance_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'governance events are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_events_before_insert
        BEFORE INSERT ON governance_events
        FOR EACH ROW EXECUTE FUNCTION governance_event_before_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_events_immutable
        BEFORE UPDATE OR DELETE ON governance_events
        FOR EACH ROW EXECUTE FUNCTION reject_governance_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER governance_events_no_truncate
        BEFORE TRUNCATE ON governance_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_governance_event_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("governance_events")
    op.execute("DROP FUNCTION reject_governance_event_mutation()")
    op.execute("DROP FUNCTION governance_event_before_insert()")
    op.execute("DROP FUNCTION governance_lock_key(text)")
