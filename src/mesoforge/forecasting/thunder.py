"""Native thunder probabilities and their events, separate from deterministic diagnostics."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

THUNDER = "probability_of_thunder_1h"
UNIT = "1"
NATIVE_FRACTION_FACTORS = {"1": 1.0, "%": 0.01, "percent": 0.01}
ACTIVE_POLICY: dict[str, Any] = {
    "policy_id": "nbm-native-hourly-thunder-baseline.v1",
    "source_id": "NBM_1H",
    "method": "native_hourly_probability_passthrough",
    "temporary": True,
    "interpretation": "Provider-defined native NBM thunder probability at the sampled source cell; "
    "not an exact-point lightning probability. No multisource blend or period conversion.",
}


def thunder_fraction(value: float, native_unit: str = UNIT) -> float:
    """Normalize an actual native probability without clipping or fabricating one."""
    factor = NATIVE_FRACTION_FACTORS.get(native_unit)
    if factor is None:
        raise ValueError(f"Unsupported thunder probability unit: {native_unit}")
    result = float(value) * factor
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("Thunder probability must be finite and within [0,1]; no clipping")
    return result


def thunder_percent(fraction: float) -> float:
    """Return unrounded display percent while keeping the numerical fraction in storage."""
    return thunder_fraction(fraction) * 100.0


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Thunder event times require an explicit timezone")
    return result.astimezone(UTC)


def validate_thunder_event(event: dict[str, Any]) -> tuple[datetime, datetime]:
    """Validate a native event without substituting rainfall or deterministic diagnostics."""
    start, end, cycle = (
        _time(event[key])
        for key in (
            "interval_start",
            "interval_end",
            "source_cycle",
        )
    )
    lead, duration = event["source_lead_hours"], event["duration_hours"]
    if (
        type(lead) not in (int, float)
        or not math.isfinite(lead)
        or lead < 0
        or type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration <= 0
        or start < cycle
        or start >= end
        or cycle + timedelta(hours=lead) != end
        or end - start != timedelta(hours=duration)
        or _time(event["valid_time"]) != end
        or event.get("temporal_semantics") != "interval_probability"
        or event.get("interval_closure") != "left_open_right_closed"
    ):
        raise ValueError(
            "Thunder native interval, cycle, lead or valid-time semantics incompatible"
        )
    definition, support = event.get("event_definition"), event.get("spatial_support")
    if (
        not isinstance(definition, dict)
        or not isinstance(definition.get("id"), str)
        or not definition["id"]
        or not isinstance(definition.get("parameter"), str)
        or not definition["parameter"]
        or event.get("probability_method") != "native_published_probability"
        or "physical_threshold" not in definition
        or not definition.get("threshold_status")
        or not isinstance(support, dict)
        or not support.get("kind")
    ):
        raise ValueError("Native thunder event definition or spatial support metadata missing")
    if definition["threshold_status"] == "provider_defined_not_encoded" and (
        definition["physical_threshold"] is not None
    ):
        raise ValueError("An unencoded physical thunder threshold cannot be invented")
    if support.get("geometry_status") == "not_encoded" and support.get("radius_km") is not None:
        raise ValueError("An unencoded thunder-event footprint cannot acquire an invented radius")
    return start, end


def thunder_event_comparison_reasons(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """A native grid spacing does not establish equivalence of probability event footprints."""
    try:
        left_interval, right_interval = validate_thunder_event(left), validate_thunder_event(right)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return [f"Invalid native thunder event: {exc}"]
    reasons = []
    if left_interval != right_interval or left["interval_closure"] != right["interval_closure"]:
        reasons.append("Native thunder intervals differ; no period conversion is approved")
    if left["event_definition"] != right["event_definition"]:
        reasons.append("Native thunder event definitions or physical thresholds differ")
    if left["spatial_support"] != right["spatial_support"]:
        reasons.append("Native thunder spatial supports differ")
    for support in (left["spatial_support"], right["spatial_support"]):
        if not _support_known(support):
            reasons.append("Cross-source thunder-event spatial equivalence is unproven")
            break
    return reasons


def _support_known(support: dict[str, Any]) -> bool:
    if support.get("geometry_status") != "known":
        return False
    if support.get("kind") == "grid_point":
        return True
    radius = support.get("radius_km")
    return (
        support.get("kind") == "neighborhood"
        and isinstance(radius, (int, float))
        and not isinstance(radius, bool)
        and math.isfinite(radius)
        and radius > 0
        and isinstance(support.get("operator"), str)
        and support["operator"] not in ("", "unknown")
    )
