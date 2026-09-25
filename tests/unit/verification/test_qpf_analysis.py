"""Behavioral canonicalization and amount metrics; fixtures are not real skill evidence."""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.verification.qpf_analysis import analyze_qpf_facts, canonicalize_qpf_facts

REFERENCE = datetime(2026, 9, 17, 18, tzinfo=UTC)


def fact(
    *,
    artifact: str = "art-a",
    version: str = "issued-a",
    horizon: int = 1,
    forecast: float = 3.0,
    observation: float = 1.0,
    stage: str = "baseline",
    mode: str | None = "primary",
) -> dict[str, Any]:
    end = REFERENCE + timedelta(hours=horizon)
    start = end - timedelta(hours=1)
    return {
        "schema_version": "issued-qpf-verification.v1",
        "field": "liquid_equivalent_precipitation_amount_1h",
        "verification_policy_id": "qpf-mrms-verification.v1",
        "artifact_id": artifact,
        "registered_at": (end + timedelta(hours=2)).isoformat(),
        "status": "verified",
        "reasons": [],
        "stage": stage,
        "issued_forecast_id": version,
        "issued_forecast_digest": f"digest-{version}",
        "issued_at": REFERENCE.isoformat(),
        "issuance_mode": mode,
        "target_reference_time": REFERENCE.isoformat(),
        "latitude": 44.98859,
        "longitude": -93.25557,
        "interval_start": start.isoformat(),
        "interval_end": end.isoformat(),
        "duration_seconds": 3600,
        "interval_closure": "left_open_right_closed",
        "lead_hours": float(horizon),
        "forecast": {
            "amount_mm": forecast,
            "native_value": forecast,
            "unit": "kg/m^2",
            "policy": "phase2-qpf-policy",
            "weights": {"HRRR": 0.7, "GFS": 0.3},
            "missing_reasons": [],
        },
        "observation": {
            "amount_mm": observation,
            "state": "positive" if observation else "zero",
            "product": "MultiSensor_QPE_01H_Pass2",
            "contract_id": "mrms.multisensor-qpe-01h-pass2.v1",
            "interval_start": start.isoformat(),
            "interval_end": end.isoformat(),
            "semantic_revision_digest": f"sha256:message-{horizon}",
            "message_sha256": f"message-{horizon}",
            "raw_sha256": "gzip-sha",
            "acquired_at": (end + timedelta(hours=1)).isoformat(),
            "artifact_reference": {"artifact_id": "extract-1", "content_digest": "gzip-1"},
            "raw_artifact_reference": {"artifact_id": "raw-1", "content_digest": "gzip-1"},
            "grid_identity": "mrms-grid",
            "cell": {"row": 1001, "column": 3674},
            "quality_support": {
                "GaugeInflIndex_01H_Pass2": {
                    "value": {"value": 0.75, "state": "positive", "units": "1"},
                    "message_sha256": f"gauge-message-{horizon}",
                    "raw_sha256": "gauge-gzip",
                    "acquired_at": (end + timedelta(hours=1)).isoformat(),
                    "artifact_reference": {"artifact_id": "gauge-original"},
                }
            },
        },
        "contributors": {
            model: {
                "amount_mm": amount,
                "native_value": amount,
                "unit": "kg/m^2",
                "compatible": True,
                "reasons": [],
                "interval_start": start.isoformat(),
                "interval_end": end.isoformat(),
                "source_cycle": REFERENCE.isoformat(),
                "source_lead_hours": horizon,
            }
            for model, amount in (("HRRR", 4.0), ("GFS", 2.0))
        },
        "qpf_error_mm": forecast - observation,
    }


def test_independent_amount_metrics_zero_counts_and_stage_isolation() -> None:
    rows = [
        fact(forecast=3, observation=1),
        fact(artifact="art-b", horizon=2, forecast=0, observation=2),
        fact(artifact="art-c", horizon=3, forecast=0, observation=0),
        fact(artifact="art-final", stage="final_issued", forecast=8, observation=1),
    ]
    result = analyze_qpf_facts(rows)
    baseline = result["stages"]["baseline"]
    assert baseline["overall"]["n"] == 3
    assert baseline["overall"]["mean_error_mm"] == baseline["overall"]["bias_mm"] == 0
    assert baseline["overall"]["mae_mm"] == 4 / 3
    assert baseline["overall"]["rmse_mm"] == math.sqrt(8 / 3)
    assert baseline["overall"]["forecast_total_mm"] == baseline["overall"]["observed_total_mm"] == 3
    assert baseline["overall"]["numeric_occurrence_counts"] == {
        "forecast_positive_observed_positive": 1,
        "forecast_zero_observed_positive": 1,
        "forecast_zero_observed_zero": 1,
    }
    assert result["stages"]["final_issued"]["overall"]["bias_mm"] == 7
    assert baseline["by_lead_bucket"]["19-36"]["mae_mm"] is None
    assert result["canonicalization"]["canonical_samples"] == 4
    assert "overall" not in result  # Stages must never silently pool.


