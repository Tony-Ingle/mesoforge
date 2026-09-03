"""Deterministic synthetic NBM-like *projected Lambert* GRIB2 message
generation for Phase 2 tests (plan Section 5.3 / Task 3 step 1).

Codex re-review finding 2: the previous fixture emitted a
``regular_ll`` geographic mesh, which is not the NBM contract at all --
the operational NBM CONUS core product is a Lambert conformal conic
projected grid. Testing decoding/normalization against a lat/lon mesh
could never detect that production accepted the wrong projection.

These fixtures now emit the registered
``nbm-core-conus-fixture.v1`` grid profile
(``catalog.grid_profiles.NBM_CONUS_FIXTURE_GRID_PROFILE``): the exact
operational projection parameters (LoV/LaD/Latin1/Latin2, spherical
earth radius 6371200 m) and scan flags, at a reduced shape/increment so
eccodes fixtures stay small while still covering the whole configured
Grasston bbox plus a halo.

``mutate_grid_keys`` deliberately produces contract-violating variants
(wrong projection, shape, increment, scan order, or coverage) so tests
can prove each one is rejected independently.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from mesoforge.catalog.grid_profiles import NBM_CONUS_FIXTURE_GRID_PROFILE

PROFILE = NBM_CONUS_FIXTURE_GRID_PROFILE
NX = PROFILE.nx
NY = PROFILE.ny


def _grid_keys() -> dict[str, Any]:
    """The exact projected grid keys the fixture profile declares."""
    return {
        "gridType": "lambert",
        "Nx": PROFILE.nx,
        "Ny": PROFILE.ny,
        "DxInMetres": PROFILE.dx_metres,
        "DyInMetres": PROFILE.dy_metres,
        "LoVInDegrees": PROFILE.lov_degrees,
        "LaDInDegrees": PROFILE.lad_degrees,
        "Latin1InDegrees": PROFILE.latin1_degrees,
        "Latin2InDegrees": PROFILE.latin2_degrees,
        "latitudeOfFirstGridPointInDegrees": PROFILE.first_latitude_degrees,
        "longitudeOfFirstGridPointInDegrees": PROFILE.first_longitude_degrees,
        "iScansNegatively": PROFILE.i_scans_negatively,
        "jScansPositively": PROFILE.j_scans_positively,
        "jPointsAreConsecutive": PROFILE.j_points_are_consecutive,
    }


def mutate_grid_keys(**mutations: Any) -> dict[str, Any]:
    """Return the fixture grid keys with specific values replaced.

    Used by the independent mutation probes: each mutation makes the
    emitted message contradict exactly one clause of the approved grid
    contract (projection, shape, increment, scan order, or coverage).
    """
    keys = _grid_keys()
    unknown = set(mutations) - set(keys)
    if unknown:
        raise ValueError(f"unknown grid key(s) to mutate: {sorted(unknown)!r}")
    keys.update(mutations)
    return keys


def _base_message(
    *,
    forecast_hour: int,
    cycle_date: str,
    cycle_hour: int,
    grid_keys: dict[str, Any] | None = None,
) -> int:
    import eccodes

    keys = _grid_keys() if grid_keys is None else dict(grid_keys)
    grid_type = keys.pop("gridType")
    # The eccodes memfs distribution ships only the regular_ll sample;
    # HRRR fixtures already build their Lambert grid the same way, by
    # setting ``gridType`` on that sample before any grid key.
    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(gid, "centre", "kwbc")
    eccodes.codes_set(gid, "gridType", grid_type)
    if grid_type == "regular_ll":
        # Only used by the wrong-projection mutation probe: a geographic
        # mesh carries increments in degrees, not metres.
        increment = 0.25
        nx_ll = int(keys["Nx"])
        ny_ll = int(keys["Ny"])
        first_lat_ll = float(keys["latitudeOfFirstGridPointInDegrees"])
        first_lon_ll = float(keys["longitudeOfFirstGridPointInDegrees"])
        eccodes.codes_set(gid, "Ni", nx_ll)
        eccodes.codes_set(gid, "Nj", ny_ll)
        eccodes.codes_set(gid, "iDirectionIncrementInDegrees", increment)
        eccodes.codes_set(gid, "jDirectionIncrementInDegrees", increment)
        eccodes.codes_set(gid, "jScansPositively", int(keys["jScansPositively"]))
        eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", first_lat_ll)
        eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", first_lon_ll)
        eccodes.codes_set(
            gid, "latitudeOfLastGridPointInDegrees", first_lat_ll + increment * (ny_ll - 1)
        )
        eccodes.codes_set(
            gid, "longitudeOfLastGridPointInDegrees", first_lon_ll + increment * (nx_ll - 1)
        )
    else:
        eccodes.codes_set(gid, "shapeOfTheEarth", 1)
        eccodes.codes_set(gid, "scaleFactorOfRadiusOfSphericalEarth", 0)
        eccodes.codes_set(
            gid, "scaledValueOfRadiusOfSphericalEarth", int(PROFILE.earth_radius_metres)
        )
        integer_keys = ("Nx", "Ny", "iScansNegatively", "jScansPositively", "jPointsAreConsecutive")
        for name, value in keys.items():
            if name in integer_keys:
                eccodes.codes_set(gid, name, int(value))
            else:
                eccodes.codes_set(gid, name, float(value))
    eccodes.codes_set(gid, "discipline", 0)
    eccodes.codes_set(gid, "dataDate", int(cycle_date))
    eccodes.codes_set(gid, "dataTime", cycle_hour * 100)
    eccodes.codes_set(gid, "step", forecast_hour)
    return gid


_INSTANTANEOUS_FIELD_MAP: dict[str, tuple[int, int, str, int, str]] = {
    "air_temperature_2m": (0, 0, "heightAboveGround", 2, "K"),
    "dew_point_temperature_2m": (0, 6, "heightAboveGround", 2, "K"),
    "wind_speed_10m": (2, 1, "heightAboveGround", 10, "m s-1"),
    "wind_from_direction_10m": (2, 0, "heightAboveGround", 10, "degree"),
    "wind_gust_10m": (2, 22, "heightAboveGround", 10, "m s-1"),
}


def make_instantaneous_message(
    *,
    canonical_variable_id: str,
    forecast_hour: int,
    values: np.ndarray,
    cycle_date: str = "20260830",
    cycle_hour: int = 12,
    grid_keys: dict[str, Any] | None = None,
) -> bytes:
    import eccodes

    category, number, type_of_level, level, _units = _INSTANTANEOUS_FIELD_MAP[canonical_variable_id]
    gid = _base_message(
        forecast_hour=forecast_hour,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        grid_keys=grid_keys,
    )
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
    grid_keys: dict[str, Any] | None = None,
) -> bytes:
    import eccodes

    gid = _base_message(
        forecast_hour=forecast_hour,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        grid_keys=grid_keys,
    )
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
    grid_keys: dict[str, Any] | None = None,
    probability_type: int = 1,
) -> bytes:
    import eccodes

    gid = _base_message(
        forecast_hour=forecast_hour,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        grid_keys=grid_keys,
    )
    try:
        eccodes.codes_set(gid, "typeOfLevel", "surface")
        eccodes.codes_set(gid, "level", 0)
        eccodes.codes_set(gid, "parameterCategory", 1)
        eccodes.codes_set(gid, "parameterNumber", 8)
        eccodes.codes_set(gid, "stepType", "accum")
        eccodes.codes_set(gid, "startStep", forecast_hour - 1)
        eccodes.codes_set(gid, "endStep", forecast_hour)
        eccodes.codes_set(gid, "productDefinitionTemplateNumber", 9)
        # Match the operational NBM core PoP01 encoding exactly: Code
        # Table 4.9 value 1, "Probability of event above upper limit",
        # with the >0.254 kg/m^2 threshold in the upper-limit keys.
        # ``probability_type`` is overridable only so a test can build a
        # deliberately wrong-typed decoy; production NBM is always 1.
        eccodes.codes_set(gid, "probabilityType", probability_type)
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
