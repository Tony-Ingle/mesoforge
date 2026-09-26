"""Shared canonical-event evaluation of compact, immutable variant stages."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.verification.qpf_analysis import analyze_qpf_facts
from mesoforge.verification.site_analysis import analyze_facts
from mesoforge.verification.variant_evaluation import QPF, TEMPERATURE, evaluate_variants
from tests.unit.verification.test_qpf_analysis import fact as qpf_fact
from tests.unit.verification.test_site_analysis import fact as temperature_fact


def stage(sample: dict[str, Any], field: str = TEMPERATURE, **overrides: Any) -> dict[str, Any]:
    reference = sample["target_reference_time"]
    cutoff = datetime.fromisoformat(reference)
    row = {
        "variant_id": "stage-one",
        "parent_stage_id": "control-stage",
        "transformation_type": "DETERMINISTIC_CORRECTED",
        "lifecycle_role": "shadow",
        "fields": [field],
        "policy": {"id": "fixture-only-correction", "version": "1"},
        "baseline_snapshot_id": "baseline-a",
        "prepared_snapshot_id": "prepared-a",
        "location": {"latitude": sample["latitude"], "longitude": sample["longitude"]},
        "reference_time": reference,
        "analysis_cutoff": cutoff.isoformat(),
        "evidence_cutoff": (cutoff - timedelta(days=1)).isoformat(),
        "policy_created_at": (cutoff - timedelta(hours=2)).isoformat(),
        "policy_activated_at": (cutoff - timedelta(hours=1)).isoformat(),
        "created_at": (cutoff + timedelta(minutes=1)).isoformat(),
        "activated_at": (cutoff - timedelta(hours=1)).isoformat(),
        "code_identity": {"revision": "test-fixture"},
        "control_issued_forecast_ids": [sample["canonical_issued_forecast_id"]],
        "overlay": {"inherit_unchanged": True, "predictions": []},
    }
    row.update(overrides)
    return row


def prediction(
    sample: dict[str, Any], value: float | None, field: str = TEMPERATURE
) -> dict[str, Any]:
    return {
        "field": field,
        "valid_time": sample.get("valid_time", sample.get("interval_end")),
        "value": value,
        "unit": "K" if field == TEMPERATURE else "mm",
        **(
            {
                "interval_start": sample["interval_start"],
                "interval_end": sample["interval_end"],
            }
            if field == QPF
            else {}
        ),
    }


def comparison(result: dict[str, Any]) -> dict[str, Any]:
    return next(iter(result["comparisons"].values()))


def test_noop_reuses_control_and_cannot_inflate_canonical_samples() -> None:
    analysis = analyze_facts([temperature_fact(), temperature_fact(artifact="duplicate")])
    sample = analysis["samples"][0]
    variant = stage(
        sample,
        evidence_cutoff=None,
        evidence_required=False,
        evidence_status="no_policy",
        policy_created_at=None,
        policy_activated_at=None,
    )
    before = deepcopy((analysis, variant))
    result = evaluate_variants(TEMPERATURE, analysis, [variant, deepcopy(variant)])
    assert result["canonical_control_samples"] == result["shared_sample_count"] == 1
    metrics = comparison(result)
    assert metrics["control"] == metrics["variant"]
    assert metrics["metric_deltas_variant_minus_control"] == {"bias": 0, "mae": 0, "rmse": 0}
    assert result["writes"] == 0
    assert (analysis, variant) == before
    assert evaluate_variants(TEMPERATURE, analysis, [variant, deepcopy(variant)]) == result


@pytest.mark.parametrize("field", [TEMPERATURE, QPF])
def test_ai_final_and_corrected_parent_compare_same_event_without_control_aliasing(field):
    analysis = (
        analyze_facts([temperature_fact(forecast=297, observed=295)])
        if field == TEMPERATURE
        else analyze_qpf_facts([qpf_fact()])
    )
    sample = (
        analysis["samples"][0]
        if field == TEMPERATURE
        else analysis["canonicalization"]["samples"][0]
    )
    raw_value = 294.0 if field == TEMPERATURE else 1.0
    final_value = (
        sample["forecast_temperature_k"]
        if field == TEMPERATURE
        else sample["forecast"]["amount_mm"]
    )
    raw = stage(
        sample,
        field,
        variant_id="raw",
        transformation_type="active_baseline",
        parent_stage_id=None,
        evidence_required=False,
        evidence_status="baseline",
        policy={"id": "raw", "version": "1"},
        overlay={"inherit_unchanged": False, "predictions": [prediction(sample, raw_value, field)]},
    )
    corrected = stage(
        sample,
        field,
        variant_id="corrected",
        parent_stage_id="raw",
        transformation_type="deterministic_corrected",
        evidence_required=False,
        evidence_status="no_policy",
        policy={"id": "no-policy", "version": "1"},
        overlay={"inherit_unchanged": True, "inheritance_basis": "parent_stage", "predictions": []},
    )
    ai = stage(
        sample,
        field,
        variant_id="ai",
        parent_stage_id="corrected",
        transformation_type="ai_adjusted",
        lifecycle_role="active",
        policy={"id": "desk", "version": "1"},
        evidence_basis="pinned_forecast_evidence",
        evidence_cutoff=raw["analysis_cutoff"],
        policy_created_at=None,
        policy_activated_at=None,
        validation={"status": "valid"},
        overlay={
            "inherit_unchanged": False,
            "predictions": [prediction(sample, final_value, field)],
        },
    )
    ai["pinned_evidence"] = {
        k: ai[k] for k in ("baseline_snapshot_id", "prepared_snapshot_id", "analysis_cutoff")
    }
    ai["pinned_evidence"]["corrected_stage_id"] = "corrected"
    result = evaluate_variants(field, analysis, [raw, corrected, ai])
    assert result["shared_sample_count"] == 1 and result["exclusions"] == []
    metrics = {r["policy_id"]: r for r in result["comparisons"].values()}
    assert metrics["raw"]["variant"] == metrics["no-policy"]["variant"]
    assert metrics["desk"]["variant"] == metrics["desk"]["control"]
    selected = evaluate_variants(field, analysis, [corrected], ancestor_stages=[raw])
    assert selected["shared_sample_count"] == 1 and len(selected["comparisons"]) == 1
    assert next(iter(selected["comparisons"].values()))["variant"] == metrics["raw"]["variant"]


def test_policy_series_spans_locations_dates_and_leads_without_new_canonicalizer() -> None:
    first = analyze_facts([temperature_fact(forecast=297, observed=295)])
    other = analyze_facts(
        [
            temperature_fact(
                target=datetime(2026, 9, 18, 22, tzinfo=UTC),
                horizon=8,
                forecast=294,
                observed=295,
                latitude=45.8,
                longitude=-93.1,
            )
        ]
    )
    samples = first["samples"] + other["samples"]
    variants = [
        stage(
            sample,
            variant_id=f"stage-{index}",
            overlay={"predictions": [prediction(sample, value)], "inherit_unchanged": False},
        )
        for index, (sample, value) in enumerate(zip(samples, (295.0, 295.0), strict=True))
    ]
    result = evaluate_variants(TEMPERATURE, {"samples": samples}, variants)
    assert result["shared_sample_count"] == 2
    assert len(result["comparisons"]) == 1
    metrics = comparison(result)
    assert metrics["control"] == {"sample_count": 2, "bias": 0.5, "mae": 1.5, "rmse": (2.5) ** 0.5}
    assert metrics["variant"] == {"sample_count": 2, "bias": 0, "mae": 0, "rmse": 0}
    assert len(metrics["by_location"]) == 2
    assert metrics["by_lead_bucket"]["19-36"]["variant"]["sample_count"] == 0
    assert result["concentration"]["decision_date_utc"] == {"2026-09-16": 1, "2026-09-18": 1}
    assert "winner" not in result


def test_every_policy_series_uses_same_intersection_and_ai_identity_needs_no_new_engine() -> None:
    analysis = analyze_facts([temperature_fact(), temperature_fact(horizon=2, artifact="two")])
    first, second = analysis["samples"]
    correction = stage(first)
    ai_named_fixture = stage(
        first,
        variant_id="synthetic-future-ai-identity",
        transformation_type="FUTURE_AI_ADJUSTED",
        policy={"id": "synthetic-ai-fixture-no-ai-execution", "version": "1"},
        overlay={"inherit_unchanged": False, "predictions": [prediction(second, 295.5)]},
    )
    result = evaluate_variants(TEMPERATURE, analysis, [correction, ai_named_fixture])
    assert result["shared_sample_count"] == 1
    assert all(value["control"]["sample_count"] == 1 for value in result["comparisons"].values())
    assert result["samples"][0]["valid_time"] == second["valid_time"]
    assert result["exclusion_counts"] == {"variant_prediction_unavailable": 1}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"evidence_cutoff": "2026-09-17T00:00:00Z"}, "future_learning_evidence"),
        (
            {"policy_created_at": "2026-09-17T00:00:00Z"},
            "variant_policy_created_at_after_analysis_cutoff",
        ),
        (
            {"policy_activated_at": "2026-09-17T00:00:00Z"},
            "variant_policy_activated_at_after_analysis_cutoff",
        ),
        ({"evidence_cutoff": None}, "variant_timing_or_identity_unproven"),
        ({"analysis_cutoff": "2026-09-16T22:00:00"}, "variant_timing_or_identity_unproven"),
        (
            {"control_issued_forecast_ids": ["different-issued-version"]},
            "variant_not_bound_to_control_event",
        ),
    ],
)
def test_leakage_and_lineage_rejection(change: dict[str, Any], reason: str) -> None:
    analysis = analyze_facts([temperature_fact()])
    result = evaluate_variants(TEMPERATURE, analysis, [stage(analysis["samples"][0], **change)])
    assert result["shared_sample_count"] == 0
    assert result["exclusion_counts"] == {reason: 1}
    assert comparison(result)["variant"]["mae"] is None


def test_observation_population_and_conflicting_stage_rejected() -> None:
    analysis = analyze_facts([temperature_fact()])
    sample = analysis["samples"][0]
    variant = stage(sample, overlay={"predictions": [prediction(sample, 295)]})
    variant["overlay"]["predictions"][0]["observation_revision"] = "different-revision"
    result = evaluate_variants(TEMPERATURE, analysis, [variant])
    assert result["exclusion_counts"] == {"different_observation_population": 1}
    variant["overlay"]["predictions"][0].pop("observation_revision")
    different = deepcopy(variant)
    different["variant_id"] = "conflicting-stage"
    different["overlay"]["predictions"][0]["value"] = 294
    result = evaluate_variants(TEMPERATURE, analysis, [variant, different])
    assert result["exclusion_counts"] == {"conflicting_variant_stages": 1}


def test_qpf_uses_same_native_mrms_revision_exact_interval_and_quality_evidence() -> None:
    analysis = analyze_qpf_facts(
        [
            qpf_fact(forecast=3, observation=1),
            qpf_fact(artifact="two", horizon=2, forecast=0, observation=0),
        ]
    )
    samples = analysis["canonicalization"]["samples"]
    variant = stage(
        samples[0],
        QPF,
        overlay={
            "inherit_unchanged": False,
            "predictions": [
                prediction(sample, value, QPF)
                for sample, value in zip(samples, (2, 0), strict=True)
            ],
        },
    )
    result = evaluate_variants(QPF, analysis, [variant])
    metrics = comparison(result)
    assert result["shared_sample_count"] == 2
    assert metrics["control"]["mae"] == 1
    assert metrics["variant"]["mae"] == 0.5
    assert metrics["variant"]["rmse"] == pytest.approx(0.5**0.5)
    assert metrics["variant"]["observed_total_mm"] == 1
    assert metrics["variant"]["observed_zero_count"] == 1
    assert result["samples"][0]["quality_support"] == samples[0]["observation"]["quality_support"]
    variant["overlay"]["predictions"][0]["interval_start"] = "2026-09-17T16:00:00Z"
    excluded = evaluate_variants(QPF, analysis, [variant])
    assert excluded["shared_sample_count"] == 1
    assert excluded["exclusion_counts"] == {"incompatible_variant_interval": 1}


def test_existing_revision_conflict_remains_excluded_no_new_fake_sample() -> None:
    first = qpf_fact()
    revised = deepcopy(first)
    revised["artifact_id"] = "revision-two"
    revised["observation"]["semantic_revision_digest"] = "sha256:conflicting"
    control = analyze_qpf_facts([first, revised])
    reference_sample = analyze_qpf_facts([first])["canonicalization"]["samples"][0]
    result = evaluate_variants(QPF, control, [stage(reference_sample, QPF)])
    assert result["shared_sample_count"] == 0
    assert (
        result["control_canonicalization"]["ambiguous"][0]["reason"]
        == "conflicting_observation_revisions"
    )


def test_immutable_stage_and_policy_definitions_cannot_conflict() -> None:
    analysis = analyze_facts([temperature_fact()])
    variant = stage(analysis["samples"][0])
    conflicting = deepcopy(variant)
    conflicting["parent_stage_id"] = "another-parent"
    result = evaluate_variants(TEMPERATURE, analysis, [variant, conflicting])
    assert result["exclusion_counts"] == {"conflicting_immutable_stage_identity": 1}
    conflicting["variant_id"] = "separate-variant"
    conflicting["policy"]["digest"] = "other-definition-under-same-version"
    result = evaluate_variants(TEMPERATURE, analysis, [variant, conflicting])
    assert result["shared_sample_count"] == 0
    assert result["exclusion_counts"] == {"conflicting_immutable_policy_identity": 2}


def test_baseline_proof_and_exact_control_interval_are_required_for_qpf() -> None:
    analysis = analyze_qpf_facts([qpf_fact()])
    sample = analysis["canonicalization"]["samples"][0]
    sample["baseline_snapshot"] = {"baseline_snapshot_id": "baseline-a"}
    variant = stage(sample, QPF, baseline_snapshot_id="wrong-baseline")
    result = evaluate_variants(QPF, analysis, [variant])
    assert result["exclusion_counts"] == {"baseline_lineage_mismatch": 1}
    sample["observation"]["interval_start"] = "2026-09-17T15:00:00Z"
    result = evaluate_variants(QPF, analysis, [variant])
    assert result["exclusion_counts"] == {"invalid_canonical_control": 1}


def test_saved_raw_baseline_stays_distinct_from_active_corrected_issuance() -> None:
    # Operational correction changed 297 K raw to 295 K issued; one observation
    # is 294 K. Raw error must remain +3 K, not inherit issued error +1 K.
    analysis = analyze_facts([temperature_fact(forecast=295, observed=294)])
    sample = analysis["samples"][0]
    raw = stage(
        sample,
        parent_stage_id=None,
        transformation_type="active_baseline",
        lifecycle_role="active",
        policy={"id": "saved-active-field-policies", "version": "1"},
        evidence_required=False,
        evidence_status="baseline",
        evidence_cutoff=None,
        policy_created_at=None,
        policy_activated_at=None,
        overlay={"inherit_unchanged": True, "predictions": [prediction(sample, 297)]},
    )
    result = evaluate_variants(TEMPERATURE, analysis, [raw])
    assert result["control_identity"] == "canonical_issued_forecast_stage"
    assert comparison(result)["control"]["bias"] == 1
    assert comparison(result)["variant"]["bias"] == 3
    assert comparison(result)["metric_deltas_variant_minus_control"]["bias"] == 2
    raw["overlay"]["predictions"] = []
    result = evaluate_variants(TEMPERATURE, analysis, [raw])
    assert result["shared_sample_count"] == 0
    assert result["exclusion_counts"] == {"saved_baseline_prediction_unavailable": 1}


def test_fallback_status_qpf_stage_rows_stay_in_the_shared_cohort() -> None:
    """A saved blend 'fallback' amount is a value with notes, as issued QPF treats it."""
    analysis = analyze_qpf_facts([qpf_fact(forecast=3, observation=1)])
    sample = analysis["canonicalization"]["samples"][0]
    row = {
        **prediction(sample, 2, QPF),
        "status": "fallback",
        "missing_reasons": ["HRRR: hour unavailable; approved single-model fallback"],
    }
    variant = stage(sample, QPF, overlay={"inherit_unchanged": False, "predictions": [row]})
    result = evaluate_variants(QPF, analysis, [variant])
    assert result["shared_sample_count"] == 1
    assert comparison(result)["variant"]["mae"] == 1
    for status in ("unavailable", "policy_unavailable"):
        variant["overlay"]["predictions"][0]["status"] = status
        excluded = evaluate_variants(QPF, analysis, [variant])
        assert excluded["exclusion_counts"] == {"variant_prediction_unavailable": 1}
    # Rows without status keep the historical missing-reason rule.
    variant["overlay"]["predictions"][0] = {**prediction(sample, 2, QPF), "missing_reasons": ["x"]}
    assert evaluate_variants(QPF, analysis, [variant])["exclusion_counts"] == {
        "variant_prediction_unavailable": 1
    }
