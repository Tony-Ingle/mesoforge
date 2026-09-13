"""Distinct native ice mass and freezing-rain liquid amounts; no accretion algorithm."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

FLAT_ICE = "flat_ice_accretion_mass_equivalent"
FRZR = "freezing_rain_liquid_equivalent_amount"
FREEZING_RAIN = "freezing_rain_liquid_equivalent"
UNIT = "kg/m^2"
CLOSURE = "left_open_right_closed"
QUANTITIES = {FLAT_ICE, FREEZING_RAIN}
NATIVE_FACTORS = {"kg/m^2": 1.0, "kg m**-2": 1.0, "kg m-2": 1.0, "kg m^-2": 1.0}
POLICY: dict[str, Any] = {
    "policy_id": "native-ice-evidence.v1",
    "status": "policy_unavailable",
    "active_weights": {},
    "interpretation": "No approved active ice or freezing-rain-liquid blend. Preserve native "
    "flat-ice mass-equivalent and freezing-rain liquid separately; no density, thickness, "
    "ice/liquid-ratio or locally derived accretion conversion.",
}


def ice_amount(value: float, *, quantity_kind: str, native_unit: str = UNIT) -> float:
    """Normalize supported mass-per-area encodings without assigning ice density."""
    if quantity_kind not in QUANTITIES or native_unit not in NATIVE_FACTORS:
        raise ValueError("Unsupported native ice quantity/unit; no liquid-to-ice conversion")
    result = float(value) * NATIVE_FACTORS[native_unit]
    if not math.isfinite(result) or result < 0:
        raise ValueError("Ice/freezing-rain amount must be finite and nonnegative; no clipping")
    return result


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Ice accumulation times require an explicit timezone")
    return result.astimezone(UTC)


def validate_ice_event(event: dict[str, Any]) -> tuple[datetime, datetime]:
    """Keep physical quantity, native geometry and exact accumulation bounds inseparable."""
    start, end, cycle = (
        _time(event[key]) for key in ("interval_start", "interval_end", "source_cycle")
    )
    lead, duration = event["source_lead_hours"], event["duration_hours"]
    quantity, factor = event["quantity_kind"], event["native_factor_to_canonical"]
    if (
        quantity not in QUANTITIES
        or event.get("unit") != UNIT
        or (factor != NATIVE_FACTORS.get(event.get("native_unit", "")))
    ):
        raise ValueError("Native ice quantity, units or conversion factor disagree")
    if (
        type(lead) not in (int, float)
        or not math.isfinite(lead)
        or lead <= 0
        or type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration <= 0
        or start < cycle
        or start >= end
        or cycle + timedelta(hours=lead) != end
        or end - start != timedelta(hours=duration)
        or _time(event["valid_time"]) != end
        or event.get("temporal_semantics") != "accumulation"
        or event.get("interval_closure") != CLOSURE
    ):
        raise ValueError("Ice accumulation interval, cycle, lead or valid time disagree")
    if (
        not event.get("spatial_support")
        or (quantity == FLAT_ICE and event.get("accretion_geometry") != "elevated_flat_surface")
        or (quantity == FREEZING_RAIN and event.get("accretion_geometry") is not None)
    ):
        raise ValueError("Native flat-ice geometry and freezing-rain-liquid support are distinct")
    normalization = event.get("normalization", {})
    if (
        not isinstance(normalization, dict)
        or normalization.get("method")
        not in (
            "native_interval_amount_times_unit_factor",
            "native_cumulative_end_minus_start_then_unit_factor",
        )
        or normalization.get("unit_factor_to_kg_m2") != factor
    ):
        raise ValueError("Unsupported or inconsistent native accumulation normalization")
    if normalization["method"] == "native_cumulative_end_minus_start_then_unit_factor" and (
        quantity != FREEZING_RAIN or _time(event["native_interval_start"]) != cycle
    ):
        raise ValueError("Only native cumulative freezing-rain liquid is differenced here")
    return start, end


def ice_comparison_reasons(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Compare descriptions of the same physical amount and interval, never imply skill."""
    try:
        a, b = validate_ice_event(left), validate_ice_event(right)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return [f"Invalid native ice interval: {exc}"]
    reasons = []
    if left["quantity_kind"] != right["quantity_kind"]:
        reasons.append("Flat-ice accretion mass and freezing-rain liquid are different quantities")
    if a != b or left["interval_closure"] != right["interval_closure"]:
        reasons.append("Accumulation intervals differ; no period conversion or filling")
    if any(
        left.get(key) != right.get(key) for key in ("unit", "spatial_support", "accretion_geometry")
    ):
        reasons.append("Native units, spatial support or accretion geometries differ")
    return reasons
