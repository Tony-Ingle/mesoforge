"""Canonical verification samples and descriptive statistics, as pure computation."""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mesoforge.verification import site_analysis
from mesoforge.verification.site_analysis import analyze_facts, canonicalize, describe

TARGET = datetime(2026, 9, 16, 22, tzinfo=UTC)
VERSION_A = "00000000-0000-0000-0000-00000000000a"
VERSION_B = "00000000-0000-0000-0000-00000000000b"
VERSION_C = "00000000-0000-0000-0000-00000000000c"


def fact(
    *,
    version: str = VERSION_A,
    target: datetime = TARGET,
    horizon: int = 1,
    forecast: float = 296.0,
    observed: float = 295.0,
    station: str = "KMIC",
    revision: str = "rev-1",
    logical: str | None = None,
    artifact: str = "art_1",
    registered_minutes: int = 0,
    issued_minutes: int = 40,
    zone: str | None = "America/Chicago",
    **overrides: Any,
) -> dict[str, Any]:
    valid = target + timedelta(hours=horizon)
    row: dict[str, Any] = {
        "artifact_id": artifact,
        "registered_at": valid + timedelta(minutes=30 + registered_minutes),
        "indexed": True,
        "quality_state": "valid",
        "schema_version": "issued-temperature-verification.v1",
        "status": "verified",
        "verification_policy_id": "issued-temperature-verification.v1",
        "matching_policy_digest": "sha256:policy",
        "issued_forecast_id": version,
        "issued_at": (target + timedelta(minutes=issued_minutes)).isoformat(),
        "issued_forecast_digest": f"sha256:{version}",
        "target_reference_time": target.isoformat(),
        "valid_time": valid.isoformat().replace("+00:00", "Z"),
        "horizon_hours": horizon,
        "latitude": 44.98859,
        "longitude": -93.25557,
        "forecast_temperature_k": forecast,
        "temperature_error_k": forecast - observed,
        "observation": {
            "station_id": station,
            "network": "METAR",
            "provider": "aviationweather.gov",
            "distance_km": 11.1 if station == "KMIC" else 14.9,
            "elevation_m": 263 if station == "KMIC" else 256,
            "observation_time": (valid - timedelta(minutes=7)).isoformat(),
            "time_difference_seconds": -420,
            "temperature_k": observed,
            "revision_digest": revision,
            "logical_observation_digest": logical or f"logical-{station}-{valid.isoformat()}",
            "raw_artifact_id": f"raw-for-{artifact}",
        },
        "display_timezone": zone,
        "context": {
            "fields": {"wind_speed_10m": 3.4, "cloud_area_fraction": None},
            "model_temperatures_k": {"HRRR": 296.3, "GFS": 295.7, "RAP": None},
        },
    }
    row.update(overrides)
    return row


def test_single_fact_is_one_opportunity_and_one_sample_with_full_identity():
    result = analyze_facts([fact()])
    canonical = result["canonicalization"]
    assert canonical["stored_facts"] == canonical["usable_facts"] == 1
    assert canonical["verified_opportunities"] == canonical["analytical_samples"] == 1
    assert canonical["fact_classification"] == {"single_fact": 1}
    assert canonical["sample_classification"] == {"single_version": 1}
    sample = result["samples"][0]
    assert sample["target_reference_time"] == "2026-09-16T22:00:00Z"
    assert sample["valid_time"] == "2026-09-16T23:00:00Z"
    assert sample["horizon_hours"] == 1 and sample["lead_bucket"] == "1-6"
    assert sample["forecast_temperature_k"] == 296.0
    assert sample["temperature_error_k"] == 1.0
    assert sample["observation"]["station_id"] == "KMIC"
    assert sample["observation"]["revision_digest"] == "rev-1"
    assert sample["canonical_issued_forecast_id"] == VERSION_A
    assert sample["canonical_fact_id"] == "art_1"
    assert sample["provenance"]["stored_fact_count"] == 1


