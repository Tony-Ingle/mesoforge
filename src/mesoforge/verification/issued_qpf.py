"""Compact immutable QPF comparison evidence from saved forecasts and MRMS extracts.

This module neither generates forecasts nor acquires observations. Exact interval
identity is mandatory; station-temperature time tolerances do not apply.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.observations.mrms import (
    EXTRACTION_POLICY,
    PRODUCT_CONTRACTS,
    MRMSContractError,
    validate_support_alignment,
)
from mesoforge.observations.mrms import (
    SCHEMA_VERSION as MRMS_SCHEMA,
)

SCHEMA_VERSION = "issued-qpf-verification.v1"
POLICY_ID = "qpf-mrms-verification.v1"
FIELD = "liquid_equivalent_precipitation_amount_1h"
QPE_PRODUCT = "MultiSensor_QPE_01H_Pass2"


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _time(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return (
        result.astimezone(UTC)
        if result.tzinfo is not None and result.utcoffset() is not None
        else None
    )


def _iso(value: Any) -> str | None:
    instant = _time(value)
    return instant.isoformat().replace("+00:00", "Z") if instant else None


def _number(value: Any, *, nonnegative: bool = True) -> float | None:
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    return float(value) if not nonnegative or value >= 0 else None


def _amount(field: Mapping[str, Any]) -> float | None:
    # One kg/m² liquid water is numerically one mm; no temporal redistribution.
    return _number(field.get("value")) if field.get("unit") in ("kg/m^2", "mm") else None


def _interval(field: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    start, end = _time(field.get("interval_start")), _time(field.get("interval_end"))
    if (
        start is None
        or end is None
        or (end - start).total_seconds() != 3600
        or field.get("interval_closure") != "left_open_right_closed"
        or field.get("temporal_semantics") != "accumulation"
    ):
        return None
    return start, end


def _digest(value: Any) -> str | None:
    try:
        return str(Digest(value))
    except (TypeError, ValueError):
        return None


def _baseline_reference(forecast: Mapping[str, Any]) -> dict[str, Any]:
    baseline = _map(forecast.get("baseline_snapshot"))
    return {
        key: baseline.get(key)
        for key in (
            "baseline_snapshot_id",
            "schema_version",
            "manifest_sha256",
            "prepared_snapshot_id",
            "background_analysis_cutoff",
            "forecast_analysis_cutoff",
            "reference_time",
        )
    }


def _contributors(
    surface: Mapping[str, Any], interval: tuple[datetime, datetime] | None
) -> dict[str, Any]:
    results = {}
    for model, source in _map(surface.get("contributors")).items():
        source = _map(source)
        field = _map(_map(source.get("fields")).get(FIELD))
        own_interval = _interval(field)
        value = _amount(field)
        reasons = []
        if own_interval is None or own_interval != interval:
            reasons.append("incompatible_or_unavailable_hourly_interval")
        if value is None:
            reasons.append("amount_unavailable_or_invalid")
        if field.get("missing_reasons"):
            reasons.append("contributor_has_explicit_missingness")
        cycle = _time(field.get("source_cycle", source.get("cycle")))
        lead = _number(field.get("source_lead_hours", source.get("source_lead_hours")))
        if cycle is None or lead is None or own_interval is None:
            reasons.append("source_cycle_or_lead_unavailable")
        elif (own_interval[1] - cycle).total_seconds() != lead * 3600:
            reasons.append("source_cycle_lead_interval_inconsistent")
        results[model] = {
            "amount_mm": value,
            "native_value": field.get("value"),
            "unit": field.get("unit"),
            "compatible": not reasons,
            "reasons": reasons,
            "missing_reasons": deepcopy(field.get("missing_reasons", [])),
            "interval_start": _iso(field.get("interval_start")),
            "interval_end": _iso(field.get("interval_end")),
            "interval_closure": field.get("interval_closure"),
            "source_cycle": field.get("source_cycle", source.get("cycle")),
            "source_lead_hours": field.get("source_lead_hours", source.get("source_lead_hours")),
            "role": source.get("role"),
            "policy": field.get("policy"),
            "evidence_path": f"forecast.hours[*].surface.contributors.{model}.fields.{FIELD}",
            "native_evidence_digest": str(Digest.of_bytes(canonical_json_bytes(dict(field)))),
        }
    return results


def _observation(
    extraction: Any,
    reference: Any,
    *,
    coordinate: dict[str, Any],
    interval: tuple[datetime, datetime] | None,
    cutoff: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    if extraction is None:
        return None, ["observation_missing"]
    if not isinstance(extraction, Mapping):
        return None, ["malformed_observation"]
    qpe = _map(extraction.get("qpe"))
    temporal = _map(qpe.get("temporal"))
    native = _map(qpe.get("extraction"))
    value = _map(qpe.get("value"))
    grid = _map(qpe.get("grid"))
    reference = _map(reference)
    reasons: list[str] = []
    if (
        extraction.get("schema_version") != "mesoforge.mrms-coordinate-extraction.v1"
        or qpe.get("schema_version") != MRMS_SCHEMA
        or qpe.get("product_contract") != asdict(PRODUCT_CONTRACTS[QPE_PRODUCT])
        or temporal.get("semantics_origin") != "documented_product_contract"
        or temporal.get("closure") != "(start,end]"
        or temporal.get("duration_seconds") != 3600
        or temporal.get("product_time") != qpe.get("product_time")
    ):
        reasons.append("observation_contract_incompatible")
    start, end = _time(temporal.get("interval_start")), _time(temporal.get("interval_end"))
    if start is None or end is None or interval != (start, end):
        reasons.append("incompatible_interval")
    if end is not None and (_time(qpe.get("product_time")) != end or end > cutoff):
        reasons.append("observation_interval_or_product_time_invalid")
    distance = _number(native.get("distance_m"))
    if (
        native.get("forecast_coordinate") != coordinate
        or native.get("policy_id") != EXTRACTION_POLICY
        or distance is None
        or not isinstance(native.get("native_coordinate"), Mapping)
        or any(
            type(native.get(key)) is not int or native[key] < 0
            for key in ("row", "column", "grid_index")
        )
        or not isinstance(grid.get("identity_sha256"), str)
    ):
        reasons.append("unsuitable_spatial_support")
    amount = _number(value.get("value")) if value.get("units") == "mm" else None
    state = value.get("state")
    if state in ("missing", "no_coverage"):
        reasons.append("observation_" + state)
    elif state not in ("zero", "positive") or amount is None:
        reasons.append("malformed_observation_amount")
    elif (state == "zero") != (amount == 0) or value.get("raw_value") != amount:
        reasons.append("malformed_observation_amount")
    raw_digest = _digest("sha256:" + str(qpe.get("raw_sha256")))
    message_digest = _digest("sha256:" + str(qpe.get("message_sha256")))
    raw_refs = _map(extraction.get("raw_sources"))
    sources = _map(_map(extraction.get("source_bundle")).get("sources"))
    source = _map(sources.get(QPE_PRODUCT))
    raw_ref = _map(raw_refs.get(QPE_PRODUCT))
    if (
        raw_digest is None
        or message_digest is None
        or raw_ref.get("content_digest") != raw_digest
        or source.get("content_digest") != raw_digest
    ):
        reasons.append("observation_revision_identity_unavailable_or_inconsistent")
    try:
        for ref in (reference, raw_ref):
            artifact_id = ref.get("artifact_id")
            if not isinstance(artifact_id, str):
                raise ValueError("Missing artifact identity")
            ArtifactId(artifact_id)
        if _digest(reference.get("content_digest")) is None:
            raise ValueError("Invalid extraction digest")
    except (TypeError, ValueError):
        reasons.append("observation_reference_unavailable")
    acquired = _time(source.get("acquired_at"))
    available = _time(reference.get("available_at"))
    if acquired is None or available is None:
        reasons.append("observation_availability_unproven")
    elif acquired > cutoff or available > cutoff:
        reasons.append("observation_not_yet_available")
    elif end is not None and (acquired < end or available < acquired):
        reasons.append("observation_availability_inconsistent")
    quality = {}
    if set(_map(extraction.get("quality_support"))) != set(PRODUCT_CONTRACTS) - {QPE_PRODUCT}:
        reasons.append("quality_support_products_unavailable_or_incompatible")
    for product, support in _map(extraction.get("quality_support")).items():
        support = _map(support)
        try:
            validate_support_alignment(dict(qpe), dict(support))
        except (MRMSContractError, KeyError, TypeError):
            reasons.append("quality_support_alignment_invalid")
        record = _map(sources.get(product))
        quality[product] = {
            "value": deepcopy(support.get("value")),
            "product_time": support.get("product_time"),
            "raw_sha256": support.get("raw_sha256"),
            "message_sha256": support.get("message_sha256"),
            "contract_id": _map(support.get("product_contract")).get("contract_id"),
            "acquired_at": _iso(record.get("acquired_at")),
            "artifact_reference": dict(_map(raw_refs.get(product))),
        }
        support_acquired = _time(record.get("acquired_at"))
        if support_acquired is None or support_acquired > cutoff:
            reasons.append("quality_support_availability_unproven_or_after_cutoff")
    return {
        "amount_mm": amount,
        "state": state,
        "product": QPE_PRODUCT,
        "product_time": qpe.get("product_time"),
        "contract_id": _map(qpe.get("product_contract")).get("contract_id"),
        "interval_start": _iso(start),
        "interval_end": _iso(end),
        "interval_closure": "left_open_right_closed",
        "raw_sha256": qpe.get("raw_sha256"),
        "message_sha256": qpe.get("message_sha256"),
        "semantic_revision_digest": message_digest,
        "grid_identity": grid.get("identity_sha256"),
        "cell": dict(native),
        "extraction_policy": native.get("policy_id"),
        "acquired_at": _iso(acquired),
        "available_at": _iso(available),
        "artifact_reference": dict(reference),
        "raw_artifact_reference": dict(raw_ref),
        "source_url": source.get("url"),
        "quality_support": quality,
        "quality_threshold_policy": "none_approved_raw_evidence_only",
        "reference_type": "gridded_precipitation_analysis_not_point_gauge_or_perfect_truth",
    }, reasons


def evaluate_qpf_verification(
    saved: dict[str, Any],
    valid_time: datetime,
    *,
    stage: str,
    issued_forecast_digest: Digest,
    verification_cutoff: datetime,
    extraction: Any = None,
    extraction_reference: Any = None,
) -> dict[str, Any]:
    """Evaluate one exact saved interval; output is small and replay-deterministic."""
    cutoff, valid = _time(verification_cutoff), _time(valid_time)
    if cutoff is None or valid is None:
        raise ValueError("valid_time and verification_cutoff must include timezones")
    digest = Digest(issued_forecast_digest)
    issued_id = str(UUID(saved["issued_forecast_id"]))
    forecast = _map(saved.get("forecast"))
    coordinate = {key: saved.get(key) for key in ("latitude", "longitude")}
    reasons = []
    hours = [
        hour for hour in forecast.get("hours", []) if _time(_map(hour).get("valid_time")) == valid
    ]
    hour = _map(hours[0]) if len(hours) == 1 else {}
    if len(hours) != 1:
        reasons.append("forecast_hour_unavailable_or_ambiguous")
    surface = _map(hour.get("surface"))
    field = _map(_map(surface.get("fields")).get(FIELD))
    interval = _interval(field)
    amount = _amount(field)
    if amount is None or field.get("status") not in ("available", "fallback"):
        reasons.append("forecast_qpf_unavailable_or_invalid")
    if interval is None or interval[1] != valid:
        reasons.append("incompatible_forecast_interval")
    issued, target = _time(saved.get("issued_at")), _time(saved.get("target_reference_time"))
    if issued is None or target is None:
        reasons.append("forecast_timestamps_unavailable")
    if interval and issued is not None and issued > interval[0]:
        reasons.append("forecast_not_issued_before_interval_start")
    for model, weight in _map(field.get("weights")).items():
        if (_number(weight) or 0) <= 0:
            continue
        source = _map(_map(surface.get("contributors")).get(model))
        source_field = _map(_map(source.get("fields")).get(FIELD))
        cycle = _time(source_field.get("source_cycle", source.get("cycle")))
        if cycle is not None and issued is not None and cycle > issued:
            reasons.append("forecast_source_cycle_after_issuance")
    if valid > cutoff:
        reasons.append("forecast_interval_not_yet_complete")
    if not field.get("policy"):
        reasons.append("forecast_policy_unavailable")
    for key, bound in (("latitude", 90), ("longitude", 180)):
        numeric_coordinate = _number(coordinate[key], nonnegative=False)
        if (
            numeric_coordinate is None
            or forecast.get(key) != coordinate[key]
            or not -bound <= numeric_coordinate <= bound
        ):
            reasons.append("forecast_coordinate_invalid")
    lead = (valid - target).total_seconds() / 3600 if target else None
    if lead != hour.get("horizon_hours") or lead is None or not 1 <= lead <= 36:
        reasons.append("forecast_reference_lead_inconsistent")
    baseline = _baseline_reference(forecast)
    local_version = _map(forecast.get("local_grid")).get("version")
    has_baseline = bool(baseline["baseline_snapshot_id"]) or local_version in (
        "mesoforge.local-surface-baseline.v1",
        "mesoforge.local-surface-baseline.v2",
    )
    if stage not in ("baseline", "final_issued"):
        reasons.append("unsupported_forecast_stage")
    elif stage == "baseline" and not has_baseline:
        reasons.append("baseline_stage_unavailable")
    modes = {
        _map(forecast.get(name)).get("issuance_mode")
        for name in ("baseline_snapshot", "prepared_snapshot")
    } - {None}
    if len(modes) > 1:
        reasons.append("issuance_mode_conflicting")
    observation, observation_reasons = _observation(
        extraction, extraction_reference, coordinate=coordinate, interval=interval, cutoff=cutoff
    )
    reasons.extend(observation_reasons)
    opportunity = {
        "issued_forecast_id": issued_id,
        "field": FIELD,
        "stage": stage,
        "verification_policy_id": POLICY_ID,
        "valid_time": _iso(valid),
        "interval_start": _iso(field.get("interval_start")),
        "interval_end": _iso(field.get("interval_end")),
        "interval_closure": field.get("interval_closure"),
        **coordinate,
    }
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "verification_policy_id": POLICY_ID,
        "status": "excluded" if reasons else "verified",
        "reasons": sorted(set(reasons)),
        **opportunity,
        "opportunity_id": str(Digest.of_bytes(canonical_json_bytes(opportunity))),
        "issued_forecast_digest": str(digest),
        "issued_at": _iso(issued),
        "target_reference_time": _iso(target),
        "issuance_mode": next(iter(modes)) if len(modes) == 1 else None,
        "baseline_snapshot": baseline,
        "stage_evidence": (
            "unsupported_stage"
            if stage not in ("baseline", "final_issued")
            else "saved_numerical_baseline_no_qpf_adjustment_stage_implemented"
            if has_baseline
            else "saved_final_issued_hour_baseline_lineage_unavailable"
            if stage == "final_issued"
            else "baseline_stage_unavailable"
        ),
        "interval_start": _iso(interval[0]) if interval else _iso(field.get("interval_start")),
        "interval_end": _iso(interval[1]) if interval else _iso(field.get("interval_end")),
        "duration_seconds": 3600 if interval else None,
        "interval_closure": field.get("interval_closure"),
        "lead_hours": lead,
        "verification_cutoff": _iso(cutoff),
        "forecast": {
            "amount_mm": amount,
            "native_value": field.get("value"),
            "unit": field.get("unit"),
            "status": field.get("status"),
            "policy": field.get("policy"),
            "row_id": field.get("row_id"),
            "row_sha256": field.get("row_sha256"),
            "weights": deepcopy(field.get("weights")),
            "missing_reasons": deepcopy(field.get("missing_reasons", [])),
            "evidence_path": f"forecast.hours[*].surface.fields.{FIELD}",
        },
        "contributors": _contributors(surface, interval),
        "observation": observation,
        "qpf_error_mm": None,
        "error_definition": "forecast_minus_observation",
        "occurrence": None,
    }
    if not reasons:
        assert amount is not None and observation is not None
        result["qpf_error_mm"] = amount - observation["amount_mm"]
        result["occurrence"] = {
            "definition": "strictly_positive_liquid_amount_not_pop_or_detection_skill",
            "forecast_positive": amount > 0,
            "observed_positive": observation["amount_mm"] > 0,
        }
    return result