def test_duplicate_fact_references_and_reacquired_identical_messages_are_one_sample() -> None:
    first = fact()
    reacquired = copy.deepcopy(first)
    reacquired.update(artifact_id="art-rereceipt", registered_at="2026-09-18T12:00:00Z")
    obs = reacquired["observation"]
    obs.update(raw_sha256="new-gzip-wrapper", acquired_at="2026-09-18T11:00:00Z")
    obs["artifact_reference"] = {"artifact_id": "new-extract", "content_digest": "new-gzip"}
    obs["raw_artifact_reference"] = {"artifact_id": "new-raw", "content_digest": "new-gzip"}
    quality = obs["quality_support"]["GaugeInflIndex_01H_Pass2"]
    quality.update(
        raw_sha256="new-gauge-gzip",
        acquired_at="2026-09-18T11:00:00Z",
        artifact_reference={"artifact_id": "new-gauge"},
    )
    result = canonicalize_qpf_facts([first, first, reacquired])
    assert result["input_fact_references"] == 3
    assert result["stored_facts"] == 2
    assert result["duplicate_fact_references"] == 1
    assert result["opportunities"] == result["canonical_samples"] == 1
    assert result["samples"][0]["provenance"]["versions"][0]["fact_ids"] == [
        "art-a",
        "art-rereceipt",
    ]


@pytest.mark.parametrize("revision", ["positive", "missing", "no_coverage", "quality"])
def test_conflicting_actual_revisions_are_excluded_even_if_new_revision_is_unavailable(
    revision: str,
) -> None:
    first = fact()
    revised = copy.deepcopy(first)
    revised["artifact_id"] = "art-revised"
    if revision == "quality":
        revised["observation"]["quality_support"]["GaugeInflIndex_01H_Pass2"]["message_sha256"] = (
            "changed-support"
        )
    else:
        revised["observation"]["message_sha256"] = "changed-qpe"
        revised["observation"]["semantic_revision_digest"] = "sha256:changed-qpe"
    if revision in ("missing", "no_coverage"):
        revised.update(status="excluded", reasons=["observation_" + revision], qpf_error_mm=None)
        revised["observation"].update(state=revision, amount_mm=None)
    result = canonicalize_qpf_facts([first, revised])
    assert result["opportunities"] == 1
    assert result["canonical_samples"] == 0
    assert result["ambiguous"][0]["reason"] == "conflicting_observation_revisions"


def test_no_evidence_missing_opportunity_does_not_block_later_matched_fact() -> None:
    first = fact(artifact="art-missing")
    first.update(
        status="excluded", reasons=["observation_missing"], observation=None, qpf_error_mm=None
    )
    result = canonicalize_qpf_facts([first, fact(artifact="art-match")])
    assert result["canonical_samples"] == 1
    assert result["samples"][0]["canonical_fact_id"] == "art-match"
    assert result["excluded_by_reason"] == {"observation_missing": 1}


def test_primary_identical_reissue_collapse_and_different_explicit_reissue_is_alternate() -> None:
    primary = fact()
    identical = fact(artifact="art-identical", version="issued-identical", mode="explicit_reissue")
    alternate = fact(
        artifact="art-alternate", version="issued-alternate", mode="explicit_reissue", forecast=7
    )
    result = canonicalize_qpf_facts([alternate, identical, primary])
    assert result["opportunities"] == 3
    assert result["canonical_samples"] == 1
    assert result["samples"][0]["forecast"]["amount_mm"] == 3
    assert len(result["samples"][0]["provenance"]["versions"]) == 2
    assert result["alternate_reissues"][0]["issued_forecast_id"] == "issued-alternate"
    assert result["alternate_reissues"][0]["primary_issued_forecast_id"] == "issued-a"
    primary["issuance_mode"] = identical["issuance_mode"] = alternate["issuance_mode"] = None
    assert canonicalize_qpf_facts([primary, alternate])["canonical_samples"] == 0
    assert canonicalize_qpf_facts([primary, identical])["canonical_samples"] == 1


def test_reissue_cannot_become_primary_or_resolve_conflicting_observation() -> None:
    alternate = fact(mode="explicit_reissue")
    result = canonicalize_qpf_facts([alternate])
    assert result["canonical_samples"] == 0
    assert result["ambiguous"][0]["reason"] == "primary_forecast_not_in_selection"
    primary = fact(artifact="art-primary", version="issued-primary")
    alternate["observation"]["semantic_revision_digest"] = "different-revision"
    result = canonicalize_qpf_facts([primary, alternate])
    assert result["canonical_samples"] == 0
    assert result["ambiguous"][0]["reason"] == "conflicting_observation_revisions"