def test_reacquired_identical_evidence_is_one_sample_not_two():
    first = fact(artifact="art_early", registered_minutes=0)
    later = fact(artifact="art_late", registered_minutes=46)
    # Only acquisition provenance differs, exactly as a wider later acquisition produces.
    later["observation"]["raw_artifact_id"] = "another-acquisition"
    later["verification_cutoff"] = "2026-09-17T03:20:28Z"
    result = analyze_facts([later, first])
    canonical = result["canonicalization"]
    assert canonical["stored_facts"] == 2
    assert canonical["verified_opportunities"] == 1
    assert canonical["opportunities_with_multiple_facts"] == 1
    assert canonical["fact_classification"] == {"identical_evidence_reacquired": 1}
    assert canonical["ambiguous"] == []
    assert result["overall"]["n"] == 1
    sample = result["samples"][0]
    assert sample["canonical_fact_id"] == "art_early"
    assert sample["provenance"]["versions"][0]["fact_ids"] == ["art_early", "art_late"]
    assert sample["provenance"]["stored_fact_count"] == 2


def test_revised_observation_is_ambiguous_and_never_chosen_by_rule():
    original = fact(artifact="art_1", observed=295.0, revision="rev-1", logical="logical-x")
    revised = fact(
        artifact="art_2",
        observed=294.4,
        revision="rev-2",
        logical="logical-x",
        registered_minutes=60,
    )
    untouched = fact(artifact="art_3", horizon=2, forecast=294.5, observed=294.85)
    result = analyze_facts([original, revised, untouched])
    canonical = result["canonicalization"]
    assert canonical["verified_opportunities"] == 2
    assert canonical["analytical_samples"] == 1
    assert canonical["fact_classification"] == {"ambiguous": 1, "single_fact": 1}
    [row] = canonical["ambiguous"]
    assert row["level"] == "opportunity"
    assert row["reason"] == "conflicting_observation_revisions"
    assert row["fact_ids"] == ["art_1", "art_2"]
    assert row["distinct_observation_revisions"] == ["rev-1", "rev-2"]
    assert len(row["distinct_errors_k"]) == 2
    # Neither revision's error reaches the statistics.
    assert result["overall"]["n"] == 1
    assert result["overall"]["bias_k"] == pytest.approx(294.5 - 294.85)


def test_other_conflicts_name_their_reason():
    different_station = canonicalize(
        [fact(artifact="art_1"), fact(artifact="art_2", station="KMSP", observed=295.4)]
    )
    assert different_station["ambiguous"][0]["reason"] == "different_selected_observation"
    different_policy = canonicalize(
        [fact(artifact="art_1"), fact(artifact="art_2", matching_policy_digest="sha256:other")]
    )
    assert different_policy["ambiguous"][0]["reason"] == "different_matching_policy"
    different_forecast = canonicalize(
        [fact(artifact="art_1"), fact(artifact="art_2", forecast=296.5)]
    )
    assert different_forecast["ambiguous"][0]["reason"] == "different_forecast_evidence"
    assert different_forecast["analytical_samples"] == 0


def test_identical_reissued_versions_collapse_but_differing_versions_do_not():
    reissued = analyze_facts(
        [
            fact(version=VERSION_B, artifact="art_b", issued_minutes=49),
            fact(version=VERSION_A, artifact="art_a", issued_minutes=42),
        ]
    )
    canonical = reissued["canonicalization"]
    assert canonical["verified_opportunities"] == 2
    assert canonical["analytical_samples"] == 1
    assert canonical["sample_classification"] == {"identical_reissued_versions": 1}
    sample = reissued["samples"][0]
    assert sample["canonical_issued_forecast_id"] == VERSION_A  # earliest issued
    assert [v["issued_forecast_id"] for v in sample["provenance"]["versions"]] == [
        VERSION_A,
        VERSION_B,
    ]
    assert reissued["overall"]["n"] == 1
    assert reissued["overall"]["issued_versions_represented"] == 2

    differing = analyze_facts(
        [
            fact(version=VERSION_A, artifact="art_a", forecast=296.0),
            fact(version=VERSION_B, artifact="art_b", forecast=295.2),
        ]
    )
    assert differing["canonicalization"]["analytical_samples"] == 0
    [row] = differing["canonicalization"]["ambiguous"]
    assert row["level"] == "sample" and row["reason"] == "same_target_versions_differ"
    assert row["issued_forecast_ids"] == [VERSION_A, VERSION_B]
    assert row["distinct_forecast_temperatures_k"] == [295.2, 296.0]
    assert differing["overall"]["n"] == 0 and differing["overall"]["bias_k"] is None


