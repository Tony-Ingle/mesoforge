"""Deterministic synthetic NBM-like projected GRIB2 message generation
for Phase 2 tests (plan Section 5.3 / Task 3 step 1: 'Generate a
synthetic NBM-like projected fixture with every deterministic field
plus confounding std-dev/percentile/QMD-like records').
"""

from __future__ import annotations

import numpy as np

NX = 20
NY = 18
DX_DEGREES = 0.5
FIRST_LAT_DEGREES = 44.0
FIRST_LON_DEGREES = 265.0  # -95.0


def _base_message(*, forecast_hour: int, cycle_date: str, cycle_hour: int) -> int:
    import eccodes

    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(gid, "centre", "kwbc")
    eccodes.codes_set(gid, "gridType", "regular_ll")
    eccodes.codes_set(gid, "Ni", NX)
    eccodes.codes_set(gid, "Nj", NY)
    eccodes.codes_set(gid, "iDirectionIncrementInDegrees", DX_DEGREES)
    eccodes.codes_set(gid, "jDirectionIncrementInDegrees", DX_DEGREES)
    eccodes.codes_set(gid, "jScansPositively", 1)
    eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", FIRST_LAT_DEGREES)
    eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", FIRST_LON_DEGREES)
    eccodes.codes_set(
        gid, "latitudeOfLastGridPointInDegrees", FIRST_LAT_DEGREES + DX_DEGREES * (NY - 1)
    )
    eccodes.codes_set(
        gid, "longitudeOfLastGridPointInDegrees", FIRST_LON_DEGREES + DX_DEGREES * (NX - 1)
    )
    eccodes.codes_set(gid, "discipline", 0)
    eccodes.codes_set(gid, "dataDate", int(cycle_date))
    eccodes.codes_set(gid, "dataTime", cycle_hour * 100)
    eccodes.codes_set(gid, "step", forecast_hour)
    return gid


def make_instantaneous_message(
    *,
    canonical_variable_id: str,
    forecast_hour: int,
    values: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 12,
) -> bytes:
    import eccodes

    field_map = {
        "air_temperature_2m": (0, 0, "heightAboveGround", 2, "K"),
        "dew_point_temperature_2m": (0, 6, "heightAboveGround", 2, "K"),
        "wind_speed_10m": (2, 1, "heightAboveGround", 10, "m s-1"),
        "wind_from_direction_10m": (2, 0, "heightAboveGround", 10, "degree"),
        "wind_gust_10m": (2, 22, "heightAboveGround", 10, "m s-1"),
    }
    category, number, type_of_level, level, units = field_map[canonical_variable_id]
    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", type_of_level)
        eccodes.codes_set(gid, "level", level)
        eccodes.codes_set(gid, "parameterCategory", category)
        eccodes.codes_set(gid, "parameterNumber", number)
        eccodes.codes_set(gid, "stepType", "instant")
        eccodes.codes_set_array(gid, "values", values.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_apcp_deterministic_message(
    *,
    forecast_hour: int,
    values_kg_m2: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 12,
) -> bytes:
    import eccodes

    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "surface")
        eccodes.codes_set(gid, "level", 0)
        eccodes.codes_set(gid, "parameterCategory", 1)
        eccodes.codes_set(gid, "parameterNumber", 8)
        eccodes.codes_set(gid, "stepType", "accum")
        eccodes.codes_set(gid, "startStep", forecast_hour - 1)
        eccodes.codes_set(gid, "endStep", forecast_hour)
        eccodes.codes_set_array(gid, "values", values_kg_m2.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_pop01_message(
    *,
    forecast_hour: int,
    values_percent: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 12,
) -> bytes:
    import eccodes

    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "surface")
        eccodes.codes_set(gid, "level", 0)
        eccodes.codes_set(gid, "parameterCategory", 1)
        eccodes.codes_set(gid, "parameterNumber", 8)
        eccodes.codes_set(gid, "stepType", "accum")
        eccodes.codes_set(gid, "startStep", forecast_hour - 1)
        eccodes.codes_set(gid, "endStep", forecast_hour)
        eccodes.codes_set(gid, "productDefinitionTemplateNumber", 9)
        eccodes.codes_set(gid, "probabilityType", 3)
        eccodes.codes_set(gid, "scaledValueOfUpperLimit", 254)
        eccodes.codes_set(gid, "scaleFactorOfUpperLimit", 3)
        eccodes.codes_set_array(gid, "values", values_percent.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_confounding_temperature_stddev_message(
    *,
    forecast_hour: int,
    values: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 12,
) -> bytes:
    """A decoy record sharing temperature's discipline/category/number/
    typeOfLevel but tagged as an ensemble standard-deviation product
    (``productDefinitionTemplateNumber=2``, derived-forecast type 11 =
    standard deviation) -- NBM decode must never select this record for
    the deterministic ``air_temperature_2m`` contract (Task 3 step 1)."""
    import eccodes

    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "heightAboveGround")
        eccodes.codes_set(gid, "level", 2)
        eccodes.codes_set(gid, "parameterCategory", 0)
        eccodes.codes_set(gid, "parameterNumber", 0)
        eccodes.codes_set(gid, "stepType", "instant")
        eccodes.codes_set(gid, "productDefinitionTemplateNumber", 2)
        eccodes.codes_set(gid, "derivedForecast", 11)
        eccodes.codes_set_array(gid, "values", values.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)
