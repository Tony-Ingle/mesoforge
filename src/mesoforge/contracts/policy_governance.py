"""Append-only governance of persistent forecast policies; pure contract and derivations.

Immutable policy artifacts stay the scientific body. Lifecycle is a per-scope chain of
immutable events: a policy is registered (explicit shadow enablement), evaluated,
activated, rolled back or retired only by a new event. Nothing here executes science,
reads storage or promotes anything. The PostgreSQL trigger in migration 0005 enforces
the same append rules as :func:`validate_append`, which the in-memory double and the
service prechecks reuse, so the invariants cannot diverge silently.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mesoforge.common.errors import Conflict, MesoForgeError
from mesoforge.common.identifiers import (
    ArtifactId,
    Digest,
    GovernanceEventId,
    validate_code_revision,
)
from mesoforge.contracts.serialization import canonical_json_bytes

GOVERNANCE_EVENT_SCHEMA = "mesoforge.policy-governance-event.v1"
RESOLUTION_SCHEMA = "mesoforge.governance-resolution.v1"
TEMPERATURE_CORRECTION = "temperature_correction"
BLEND_POLICY = "blend_policy"
AI_DESK_POLICY = "ai_desk_policy"
FAMILIES = (TEMPERATURE_CORRECTION, BLEND_POLICY, AI_DESK_POLICY)
REGISTERED = "REGISTERED"
ELIGIBILITY_EVALUATED = "ELIGIBILITY_EVALUATED"
ACTIVATED = "ACTIVATED"
ROLLED_BACK = "ROLLED_BACK"
EMERGENCY_ROLLED_BACK = "EMERGENCY_ROLLED_BACK"
RETIRED = "RETIRED"
EVENT_TYPES = (
    REGISTERED,
    ELIGIBILITY_EVALUATED,
    ACTIVATED,
    ROLLED_BACK,
    EMERGENCY_ROLLED_BACK,
    RETIRED,
)
CHAIN_EVENTS = frozenset({ACTIVATED, ROLLED_BACK, EMERGENCY_ROLLED_BACK})
ROLLBACK_EVENTS = frozenset({ROLLED_BACK, EMERGENCY_ROLLED_BACK})
DESK_SCOPE = canonical_json_bytes({"desk": "global"}).decode("utf-8")
GENESIS = "genesis"
MAX_PAYLOAD_BYTES = 32768
# Readers and writers wait at most this long for the governance xact lock. It is an
# execution control for availability, not forecast science.
GOVERNANCE_LOCK_TIMEOUT_SECONDS = 10.0

# Versioned lifecycle rules. Scientific eligibility rules live in
# verification/governance_eligibility.py and are referenced by id; nothing here is a
# threshold. Any wording/rule change requires a new version (digest pinned in tests).
GOVERNANCE_POLICY: dict[str, Any] = {
    "id": "mesoforge.policy-governance",
    "version": "1",
    "eligibility_policy_id": "mesoforge-governance-eligibility.v1",
    "principles": [
        "MesoForge may learn, evaluate and determine eligibility automatically.",
        "Persistent scientific behavior changes only through an explicit governance event.",
        "No forecast job, AI desk or provider may append governance events.",
        "Eligibility is recorded evidence; activation is a separate explicit operator act.",
        "History is append-only; rollback and retirement are new events.",
    ],
    "families": {
        TEMPERATURE_CORRECTION: {
            "scope": "exact configured coordinate and field",
            "policy_schema": "mesoforge.temperature-correction-policy.v1",
            "governed_payload_role": "candidate",
            "activation": "explicit, exact candidate and eligible evaluation, expected head",
            "execution": "LearningService.local_stage through apply_temperature_correction",
        },
        BLEND_POLICY: {
            "scope": "field",
            "policy_schema": "mesoforge.candidate-blend-policy.v1",
            "governed_payload_role": "candidate",
            "activation": (
                "mechanism only: no approved blend promotion rule exists; table fields with "
                "leads 1..36 only; temperature recipes are never activated"
            ),
            "execution": "build_baseline through FieldBlendEngine policy_overrides",
        },
        AI_DESK_POLICY: {
            "scope": "global",
            "policy_schema": "mesoforge.forecast-desk-version.v1",
            "governed_payload_role": "code_versioned",
            "activation": "not supported: the desk policy is code-versioned and always attempted",
            "execution": "unchanged; AI issuance never depends on governance state",
        },
    },
    "lifecycle": {
        "register": "explicit operator act; enables prospective shadow evaluation",
        "evaluate": "deterministic, explicit information cutoff; records eligibility",
        "activate": "eligible evaluation for exactly this candidate; head unchanged since",
        "rollback": "to a previously active non-retired policy or to none; no evidence",
        "emergency_rollback": (
            "restores the policy superseded by the current ACTIVATED head; refused when "
            "the head is a rollback or the target is retired"
        ),
        "retire": "never the current head; retired policies never execute again",
    },
    "time": (
        "recorded_at is the only governance time: database clock after the exclusive "
        "transaction lock; effective intervals are half-open [recorded_at, next chain "
        "event recorded_at); readers hold the shared lock and require decision time <= "
        "database now"
    ),
}


GOVERNANCE_POLICY_VERSION = f"{GOVERNANCE_POLICY['id']}.v{GOVERNANCE_POLICY['version']}"


def governance_policy_digest() -> Digest:
    return Digest.of_bytes(canonical_json_bytes(GOVERNANCE_POLICY))


class GovernanceConflict(Conflict):
    """A governance precondition or database invariant refused the event."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if message and message != code else code)
        self.code = code


