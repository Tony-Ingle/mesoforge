"""NBM CONUS core GRIB2 decoding (plan Section 2.3, Task 3).

Reuses the same cfgrib-backed decode strategy as HRRR
(``guidance.decoding``): a temp file, ``indexpath=""`` (no persisted
``.idx`` cache), and strict semantic-key assertion before any decoded
array is trusted. NBM decoding additionally must reject any decoded
ensemble standard-deviation/percentile/QMD-shaped record and any
non-deterministic APCP record other than the exact PoP01 probability
contract.

Codex review (Phase 2 remediation, finding 2): failing open on level,
units, interval window, PoP01 threshold/scaling/probability identity,
projection/grid keys, cycle/valid time, and array shape let a message
carrying the wrong physical field or the wrong grid pass silently.
Every one of those keys is now asserted here, matching the strictness
HRRR's ``guidance.decoding`` already applies.

Codex re-review (finding 2): the grid clause previously accepted *any*
nonempty ``gridType`` and *any* positive ``Nx``/``Ny``, so a message on
an entirely different projection, shape, or domain still passed. The
decoder now asserts the exact approved operational grid contract
declared by ``NbmSourceSettings.grid_profile``
(``catalog.grid_profiles``): projection/grid type, exact shape, exact
increments, every projection parameter, the earth figure, the scan
flags, and first/last-point geographic coverage derived from the
message's own declared geometry.
"""

from __future__ import annotations

import math
import tempfile
import warnings
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import xarray as xr

from mesoforge.catalog.grid_profiles import NbmGridProfile
from mesoforge.catalog.sources import NbmSourceSettings, Phase2FieldContract
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.nbm_geometry import (
    EARTH_RADIUS_TOLERANCE_M as _EARTH_RADIUS_TOLERANCE_M,
)
from mesoforge.guidance.nbm_geometry import (
    GRID_ANGLE_TOLERANCE_DEGREES as _GRID_ANGLE_TOLERANCE_DEGREES,
)
from mesoforge.guidance.nbm_geometry import (
    compute_last_grid_point,
)
from mesoforge.guidance.nbm_geometry import (
    wrap_longitude_0_360 as _wrap_longitude_0_360,
)

_BACKEND_KWARGS_TEMPLATE: dict[str, Any] = {"indexpath": "", "errors": "raise"}

# Raw eccodes/cfgrib ``GRIB_units`` strings (compared case-insensitively)
# accepted for each controlled MesoForge unit ID used by an NBM field
# contract. Any other decoded ``GRIB_units`` value is a terminal
# semantic mismatch. PoP01's underlying GRIB2 parameter is APCP
# (Total Precipitation, physical unit kg/m^2); its PDT 4.9 probability
# wrapper is expected to report the probability's own "%" unit, but
# not every eccodes/table build overrides the base parameter's units
# for a probability-templated record, so both are accepted here -- the
# threshold/scale/probabilityType assertion below is PoP01's real
# semantic anchor, not the reported unit string.
_GRIB_UNITS_BY_EXPECTED_UNIT: dict[str, frozenset[str]] = {
    "K": frozenset({"k"}),
    "m/s": frozenset({"m s**-1", "m s-1"}),
    "degree": frozenset({"degree true", "degrees", "degree"}),
    "kg/m^2": frozenset({"kg m**-2", "kg m-2"}),
    "percent": frozenset({"%", "kg m**-2", "kg m-2"}),
}

# NBM PoP01's exact probability-forecast identity (plan Section 2.3).
#
# The operational NBM core PoP01 record is GRIB2 product definition
# template 4.9 with ``probabilityType = 1`` -- eccodes resolves that
# Code Table 4.9 entry to ``"Probability of event above upper limit"``,
# which is precisely the semantics of the inventory's ``prob >0.254``:
# the probability that accumulation *exceeds* the upper limit encoded by
# ``scaledValueOfUpperLimit``/``scaleFactorOfUpperLimit`` (254 / 10**3 =
# 0.254 kg/m^2). The threshold keys are asserted separately below
# against ``NbmSourceSettings.probability_threshold_kg_m2``, so the
# physical field is pinned by its own GRIB semantics and not merely by
# the inventory text.
_POP_PROBABILITY_TYPE = 1
_POP_SCALE_FACTOR = 3