def test_an_ambiguous_member_makes_the_whole_same_target_sample_ambiguous():
    result = canonicalize(
        [
            fact(version=VERSION_A, artifact="art_1", revision="rev-1", logical="x"),
            fact(
                version=VERSION_A, artifact="art_2", revision="rev-2", logical="x", observed=294.0
            ),
            fact(version=VERSION_B, artifact="art_3", revision="rev-1", logical="x"),
        ]
    )
    assert result["analytical_samples"] == 0
    assert [row["reason"] for row in result["ambiguous"]] == [
        "conflicting_observation_revisions",
        "member_opportunity_ambiguous",
    ]


def test_different_targets_at_one_valid_time_are_separate_samples_at_their_own_leads():
    valid = TARGET + timedelta(hours=20)
    facts = [
        fact(version=VERSION_A, artifact="a", target=valid - timedelta(hours=20), horizon=20),
        fact(
            version=VERSION_B,
            artifact="b",
            target=valid - timedelta(hours=8),
            horizon=8,
            forecast=295.5,
        ),
        fact(
            version=VERSION_C,
            artifact="c",
            target=valid - timedelta(hours=2),
            horizon=2,
            forecast=295.1,
        ),
    ]
    result = analyze_facts(facts)
    assert result["canonicalization"]["analytical_samples"] == 3
    assert {s["valid_time"] for s in result["samples"]} == {"2026-09-17T18:00:00Z"}
    assert [s["lead_bucket"] for s in result["samples"]] == ["19-36", "7-18", "1-6"]
    assert result["overall"]["distinct_target_reference_times"] == 3
    buckets = result["lead_buckets"]
    assert [buckets[name]["n"] for name in ("1-6", "7-18", "19-36")] == [1, 1, 1]
    assert buckets["1-6"]["bias_k"] == pytest.approx(0.1)
    assert buckets["7-18"]["bias_k"] == pytest.approx(0.5)
    assert buckets["19-36"]["bias_k"] == pytest.approx(1.0)
    # All three share one observation; the station row says so.
    assert result["stations"][0]["distinct_observations"] == 1
    assert result["stations"][0]["n"] == 3


def test_bias_mae_rmse_and_explicit_empty_groups():
    facts = [
        fact(artifact=f"art_{index}", horizon=index + 1, forecast=290.0 + error, observed=290.0)
        for index, error in enumerate((1.0, -2.0, 3.0))
    ]
    result = analyze_facts(facts)
    overall = result["overall"]
    assert overall["n"] == 3
    assert overall["bias_k"] == pytest.approx(2 / 3)
    assert overall["mae_k"] == pytest.approx(2.0)
    assert overall["rmse_k"] == pytest.approx(math.sqrt(14 / 3))
    assert overall["min_error_k"] == -2.0 and overall["max_error_k"] == 3.0
    assert overall["evidence"] == "descriptive_only"
    assert overall["earliest_valid_time"] == "2026-09-16T23:00:00Z"
    assert overall["latest_valid_time"] == "2026-09-17T01:00:00Z"
    assert result["lead_buckets"]["1-6"]["n"] == 3
    for empty in ("7-18", "19-36"):
        assert result["lead_buckets"][empty] == {
            "n": 0,
            "bias_k": None,
            "mae_k": None,
            "rmse_k": None,
            "min_error_k": None,
            "max_error_k": None,
            "evidence": "no_samples",
        }
    assert describe([]) == result["lead_buckets"]["7-18"]
    with pytest.raises(ValueError):
        describe([1.0, float("nan")])


