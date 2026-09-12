"""Native horizontal visibility units; no blend, censoring or weather diagnosis."""

from __future__ import annotations

import math

VISIBILITY = "visibility"
UNIT = "m"
METRES_PER_STATUTE_MILE = 1609.344
NATIVE_METRE_FACTORS = {"m": 1.0, "km": 1000.0, "statute_mile": METRES_PER_STATUTE_MILE}


def visibility_metres(value: float, native_unit: str = UNIT) -> float:
    """Preserve finite nonnegative native distances, without imposing an upper cap."""
    factor = NATIVE_METRE_FACTORS.get(native_unit)
    if factor is None:
        raise ValueError(f"Unsupported visibility unit: {native_unit}")
    result = float(value) * factor
    if not math.isfinite(result) or result < 0:
        raise ValueError("Visibility must be finite and nonnegative; no clipping or replacement")
    return result


def visibility_miles(metres: float) -> float:
    """Unrounded statute miles for display, retaining stored native/canonical values."""
    return visibility_metres(metres) / METRES_PER_STATUTE_MILE