class GovernanceUnavailableError(MesoForgeError):
    """The governed state at a decision time cannot be proven; nothing may issue under it."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if message and message != code else code)
        self.code = code


class GovernanceBlockedError(MesoForgeError):
    """One location must not issue: its governed state failed or was revoked in flight."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message}" if message and message != code else code)
        self.code = code


def _finite_coordinate(value: float, bound: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > bound:
        raise ValueError(f"Governance scope {name} must be a finite coordinate")
    return float(value)


def correction_scope(latitude: float, longitude: float) -> str:
    """Exact configured floats, matching the correction coordinate equality rule."""
    return canonical_json_bytes(
        {
            "field": "air_temperature_2m",
            "latitude": _finite_coordinate(latitude, 90, "latitude"),
            "longitude": _finite_coordinate(longitude, 180, "longitude"),
        }
    ).decode("utf-8")


def blend_scope(field: str) -> str:
    if not isinstance(field, str) or not field:
        raise ValueError("Blend governance scope requires a field")
    return canonical_json_bytes({"field": field}).decode("utf-8")


def parse_scope(scope_key: str) -> dict[str, Any]:
    value = json.loads(scope_key)
    if not isinstance(value, dict) or canonical_json_bytes(value).decode("utf-8") != scope_key:
        raise ValueError("Governance scope key must be canonical JSON")
    return value


def request_key(**parts: Any) -> Digest:
    """Operation identity used for retry safety; actor and reason are excluded."""
    return Digest.of_bytes(canonical_json_bytes({"governance_request": parts}))


def request_digest(key: Digest, *, actor: str, reason: str) -> Digest:
    return Digest.of_bytes(
        canonical_json_bytes({"request_key": str(key), "actor": actor, "reason": reason})
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Governance times require explicit timezones")
    return value.astimezone(UTC)


class GovernanceEvent(BaseModel):
    """One immutable lifecycle event. recorded_at is assigned by the database clock."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["mesoforge.policy-governance-event.v1"] = (
        "mesoforge.policy-governance-event.v1"
    )
    event_id: GovernanceEventId
    family: Literal["temperature_correction", "blend_policy", "ai_desk_policy"]
    scope_key: str = Field(min_length=2, max_length=512)
    scope_seq: int = Field(ge=1)
    event_type: Literal[
        "REGISTERED",
        "ELIGIBILITY_EVALUATED",
        "ACTIVATED",
        "ROLLED_BACK",
        "EMERGENCY_ROLLED_BACK",
        "RETIRED",
    ]
    policy_artifact_id: ArtifactId | None = None
    policy_content_digest: Digest | None = None
    previous_head_event_id: GovernanceEventId | None = None
    rolled_back_event_id: GovernanceEventId | None = None
    evaluation_artifact_id: ArtifactId | None = None
    decision: Literal["eligible", "not_eligible"] | None = None
    information_cutoff: datetime | None = None
    governance_policy_version: str = Field(min_length=1, max_length=64)
    governance_policy_digest: Digest
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    recorded_at: datetime | None = None
    code_revision: str
    environment_digest: Digest
    request_key: Digest
    request_digest: Digest
    payload: dict[str, Any] | None = None

    @field_validator("information_cutoff", "recorded_at")
    @classmethod
    def _times(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @field_validator("code_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_code_revision(value)

    @field_validator("actor", "reason")
    @classmethod
    def _text(cls, value: str) -> str:
        if value != value.strip() or any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            raise ValueError("Governance actor/reason must be trimmed printable text")
        return value

    @model_validator(mode="after")
    def _shape(self) -> GovernanceEvent:
        parse_scope(self.scope_key)
        if self.payload is not None and len(canonical_json_bytes(self.payload)) > (
            MAX_PAYLOAD_BYTES
        ):
            raise ValueError("Governance event payload exceeds its compact bound")
        needs_policy = self.event_type in {REGISTERED, ELIGIBILITY_EVALUATED, ACTIVATED, RETIRED}
        if needs_policy and self.policy_artifact_id is None:
            raise ValueError(f"{self.event_type} requires a policy artifact")
        if (self.policy_artifact_id is None) != (self.policy_content_digest is None):
            raise ValueError("Policy artifact and content digest are recorded together")
        if self.event_type == ELIGIBILITY_EVALUATED:
            if (
                self.decision is None
                or self.evaluation_artifact_id is None
                or self.information_cutoff is None
            ):
                raise ValueError("An eligibility event records its decision, evidence and cutoff")
        elif self.decision is not None or self.evaluation_artifact_id is not None:
            if self.event_type != ACTIVATED or self.decision is not None:
                raise ValueError("Only eligibility/activation events reference an evaluation")
        if self.event_type == ACTIVATED and self.evaluation_artifact_id is None:
            raise ValueError("Activation requires the exact eligible evaluation")
        if self.event_type in ROLLBACK_EVENTS and self.rolled_back_event_id is None:
            raise ValueError("A rollback names the chain head it closes")
        if self.event_type not in ROLLBACK_EVENTS and self.rolled_back_event_id is not None:
            raise ValueError("Only rollbacks close a named chain head")
        if self.event_type not in CHAIN_EVENTS and self.previous_head_event_id is not None:
            raise ValueError("Only chain events name a previous head")
        if self.family == AI_DESK_POLICY and self.event_type not in {REGISTERED, RETIRED}:
            raise ValueError("AI desk policy versions are registered or retired only")
        return self


@dataclass(frozen=True)
class GovernanceGrant:
    """Permission, derived from one committed event, to execute a candidate payload.

    ``operational`` comes from the ACTIVATED/rollback chain head in force at the
    decision time; ``shadow`` from the candidate's REGISTERED event. The payload itself
    never carries execution authority.
    """

    role: Literal["operational", "shadow"]
    effective_from: datetime
    event_id: GovernanceEventId

    def __post_init__(self) -> None:
        if self.role not in ("operational", "shadow"):
            raise ValueError("Governance grants are operational or shadow")
        _aware(self.effective_from)
        GovernanceEventId(self.event_id)


@dataclass(frozen=True)
class Interval:
    """Derived half-open effective period of one chain event."""

    event: GovernanceEvent
    effective_from: datetime
    effective_until: datetime | None

    @property
    def policy_artifact_id(self) -> ArtifactId | None:
        return self.event.policy_artifact_id


def _ordered(events: Sequence[GovernanceEvent]) -> list[GovernanceEvent]:
    ordered = sorted(events, key=lambda row: row.scope_seq)
    if any(row.recorded_at is None for row in ordered):
        raise ValueError("Derivation requires recorded governance events")
    return ordered


def _recorded(event: GovernanceEvent) -> datetime:
    if event.recorded_at is None:
        raise ValueError("Derivation requires recorded governance events")
    return event.recorded_at


def chain(events: Sequence[GovernanceEvent]) -> list[GovernanceEvent]:
    return [row for row in _ordered(events) if row.event_type in CHAIN_EVENTS]


def head_at(
    events: Sequence[GovernanceEvent], at: datetime | None = None
) -> GovernanceEvent | None:
    """The chain event in force at ``at`` (all recorded events when None)."""
    moment = _aware(at)
    rows = [row for row in chain(events) if moment is None or _recorded(row) <= moment]
    return rows[-1] if rows else None


def intervals(events: Sequence[GovernanceEvent]) -> list[Interval]:
    rows = chain(events)
    result = []
    for index, row in enumerate(rows):
        until = _recorded(rows[index + 1]) if index + 1 < len(rows) else None
        result.append(Interval(row, _recorded(row), until))
    return result


def registered_at(events: Sequence[GovernanceEvent], policy: ArtifactId) -> GovernanceEvent | None:
    return next(
        (
            r
            for r in _ordered(events)
            if r.event_type == REGISTERED and r.policy_artifact_id == policy
        ),
        None,
    )


def retired_event(events: Sequence[GovernanceEvent], policy: ArtifactId) -> GovernanceEvent | None:
    return next(
        (r for r in _ordered(events) if r.event_type == RETIRED and r.policy_artifact_id == policy),
        None,
    )


def shadow_candidates_at(events: Sequence[GovernanceEvent], at: datetime) -> list[GovernanceEvent]:
    """REGISTERED <= at, not RETIRED <= at, and not the policy in force at ``at``."""
    moment = _aware(at)
    assert moment is not None
    head = head_at(events, moment)
    active = head.policy_artifact_id if head is not None else None
    retired = {
        row.policy_artifact_id
        for row in _ordered(events)
        if row.event_type == RETIRED and _recorded(row) <= moment
    }
    return [
        row
        for row in _ordered(events)
        if row.event_type == REGISTERED
        and _recorded(row) <= moment
        and row.policy_artifact_id not in retired
        and row.policy_artifact_id != active
    ]


def rolled_back_after(events: Sequence[GovernanceEvent], scope_key: str, scope_seq: int) -> bool:
    """Whether a rollback was recorded in this scope after the pinned sequence."""
    return any(
        row.scope_key == scope_key
        and row.event_type in ROLLBACK_EVENTS
        and row.scope_seq > scope_seq
        for row in events
    )


def pinned_scopes(forecast: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Governed scopes a configured forecast pinned: (family, scope_key, scope_seq).

    Only scopes that resolved a chain head can be revoked; an unpinned forecast has none.
    """
    pinned: list[tuple[str, str, int]] = []
    stage = forecast.get("deterministic_stage") or forecast.get("learning_stage")
    if isinstance(stage, dict) and stage.get("transformation_type") == "deterministic_corrected":
        resolution = stage.get("governance_resolution")
        if isinstance(resolution, dict) and resolution.get("head_event_id"):
            pinned.append(
                (resolution["family"], resolution["scope_key"], int(resolution["scope_seq"]))
            )
    lineage = forecast.get("baseline_snapshot") or {}
    heads = (lineage.get("blend_governance") or {}).get("heads", {})
    for target, row in sorted(heads.items()):
        if isinstance(row, dict) and row.get("head_event_id"):
            pinned.append((BLEND_POLICY, blend_scope(target), int(row["scope_seq"])))
    return pinned


def emergency_target(
    events: Sequence[GovernanceEvent],
) -> tuple[GovernanceEvent, ArtifactId | None]:
    """The policy an emergency rollback restores, or a refusal."""
    head = head_at(events)
    if head is None or head.event_type != ACTIVATED:
        raise GovernanceConflict(
            "emergency_target_unsafe",
            "Emergency rollback requires an ACTIVATED head; use an explicit rollback target",
        )
    previous = next(
        (row for row in _ordered(events) if row.event_id == head.previous_head_event_id), None
    )
    target = previous.policy_artifact_id if previous is not None else None
    if target is not None and retired_event(events, target) is not None:
        raise GovernanceConflict(
            "emergency_target_unsafe", "The previously active policy has been retired"
        )
    return head, target


def validate_append(existing: Sequence[GovernanceEvent], event: GovernanceEvent) -> None:
    """The append invariants; migration 0005's trigger enforces the same rules."""
    scope = [
        row for row in existing if row.family == event.family and row.scope_key == event.scope_key
    ]
    rows = _ordered(scope)
    if event.scope_seq != (rows[-1].scope_seq if rows else 0) + 1:
        raise GovernanceConflict("scope_seq_conflict", "Governance scope changed concurrently")
    if rows and event.recorded_at is not None and event.recorded_at <= _recorded(rows[-1]):
        raise GovernanceConflict("clock_regression", "Governance clock did not advance")
    head = head_at(rows)
    policy = event.policy_artifact_id
    if event.event_type == REGISTERED and any(
        row.event_type == REGISTERED and row.policy_artifact_id == policy for row in existing
    ):
        raise GovernanceConflict("already_registered")
    registered = policy is not None and registered_at(rows, policy) is not None
    retired = policy is not None and retired_event(rows, policy) is not None
    if event.event_type in CHAIN_EVENTS and event.previous_head_event_id != (
        head.event_id if head is not None else None
    ):
        raise GovernanceConflict("stale_head", "The expected governance head is stale")
    if event.family == AI_DESK_POLICY and event.event_type not in {REGISTERED, RETIRED}:
        raise GovernanceConflict("ai_desk_policy_not_activatable")
    if event.event_type == REGISTERED:
        if registered:
            raise GovernanceConflict("already_registered")
    elif event.event_type == ELIGIBILITY_EVALUATED:
        if not registered or retired:
            raise GovernanceConflict("candidate_not_registered_or_retired")
    elif event.event_type == ACTIVATED:
        if not registered:
            raise GovernanceConflict("candidate_not_registered")
        if retired:
            raise GovernanceConflict("candidate_retired")
    elif event.event_type == ROLLED_BACK:
        if head is None or event.rolled_back_event_id != head.event_id:
            raise GovernanceConflict("rollback_head_mismatch")
        if policy is not None and (
            retired
            or not any(r.event_type == ACTIVATED and r.policy_artifact_id == policy for r in rows)
        ):
            raise GovernanceConflict("rollback_target_unsafe")
    elif event.event_type == EMERGENCY_ROLLED_BACK:
        target_head, target = emergency_target(rows)
        if event.rolled_back_event_id != target_head.event_id or policy != target:
            raise GovernanceConflict("emergency_target_unsafe")
    elif event.event_type == RETIRED:
        if not registered:
            raise GovernanceConflict("candidate_not_registered")
        if retired:
            raise GovernanceConflict("already_retired")
        if head is not None and head.policy_artifact_id == policy:
            raise GovernanceConflict("retire_active_policy", "Roll back before retiring")


def _zulu(value: datetime) -> str:
    moment = _aware(value)
    assert moment is not None
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ResolvedPolicy:
    """One governed policy payload plus the committed event that permits its execution."""

    policy_artifact_id: ArtifactId
    content_digest: Digest
    event: GovernanceEvent
    registration: GovernanceEvent
    payload: dict[str, Any]
    reference: dict[str, Any]

    def grant(self, role: str) -> GovernanceGrant:
        assert self.event.recorded_at is not None
        return GovernanceGrant(
            role=role,  # type: ignore[arg-type]
            effective_from=self.event.recorded_at,
            event_id=self.event.event_id,
        )


@dataclass(frozen=True)
class ScopeResolution:
    """Committed governance state of one scope at one decision time."""

    family: str
    scope_key: str
    decision_time: datetime
    snapshot_read_at: datetime
    scope_seq: int
    head_event_id: GovernanceEventId | None
    active: ResolvedPolicy | None
    shadows: tuple[ResolvedPolicy, ...] = ()
    shadow_failures: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    def record(self, status: str, policy: ResolvedPolicy | None = None) -> dict[str, Any]:
        """The compact resolution sealed into every governed stage."""
        return {
            "schema_version": RESOLUTION_SCHEMA,
            "family": self.family,
            "scope_key": self.scope_key,
            "status": status,
            "decision_time": _zulu(self.decision_time),
            "snapshot_read_at": _zulu(self.snapshot_read_at),
            "scope_seq": self.scope_seq,
            "head_event_id": str(self.head_event_id) if self.head_event_id else None,
            **(
                {
                    "registration_event_id": str(policy.registration.event_id),
                    "grant_event_id": str(policy.event.event_id),
                    "policy_artifact_id": str(policy.policy_artifact_id),
                    "policy_content_digest": str(policy.content_digest),
                }
                if policy is not None
                else {}
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {
            **self.record("resolved_active" if self.active else "resolved_none", self.active),
            "shadow_candidates": [
                {
                    "policy_artifact_id": str(row.policy_artifact_id),
                    "registration_event_id": str(row.registration.event_id),
                }
                for row in self.shadows
            ],
            "shadow_failures": list(self.shadow_failures),
            "error": self.error,
        }


@dataclass(frozen=True)
class GovernanceSnapshot:
    family: str
    decision_time: datetime
    snapshot_read_at: datetime
    scopes: dict[str, ScopeResolution]
    seconds: float = 0.0
    measurements: dict[str, Any] = field(default_factory=dict)

    def scope(self, scope_key: str) -> ScopeResolution:
        return self.scopes.get(scope_key) or ScopeResolution(
            self.family, scope_key, self.decision_time, self.snapshot_read_at, 0, None, None
        )
