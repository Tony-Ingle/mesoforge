"""NOAA GFS 0.25-degree pgrb2 provider adapter (plan Section 2.4, Task 4).

Pure URL construction and per-lead inventory selector construction --
no I/O. GFS's global regular lat/lon grid uses source longitudes in
``[0, 360)``; this module never converts them (that conversion for
coordinate lookup only happens in normalization, plan Section 2.5).
"""

from __future__ import annotations

from datetime import date

from mesoforge.catalog.sources import GfsSourceSettings
from mesoforge.guidance.precipitation import compute_bucket_start

_INSTANTANEOUS_SELECTOR_FRAGMENTS: dict[str, str] = {
    "air_temperature_2m": ":TMP:2 m above ground:",
    "dew_point_temperature_2m": ":DPT:2 m above ground:",
    "eastward_wind_10m": ":UGRD:10 m above ground:",
    "northward_wind_10m": ":VGRD:10 m above ground:",
    "wind_gust_10m": ":GUST:surface:",
}


def format_grib_filename(
    settings: GfsSourceSettings, *, cycle_hour: int, forecast_hour: int
) -> str:
    """Section 1.3: ``gfs.t{HH:02d}z.pgrb2.0p25.f{FFF:03d}``."""
    if not (0 <= cycle_hour <= 23):
        raise ValueError(f"cycle_hour must be in [0, 23], got {cycle_hour!r}")
    if forecast_hour < 0:
        raise ValueError(f"forecast_hour must be nonnegative, got {forecast_hour!r}")
    return settings.file_template.format(HH=cycle_hour, FFF=forecast_hour)


def build_grib_url(
    settings: GfsSourceSettings,
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
    settings: GfsSourceSettings,
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
    """Section 2.4: build the exact lead-dependent inventory selector
    for one GFS canonical variable. Instantaneous fields select on
    ``{lead} hour fcst``. APCP selects the bucket-accumulation record
    with the exact computed ``bucket_start-lead hour acc fcst`` text;
    the caller (acquisition) is responsible for retaining *both*
    candidate rows when ``forecast_hour <= 6`` (duplicate bucket/
    continuous inventory text) per ``select_field_rows``."""
    if forecast_hour < 1:
        raise ValueError(f"GFS forecast_hour must be >= 1, got {forecast_hour!r}")
    step = f"{forecast_hour} hour fcst"

    if canonical_variable_id in _INSTANTANEOUS_SELECTOR_FRAGMENTS:
        fragment = _INSTANTANEOUS_SELECTOR_FRAGMENTS[canonical_variable_id]
        return f"{fragment}{step}:$"

    if canonical_variable_id == "liquid_equivalent_precipitation_amount_1h":
        bucket_start = compute_bucket_start(forecast_hour)
        return rf":APCP:surface:{bucket_start}-{forecast_hour} hour acc fcst:$"

    raise ValueError(f"unknown GFS canonical_variable_id {canonical_variable_id!r}")


def normalize_longitude_to_minus180_180(longitude_degrees: float) -> float:
    """Section 2.5: normalize a GFS source longitude (``[0, 360)``)
    to ``[-180, 180)`` *only for coordinate lookup*; the source
    longitude convention is retained unchanged in the canonical
    artifact's own lineage attributes."""
    wrapped = longitude_degrees % 360.0
    return wrapped - 360.0 if wrapped >= 180.0 else wrapped