def test_contributor_comparisons_use_explicit_identical_hourly_cohorts() -> None:
    first, second = fact(), fact(artifact="art-b", horizon=2)
    for row in (first, second):
        row["contributors"]["IFS"] = copy.deepcopy(row["contributors"]["GFS"])
    first["contributors"]["IFS"]["interval_start"] = (REFERENCE - timedelta(hours=5)).isoformat()
    result = analyze_qpf_facts([first, second], contributors=("HRRR", "GFS", "IFS"))
    comparisons = result["stages"]["baseline"]["contributor_comparison"]
    assert comparisons["pairwise"]["HRRR"]["mesoforge"]["n"] == 2
    assert comparisons["pairwise"]["IFS"]["mesoforge"]["n"] == 1
    assert comparisons["pairwise"]["IFS"]["contributor"]["n"] == 1
    assert comparisons["pairwise"]["IFS"]["excluded_incompatible_or_missing"] == 1
    assert comparisons["joint"]["mesoforge"]["n"] == 1
    assert all(value["n"] == 1 for value in comparisons["joint"]["models"].values())
    assert comparisons["joint"]["sample_ids"] == comparisons["pairwise"]["IFS"]["sample_ids"]


@pytest.mark.parametrize("defect", ["interval", "error", "nonfinite", "trace"])
def test_claimed_verified_facts_are_rechecked_before_metrics(defect: str) -> None:
    row = fact()
    if defect == "interval":
        row["observation"]["interval_start"] = (REFERENCE - timedelta(hours=5)).isoformat()
    elif defect == "error":
        row["qpf_error_mm"] = 99
    elif defect == "trace":
        row["observation"]["state"] = "trace"
    else:
        row["forecast"]["amount_mm"] = float("nan")
    result = analyze_qpf_facts([row])
    assert result["canonicalization"]["canonical_samples"] == 0
    assert result["stages"]["baseline"]["overall"]["n"] == 0


def test_concentration_reports_dates_locations_shared_events_and_raw_quality_without_gates() -> (
    None
):
    first = fact()
    second = fact(artifact="art-b", horizon=7)
    third = fact(artifact="art-c", version="issued-c", horizon=7)
    third["latitude"] = 37.6872
    result = analyze_qpf_facts([third, first, second])
    stage = result["stages"]["baseline"]
    assert stage["concentration"]["decision_date_utc"]["distinct"] == 1
    assert stage["concentration"]["decision_date_utc"]["largest_share"] == 1
    assert stage["concentration"]["location"]["distinct"] == 2
    assert stage["concentration"]["native_analysis_event"]["distinct"] == 2
    assert stage["by_lead_bucket"]["1-6"]["n"] == 1
    assert stage["by_lead_bucket"]["7-18"]["n"] == 2
    assert stage["quality_support"]["GaugeInflIndex_01H_Pass2"]["minimum"] == 0.75
    assert analyze_qpf_facts([first, second, third]) == result


def test_conflicting_payloads_for_one_artifact_id_cannot_create_two_samples() -> None:
    first, conflicting = fact(), fact(horizon=2)
    result = canonicalize_qpf_facts([first, conflicting])
    assert result["canonical_samples"] == 0
    assert result["ambiguous"]


def test_canonical_json_integer_float_equivalence_preserves_analysis_groups() -> None:
    row = fact(forecast=3.0, observation=1.0)
    row.update(latitude=45.0, longitude=-93.0, lead_hours=1.0)
    decoded = json.loads(canonical_json_bytes(row))
    assert type(row["lead_hours"]) is float and type(decoded["lead_hours"]) is int
    before = analyze_qpf_facts([row])
    assert before == analyze_qpf_facts([decoded])
    assert before["stages"]["baseline"]["concentration"]["lead_hours"]["counts"] == {"1": 1}
    assert list(before["stages"]["baseline"]["by_location"]) == ["45,-93"]


def test_historical_missing_qpf_preserves_reason_and_known_hour_without_fabricated_interval() -> (
    None
):
    row = fact()
    row.update(
        valid_time=row["interval_end"],
        interval_start=None,
        interval_end=None,
        duration_seconds=None,
        status="excluded",
        observation=None,
        qpf_error_mm=None,
        reasons=["forecast_qpf_unavailable_or_invalid", "incompatible_forecast_interval"],
    )
    row["forecast"].update(amount_mm=None, policy=None)
    result = canonicalize_qpf_facts([row])
    assert result["known_issued_hour_opportunities"] == 1
    assert result["opportunities"] == result["canonical_samples"] == 0
    assert result["excluded_by_reason"] == {"forecast_qpf_unavailable_or_invalid": 1}
    assert result["excluded_facts"][0]["fact_reasons"] == row["reasons"]
    assert result["excluded_facts"][0]["analysis_exclusion"] == "incomplete_fact_identity"
    assert row["interval_start"] is row["interval_end"] is None


def test_explicit_reissue_does_not_become_primary_before_identical_legacy_version() -> None:
    reissue = fact(artifact="art-reissue", version="issued-reissue", mode="explicit_reissue")
    legacy = fact(artifact="art-legacy", version="issued-legacy", mode=None)
    legacy["issued_at"] = (REFERENCE + timedelta(minutes=1)).isoformat()
    result = canonicalize_qpf_facts([reissue, legacy])
    assert result["canonical_samples"] == 1
    assert result["samples"][0]["canonical_issued_forecast_id"] == "issued-legacy"