class NbmDecodeError(MesoForgeError):
    """Raised when a decoded NBM message fails semantic assertion, is
    ambiguous, or matches a disallowed ensemble/percentile/QMD-shaped
    record."""


def cfgrib_backend_kwargs(read_keys: tuple[str, ...]) -> dict[str, Any]:
    kwargs = dict(_BACKEND_KWARGS_TEMPLATE)
    kwargs["read_keys"] = list(read_keys)
    return kwargs


def decode_selected_message(
    payload: bytes,
    *,
    contract: Phase2FieldContract,
    settings: NbmSourceSettings,
    forecast_hour: int,
    cycle_date: date,
    cycle_hour: int,
) -> xr.DataArray:
    """Decode exactly one selected NBM GRIB2 message and assert its
    keys/units/step/level/interval/grid/cycle/valid-time against
    ``contract``/``settings``. Never decodes an ensemble std-dev/
    percentile/QMD record: ``probabilityType``/percentile keys that
    indicate a non-deterministic, non-PoP01 record are rejected."""
    backend_kwargs = cfgrib_backend_kwargs(settings.read_keys)

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)

    try:
        import cfgrib

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", category=FutureWarning, module=r"cfgrib\.xarray_store"
            )
            datasets = cfgrib.open_datasets(str(temp_path), backend_kwargs=backend_kwargs)
            datasets = [dataset.load() for dataset in datasets]
    finally:
        temp_path.unlink(missing_ok=True)

    candidates: list[xr.DataArray] = []
    for dataset in datasets:
        for data_var in dataset.data_vars.values():
            if _matches_contract(data_var, contract):
                candidates.append(_with_dataset_coords(data_var, dataset))

    if len(candidates) == 0:
        raise NbmDecodeError(
            f"no decoded NBM message matches field contract for {contract.canonical_variable_id!r}"
        )
    if len(candidates) > 1:
        raise NbmDecodeError(
            f"ambiguous decoded NBM messages for {contract.canonical_variable_id!r}: "
            f"{len(candidates)} candidates matched"
        )
    data_array = candidates[0]
    _assert_matches(
        data_array,
        contract,
        settings=settings,
        forecast_hour=forecast_hour,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
    )
    return data_array


def _matches_contract(data_array: xr.DataArray, contract: Phase2FieldContract) -> bool:
    attrs = data_array.attrs
    if (
        attrs.get("GRIB_discipline") != contract.discipline
        or attrs.get("GRIB_parameterCategory") != contract.parameter_category
        or attrs.get("GRIB_parameterNumber") != contract.parameter_number
        or attrs.get("GRIB_typeOfLevel") != contract.type_of_level
    ):
        return False
    if contract.is_probability:
        return attrs.get("GRIB_probabilityType") is not None
    return attrs.get("GRIB_probabilityType") is None


def _with_dataset_coords(data_array: xr.DataArray, dataset: xr.Dataset) -> xr.DataArray:
    result = data_array.copy()
    for name in ("latitude", "longitude", "valid_time", "time", "step"):
        if name in dataset.coords and name not in result.coords:
            result = result.assign_coords({name: dataset.coords[name]})
    return result


def _require_finite(attrs: dict[str, Any], key: str, errors: list[str]) -> float | None:
    value = attrs.get(key)
    if value is None:
        errors.append(f"required grid key {key!r} is missing")
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        errors.append(f"required grid key {key!r} is not numeric, got {value!r}")
        return None
    if not math.isfinite(numeric):
        errors.append(f"required grid key {key!r} is not finite, got {value!r}")
        return None
    return numeric


