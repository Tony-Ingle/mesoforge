"""Pure governance derivations: exact scopes, half-open intervals and append invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.common.identifiers import ArtifactId, Digest, GovernanceEventId
from mesoforge.contracts.policy_governance import (
    AI_DESK_POLICY,
    DESK_SCOPE,
    GOVERNANCE_POLICY,
    GOVERNANCE_POLICY_VERSION,
    TEMPERATURE_CORRECTION,
    GovernanceConflict,
    GovernanceEvent,
    blend_scope,
    correction_scope,
    emergency_target,
    governance_policy_digest,
    head_at,
    intervals,
    parse_scope,
    request_digest,
    request_key,
    shadow_candidates_at,
    validate_append,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
SCOPE = correction_scope(44.98861, -93.25553)
A = ArtifactId("art_00000000-0000-4000-8000-00000000000a")
B = ArtifactId("art_00000000-0000-4000-8000-00000000000b")
E = ArtifactId("art_00000000-0000-4000-8000-0000000000ee")
DIGEST = Digest.of_bytes(b"policy")
# Pinned so any wording or rule change is a deliberate new governance policy version.
PINNED_GOVERNANCE_POLICY_DIGEST = str(governance_policy_digest())


def event(events, event_type, *, policy=None, minutes=None, rolled_back=None, family=None):
    rows = [row for row in events if row.scope_key == SCOPE]
    head = head_at(rows)
    chain = event_type in ("ACTIVATED", "ROLLED_BACK", "EMERGENCY_ROLLED_BACK")
    key = request_key(n=len(events), kind=event_type)
    return GovernanceEvent(
        event_id=GovernanceEventId.generate(),
        family=family or TEMPERATURE_CORRECTION,
        scope_key=SCOPE if family != AI_DESK_POLICY else DESK_SCOPE,
        scope_seq=len(rows) + 1,
        event_type=event_type,
        policy_artifact_id=policy,
        policy_content_digest=DIGEST if policy else None,
        previous_head_event_id=head.event_id if chain and head else None,
        rolled_back_event_id=rolled_back,
        evaluation_artifact_id=E if event_type == "ACTIVATED" else None,
        governance_policy_version=GOVERNANCE_POLICY_VERSION,
        governance_policy_digest=governance_policy_digest(),
        actor="operator",
        reason="test",
        recorded_at=T0 + timedelta(minutes=minutes if minutes is not None else len(events) + 1),
        code_revision="a" * 40,
        environment_digest=Digest.of_bytes(b"env"),
        request_key=key,
        request_digest=request_digest(key, actor="operator", reason="test"),
    )


def append(events, event_type, **kwargs):
    row = event(events, event_type, **kwargs)
    validate_append(events, row)
    events.append(row)
    return row


def test_scope_keys_are_canonical_exact_coordinates_and_governance_policy_is_pinned():
    assert SCOPE == '{"field":"air_temperature_2m","latitude":44.98861,"longitude":-93.25553}'
    assert parse_scope(SCOPE)["latitude"] == 44.98861
    assert correction_scope(44.98861, -93.25553) != correction_scope(44.988610001, -93.25553)
    assert blend_scope("wind_10m") == '{"field":"wind_10m"}'
    with pytest.raises(ValueError):
        correction_scope(float("nan"), 0.0)
    with pytest.raises(ValueError):
        parse_scope('{"latitude": 1, "field": "x"}')
    assert GOVERNANCE_POLICY_VERSION == "mesoforge.policy-governance.v1"
    assert str(governance_policy_digest()) == PINNED_GOVERNANCE_POLICY_DIGEST
    assert (
        "No forecast job, AI desk or provider may append governance events."
        in (GOVERNANCE_POLICY["principles"])
    )


def test_event_shapes_reject_ai_activation_missing_evaluation_and_oversized_payloads():
    with pytest.raises(ValueError, match="AI desk"):
        event([], "ACTIVATED", policy=A, family=AI_DESK_POLICY)
    good = event([], "REGISTERED", policy=A)
    with pytest.raises(ValueError, match="evaluation"):
        GovernanceEvent.model_validate(
            {**good.model_dump(), "event_type": "ACTIVATED", "evaluation_artifact_id": None}
        )
    with pytest.raises(ValueError, match="compact"):
        GovernanceEvent.model_validate({**good.model_dump(), "payload": {"x": "y" * 40000}})
    with pytest.raises(ValueError, match="timezones"):
        GovernanceEvent.model_validate(
            {**good.model_dump(), "recorded_at": datetime(2026, 9, 1)}  # noqa: DTZ001
        )


def test_intervals_are_half_open_contiguous_and_derived_only_from_chain_events():
    events: list[GovernanceEvent] = []
    append(events, "REGISTERED", policy=A)
    append(events, "REGISTERED", policy=B)
    first = append(events, "ACTIVATED", policy=A)
    second = append(events, "ACTIVATED", policy=B)
    rollback = append(events, "ROLLED_BACK", policy=None, rolled_back=second.event_id)
    spans = intervals(events)
    assert [row.policy_artifact_id for row in spans] == [A, B, None]
    assert spans[0].effective_until == spans[1].effective_from == second.recorded_at
    assert spans[1].effective_until == rollback.recorded_at and spans[2].effective_until is None
    assert head_at(events, first.recorded_at).event_id == first.event_id
    assert head_at(events, second.recorded_at - timedelta(microseconds=1)) == first
    assert head_at(events, first.recorded_at - timedelta(microseconds=1)) is None
    # A superseded or rolled-back, non-retired policy resumes shadowing.
    assert {row.policy_artifact_id for row in shadow_candidates_at(events, T0 + timedelta(1))} == {
        A,
        B,
    }
    assert [row.policy_artifact_id for row in shadow_candidates_at(events, first.recorded_at)] == [
        B
    ]


def test_append_rules_mirror_the_database_trigger():
    events: list[GovernanceEvent] = []
    with pytest.raises(GovernanceConflict, match="candidate_not_registered"):
        append(events, "ACTIVATED", policy=A)
    append(events, "REGISTERED", policy=A)
    with pytest.raises(GovernanceConflict, match="already_registered"):
        append(events, "REGISTERED", policy=A)
    stale = event(events, "REGISTERED", policy=B)
    append(events, "REGISTERED", policy=B)
    with pytest.raises(GovernanceConflict, match="scope_seq_conflict"):
        validate_append(events, stale)
    activated = append(events, "ACTIVATED", policy=A)
    with pytest.raises(GovernanceConflict, match="retire_active_policy"):
        append(events, "RETIRED", policy=A)
    stale_chain = event(events, "ACTIVATED", policy=B)
    append(events, "ACTIVATED", policy=B)
    with pytest.raises(GovernanceConflict):
        validate_append(events, stale_chain.model_copy(update={"scope_seq": len(events) + 1}))
    with pytest.raises(GovernanceConflict, match="clock_regression"):
        append(events, "RETIRED", policy=A, minutes=0)
    with pytest.raises(GovernanceConflict, match="rollback_head_mismatch"):
        append(events, "ROLLED_BACK", policy=None, rolled_back=activated.event_id)


def test_emergency_restores_the_superseded_policy_and_refuses_after_a_rollback():
    events: list[GovernanceEvent] = []
    append(events, "REGISTERED", policy=A)
    append(events, "REGISTERED", policy=B)
    append(events, "ACTIVATED", policy=A)
    second = append(events, "ACTIVATED", policy=B)
    closed, target = emergency_target(events)
    assert closed == second and target == A
    restored = append(events, "EMERGENCY_ROLLED_BACK", policy=A, rolled_back=second.event_id)
    assert head_at(events) == restored
    with pytest.raises(GovernanceConflict, match="emergency_target_unsafe"):
        emergency_target(events)  # the head is a rollback: use an explicit target
    back = append(events, "ACTIVATED", policy=B)
    append(events, "ROLLED_BACK", policy=A, rolled_back=back.event_id)
    with pytest.raises(GovernanceConflict, match="emergency_target_unsafe"):
        emergency_target(events)  # never re-activates the policy just rolled back
    # A retired predecessor is never restored.
    events2: list[GovernanceEvent] = []
    append(events2, "REGISTERED", policy=A)
    append(events2, "REGISTERED", policy=B)
    first = append(events2, "ACTIVATED", policy=A)
    append(events2, "ACTIVATED", policy=B)
    append(events2, "RETIRED", policy=A)
    assert first.policy_artifact_id == A
    with pytest.raises(GovernanceConflict, match="emergency_target_unsafe"):
        emergency_target(events2)
    with pytest.raises(GovernanceConflict, match="rollback_target_unsafe"):
        append(events2, "ROLLED_BACK", policy=A, rolled_back=head_at(events2).event_id)
