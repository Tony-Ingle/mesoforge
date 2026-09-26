"""Explicit, append-only governance of persistent forecast policies.

MesoForge learns, evaluates and determines eligibility automatically. Which persistent
policy executes changes only through an explicit operator command recorded as an
immutable governance event; forecast jobs, the AI desk and providers only read the
committed state at a pinned decision time. Nothing here blends, corrects or issues.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, NoReturn, TypeVar

from mesoforge.application.learning import LearningService, configured_learning
from mesoforge.common.errors import NotFound
from mesoforge.common.identifiers import ArtifactId, Digest, GovernanceEventId
from mesoforge.contracts.forecast_variants import instant, validate_variant
from mesoforge.contracts.policy_governance import (
    ACTIVATED,
    AI_DESK_POLICY,
    BLEND_POLICY,
    DESK_SCOPE,
    ELIGIBILITY_EVALUATED,
    EMERGENCY_ROLLED_BACK,
    FAMILIES,
    GENESIS,
    GOVERNANCE_LOCK_TIMEOUT_SECONDS,
    GOVERNANCE_POLICY_VERSION,
    REGISTERED,
    RETIRED,
    ROLLED_BACK,
    TEMPERATURE_CORRECTION,
    GovernanceBlockedError,
    GovernanceConflict,
    GovernanceEvent,
    GovernanceSnapshot,
    GovernanceUnavailableError,
    ResolvedPolicy,
    ScopeResolution,
    blend_scope,
    correction_scope,
    emergency_target,
    governance_policy_digest,
    head_at,
    intervals,
    parse_scope,
    registered_at,
    request_digest,
    request_key,
    retired_event,
    rolled_back_after,
    shadow_candidates_at,
    validate_append,
)
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.coherence import QPF, TEMPERATURE

EVALUATION_SCHEMA = "mesoforge.governance-evaluation.v1"
DESK_VERSION_SCHEMA = "mesoforge.forecast-desk-version.v1"
FINAL_REVIEW_BEHAVIOR = "negative_review_restores_corrected_parent.v1"
CORRECTION_SCHEMA = "mesoforge.temperature-correction-policy.v1"
BLEND_SCHEMA = "mesoforge.candidate-blend-policy.v1"
_T = TypeVar("_T")


def _iso(value: datetime | str) -> str:
    return instant(value).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return str(Digest.of_bytes(canonical_json_bytes(value)))


def _available_by(saved: dict[str, Any], at: datetime) -> bool:
    return max(instant(saved["registered_at"]), instant(saved["available_at"])) <= at


def validate_desk_version(payload: dict[str, Any]) -> None:
    """A NEW registration must equal the current code-versioned desk identity."""
    validate_desk_version_record(payload)
    if payload != desk_version_payload():
        raise ValueError(
            "A desk version record must equal the current code-versioned desk identity"
        )


def validate_desk_version_record(payload: dict[str, Any]) -> None:
    """Shape of a retained desk-version record; historical versions stay readable."""
    if (
        payload.get("schema_version") != DESK_VERSION_SCHEMA
        or payload.get("lifecycle_role") != "code_versioned"
        or any(
            not isinstance(payload.get(key), str) or not payload[key]
            for key in ("policy_id", "version", "final_review_behavior", "tool_policy_version")
        )
    ):
        raise ValueError("Not a code-versioned desk version record")
    Digest(payload["identity_digest"])


def desk_version_payload() -> dict[str, Any]:
    """Derived from code only, so an explicit registration is deterministic and replayable.

    Any change to instructions, tools, edit permissions or final-review handling requires
    a DESK_POLICY version bump and a new explicit registration.
    """
    from mesoforge.application.forecast_desk import desk_policy_identity
    from mesoforge.contracts.forecast_desk import DESK_POLICY

    identity = desk_policy_identity(
        provider="registration", model="registration", reasoning_effort=None
    )
    return {
        "schema_version": DESK_VERSION_SCHEMA,
        "policy_id": DESK_POLICY["id"],
        "version": DESK_POLICY["version"],
        "lifecycle_role": "code_versioned",
        "identity_digest": identity["digest"],
        "tool_policy_version": identity["tool_policy_version"],
        "instructions_sha256": hashlib.sha256(canonical_json_bytes(dict(DESK_POLICY))).hexdigest(),
        "final_review_behavior": FINAL_REVIEW_BEHAVIOR,
        "series_scope": (
            "every provider/model/reasoning-effort runtime series carrying this desk "
            "identity digest; runtime identity splits series, it never pools them"
        ),
        "execution": "always attempted after correction; never selected by governance",
    }


def policy_scope(payload: dict[str, Any]) -> tuple[str, str]:
    """Family and scope derive from the immutable payload; never typed by an operator."""
    schema = payload.get("schema_version")
    if schema == CORRECTION_SCHEMA:
        from mesoforge.application.corrections import validate_temperature_policy

        validate_temperature_policy(payload)
        if payload["lifecycle_role"] != "candidate":
            raise GovernanceConflict(
                "payload_not_governable_candidate",
                "Only candidate payloads are governed; legacy lifecycle roles never execute",
            )
        coordinate = payload["coordinate"]
        return TEMPERATURE_CORRECTION, correction_scope(
            coordinate["latitude"], coordinate["longitude"]
        )
    if schema == BLEND_SCHEMA:
        from mesoforge.forecasting.candidate_policy import CandidateBlendPolicy

        policy = CandidateBlendPolicy.model_validate_json(canonical_json_bytes(payload))
        if policy.lifecycle_role != "candidate":
            raise GovernanceConflict(
                "payload_not_governable_candidate",
                "Only candidate blend payloads are governed; shadow payload roles are legacy",
            )
        return BLEND_POLICY, blend_scope(policy.field)
    if schema == DESK_VERSION_SCHEMA:
        validate_desk_version_record(payload)
        return AI_DESK_POLICY, DESK_SCOPE
    raise GovernanceConflict("unsupported_policy_schema", f"Not a governed policy: {schema!r}")


class GovernanceService:
    """Reads under the shared family lock; writes one event per explicit request."""

    def __init__(
        self,
        learning: LearningService,
        *,
        lock_timeout_seconds: float = GOVERNANCE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.learning = learning
        self.factory = learning.storage.factory
        self.lock_timeout_seconds = lock_timeout_seconds

    # ----------------------------------------------------------------- reading
    def _read(
        self,
        family: str,
        scope_keys: tuple[str, ...] | None,
        at: datetime | None,
        reader: Callable[[Any], _T] | None = None,
    ) -> tuple[tuple[GovernanceEvent, ...], datetime, _T | None]:
        """Shared lock, then the decision-time check, then the read (in that order)."""
        try:
            with self.factory() as uow:
                uow.governance.lock(family, shared=True, timeout_seconds=self.lock_timeout_seconds)
                now = uow.governance.db_now()
                if at is not None and instant(at) > now:
                    raise GovernanceUnavailableError(
                        "decision_time_in_future",
                        "Decision time follows the governance database clock",
                    )
                events = uow.governance.events(family, scope_keys)
                extra = reader(uow) if reader is not None else None
                uow.rollback()
        except GovernanceUnavailableError:
            raise
        except GovernanceConflict as exc:
            raise GovernanceUnavailableError(exc.code, str(exc)) from exc
        except Exception as exc:
            raise GovernanceUnavailableError(
                "governance_read_failed", f"{type(exc).__name__}: {exc}"
            ) from exc
        return events, now, extra

    def _resolved(
        self,
        policy: ArtifactId,
        event: GovernanceEvent,
        registration: GovernanceEvent,
        at: datetime,
    ) -> ResolvedPolicy:
        saved = self.learning.read(ArtifactId(policy))
        if saved["content_digest"] != event.policy_content_digest or not _available_by(saved, at):
            raise GovernanceUnavailableError(
                "policy_artifact_not_available_at_decision",
                "Governed policy artifact identity/availability is unproven at decision time",
            )
        return ResolvedPolicy(
            ArtifactId(policy),
            Digest(saved["content_digest"]),
            event,
            registration,
            saved["payload"],
            self.learning._reference(saved),
        )

    def snapshot(
        self, family: str, scope_keys: Iterable[str] | None, at: datetime
    ) -> GovernanceSnapshot:
        """One committed-state read for many scopes; payloads are read afterwards."""
        started = time.perf_counter()
        at = instant(at)
        keys = tuple(sorted(set(scope_keys))) if scope_keys is not None else None
        events, read_at, _ = self._read(family, keys, at)
        by_scope: dict[str, list[GovernanceEvent]] = defaultdict(list)
        for event in events:
            by_scope[event.scope_key].append(event)
        scopes = {}
        for key in keys if keys is not None else tuple(sorted(by_scope)):
            rows = [
                row
                for row in by_scope.get(key, [])
                if row.recorded_at is not None and row.recorded_at <= at
            ]
            head = head_at(rows)
            active: ResolvedPolicy | None = None
            error = None
            if head is not None and head.policy_artifact_id is not None:
                registration = registered_at(rows, head.policy_artifact_id)
                try:
                    if registration is None:
                        raise GovernanceUnavailableError("active_policy_not_registered")
                    active = self._resolved(head.policy_artifact_id, head, registration, at)
                except Exception as exc:
                    error = f"{getattr(exc, 'code', 'policy_artifact_unreadable')}: {exc}"
            shadows, failures = [], []
            for registration in shadow_candidates_at(rows, at):
                assert registration.policy_artifact_id is not None
                try:
                    shadows.append(
                        self._resolved(
                            registration.policy_artifact_id, registration, registration, at
                        )
                    )
                except Exception as exc:
                    failures.append(
                        {
                            "policy_artifact_id": str(registration.policy_artifact_id),
                            "registration_event_id": str(registration.event_id),
                            "status": "storage_failed",
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    )
            scopes[key] = ScopeResolution(
                family,
                key,
                at,
                read_at,
                max((row.scope_seq for row in rows), default=0),
                head.event_id if head is not None else None,
                active,
                tuple(shadows),
                tuple(failures),
                error,
            )
        return GovernanceSnapshot(
            family, at, read_at, scopes, time.perf_counter() - started, {"events_read": len(events)}
        )

    def correction_snapshot(
        self, coordinates: Iterable[tuple[float, float]], at: datetime
    ) -> GovernanceSnapshot:
        return self.snapshot(
            TEMPERATURE_CORRECTION,
            [correction_scope(latitude, longitude) for latitude, longitude in coordinates],
            at,
        )

    def rollback_guard(self, resolution: ScopeResolution) -> None:
        """Refuse issuance when the pinned state was rolled back after resolution."""
        if resolution.head_event_id is None:
            return
        events, _, _ = self._read(resolution.family, (resolution.scope_key,), None)
        if rolled_back_after(events, resolution.scope_key, resolution.scope_seq):
            raise GovernanceBlockedError(
                "policy_rolled_back_before_issuance",
                "The governed policy resolved for this decision was rolled back in flight",
            )

    def blend_resolution(
        self, at: datetime, *, previous: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Active blend policies at the build cutoff, with their execution overrides."""
        from mesoforge.forecasting.candidate_policy import CandidateBlendPolicy

        started = time.perf_counter()
        at = instant(at)
        events, read_at, _ = self._read(BLEND_POLICY, None, at)
        by_scope: dict[str, list[GovernanceEvent]] = defaultdict(list)
        for event in events:
            if event.recorded_at is not None and event.recorded_at <= at:
                by_scope[event.scope_key].append(event)
        heads: dict[str, Any] = {}
        policies: dict[str, Any] = {}
        overrides: dict[str, Any] = {}
        prior = (previous or {}).get("policies", {})
        for key, rows in sorted(by_scope.items()):
            target = parse_scope(key)["field"]
            head = head_at(rows)
            heads[target] = {
                "head_event_id": str(head.event_id) if head else None,
                "scope_seq": max(row.scope_seq for row in rows),
            }
            if head is None or head.policy_artifact_id is None:
                continue
            try:
                saved = self.learning.read(head.policy_artifact_id)
                if saved["content_digest"] != head.policy_content_digest or not _available_by(
                    saved, at
                ):
                    raise GovernanceUnavailableError("policy_artifact_not_available_at_decision")
                payload = saved["payload"]
                source = "artifact"
            except Exception as exc:
                cached = prior.get(target)
                if (
                    cached is None
                    or cached.get("policy_artifact_id") != str(head.policy_artifact_id)
                    or cached.get("content_digest") != head.policy_content_digest
                    or (previous or {}).get("heads", {}).get(target) != heads[target]
                ):
                    raise GovernanceUnavailableError(
                        "blend_policy_unreadable", f"{type(exc).__name__}: {exc}"
                    ) from exc
                payload, source = cached["policy"], "previous_manifest_last_known_provable"
            policy = CandidateBlendPolicy.model_validate_json(canonical_json_bytes(payload))
            if policy.field != target or (policy.lead_start, policy.lead_end) != (1, 36):
                raise GovernanceUnavailableError("governed_blend_scope_invalid")
            if policy.field == TEMPERATURE:
                raise GovernanceUnavailableError("temperature_recipe_activation_unsupported")
            overrides[target] = policy.parameters
            policies[target] = {
                "policy_artifact_id": str(head.policy_artifact_id),
                "content_digest": str(head.policy_content_digest),
                "policy_id": policy.policy_id,
                "version": policy.version,
                "head_event_id": str(head.event_id),
                "policy": payload,
                "source": source,
            }
        return {
            "status": "resolved",
            "decision_time": _iso(at),
            "db_read_at": _iso(read_at),
            "heads": heads,
            "policies": policies,
            "overrides": overrides,
            "seconds": time.perf_counter() - started,
        }

    def blend_revoked(self, blend_governance: dict[str, Any]) -> bool:
        """Whether a blend pinned by a baseline was rolled back after that baseline."""
        pinned = {
            blend_scope(target): row["scope_seq"]
            for target, row in blend_governance.get("heads", {}).items()
            if row.get("head_event_id")
        }
        if not pinned:
            return False
        events, _, _ = self._read(BLEND_POLICY, tuple(pinned), None)
        return any(rolled_back_after(events, key, seq) for key, seq in pinned.items())

    def blend_candidates(self, at: datetime) -> list[ResolvedPolicy]:
        snapshot = self.snapshot(BLEND_POLICY, None, at)
        return [row for scope in snapshot.scopes.values() for row in scope.shadows]

    def issuance_guard(
        self, coordinates: Sequence[tuple[float, float]], at: datetime
    ) -> dict[tuple[float, float], str | None]:
        """Development/replay issuance paths never issue past an ACTIVE governed policy."""
        corrections = self.correction_snapshot(coordinates, at)
        blends = self.snapshot(BLEND_POLICY, None, at)
        blend_active = any(scope.head_event_id for scope in blends.scopes.values())
        result: dict[tuple[float, float], str | None] = {}
        for latitude, longitude in coordinates:
            scope = corrections.scope(correction_scope(latitude, longitude))
            result[latitude, longitude] = (
                "governed_policy_active_use_baseline_path"
                if blend_active or scope.head_event_id and scope.active is not None
                else "governance_unavailable"
                if scope.error
                else None
            )
        return result

    # ------------------------------------------------------------------ writing
    def _identity(self) -> tuple[str, Digest]:
        return self.learning.identity["git_commit"], Digest.of_bytes(
            canonical_json_bytes(self.learning.identity)
        )

    def _event(
        self,
        *,
        family: str,
        scope_key: str,
        seq: int,
        event_type: str,
        key: Digest,
        digest: Digest,
        actor: str,
        reason: str,
        policy: ArtifactId | None = None,
        content_digest: Digest | None = None,
        previous_head: GovernanceEventId | None = None,
        rolled_back: GovernanceEventId | None = None,
        evaluation: ArtifactId | None = None,
        decision: str | None = None,
        information_cutoff: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> GovernanceEvent:
        revision, environment = self._identity()
        return GovernanceEvent(
            event_id=GovernanceEventId.generate(),
            family=family,  # type: ignore[arg-type]
            scope_key=scope_key,
            scope_seq=seq,
            event_type=event_type,  # type: ignore[arg-type]
            policy_artifact_id=policy,
            policy_content_digest=content_digest,
            previous_head_event_id=previous_head,
            rolled_back_event_id=rolled_back,
            evaluation_artifact_id=evaluation,
            decision=decision,  # type: ignore[arg-type]
            information_cutoff=information_cutoff,
            governance_policy_version=GOVERNANCE_POLICY_VERSION,
            governance_policy_digest=governance_policy_digest(),
            actor=actor,
            reason=reason,
            code_revision=revision,
            environment_digest=environment,
            request_key=key,
            request_digest=digest,
            payload=payload,
        )

    def _append(
        self,
        family: str,
        scope_key: str,
        key: Digest,
        digest: Digest,
        build: Callable[[list[GovernanceEvent], datetime, int], GovernanceEvent],
    ) -> dict[str, Any]:
        """One short transaction: exclusive lock, idempotency, prechecks, append, commit."""
        started = time.perf_counter()
        for attempt in (1, 2):
            try:
                with self.factory() as uow:
                    uow.governance.lock(
                        family, shared=False, timeout_seconds=self.lock_timeout_seconds
                    )
                    prior = uow.governance.find_by_request_key(key)
                    if prior is not None:
                        if prior.request_digest != digest:
                            raise GovernanceConflict(
                                "request_key_reused",
                                "The same request was recorded with a different actor/reason",
                            )
                        return {
                            "event": prior.model_dump(mode="json"),
                            "already_existing": True,
                            "transaction_seconds": time.perf_counter() - started,
                        }
                    events = list(uow.governance.events(family, (scope_key,)))
                    now = uow.governance.db_now()
                    seq = max((row.scope_seq for row in events), default=0) + 1
                    event = build(events, now, seq)
                    validate_append(events, event)
                    stored = uow.governance.append(event)
                    uow.commit()
                return {
                    "event": stored.model_dump(mode="json"),
                    "already_existing": False,
                    "transaction_seconds": time.perf_counter() - started,
                }
            except GovernanceConflict as exc:
                # A concurrent identical request won the unique key: return its event.
                if exc.code == "request_key_conflict" and attempt == 1:
                    continue
                raise
        raise GovernanceConflict("request_key_conflict")

    def _policy(self, policy_artifact_id: ArtifactId) -> dict[str, Any]:
        saved = self.learning.read(ArtifactId(policy_artifact_id))
        family, scope_key = policy_scope(saved["payload"])
        return {**saved, "family": family, "scope_key": scope_key}

    def register(
        self, policy_artifact_id: ArtifactId, *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Explicit shadow enablement of one immutable candidate; never activation."""
        saved = self._policy(policy_artifact_id)
        payload = saved["payload"]
        if saved["family"] == AI_DESK_POLICY:
            raise GovernanceConflict(
                "use_register_desk_version", "Desk versions register through their own command"
            )
        canonical = self.learning.find(
            "learning-policy", {"policy_id": payload["policy_id"], "version": payload["version"]}
        )
        if [row["artifact_id"] for row in canonical] != [str(policy_artifact_id)]:
            raise GovernanceConflict(
                "policy_identity_not_canonical",
                "Register the artifact that register_policy returns for this ID/version",
            )
        return self._register(saved, actor=actor, reason=reason)

    def _register(self, saved: dict[str, Any], *, actor: str, reason: str) -> dict[str, Any]:
        family, scope_key = saved["family"], saved["scope_key"]
        policy = ArtifactId(saved["artifact_id"])
        key = request_key(operation="register", family=family, scope=scope_key, policy=policy)
        created = saved["payload"].get("created_at")

        def build(events: list[GovernanceEvent], now: datetime, seq: int) -> GovernanceEvent:
            if created is not None and instant(created) > now:
                raise GovernanceConflict(
                    "policy_created_after_registration_clock",
                    "Policy creation time follows the governance database clock",
                )
            return self._event(
                family=family,
                scope_key=scope_key,
                seq=seq,
                event_type=REGISTERED,
                key=key,
                digest=request_digest(key, actor=actor, reason=reason),
                actor=actor,
                reason=reason,
                policy=policy,
                content_digest=Digest(saved["content_digest"]),
            )

        return self._append(
            family, scope_key, key, request_digest(key, actor=actor, reason=reason), build
        )

    def register_desk_version(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Explicitly record the code-versioned desk policy; the desk never calls this."""
        saved = self.learning.register_policy(desk_version_payload())
        return self._register(
            {**saved, "family": AI_DESK_POLICY, "scope_key": DESK_SCOPE}, actor=actor, reason=reason
        )

    def retire(self, policy_artifact_id: ArtifactId, *, actor: str, reason: str) -> dict[str, Any]:
        registration = self._registration(policy_artifact_id)
        family, scope_key = registration.family, registration.scope_key
        key = request_key(
            operation="retire", family=family, scope=scope_key, policy=policy_artifact_id
        )

        def build(events: list[GovernanceEvent], now: datetime, seq: int) -> GovernanceEvent:
            head = head_at(events)
            if head is not None and head.policy_artifact_id == policy_artifact_id:
                raise GovernanceConflict(
                    "retire_active_policy", "Roll back the active policy before retiring it"
                )
            return self._event(
                family=family,
                scope_key=scope_key,
                seq=seq,
                event_type=RETIRED,
                key=key,
                digest=request_digest(key, actor=actor, reason=reason),
                actor=actor,
                reason=reason,
                policy=ArtifactId(policy_artifact_id),
                content_digest=registration.policy_content_digest,
            )

        return self._append(
            family, scope_key, key, request_digest(key, actor=actor, reason=reason), build
        )

    def _registration(self, policy_artifact_id: ArtifactId) -> GovernanceEvent:
        with self.factory() as uow:
            rows: tuple[GovernanceEvent, ...] = uow.governance.policy_events(
                ArtifactId(policy_artifact_id)
            )
        registration = next((row for row in rows if row.event_type == REGISTERED), None)
        if registration is None:
            raise GovernanceConflict("candidate_not_registered", "Policy is not registered")
        return registration

    def _head_event(self, event_id: GovernanceEventId) -> GovernanceEvent:
        with self.factory() as uow:
            try:
                event: GovernanceEvent = uow.governance.get(GovernanceEventId(event_id))
                return event
            except NotFound as exc:
                raise GovernanceConflict("unknown_expected_head", str(exc)) from exc

    def rollback(
        self,
        *,
        expected_head: GovernanceEventId,
        target: ArtifactId | None,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Restore none or a previously active, non-retired policy as a new event."""
        head = self._head_event(expected_head)
        family, scope_key = head.family, head.scope_key
        key = request_key(
            operation="rollback",
            family=family,
            scope=scope_key,
            target=str(target) if target else None,
            expected_head=str(expected_head),
        )

        def build(events: list[GovernanceEvent], now: datetime, seq: int) -> GovernanceEvent:
            current = head_at(events)
            if current is None or current.event_id != expected_head:
                raise GovernanceConflict("active_changed", "The expected head is not current")
            content = None
            if target is not None:
                if retired_event(events, target) is not None or not any(
                    row.event_type == ACTIVATED and row.policy_artifact_id == target
                    for row in events
                ):
                    raise GovernanceConflict(
                        "rollback_target_unsafe",
                        "Rollback targets none or a previously active, non-retired policy",
                    )
                registration = registered_at(events, target)
                assert registration is not None
                content = registration.policy_content_digest
            return self._event(
                family=family,
                scope_key=scope_key,
                seq=seq,
                event_type=ROLLED_BACK,
                key=key,
                digest=request_digest(key, actor=actor, reason=reason),
                actor=actor,
                reason=reason,
                policy=target,
                content_digest=content,
                previous_head=current.event_id,
                rolled_back=current.event_id,
            )

        return self._append(
            family, scope_key, key, request_digest(key, actor=actor, reason=reason), build
        )

    def emergency_rollback(
        self, *, expected_head: GovernanceEventId, actor: str, reason: str
    ) -> dict[str, Any]:
        """Restore the policy the ACTIVATED head superseded; no evidence is required."""
        head = self._head_event(expected_head)
        family, scope_key = head.family, head.scope_key
        key = request_key(
            operation="emergency_rollback",
            family=family,
            scope=scope_key,
            expected_head=str(expected_head),
        )

        def build(events: list[GovernanceEvent], now: datetime, seq: int) -> GovernanceEvent:
            current = head_at(events)
            if current is None or current.event_id != expected_head:
                raise GovernanceConflict("active_changed", "The expected head is not current")
            closed, target = emergency_target(events)
            registration = registered_at(events, target) if target is not None else None
            return self._event(
                family=family,
                scope_key=scope_key,
                seq=seq,
                event_type=EMERGENCY_ROLLED_BACK,
                key=key,
                digest=request_digest(key, actor=actor, reason=reason),
                actor=actor,
                reason=reason,
                policy=target,
                content_digest=registration.policy_content_digest if registration else None,
                previous_head=closed.event_id,
                rolled_back=closed.event_id,
            )

        return self._append(
            family, scope_key, key, request_digest(key, actor=actor, reason=reason), build
        )

    def activate(
        self,
        policy_artifact_id: ArtifactId,
        evaluation_artifact_id: ArtifactId,
        *,
        expected_head: GovernanceEventId | None,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Explicit compare-and-set activation of exactly one eligible evaluated candidate."""
        saved = self._policy(policy_artifact_id)
        family, scope_key = saved["family"], saved["scope_key"]
        if family == AI_DESK_POLICY:
            raise GovernanceConflict("ai_desk_policy_not_activatable")
        evaluation = self.learning.read(ArtifactId(evaluation_artifact_id))
        record = evaluation["payload"]
        if (
            record.get("schema_version") != EVALUATION_SCHEMA
            or record["candidate"]["policy_artifact_id"] != str(policy_artifact_id)
            or record["family"] != family
            or record["scope_key"] != scope_key
        ):
            raise GovernanceConflict(
                "evaluation_candidate_mismatch", "Evaluation belongs to another candidate/scope"
            )
        from mesoforge.verification.governance_eligibility import (
            eligibility_policy_digest,
            evidence_policy_identity,
            revalidate_qualification,
        )

        revalidate_qualification(record.get("eligibility", {}).get("evidence_qualification", {}))
        if record["decision"] != "eligible":
            raise GovernanceConflict("candidate_not_eligible", ",".join(record["reasons"]))
        if (
            record["eligibility_policy"]["digest"] != eligibility_policy_digest()
            or record["evidence_policy"] != evidence_policy_identity()
            or record["governance_policy"]["digest"] != str(governance_policy_digest())
            or record["evidence_code_identity"] != self._evidence_code_identity()
        ):
            raise GovernanceConflict("eligibility_stale", "Rule, evidence policy or code changed")
        if family == BLEND_POLICY:
            policy = saved["payload"]
            if policy["field"] == TEMPERATURE:
                raise GovernanceConflict("temperature_recipe_activation_unsupported")
            if (policy.get("lead_start", 1), policy.get("lead_end", 36)) != (1, 36):
                raise GovernanceConflict("blend_activation_requires_leads_1_to_36")
        # Re-evaluate outside every lock at the same information cutoff.
        again = self.evaluate(
            ArtifactId(policy_artifact_id),
            information_cutoff=instant(record["information_cutoff"]),
            locations=[tuple(row) for row in record.get("locations", [])] or None,
        )["evaluation"]
        if again["cohort_digest"] != record["cohort_digest"] or again["decision"] != "eligible":
            before = set(record["cohort"]["common_sample_ids"])
            after = set(again["cohort"]["common_sample_ids"])
            raise GovernanceConflict(
                "evidence_identity_changed",
                json.dumps(
                    {"added": sorted(after - before), "removed": sorted(before - after)},
                    sort_keys=True,
                ),
            )
        expected = str(expected_head) if expected_head is not None else GENESIS
        key = request_key(
            operation="activate",
            family=family,
            scope=scope_key,
            policy=str(policy_artifact_id),
            evaluation=str(evaluation_artifact_id),
            expected_head=expected,
        )

        def build(events: list[GovernanceEvent], now: datetime, seq: int) -> GovernanceEvent:
            if registered_at(events, policy_artifact_id) is None:
                raise GovernanceConflict("candidate_not_registered")
            if retired_event(events, policy_artifact_id) is not None:
                raise GovernanceConflict("candidate_retired")
            recorded = next(
                (
                    row
                    for row in events
                    if row.event_type == ELIGIBILITY_EVALUATED
                    and row.policy_artifact_id == policy_artifact_id
                    and row.evaluation_artifact_id == evaluation_artifact_id
                    and row.decision == "eligible"
                ),
                None,
            )
            if recorded is None:
                raise GovernanceConflict(
                    "eligibility_not_recorded", "Record the eligible evaluation before activation"
                )
            current = head_at(events)
            current_id = str(current.event_id) if current is not None else None
            if not (
                current_id
                == record["heads"]["at_information_cutoff"]
                == (recorded.payload or {}).get("head_at_record")
                == (str(expected_head) if expected_head is not None else None)
            ):
                raise GovernanceConflict(
                    "active_changed", "Active state changed since the evaluation or expectation"
                )
            return self._event(
                family=family,
                scope_key=scope_key,
                seq=seq,
                event_type=ACTIVATED,
                key=key,
                digest=request_digest(key, actor=actor, reason=reason),
                actor=actor,
                reason=reason,
                policy=ArtifactId(policy_artifact_id),
                content_digest=Digest(saved["content_digest"]),
                previous_head=current.event_id if current is not None else None,
                evaluation=ArtifactId(evaluation_artifact_id),
            )

        return self._append(
            family, scope_key, key, request_digest(key, actor=actor, reason=reason), build
        )

    # --------------------------------------------------------------- evaluating
    def _evidence_code_identity(self) -> str:
        return _digest(self.learning.identity.get("learning_sources", {}))

    def evaluate(
        self,
        policy_artifact_id: ArtifactId,
        *,
        information_cutoff: datetime,
        locations: Sequence[tuple[float, float]] | None = None,
        record: bool = False,
        actor: str | None = None,
        reason: str | None = None,
        window_start: datetime | None = None,
    ) -> dict[str, Any]:
        """Deterministic eligibility at an explicit cutoff; read-only unless ``record``.

        ``window_start`` is an explicit, reporting-only start for the AI family (its
        default is the desk version's registration); candidate families always start
        at their own registration.
        """
        started = time.perf_counter()
        cutoff = instant(information_cutoff)
        if cutoff > instant(self.learning.clock()):
            raise GovernanceConflict("information_cutoff_in_future")
        saved = self._policy(policy_artifact_id)
        family, scope_key = saved["family"], saved["scope_key"]
        events, _, _ = self._read(family, (scope_key,), cutoff)
        visible = [row for row in events if row.recorded_at and row.recorded_at <= cutoff]
        registration = registered_at(visible, ArtifactId(policy_artifact_id))
        head = head_at(visible)
        reads: Counter[str] = Counter()
        common: dict[str, Any] = {
            "schema_version": EVALUATION_SCHEMA,
            "family": family,
            "scope_key": scope_key,
            "candidate": {
                "policy_artifact_id": str(policy_artifact_id),
                "content_digest": saved["content_digest"],
                "policy_id": saved["payload"].get("policy_id"),
                "version": saved["payload"].get("version"),
                "registration_event_id": str(registration.event_id) if registration else None,
                "registered_at": _iso(registration.recorded_at)
                if registration and registration.recorded_at
                else None,
            },
            "information_cutoff": _iso(cutoff),
            "heads": {
                "at_information_cutoff": str(head.event_id) if head else None,
                "scope_seq_at_information_cutoff": max(
                    (row.scope_seq for row in visible), default=0
                ),
            },
            "governance_policy": {
                "version": GOVERNANCE_POLICY_VERSION,
                "digest": str(governance_policy_digest()),
            },
            "evidence_code_identity": self._evidence_code_identity(),
        }
        from mesoforge.verification.governance_eligibility import (
            GOVERNANCE_ELIGIBILITY_POLICY,
            ai_eligibility,
            blend_eligibility,
            eligibility_policy_digest,
            evidence_policy_identity,
        )

        common["eligibility_policy"] = {
            "id": GOVERNANCE_ELIGIBILITY_POLICY["id"],
            "digest": eligibility_policy_digest(),
        }
        common["evidence_policy"] = evidence_policy_identity()
        if window_start is not None and family != AI_DESK_POLICY:
            raise GovernanceConflict(
                "window_start_ai_family_only",
                "Candidate cohorts always start at the candidate's registration",
            )
        if registration is None or retired_event(visible, ArtifactId(policy_artifact_id)):
            body: dict[str, Any] = {
                "cohort": None,
                "cohort_digest": _digest({"unregistered": str(policy_artifact_id)}),
                "decision": "not_eligible",
                "reasons": [
                    "candidate_retired"
                    if registration is not None
                    else "candidate_not_registered_at_information_cutoff"
                ],
            }
        elif family == TEMPERATURE_CORRECTION:
            body = self._evaluate_correction(saved, registration, visible, cutoff, reads)
        elif family == BLEND_POLICY:
            body = self._evaluate_blend(saved, registration, cutoff, locations or (), reads)
            body.update({k: v for k, v in blend_eligibility(saved["payload"]["field"]).items()})
            body["reasons"] = sorted(set(body.pop("pair_reasons", [])) | set(body["reasons"]))
        else:
            assert registration.recorded_at is not None
            start = instant(window_start) if window_start is not None else registration.recorded_at
            if start > cutoff:
                raise GovernanceConflict("window_start_after_information_cutoff")
            body = self._evaluate_ai(saved, registration, start, cutoff, locations or (), reads)
            body.update(ai_eligibility())
            body["reasons"] = sorted(set(body.pop("pair_reasons", [])) | set(body["reasons"]))
        evaluation = {**common, **body}
        if locations:
            evaluation["locations"] = [list(row) for row in sorted(set(locations))]
        evaluation.pop("rule", None)
        result: dict[str, Any] = {
            "evaluation": evaluation,
            "measurements": {
                "evaluation_seconds": time.perf_counter() - started,
                "artifact_reads": dict(reads),
                "evaluation_bytes": len(canonical_json_bytes(evaluation)),
            },
            "writes": 0,
        }
        if record:
            if not actor or not reason:
                raise GovernanceConflict("actor_and_reason_required")
            result["recorded"] = self._record_evaluation(evaluation, actor=actor, reason=reason)
            result["writes"] = 1
        return result

    def _record_evaluation(
        self, evaluation: dict[str, Any], *, actor: str, reason: str
    ) -> dict[str, Any]:
        saved = self.learning.save(
            "governance-evaluation",
            evaluation,
            attributes={
                "family": evaluation["family"],
                "policy_artifact_id": evaluation["candidate"]["policy_artifact_id"],
                "information_cutoff": evaluation["information_cutoff"],
            },
        )
        family, scope_key = evaluation["family"], evaluation["scope_key"]
        policy = ArtifactId(evaluation["candidate"]["policy_artifact_id"])
        evaluation_id = ArtifactId(saved["artifact_id"])
        key = request_key(
            operation="evaluate", family=family, scope=scope_key, evaluation=str(evaluation_id)
        )

        def build(events: list[GovernanceEvent], now: datetime, seq: int) -> GovernanceEvent:
            current = head_at(events)
            current_id = str(current.event_id) if current is not None else None
            if (
                evaluation["decision"] == "eligible"
                and current_id != evaluation["heads"]["at_information_cutoff"]
            ):
                raise GovernanceConflict(
                    "active_state_changed_after_information_cutoff",
                    "Re-evaluate at a cutoff after the latest lifecycle change",
                )
            return self._event(
                family=family,
                scope_key=scope_key,
                seq=seq,
                event_type=ELIGIBILITY_EVALUATED,
                key=key,
                digest=request_digest(key, actor=actor, reason=reason),
                actor=actor,
                reason=reason,
                policy=policy,
                content_digest=Digest(evaluation["candidate"]["content_digest"]),
                evaluation=evaluation_id,
                decision=evaluation["decision"],
                information_cutoff=instant(evaluation["information_cutoff"]),
                payload={
                    "head_at_record": current_id,
                    "cohort_digest": evaluation["cohort_digest"],
                    "reasons": evaluation["reasons"],
                },
            )

        stored = self._append(
            family, scope_key, key, request_digest(key, actor=actor, reason=reason), build
        )
        return {"evaluation_reference": self.learning._reference(saved), **stored}

    def _bound_stages(
        self,
        latitude: float,
        longitude: float,
        start: datetime,
        cutoff: datetime,
        reads: Counter[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        """Stages and shadow attempts bound to issuances made in [start, cutoff]."""
        with self.factory() as uow:
            issued = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        window = {
            str(record.issued_forecast_id)
            for record in issued
            if start <= record.issued_at <= cutoff
        }
        stages: dict[str, dict[str, Any]] = {}
        bindings: dict[str, list[str]] = defaultdict(list)
        attempts: list[dict[str, Any]] = []
        for identifier in sorted(window):
            for binding in self.learning.find(
                "learning-binding", {"issued_forecast_id": identifier}
            ):
                reads["bindings"] += 1
                if not _available_by(binding, cutoff):
                    continue
                payload = binding["payload"]
                attempts.extend(
                    {**row, "issued_forecast_id": identifier}
                    for row in payload.get("shadow_attempts", [])
                )
                for ref in payload["variants"]:
                    bindings[ref["artifact_id"]].append(identifier)
        for artifact, issued_ids in sorted(bindings.items()):
            saved = self.learning.read(ArtifactId(artifact))
            reads["stages"] += 1
            reads["stage_bytes"] += saved["byte_size"]
            if not _available_by(saved, cutoff):
                continue
            validate_variant(saved["payload"])
            stages[artifact] = {**saved["payload"], "control_issued_forecast_ids": issued_ids}
        return list(stages.values()), attempts, window

    @staticmethod
    def _restricted(
        field_name: str, control: dict[str, Any], window: set[str]
    ) -> tuple[dict[str, Any], int]:
        """Only samples whose issued versions fall in the eligibility window."""
        key = "samples" if field_name == TEMPERATURE else None

        def inside(sample: dict[str, Any]) -> bool:
            versions = {sample.get("canonical_issued_forecast_id")}
            versions.update(row["issued_forecast_id"] for row in sample["provenance"]["versions"])
            return bool(versions & window)

        if key is not None:
            samples = control.get("samples", [])
            kept = [sample for sample in samples if inside(sample)]
            return {**control, "samples": kept}, len(samples) - len(kept)
        canonical = control.get("canonicalization", {})
        samples = canonical.get("samples", [])
        kept = [sample for sample in samples if inside(sample)]
        return (
            {**control, "canonicalization": {**canonical, "samples": kept}},
            len(samples) - len(kept),
        )

    def _temperature_control(
        self, latitude: float, longitude: float, cutoff: datetime
    ) -> dict[str, Any]:
        from mesoforge.application.site_verification_analysis import analyze_site_verification

        return analyze_site_verification(
            latitude,
            longitude,
            now=cutoff,
            as_of=cutoff,
            unit_of_work_factory=self.learning.storage.factory,
            load_payload=lambda manifest: self.learning.storage.artifacts.load_verified_payload(
                manifest.artifact_id
            )[1],
        )

    def _qpf_control(
        self, latitude: float, longitude: float, start: datetime, cutoff: datetime
    ) -> dict[str, Any]:
        begin = start.replace(minute=0, second=0, microsecond=0)
        end = cutoff.replace(minute=0, second=0, microsecond=0)
        if end <= begin:
            return {"canonicalization": {"samples": []}}
        return self.learning.storage.analyze_window(
            latitude=latitude,
            longitude=longitude,
            start_valid_time=begin,
            end_valid_time=end,
            stages=("final_issued",),
            as_of=cutoff,
        )

    def _reproduce(self, payload: dict[str, Any]) -> dict[str, Any]:
        from mesoforge.application.corrections import propose_temperature_policy

        try:
            coordinate = payload["coordinate"]
            evidence = self.learning.evidence(
                coordinate["latitude"], coordinate["longitude"], instant(payload["evidence_cutoff"])
            )
            if evidence.get("inventory", {}).get("legacy_scan", {}).get("truncated"):
                return {"status": "unproven", "reason": "evidence_identity_unproven"}
            again = propose_temperature_policy(
                evidence,
                policy_id=payload["policy_id"],
                version=payload["version"],
                evidence_cutoff=instant(payload["evidence_cutoff"]),
                created_at=instant(payload["created_at"]),
            )
        except Exception as exc:
            return {
                "status": "unproven",
                "reason": "evidence_identity_unproven",
                "detail": str(exc),
            }
        if again["digest"] == payload["digest"]:
            return {"status": "reproduced", "digest": again["digest"]}
        before = set(payload["provenance"]["canonical_fact_ids"])
        after = set(again["provenance"]["canonical_fact_ids"])
        if before != after:
            return {
                "status": "changed",
                "reason": "evidence_set_changed_after_proposal",
                "added_fact_ids": sorted(after - before),
                "removed_fact_ids": sorted(before - after),
            }
        if (
            again["provenance"]["canonicalization_policy"]
            != payload["provenance"]["canonicalization_policy"]
        ):
            return {"status": "changed", "reason": "evidence_code_identity_changed"}
        return {"status": "changed", "reason": "evidence_not_reproducible"}

    def _evaluate_correction(
        self,
        saved: dict[str, Any],
        registration: GovernanceEvent,
        events: list[GovernanceEvent],
        cutoff: datetime,
        reads: Counter[str],
    ) -> dict[str, Any]:
        from mesoforge.verification.governance_eligibility import (
            temperature_correction_eligibility,
        )
        from mesoforge.verification.variant_evaluation import evaluate_pair

        payload = saved["payload"]
        policy = ArtifactId(saved["artifact_id"])
        latitude, longitude = payload["coordinate"]["latitude"], payload["coordinate"]["longitude"]
        assert registration.recorded_at is not None
        start = registration.recorded_at
        reasons: list[str] = []
        control = self._temperature_control(latitude, longitude, cutoff)
        if control.get("inventory", {}).get("legacy_scan", {}).get("truncated"):
            reasons.append("evidence_identity_unproven")
        stages, attempts, window = self._bound_stages(latitude, longitude, start, cutoff, reads)
        control, outside = self._restricted(TEMPERATURE, control, window)
        candidate = [
            stage
            for stage in stages
            if stage["transformation_type"] == "deterministic_corrected"
            and stage["lifecycle_role"] == "shadow"
            and (stage.get("governance_resolution") or {}).get("registration_event_id")
            == str(registration.event_id)
            and (stage.get("governance_resolution") or {}).get("policy_artifact_id") == str(policy)
        ]
        reference = [stage for stage in stages if stage["transformation_type"] == "active_baseline"]
        ids = {stage["variant_id"] for stage in (*candidate, *reference)}
        ancestors = [stage for stage in stages if stage["variant_id"] not in ids]
        operational = [
            (row.effective_from, row.effective_until, "candidate_was_operational")
            for row in intervals(events)
            if row.policy_artifact_id == policy
        ]
        pair = evaluate_pair(
            TEMPERATURE,
            control,
            candidate,
            reference,
            ancestors=ancestors,
            decision_window=(start, cutoff),
            in_sample_until=instant(payload["evidence_cutoff"]),
            excluded_decision_intervals=operational,
        )
        mine = [
            row
            for row in attempts
            if row.get("policy_artifact_id") == str(policy)
            and row.get("registration_event_id") == str(registration.event_id)
        ]
        failures = Counter(row["status"] for row in mine)
        head = head_at(events)
        active = None
        comparisons: dict[str, Any] = {}
        if head is not None and head.policy_artifact_id is not None:
            active = {
                "policy_artifact_id": str(head.policy_artifact_id),
                "head_event_id": str(head.event_id),
            }
            current = [
                stage
                for stage in stages
                if stage["transformation_type"] == "deterministic_corrected"
                and stage["lifecycle_role"] == "active"
                and (stage.get("governance_resolution") or {}).get("head_event_id")
                == str(head.event_id)
            ]
            versus = evaluate_pair(
                TEMPERATURE,
                control,
                candidate,
                current,
                ancestors=[s for s in stages if s not in candidate and s not in current],
                decision_window=(max(start, head.recorded_at or start), cutoff),
                in_sample_until=instant(payload["evidence_cutoff"]),
            )
            versus.pop("rows")
            comparisons["candidate_vs_current_active"] = versus
        eligibility = temperature_correction_eligibility(
            pair,
            payload,
            reproduction=(reproduction := self._reproduce(payload)),
            active_at_cutoff=active,
            candidate_failures=failures["candidate_failed"],
        )
        pair.pop("rows")
        pair["outside_decision_eligibility_window"] += outside
        eligibility_reasons = sorted(set(eligibility.pop("reasons")) | set(reasons))
        return {
            "window": {"start": _iso(start), "end": _iso(cutoff), "closure": "[start,end]"},
            "cohort": pair,
            "cohort_digest": pair["cohort_digest"],
            "comparisons": comparisons,
            "execution_attempts": {
                "candidate_execution_failed": failures["candidate_failed"],
                "stage_storage_failed_excluded": failures["storage_failed"],
                "stored": failures["stored"],
            },
            "reproduction": reproduction,
            "active_at_information_cutoff": active,
            "eligibility": {k: v for k, v in eligibility.items() if k not in ("decision", "rule")},
            "decision": "not_eligible" if eligibility_reasons else "eligible",
            "reasons": eligibility_reasons,
        }

    def _evaluate_blend(
        self,
        saved: dict[str, Any],
        registration: GovernanceEvent,
        cutoff: datetime,
        locations: Sequence[tuple[float, float]],
        reads: Counter[str],
    ) -> dict[str, Any]:
        from mesoforge.verification.variant_evaluation import evaluate_pair

        payload = saved["payload"]
        target = payload["field"]
        assert registration.recorded_at is not None
        start = registration.recorded_at
        if target not in (TEMPERATURE, QPF):
            return {
                "cohort": None,
                "cohort_digest": _digest({"unavailable": target}),
                "comparison_status": "unavailable",
                "pair_reasons": ["no_verification_contract_for_field"],
            }
        if not locations:
            return {
                "cohort": None,
                "cohort_digest": _digest({"no_locations": target}),
                "comparison_status": "unavailable",
                "pair_reasons": ["evaluation_locations_required"],
            }
        cohorts, reasons = [], []
        for latitude, longitude in sorted(set(locations)):
            if target == TEMPERATURE:
                control = self._temperature_control(latitude, longitude, cutoff)
            else:
                try:
                    control = self._qpf_control(latitude, longitude, start, cutoff)
                except ValueError as exc:
                    if "Too many QPF facts" not in str(exc):
                        raise
                    reasons.append("evidence_window_truncated")
                    continue
            stages, _, window = self._bound_stages(latitude, longitude, start, cutoff, reads)
            control, outside = self._restricted(target, control, window)
            candidate = [
                stage
                for stage in stages
                if stage["transformation_type"] == "candidate_blend"
                and (stage.get("governance_resolution") or {}).get("registration_event_id")
                == str(registration.event_id)
            ]
            reference = [s for s in stages if s["transformation_type"] == "active_baseline"]
            ids = {stage["variant_id"] for stage in (*candidate, *reference)}
            pair = evaluate_pair(
                target,
                control,
                candidate,
                reference,
                ancestors=[s for s in stages if s["variant_id"] not in ids],
                decision_window=(start, cutoff),
                in_sample_until=instant(payload["evidence_cutoff"]),
                lead_range=(payload.get("lead_start", 1), payload.get("lead_end", 36)),
            )
            pair.pop("rows")
            pair["outside_decision_eligibility_window"] += outside
            cohorts.append({"latitude": latitude, "longitude": longitude, **pair})
        return {
            "window": {"start": _iso(start), "end": _iso(cutoff), "closure": "[start,end]"},
            "cohort": {
                "by_location": cohorts,
                "common_sample_ids": sorted(
                    {sample for row in cohorts for sample in row["common_sample_ids"]}
                ),
                "total_samples": sum(row["total_samples"] for row in cohorts),
                "common_samples": sum(row["common_samples"] for row in cohorts),
                "excluded_samples": sum(row["excluded_samples"] for row in cohorts),
            },
            "cohort_digest": _digest([row["cohort_digest"] for row in cohorts]),
            "comparison_status": "computed",
            "pair_reasons": reasons,
        }

    def _evaluate_ai(
        self,
        saved: dict[str, Any],
        registration: GovernanceEvent,
        start: datetime,
        cutoff: datetime,
        locations: Sequence[tuple[float, float]],
        reads: Counter[str],
    ) -> dict[str, Any]:
        """AI_OPERATIONAL against its exact deterministic corrected parent, per series.

        AI issuance never depends on governance, so stages with the registered identity
        digest are compared over an explicit bounded window [start, cutoff]; decisions
        before the registration and issuances before the window are counted, not hidden.
        """
        from mesoforge.verification.variant_evaluation import evaluate_pair

        identity = saved["payload"]["identity_digest"]
        assert registration.recorded_at is not None
        registered = registration.recorded_at
        results: list[dict[str, Any]] = []
        reasons: set[str] = set()
        for latitude, longitude in sorted(set(locations)):
            stages, _, window = self._bound_stages(latitude, longitude, start, cutoff, reads)
            with self.factory() as uow:
                issued = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
            ai = [s for s in stages if s["transformation_type"] == "ai_adjusted"]
            composition = {
                "issuances_before_window": sum(record.issued_at < start for record in issued),
                "ai_stages": len(ai),
                "decided_before_desk_version_registration": sum(
                    instant(stage["analysis_cutoff"]) < registered for stage in ai
                ),
                "desk_attempted_issuances_without_ai_stage": len(
                    window
                    - {issued for stage in ai for issued in stage["control_issued_forecast_ids"]}
                ),
                "completion_reason": dict(
                    Counter(s["overlay"]["desk"].get("completion_reason", "unknown") for s in ai)
                ),
                "parent_policy": dict(
                    Counter(
                        next(
                            (
                                p["policy"]["id"]
                                for p in stages
                                if p["variant_id"] == s["parent_stage_id"]
                            ),
                            "parent_unavailable",
                        )
                        for s in ai
                    )
                ),
            }
            series: dict[str, list[dict[str, Any]]] = defaultdict(list)
            excluded: Counter[str] = Counter()
            for stage in ai:
                inference = stage["overlay"]["desk"].get("inference_settings", {})
                if stage["policy"].get("digest") != identity:
                    excluded["unregistered_desk_version"] += 1
                elif (
                    stage["policy"]["id"].endswith(":provider-default")
                    and inference.get("effort_recording") != "explicit.v1"
                ):
                    excluded["legacy_effort_identity_ambiguous"] += 1
                else:
                    series[stage["policy"]["id"]].append(stage)
            fields: dict[str, Any] = {}
            for target in (TEMPERATURE, QPF):
                try:
                    control = (
                        self._temperature_control(latitude, longitude, cutoff)
                        if target == TEMPERATURE
                        else self._qpf_control(latitude, longitude, start, cutoff)
                    )
                except ValueError as exc:
                    if "Too many QPF facts" not in str(exc):
                        raise
                    reasons.add("evidence_window_truncated")
                    fields[target] = {
                        "status": "unavailable",
                        "reason": "evidence_window_truncated",
                        "series": {},
                    }
                    continue
                control, outside = self._restricted(target, control, window)
                by_series = {}
                for name, members in sorted(series.items()):
                    ids = {stage["variant_id"] for stage in members}
                    pair = evaluate_pair(
                        target,
                        control,
                        members,
                        None,
                        ancestors=[s for s in stages if s["variant_id"] not in ids],
                        decision_window=(start, cutoff),
                    )
                    pair["control_samples_available"] = len(
                        control.get("samples", [])
                        if target == TEMPERATURE
                        else control.get("canonicalization", {}).get("samples", [])
                    )
                    pair.pop("rows")
                    pair["metrics_label"] = "conditional_on_retained_ai_stage"
                    by_series[name] = pair
                fields[target] = {"outside_window": outside, "series": by_series}
            results.append(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "composition": composition,
                    "excluded_stages": dict(excluded),
                    "fields": fields,
                }
            )
        common = sorted(
            {
                sample
                for row in results
                for target in row["fields"].values()
                for pair in target["series"].values()
                for sample in pair["common_sample_ids"]
            }
        )
        return {
            "window": {
                "start": _iso(start),
                "end": _iso(cutoff),
                "closure": "[start,end]",
                "desk_version_registered_at": _iso(registered),
            },
            "pair_reasons": sorted(reasons),
            "reference": "exact deterministic corrected parent (CONTROL for the AI family)",
            "cohort": {"by_location": results, "common_sample_ids": common},
            "cohort_digest": _digest(
                [
                    pair["cohort_digest"]
                    for row in results
                    for target in row["fields"].values()
                    for pair in target["series"].values()
                ]
            ),
        }

    # -------------------------------------------------------------- reporting
    def status(self, family: str | None = None) -> dict[str, Any]:
        now = datetime.now(UTC)
        rows = []
        for name in (family,) if family else FAMILIES:
            events, read_at, _ = self._read(name, None, None)
            by_scope: dict[str, list[GovernanceEvent]] = defaultdict(list)
            for event in events:
                by_scope[event.scope_key].append(event)
            for key, scope_events in sorted(by_scope.items()):
                head = head_at(scope_events)
                rows.append(
                    {
                        "family": name,
                        "scope_key": key,
                        "head_event_id": str(head.event_id) if head else None,
                        "head_event_type": head.event_type if head else None,
                        "active_policy_artifact_id": str(head.policy_artifact_id)
                        if head and head.policy_artifact_id
                        else None,
                        "candidates": [
                            str(row.policy_artifact_id)
                            for row in shadow_candidates_at(scope_events, read_at)
                        ],
                        "retired": [
                            str(row.policy_artifact_id)
                            for row in scope_events
                            if row.event_type == RETIRED
                        ],
                        "events": len(scope_events),
                    }
                )
        return {"as_of": _iso(now), "scopes": rows, "writes": 0}

    def history(
        self,
        *,
        policy_artifact_id: ArtifactId | None = None,
        family: str | None = None,
        scope_key: str | None = None,
    ) -> dict[str, Any]:
        if policy_artifact_id is not None:
            registration = self._registration(policy_artifact_id)
            family, scope_key = registration.family, registration.scope_key
        if family is None:
            raise GovernanceConflict("history_scope_required")
        events, _, _ = self._read(family, (scope_key,) if scope_key else None, None)
        return {
            "family": family,
            "scope_key": scope_key,
            "events": [row.model_dump(mode="json") for row in events],
            "intervals": [
                {
                    "event_id": str(row.event.event_id),
                    "event_type": row.event.event_type,
                    "policy_artifact_id": str(row.policy_artifact_id)
                    if row.policy_artifact_id
                    else None,
                    "effective_from": _iso(row.effective_from),
                    "effective_until": _iso(row.effective_until) if row.effective_until else None,
                    "closure": "[from,until)",
                }
                for key in sorted({row.scope_key for row in events})
                for row in intervals([e for e in events if e.scope_key == key])
            ],
            "writes": 0,
        }

    def resolve(self, family: str, scope_key: str, at: datetime) -> dict[str, Any]:
        snapshot = self.snapshot(family, [scope_key], at)
        return {**snapshot.scope(scope_key).summary(), "seconds": snapshot.seconds, "writes": 0}


def configured_governance() -> GovernanceService:
    return GovernanceService(configured_learning())


def _location(value: str) -> tuple[float, float]:
    latitude, longitude = (float(part) for part in value.split(","))
    return latitude, longitude


def _expected(value: str) -> GovernanceEventId | None:
    return None if value == GENESIS else GovernanceEventId(value)


def _target(value: str) -> ArtifactId | None:
    return None if value == "none" else ArtifactId(value)


class _JsonArgumentParser(argparse.ArgumentParser):
    """Argument errors use the same JSON error envelope and exit code as the commands."""

    def error(self, message: str) -> NoReturn:
        print(
            json.dumps({"error": {"code": "invalid_arguments", "message": message}}),
            file=sys.stderr,
        )
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Read-only current governed state")
    status.add_argument("--family", choices=FAMILIES)
    candidates = commands.add_parser("candidates", help="Read-only registered candidates")
    candidates.add_argument("--family", choices=FAMILIES)
    history = commands.add_parser("history", help="Read-only immutable events and intervals")
    history.add_argument("--policy-artifact-id", type=ArtifactId)
    history.add_argument("--family", choices=FAMILIES)
    history.add_argument("--lat", type=float)
    history.add_argument("--lon", type=float)
    history.add_argument("--field")
    resolve = commands.add_parser("resolve", help="Read-only state in force at a decision time")
    resolve.add_argument("--family", choices=FAMILIES, required=True)
    resolve.add_argument("--lat", type=float)
    resolve.add_argument("--lon", type=float)
    resolve.add_argument("--field")
    resolve.add_argument("--at", type=datetime.fromisoformat, required=True)
    show = commands.add_parser("show-evaluation", help="Read one recorded evaluation")
    show.add_argument("--evaluation-id", type=ArtifactId, required=True)
    evaluate = commands.add_parser(
        "evaluate", help="Deterministic eligibility; read-only unless --record"
    )
    evaluate.add_argument("--policy-artifact-id", type=ArtifactId, required=True)
    evaluate.add_argument("--information-cutoff", type=datetime.fromisoformat, required=True)
    evaluate.add_argument("--location", type=_location, action="append")
    evaluate.add_argument(
        "--window-start",
        type=datetime.fromisoformat,
        help="AI family only: explicit reporting window start (default: desk registration)",
    )
    evaluate.add_argument("--record", action="store_true")
    evaluate.add_argument("--actor")
    evaluate.add_argument("--reason")
    for name, text in (
        ("register", "Explicit shadow enablement of one candidate"),
        ("retire", "Retire one registered, non-active policy"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--policy-artifact-id", type=ArtifactId, required=True)
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
    desk = commands.add_parser("register-desk-version", help="Record the code-versioned desk")
    desk.add_argument("--actor", required=True)
    desk.add_argument("--reason", required=True)
    activate = commands.add_parser("activate", help="Explicit CAS activation")
    activate.add_argument("--policy-artifact-id", type=ArtifactId, required=True)
    activate.add_argument("--evaluation-id", type=ArtifactId, required=True)
    activate.add_argument("--expected-head", required=True, help="gev_... or genesis")
    activate.add_argument("--actor", required=True)
    activate.add_argument("--reason", required=True)
    rollback = commands.add_parser("rollback", help="Explicit rollback to none or a prior policy")
    rollback.add_argument("--expected-head", type=GovernanceEventId, required=True)
    rollback.add_argument("--target", required=True, help="art_... or none")
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--reason", required=True)
    emergency = commands.add_parser(
        "emergency-rollback", help="Restore the policy the ACTIVATED head superseded"
    )
    emergency.add_argument("--expected-head", type=GovernanceEventId, required=True)
    emergency.add_argument("--actor", required=True)
    emergency.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    def scope() -> str:
        if args.family == TEMPERATURE_CORRECTION and args.lat is not None and args.lon is not None:
            return correction_scope(args.lat, args.lon)
        if args.family == BLEND_POLICY and args.field:
            return blend_scope(args.field)
        if args.family == AI_DESK_POLICY:
            return DESK_SCOPE
        raise GovernanceConflict("scope_arguments_required", "Give --lat/--lon or --field")

    try:
        service = configured_governance()
        result: dict[str, Any]
        if args.command == "status":
            result = service.status(args.family)
        elif args.command == "candidates":
            state = service.status(args.family)
            result = {
                "scopes": [
                    {k: row[k] for k in ("family", "scope_key", "candidates")}
                    for row in state["scopes"]
                ],
                "writes": 0,
            }
        elif args.command == "history":
            result = service.history(
                policy_artifact_id=args.policy_artifact_id,
                family=args.family,
                scope_key=scope() if args.family and (args.lat is not None or args.field) else None,
            )
        elif args.command == "resolve":
            result = service.resolve(args.family, scope(), args.at)
        elif args.command == "show-evaluation":
            saved = service.learning.read(args.evaluation_id)
            if saved["payload"].get("schema_version") != EVALUATION_SCHEMA:
                raise GovernanceConflict("not_a_governance_evaluation")
            result = saved
        elif args.command == "evaluate":
            result = service.evaluate(
                args.policy_artifact_id,
                information_cutoff=args.information_cutoff,
                locations=args.location,
                record=args.record,
                actor=args.actor,
                reason=args.reason,
                window_start=args.window_start,
            )
        elif args.command == "register":
            result = service.register(args.policy_artifact_id, actor=args.actor, reason=args.reason)
        elif args.command == "register-desk-version":
            result = service.register_desk_version(actor=args.actor, reason=args.reason)
        elif args.command == "retire":
            result = service.retire(args.policy_artifact_id, actor=args.actor, reason=args.reason)
        elif args.command == "activate":
            result = service.activate(
                args.policy_artifact_id,
                args.evaluation_id,
                expected_head=_expected(args.expected_head),
                actor=args.actor,
                reason=args.reason,
            )
        elif args.command == "rollback":
            result = service.rollback(
                expected_head=args.expected_head,
                target=_target(args.target),
                actor=args.actor,
                reason=args.reason,
            )
        else:
            result = service.emergency_rollback(
                expected_head=args.expected_head, actor=args.actor, reason=args.reason
            )
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(json.dumps({"error": {"code": code, "message": str(exc)}}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