def test_station_proxies_are_accounted_separately():
    facts = [
        fact(artifact="a", horizon=1, forecast=296.0, observed=295.0),
        fact(artifact="b", horizon=2, forecast=295.0, observed=295.5),
        fact(artifact="c", horizon=3, forecast=294.0, observed=292.0, station="KMSP"),
    ]
    result = analyze_facts(facts)
    assert result["overall"]["observation_stations"] == ["KMIC", "KMSP"]
    kmic, kmsp = result["stations"]
    assert (kmic["station_id"], kmic["n"], kmic["distance_km"]) == ("KMIC", 2, [11.1])
    assert kmic["bias_k"] == pytest.approx(0.25)
    assert (kmsp["station_id"], kmsp["n"], kmsp["elevation_m"]) == ("KMSP", 1, [256])
    assert kmsp["bias_k"] == pytest.approx(2.0)
    assert kmic["observation_time_offset_seconds"] == {"min": -420, "max": -420}
    assert "proxy" in site_analysis.ANALYSIS_POLICY["observation_proxy"]


def test_day_night_grouping_uses_the_period_convention_and_reports_its_zone():
    # 23Z and 00Z are 18:00 and 19:00 CDT (night); 17Z next day is 12:00 CDT (day).
    facts = [
        fact(artifact="a", horizon=1),
        fact(artifact="b", horizon=2, forecast=295.0, observed=295.5),
        fact(artifact="c", horizon=19, forecast=300.0, observed=298.0),
    ]
    local = analyze_facts(facts)["time_of_day"]
    assert local["zone"] == {
        "name": "America/Chicago",
        "source": "issuance_hourly_report",
        "saved_report_zones": ["America/Chicago"],
    }
    assert local["groups"]["night"]["n"] == 2
    assert local["groups"]["night"]["local_hours_represented"] == [18, 19]
    assert local["groups"]["day"]["n"] == 1
    assert local["groups"]["day"]["local_hours_represented"] == [12]
    assert local["groups"]["day"]["bias_k"] == pytest.approx(2.0)

    requested = analyze_facts(facts, display_timezone="UTC")["time_of_day"]
    assert requested["zone"]["source"] == "request"
    assert requested["groups"]["day"]["local_hours_represented"] == [17]
    assert requested["groups"]["night"]["local_hours_represented"] == [0, 23]
    assert "UTC wall clock" in requested["note"]

    mixed = analyze_facts([fact(artifact="a"), fact(artifact="b", horizon=2, zone="UTC")])[
        "time_of_day"
    ]
    assert mixed["zone"]["source"] == "default_utc"
    assert mixed["zone"]["saved_report_zones"] == ["America/Chicago", "UTC"]


def test_unusable_facts_are_excluded_with_reasons_and_never_counted():
    good = fact(artifact="good")
    rows = [
        good,
        fact(artifact="unverified", horizon=2, status="unavailable"),
        fact(artifact="policy", horizon=3, verification_policy_id="other-policy.v9"),
        fact(artifact="schema", horizon=4, schema_version="other.v2"),
        fact(artifact="invalid", horizon=5, quality_state="invalid"),
        fact(artifact="nan", horizon=6, forecast_temperature_k=float("nan")),
        fact(artifact="horizon", horizon=7, horizon_hours=9),
        fact(artifact="error", horizon=8, temperature_error_k=9.0),
        fact(artifact="range", horizon=9, horizon_hours=40),
        fact(artifact="integrity", horizon=10, integrity_exclusion="issued_forecast_not_found"),
        {**fact(artifact="incomplete", horizon=11), "observation": None},
    ]
    result = analyze_facts(rows)
    canonical = result["canonicalization"]
    assert canonical["stored_facts"] == 11 and canonical["usable_facts"] == 1
    assert canonical["excluded_by_reason"] == {
        "horizon_inconsistent_with_times": 1,
        "horizon_outside_1_to_36": 1,
        "invalid_quality_state": 1,
        "issued_forecast_not_found": 1,
        "nonfinite_or_missing_values": 1,
        "not_a_verified_fact": 1,
        "payload_incomplete": 1,
        "saved_error_disagrees_with_values": 1,
        "unsupported_fact_schema": 1,
        "unsupported_verification_policy": 1,
    }
    assert result["overall"]["n"] == 1


