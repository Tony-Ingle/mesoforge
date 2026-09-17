"""Canonical verified temperature samples and descriptive site statistics.

Pure computation over already-extracted verification fact records. A stored fact is
evidence, not automatically a statistical sample: facts are first resolved to one
verified opportunity per issued version and hour, then to one analytical sample per
target reference time and valid time. Conflicts are reported, never resolved by rule.
No correction, weight, regime label or skill claim is derived here.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from mesoforge.forecasting.periods import DAY_START, NIGHT_START
from mesoforge.verification.metrics import _compute_scalar_metrics
from mesoforge.verification.model_comparison import LEAD_BUCKETS, lead_bucket

SCHEMA_VERSION = "mesoforge.site-verification-analysis.v1"
FACT_SCHEMA_VERSION = "issued-temperature-verification.v1"
VERIFICATION_POLICY_ID = "issued-temperature-verification.v1"
_ERROR_TOLERANCE_K = 1e-9

CANONICALIZATION_POLICY: dict[str, Any] = {
    "id": "mesoforge-verification-canonicalization.v1",
    "principle": (
        "A stored verification fact is immutable evidence, not automatically a statistical "
        "sample. Every fact stays listed in provenance; statistics use canonical samples only."
    ),
    "usable_fact": (
        f"schema {FACT_SCHEMA_VERSION}, status verified, verification policy "
        f"{VERIFICATION_POLICY_ID}, not invalid, finite kelvin values, horizon equal to "
        "valid_time - target_reference_time within 1..36 h, and a saved error equal to "
        "forecast minus observation"
    ),
    "opportunity_key": ["issued_forecast_id", "valid_time", "verification_policy_id"],
    "evidence_signature": [
        "forecast_temperature_k",
        "issued_forecast_digest",
        "station_id",
        "observation_time",
        "observation_temperature_k",
        "observation_revision_digest",
        "logical_observation_digest",
        "matching_policy_digest",
        "temperature_error_k",
    ],
    "duplicate_facts": (
        "Facts of one opportunity with one identical evidence signature are the same "
        "verification repeated over a re-acquired copy of the same observation revision "
        "(acquisition artifact, ingest time, cutoff and candidate list may differ). They form "
        "one opportunity; the canonical fact is the earliest registered, then lowest ID."
    ),
    "conflicting_facts": (
        "Facts of one opportunity with different evidence signatures are analytically "
        "ambiguous (for example a revised observation). No revision is preferred by rule; the "
        "opportunity is excluded from statistics and reported with its reason."
    ),
    "sample_key": [
        "latitude",
        "longitude",
        "target_reference_time",
        "valid_time",
        "verification_policy_id",
    ],
    "same_target_versions": (
        "Several issued versions for one target reference time and valid time are one "
        "sample only when their forecast value, selected observation revision and error are "
        "identical (a re-issue of the same forecast); the canonical version is the earliest "
        "issued, then lowest ID. Versions that differ are ambiguous and excluded, because no "
        "approved rule says which version represents the decision window."
    ),
    "different_targets": (
        "Versions with different target reference times verified at the same valid time are "
        "separate samples at their own horizons, although they share one observation."
    ),
    "not_changed": "No stored fact, issuance, observation or attribute is modified or deleted.",
}

DECISION_WINDOW_POLICY: dict[str, Any] = {
    "id": "mesoforge-decision-window-policy.v1",
    "status": "owner-approved; stored issuances do not yet carry decision-window metadata",
    "primary": (
        "The first successful eligible issuance for a scheduled decision window is the "
        "canonical operational forecast of that window."
    ),
    "identical_reissue": "Preserved, and collapsed analytically with the primary version.",
    "different_reissue": (
        "Preserved as an alternate/reissue; it never silently replaces the primary version."
    ),
    "historical_versions": (
        "Versions issued before this policy carry no decision-window identity or role. When "
        "the operational version cannot be determined safely they remain ambiguous; nothing "
        "is rewritten or relabelled."
    ),
    "current_analysis": (
        "Until issuances carry decision_window_id and issuance_role, a decision window is "
        "identified by coordinate and target_reference_time, identical versions collapse to "
        "the earliest issued, and versions that differ stay ambiguous."
    ),
    "future_metadata": {
        "decision_window_id": "stable identity of the scheduled decision window",
        "issuance_role": "primary | reissue",
        "reissue_of": "issued_forecast_id of the primary version, only for a reissue",
    },
}

_EVIDENCE_MIN_SAMPLES = 30
_EVIDENCE_MIN_DECISION_DATES = 10
_EVIDENCE_MAX_DATE_SHARE = 0.25
EVIDENCE_POLICY: dict[str, Any] = {
    "id": "mesoforge-bias-evidence-policy.v1",
    "purpose": (
        "Minimum evidence before a deterministic temperature-bias correction may be proposed "
        "for shadow evaluation. Versioned initial governance thresholds, not a claim that "
        "they are statistically sufficient."
    ),
    "samples": "canonical verified samples only",
    "scope": "each lead bucket is evaluated independently; nothing is pooled or extrapolated",
    "lead_buckets": list(LEAD_BUCKETS),
    "criteria": {
        "min_canonical_samples": _EVIDENCE_MIN_SAMPLES,
        "min_distinct_decision_dates": _EVIDENCE_MIN_DECISION_DATES,
        "max_share_from_one_decision_date": _EVIDENCE_MAX_DATE_SHARE,
        "bias_interval_excludes_zero": True,
    },
    "definitions": {
        "decision_date": "UTC calendar date of the sample's target_reference_time",
        "multiple_episodes": (
            "No weather-episode detection exists. Ten distinct decision dates span at least "
            "nine days, longer than one contiguous weather event, and the share limit keeps "
            "one date from dominating; together they are the v1 proxy for several episodes."
        ),
        "mean_bias_uncertainty": (
            "Hourly errors within one decision date are not independent, so the interval is "
            "built from decision-date means: mean of the date means +/- t(0.975, D-1) * "
            "sd(date means) / sqrt(D). Reported whenever at least two decision dates exist."
        ),
        "inconsistent": "the 95% interval of the mean bias includes zero",
    },
    "owner_specified": [
        "canonical samples only",
        "independent lead buckets 1-6, 7-18 and 19-36",
        "at least 30 canonical samples in the bucket",
        "at least 10 distinct forecast decision dates/windows",
        "evidence spanning several forecast episodes",
        "reported uncertainty of the mean bias",
        "no proposal from sparse, concentrated or inconsistent evidence",
    ],
    "operational_proxies": [
        "decision dates are counted as UTC dates (hourly windows of one evening are one date)",
        "concentration limit of 25% of a bucket's samples from one decision date",
        "date-clustered 95% interval; inconsistent when it includes zero",
    ],
    "lifecycle": [
        "verified historical evidence",
        "deterministic candidate correction",
        "shadow correction on future forecasts",
        "identical-sample verification against the unchanged baseline",
        "human, versioned promotion decision only if improvement is demonstrated",
    ],
    "promotion": (
        "Meeting this policy never activates a correction. Promotion must weigh at least MAE "
        "and RMSE, not mean bias alone, on identical samples against the unchanged baseline."
    ),
    "ai_desk": "A later AI forecast desk is evaluated against the bias-corrected baseline.",
    "not_implemented": "No candidate correction value is calculated and nothing is applied.",
}

# Two-sided 95% Student t multipliers for 1..30 degrees of freedom.
_T975: tuple[float, ...] = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip

ANALYSIS_POLICY: dict[str, Any] = {
    "id": "mesoforge-site-verification-analysis.v2",
    "field": "air_temperature_2m",
    "unit": "K",
    "error_definition": "forecast_minus_observation",
    "metrics": "bias = mean(error), mae = mean(|error|), rmse = sqrt(mean(error^2)), float64",
    "lead_buckets": {
        "basis": "saved horizon_hours since target_reference_time",
        "buckets": list(LEAD_BUCKETS),
        "empty_bucket": "n = 0 with null metrics; nothing is extrapolated between buckets",
    },
    "time_of_day": {
        "convention": "local_12_hour_day_night (period-summary convention)",
        "day": "06:00-18:00 local valid time",
        "night": "18:00-06:00 local valid time",
        "closure": "left_closed_right_open",
        "zone": "request, otherwise the single saved report zone of the samples, otherwise UTC",
    },
    "observation_proxy": (
        "The observation is a nearby station selected by the saved matching policy; a "
        "station is a proxy for, not physically identical to, the forecast coordinate."
    ),
    "evidence_states": {
        "descriptive_only": "a metric computed from the available canonical samples",
        "no_samples": "no canonical sample exists for this group; metrics are null",
        "evidence_policy_met": (
            f"a lead bucket satisfies every criterion of {EVIDENCE_POLICY['id']}: a candidate "
            "correction may be proposed for shadow evaluation; nothing becomes active"
        ),
        "insufficient_evidence": (
            "at least one evidence criterion is unmet; no correction may be proposed"
        ),
    },
    "changes": "v2 evaluates the evidence policy per lead bucket; v1 metrics are unchanged",
    "not_derived": "No correction, learned weight, regime label, preferred model or skill claim.",
}

_REGIME_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("sky_cloud_fraction", "cloud_area_fraction", "numeric"),
    ("wind_speed", "wind_speed_10m", "numeric"),
    ("wind_direction", "wind_from_direction_10m", "numeric"),
    ("wind_gust", "wind_gust_10m", "numeric"),
    ("dew_point", "dew_point_temperature_2m", "numeric"),
    ("relative_humidity", "relative_humidity_2m", "numeric"),
    ("precipitation_amount", "liquid_equivalent_precipitation_amount_1h", "numeric"),
    ("precipitation_probability", "probability_of_precipitation_1h", "numeric"),
    ("precipitation_type", "precipitation_type", "category"),
    ("thunder_probability", "probability_of_thunder_1h", "numeric"),
)


def _instant(value: Any) -> datetime:
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instants must include a timezone")
    return instant.astimezone(UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def fact_exclusion_reason(fact: Mapping[str, Any]) -> str | None:
    """Why one extracted fact cannot enter canonicalization, or None when usable."""
    integrity = fact.get("integrity_exclusion")
    if integrity is not None:
        return str(integrity)
    if fact.get("schema_version") != FACT_SCHEMA_VERSION:
        return "unsupported_fact_schema"
    if fact.get("quality_state") == "invalid":
        return "invalid_quality_state"
    if fact.get("status") != "verified":
        return "not_a_verified_fact"
    if fact.get("verification_policy_id") != VERIFICATION_POLICY_ID:
        return "unsupported_verification_policy"
    observation = fact.get("observation")
    if not isinstance(observation, Mapping):
        return "payload_incomplete"
    required = (
        fact.get("issued_forecast_id"),
        fact.get("issued_forecast_digest"),
        fact.get("matching_policy_digest"),
        observation.get("station_id"),
        observation.get("revision_digest"),
        observation.get("logical_observation_digest"),
    )
    if any(not isinstance(value, str) or not value for value in required):
        return "payload_incomplete"
    try:
        target = _instant(fact.get("target_reference_time"))
        valid = _instant(fact.get("valid_time"))
        _instant(observation.get("observation_time"))
        _instant(fact.get("registered_at"))
    except (TypeError, ValueError):
        return "payload_incomplete"
    values = (
        fact.get("forecast_temperature_k"),
        observation.get("temperature_k"),
        fact.get("temperature_error_k"),
    )
    if not all(_finite(value) for value in values):
        return "nonfinite_or_missing_values"
    horizon = fact.get("horizon_hours")
    if type(horizon) is not int or not 1 <= horizon <= 36:
        return "horizon_outside_1_to_36"
    if (valid - target).total_seconds() != horizon * 3600:
        return "horizon_inconsistent_with_times"
    forecast, observed, error = (float(cast(float, value)) for value in values)
    if abs((forecast - observed) - error) > _ERROR_TOLERANCE_K:
        return "saved_error_disagrees_with_values"
    return None


def _signature(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    observation = fact["observation"]
    return (
        fact["forecast_temperature_k"],
        fact["issued_forecast_digest"],
        observation["station_id"],
        _instant(observation["observation_time"]),
        observation["temperature_k"],
        observation["revision_digest"],
        observation["logical_observation_digest"],
        fact["matching_policy_digest"],
        fact["temperature_error_k"],
    )


def _version_signature(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    # Versions are different immutable objects, so their issuance digest always differs.
    signature = _signature(fact)
    return signature[:1] + signature[2:]


def _conflict_reason(facts: Sequence[Mapping[str, Any]]) -> str:
    def distinct(getter: Any) -> int:
        return len({getter(fact) for fact in facts})

    if distinct(lambda f: (f["forecast_temperature_k"], f["issued_forecast_digest"])) > 1:
        return "different_forecast_evidence"
    if distinct(lambda f: f["matching_policy_digest"]) > 1:
        return "different_matching_policy"
    if distinct(lambda f: f["observation"]["logical_observation_digest"]) > 1:
        return "different_selected_observation"
    if distinct(lambda f: f["observation"]["revision_digest"]) > 1:
        return "conflicting_observation_revisions"
    return "different_error_or_observation_value"


def _fact_order(fact: Mapping[str, Any]) -> tuple[datetime, str]:
    return _instant(fact["registered_at"]), str(fact["artifact_id"])


def canonicalize(facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Resolve facts to opportunities, then opportunities to analytical samples."""
    excluded: list[dict[str, Any]] = []
    usable: list[Mapping[str, Any]] = []
    for fact in facts:
        reason = fact_exclusion_reason(fact)
        if reason is None:
            usable.append(fact)
        else:
            excluded.append({"artifact_id": str(fact.get("artifact_id")), "reason": reason})

    grouped: dict[tuple[str, datetime, str], list[Mapping[str, Any]]] = {}
    for fact in usable:
        key = (
            fact["issued_forecast_id"],
            _instant(fact["valid_time"]),
            fact["verification_policy_id"],
        )
        grouped.setdefault(key, []).append(fact)

    opportunities: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    classifications: Counter[str] = Counter()
    for key in sorted(grouped, key=lambda item: (item[1], item[0], item[2])):
        members = sorted(grouped[key], key=_fact_order)
        fact_ids = [str(fact["artifact_id"]) for fact in members]
        if len({_signature(fact) for fact in members}) == 1:
            classification = "single_fact" if len(members) == 1 else "identical_evidence_reacquired"
            classifications[classification] += 1
            opportunities.append(
                {"fact": members[0], "fact_ids": fact_ids, "classification": classification}
            )
        else:
            reason = _conflict_reason(members)
            classifications["ambiguous"] += 1
            ambiguous.append(
                {
                    "level": "opportunity",
                    "reason": reason,
                    "issued_forecast_ids": [key[0]],
                    "valid_time": _iso(key[1]),
                    "fact_ids": fact_ids,
                    "distinct_observation_revisions": sorted(
                        {fact["observation"]["revision_digest"] for fact in members}
                    ),
                    "distinct_errors_k": sorted({fact["temperature_error_k"] for fact in members}),
                }
            )

    ambiguous_keys = {
        (_instant(grouped[key][0]["target_reference_time"]), key[1], key[2])
        for key in grouped
        if len({_signature(fact) for fact in grouped[key]}) > 1
    }
    by_sample: dict[tuple[datetime, datetime, str], list[dict[str, Any]]] = {}
    for opportunity in opportunities:
        fact = opportunity["fact"]
        sample_key = (
            _instant(fact["target_reference_time"]),
            _instant(fact["valid_time"]),
            fact["verification_policy_id"],
        )
        by_sample.setdefault(sample_key, []).append(opportunity)

    samples: list[dict[str, Any]] = []
    version_classifications: Counter[str] = Counter()
    for sample_key in sorted(by_sample):
        members = sorted(
            by_sample[sample_key],
            key=lambda item: (
                _instant(item["fact"]["issued_at"]),
                item["fact"]["issued_forecast_id"],
            ),
        )
        version_ids = [item["fact"]["issued_forecast_id"] for item in members]
        all_fact_ids = [fact_id for item in members for fact_id in item["fact_ids"]]
        if sample_key in ambiguous_keys:
            version_classifications["ambiguous"] += 1
            ambiguous.append(
                {
                    "level": "sample",
                    "reason": "member_opportunity_ambiguous",
                    "issued_forecast_ids": version_ids,
                    "valid_time": _iso(sample_key[1]),
                    "fact_ids": all_fact_ids,
                }
            )
            continue
        if len({_version_signature(item["fact"]) for item in members}) > 1:
            version_classifications["ambiguous"] += 1
            ambiguous.append(
                {
                    "level": "sample",
                    "reason": "same_target_versions_differ",
                    "issued_forecast_ids": version_ids,
                    "valid_time": _iso(sample_key[1]),
                    "fact_ids": all_fact_ids,
                    "distinct_forecast_temperatures_k": sorted(
                        {item["fact"]["forecast_temperature_k"] for item in members}
                    ),
                }
            )
            continue
        classification = "single_version" if len(members) == 1 else "identical_reissued_versions"
        version_classifications[classification] += 1
        canonical = members[0]
        fact = canonical["fact"]
        samples.append(
            {
                "latitude": fact["latitude"],
                "longitude": fact["longitude"],
                "target_reference_time": _iso(sample_key[0]),
                "valid_time": _iso(sample_key[1]),
                "horizon_hours": fact["horizon_hours"],
                "lead_bucket": lead_bucket(fact["horizon_hours"]),
                "verification_policy_id": fact["verification_policy_id"],
                "forecast_temperature_k": fact["forecast_temperature_k"],
                "observation": dict(fact["observation"]),
                "temperature_error_k": fact["temperature_error_k"],
                "canonical_issued_forecast_id": fact["issued_forecast_id"],
                "canonical_fact_id": str(fact["artifact_id"]),
                "version_classification": classification,
                "saved_display_timezone": fact.get("display_timezone"),
                "context": dict(fact.get("context") or {}),
                "provenance": {
                    "versions": [
                        {
                            "issued_forecast_id": item["fact"]["issued_forecast_id"],
                            "issued_at": _iso(_instant(item["fact"]["issued_at"])),
                            "fact_classification": item["classification"],
                            "canonical_fact_id": str(item["fact"]["artifact_id"]),
                            "fact_ids": item["fact_ids"],
                        }
                        for item in members
                    ],
                    "stored_fact_count": len(all_fact_ids),
                },
            }
        )

    return {
        "stored_facts": len(facts),
        "usable_facts": len(usable),
        "excluded_facts": excluded,
        "excluded_by_reason": dict(sorted(Counter(row["reason"] for row in excluded).items())),
        "verified_opportunities": len(grouped),
        "opportunities_with_multiple_facts": sum(len(rows) > 1 for rows in grouped.values()),
        "fact_classification": dict(sorted(classifications.items())),
        "sample_classification": dict(sorted(version_classifications.items())),
        "ambiguous": ambiguous,
        "analytical_samples": len(samples),
        "samples": samples,
    }


