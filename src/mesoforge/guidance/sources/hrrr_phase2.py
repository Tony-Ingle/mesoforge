"""HRRR Phase 2 field selector construction (plan Section 2.2, Task 5).

Extends the Phase 1 fixed 3-field HRRR contract with the additional
Phase 2 fields (dew point, gust, one-hour QPF) and the +36-hour
extended-cycle lead range. Distinct from Phase 1's
``guidance.sources.hrrr`` (which remains locked to the exact 0..6
7-lead Phase 1 contract) -- this module is the Phase 2-specific
selector builder used against ``HrrrPhase2SourceSettings``.
"""

from __future__ import annotations

_INSTANTANEOUS_SELECTOR_FRAGMENTS: dict[str, str] = {
    "air_temperature_2m": ":TMP:2 m above ground:",
    "dew_point_temperature_2m": ":DPT:2 m above ground:",
    "eastward_wind_10m": ":UGRD:10 m above ground:",
    "northward_wind_10m": ":VGRD:10 m above ground:",
    "wind_gust_10m": ":GUST:surface:",
}


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