DAY = datetime(2026, 10, 1, 12, tzinfo=UTC)


def history(errors_by_target: dict[datetime, float], horizons: range) -> list[dict[str, Any]]:
    """One version per target; every horizon of a target shares that target's error."""
    rows = []
    for number, (target, error) in enumerate(sorted(errors_by_target.items())):
        for horizon in horizons:
            rows.append(
                fact(
                    version=f"00000000-0000-0000-0000-{number:012d}",
                    artifact=f"art_{number}_{horizon}",
                    target=target,
                    horizon=horizon,
                    forecast=290.0 + error,
                    observed=290.0,
                )
            )
    return rows


def test_tiny_history_fails_every_evidence_criterion():
    readiness = analyze_facts([fact()])["correction_readiness"]
    assert readiness["status"] == "insufficient_evidence"
    assert readiness["evidence_policy"] == "mesoforge-bias-evidence-policy.v1"
    assert readiness["correction_ready_lead_buckets"] == []
    assert readiness["candidate_correction"] is None
    assert readiness["observed_evidence"]["canonical_samples"] == 1
    assert readiness["observed_evidence"]["empty_lead_buckets"] == ["7-18", "19-36"]
    assert "recommended_adjustment" in readiness["not_concluded"]
    bucket = readiness["lead_buckets"]["1-6"]
    assert bucket["status"] == "insufficient_evidence"
    assert bucket["unmet_criteria"] == [
        "min_canonical_samples",
        "min_distinct_decision_dates",
        "max_share_from_one_decision_date",
        "bias_interval_excludes_zero",
    ]
    assert bucket["mean_bias_uncertainty"] is None  # one decision date: no interval
    empty = readiness["lead_buckets"]["19-36"]
    assert empty["canonical_samples"] == 0 and empty["mean_bias_k"] is None
    assert empty["criteria"]["max_share_from_one_decision_date"]["observed"] is None


def test_sample_count_alone_never_satisfies_the_policy():
    # Like the real Minneapolis 7-18 bucket: plenty of samples from one short episode.
    targets = {
        datetime(2026, 9, 16, 22, tzinfo=UTC): 1.3,
        datetime(2026, 9, 17, 2, tzinfo=UTC): 1.2,
        datetime(2026, 9, 17, 3, tzinfo=UTC): 1.4,
        datetime(2026, 9, 17, 4, tzinfo=UTC): 1.5,
    }
    readiness = analyze_facts(history(targets, range(7, 19)))["correction_readiness"]
    bucket = readiness["lead_buckets"]["7-18"]
    assert bucket["canonical_samples"] == 48
    assert bucket["criteria"]["min_canonical_samples"]["met"] is True
    assert bucket["distinct_decision_windows"] == 4
    assert bucket["distinct_decision_dates"] == 2
    assert bucket["criteria"]["min_distinct_decision_dates"] == {
        "required": 10,
        "observed": 2,
        "met": False,
    }
    assert bucket["criteria"]["max_share_from_one_decision_date"]["observed"] == 0.75
    assert bucket["status"] == "insufficient_evidence"
    assert readiness["status"] == "insufficient_evidence"
    assert readiness["correction_ready_lead_buckets"] == []