def describe(errors: Sequence[float]) -> dict[str, Any]:
    """Descriptive error metrics; an empty group stays explicitly empty."""
    bias, mae, rmse = _compute_scalar_metrics(list(errors))
    return {
        "n": len(errors),
        "bias_k": bias,
        "mae_k": mae,
        "rmse_k": rmse,
        "min_error_k": min(errors) if errors else None,
        "max_error_k": max(errors) if errors else None,
        "evidence": "descriptive_only" if errors else "no_samples",
    }


def _time_of_day(
    samples: Sequence[Mapping[str, Any]], display_timezone: str | None
) -> dict[str, Any]:
    saved = sorted(
        {str(s["saved_display_timezone"]) for s in samples if s["saved_display_timezone"]}
    )
    if display_timezone is not None:
        name, source = display_timezone, "request"
    elif len(saved) == 1:
        name, source = saved[0], "issuance_hourly_report"
    else:
        name, source = "UTC", "default_utc"
    zone = ZoneInfo(name)
    groups: dict[str, list[float]] = {"day": [], "night": []}
    local_hours: dict[str, set[int]] = {"day": set(), "night": set()}
    for sample in samples:
        local = _instant(sample["valid_time"]).astimezone(zone)
        kind = "day" if DAY_START <= local.time() < NIGHT_START else "night"
        groups[kind].append(sample["temperature_error_k"])
        local_hours[kind].add(local.hour)
    note = None
    if name == "UTC":
        note = (
            "Grouped by UTC wall clock; request a display_timezone for a local grouping "
            "when the saved report zone is UTC."
        )
    elif len(saved) > 1 and source != "request":
        note = "Samples were saved with different report zones."
    return {
        "convention": ANALYSIS_POLICY["time_of_day"]["convention"],
        "zone": {"name": name, "source": source, "saved_report_zones": saved},
        "note": note,
        "groups": {
            kind: {**describe(errors), "local_hours_represented": sorted(local_hours[kind])}
            for kind, errors in groups.items()
        },
    }


