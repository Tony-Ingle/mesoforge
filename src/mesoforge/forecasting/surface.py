"""Retained Bolton liquid-water RH diagnostic; field dispatch lives in field_blend."""

from __future__ import annotations

import math

from mesoforge.common.errors import MesoForgeError

RH_POLICY = "bolton-1980-relative-humidity-liquid-water.v1"
RH_SOURCE = "https://archive.eol.ucar.edu/projects/ceop/dm/documents/refdata_report/eqns.html"


class SurfaceBlendError(MesoForgeError, ValueError):
    """A diagnostic cannot be calculated from scientifically valid inputs."""


def _usable(value: float | None, lower: float, upper: float) -> bool:
    return (
        value is not None
        and not isinstance(value, bool)
        and math.isfinite(value)
        and lower <= value <= upper
    )


def relative_humidity_percent(*, temperature_k: float, dew_point_k: float) -> float:
    """Return 100 e(Td)/es(T), the Bolton liquid-water approximation.

    Canonical Phase 2 temperature bounds apply. In degrees Celsius,
    e(x) = 6.112 exp(17.67 x / (x + 243.5)) hPa. The identical 6.112
    factor cancels in this ratio. Invalid or supersaturated results are
    rejected, never clipped to 100 percent.
    """
    if not _usable(temperature_k, 150.0, 340.0) or not _usable(dew_point_k, 150.0, 340.0):
        raise SurfaceBlendError("RH requires finite temperature/dew point within [150, 340] K")
    if dew_point_k > temperature_k:
        raise SurfaceBlendError("RH unavailable: dew point exceeds temperature")
    temperature_c = temperature_k - 273.15
    dew_point_c = dew_point_k - 273.15
    humidity = 100.0 * math.exp(
        17.67 * dew_point_c / (dew_point_c + 243.5)
        - 17.67 * temperature_c / (temperature_c + 243.5)
    )
    if not math.isfinite(humidity) or not 0.0 <= humidity <= 100.0:
        raise SurfaceBlendError("RH diagnostic is outside [0, 100] percent")
    return humidity
