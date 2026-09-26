"""Deterministic promotion eligibility over identical-sample pairwise cohorts.

Evidence qualification, promotion eligibility and activation stay separate. This
module decides eligibility only; nothing here activates, registers or edits a policy.
Qualification reuses the unchanged ``mesoforge-bias-evidence-policy.v1`` evaluator with
every criterion. No threshold, margin or significance level is introduced: a family
without an approved promotion rule is ``not_eligible`` with machine-readable reasons.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.coherence import DEW_POINT, GUST, TEMPERATURE, WIND
from mesoforge.verification.model_comparison import LEAD_BUCKETS
from mesoforge.verification.site_analysis import EVIDENCE_POLICY, evaluate_evidence
from mesoforge.verification.variant_evaluation import paired_date_intervals

GOVERNANCE_ELIGIBILITY_POLICY: dict[str, Any] = {
    "id": "mesoforge-governance-eligibility.v1",
    "version": "1",
    "separation": [
        "evidence qualification: the unchanged bias evidence policy with every criterion",
        "promotion eligibility: this versioned rule; eligible is a recorded fact only",
        "activation: a separate explicit operator act naming this exact evaluation",
    ],
    "temperature_correction": {
        "cohort": (
            "prospective shadow stages of exactly this registered candidate against the raw "
            "CONTROL stages of the same issued decisions, on identical canonical samples "
            "decided after registration and at or before the information cutoff, with valid "
            "times after the candidate's evidence cutoff"
        ),
        "rules": {
            "evidence_reproducible": (
                "the candidate digest is reproduced from raw-baseline evidence as of its own "
                "evidence cutoff with its own identity and creation time"
            ),
            "evidence_qualified": (
                f"{EVIDENCE_POLICY['id']} met with all criteria in every applied lead bucket, "
                "computed on the prospective cohort's raw CONTROL errors"
            ),
            "mae_and_rmse_improved": (
                "candidate MAE and RMSE both strictly lower than CONTROL in every applied lead "
                "bucket on identical samples; a descriptive improvement, not a significance claim"
            ),
            "unapplied_buckets_unchanged": "candidate equals CONTROL in every unapplied bucket",
            "no_candidate_execution_failures": (
                "no candidate-caused shadow execution failure in the window; stage-storage "
                "failures are infrastructure, excluded and reported"
            ),
            "no_active_policy": (
                "no ACTIVE correction at the information cutoff: no approved rule exists for "
                "replacing an active policy, so replacement needs an audited rollback first"
            ),
        },
        "reported_only": [
            "date-clustered paired error-difference intervals",
            "candidate versus CURRENT_ACTIVE comparison",
            "cross-bucket aggregate metrics",
        ],
    },
    "blend_policy": {
        "decision": "not_eligible",
        "reason": "promotion_rule_not_defined",
        "field_reasons": {
            TEMPERATURE: "temperature_recipe_activation_unsupported",
            DEW_POINT: "no_verification_contract_for_field",
            WIND: "no_verification_contract_for_field",
            GUST: "no_verification_contract_for_field",
        },
    },
    "ai_desk_policy": {"decision": "not_eligible", "reason": "no_approved_ai_promotion_rule"},
    "owner_specified_sources": [
        "Promotion must weigh at least MAE and RMSE, not mean bias alone, on identical "
        "samples against the unchanged baseline (site evidence policy promotion note).",
        "Human, versioned promotion decision only if improvement is demonstrated.",
        "Families without a rule report not_eligible with machine-readable reasons.",
        "No single-metric promotion; evidence qualification, eligibility and activation "
        "remain separate.",
    ],
}


def eligibility_policy_digest() -> str:
    return str(canonical_json_digest(GOVERNANCE_ELIGIBILITY_POLICY))


def evidence_policy_identity() -> dict[str, Any]:
    return {"id": EVIDENCE_POLICY["id"], "criteria": dict(EVIDENCE_POLICY["criteria"])}


def qualification_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Map common cohort rows to the evidence evaluator's CONTROL (raw) error rows."""
    return [
        {
            "target_reference_time": row["reference_time"],
            "valid_time": row["valid_time"],
            "temperature_error_k": row["reference_value"] - row["observed_value"],
            "observation": {"station_id": row["observation_source"]},
        }
        for row in rows
    ]