def _stations(samples: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_station: dict[str, list[Mapping[str, Any]]] = {}
    for sample in samples:
        by_station.setdefault(sample["observation"]["station_id"], []).append(sample)
    rows = []
    for station_id in sorted(by_station):
        members = by_station[station_id]
        first = members[0]["observation"]
        offsets = [m["observation"].get("time_difference_seconds") for m in members]
        numeric_offsets = [value for value in offsets if _finite(value)]
        rows.append(
            {
                "station_id": station_id,
                "network": first.get("network"),
                "provider": first.get("provider"),
                "distance_km": sorted({m["observation"].get("distance_km") for m in members}),
                "elevation_m": sorted({m["observation"].get("elevation_m") for m in members}),
                "distinct_observations": len(
                    {m["observation"]["logical_observation_digest"] for m in members}
                ),
                "observation_time_offset_seconds": {
                    "min": min(numeric_offsets) if numeric_offsets else None,
                    "max": max(numeric_offsets) if numeric_offsets else None,
                },
                **describe([m["temperature_error_k"] for m in members]),
            }
        )
    return rows


def _regime_readiness(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dimensions: dict[str, Any] = {}
    total = len(samples)
    for name, field, kind in _REGIME_DIMENSIONS:
        values = [(s["context"].get("fields") or {}).get(field) for s in samples]
        present = [value for value in values if value is not None]
        row: dict[str, Any] = {
            "saved_field": field,
            "available_samples": len(present),
            "total_samples": total,
        }
        if kind == "numeric":
            numbers = [float(value) for value in present if _finite(value)]
            row["min"] = min(numbers) if numbers else None
            row["max"] = max(numbers) if numbers else None
        else:
            row["categories"] = dict(sorted(Counter(str(value) for value in present).items()))
        dimensions[name] = row
    spreads = []
    model_counts: Counter[int] = Counter()
    for sample in samples:
        temperatures = [
            float(value)
            for value in (sample["context"].get("model_temperatures_k") or {}).values()
            if _finite(value)
        ]
        model_counts[len(temperatures)] += 1
        if len(temperatures) >= 2:
            spreads.append(max(temperatures) - min(temperatures))
    dimensions["model_temperature_spread"] = {
        "saved_field": "sources and shadow_sources temperatures of the saved hour",
        "available_samples": len(spreads),
        "total_samples": total,
        "min": min(spreads) if spreads else None,
        "max": max(spreads) if spreads else None,
        "samples_by_model_count": {str(k): v for k, v in sorted(model_counts.items())},
    }
    return {
        "status": "reconstructable_dimensions_only",
        "source": "the immutable saved forecast hour embedded in each canonical fact",
        "dimensions": dimensions,
        "exploratory_splits": [],
        "samples_with_several_identical_versions": sum(
            len(sample["provenance"]["versions"]) > 1 for sample in samples
        ),
        "note": (
            "Availability and value ranges only. No regime classes, thresholds or splits are "
            "defined; the accumulated data should drive the eventual regime design. Context "
            "is read from each sample's canonical version; an identical re-issued version may "
            "have saved additional fields."
        ),
    }


def _t975(degrees_of_freedom: int) -> float:
    if degrees_of_freedom <= len(_T975):
        return _T975[degrees_of_freedom - 1]
    # Conservative upper bounds beyond the table (t(31) = 2.040, t(61) = 2.000).
    return 2.040 if degrees_of_freedom <= 60 else 2.000


def _mean_bias_uncertainty(date_errors: Mapping[str, Sequence[float]]) -> dict[str, Any] | None:
    """Date-clustered 95% interval of the mean bias; None with fewer than two dates."""
    means = [sum(errors) / len(errors) for _, errors in sorted(date_errors.items())]
    count = len(means)
    if count < 2:
        return None
    center = sum(means) / count
    variance = sum((value - center) ** 2 for value in means) / (count - 1)
    standard_error = math.sqrt(variance / count)
    multiplier = _t975(count - 1)
    return {
        "method": "decision_date_means_student_t_95",
        "decision_dates": count,
        "mean_of_decision_date_means_k": center,
        "standard_error_k": standard_error,
        "t_multiplier": multiplier,
        "interval_95_k": [
            center - multiplier * standard_error,
            center + multiplier * standard_error,
        ],
    }


def evaluate_evidence(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate one lead bucket's canonical samples against the evidence policy."""
    date_errors: dict[str, list[float]] = {}
    for sample in samples:
        date = _instant(sample["target_reference_time"]).date().isoformat()
        date_errors.setdefault(date, []).append(sample["temperature_error_k"])
    count = len(samples)
    dates = sorted(date_errors)
    largest_share = max(len(errors) for errors in date_errors.values()) / count if count else None
    uncertainty = _mean_bias_uncertainty(date_errors)
    excludes_zero = uncertainty is not None and not (
        uncertainty["interval_95_k"][0] <= 0.0 <= uncertainty["interval_95_k"][1]
    )
    valid_times = sorted(_instant(sample["valid_time"]) for sample in samples)
    criteria: dict[str, dict[str, Any]] = {
        "min_canonical_samples": {
            "required": _EVIDENCE_MIN_SAMPLES,
            "observed": count,
            "met": count >= _EVIDENCE_MIN_SAMPLES,
        },
        "min_distinct_decision_dates": {
            "required": _EVIDENCE_MIN_DECISION_DATES,
            "observed": len(dates),
            "met": len(dates) >= _EVIDENCE_MIN_DECISION_DATES,
        },
        "max_share_from_one_decision_date": {
            "required": _EVIDENCE_MAX_DATE_SHARE,
            "observed": largest_share,
            "met": largest_share is not None and largest_share <= _EVIDENCE_MAX_DATE_SHARE,
        },
        "bias_interval_excludes_zero": {
            "required": True,
            "observed": excludes_zero if uncertainty is not None else None,
            "met": excludes_zero,
        },
    }
    unmet = [name for name, row in criteria.items() if not row["met"]]
    return {
        "status": "insufficient_evidence" if unmet else "evidence_policy_met",
        "unmet_criteria": unmet,
        "criteria": criteria,
        "canonical_samples": count,
        "distinct_decision_windows": len({s["target_reference_time"] for s in samples}),
        "distinct_decision_dates": len(dates),
        "earliest_decision_date": dates[0] if dates else None,
        "latest_decision_date": dates[-1] if dates else None,
        "verified_valid_time_span_hours": (
            (valid_times[-1] - valid_times[0]).total_seconds() / 3600 if valid_times else None
        ),
        "observation_stations": sorted({s["observation"]["station_id"] for s in samples}),
        "mean_bias_k": sum(s["temperature_error_k"] for s in samples) / count if count else None,
        "mean_bias_uncertainty": uncertainty,
    }


def _correction_readiness(
    samples: Sequence[Mapping[str, Any]], lead_buckets: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    valid_times = sorted(_instant(sample["valid_time"]) for sample in samples)
    span_hours = (valid_times[-1] - valid_times[0]).total_seconds() / 3600 if valid_times else None
    by_bucket = {
        bucket: evaluate_evidence([s for s in samples if s["lead_bucket"] == bucket])
        for bucket in LEAD_BUCKETS
    }
    ready = [b for b in LEAD_BUCKETS if by_bucket[b]["status"] == "evidence_policy_met"]
    return {
        "status": "evidence_policy_met_for_some_lead_buckets" if ready else "insufficient_evidence",
        "evidence_policy": EVIDENCE_POLICY["id"],
        "basis": (
            "Each lead bucket is evaluated independently against the evidence policy. Meeting "
            "it only permits proposing a candidate correction for shadow evaluation; this "
            "report calculates no correction value and nothing becomes active."
        ),
        "correction_ready_lead_buckets": ready,
        "lead_buckets": by_bucket,
        "candidate_correction": None,
        "observed_evidence": {
            "canonical_samples": len(samples),
            "lead_buckets_with_samples": [b for b in LEAD_BUCKETS if lead_buckets[b]["n"]],
            "empty_lead_buckets": [b for b in LEAD_BUCKETS if not lead_buckets[b]["n"]],
            "distinct_target_reference_times": len({s["target_reference_time"] for s in samples}),
            "distinct_valid_times": len({s["valid_time"] for s in samples}),
            "distinct_observations": len(
                {s["observation"]["logical_observation_digest"] for s in samples}
            ),
            "distinct_utc_valid_dates": len({s["valid_time"][:10] for s in samples}),
            "verified_valid_time_span_hours": span_hours,
            "observation_stations": sorted({s["observation"]["station_id"] for s in samples}),
        },
        "not_concluded": [
            "persistent_site_bias",
            "lead_dependent_correction",
            "regime_bias",
            "preferred_model",
            "recommended_adjustment",
        ],
    }


def analyze_facts(
    facts: Sequence[Mapping[str, Any]], *, display_timezone: str | None = None
) -> dict[str, Any]:
    """Canonicalize facts for one coordinate and describe the verified temperature errors."""
    canonical = canonicalize(facts)
    samples = canonical["samples"]
    errors = [sample["temperature_error_k"] for sample in samples]
    valid_times = sorted(sample["valid_time"] for sample in samples)
    lead_buckets = {
        bucket: describe([s["temperature_error_k"] for s in samples if s["lead_bucket"] == bucket])
        for bucket in LEAD_BUCKETS
    }
    return {
        "canonicalization": {key: value for key, value in canonical.items() if key != "samples"},
        "overall": {
            **describe(errors),
            "earliest_valid_time": valid_times[0] if valid_times else None,
            "latest_valid_time": valid_times[-1] if valid_times else None,
            "distinct_target_reference_times": len({s["target_reference_time"] for s in samples}),
            "issued_versions_represented": len(
                {
                    version["issued_forecast_id"]
                    for sample in samples
                    for version in sample["provenance"]["versions"]
                }
            ),
            "observation_stations": sorted({s["observation"]["station_id"] for s in samples}),
        },
        "lead_buckets": lead_buckets,
        "time_of_day": _time_of_day(samples, display_timezone),
        "stations": _stations(samples),
        "regime_readiness": _regime_readiness(samples),
        "correction_readiness": _correction_readiness(samples, lead_buckets),
        "samples": samples,
    }
