"""Read-only, identical-observation comparisons of immutable forecast stage overlays.

Field-specific canonicalizers remain authoritative. This projection attaches saved
variant predictions to their canonical control events; it creates no verification
facts, forecasts, learned policy or promotion decision.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mesoforge.common.identifiers import Digest
from mesoforge.contracts.forecast_variants import NO_EVIDENCE_STATES
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.verification.metrics import _compute_scalar_metrics
from mesoforge.verification.model_comparison import analytical_lead_bucket, analytical_lead_buckets
from mesoforge.verification.qpf_analysis import _observation_signature

SCHEMA_VERSION = "mesoforge.variant-evaluation.v1"
TEMPERATURE = "air_temperature_2m"
QPF = "liquid_equivalent_precipitation_amount_1h"


def _time(value: Any) -> datetime:
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Evaluation identity requires timezone-aware timestamps")
    return instant.astimezone(UTC)


def _iso(value: Any) -> str:
    return _time(value).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return str(Digest.of_bytes(canonical_json_bytes(value)))


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _control(field: str, sample: Mapping[str, Any]) -> dict[str, Any]:
    observation = sample["observation"]
    valid = _iso(sample["valid_time"] if field == TEMPERATURE else sample["interval_end"])
    reference = _iso(sample["target_reference_time"])
    lead = int(sample["horizon_hours"] if field == TEMPERATURE else sample["lead_hours"])
    if (_time(valid) - _time(reference)).total_seconds() != lead * 3600:
        raise ValueError("Canonical sample has inconsistent lead")
    event = {
        "field": field,
        "latitude": sample["latitude"],
        "longitude": sample["longitude"],
        "reference_time": reference,
        "valid_time": valid,
        "verification_policy_id": sample["verification_policy_id"],
        "control_stage": sample.get("stage", "final_issued"),
    }
    if field == QPF:
        start = _iso(sample["interval_start"])
        if (
            (_time(valid) - _time(start)).total_seconds() != 3600
            or sample["interval_closure"] != "left_open_right_closed"
            or (_iso(observation["interval_start"]), _iso(observation["interval_end"]))
            != (start, valid)
        ):
            raise ValueError("QPF comparison requires the exact same (start,end] hourly event")
        event.update(interval_start=start, interval_end=valid, interval_closure="(start,end]")
        control, observed = sample["forecast"]["amount_mm"], observation["amount_mm"]
        signature = _observation_signature(sample)
        if signature is None:
            raise ValueError("MRMS revision identity unavailable")
        observation_id = str(Digest.of_bytes(signature))
        revision = observation.get("semantic_revision_digest") or observation.get("message_sha256")
        source = observation["product"]
    else:
        control, observed = sample["forecast_temperature_k"], observation["temperature_k"]
        revision = observation["revision_digest"]
        observation_id = _digest(
            {
                key: observation.get(key)
                for key in (
                    "station_id",
                    "network",
                    "provider",
                    "observation_time",
                    "revision_digest",
                    "logical_observation_digest",
                    "temperature_k",
                )
            }
        )
        source = f"{observation.get('network')}:{observation['station_id']}"
    if (
        not _finite(control)
        or not _finite(observed)
        or (field == QPF and min(control, observed) < 0)
    ):
        raise ValueError("Canonical sample requires finite compatible amounts")
    versions = {sample["canonical_issued_forecast_id"]}
    versions.update(row["issued_forecast_id"] for row in sample["provenance"]["versions"])
    return {
        **event,
        "event_id": _digest(event),
        "sample_id": _digest({**event, "observation_identity": observation_id}),
        "control_issued_forecast_ids": sorted(versions),
        "canonical_fact_id": sample["canonical_fact_id"],
        "control_value": float(control),
        "observed_value": float(observed),
        "observation_identity": observation_id,
        "observation_revision": revision,
        "observation_source": source,
        "observation_available_at": observation.get("available_at"),
        "quality_support": observation.get("quality_support", {}),
        "baseline_snapshot": sample.get("baseline_snapshot", {}),
        "unit": "K" if field == TEMPERATURE else "mm",
        "lead_hours": lead,
        "lead_bucket": analytical_lead_bucket(lead),
    }


def _stage_reason(stage: Mapping[str, Any], field: str) -> str | None:
    if stage.get("evaluation_exclusion"):
        return str(stage["evaluation_exclusion"])
    try:
        if field not in stage["fields"]:
            return "variant_field_not_supported"
        baseline_root = (
            stage.get("transformation_type") == "active_baseline"
            and stage.get("parent_stage_id") is None
            and stage.get("evidence_required") is False
            and stage.get("evidence_status") == "baseline"
        )
        for key in (
            "variant_id",
            "baseline_snapshot_id",
            "prepared_snapshot_id",
        ):
            if not isinstance(stage[key], str) or not stage[key]:
                return "variant_lineage_unavailable"
        if not baseline_root and (
            not isinstance(stage.get("parent_stage_id"), str) or not stage["parent_stage_id"]
        ):
            return "variant_lineage_unavailable"
        cutoff = _time(stage["analysis_cutoff"])
        _time(stage["created_at"])
        no_evidence = (
            stage.get("evidence_required") is False
            and stage.get("evidence_status") in NO_EVIDENCE_STATES
            and stage.get("overlay", {}).get("inherit_unchanged") is True
            and not stage.get("overlay", {}).get("predictions")
        )
        runtime_ai = (
            stage.get("transformation_type") == "ai_adjusted"
            and stage.get("evidence_basis") == "pinned_forecast_evidence"
        )
        if runtime_ai:
            pinned = stage.get("pinned_evidence", {})
            if (
                _time(stage["evidence_cutoff"]) != cutoff
                or pinned.get("corrected_stage_id") != stage["parent_stage_id"]
                or any(
                    pinned.get(k) != stage[k]
                    for k in ("baseline_snapshot_id", "prepared_snapshot_id", "analysis_cutoff")
                )
                or stage.get("validation", {}).get("status") != "valid"
            ):
                return "variant_pinned_evidence_unproven"
        if not no_evidence and not baseline_root and not runtime_ai:
            for key in ("policy_created_at", "policy_activated_at"):
                if _time(stage[key]) > cutoff:
                    return f"variant_{key}_after_analysis_cutoff"
            if _time(stage["evidence_cutoff"]) > cutoff:
                return "future_learning_evidence"
        _time(stage["reference_time"])
        if not stage.get("control_issued_forecast_ids"):
            return "control_issuance_lineage_unavailable"
    except (KeyError, TypeError, ValueError):
        return "variant_timing_or_identity_unproven"
    return None


def _matches(stage: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    try:
        return (
            stage["location"]["latitude"] == row["latitude"]
            and stage["location"]["longitude"] == row["longitude"]
            and _iso(stage["reference_time"]) == row["reference_time"]
            and bool(
                set(stage["control_issued_forecast_ids"]) & set(row["control_issued_forecast_ids"])
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _prediction(
    stage: Mapping[str, Any],
    row: Mapping[str, Any],
    parents: Mapping[str, Mapping[str, Any]] | None = None,
    visited: frozenset[str] = frozenset(),
) -> tuple[float | None, str | None]:
    identifier = str(stage.get("variant_id"))
    if identifier in visited:
        return None, "variant_parent_cycle"
    visited = visited | {identifier}
    reason = _stage_reason(stage, str(row["field"]))
    if reason:
        return None, reason
    baseline = row["baseline_snapshot"]
    if any(
        baseline.get(key) is not None and baseline[key] != stage[key]
        for key in ("baseline_snapshot_id", "prepared_snapshot_id")
    ):
        return None, "baseline_lineage_mismatch"
    overlay = stage.get("overlay", {})
    candidates = []
    try:
        for prediction in overlay.get("predictions", []):
            if prediction.get("field", row["field"]) != row["field"]:
                continue
            if _iso(prediction["valid_time"]) == row["valid_time"]:
                candidates.append(prediction)
    except (KeyError, TypeError, ValueError):
        return None, "malformed_variant_prediction"
    if not candidates:
        # A raw baseline is a distinct stage from a potentially corrected issuance.
        # Its saved numerical values must be explicit; inheriting would mislabel
        # an operational correction as the original raw numerical forecast.
        if stage.get("transformation_type") == "active_baseline":
            return None, "saved_baseline_prediction_unavailable"
        if overlay.get("inherit_unchanged") is True:
            if overlay.get("inheritance_basis") == "parent_stage":
                parent = (parents or {}).get(stage.get("parent_stage_id", ""))
                if parent is None or not _matches(parent, row):
                    return None, "variant_parent_prediction_unavailable"
                return _prediction(parent, row, parents, visited)
            return float(row["control_value"]), None
        return None, "variant_prediction_unavailable"
    if len({_digest(value) for value in candidates}) != 1:
        return None, "conflicting_variant_predictions"
    prediction = candidates[0]
    if row["field"] == QPF:
        try:
            if (_iso(prediction["interval_start"]), _iso(prediction["interval_end"])) != (
                row["interval_start"],
                row["interval_end"],
            ):
                return None, "incompatible_variant_interval"
        except (KeyError, TypeError, ValueError):
            return None, "incompatible_variant_interval"
    for key in ("observation_identity", "observation_revision"):
        if key in prediction and prediction[key] != row[key]:
            return None, "different_observation_population"
    if prediction.get("unit") != row["unit"]:
        return None, "incompatible_variant_units"
    value = prediction.get("value")
    status = prediction.get("status")
    # Status-bearing rows follow the issued-field contract: "fallback" is a complete
    # value with explanatory notes. Rows without status keep the missing-reason rule.
    unusable = (
        status not in ("available", "fallback")
        if status is not None
        else bool(prediction.get("missing_reasons"))
    )
    if not _finite(value) or unusable or (row["field"] == QPF and value < 0):
        return None, "variant_prediction_unavailable"
    return float(value), None


def _metrics(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = [float(row[key]) for row in rows]
    observations = [float(row["observed_value"]) for row in rows]
    bias, mae, rmse = _compute_scalar_metrics(
        [v - o for v, o in zip(values, observations, strict=True)]
    )
    result: dict[str, Any] = {"sample_count": len(rows), "bias": bias, "mae": mae, "rmse": rmse}
    if rows and rows[0]["field"] == QPF:
        result.update(
            forecast_total_mm=sum(values),
            observed_total_mm=sum(observations),
            observed_zero_count=observations.count(0),
            observed_positive_count=sum(value > 0 for value in observations),
        )
    return result


def _comparison(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    control, variant = _metrics(rows, "control_value"), _metrics(rows, key)
    return {
        "control": control,
        "variant": variant,
        "metric_deltas_variant_minus_control": {
            metric: variant[metric] - control[metric] if rows else None
            for metric in ("bias", "mae", "rmse")
        },
    }


def _controls(
    field: str, control_analysis: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Canonical control events; ambiguous or invalid evidence is excluded explicitly."""
    raw_samples = (
        control_analysis.get("samples", [])
        if field == TEMPERATURE
        else control_analysis.get("canonicalization", {}).get("samples", [])
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exclusions: list[dict[str, Any]] = []
    for sample in raw_samples:
        try:
            row = _control(field, sample)
            grouped[row["event_id"]].append(row)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            exclusions.append({"reason": "invalid_canonical_control", "detail": str(exc)})
    controls = []
    for event, rows in sorted(grouped.items()):
        signatures = {(r["sample_id"], r["control_value"]) for r in rows}
        if len(signatures) != 1:
            exclusions.append({"event_id": event, "reason": "ambiguous_control_evidence"})
        else:
            controls.append(rows[0])
    return controls, exclusions


def evaluate_variants(
    field: str,
    control_analysis: Mapping[str, Any],
    variants: Sequence[Mapping[str, Any]],
    *,
    ancestor_stages: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Compare policy series on one common cohort of canonical control observations.

    A policy series may contain immutable stages for multiple decisions/locations.
    Values are inherited only with an explicit sparse-overlay declaration. Callers
    must resolve control issuance IDs from authoritative immutable lineage. No
    forecast or observation is recalculated here, including when a stage is AI-named.
    """
    if field not in (TEMPERATURE, QPF):
        raise ValueError("Only temperature and exact hourly QPF have metric contracts")
    controls, exclusions = _controls(field, control_analysis)
    series: dict[str, dict[str, Any]] = {}
    identities: dict[str, set[str]] = defaultdict(set)
    policy_identities: dict[tuple[str, str], set[str]] = defaultdict(set)
    for stage in variants:
        try:
            policy = stage["policy"]
            identities[stage["variant_id"]].add(
                _digest(
                    {
                        key: value
                        for key, value in stage.items()
                        if key != "control_issued_forecast_ids"
                    }
                )
            )
            policy_identities[policy["id"], policy["version"]].add(_digest(policy))
            definition = {
                "policy_id": policy["id"],
                "policy_version": policy["version"],
                "policy_digest": policy.get("digest"),
                "transformation_type": stage["transformation_type"],
                "lifecycle_role": stage["lifecycle_role"],
            }
            identifier = _digest(definition)
            series.setdefault(identifier, {**definition, "stages": []})["stages"].append(stage)
        except (KeyError, TypeError, ValueError):
            exclusions.append(
                {"variant_id": stage.get("variant_id"), "reason": "invalid_policy_identity"}
            )
    for definition in series.values():
        definition["stages"] = [
            {
                **stage,
                "evaluation_exclusion": (
                    "conflicting_immutable_stage_identity"
                    if len(identities[stage["variant_id"]]) > 1
                    else "conflicting_immutable_policy_identity"
                    if len(policy_identities[stage["policy"]["id"], stage["policy"]["version"]]) > 1
                    else None
                ),
            }
            for stage in definition["stages"]
        ]
    parents = {str(stage.get("variant_id")): stage for stage in ancestor_stages} | {
        str(stage.get("variant_id")): stage
        for definition in series.values()
        for stage in definition["stages"]
    }
    paired = []
    for row in controls:
        candidate = dict(row)
        candidate["variant_stage_ids"] = {}
        complete = bool(series)
        for identifier, definition in sorted(series.items()):
            stages = [stage for stage in definition["stages"] if _matches(stage, row)]
            predictions = [_prediction(stage, row, parents) for stage in stages]
            reasons = sorted({reason for _, reason in predictions if reason})
            values = {value for value, reason in predictions if reason is None}
            reason = (
                "variant_not_bound_to_control_event"
                if not stages
                else ";".join(reasons)
                if reasons
                else "conflicting_variant_stages"
                if len(values) != 1
                else None
            )
            if reason:
                complete = False
                exclusions.append(
                    {"sample_id": row["sample_id"], "series_id": identifier, "reason": reason}
                )
            else:
                candidate[identifier] = next(iter(values))
                candidate["variant_stage_ids"][identifier] = sorted(
                    {s["variant_id"] for s in stages}
                )
        if complete:
            paired.append(candidate)
    comparisons = {}
    for identifier, definition in sorted(series.items()):
        comparisons[identifier] = {
            **{key: value for key, value in definition.items() if key != "stages"},
            "variant_ids": sorted({stage["variant_id"] for stage in definition["stages"]}),
            "evidence_cutoffs": sorted(
                {str(stage.get("evidence_cutoff")) for stage in definition["stages"]}
            ),
            **_comparison(paired, identifier),
            "by_lead_bucket": {
                bucket: _comparison([r for r in paired if r["lead_bucket"] == bucket], identifier)
                for bucket in analytical_lead_buckets(r["lead_hours"] for r in paired)
            },
            "by_location": {
                location: _comparison([r for r in paired if _location(r) == location], identifier)
                for location in sorted({_location(r) for r in paired})
            },
            "by_exact_lead_hours": {
                str(lead): _comparison([r for r in paired if r["lead_hours"] == lead], identifier)
                for lead in sorted({r["lead_hours"] for r in paired})
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "field": field,
        "unit": "K" if field == TEMPERATURE else "mm",
        "canonical_control_samples": len(controls),
        "control_identity": "canonical_issued_forecast_stage",
        "shared_sample_count": len(paired),
        "shared_sample_ids": [row["sample_id"] for row in paired],
        "comparisons": comparisons,
        "samples": paired,
        "exclusions": exclusions,
        "exclusion_counts": dict(sorted(Counter(row["reason"] for row in exclusions).items())),
        "control_canonicalization": {
            key: value
            for key, value in control_analysis.get("canonicalization", {}).items()
            if key != "samples"
        },
        "concentration": {
            name: dict(sorted(Counter(getter(row) for row in paired).items()))
            for name, getter in (
                ("decision_date_utc", lambda row: row["reference_time"][:10]),
                ("valid_date_utc", lambda row: row["valid_time"][:10]),
                ("location", _location),
                ("observation_source", lambda row: row["observation_source"]),
                ("lead_hours", lambda row: str(row["lead_hours"])),
            )
        },
        "interpretation": (
            "Every metric uses the same canonical observation revisions and common sample cohort. "
            "Dates and locations describe concentration, not independent weather episodes. "
            "No winner score, promotion or operational forecast change is produced."
        ),
        "writes": 0,
    }


def _location(row: Mapping[str, Any]) -> str:
    return canonical_json_bytes({key: row[key] for key in ("latitude", "longitude")}).decode(
        "utf-8"
    )


PAIR_SCHEMA = "mesoforge.pairwise-cohort.v1"


def _stage_value(
    stages: Sequence[Mapping[str, Any]],
    row: Mapping[str, Any],
    parents: Mapping[str, Mapping[str, Any]],
    missing: str,
) -> tuple[float | None, str | None]:
    if not stages:
        return None, missing
    predictions = [_prediction(stage, row, parents) for stage in stages]
    reasons = sorted({reason for _, reason in predictions if reason})
    values = {value for value, reason in predictions if reason is None}
    if reasons:
        return None, ";".join(reasons)
    if len(values) != 1:
        return None, "conflicting_variant_stages"
    return next(iter(values)), None


def _pair_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidate = _metrics([{**r, "value": r["candidate_value"]} for r in rows], "value")
    reference = _metrics([{**r, "value": r["reference_value"]} for r in rows], "value")
    return {
        "sample_count": len(rows),
        "candidate": candidate,
        "reference": reference,
        "deltas_candidate_minus_reference": {
            metric: candidate[metric] - reference[metric] if rows else None
            for metric in ("bias", "mae", "rmse")
        },
    }


def paired_date_intervals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Date-clustered 95% intervals of paired |e| and e^2 differences; reporting only."""
    from mesoforge.verification.site_analysis import _mean_bias_uncertainty

    absolute: dict[str, list[float]] = defaultdict(list)
    squared: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        date = row["reference_time"][:10]
        candidate = row["candidate_value"] - row["observed_value"]
        reference = row["reference_value"] - row["observed_value"]
        absolute[date].append(abs(candidate) - abs(reference))
        squared[date].append(candidate * candidate - reference * reference)
    return {
        "status": "reported_only_not_a_gate",
        "method": "decision_date_means_student_t_95 (same method as the bias evidence policy)",
        "absolute_error_difference": _mean_bias_uncertainty(absolute),
        "squared_error_difference": _mean_bias_uncertainty(squared),
    }


def evaluate_pair(
    field: str,
    control_analysis: Mapping[str, Any],
    candidate_stages: Sequence[Mapping[str, Any]],
    reference_stages: Sequence[Mapping[str, Any]] | None,
    *,
    ancestors: Sequence[Mapping[str, Any]] = (),
    decision_window: tuple[datetime, datetime] | None = None,
    in_sample_until: datetime | None = None,
    excluded_decision_intervals: Sequence[tuple[datetime, datetime | None, str]] = (),
    lead_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Exactly two series on their own identical-sample cohort.

    ``reference_stages`` is the comparison series (for example the raw CONTROL stages);
    ``None`` compares each candidate stage with its own exact parent stage (the AI desk
    against its deterministic corrected parent). Other bound stages are ancestors only,
    so a sparse unrelated series never shrinks this cohort. Metrics are candidate minus
    reference on the same rows, never relative to the issued value. Decision times come
    from the retained stages. Rows outside the decision window or the candidate's lead
    scope are reported separately and are not exclusions.
    """
    if field not in (TEMPERATURE, QPF):
        raise ValueError("Only temperature and exact hourly QPF have metric contracts")
    controls, control_exclusions = _controls(field, control_analysis)
    parents = {str(stage.get("variant_id")): stage for stage in ancestors}
    for stage in (*candidate_stages, *(reference_stages or ())):
        parents[str(stage.get("variant_id"))] = stage
    window = (
        (_time(decision_window[0]), _time(decision_window[1]))
        if decision_window is not None
        else None
    )
    in_sample = _time(in_sample_until) if in_sample_until is not None else None
    intervals = [
        (_time(start), _time(end) if end is not None else None, reason)
        for start, end, reason in excluded_decision_intervals
    ]
    common: list[dict[str, Any]] = []
    excluded: dict[str, list[str]] = defaultdict(list)
    outside: list[str] = []
    not_applicable: list[str] = []
    for row in controls:
        sample = row["sample_id"]
        if lead_range is not None and not lead_range[0] <= row["lead_hours"] <= lead_range[1]:
            not_applicable.append(sample)
            continue
        candidates = [stage for stage in candidate_stages if _matches(stage, row)]
        if reference_stages is None:
            references = [
                parents[str(stage.get("parent_stage_id"))]
                for stage in candidates
                if str(stage.get("parent_stage_id")) in parents
            ]
            timed = candidates
        else:
            references = [stage for stage in reference_stages if _matches(stage, row)]
            timed = references
        try:
            decisions = {_time(stage["analysis_cutoff"]) for stage in timed}
        except (KeyError, TypeError, ValueError):
            decisions = set()
        if len(decisions) != 1:
            side = "candidate" if reference_stages is None else "reference"
            excluded[
                f"{side}_not_bound_to_control_event" if not decisions else "ambiguous_decision_time"
            ].append(sample)
            continue
        decision = next(iter(decisions))
        if window is not None and not window[0] <= decision <= window[1]:
            outside.append(sample)
            continue
        interval_reason = next(
            (
                reason
                for start, end, reason in intervals
                if start <= decision and (end is None or decision < end)
            ),
            None,
        )
        if interval_reason is not None:
            excluded[interval_reason].append(sample)
            continue
        if in_sample is not None and _time(row["valid_time"]) <= in_sample:
            excluded["in_sample_valid_time"].append(sample)
            continue
        candidate, reason = _stage_value(
            candidates, row, parents, "candidate_not_bound_to_control_event"
        )
        if reason:
            excluded[f"candidate:{reason}"].append(sample)
            continue
        reference, reason = _stage_value(
            references, row, parents, "reference_not_bound_to_control_event"
        )
        if reason:
            excluded[f"reference:{reason}"].append(sample)
            continue
        common.append(
            {
                **{
                    k: v
                    for k, v in row.items()
                    if k not in ("quality_support", "baseline_snapshot")
                },
                "decision_time": _iso(decision),
                "candidate_value": candidate,
                "reference_value": reference,
                "candidate_stage_ids": sorted({str(s["variant_id"]) for s in candidates}),
                "reference_stage_ids": sorted({str(s["variant_id"]) for s in references}),
            }
        )
    common.sort(key=lambda r: r["sample_id"])
    exclusions = {reason: sorted(ids) for reason, ids in sorted(excluded.items())}
    identity = {
        "field": field,
        "rows": [
            [
                r["sample_id"],
                r["candidate_value"],
                r["reference_value"],
                r["candidate_stage_ids"],
                r["reference_stage_ids"],
            ]
            for r in common
        ],
        "exclusions": exclusions,
        "control_exclusions": sorted(
            str(e.get("event_id", e["reason"])) for e in control_exclusions
        ),
    }
    total = len(common) + sum(len(ids) for ids in exclusions.values())
    if field == QPF:
        wet: dict[str, Any] = {
            "status": "observed_amount_composition",
            "threshold": "none: exact amount greater than zero versus exactly zero",
            "observed_positive_gt_0mm": sum(r["observed_value"] > 0 for r in common),
            "observed_zero": sum(r["observed_value"] == 0 for r in common),
            "candidate_forecast_positive_gt_0mm": sum(r["candidate_value"] > 0 for r in common),
            "reference_forecast_positive_gt_0mm": sum(r["reference_value"] > 0 for r in common),
        }
    else:
        wet = {
            "status": "not_available",
            "reason": "no_approved_observed_precipitation_join_for_temperature_samples",
        }
    decision_dates = sorted({r["reference_time"][:10] for r in common})
    return {
        "schema_version": PAIR_SCHEMA,
        "field": field,
        "unit": "K" if field == TEMPERATURE else "mm",
        "reference_series": "exact_parent_stage" if reference_stages is None else "series",
        "cohort_digest": _digest(identity),
        "total_samples": total,
        "common_samples": len(common),
        "excluded_samples": total - len(common),
        "exclusions_by_reason": {reason: len(ids) for reason, ids in exclusions.items()},
        "excluded_sample_ids": exclusions,
        "control_exclusions": len(control_exclusions),
        "outside_decision_eligibility_window": len(outside),
        "not_applicable_outside_candidate_scope": len(not_applicable),
        "common_sample_ids": [r["sample_id"] for r in common],
        "composition": {
            "distinct_utc_decision_dates": len(decision_dates),
            "independence_claim": False,
            "decision_dates_utc": decision_dates,
            "valid_dates_utc": dict(sorted(Counter(r["valid_time"][:10] for r in common).items())),
            "lead_buckets": {
                bucket: sum(r["lead_bucket"] == bucket for r in common)
                for bucket in analytical_lead_buckets(r["lead_hours"] for r in common)
            },
            "locations": dict(sorted(Counter(_location(r) for r in common).items())),
            "observation_sources": dict(
                sorted(Counter(r["observation_source"] for r in common).items())
            ),
            "verification_policies": dict(
                sorted(Counter(r["verification_policy_id"] for r in common).items())
            ),
            "wet_non_wet": wet,
        },
        "by_lead_bucket": {
            bucket: _pair_metrics([r for r in common if r["lead_bucket"] == bucket])
            for bucket in analytical_lead_buckets(r["lead_hours"] for r in common)
        },
        "overall": {
            **_pair_metrics(common),
            "status": "descriptive_only",
            "note": "identical-sample aggregate differences across lead buckets",
        },
        "rows": common,
        "writes": 0,
    }