def revalidate_qualification(qualification: Mapping[str, Any]) -> None:
    """A stored qualification must carry the complete, unchanged evidence criteria."""
    approved = EVIDENCE_POLICY["criteria"]
    for bucket, evidence in qualification.items():
        criteria = evidence.get("criteria", {}) if isinstance(evidence, Mapping) else {}
        if set(criteria) != set(approved) or any(
            criteria[name].get("required") != required for name, required in approved.items()
        ):
            raise ValueError(f"Stored evidence qualification weakens the policy in {bucket}")


def temperature_correction_eligibility(
    pair: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    reproduction: Mapping[str, Any],
    active_at_cutoff: Mapping[str, Any] | None,
    candidate_failures: int,
) -> dict[str, Any]:
    """Apply the versioned rule; every failed rule contributes a machine-readable reason."""
    rows = pair["rows"]
    reasons: list[str] = []
    if reproduction.get("status") != "reproduced":
        reasons.append(str(reproduction.get("reason", "evidence_not_reproducible")))
    applied = [
        bucket for bucket in LEAD_BUCKETS if policy["lead_buckets"][bucket]["delta_k"] is not None
    ]
    qualification: dict[str, Any] = {}
    improvement: dict[str, Any] = {}
    for bucket in applied:
        bucket_rows = [row for row in rows if row["lead_bucket"] == bucket]
        evidence = evaluate_evidence(qualification_rows(bucket_rows))
        qualification[bucket] = evidence
        if evidence["status"] != "evidence_policy_met":
            reasons.append(f"insufficient_evidence:{bucket}")
        metrics = pair["by_lead_bucket"][bucket]
        mae, rmse = metrics["candidate"]["mae"], metrics["candidate"]["rmse"]
        control_mae, control_rmse = metrics["reference"]["mae"], metrics["reference"]["rmse"]
        improved = (
            bool(bucket_rows)
            and mae is not None
            and rmse is not None
            and mae < control_mae
            and rmse < control_rmse
        )
        improvement[bucket] = {
            "mae_lower": bool(bucket_rows) and mae is not None and mae < control_mae,
            "rmse_lower": bool(bucket_rows) and rmse is not None and rmse < control_rmse,
            "label": "descriptive improvement on identical samples; not a significance claim",
            "paired_intervals": paired_date_intervals(bucket_rows),
        }
        if not improved:
            reasons.append(f"mae_and_rmse_not_both_improved:{bucket}")
    for bucket in LEAD_BUCKETS:
        if bucket in applied:
            continue
        if any(
            row["candidate_value"] != row["reference_value"]
            for row in rows
            if row["lead_bucket"] == bucket
        ):
            reasons.append(f"unapplied_bucket_changed:{bucket}")
    if not applied:
        reasons.append("no_applied_lead_bucket")
    if candidate_failures:
        reasons.append("candidate_execution_failed")
    if active_at_cutoff is not None:
        reasons.append("active_replacement_rule_not_defined")
    return {
        "decision": "not_eligible" if reasons else "eligible",
        "reasons": sorted(set(reasons)),
        "applied_lead_buckets": applied,
        "evidence_qualification": qualification,
        "improvement": improvement,
        "rule": {"id": GOVERNANCE_ELIGIBILITY_POLICY["id"], "digest": eligibility_policy_digest()},
    }


def blend_eligibility(field: str) -> dict[str, Any]:
    field_reasons = GOVERNANCE_ELIGIBILITY_POLICY["blend_policy"]["field_reasons"]
    reasons = ["promotion_rule_not_defined"]
    if field in field_reasons:
        reasons.append(field_reasons[field])
    return {
        "decision": "not_eligible",
        "reasons": sorted(reasons),
        "rule": {"id": GOVERNANCE_ELIGIBILITY_POLICY["id"], "digest": eligibility_policy_digest()},
    }


def ai_eligibility() -> dict[str, Any]:
    return {
        "decision": "not_eligible",
        "reasons": ["no_approved_ai_promotion_rule"],
        "rule": {"id": GOVERNANCE_ELIGIBILITY_POLICY["id"], "digest": eligibility_policy_digest()},
    }