def _assert_grid_profile(
    data_array: xr.DataArray, profile: NbmGridProfile, errors: list[str]
) -> None:
    """Assert one decoded NBM message against the exact approved
    operational grid/projection contract (Codex re-review finding 2).

    Every clause below independently fails closed: projection/grid type,
    exact shape, exact increments, every projection parameter, the earth
    figure, the scan flags, and the first/last-point geographic
    coverage. Coverage is checked by projecting the declared first point
    into the profile's own CRS, walking the declared shape/increments,
    and inverse-projecting the far corner -- so a message that mutates
    any projection parameter, increment, or dimension contradicts the
    pinned corner even when each key looks individually plausible.
    """
    attrs = data_array.attrs

    grid_type = attrs.get("GRIB_gridType")
    if grid_type != profile.grid_type:
        errors.append(
            f"grid type mismatch: expected exactly {profile.grid_type!r} for approved profile "
            f"{profile.profile_id!r}, got {grid_type!r}"
        )
        # Every remaining clause reads projected-grid keys a geographic
        # mesh does not carry; reporting them all would bury the cause.
        return

    nx = attrs.get("GRIB_Nx")
    ny = attrs.get("GRIB_Ny")
    if nx != profile.nx or ny != profile.ny:
        errors.append(
            f"grid shape mismatch: expected exactly Nx={profile.nx!r}, Ny={profile.ny!r} for "
            f"approved profile {profile.profile_id!r}, got Nx={nx!r}, Ny={ny!r}"
        )
    if data_array.shape[-2:] != profile.shape:
        errors.append(
            f"decoded array shape {data_array.shape[-2:]!r} does not match the approved grid "
            f"shape {profile.shape!r} (Ny, Nx)"
        )

    for key, expected, tolerance in (
        ("GRIB_DxInMetres", profile.dx_metres, profile.increment_tolerance_metres),
        ("GRIB_DyInMetres", profile.dy_metres, profile.increment_tolerance_metres),
    ):
        actual = _require_finite(attrs, key, errors)
        if actual is not None and abs(actual - expected) > tolerance:
            errors.append(
                f"grid increment mismatch: expected {key}={expected!r} m within "
                f"{tolerance!r} m, got {actual!r}"
            )

    for key, expected in (
        ("GRIB_LoVInDegrees", profile.lov_degrees),
        ("GRIB_LaDInDegrees", profile.lad_degrees),
        ("GRIB_Latin1InDegrees", profile.latin1_degrees),
        ("GRIB_Latin2InDegrees", profile.latin2_degrees),
    ):
        actual = _require_finite(attrs, key, errors)
        if actual is not None and abs(actual - expected) > _GRID_ANGLE_TOLERANCE_DEGREES:
            errors.append(
                f"projection parameter mismatch: expected {key}={expected!r} within "
                f"{_GRID_ANGLE_TOLERANCE_DEGREES!r} degrees, got {actual!r}"
            )

    radius = _require_finite(attrs, "GRIB_radius", errors)
    if radius is not None and abs(radius - profile.earth_radius_metres) > _EARTH_RADIUS_TOLERANCE_M:
        errors.append(
            f"earth figure mismatch: expected a spherical earth of radius "
            f"{profile.earth_radius_metres!r} m within {_EARTH_RADIUS_TOLERANCE_M!r} m, "
            f"got {radius!r}"
        )

    for key, expected in (
        ("GRIB_iScansNegatively", profile.i_scans_negatively),
        ("GRIB_jScansPositively", profile.j_scans_positively),
        ("GRIB_jPointsAreConsecutive", profile.j_points_are_consecutive),
    ):
        actual_flag = attrs.get(key)
        if actual_flag != expected:
            errors.append(
                f"scan flag mismatch: expected {key}={expected!r} for approved profile "
                f"{profile.profile_id!r}, got {actual_flag!r}"
            )

    first_lat = _require_finite(attrs, "GRIB_latitudeOfFirstGridPointInDegrees", errors)
    first_lon = _require_finite(attrs, "GRIB_longitudeOfFirstGridPointInDegrees", errors)
    if first_lat is None or first_lon is None:
        return
    if abs(first_lat - profile.first_latitude_degrees) > profile.coordinate_tolerance_degrees or (
        abs(_wrap_longitude_0_360(first_lon) - profile.first_longitude_degrees)
        > profile.coordinate_tolerance_degrees
    ):
        errors.append(
            "first grid point mismatch: expected "
            f"({profile.first_latitude_degrees!r}, {profile.first_longitude_degrees!r}) within "
            f"{profile.coordinate_tolerance_degrees!r} degrees, got ({first_lat!r}, "
            f"{_wrap_longitude_0_360(first_lon)!r})"
        )
        return

    # Last-point coverage, derived from the message's own declared
    # shape/increments/projection rather than from the profile, so a
    # wrong shape or increment produces a wrong corner here even if the
    # individual keys were not separately compared.
    declared_nx = nx if isinstance(nx, int) and nx > 0 else profile.nx
    declared_ny = ny if isinstance(ny, int) and ny > 0 else profile.ny
    declared_dx = attrs.get("GRIB_DxInMetres", profile.dx_metres)
    declared_dy = attrs.get("GRIB_DyInMetres", profile.dy_metres)
    try:
        last_lat, last_lon = compute_last_grid_point(
            profile,
            nx=int(declared_nx),
            ny=int(declared_ny),
            dx_metres=float(declared_dx),
            dy_metres=float(declared_dy),
            first_latitude_degrees=first_lat,
            first_longitude_degrees=first_lon,
        )
    except (TypeError, ValueError) as exc:
        errors.append(f"could not derive last grid point coverage: {exc}")
        return
    if abs(last_lat - profile.last_latitude_degrees) > profile.coordinate_tolerance_degrees or (
        abs(last_lon - profile.last_longitude_degrees) > profile.coordinate_tolerance_degrees
    ):
        errors.append(
            "grid coverage mismatch: the declared projection/shape/increments place the last "
            f"grid point at ({last_lat!r}, {last_lon!r}), but approved profile "
            f"{profile.profile_id!r} requires ({profile.last_latitude_degrees!r}, "
            f"{profile.last_longitude_degrees!r}) within "
            f"{profile.coordinate_tolerance_degrees!r} degrees"
        )


