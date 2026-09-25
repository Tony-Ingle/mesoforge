"""Canonical hourly QPF samples and descriptive analysis of immutable verification facts.

This module neither chooses an observation revision nor changes a forecast. Numerical
baseline and final-stage facts always remain separate analytical populations.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import jcs

from mesoforge.common.identifiers import Digest
from mesoforge.verification.metrics import _compute_scalar_metrics
from mesoforge.verification.model_comparison import LEAD_BUCKETS, lead_bucket

SCHEMA_VERSION = "mesoforge.qpf-verification-analysis.v1"
CANONICALIZATION_POLICY = {
    "id": "mesoforge.qpf-canonicalization.v1",
    "opportunity": (
        "issued version, stage, coordinate, exact interval and field/verification policy"
    ),
    "sample": "coordinate, reference decision time, stage and exact hourly interval",
    "duplicates": "Identical native message/cell/quality evidence collapses despite rereceipt.",
    "revisions": "Conflicting retained observation or forecast evidence is ambiguous; none wins.",
    "reissues": (
        "One explicit primary is canonical. Identical versions collapse; materially different "
        "explicit reissues remain alternates. Legacy differing versions are ambiguous."
    ),
    "stages": "Never pool numerical baseline and final or other stages into one metric population.",
}
_FACT_SCHEMA = "issued-qpf-verification.v1"
_POLICY = "qpf-mrms-verification.v1"
_FIELD = "liquid_equivalent_precipitation_amount_1h"


def _instant(value: Any) -> datetime:
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Timestamp must have a timezone")
    return instant.astimezone(UTC)


def _iso(value: Any) -> str:
    return _instant(value).isoformat().replace("+00:00", "Z")


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _signature(value: Any) -> bytes:
    return bytes(jcs.canonicalize(value))


def _interval(fact: Mapping[str, Any]) -> tuple[str, str]:
    return _iso(fact["interval_start"]), _iso(fact["interval_end"])


def _sample_key(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact["latitude"],
        fact["longitude"],
        _iso(fact["target_reference_time"]),
        fact["stage"],
        *_interval(fact),
    )


def _opportunity_key(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fact["issued_forecast_id"],
        *_sample_key(fact),
        fact["verification_policy_id"],
        _signature(fact["forecast"]["policy"]),
    )


def _identity_reason(fact: Mapping[str, Any]) -> str | None:
    if fact.get("integrity_exclusion"):
        return str(fact["integrity_exclusion"])
    if fact.get("schema_version") != _FACT_SCHEMA or fact.get("field") != _FIELD:
        return "unsupported_fact_contract"
    if fact.get("verification_policy_id") != _POLICY:
        return "unsupported_verification_policy"
    try:
        for name in ("artifact_id", "issued_forecast_id", "issued_forecast_digest", "stage"):
            if not isinstance(fact[name], str) or not fact[name]:
                return "incomplete_fact_identity"
        _instant(fact["issued_at"])
        _instant(fact["registered_at"])
        if not _finite(fact["latitude"]) or not -90 <= fact["latitude"] <= 90:
            return "invalid_coordinate"
        if not _finite(fact["longitude"]) or not -180 <= fact["longitude"] <= 180:
            return "invalid_coordinate"
        _opportunity_key(fact)
    except (KeyError, TypeError, ValueError, OverflowError):
        return "incomplete_fact_identity"
    return None


def _matched_reason(fact: Mapping[str, Any]) -> str | None:
    if fact.get("quality_state") == "invalid":
        return "invalid_quality_state"
    if fact.get("status") != "verified":
        return str(next(iter(fact.get("reasons") or []), "not_verified"))
    try:
        start, end = _interval(fact)
        if (
            (_instant(end) - _instant(start)).total_seconds() != 3600
            or fact["duration_seconds"] != 3600
            or fact["interval_closure"] != "left_open_right_closed"
        ):
            return "incompatible_forecast_interval"
        horizon = fact["lead_hours"]
        if not _finite(horizon) or horizon != int(horizon):
            return "invalid_forecast_lead"
        lead_bucket(int(horizon))
        if (
            _instant(end) - _instant(fact["target_reference_time"])
        ).total_seconds() != horizon * 3600:
            return "inconsistent_forecast_lead"
        observation = fact["observation"]
        if (_iso(observation["interval_start"]), _iso(observation["interval_end"])) != (start, end):
            return "incompatible_observation_interval"
        values = (fact["forecast"]["amount_mm"], observation["amount_mm"], fact["qpf_error_mm"])
        if not all(_finite(value) for value in values) or values[0] < 0 or values[1] < 0:
            return "invalid_amount_or_error"
        if observation["state"] not in ("zero", "positive"):
            return "observation_not_numeric"
        if (values[1] == 0) != (observation["state"] == "zero"):
            return "inconsistent_observation_state"
        if values[0] - values[1] != values[2]:
            return "saved_error_disagrees_with_values"
        if not _observation_signature(fact):
            return "observation_revision_unavailable"
    except (KeyError, TypeError, ValueError, OverflowError):
        return "incomplete_matched_fact"
    return None


def _observation_signature(fact: Mapping[str, Any]) -> bytes | None:
    observation = fact.get("observation")
    if not isinstance(observation, Mapping):
        return None
    revision = observation.get("semantic_revision_digest") or observation.get("message_sha256")
    if not revision:
        return None
    # Acquisition IDs/timestamps and compression bytes are not meteorological revisions.
    audit_keys = {
        "artifact_id",
        "artifact_reference",
        "acquired_at",
        "available_at",
        "ingested_at",
        "raw_sha256",
        "raw_artifact_id",
        "raw_artifact_reference",
        "source_url",
        "url",
        "retrieved_at",
    }

    def semantic(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: semantic(item) for key, item in value.items() if key not in audit_keys}
        if isinstance(value, list):
            return [semantic(item) for item in value]
        return value

    return _signature(semantic(observation))


def _forecast_signature(fact: Mapping[str, Any]) -> bytes:
    return _signature(
        {
            "forecast": fact["forecast"],
            "contributors": {
                model: {key: value for key, value in row.items() if key != "native_evidence_digest"}
                for model, row in fact.get("contributors", {}).items()
            },
            "verification_policy_id": fact["verification_policy_id"],
        }
    )


def _version_signature(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return _forecast_signature(fact), _observation_signature(fact), fact["qpf_error_mm"]


def _order(fact: Mapping[str, Any]) -> tuple[Any, ...]:
    return _instant(fact["registered_at"]), fact["artifact_id"]


def _excluded(fact: Mapping[str, Any], reason: str) -> dict[str, Any]:
    retained = fact.get("reasons")
    reasons = retained if isinstance(retained, list) else []
    return {
        "artifact_id": fact.get("artifact_id"),
        "issued_forecast_id": fact.get("issued_forecast_id"),
        "stage": fact.get("stage"),
        "reason": reasons[0] if fact.get("status") == "excluded" and reasons else reason,
        "fact_reasons": list(reasons),
        "analysis_exclusion": reason,
    }


def _known_hour(fact: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """A saved-hour attempt can be known even when a historical QPF interval is absent."""
    try:
        if (
            fact.get("field") != _FIELD
            or not fact.get("issued_forecast_id")
            or not fact.get("stage")
            or not _finite(fact.get("latitude"))
            or not _finite(fact.get("longitude"))
        ):
            return None
        return (
            fact["issued_forecast_id"],
            fact["stage"],
            fact["latitude"],
            fact["longitude"],
            _iso(fact.get("valid_time") or fact.get("interval_end")),
            fact.get("verification_policy_id"),
        )
    except (TypeError, ValueError):
        return None


def canonicalize_qpf_facts(facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Facts → issued-hour opportunities → one primary analytical sample per decision."""
    grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    excluded, ambiguous, alternates, samples = [], [], [], []
    seen: dict[str, bytes] = {}
    inconsistent_ids: set[str] = set()
    duplicate_references = 0
    for fact in facts:
        reason = _identity_reason(fact)
        if reason:
            excluded.append(_excluded(fact, reason))
            continue
        identity = str(fact["artifact_id"])
        try:
            digest = _signature(
                {key: value for key, value in fact.items() if key != "registered_at"}
            )
        except (TypeError, ValueError, OverflowError):
            excluded.append(_excluded(fact, "noncanonical_fact_payload"))
            continue
        if identity in seen and seen[identity] == digest:
            duplicate_references += 1
            continue
        if identity in seen:
            inconsistent_ids.add(identity)
        seen[identity] = digest
        grouped[_opportunity_key(fact)].append(fact)

    opportunities: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    tainted: set[tuple[Any, ...]] = set()
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=_order)
        eligible = []
        for row in rows:
            reason = _matched_reason(row)
            if reason:
                excluded.append(_excluded(row, reason))
            else:
                eligible.append(row)
        revisions = {_observation_signature(row) for row in rows} - {None}
        forecast_evidence = {_forecast_signature(row) for row in rows}
        conflicting = (
            any(row["artifact_id"] in inconsistent_ids for row in rows)
            or len(revisions) > 1
            or len(forecast_evidence) > 1
            or len({row["issued_forecast_digest"] for row in rows}) > 1
        )
        if conflicting:
            tainted.add(_sample_key(rows[0]))
            ambiguous.append(
                {
                    "level": "opportunity",
                    "reason": "conflicting_observation_revisions"
                    if len(revisions) > 1
                    else "conflicting_forecast_evidence",
                    "issued_forecast_ids": [rows[0]["issued_forecast_id"]],
                    "fact_ids": [row["artifact_id"] for row in rows],
                    "stage": rows[0]["stage"],
                    "interval_end": rows[0]["interval_end"],
                }
            )
        elif eligible:
            opportunities[_sample_key(rows[0])].append(
                {
                    "fact": eligible[0],
                    "fact_ids": [row["artifact_id"] for row in eligible],
                }
            )

    for key in sorted(opportunities):
        members = sorted(
            opportunities[key],
            key=lambda item: (
                _instant(item["fact"]["issued_at"]),
                item["fact"]["issued_forecast_id"],
            ),
        )
        facts_for_sample = [item["fact"] for item in members]
        primary = [item for item in members if item["fact"].get("issuance_mode") == "primary"]
        differing = len({_version_signature(fact) for fact in facts_for_sample}) > 1
        reason = "member_opportunity_ambiguous" if key in tainted else None
        if len({_observation_signature(fact) for fact in facts_for_sample}) > 1:
            reason = reason or "conflicting_observation_revisions"
        if differing and reason is None:
            if len(primary) != 1 or any(
                item not in primary and item["fact"].get("issuance_mode") != "explicit_reissue"
                for item in members
            ):
                reason = "same_decision_versions_ambiguous"
        if not primary and all(
            fact.get("issuance_mode") == "explicit_reissue" for fact in facts_for_sample
        ):
            reason = reason or "primary_forecast_not_in_selection"
        if reason:
            ambiguous.append(
                {
                    "level": "sample",
                    "reason": reason,
                    "stage": key[3],
                    "interval_end": key[-1],
                    "issued_forecast_ids": [
                        fact["issued_forecast_id"] for fact in facts_for_sample
                    ],
                    "fact_ids": [identity for item in members for identity in item["fact_ids"]],
                }
            )
            continue
        # Known explicit reissues cannot become primary simply by sorting before a
        # metadata-less historical version. Identical legacy versions may still collapse.
        canonical = (
            primary[0]
            if primary
            else next(
                item for item in members if item["fact"].get("issuance_mode") != "explicit_reissue"
            )
        )
        fact = canonical["fact"]
        equal = [
            item for item in members if _version_signature(item["fact"]) == _version_signature(fact)
        ]
        for item in members:
            if item not in equal:
                alternates.append(
                    {
                        "reason": "materially_different_explicit_reissue",
                        "stage": fact["stage"],
                        "primary_issued_forecast_id": fact["issued_forecast_id"],
                        "issued_forecast_id": item["fact"]["issued_forecast_id"],
                        "fact_ids": item["fact_ids"],
                        "interval_end": fact["interval_end"],
                    }
                )
        samples.append(
            {
                **dict(fact),
                "sample_id": str(Digest.of_bytes(_signature(list(key)))),
                "canonical_fact_id": fact["artifact_id"],
                "canonical_issued_forecast_id": fact["issued_forecast_id"],
                "lead_bucket": lead_bucket(int(fact["lead_hours"])),
                "provenance": {
                    "versions": [
                        {
                            "issued_forecast_id": item["fact"]["issued_forecast_id"],
                            "fact_ids": item["fact_ids"],
                        }
                        for item in equal
                    ]
                },
            }
        )
    return {
        "policy": CANONICALIZATION_POLICY,
        "input_fact_references": len(facts),
        "stored_facts": len(
            {row["artifact_id"] for row in facts if isinstance(row.get("artifact_id"), str)}
        ),
        "duplicate_fact_references": duplicate_references,
        "opportunities": len(grouped),
        "opportunity_basis": "Exact retained forecast interval and field/verification policy.",
        "known_issued_hour_opportunities": len({_known_hour(row) for row in facts} - {None}),
        "known_hour_basis": (
            "Issued version, stage, coordinate and saved valid time, including historical "
            "hours whose QPF interval/policy is unavailable; no bounds are invented."
        ),
        "canonical_samples": len(samples),
        "excluded_facts": excluded,
        "excluded_by_reason": dict(sorted(Counter(row["reason"] for row in excluded).items())),
        "ambiguous": ambiguous,
        "alternate_reissues": alternates,
        "samples": samples,
    }


