"""NOAA NBM CONUS core provider adapter (plan Section 2.3, Task 3).

Pure URL construction, per-lead inventory selector construction, and
the speed/direction-to-earth-relative-components conversion live here
as plain functions with no I/O -- reused by ``guidance/acquisition.py``
(with an injected transport) and any live smoke test. NBM's operational
CONUS core filename carries no marketing version (plan Section 1.3), so
identity is pinned by ``contract_profile`` in configuration, not parsed
from the URL.
"""

from __future__ import annotations

import math
from datetime import date

from mesoforge.catalog.sources import NbmSourceSettings

# NBM canonical variable -> the exact wgrib2-style inventory selector
# fragment (parameter/level text) used to build a lead-specific regex.
# Deterministic accumulation/probability fields additionally need the
# exact lead-dependent interval text, built by
# ``build_field_selector`` below.
_INSTANTANEOUS_SELECTOR_FRAGMENTS: dict[str, str] = {
    "air_temperature_2m": ":TMP:2 m above ground:",
    "dew_point_temperature_2m": ":DPT:2 m above ground:",
    "wind_speed_10m": ":WIND:10 m above ground:",
    "wind_from_direction_10m": ":WDIR:10 m above ground:",
    "wind_gust_10m": ":GUST:10 m above ground:",
}


def format_grib_filename(
    settings: NbmSourceSettings, *, cycle_hour: int, forecast_hour: int
) -> str:
    """Section 1.3: ``blend.t{HH:02d}z.core.f{FFF:03d}.co.grib2``."""
    if not (0 <= cycle_hour <= 23):
        raise ValueError(f"cycle_hour must be in [0, 23], got {cycle_hour!r}")
    if forecast_hour < 0:
        raise ValueError(f"forecast_hour must be nonnegative, got {forecast_hour!r}")
    return settings.file_template.format(HH=cycle_hour, FFF=forecast_hour)


def build_grib_url(
    settings: NbmSourceSettings,
    *,
    endpoint: str,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
) -> str:
    if endpoint not in settings.endpoint_url_templates:
        raise ValueError(
            f"unknown endpoint {endpoint!r}; expected one of {settings.endpoint_order!r}"
        )
    filename = format_grib_filename(settings, cycle_hour=cycle_hour, forecast_hour=forecast_hour)
    template = settings.endpoint_url_templates[endpoint]
    return template.format(YYYYMMDD=cycle_date.strftime("%Y%m%d"), HH=cycle_hour, FILE=filename)


def build_index_url(
    settings: NbmSourceSettings,
    *,
    endpoint: str,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
) -> str:
    return (
        build_grib_url(
            settings,
            endpoint=endpoint,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            forecast_hour=forecast_hour,
        )
        + settings.index_suffix
    )


def build_field_selector(canonical_variable_id: str, *, forecast_hour: int) -> str:
    """Section 2.3: build the exact lead-dependent inventory selector
    for one NBM canonical variable.

    Instantaneous fields (temperature, dew point, wind speed/direction,
    gust) select on ``{lead} hour fcst`` only. Deterministic APCP
    selects the plain rolling one-hour accumulation record and
    explicitly excludes any probability-forecast suffix. PoP01 selects
    exactly the ``prob >0.254`` probability-forecast record and
    excludes the deterministic accumulation.
    """
    if forecast_hour < 1:
        raise ValueError(f"NBM forecast_hour must be >= 1, got {forecast_hour!r}")
    step = f"{forecast_hour} hour fcst"
    lead_minus_one = forecast_hour - 1

    if canonical_variable_id in _INSTANTANEOUS_SELECTOR_FRAGMENTS:
        fragment = _INSTANTANEOUS_SELECTOR_FRAGMENTS[canonical_variable_id]
        return f"{fragment}{step}:$"

    if canonical_variable_id == "liquid_equivalent_precipitation_amount_1h":
        return rf":APCP:surface:{lead_minus_one}-{forecast_hour} hour acc fcst:$"

    if canonical_variable_id == "probability_of_precipitation_1h":
        return (
            rf":APCP:surface:{lead_minus_one}-{forecast_hour} hour acc fcst:"
            r"prob >0\.254:.*probability forecast:$"
        )

    raise ValueError(f"unknown NBM canonical_variable_id {canonical_variable_id!r}")


def convert_speed_direction_to_components(
    *, speed_m_s: float, direction_degrees: float
) -> tuple[float, float]:
    """Section 2.3: NBM's speed/direction pair is converted to
    earth-relative U/V components *before* spatial interpolation.

    ``u = -speed * sin(direction_degrees)``,
    ``v = -speed * cos(direction_degrees)``. Calm speed (exactly 0)
    yields ``u=v=0`` regardless of the reported direction (direction
    is ignored/undefined at calm). Rejects a negative speed or a
    direction outside ``[0, 360)`` at nonzero speed.
    """
    if not math.isfinite(speed_m_s) or speed_m_s < 0:
        raise ValueError(f"speed_m_s must be finite and nonnegative, got {speed_m_s!r}")
    if speed_m_s == 0.0:
        return 0.0, 0.0
    if not math.isfinite(direction_degrees) or not (0.0 <= direction_degrees < 360.0):
        raise ValueError(
            f"direction_degrees must be finite and in [0, 360) at nonzero speed, got "
            f"{direction_degrees!r}"
        )
    radians = math.radians(direction_degrees)
    u = -speed_m_s * math.sin(radians)
    v = -speed_m_s * math.cos(radians)
    return u, v


def convert_pop_percent_to_fraction(percent: float) -> float:
    """Section 2.3: PoP01 is reported as percent and converted once to
    a fraction by dividing by 100. Rejects a value outside ``[0, 100]``."""
    if not math.isfinite(percent) or not (0.0 <= percent <= 100.0):
        raise ValueError(f"PoP percent must be finite and in [0, 100], got {percent!r}")
    return percent / 100.0