def _assert_matches(
    data_array: xr.DataArray,
    contract: Phase2FieldContract,
    *,
    settings: NbmSourceSettings,
    forecast_hour: int,
    cycle_date: date,
    cycle_hour: int,
) -> None:
    attrs = data_array.attrs
    errors: list[str] = []

    raw_step = attrs.get("GRIB_step")
    try:
        step_matches = raw_step is not None and int(raw_step) == forecast_hour
    except (TypeError, ValueError):
        step_matches = False
    if not step_matches:
        errors.append(
            f"forecast lead mismatch: expected GRIB_step={forecast_hour!r}, got {raw_step!r}"
        )

    # -- exact level (finding 2) -------------------------------------
    raw_level = attrs.get("GRIB_level")
    try:
        level_matches = raw_level is not None and float(raw_level) == contract.level
    except (TypeError, ValueError):
        level_matches = False
    if not level_matches:
        errors.append(f"level mismatch: expected {contract.level!r}, got {raw_level!r}")

    # -- approved unit (finding 2) ------------------------------------
    expected_units = _GRIB_UNITS_BY_EXPECTED_UNIT.get(contract.expected_unit_id)
    if expected_units is None:
        errors.append(
            f"contract expected_unit_id {contract.expected_unit_id!r} has no known GRIB "
            "units mapping; this is a configuration error"
        )
    else:
        raw_units = attrs.get("GRIB_units")
        if not isinstance(raw_units, str) or raw_units.strip().lower() not in expected_units:
            errors.append(
                f"units mismatch: expected one of {sorted(expected_units)!r} for "
                f"{contract.expected_unit_id!r}, got GRIB_units={raw_units!r}"
            )

    if contract.is_accumulation:
        step_type = attrs.get("GRIB_stepType")
        if step_type != "accum":
            errors.append(f"expected accumulation stepType, got {step_type!r}")

        # -- exact start/end interval (finding 2) ----------------------
        expected_start = forecast_hour - 1
        raw_start = attrs.get("GRIB_startStep")
        raw_end = attrs.get("GRIB_endStep")
        try:
            interval_matches = (
                raw_start is not None
                and raw_end is not None
                and int(raw_start) == expected_start
                and int(raw_end) == forecast_hour
            )
        except (TypeError, ValueError):
            interval_matches = False
        if not interval_matches:
            errors.append(
                f"accumulation interval mismatch: expected startStep={expected_start!r}, "
                f"endStep={forecast_hour!r}, got startStep={raw_start!r}, endStep={raw_end!r}"
            )

    # Section 2.3/Task 3 step 1: never decode an ensemble standard-
    # deviation/percentile/QMD-shaped record for a deterministic
    # (non-PoP01) contract. Those products declare a derived-forecast
    # key (statistics-across-ensemble-members product definition) or a
    # percentile value that a genuine NBM deterministic core field
    # never sets; reject their presence explicitly rather than relying
    # only on discipline/category/number/typeOfLevel, which a
    # confounding ensemble product can share. ``typeOfStatisticalProcessing``
    # is deliberately excluded from this check: value 1 ("accumulation")
    # is a normal, expected key on every genuine deterministic APCP
    # record and must not be misclassified as a confounding signal.
    if not contract.is_probability:
        for confounding_key in ("GRIB_derivedForecast", "GRIB_percentileValue"):
            if attrs.get(confounding_key) is not None:
                errors.append(
                    f"decoded message for {contract.canonical_variable_id!r} carries "
                    f"{confounding_key}={attrs.get(confounding_key)!r}, indicating an "
                    "ensemble standard-deviation/percentile/QMD-shaped record; the "
                    "deterministic NBM core contract forbids this"
                )
    else:
        # -- PoP01 threshold/scaling/probability identity (finding 2) --
        expected_scaled_value = round(
            settings.probability_threshold_kg_m2 * (10**_POP_SCALE_FACTOR)
        )
        raw_probability_type = attrs.get("GRIB_probabilityType")
        raw_scaled_value = attrs.get("GRIB_scaledValueOfUpperLimit")
        raw_scale_factor = attrs.get("GRIB_scaleFactorOfUpperLimit")
        try:
            probability_identity_matches = (
                raw_probability_type is not None
                and int(raw_probability_type) == _POP_PROBABILITY_TYPE
                and raw_scaled_value is not None
                and int(raw_scaled_value) == expected_scaled_value
                and raw_scale_factor is not None
                and int(raw_scale_factor) == _POP_SCALE_FACTOR
            )
        except (TypeError, ValueError):
            probability_identity_matches = False
        if not probability_identity_matches:
            errors.append(
                "PoP01 probability identity mismatch: expected probabilityType="
                f"{_POP_PROBABILITY_TYPE!r}, scaledValueOfUpperLimit={expected_scaled_value!r}, "
                f"scaleFactorOfUpperLimit={_POP_SCALE_FACTOR!r} (threshold "
                f"{settings.probability_threshold_kg_m2!r} kg/m^2), got probabilityType="
                f"{raw_probability_type!r}, scaledValueOfUpperLimit={raw_scaled_value!r}, "
                f"scaleFactorOfUpperLimit={raw_scale_factor!r}"
            )

    # -- exact approved projected grid contract (finding 2) -----------
    _assert_grid_profile(data_array, settings.grid_profile, errors)

    # -- exact cycle/valid time (finding 2) ----------------------------
    expected_data_date = int(cycle_date.strftime("%Y%m%d"))
    expected_data_time = cycle_hour * 100
    if attrs.get("GRIB_dataDate") != expected_data_date:
        errors.append(
            f"cycle reference date mismatch: expected GRIB_dataDate={expected_data_date!r}, "
            f"got {attrs.get('GRIB_dataDate')!r}"
        )
    if attrs.get("GRIB_dataTime") != expected_data_time:
        errors.append(
            f"cycle reference time mismatch: expected GRIB_dataTime={expected_data_time!r}, "
            f"got {attrs.get('GRIB_dataTime')!r}"
        )
    cycle_dt = datetime(cycle_date.year, cycle_date.month, cycle_date.day, cycle_hour, tzinfo=UTC)
    valid_dt = cycle_dt + timedelta(hours=forecast_hour)
    expected_validity_date = int(valid_dt.strftime("%Y%m%d"))
    expected_validity_time = valid_dt.hour * 100 + valid_dt.minute
    if attrs.get("GRIB_validityDate") != expected_validity_date:
        errors.append(
            f"valid date mismatch: expected GRIB_validityDate={expected_validity_date!r}, "
            f"got {attrs.get('GRIB_validityDate')!r}"
        )
    if attrs.get("GRIB_validityTime") != expected_validity_time:
        errors.append(
            f"valid time mismatch: expected GRIB_validityTime={expected_validity_time!r}, "
            f"got {attrs.get('GRIB_validityTime')!r}"
        )

    if errors:
        raise NbmDecodeError(
            f"decoded NBM message for {contract.canonical_variable_id!r} failed semantic "
            f"assertion: {'; '.join(errors)}"
        )