def test_a_bucket_meeting_every_criterion_is_ready_only_for_a_shadow_proposal():
    targets = {DAY + timedelta(days=day): 1.0 + 0.05 * (day % 3) for day in range(12)}
    result = analyze_facts(history(targets, range(1, 7)))
    readiness = result["correction_readiness"]
    bucket = readiness["lead_buckets"]["1-6"]
    assert bucket["status"] == "evidence_policy_met" and bucket["unmet_criteria"] == []
    assert bucket["canonical_samples"] == 72 and bucket["distinct_decision_dates"] == 12
    assert bucket["criteria"]["max_share_from_one_decision_date"]["observed"] == pytest.approx(
        1 / 12
    )
    low, high = bucket["mean_bias_uncertainty"]["interval_95_k"]
    assert 0 < low < bucket["mean_bias_k"] < high
    assert readiness["status"] == "evidence_policy_met_for_some_lead_buckets"
    assert readiness["correction_ready_lead_buckets"] == ["1-6"]
    # Other buckets are judged on their own samples; nothing is extrapolated to them.
    assert readiness["lead_buckets"]["7-18"]["status"] == "insufficient_evidence"
    assert readiness["lead_buckets"]["19-36"]["canonical_samples"] == 0
    # Meeting the policy computes and activates nothing.
    assert readiness["candidate_correction"] is None
    assert "recommended_adjustment" in readiness["not_concluded"]
    assert result["overall"]["evidence"] == "descriptive_only"
    assert "Promotion must weigh at least MAE" in site_analysis.EVIDENCE_POLICY["promotion"]
    assert site_analysis.EVIDENCE_POLICY["lifecycle"][2] == "shadow correction on future forecasts"


def test_concentrated_evidence_is_refused_even_with_enough_samples_and_dates():
    targets = {DAY + timedelta(hours=hour): 1.0 for hour in range(8)}  # one busy date
    targets.update({DAY + timedelta(days=day): 1.1 for day in range(1, 10)})
    bucket = analyze_facts(history(targets, range(1, 7)))["correction_readiness"]["lead_buckets"][
        "1-6"
    ]
    assert bucket["canonical_samples"] == 102 and bucket["distinct_decision_dates"] == 10
    assert bucket["distinct_decision_windows"] == 17
    assert bucket["criteria"]["max_share_from_one_decision_date"]["observed"] == pytest.approx(
        48 / 102
    )
    assert bucket["unmet_criteria"] == ["max_share_from_one_decision_date"]
    assert bucket["status"] == "insufficient_evidence"


def test_inconsistent_evidence_is_refused_and_its_uncertainty_is_reported():
    targets = {DAY + timedelta(days=day): (1.0 if day % 2 else -1.0) for day in range(12)}
    bucket = analyze_facts(history(targets, range(1, 7)))["correction_readiness"]["lead_buckets"][
        "1-6"
    ]
    assert bucket["unmet_criteria"] == ["bias_interval_excludes_zero"]
    low, high = bucket["mean_bias_uncertainty"]["interval_95_k"]
    assert low < 0 < high
    assert bucket["criteria"]["bias_interval_excludes_zero"]["observed"] is False


def test_mean_bias_uncertainty_is_clustered_by_decision_date():
    # Date means 1.0 and 3.0: centre 2.0, sd sqrt(2), standard error 1.0, t(1) = 12.706.
    targets = {DAY: 1.0, DAY + timedelta(hours=1): 1.0, DAY + timedelta(days=1): 3.0}
    bucket = site_analysis.evaluate_evidence(
        analyze_facts(history(targets, range(1, 3)))["samples"]
    )
    uncertainty = bucket["mean_bias_uncertainty"]
    assert uncertainty["method"] == "decision_date_means_student_t_95"
    assert uncertainty["decision_dates"] == 2
    assert uncertainty["mean_of_decision_date_means_k"] == pytest.approx(2.0)
    assert uncertainty["standard_error_k"] == pytest.approx(1.0)
    assert uncertainty["t_multiplier"] == 12.706
    assert uncertainty["interval_95_k"] == pytest.approx([2.0 - 12.706, 2.0 + 12.706])
    # The plain sample mean is reported separately: four samples at 1.0, two at 3.0.
    assert bucket["mean_bias_k"] == pytest.approx((4 * 1.0 + 2 * 3.0) / 6)
    assert site_analysis._t975(9) == 2.262
    assert site_analysis._t975(30) == 2.042
    assert site_analysis._t975(45) == 2.040 and site_analysis._t975(200) == 2.000


