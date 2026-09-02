"""HRRR Phase 2 field selector construction (plan Section 2.2, Task 5).

Extends the Phase 1 fixed 3-field HRRR contract with the additional
Phase 2 fields (dew point, gust, one-hour QPF) and the +36-hour
extended-cycle lead range. Distinct from Phase 1's
``guidance.sources.hrrr`` (which remains locked to the exact 0..6
7-lead Phase 1 contract) -- this module is the Phase 2-specific
selector/URL builder used against ``HrrrPhase2SourceSettings``.
"""

from __future__ import annotations

from datetime import date

from mesoforge.catalog.sources import HrrrPhase2SourceSettings

_INSTANTANEOUS_SELECTOR_FRAGMENTS: dict[str, str] = {
    "air_temperature_2m": ":TMP:2 m above ground:",
    "dew_point_temperature_2m": ":DPT:2 m above ground:",
    "eastward_wind_10m": ":UGRD:10 m above ground:",
    "northward_wind_10m": ":VGRD:10 m above ground:",
    "wind_gust_10m": ":GUST:surface:",
}


def format_grib_filename(
    settings: HrrrPhase2SourceSettings, *, cycle_hour: int, forecast_hour: int
) -> str:
    """``hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2``."""
    if not (0 <= cycle_hour <= 23):
        raise ValueError(f"cycle_hour must be in [0, 23], got {cycle_hour!r}")
    if forecast_hour < 0:
        raise ValueError(f"forecast_hour must be nonnegative, got {forecast_hour!r}")
    return settings.file_template.format(HH=cycle_hour, FF=forecast_hour)


def build_grib_url(
    settings: HrrrPhase2SourceSettings,
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
    settings: HrrrPhase2SourceSettings,
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
    """Section 2.2: exact lead-dependent HRRR Phase 2 inventory
    selector. Instantaneous fields (temperature, dew point, U/V,
    gust) select on the exact step text (``anl`` at lead 0, else
    ``{lead} hour fcst``). QPF selects HRRR's direct rolling one-hour
    ``APCP`` record (``start=end-1``), never the run-total record."""
    if forecast_hour < 0:
        raise ValueError(f"forecast_hour must be nonnegative, got {forecast_hour!r}")
    step = "anl" if forecast_hour == 0 else f"{forecast_hour} hour fcst"

    if canonical_variable_id in _INSTANTANEOUS_SELECTOR_FRAGMENTS:
        fragment = _INSTANTANEOUS_SELECTOR_FRAGMENTS[canonical_variable_id]
        return f"{fragment}{step}:$"

    if canonical_variable_id == "liquid_equivalent_precipitation_amount_1h":
        if forecast_hour < 1:
            raise ValueError(
                "liquid_equivalent_precipitation_amount_1h requires forecast_hour >= 1 "
                f"(no accumulation is defined at lead 0), got {forecast_hour!r}"
            )
        start = forecast_hour - 1
        return rf":APCP:surface:{start}-{forecast_hour} hour acc fcst:$"

    raise ValueError(f"unknown HRRR Phase 2 canonical_variable_id {canonical_variable_id!r}")
