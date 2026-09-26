"""Exact-event QPF facts reuse the issued surface shape and real GRIB normalization."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.common.identifiers import Digest
from mesoforge.contracts.forecast_variants import seal_variant
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.observations.mrms import PRODUCT_CONTRACTS, extract_mrms
from mesoforge.verification.issued_qpf import FIELD, evaluate_qpf_verification
from tests.unit.contracts.test_forecast_variants import body
from tests.unit.observations.test_mrms import make_mrms_message

VALID = datetime(2026, 9, 24, 12, tzinfo=UTC)
CUTOFF = VALID + timedelta(hours=3)
LAT, LON = 45.005, -93.265


def saved_forecast(amount: float | None = 2.5) -> dict[str, Any]:
    """Current issuance envelope and exact precipitation_forecast/field_blend schema."""
    field = {
        "value": amount,
        "unit": "kg/m^2",
        "temporal_semantics": "accumulation",
        "interval_start": "2026-09-24T11:00:00Z",
        "interval_end": "2026-09-24T12:00:00Z",
        "interval_closure": "left_open_right_closed",
        "status": "available",
        "policy": "phase2-qpf",
        "weights": {"HRRR": 0.7, "GFS": 0.3},
        "missing_reasons": [],
        "row_id": "existing-row",
        "row_sha256": "f" * 64,
    }
    contributors = {}
    for model, value in (("HRRR", 3.0), ("GFS", 2.0), ("RAP", None), ("IFS", None)):
        contributors[model] = {
            "model": model,
            "cycle": "2026-09-24T06:00:00Z",
            "source_lead_hours": 6,
            "role": "active" if model in ("HRRR", "GFS") else "shadow",
            "fields": {
                FIELD: {
                    **deepcopy(field),
                    "value": value,
                    "source_cycle": "2026-09-24T06:00:00Z",
                    "source_lead_hours": 6,
                    "provenance": {"source_inputs": [{"messages": ["retained"]}]},
                }
            },
        }
    hours = []
    for horizon in range(1, 37):
        end = VALID + timedelta(hours=horizon - 1)
        start = end - timedelta(hours=1)
        fields = deepcopy(field)
        fields.update(interval_start=start.isoformat(), interval_end=end.isoformat())
        native = deepcopy(contributors)
        for contributor in native.values():
            contributor["source_lead_hours"] = horizon + 5
            contributor["fields"][FIELD].update(
                interval_start=start.isoformat(),
                interval_end=end.isoformat(),
                source_lead_hours=horizon + 5,
            )
        hours.append(
            {
                "horizon_hours": horizon,
                "valid_time": end.isoformat(),
                "surface": {"fields": {FIELD: fields}, "contributors": native},
            }
        )
    return {
        "schema_version": "issued-forecast.v1",
        "issued_forecast_id": "00000000-0000-0000-0000-000000000001",
        "issued_at": "2026-09-24T10:30:00Z",
        "target_reference_time": "2026-09-24T11:00:00Z",
        "latitude": LAT,
        "longitude": LON,
        "forecast": {
            "latitude": LAT,
            "longitude": LON,
            "target_reference_time": "2026-09-24T11:00:00Z",
            "baseline_snapshot": {
                "baseline_snapshot_id": "retained-baseline",
                "issuance_mode": "primary",
            },
            "hours": hours,
        },
    }


def observation(amount: float = 1.5) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed, refs, sources = {}, {}, {}
    for index, product in enumerate(PRODUCT_CONTRACTS):
        raw = make_mrms_message(product, (amount if index == 0 else 0.5,) * 4)
        parsed[product] = extract_mrms(raw, product=product, latitude=LAT, longitude=LON)
        refs[product] = {
            "artifact_id": f"art_00000000-0000-0000-0000-{index + 10:012d}",
            "content_digest": str(Digest.of_bytes(raw)),
        }
        sources[product] = {
            "content_digest": refs[product]["content_digest"],
            "url": f"https://example.test/{product}",
            "acquired_at": "2026-09-24T14:00:00Z",
        }
    result = {
        "schema_version": "mesoforge.mrms-coordinate-extraction.v1",
        "qpe": parsed.pop("MultiSensor_QPE_01H_Pass2"),
        "quality_support": parsed,
        "raw_sources": refs,
        "source_bundle": {"sources": sources},
        "quality_threshold_policy": "none_approved_raw_evidence_only",
    }
    reference = {
        "artifact_id": "art_00000000-0000-0000-0000-000000000099",
        "content_digest": str(Digest.of_bytes(canonical_json_bytes(result))),
        "available_at": "2026-09-24T14:01:00Z",
    }
    return result, reference


def evaluate(saved: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    saved = saved or saved_forecast()
    extraction, reference = observation()
    parameters = {
        "stage": "final_issued",
        "verification_cutoff": CUTOFF,
        "issued_forecast_digest": Digest.of_bytes(canonical_json_bytes(saved)),
        "extraction": extraction,
        "extraction_reference": reference,
    }
    return evaluate_qpf_verification(saved, VALID, **(parameters | kwargs))


def test_exact_hour_fact_is_compact_reproducible_and_never_changes_issuance() -> None:
    saved = saved_forecast()
    original = deepcopy(saved)
    fact = evaluate(saved)
    assert fact == evaluate(saved)
    assert saved == original
    assert fact["status"] == "verified"
    assert fact["qpf_error_mm"] == 1.0
    assert fact["forecast"]["amount_mm"] == 2.5
    assert fact["observation"]["amount_mm"] == 1.5
    assert fact["observation"]["semantic_revision_digest"].startswith("sha256:")
    assert fact["interval_start"] == "2026-09-24T11:00:00Z"
    assert fact["interval_end"] == "2026-09-24T12:00:00Z"
    assert fact["interval_closure"] == "left_open_right_closed"
    assert fact["duration_seconds"] == 3600
    assert fact["lead_hours"] == 1
    assert fact["baseline_snapshot"]["baseline_snapshot_id"] == "retained-baseline"
    assert fact["issuance_mode"] == "primary"
    assert fact["contributors"]["HRRR"]["amount_mm"] == 3.0
    assert fact["contributors"]["HRRR"]["compatible"] is True
    assert fact["contributors"]["RAP"]["compatible"] is False
    assert len(canonical_json_bytes(fact)) < 12_000
    assert "source_bundle" not in fact["observation"]
    assert "source_inputs" not in fact["contributors"]["HRRR"]


def test_fallback_diagnostics_do_not_turn_valid_qpf_into_missing() -> None:
    saved = saved_forecast()
    field = saved["forecast"]["hours"][0]["surface"]["fields"][FIELD]
    field.update(status="fallback", missing_reasons=["GFS unavailable"], weights={"HRRR": 1.0})
    fact = evaluate(saved)
    assert fact["status"] == "verified"
    assert fact["forecast"]["missing_reasons"] == ["GFS unavailable"]


@pytest.mark.parametrize("change", ["six_hour", "shifted_hour", "closure", "instantaneous"])
def test_forecast_interval_must_match_exact_hour(change: str) -> None:
    saved = saved_forecast()
    field = saved["forecast"]["hours"][0]["surface"]["fields"][FIELD]
    if change == "six_hour":
        field["interval_start"] = "2026-09-24T06:00:00Z"
    elif change == "shifted_hour":
        field.update(interval_start="2026-09-24T11:05:00Z", interval_end="2026-09-24T12:05:00Z")
    elif change == "closure":
        field["interval_closure"] = "closed"
    else:
        field["temporal_semantics"] = "instantaneous"
    fact = evaluate(saved)
    assert fact["status"] == "excluded"
    assert "incompatible_forecast_interval" in fact["reasons"]
    assert fact["qpf_error_mm"] is None


def test_observation_interval_and_coordinate_must_match_not_temperature_tolerances() -> None:
    obs, ref = observation()
    obs["qpe"]["temporal"].update(
        interval_start="2026-09-24T10:55:00Z", interval_end="2026-09-24T11:55:00Z"
    )
    assert "incompatible_interval" in evaluate(extraction=obs, extraction_reference=ref)["reasons"]
    obs, ref = observation()
    obs["qpe"]["extraction"]["forecast_coordinate"]["latitude"] += 0.001
    fact = evaluate(extraction=obs, extraction_reference=ref)
    assert "unsuitable_spatial_support" in fact["reasons"]
    assert fact["status"] == "excluded"


@pytest.mark.parametrize(
    ("amount", "state"), [(0.0, "zero"), (-1.0, "missing"), (-3.0, "no_coverage")]
)
def test_zero_missing_and_no_coverage_are_distinct(amount: float, state: str) -> None:
    obs, ref = observation(amount)
    fact = evaluate(saved_forecast(0), extraction=obs, extraction_reference=ref)
    assert fact["observation"]["state"] == state
    assert fact["status"] == ("verified" if state == "zero" else "excluded")
    assert fact["qpf_error_mm"] == (0 if state == "zero" else None)
    if state == "zero":
        assert fact["occurrence"]["observed_positive"] is False
    else:
        assert fact["observation"]["amount_mm"] is None


def test_absent_and_malformed_observation_have_no_error() -> None:
    assert evaluate(extraction=None)["reasons"] == ["observation_missing"]
    assert evaluate(extraction="broken")["reasons"] == ["malformed_observation"]
    obs, ref = observation()
    obs["qpe"]["value"]["state"] = "trace"
    assert (
        "malformed_observation_amount"
        in evaluate(extraction=obs, extraction_reference=ref)["reasons"]
    )


def test_issuance_and_observation_availability_cutoffs_are_explicit() -> None:
    saved = saved_forecast()
    saved["issued_at"] = "2026-09-24T11:00:01Z"
    assert "forecast_not_issued_before_interval_start" in evaluate(saved)["reasons"]
    saved["issued_at"] = "2026-09-24T11:00:00Z"
    assert evaluate(saved)["status"] == "verified"
    assert (
        "observation_not_yet_available"
        in evaluate(verification_cutoff=VALID + timedelta(minutes=30))["reasons"]
    )
    fact = evaluate(verification_cutoff=VALID - timedelta(minutes=1))
    assert "forecast_interval_not_yet_complete" in fact["reasons"]


def test_baseline_final_and_multiple_issued_versions_have_separate_identities() -> None:
    final = evaluate()
    baseline = evaluate(stage="baseline")
    assert final["opportunity_id"] != baseline["opportunity_id"]
    assert final["qpf_error_mm"] == baseline["qpf_error_mm"]
    saved = saved_forecast()
    saved["issued_forecast_id"] = "00000000-0000-0000-0000-000000000002"
    assert evaluate(saved)["opportunity_id"] != final["opportunity_id"]
    assert evaluate(stage="ai_adjusted")["reasons"] == ["unsupported_forecast_stage"]


def test_historical_without_baseline_is_not_rewritten_or_falsely_identified() -> None:
    saved = saved_forecast()
    del saved["forecast"]["baseline_snapshot"]
    assert evaluate(saved)["status"] == "verified"
    assert evaluate(saved)["baseline_snapshot"]["baseline_snapshot_id"] is None
    assert evaluate(saved, stage="baseline")["reasons"] == ["baseline_stage_unavailable"]
    saved["forecast"]["local_grid"] = {"version": "mesoforge.local-surface-baseline.v1"}
    assert evaluate(saved, stage="baseline")["status"] == "verified"
    del saved["forecast"]["hours"][0]["surface"]
    assert "forecast_qpf_unavailable_or_invalid" in evaluate(saved)["reasons"]


def test_contributor_intervals_are_independent_and_cannot_be_resampled() -> None:
    saved = saved_forecast()
    field = saved["forecast"]["hours"][0]["surface"]["contributors"]["IFS"]["fields"][FIELD]
    field.update(value=3.0, interval_start="2026-09-24T09:00:00Z")
    fact = evaluate(saved)
    assert fact["status"] == "verified"
    assert fact["contributors"]["IFS"]["amount_mm"] == 3.0
    assert fact["contributors"]["IFS"]["compatible"] is False


def test_quality_values_remain_evidence_and_changed_revision_is_distinct() -> None:
    obs, ref = observation()
    original = evaluate(extraction=obs, extraction_reference=ref)
    support = next(iter(obs["quality_support"].values()))
    support["value"].update(value=None, raw_value=-1, state="missing")
    fact = evaluate(extraction=obs, extraction_reference=ref)
    assert fact["status"] == "verified"  # No unapproved quality threshold/exclusion.
    assert (
        next(iter(fact["observation"]["quality_support"].values()))["value"]["state"] == "missing"
    )
    revised, reference = observation(2.0)
    changed = evaluate(extraction=revised, extraction_reference=reference)
    assert (
        changed["observation"]["semantic_revision_digest"]
        != original["observation"]["semantic_revision_digest"]
    )
    assert changed["opportunity_id"] == original["opportunity_id"]
    assert changed["qpf_error_mm"] == 0.5


def test_units_and_raw_source_revision_mismatch_fail_explicitly() -> None:
    saved = saved_forecast()
    saved["forecast"]["hours"][0]["surface"]["fields"][FIELD]["unit"] = "inch"
    assert "forecast_qpf_unavailable_or_invalid" in evaluate(saved)["reasons"]
    obs, ref = observation()
    obs["qpe"]["raw_sha256"] = "a" * 64
    assert (
        "observation_revision_identity_unavailable_or_inconsistent"
        in evaluate(extraction=obs, extraction_reference=ref)["reasons"]
    )


def test_known_active_source_cycle_cannot_follow_issuance_but_shadow_does_not_gate() -> None:
    saved = saved_forecast()
    native = saved["forecast"]["hours"][0]["surface"]["contributors"]
    native["RAP"]["fields"][FIELD]["source_cycle"] = "2026-09-24T13:00:00Z"
    assert evaluate(saved)["status"] == "verified"
    native["HRRR"]["fields"][FIELD].update(source_cycle="2026-09-24T11:00:00Z", source_lead_hours=1)
    result = evaluate(saved)
    assert "forecast_source_cycle_after_issuance" in result["reasons"]
    assert result["status"] == "excluded"
    assert result["qpf_error_mm"] is None


def test_ai_qpf_final_and_retained_raw_baseline_are_not_aliased() -> None:
    saved = saved_forecast(4.0)
    forecast = saved["forecast"]
    prediction = {
        "field": FIELD,
        "valid_time": VALID.isoformat(),
        "value": 2.5,
        "unit": "mm",
        "interval_start": "2026-09-24T11:00:00Z",
        "interval_end": VALID.isoformat(),
        "policy": "phase2-qpf",
        "status": "available",
        "missing_reasons": [],
    }
    forecast["baseline_stage"] = seal_variant(
        body(
            parent_stage_id=None,
            transformation_type="active_baseline",
            lifecycle_role="active",
            baseline_snapshot_id=forecast["baseline_snapshot"]["baseline_snapshot_id"],
            location={"latitude": LAT, "longitude": LON},
            reference_time=saved["target_reference_time"],
            evidence_required=False,
            evidence_status="baseline",
            evidence_cutoff=None,
            policy_created_at=None,
            policy_activated_at=None,
            overlay={"inherit_unchanged": False, "predictions": [prediction]},
        )
    )
    forecast["learning_stage"] = {"transformation_type": "ai_adjusted"}
    raw, final = evaluate(saved, stage="baseline"), evaluate(saved)
    assert raw["status"] == final["status"] == "verified"
    assert raw["forecast"]["amount_mm"] == 2.5
    assert final["forecast"]["amount_mm"] == 4.0
    assert raw["stage_evidence"] == "saved_raw_baseline_stage"
    assert final["stage_evidence"] == "saved_ai_adjusted_final_stage"
    forecast.pop("baseline_stage")
    assert "baseline_stage_unavailable" in evaluate(saved, stage="baseline")["reasons"]