def test_policies_are_versioned_and_state_their_limits():
    evidence = site_analysis.EVIDENCE_POLICY
    assert evidence["id"] == "mesoforge-bias-evidence-policy.v1"
    assert evidence["criteria"] == {
        "min_canonical_samples": 30,
        "min_distinct_decision_dates": 10,
        "max_share_from_one_decision_date": 0.25,
        "bias_interval_excludes_zero": True,
    }
    assert evidence["lead_buckets"] == ["1-6", "7-18", "19-36"]
    assert "not a claim" in evidence["purpose"]
    assert evidence["lifecycle"][0] == "verified historical evidence"
    assert "bias-corrected baseline" in evidence["ai_desk"]
    window = site_analysis.DECISION_WINDOW_POLICY
    assert window["id"] == "mesoforge-decision-window-policy.v1"
    assert "first successful eligible issuance" in window["primary"]
    assert "never silently replaces" in window["different_reissue"]
    assert set(window["future_metadata"]) == {"decision_window_id", "issuance_role", "reissue_of"}
    assert site_analysis.ANALYSIS_POLICY["id"] == "mesoforge-site-verification-analysis.v2"
    assert "evidence_policy_met" in site_analysis.ANALYSIS_POLICY["evidence_states"]


def test_regime_readiness_reports_availability_without_splits():
    facts = [
        fact(artifact="a"),
        fact(
            artifact="b",
            horizon=2,
            context={
                "fields": {
                    "wind_speed_10m": 5.5,
                    "cloud_area_fraction": 0.57,
                    "precipitation_type": "rain",
                },
                "model_temperatures_k": {"HRRR": 296.0, "GFS": 294.0, "RAP": 295.0, "IFS": 293.5},
            },
        ),
    ]
    readiness = analyze_facts(facts)["regime_readiness"]
    assert readiness["status"] == "reconstructable_dimensions_only"
    assert readiness["exploratory_splits"] == []
    dimensions = readiness["dimensions"]
    assert dimensions["wind_speed"] == {
        "saved_field": "wind_speed_10m",
        "available_samples": 2,
        "total_samples": 2,
        "min": 3.4,
        "max": 5.5,
    }
    assert dimensions["sky_cloud_fraction"]["available_samples"] == 1
    assert dimensions["precipitation_type"]["categories"] == {"rain": 1}
    assert dimensions["thunder_probability"]["available_samples"] == 0
    spread = dimensions["model_temperature_spread"]
    assert spread["available_samples"] == 2
    assert spread["min"] == pytest.approx(0.6) and spread["max"] == pytest.approx(2.5)
    assert spread["samples_by_model_count"] == {"2": 1, "4": 1}


def test_result_is_independent_of_input_order_and_does_not_mutate_inputs():
    facts = [
        fact(artifact="a1"),
        fact(artifact="a2", registered_minutes=50),
        fact(version=VERSION_B, artifact="b1", issued_minutes=49),
        fact(artifact="c", horizon=5, forecast=292.7, observed=290.95),
        fact(version=VERSION_C, artifact="d", target=TARGET + timedelta(hours=4), horizon=1),
    ]
    original = deepcopy(facts)
    baseline = analyze_facts(facts)
    for shift in range(1, len(facts)):
        rotated = facts[shift:] + facts[:shift]
        assert analyze_facts(rotated) == baseline
        assert analyze_facts(list(reversed(rotated))) == baseline
    assert facts == original
    assert baseline["canonicalization"]["stored_facts"] == 5
    assert baseline["canonicalization"]["analytical_samples"] == 3