def _metrics(samples: Sequence[Mapping[str, Any]], model: str | None = None) -> dict[str, Any]:
    predicted = [
        float(
            row["forecast"]["amount_mm"]
            if model is None
            else row["contributors"][model]["amount_mm"]
        )
        for row in samples
    ]
    observed = [float(row["observation"]["amount_mm"]) for row in samples]
    bias, mae, rmse = _compute_scalar_metrics(
        [forecast - observation for forecast, observation in zip(predicted, observed, strict=True)]
    )
    return {
        "n": len(samples),
        "mean_error_mm": bias,
        "bias_mm": bias,
        "mae_mm": mae,
        "rmse_mm": rmse,
        "forecast_total_mm": sum(predicted),
        "observed_total_mm": sum(observed),
        "numeric_occurrence_counts": dict(
            sorted(
                Counter(
                    f"forecast_{'positive' if f > 0 else 'zero'}_"
                    f"observed_{'positive' if o > 0 else 'zero'}"
                    for f, o in zip(predicted, observed, strict=True)
                ).items()
            )
        ),
        "occurrence_definition": (
            "Numeric positive (>0 mm) versus zero; no measurable-precip threshold "
            "or categorical skill claim."
        ),
    }


def _compatible(sample: Mapping[str, Any], model: str) -> bool:
    row = sample.get("contributors", {}).get(model, {})
    try:
        return (
            row.get("compatible") is True
            and not row.get("reasons")
            and _finite(row.get("amount_mm"))
            and row["amount_mm"] >= 0
            and (_iso(row["interval_start"]), _iso(row["interval_end"])) == _interval(sample)
        )
    except (KeyError, TypeError, ValueError):
        return False


