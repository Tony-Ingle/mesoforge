"""Documented Kuchera ratio from an explicit, complete sampled air-temperature column.

NOAA Forecast Operations Guide: maximum air temperature from surface to 500 hPa.
https://vlab.noaa.gov/web/forecast-guide/fog?page=numerical-methods-for-determining-snow-accumulation
The formula has no imposed 10:1 fallback or upper cap. It does not diagnose p-type.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

KUCHERA_METHOD = {
    "id": "kuchera_surface_to_500hpa_25hpa_profile.v1",
    "formula": "12+2*(271.16-Tmax) if Tmax>271.16 K; otherwise 12+(271.16-Tmax)",
    "maximum_temperature_domain": "above-ground sampled air levels from surface through 500 hPa",
    "required_pressure_levels_hpa": list(range(500, 1001, 25)),
    "near_surface_air_temperature": "2 m air temperature, not land-surface skin temperature",
    "ratio_bounds": "No upper cap or fixed-ratio fallback; nonpositive ratios are not usable "
    "for positive snowfall water equivalent",
    "source": "https://vlab.noaa.gov/web/forecast-guide/fog?"
    "page=numerical-methods-for-determining-snow-accumulation",
    "sampling_limitation": "25 hPa samples plus 2 m air temperature; unresolved vertical "
    "warm layers and microphysical/settling effects are not modeled",
}


@dataclass(frozen=True)
class KucheraResult:
    ratio: np.ndarray
    maximum_temperature_k: np.ndarray
    aboveground_mask: np.ndarray
    valid_profile: np.ndarray
    missing_reasons: np.ndarray


def kuchera_ratio(
    temperature_profile: np.ndarray,
    pressure_levels_hpa: np.ndarray,
    temperature_2m: np.ndarray,
    surface_pressure_pa: np.ndarray,
) -> KucheraResult:
    """Calculate each column independently, preserving raw finite ratios without clipping.

    The leading profile axis is pressure level; all remaining axes must match the
    surface arrays. Below-ground temperatures can be missing and are excluded.
    Every atmospheric level slot in this explicitly versioned 25 hPa sampling is
    required. Invalid profiles have a missing ratio, never a surface-only fallback.
    """
    profile = np.asarray(temperature_profile, dtype=np.float64)
    levels = np.asarray(pressure_levels_hpa, dtype=np.float64)
    near_surface = np.asarray(temperature_2m, dtype=np.float64)
    surface_pressure = np.asarray(surface_pressure_pa, dtype=np.float64)
    expected = np.arange(500, 1001, 25, dtype=np.float64)
    if levels.ndim != 1 or not np.array_equal(np.sort(levels), expected):
        raise ValueError("Kuchera needs all 21 unique pressure levels from 500 to 1000 hPa")
    if (
        profile.shape != (len(levels), *near_surface.shape)
        or surface_pressure.shape != near_surface.shape
    ):
        raise ValueError("Kuchera profile and near-surface array dimensions disagree")
    pressure = levels.reshape((len(levels),) + (1,) * near_surface.ndim) * 100.0
    aboveground = pressure <= surface_pressure
    valid_surface_pressure = np.isfinite(surface_pressure) & (surface_pressure >= 50000)
    valid_near_surface = np.isfinite(near_surface) & (near_surface > 0)
    valid_air = np.isfinite(profile) & (profile > 0)
    complete = np.all(~aboveground | valid_air, axis=0)
    valid = valid_surface_pressure & valid_near_surface & complete
    maximum = np.maximum(
        np.max(np.where(aboveground & valid_air, profile, -np.inf), axis=0), near_surface
    )
    maximum = np.where(valid, maximum, np.nan)
    ratio = np.where(maximum > 271.16, 12 + 2 * (271.16 - maximum), 12 + (271.16 - maximum))
    reasons = np.full(near_surface.shape, "", dtype=object)
    reasons = np.where(~complete, "Missing or invalid above-ground air temperature", reasons)
    reasons = np.where(~valid_near_surface, "Missing or invalid 2 m air temperature", reasons)
    reasons = np.where(
        ~valid_surface_pressure,
        "Missing/invalid surface pressure or surface above the 500 hPa column boundary",
        reasons,
    )
    return KucheraResult(ratio, maximum, aboveground, valid, reasons)
