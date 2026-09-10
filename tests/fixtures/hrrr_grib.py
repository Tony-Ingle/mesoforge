"""Deterministic synthetic HRRR-shaped GRIB2 message generation for
Phase 1 tests (plan Section 5.3: 'GRIB2 fixture bytes are generated at
test time with eccodes from deterministic keys/values').

Produces a small Lambert Conformal Conic grid whose GRIB2 keys mirror
the real HRRR CONUS surface product's projection parameters (NCEP
spherical earth radius 6371229 m; LoV=262.5, Latin1=Latin2=LaD=38.5)
but at a tiny grid size so tests run fast.
"""

from __future__ import annotations

import numpy as np

LOV_DEGREES = 262.5
LAD_DEGREES = 38.5
LATIN1_DEGREES = 38.5
LATIN2_DEGREES = 38.5
FIRST_LAT_DEGREES = 44.9
FIRST_LON_DEGREES = 265.7  # -94.3; covers the configured Grasston bbox plus halo
DX_M = 3000.0
DY_M = 3000.0
NX = 80
NY = 70


def _base_message(*, forecast_hour: int, cycle_date: str = "20260828", cycle_hour: int = 18) -> int:
    import eccodes

    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(gid, "centre", "kwbc")
    eccodes.codes_set(gid, "gridType", "lambert")
    eccodes.codes_set(gid, "Nx", NX)
    eccodes.codes_set(gid, "Ny", NY)
    eccodes.codes_set(gid, "DxInMetres", DX_M)
    eccodes.codes_set(gid, "DyInMetres", DY_M)
    eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", FIRST_LAT_DEGREES)
    eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", FIRST_LON_DEGREES)
    eccodes.codes_set(gid, "LoVInDegrees", LOV_DEGREES)
    eccodes.codes_set(gid, "Latin1InDegrees", LATIN1_DEGREES)
    eccodes.codes_set(gid, "Latin2InDegrees", LATIN2_DEGREES)
    eccodes.codes_set(gid, "LaDInDegrees", LAD_DEGREES)
    eccodes.codes_set(gid, "discipline", 0)
    eccodes.codes_set(gid, "stepType", "instant")
    eccodes.codes_set(gid, "dataDate", int(cycle_date))
    eccodes.codes_set(gid, "dataTime", cycle_hour * 100)
    eccodes.codes_set(gid, "step", forecast_hour)
    return gid


def make_temperature_message(
    *,
    forecast_hour: int,
    values_k: np.ndarray,
    cycle_date: str = "20260828",
    cycle_hour: int = 18,
) -> bytes:
    import eccodes

    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "heightAboveGround")
        eccodes.codes_set(gid, "level", 2)
        eccodes.codes_set(gid, "parameterCategory", 0)
        eccodes.codes_set(gid, "parameterNumber", 0)
        eccodes.codes_set_array(gid, "values", values_k.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_wind_message(
    *,
    forecast_hour: int,
    component: str,
    values_m_s: np.ndarray,
    grid_relative: bool,
    cycle_date: str = "20260828",
    cycle_hour: int = 18,
) -> bytes:
    import eccodes

    parameter_number = 2 if component == "u" else 3
    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "heightAboveGround")
        eccodes.codes_set(gid, "level", 10)
        eccodes.codes_set(gid, "parameterCategory", 2)
        eccodes.codes_set(gid, "parameterNumber", parameter_number)
        eccodes.codes_set(gid, "uvRelativeToGrid", 1 if grid_relative else 0)
        eccodes.codes_set_array(gid, "values", values_m_s.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_dew_point_message(
    *,
    forecast_hour: int,
    values_k: np.ndarray,
    cycle_date: str = "20260828",
    cycle_hour: int = 18,
) -> bytes:
    import eccodes

    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "heightAboveGround")
        eccodes.codes_set(gid, "level", 2)
        eccodes.codes_set(gid, "parameterCategory", 0)
        eccodes.codes_set(gid, "parameterNumber", 6)
        eccodes.codes_set_array(gid, "values", values_k.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_gust_message(
    *,
    forecast_hour: int,
    values_m_s: np.ndarray,
    cycle_date: str = "20260828",
    cycle_hour: int = 18,
) -> bytes:
    import eccodes

    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "surface")
        eccodes.codes_set(gid, "level", 0)
        eccodes.codes_set(gid, "parameterCategory", 2)
        eccodes.codes_set(gid, "parameterNumber", 22)
        eccodes.codes_set_array(gid, "values", values_m_s.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_apcp_message(
    *,
    forecast_hour: int,
    values_kg_m2: np.ndarray,
    cycle_date: str = "20260828",
    cycle_hour: int = 18,
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
