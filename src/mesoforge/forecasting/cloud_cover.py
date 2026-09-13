"""Cloud display categories and the temporary native NBM total-cloud baseline."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

CLOUD = "cloud_area_fraction"
CLOUD_ACTIVE_POLICY: dict[str, Any] = {
    "policy_id": "nbm-native-total-cloud-baseline.v1",
    "model": "NBM",
    "method": "native_instantaneous_total_cloud_passthrough",
    "temporary": True,
    "missing_policy": "required_nbm_no_fallback",
    "interpretation": "Native NBM total sky cover is the temporary active baseline. "
    "No multi-model cloud blend, layer summation, temporal interpolation or opaque-sky "
    "observation equivalence is implied.",
}

NATIVE_PERCENT_FACTORS = {"percent": 1.0, "%": 1.0, "1": 100.0, "(0 - 1)": 100.0}
SKY_CATEGORY_POLICY = {
    "policy_id": "native-cloud-percentage-display.v1",
    "input_unit": "percent",
    "round_before_classification": False,
    "upper_inclusive_bounds": [
        {"category": "clear", "percent": 5.0},
        {"category": "mostly_clear", "percent": 25.0},
        {"category": "partly_cloudy", "percent": 50.0},
        {"category": "mostly_cloudy", "percent": 87.0},
        {"category": "cloudy", "percent": 100.0},
    ],
    "interpretation": "Presentation of each native model's total cloud cover, not an "
    "opaque-sky observation or complete weather condition. No day/night wording changes.",
    "reference": "https://www.weather.gov/media/pah/ServiceGuide/A-forecast.pdf",
}


def cloud_percentage(value: float, native_unit: str = "percent") -> float:
    """Normalize native fraction/percentage without rounding or repairing invalid data."""
    factor = NATIVE_PERCENT_FACTORS.get(native_unit)
    if factor is None:
        raise ValueError(f"Unsupported cloud-cover unit: {native_unit}")
    result = float(value) * factor
    if not math.isfinite(result) or not 0.0 <= result <= 100.0:
        raise ValueError("Cloud cover must be finite and within 0–100 percent; no clipping")
    return result


def sky_category(percent: float) -> str:
    """Classify the unrounded percentage using explicit, gap-free display boundaries."""
    value = cloud_percentage(percent)
    for limit, category in (
        (5.0, "clear"),
        (25.0, "mostly_clear"),
        (50.0, "partly_cloudy"),
        (87.0, "mostly_cloudy"),
    ):
        if value <= limit:
            return category
    return "cloudy"


def validate_active_cloud_field(field: dict[str, Any], *, valid_time: str) -> None:
    """Validate the exact approved native source and saved fraction/percent/category contract."""
    if (
        field.get("policy") != CLOUD_ACTIVE_POLICY
        or field.get("weights") != {"NBM": 1.0}
        or field.get("status") != "available"
        or field.get("role") != "temporary_active_baseline"
        or field.get("active_weight") != 1.0
        or field.get("missing_reasons")
    ):
        raise ValueError("Active cloud requires the eligible temporary NBM baseline; no fallback")
    expected = {
        "model": "NBM",
        "provider": "NOAA",
        "product": "core CONUS deterministic total sky cover",
        "native_parameter": "TCDC",
        "native_vertical_binding": "surface",
        "native_step_hours": 1,
        "cloud_definition": "total_cloud_cover",
        "vertical_extent": "entire_atmosphere",
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "unit": "1",
    }
    if any(field.get(key) != value for key, value in expected.items()):
        raise ValueError("Active cloud requires native NBM instantaneous entire-column total cover")
    try:
        target, valid, cycle = (
            datetime.fromisoformat(value)
            for value in (valid_time, field["valid_time"], field["source_cycle"])
        )
        lead = field["source_lead_hours"]
        if any(t.tzinfo is None or t.utcoffset() is None for t in (target, valid, cycle)) or (
            type(lead) not in (int, float)
            or not math.isfinite(lead)
            or lead < 0
            or cycle + timedelta(hours=lead) != valid
            or valid != target
        ):
            raise ValueError("Native cloud cycle, lead and instantaneous target disagree")
        value, percent, native = field["value"], field["cloud_percentage"], field["native_value"]
        if any(type(v) not in (int, float) for v in (value, percent, native)):
            raise ValueError("Cloud fraction, percentage and native value must be numeric")
        cloud_percentage(value, "1")
        cloud_percentage(percent)
        if (
            field.get("native_unit") not in ("%", "percent")
            or field.get("native_factor_to_percent") != 1.0
        ):
            raise ValueError("Native NBM total-cloud percentage units are required")
        if value != percent / 100 or cloud_percentage(native, field["native_unit"]) != percent:
            raise ValueError("Native NBM percentage does not reproduce the active cloud fraction")
        if (
            field.get("sky_category") != sky_category(percent)
            or field.get("sky_category_policy") != SKY_CATEGORY_POLICY
        ):
            raise ValueError("Active sky category must use the unrounded native cloud percentage")
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"Active cloud metadata is incomplete or invalid: {exc}") from exc
