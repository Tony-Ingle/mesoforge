"""Compact temperature-correction policies and local transformations, never promotion.

Site evidence determines candidate eligibility only. An immutable candidate payload
executes only under an explicit governance grant derived from a committed governance
event (operational for the ACTIVE policy, shadow for a registered candidate); the
payload's own lifecycle role never grants execution. Native contributors and the
shared baseline stay unchanged; only affected local fields are copied.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.common.horizon import LEGACY_HORIZON, horizon_for
from mesoforge.common.identifiers import LearningPolicyId
from mesoforge.contracts.policy_governance import GovernanceGrant
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.coherence import BASELINE_COHERENCE, DEW_POINT, RH, TEMPERATURE
from mesoforge.verification.model_comparison import LEAD_BUCKETS, lead_bucket
from mesoforge.verification.site_analysis import EVIDENCE_POLICY, evaluate_evidence

POLICY_SCHEMA = "mesoforge.temperature-correction-policy.v1"
SPATIAL_RECIPE = "uniform_configured_local_domain_offset.v1"


def _time(value: Any) -> datetime:
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Correction timestamps require a timezone")
    return instant.astimezone(UTC)


def _iso(value: Any) -> str:
    return _time(value).isoformat().replace("+00:00", "Z")


def _digest(policy: dict[str, Any]) -> str:
    return str(
        canonical_json_digest({key: value for key, value in policy.items() if key != "digest"})
    )


def _delta_for_lead(policy: dict[str, Any], lead: int) -> float | None:
    """The approved correction buckets end at 36h, independently of forecast length."""
    if type(lead) is not int or lead < 1:
        raise ValueError("Correction lead must be a positive integer")
    if lead > LEGACY_HORIZON.duration_hours:
        return None
    value: float | None = policy["lead_buckets"][lead_bucket(lead)]["delta_k"]
    return value


def propose_temperature_policy(
    analysis: dict[str, Any],
    *,
    policy_id: LearningPolicyId,
    version: str,
    evidence_cutoff: datetime,
    created_at: datetime,
) -> dict[str, Any]:
    """Propose negative mean bias independently for qualified canonical lead buckets.

    A response with ``insufficient_evidence`` is an eligibility report, not a
    candidate to register. Uniform local-domain application is explicit policy
    data requiring shadow/active approval; station evidence proves no spatial skill.
    """
    policy_id = LearningPolicyId(policy_id)
    cutoff, created = _time(evidence_cutoff), _time(created_at)
    if cutoff > created:
        raise ValueError("Correction creation precedes its evidence cutoff")
    if not policy_id or not version:
        raise ValueError("Correction policy requires an identity and version")
    evaluation = analysis.get("evaluation", {})
    if (
        evaluation.get("evidence_availability") != "verified_input_cutoff_and_fact_registration"
        or evaluation.get("forecast_stage_scope") != "raw_baseline_only"
        or _time(evaluation.get("evidence_cutoff")) != cutoff
    ):
        raise ValueError(
            "Correction proposal requires cutoff-filtered canonical evidence of the raw baseline"
        )
    if analysis.get("evidence_policy", {}).get("id") != EVIDENCE_POLICY["id"]:
        raise ValueError("Unsupported temperature evidence policy")
    samples = analysis["samples"]
    coordinate = analysis["coordinate"]
    if any(
        sample["latitude"] != coordinate["latitude"]
        or sample["longitude"] != coordinate["longitude"]
        or _time(sample["valid_time"]) > cutoff
        or _time(sample["observation"]["observation_time"]) > cutoff
        for sample in samples
    ):
        raise ValueError("Correction evidence belongs to another coordinate or future event")
    buckets: dict[str, Any] = {}
    for bucket in LEAD_BUCKETS:
        evidence = evaluate_evidence([s for s in samples if s["lead_bucket"] == bucket])
        qualified = evidence["status"] == "evidence_policy_met"
        buckets[bucket] = {
            "status": "candidate" if qualified else "insufficient_evidence",
            "delta_k": -evidence["mean_bias_k"] if qualified else None,
            "evidence": evidence,
        }
    policy = {
        "schema_version": POLICY_SCHEMA,
        "policy_id": policy_id,
        "version": version,
        "field": TEMPERATURE,
        "unit": "K",
        "lifecycle_role": "candidate"
        if any(row["status"] == "candidate" for row in buckets.values())
        else "insufficient_evidence",
        "evidence_policy_id": EVIDENCE_POLICY["id"],
        "evidence_cutoff": _iso(cutoff),
        "created_at": _iso(created),
        "activated_at": None,
        "coordinate": dict(coordinate),
        "spatial_application": SPATIAL_RECIPE,
        "spatial_limitation": (
            "A site/proxy bias is applied uniformly only under this explicit local-domain "
            "recipe; this is not evidence of bias skill at every surrounding grid cell."
        ),
        "method": "negative_canonical_sample_mean_forecast_minus_observation_bias",
        "lead_buckets": buckets,
        "provenance": {
            "canonical_fact_ids": sorted({str(s["canonical_fact_id"]) for s in samples}),
            "canonicalization_policy": analysis.get("canonicalization_policy", {}).get("id"),
        },
    }
    return {**policy, "digest": _digest(policy)}


def validate_temperature_policy(policy: dict[str, Any]) -> None:
    """Validate immutable science/identity independent of a particular forecast cutoff."""
    if policy.get("schema_version") != POLICY_SCHEMA or policy.get("digest") != _digest(policy):
        raise ValueError("Correction policy schema/digest is invalid")
    if not policy.get("policy_id") or not policy.get("version"):
        raise ValueError("Correction policy requires immutable identity/version")
    if policy.get("field") != TEMPERATURE or policy.get("unit") != "K":
        raise ValueError("Only temperature correction in kelvin is supported")
    if policy.get("evidence_policy_id") != EVIDENCE_POLICY["id"]:
        raise ValueError("Correction evidence policy is unsupported")
    if policy.get("spatial_application") != SPATIAL_RECIPE:
        raise ValueError("Correction spatial recipe is unsupported")
    evidence, created = _time(policy["evidence_cutoff"]), _time(policy["created_at"])
    if evidence > created:
        raise ValueError("Correction creation precedes its evidence cutoff")
    role = policy.get("lifecycle_role")
    if role not in ("candidate", "shadow", "active", "retired", "insufficient_evidence"):
        raise ValueError("Unsupported correction lifecycle role")
    if role in ("shadow", "active"):
        activated = _time(policy["activated_at"])
        if created > activated:
            raise ValueError("Correction activation precedes its creation")
    if set(policy["lead_buckets"]) != set(LEAD_BUCKETS):
        raise ValueError("Correction policy must explicitly retain every independent lead bucket")
    for row in policy["lead_buckets"].values():
        delta = row["delta_k"]
        if delta is None:
            continue
        evidence_row = row["evidence"]
        criteria = evidence_row["criteria"]
        approved = EVIDENCE_POLICY["criteria"]
        if set(criteria) != set(approved) or any(
            criteria[name]["required"] != required for name, required in approved.items()
        ):
            raise ValueError("Correction cannot weaken the approved evidence thresholds")
        low, high = evidence_row["mean_bias_uncertainty"]["interval_95_k"]
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or not math.isfinite(delta)
            or evidence_row.get("status") != "evidence_policy_met"
            or delta != -evidence_row["mean_bias_k"]
            or not all(item["met"] for item in criteria.values())
            or criteria["min_canonical_samples"]["observed"] < approved["min_canonical_samples"]
            or criteria["min_distinct_decision_dates"]["observed"]
            < approved["min_distinct_decision_dates"]
            or criteria["max_share_from_one_decision_date"]["observed"]
            > approved["max_share_from_one_decision_date"]
            or low <= 0 <= high
        ):
            raise ValueError("Correction delta must retain the qualified negative mean bias")


def _validate(policy: dict[str, Any], forecast: dict[str, Any], cutoff: datetime) -> None:
    validate_temperature_policy(policy)
    if policy.get("coordinate") != {
        "latitude": forecast["latitude"],
        "longitude": forecast["longitude"],
    }:
        raise ValueError("Correction policy belongs to another configured coordinate")
    if _time(policy["created_at"]) > cutoff:
        raise ValueError("Correction policy was created after analysis")


def apply_temperature_correction(
    forecast: dict[str, Any],
    policy: dict[str, Any] | None,
    *,
    analysis_cutoff: datetime,
    mode: Literal["operational", "shadow"] = "operational",
    grant: GovernanceGrant | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one explicit policy atomically, or retain the exact uncorrected input.

    Execution requires a candidate payload and a governance grant for this mode whose
    effective time lies between the policy's creation and the analysis cutoff. Legacy
    payloads carrying shadow/active/retired roles remain readable history and never
    execute. The returned compact overlay owns only affected fields and references its
    policy/parent. Failed transformations never escape partially.
    """
    if mode not in ("operational", "shadow"):
        raise ValueError("Correction execution requires operational or shadow mode")
    cutoff = _time(analysis_cutoff)
    outcome: dict[str, Any] = {
        "status": "no_policy",
        "mode": mode,
        "policy": None,
        "applied_delta_k": 0.0,
        "analysis_cutoff": _iso(cutoff),
        "changes": [],
        "coherence": {"relationships": [], "changed_cell_hours": 0},
    }
    horizon = horizon_for(forecast)
    if horizon.duration_hours > LEGACY_HORIZON.duration_hours:
        outcome["uncovered_leads"] = {
            "status": "no_policy",
            "reason": "Approved temperature correction evidence buckets end at hour 36",
            "hours": list(horizon.leads[LEGACY_HORIZON.duration_hours :]),
        }
    if policy is None:
        return forecast, outcome
    outcome["policy"] = {
        **{key: policy.get(key) for key in ("policy_id", "version", "digest", "lifecycle_role")},
        "governed_role": grant.role if grant is not None else None,
        "grant_event_id": str(grant.event_id) if grant is not None else None,
    }
    try:
        _validate(policy, forecast, cutoff)
        role = policy["lifecycle_role"]
        outcome["lead_buckets"] = {
            key: {"status": row["status"], "delta_k": row["delta_k"]}
            for key, row in policy["lead_buckets"].items()
        }
        permitted = (
            role == "candidate"
            and grant is not None
            and grant.role == mode
            and _time(policy["created_at"]) <= _time(grant.effective_from) <= cutoff
        )
        if not permitted:
            outcome["status"] = (
                role if role in ("insufficient_evidence", "retired") else "not_active"
            )
            return forecast, outcome
        if not any(row["delta_k"] for row in policy["lead_buckets"].values()):
            outcome["status"] = "no_op"
            return forecast, outcome
        grid = forecast["local_grid_baseline"]
        cells: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        for cell in grid["cells"]:
            hours: list[dict[str, Any]] = []
            for hour in cell["hours"]:
                delta = _delta_for_lead(policy, hour["horizon_hours"])
                value = hour["temperature"]["value"]
                if not delta or value is None:
                    hours.append(hour)
                    continue
                if hour["temperature"]["unit"] != "K" or not 150 <= value + delta <= 340:
                    raise ValueError(
                        "Corrected temperature is outside the current scientific contract"
                    )
                fields = hour["surface"]["fields"]
                if fields[TEMPERATURE]["value"] != value:
                    raise ValueError("Point and surface temperature representations disagree")
                working = {**fields, TEMPERATURE: {**fields[TEMPERATURE], "value": value + delta}}
                coherent, report = BASELINE_COHERENCE.apply_local_fields(
                    working, changed_fields=(TEMPERATURE,)
                )
                updated = {
                    **hour,
                    "temperature": {**hour["temperature"], "value": value + delta},
                    "surface": {**hour["surface"], "fields": coherent},
                }
                hours.append(updated)
                changes.append(
                    {
                        "x_index": cell["x_index"],
                        "y_index": cell["y_index"],
                        "horizon_hours": hour["horizon_hours"],
                        "valid_time": hour["valid_time"],
                        "delta_k": delta,
                        "fields": {name: coherent[name] for name in (TEMPERATURE, DEW_POINT, RH)},
                        "coherence": report,
                    }
                )
            cells.append(
                {**cell, "hours": hours}
                if any(a is not b for a, b in zip(hours, cell["hours"], strict=True))
                else cell
            )
        if not changes:
            outcome["status"] = "no_op"
            return forecast, outcome
        extracted = extract_grid_point(
            {**grid, "cells": cells},
            latitude=forecast["latitude"],
            longitude=forecast["longitude"],
            copy_grid=False,
        )
        outcome.update(
            status="applied",
            applied_delta_k=None,
            changes=changes,
            parent_grid_sha256=forecast.get("local_grid", {}).get("sha256"),
            corrected_grid_sha256=extracted["local_grid"]["sha256"],
            coherence={
                "relationships": ["blended_dew_point_consistency", "relative_humidity"],
                "changed_cell_hours": len(changes),
            },
            point_values=[
                {
                    "horizon_hours": row["horizon_hours"],
                    "valid_time": row["valid_time"],
                    "temperature": row["temperature"],
                    "baseline_temperature": original["temperature"],
                    "applied_delta_k": (
                        _delta_for_lead(policy, row["horizon_hours"]) or 0.0
                        if original["temperature"]["value"] is not None
                        else 0.0
                    ),
                }
                for original, row in zip(forecast["hours"], extracted["hours"], strict=True)
            ],
        )
        return {**forecast, **extracted}, outcome
    except Exception as exc:
        outcome.update(status="fallback", reason=f"{type(exc).__name__}: {exc}")
        return forecast, outcome
