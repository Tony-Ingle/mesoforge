"""Repository-level governance fixtures for tests only.

Activation through the service requires an eligible recorded evaluation. Execution,
pinning and failure tests that need an ACTIVE policy append the lifecycle event
directly through the repository, which still enforces every append invariant
(registration, retirement, chain head, artifact availability). No test path here
reaches a real configured store.
"""

from __future__ import annotations

from typing import Any

from mesoforge.common.identifiers import ArtifactId, Digest, GovernanceEventId
from mesoforge.contracts.policy_governance import (
    CHAIN_EVENTS,
    GOVERNANCE_POLICY_VERSION,
    GovernanceEvent,
    governance_policy_digest,
    head_at,
    request_digest,
    request_key,
)

FIXTURE_REVISION = "a" * 40


def append_event(
    factory: Any,
    event_type: str,
    *,
    family: str,
    scope_key: str,
    policy: str | None = None,
    content_digest: str | None = None,
    evaluation: str | None = None,
    decision: str | None = None,
    information_cutoff: Any = None,
    payload: dict[str, Any] | None = None,
    actor: str = "test-fixture",
) -> GovernanceEvent:
    """Append one event with the correct sequence and chain predecessor."""
    with factory() as uow:
        events = list(uow.governance.events(family, (scope_key,)))
        head = head_at(events)
        chain = event_type in CHAIN_EVENTS
        key = request_key(
            fixture=event_type, scope=scope_key, policy=policy, sequence=len(events) + 1
        )
        event = GovernanceEvent(
            event_id=GovernanceEventId.generate(),
            family=family,  # type: ignore[arg-type]
            scope_key=scope_key,
            scope_seq=max((row.scope_seq for row in events), default=0) + 1,
            event_type=event_type,  # type: ignore[arg-type]
            policy_artifact_id=ArtifactId(policy) if policy else None,
            policy_content_digest=Digest(content_digest) if content_digest else None,
            previous_head_event_id=head.event_id if chain and head is not None else None,
            rolled_back_event_id=(
                head.event_id
                if event_type in ("ROLLED_BACK", "EMERGENCY_ROLLED_BACK") and head is not None
                else None
            ),
            evaluation_artifact_id=ArtifactId(evaluation) if evaluation else None,
            decision=decision,  # type: ignore[arg-type]
            information_cutoff=information_cutoff,
            governance_policy_version=GOVERNANCE_POLICY_VERSION,
            governance_policy_digest=governance_policy_digest(),
            actor=actor,
            reason="repository-level test fixture",
            code_revision=FIXTURE_REVISION,
            environment_digest=Digest.of_bytes(b"fixture"),
            request_key=key,
            request_digest=request_digest(key, actor=actor, reason="repository-level test fixture"),
            payload=payload,
        )
        stored: GovernanceEvent = uow.governance.append(event)
        uow.commit()
    return stored


def fixture_evaluation(service: Any) -> dict[str, Any]:
    """A saved evaluation artifact reference for repository-level ACTIVATED fixtures."""
    return service.save(
        "governance-evaluation",
        {"schema_version": "mesoforge.governance-evaluation.v1", "fixture": "repository-level"},
    )


def shift_forecast(forecast: dict[str, Any], delta: Any) -> dict[str, Any]:
    """The same synthetic local grid decided ``delta`` later; values are unchanged."""
    from copy import deepcopy
    from datetime import datetime

    from mesoforge.application.local_surface_grid import extract_grid_point

    def later(value: str) -> str:
        return (datetime.fromisoformat(value) + delta).isoformat()

    grid = deepcopy(forecast["local_grid_baseline"])
    context = grid["forecast_context"]
    context["target_reference_time"] = later(context["target_reference_time"])
    for cell in grid["cells"]:
        for hour in cell["hours"]:
            hour["valid_time"] = later(hour["valid_time"])
            for value in hour.get("surface", {}).get("fields", {}).values():
                for key in ("interval_start", "interval_end"):
                    if isinstance(value, dict) and value.get(key):
                        value[key] = later(value[key])
    point = extract_grid_point(
        grid, latitude=forecast["latitude"], longitude=forecast["longitude"], copy_grid=False
    )
    point["baseline_snapshot"] = {
        **forecast["baseline_snapshot"],
        "forecast_analysis_cutoff": later(
            forecast["baseline_snapshot"]["forecast_analysis_cutoff"]
        ),
    }
    return point


def synthetic_control(
    decisions: list[tuple[dict[str, Any], Any]], *, raw_bias_k: float
) -> dict[str, Any]:
    """Canonical temperature control samples for issued synthetic decisions.

    Each observation is the saved raw baseline value minus ``raw_bias_k``. This stands
    in for verified METAR facts only; stages, bindings and issuances are real.
    """
    from mesoforge.verification.model_comparison import lead_bucket

    samples = []
    for forecast, record in decisions:
        issued = str(record.issued_forecast_id)
        for hour in forecast["hours"]:
            raw = hour["surface"]["fields"]["air_temperature_2m"]["value"]
            observed = raw - raw_bias_k
            observation = {
                "temperature_k": observed,
                "revision_digest": str(Digest.of_bytes(f"obs:{hour['valid_time']}".encode())),
                "logical_observation_digest": str(Digest.of_bytes(hour["valid_time"].encode())),
                "station_id": "KMSP",
                "network": "SYNTHETIC",
                "provider": "synthetic-fixture",
                "observation_time": hour["valid_time"],
            }
            samples.append(
                {
                    "latitude": forecast["latitude"],
                    "longitude": forecast["longitude"],
                    "valid_time": hour["valid_time"],
                    "target_reference_time": forecast["target_reference_time"],
                    "horizon_hours": hour["horizon_hours"],
                    "lead_bucket": lead_bucket(hour["horizon_hours"]),
                    "verification_policy_id": "synthetic-temperature-match.v1",
                    "forecast_temperature_k": hour["temperature"]["value"],
                    "temperature_error_k": hour["temperature"]["value"] - observed,
                    "observation": observation,
                    "canonical_issued_forecast_id": issued,
                    "canonical_fact_id": f"synthetic-{issued}-{hour['horizon_hours']}",
                    "provenance": {"versions": [{"issued_forecast_id": issued, "fact_ids": []}]},
                }
            )
    return {
        "samples": samples,
        "canonicalization": {"policy": "synthetic-fixture"},
        "inventory": {"legacy_scan": {"truncated": False}},
    }


def past_policy(policy: dict[str, Any], *, evidence_cutoff: Any, created_at: Any) -> dict[str, Any]:
    """The same fixture science declared at earlier times, so a real database clock can
    register it. Evidence content is unchanged; only its declared times move."""
    from mesoforge.application.corrections import _digest

    moved = {
        **policy,
        "evidence_cutoff": evidence_cutoff.isoformat(),
        "created_at": created_at.isoformat(),
    }
    moved["digest"] = _digest(moved)
    return moved
