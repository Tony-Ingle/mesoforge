"""Deterministic synthetic GFS-like GRIB2 message generation for Phase
2 tests (mirrors ``tests/fixtures/nbm_grib.py``/``hrrr_grib.py``): a
tiny 0.25-degree regular lat/lon grid carrying the same discipline/
category/number identity as the real GFS pgrb2 product.

The grid is deliberately larger than the configured Grasston bbox: Phase
2 canonical guidance retains the domain bbox plus a one-cell
interpolation halo, so a fixture grid must be able to supply that halo
on every side or it exercises a code path the operational global grid
never takes.

It also scans **north-to-south** (``jScansPositively=0``), exactly like
the operational GFS 0.25-degree product: its first grid row is the
northernmost and its last is the southernmost. A fixture that scanned
south-to-north would silently hide a latitude-axis error that the real
product exposes.
"""

from __future__ import annotations

import numpy as np

NX = 18
NY = 16
DX_DEGREES = 0.25
# North-to-south scan: the first row is the northern edge.
FIRST_LAT_DEGREES = 47.25
LAST_LAT_DEGREES = FIRST_LAT_DEGREES - DX_DEGREES * (NY - 1)  # 43.5
FIRST_LON_DEGREES = 264.5  # -95.5


def _base_message(*, forecast_hour: int, cycle_date: str, cycle_hour: int) -> int:
    import eccodes

    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(gid, "centre", "kwbc")
    eccodes.codes_set(gid, "gridType", "regular_ll")
    eccodes.codes_set(gid, "Ni", NX)
    eccodes.codes_set(gid, "Nj", NY)
    eccodes.codes_set(gid, "iDirectionIncrementInDegrees", DX_DEGREES)
    eccodes.codes_set(gid, "jDirectionIncrementInDegrees", DX_DEGREES)
    eccodes.codes_set(gid, "jScansPositively", 0)
    eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", FIRST_LAT_DEGREES)
    eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", FIRST_LON_DEGREES)
    eccodes.codes_set(gid, "latitudeOfLastGridPointInDegrees", LAST_LAT_DEGREES)
    eccodes.codes_set(
        gid, "longitudeOfLastGridPointInDegrees", FIRST_LON_DEGREES + DX_DEGREES * (NX - 1)
    )
    eccodes.codes_set(gid, "discipline", 0)
    eccodes.codes_set(gid, "dataDate", int(cycle_date))
    eccodes.codes_set(gid, "dataTime", cycle_hour * 100)
    eccodes.codes_set(gid, "step", forecast_hour)
    return gid


_INSTANTANEOUS_FIELD_MAP: dict[str, tuple[int, int, str, float]] = {
    "air_temperature_2m": (0, 0, "heightAboveGround", 2),
    "dew_point_temperature_2m": (0, 6, "heightAboveGround", 2),
    "eastward_wind_10m": (2, 2, "heightAboveGround", 10),
    "northward_wind_10m": (2, 3, "heightAboveGround", 10),
    "wind_gust_10m": (2, 22, "surface", 0),
}


def make_instantaneous_message(
    *,
    canonical_variable_id: str,
    forecast_hour: int,
    values: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 0,
    grid_relative_wind: bool = True,
) -> bytes:
    import eccodes

    category, number, type_of_level, level = _INSTANTANEOUS_FIELD_MAP[canonical_variable_id]
    gid = _base_message(forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", type_of_level)
        eccodes.codes_set(gid, "level", level)
        eccodes.codes_set(gid, "parameterCategory", category)
        eccodes.codes_set(gid, "parameterNumber", number)
        eccodes.codes_set(gid, "stepType", "instant")
        if canonical_variable_id in ("eastward_wind_10m", "northward_wind_10m"):
            eccodes.codes_set(gid, "uvRelativeToGrid", 1 if grid_relative_wind else 0)
        eccodes.codes_set_array(gid, "values", values.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


def make_apcp_message(
    *,
    start_step: int,
    end_step: int,
    values_kg_m2: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 0,
) -> bytes:
    import eccodes

    gid = _base_message(forecast_hour=end_step, cycle_date=cycle_date, cycle_hour=cycle_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", "surface")
        eccodes.codes_set(gid, "level", 0)
        eccodes.codes_set(gid, "parameterCategory", 1)
        eccodes.codes_set(gid, "parameterNumber", 8)
        eccodes.codes_set(gid, "stepType", "accum")
        eccodes.codes_set(gid, "startStep", start_step)
        eccodes.codes_set(gid, "endStep", end_step)
        eccodes.codes_set_array(gid, "values", values_kg_m2.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)