def _concentration(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dimensions: dict[str, Callable[[Mapping[str, Any]], str]] = {
        "decision_date_utc": lambda row: _instant(row["target_reference_time"]).date().isoformat(),
        "issuance_date_utc": lambda row: _instant(row["issued_at"]).date().isoformat(),
        "valid_date_utc": lambda row: _instant(row["interval_end"]).date().isoformat(),
        "location": _location,
        "observation_product": lambda row: str(row["observation"]["product"]),
        "lead_hours": lambda row: _signature(row["lead_hours"]).decode("ascii"),
        "native_analysis_event": lambda row: (
            f"{row['observation']['product']}:{row['interval_start']}:{row['interval_end']}"
        ),
    }
    result: dict[str, Any] = {}
    for name, getter in dimensions.items():
        counts = Counter(getter(row) for row in samples)
        result[name] = {
            "distinct": len(counts),
            "counts": dict(sorted(counts.items())),
            "largest_share": max(counts.values()) / len(samples) if samples else None,
        }
    result["episode_note"] = (
        "No storm classification or independence claim. Consecutive hours and shared "
        "analysis events are correlated evidence."
    )
    return result


def _location(row: Mapping[str, Any]) -> str:
    # JSON storage canonicalizes 45.0 to 45; equivalent coordinates must keep one group.
    return ",".join(_signature(row[key]).decode("ascii") for key in ("latitude", "longitude"))


def _distribution(numbers: Sequence[float]) -> dict[str, Any]:
    """Compact empirical summaries, without rainfall/quality classification thresholds."""
    ordered = sorted(numbers)
    return {
        "numeric_count": len(ordered),
        "minimum": ordered[0] if ordered else None,
        "maximum": ordered[-1] if ordered else None,
        "mean": math.fsum(ordered) / len(ordered) if ordered else None,
        "quantiles": {
            str(percentile): ordered[max(0, math.ceil(percentile * len(ordered) / 100) - 1)]
            if ordered
            else None
            for percentile in (25, 50, 75, 90)
        },
        "quantile_method": "empirical nearest rank; descriptive, not category thresholds",
    }


def _quality_summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    products = sorted(
        {"GaugeInflIndex_01H_Pass2", "RadarAccumulationQualityIndex_01H"}
        | {product for row in samples for product in row["observation"].get("quality_support", {})}
    )
    summary = {}
    for product in products:
        values = [
            row["observation"].get("quality_support", {}).get(product, {}).get("value") or {}
            for row in samples
        ]
        numbers = [
            value["value"]
            for value in values
            if value.get("state") in ("positive", "zero") and _finite(value.get("value"))
        ]
        summary[product] = {
            "states": dict(
                sorted(Counter(value.get("state", "unavailable") for value in values).items())
            ),
            **_distribution(numbers),
            "units": sorted({value["units"] for value in values if value.get("units")}),
            "population": "canonical samples; repeated native analyses are not independent",
            "threshold_policy": "none; retained support evidence is not calibrated confidence",
        }
    return summary


def _readiness(
    samples: Sequence[Mapping[str, Any]],
    concentration: Mapping[str, Any],
    comparisons: Mapping[str, Any],
) -> dict[str, Any]:
    """Report evidence coverage, never authorize an experiment or infer model skill."""
    observed = [float(row["observation"]["amount_mm"]) for row in samples]
    positive = sum(value > 0 for value in observed)
    zero = len(observed) - positive
    groups = {
        bucket: sum(row["lead_bucket"] == bucket for row in samples) for bucket in LEAD_BUCKETS
    }
    coverage = {
        model: {
            "compatible_samples": len(comparison["sample_ids"]),
            "incompatible_or_missing_samples": comparison["excluded_incompatible_or_missing"],
            "fraction_of_canonical_samples": len(comparison["sample_ids"]) / len(samples)
            if samples
            else None,
        }
        for model, comparison in comparisons["pairwise"].items()
    }
    gaps: list[dict[str, Any]] = []
    if not samples:
        gaps.append({"reason": "no_canonical_samples"})
    else:
        for dimension in ("location", "decision_date_utc", "valid_date_utc"):
            if concentration[dimension]["distinct"] == 1:
                gaps.append({"reason": "single_" + dimension})
        if not positive:
            gaps.append({"reason": "no_positive_observed_amounts"})
        if not zero:
            gaps.append({"reason": "no_zero_observed_amounts"})
        for bucket, count in groups.items():
            if not count:
                gaps.append({"reason": "no_samples_in_provisional_lead_group", "group": bucket})
        for model, item in coverage.items():
            if item["incompatible_or_missing_samples"]:
                gaps.append({"reason": "incomplete_contributor_coverage", "contributor": model})
    return {
        "canonical_samples": len(samples),
        "positive_observed_qpf_samples": positive,
        "zero_observed_qpf_samples": zero,
        "occurrence_definition": "Positive means >0 mm, not an approved measurable threshold.",
        "measurable_sample_count": None,
        "measurable_count_unavailable_reason": "No measurable-precipitation threshold is approved.",
        "configured_locations": concentration["location"],
        "decision_dates_utc": concentration["decision_date_utc"],
        "valid_dates_utc": concentration["valid_date_utc"],
        "exact_lead_hours": concentration["lead_hours"],
        "provisional_lead_groups": groups,
        "observed_qpf_mm": _distribution(observed),
        "contributor_coverage": coverage,
        "shared_sample_comparison": {
            "contributors": comparisons["joint"]["contributors"],
            "canonical_samples": len(comparisons["joint"]["sample_ids"]),
            "metrics_reference": "contributor_comparison.joint",
        },
        "time_concentration": {
            "first_interval_start": min(
                (_iso(row["interval_start"]) for row in samples), default=None
            ),
            "last_interval_end": max((_iso(row["interval_end"]) for row in samples), default=None),
            "native_analysis_events": concentration["native_analysis_event"],
            "positive_observed_valid_dates_utc": dict(
                sorted(
                    Counter(
                        _instant(row["interval_end"]).date().isoformat()
                        for row in samples
                        if row["observation"]["amount_mm"] > 0
                    ).items()
                )
            ),
            "episode_classification": "not implemented; date/time concentration only",
            "independence_claim": False,
        },
        "heavy_rain_assessment": "not classified; no heavy-rain threshold is approved",
        "factual_gaps": gaps,
        "experiment_readiness": "requires scientific/owner review; no automatic threshold",
        "authorizes_weight_changes": False,
    }


def analyze_qpf_facts(
    facts: Sequence[Mapping[str, Any]], *, contributors: Sequence[str] | None = None
) -> dict[str, Any]:
    """Read-only amount comparisons; model metrics always identify their shared sample cohort."""
    canonical = canonicalize_qpf_facts(facts)
    samples = canonical["samples"]
    models = sorted(
        set(contributors)
        if contributors is not None
        else {model for sample in samples for model in sample.get("contributors", {})}
    )
    stages = {}
    for stage in sorted({row["stage"] for row in facts if isinstance(row.get("stage"), str)}):
        selected = [row for row in samples if row["stage"] == stage]
        pairwise = {}
        for model in models:
            cohort = [row for row in selected if _compatible(row, model)]
            pairwise[model] = {
                "sample_ids": [row["sample_id"] for row in cohort],
                "excluded_incompatible_or_missing": len(selected) - len(cohort),
                "mesoforge": _metrics(cohort),
                "contributor": _metrics(cohort, model),
            }
        joint = [row for row in selected if all(_compatible(row, model) for model in models)]
        comparisons = {
            "pairwise": pairwise,
            "joint": {
                "contributors": models,
                "sample_ids": [row["sample_id"] for row in joint],
                "mesoforge": _metrics(joint),
                "models": {model: _metrics(joint, model) for model in models},
            },
        }
        concentration = _concentration(selected)
        stages[stage] = {
            "overall": _metrics(selected),
            "by_exact_lead_hours": {
                _signature(lead).decode("ascii"): _metrics(
                    [row for row in selected if row["lead_hours"] == lead]
                )
                for lead in sorted({row["lead_hours"] for row in selected})
            },
            "by_lead_bucket": {
                bucket: _metrics([row for row in selected if row["lead_bucket"] == bucket])
                for bucket in LEAD_BUCKETS
            },
            "by_location": {
                location: _metrics([row for row in selected if _location(row) == location])
                for location in sorted({_location(row) for row in selected})
            },
            "concentration": concentration,
            "quality_support": _quality_summary(selected),
            "contributor_comparison": comparisons,
            "readiness": _readiness(selected, concentration, comparisons),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "field": _FIELD,
        "units": "mm",
        "interpretation": (
            "Descriptive analysis-reference comparisons, not point-gauge truth or model ranking."
        ),
        "lead_bucket_policy": (
            "1–6 / 7–18 / 19–36 h are provisional analysis organization, "
            "not established QPF skill regimes."
        ),
        "canonicalization": canonical,
        "stages": stages,
        "changes_to_forecasts_or_weights": False,
    }
