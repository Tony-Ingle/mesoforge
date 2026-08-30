"""Baseline-specific scientific validation beyond the generic
``baseline-forecast.v1`` contract (plan Section 3.6, Task 7):
cardinal/intercardinal direction sanity and signed-zero handling.

``contracts.forecasts.validate_baseline_forecast`` owns the schema/
completeness contract; this module owns the narrower wind-derivation
sanity check exercised directly by ``forecasting.baseline``'s own
tests (Task 7 step 4: "cardinal/intercardinal vectors, zero calm,
signed zero, finite bounds, and missing source rejection").
"""

from __future__ import annotations

import numpy as np

from mesoforge.common.errors import MesoForgeError


class WindDerivationError(MesoForgeError):
    """Raised when derived wind speed/direction violate a basic
    physical bound (negative speed, direction outside [0, 360))."""


def validate_wind_derivation(*, speed: np.ndarray, direction: np.ndarray) -> None:
    errors: list[str] = []
    finite_speed = speed[np.isfinite(speed)]
    if finite_speed.size and np.any(finite_speed < 0):
        errors.append("wind_speed_10m must be nonnegative")

    finite_direction = direction[np.isfinite(direction)]
    if finite_direction.size and (np.any(finite_direction < 0) or np.any(finite_direction >= 360)):
        errors.append("wind_from_direction_10m must be in [0, 360) wherever defined")

    calm = speed == 0
    if np.any(calm & np.isfinite(direction)):
        errors.append("direction must be undefined (NaN) at exactly zero speed")

    if errors:
        raise WindDerivationError(
            f"wind derivation validation failed with {len(errors)} problem(s): " + "; ".join(errors)
        )
