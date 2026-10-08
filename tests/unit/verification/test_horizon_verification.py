"""Long-range evidence is measurable without expanding correction-policy eligibility."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.verification.analytical_attributes import build_analytical_attributes
from mesoforge.verification.model_comparison import LEAD_BUCKETS, lead_bucket
from mesoforge.verification.qpf_analysis import analyze_qpf_facts
from mesoforge.verification.site_analysis import EVIDENCE_POLICY, analyze_facts
from mesoforge.verification.variant_evaluation import QPF, TEMPERATURE, evaluate_variants
from tests.unit.verification.test_analytical_attributes import rich_payload
from tests.unit.verification.test_issued_qpf import FIELD, VALID, evaluate, saved_forecast
from tests.unit.verification.test_qpf_analysis import fact as qpf_fact
from tests.unit.verification.test_site_analysis import fact as temperature_fact
from tests.unit.verification.test_variant_evaluation import comparison, stage


def extended_saved_forecast():
    """Synthetic complete window ending at the existing fixed MRMS fixture event."""
    saved = saved_forecast()
    forecast = saved["forecast"]
    target = VALID - timedelta(hours=120)
    cycle = target - timedelta(hours=5)
    saved["target_reference_time"] = forecast["target_reference_time"] = target.isoformat()
    saved["issued_at"] = (target - timedelta(minutes=30)).isoformat()
    forecast["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    template = forecast["hours"][0]
    forecast["hours"] = []
    for lead in FIVE_DAY_HORIZON.leads:
        hour = deepcopy(template)
        end = target + timedelta(hours=lead)
        interval = {
            "interval_start": (end - timedelta(hours=1)).isoformat(),
            "interval_end": end.isoformat(),
        }
        hour.update(horizon_hours=lead, valid_time=end.isoformat())
        hour["surface"]["fields"][FIELD].update(interval)
        for contributor in hour["surface"]["contributors"].values():
            contributor.update(cycle=cycle.isoformat(), source_lead_hours=lead + 5)
            contributor["fields"][FIELD].update(
                interval, source_cycle=cycle.isoformat(), source_lead_hours=lead + 5
            )
        forecast["hours"].append(hour)
    return saved


def test_hour_120_qpf_fact_preserves_exact_event_and_requires_declared_horizon():
    saved = extended_saved_forecast()
    original = deepcopy(saved)
    result = evaluate(saved)
    assert result["status"] == "verified"
    assert result["lead_hours"] == 120
    assert result["forecast_horizon"] == FIVE_DAY_HORIZON.payload()
    assert result["interval_start"] == "2026-09-24T11:00:00Z"
    assert result["interval_end"] == "2026-09-24T12:00:00Z"
    assert result["qpf_error_mm"] == 1.0
    assert saved == original
    del saved["forecast"]["forecast_horizon"]
    assert "forecast_reference_lead_inconsistent" in evaluate(saved)["reasons"]
    saved["forecast"]["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    saved["forecast"]["hours"][-1]["surface"]["fields"][FIELD]["interval_start"] = (
        VALID - timedelta(hours=3)
    ).isoformat()
    assert "incompatible_forecast_interval" in evaluate(saved)["reasons"]


@pytest.mark.parametrize("field", [TEMPERATURE, QPF])
def test_long_lead_canonical_evaluation_preserves_event_and_observation_identity(field):
    row = temperature_fact(horizon=120) if field == TEMPERATURE else qpf_fact(horizon=120)
    analyze = analyze_facts if field == TEMPERATURE else analyze_qpf_facts
    legacy = analyze([row])
    assert not (
        legacy["samples"] if field == TEMPERATURE else legacy["canonicalization"]["samples"]
    )
    row["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    analysis = analyze([row, deepcopy(row)])
    samples = (
        analysis["samples"] if field == TEMPERATURE else analysis["canonicalization"]["samples"]
    )
    assert len(samples) == 1
    sample = samples[0]
    assert sample["lead_bucket"] == "73-120"
    assert sample["horizon_hours" if field == TEMPERATURE else "lead_hours"] == 120
    variant = stage(
        sample, field, evidence_cutoff=None, evidence_required=False, evidence_status="no_policy"
    )
    result = evaluate_variants(field, analysis, [variant, deepcopy(variant)])
    assert result["shared_sample_count"] == 1
    metrics = comparison(result)
    assert metrics["control"] == metrics["variant"]
    assert metrics["by_lead_bucket"]["73-120"]["control"] == metrics["control"]
    assert metrics["by_exact_lead_hours"]["120"]["control"] == metrics["control"]


def test_long_lead_evidence_never_enters_approved_temperature_correction_buckets():
    rows = [
        temperature_fact(
            horizon=lead,
            artifact=f"art-{lead}",
            forecast_horizon=FIVE_DAY_HORIZON.payload(),
        )
        for lead in (37, 72, 73, 120)
    ]
    result = analyze_facts(rows)
    assert result["overall"]["n"] == 4
    assert result["lead_buckets"]["37-72"]["n"] == 2
    assert result["lead_buckets"]["73-120"]["n"] == 2
    assert set(result["exact_lead_hours"]) == {"37", "72", "73", "120"}
    readiness = result["correction_readiness"]
    assert readiness["correction_ready_lead_buckets"] == []
    assert readiness["observed_evidence"]["canonical_samples"] == 0
    assert tuple(readiness["lead_buckets"]) == LEAD_BUCKETS
    assert EVIDENCE_POLICY["lead_buckets"] == ["1-6", "7-18", "19-36"]
    assert result["long_range_correction_status"] == "no_policy_beyond_36_hours"
    with pytest.raises(ValueError):
        lead_bucket(37)


def test_temperature_compact_projection_retains_horizon_declaration():
    payload = rich_payload()
    context = payload["match"]["forecast_context"]
    forecast = payload["match"]["forecast"]
    context["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    target = datetime.fromisoformat(context["target_reference_time"])
    forecast.update(horizon_hours=120, valid_time=(target + timedelta(hours=120)).isoformat())
    attributes = build_analytical_attributes(payload)
    assert attributes["forecast_horizon"] == FIVE_DAY_HORIZON.payload()
    assert attributes["horizon_hours"] == 120
    assert attributes["valid_time"] == forecast["valid_time"]
