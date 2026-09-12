"""Cloud percentage and versioned display categories; no cloud blend policy."""

from __future__ import annotations

import math

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
