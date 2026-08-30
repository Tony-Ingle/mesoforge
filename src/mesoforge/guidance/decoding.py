"""cfgrib-backed decoding of pinned HRRR GRIB2 messages (plan Section
2.1, Task 5).

Decodes only the exact concatenated selected-message bytes acquired by
``guidance.acquisition`` -- never re-searches a remote inventory during
replay, and never persists a cfgrib ``.idx`` cache (``indexpath=""``).
Every semantic key asserted by the plan (discipline/category/number,
``typeOfLevel``/level, ``stepType``, exact forecast lead/cycle/valid
time, units, ``uvRelativeToGrid``, and the full projection/grid
definition) is checked against the configured ``HrrrFieldAssertion``
and the caller-supplied cycle/lead identity before the decoded array is
trusted (Codex review t_09a43c6c finding 2: fail-closed HRRR semantics
requires more than discipline/category/number/typeOfLevel/level).
"""

from __future__ import annotations

import math
import tempfile
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import xarray as xr

from mesoforge.catalog.sources import HrrrFieldAssertion, HrrrSourceSettings
from mesoforge.common.errors import MesoForgeError

_BACKEND_KWARGS_TEMPLATE: dict[str, Any] = {
    "indexpath": "",
    "errors": "raise",
}

# Phase 1's three canonical HRRR fields (2 m temperature, 10 m U/V) are
# all instantaneous analysis/forecast values -- never an accumulation,
# average, minimum, or maximum -- so a single expected GRIB stepType
# applies to every field assertion (mirrors
# ``guidance.normalization.assemble_canonical_hrrr_dataset``'s
# ``temporal_semantics: "instantaneous"`` attribute).
_EXPECTED_STEP_TYPE = "instant"

# eccodes/cfgrib's raw ``GRIB_units`` string for each controlled
# MesoForge unit ID used by Phase 1 field assertions. Only these two
# units ever appear in a Phase 1 field assertion; an assertion using
# any other ``expected_unit_id`` is a configuration bug caught here
# rather than silently accepted.
_GRIB_UNITS_BY_CANONICAL_ID: dict[str, frozenset[str]] = {
    "K": frozenset({"K"}),
    "m/s": frozenset({"m s**-1", "m s-1"}),
}

_WIND_VARIABLE_IDS = frozenset({"eastward_wind_10m", "northward_wind_10m"})

_GRID_FLOAT_TOLERANCE = 1e-6


class HrrrDecodeError(MesoForgeError):
    """Raised when decoded GRIB message keys do not match the
    configured field assertion, the caller-supplied cycle/lead
    identity, or a message fails to decode."""


def cfgrib_backend_kwargs(read_keys: tuple[str, ...]) -> dict[str, Any]:
    kwargs = dict(_BACKEND_KWARGS_TEMPLATE)
    kwargs["read_keys"] = list(read_keys)
    return kwargs


def decode_selected_messages(
    payload: bytes,
    *,
    settings: HrrrSourceSettings,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
) -> dict[str, xr.DataArray]:
    """Decode the exact concatenated selected-message bytes for one
    lead into ``{canonical_variable_id: xr.DataArray}``.

    ``cycle_date``/``cycle_hour``/``forecast_hour`` identify the exact
    HRRR cycle and lead this payload was acquired for (plan Section
    2.1/review finding 2: decoding must assert the decoded message's
    own reference/valid time actually matches the run's requested
    cycle and lead, not merely that *a* GRIB message decoded).

    cfgrib requires an on-disk file (it does not accept in-memory GRIB
    bytes); a ``NamedTemporaryFile`` is used, deleted immediately after
    decode, and no ``.idx`` cache is written (``indexpath=""``).
    """
    backend_kwargs = cfgrib_backend_kwargs(settings.read_keys)

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)

    try:
        import cfgrib

        # cfgrib's internal xr.merge() emits a FutureWarning about a
        # pending compat-default change (xarray >= 2026.x); this is a
        # cfgrib-internal implementation detail, not a Phase 1 scientific
        # concern -- suppressed narrowly here only, so a genuine warning
        # elsewhere in this process still surfaces (pyproject.toml's
        # filterwarnings=["error"] applies everywhere else).
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", category=FutureWarning, module=r"cfgrib\.xarray_store"
            )
            datasets = cfgrib.open_datasets(str(temp_path), backend_kwargs=backend_kwargs)
            # cfgrib/xarray keep a lazy on-disk reference to each
            # message; load eagerly before the temp file is deleted.
            datasets = [dataset.load() for dataset in datasets]
    finally:
        temp_path.unlink(missing_ok=True)

    decoded: dict[str, xr.DataArray] = {}
    signatures: dict[str, GridSignature] = {}
    for assertion in settings.field_assertions:
        data_array = _find_matching_array(datasets, assertion)
        _assert_matches(
            data_array,
            assertion,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
            forecast_hour=forecast_hour,
        )
        decoded[assertion.canonical_variable_id] = data_array
        signatures[assertion.canonical_variable_id] = GridSignature.from_data_array(data_array)

    # Grid consistency across temperature/U/V for this one lead (review
    # finding 2: "grid consistency across U/V/temperature ... before
    # artifact registration").
    assert_consistent_grid_signatures(signatures)

    return decoded


def _find_matching_array(datasets: list[xr.Dataset], assertion: HrrrFieldAssertion) -> xr.DataArray:
    candidates: list[xr.DataArray] = []
    for dataset in datasets:
        for data_var in dataset.data_vars.values():
            discipline = data_var.attrs.get("GRIB_discipline")
            category = data_var.attrs.get("GRIB_parameterCategory")
            number = data_var.attrs.get("GRIB_parameterNumber")
            type_of_level = data_var.attrs.get("GRIB_typeOfLevel")
            if (
                discipline == assertion.discipline
                and category == assertion.parameter_category
                and number == assertion.parameter_number
                and type_of_level == assertion.type_of_level
            ):
                candidates.append(data_array_with_dataset_coords(data_var, dataset))

    if len(candidates) == 0:
        raise HrrrDecodeError(
            f"no decoded message matches field assertion for "
            f"{assertion.canonical_variable_id!r} "
            f"(discipline={assertion.discipline}, category={assertion.parameter_category}, "
            f"number={assertion.parameter_number}, type_of_level={assertion.type_of_level!r})"
        )
    if len(candidates) > 1:
        raise HrrrDecodeError(
            f"ambiguous decoded messages for {assertion.canonical_variable_id!r}: "
            f"{len(candidates)} candidates matched the field assertion"
        )
    return candidates[0]


def data_array_with_dataset_coords(data_array: xr.DataArray, dataset: xr.Dataset) -> xr.DataArray:
    """cfgrib's per-message ``open_datasets`` list keeps latitude/
    longitude/level/valid_time as *dataset*-level coordinates; copy them
    onto the returned DataArray so downstream code has one
    self-contained object per canonical variable."""
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


def _assert_matches(
    data_array: xr.DataArray,
    assertion: HrrrFieldAssertion,
    *,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
) -> None:
    attrs = data_array.attrs
    errors: list[str] = []

    if attrs.get("GRIB_discipline") != assertion.discipline:
        errors.append(
            f"discipline mismatch: expected {assertion.discipline!r}, got "
            f"{attrs.get('GRIB_discipline')!r}"
        )
    if attrs.get("GRIB_parameterCategory") != assertion.parameter_category:
        errors.append(
            f"parameterCategory mismatch: expected {assertion.parameter_category!r}, got "
            f"{attrs.get('GRIB_parameterCategory')!r}"
        )
    if attrs.get("GRIB_parameterNumber") != assertion.parameter_number:
        errors.append(
            f"parameterNumber mismatch: expected {assertion.parameter_number!r}, got "
            f"{attrs.get('GRIB_parameterNumber')!r}"
        )
    if attrs.get("GRIB_typeOfLevel") != assertion.type_of_level:
        errors.append(
            f"typeOfLevel mismatch: expected {assertion.type_of_level!r}, got "
            f"{attrs.get('GRIB_typeOfLevel')!r}"
        )
    if float(attrs.get("GRIB_level", float("nan"))) != assertion.level:
        errors.append(
            f"level mismatch: expected {assertion.level!r}, got {attrs.get('GRIB_level')!r}"
        )

    # -- expected units (review finding 2) -------------------------------
    expected_grib_units = _GRIB_UNITS_BY_CANONICAL_ID.get(assertion.expected_unit_id)
    if expected_grib_units is None:
        errors.append(
            f"assertion expected_unit_id {assertion.expected_unit_id!r} has no known GRIB "
            "units mapping; this is a configuration error"
        )
    else:
        raw_units = attrs.get("GRIB_units")
        if raw_units not in expected_grib_units:
            errors.append(
                f"units mismatch: expected one of {sorted(expected_grib_units)!r} for "
                f"{assertion.expected_unit_id!r}, got GRIB_units={raw_units!r}"
            )

    # -- step type (review finding 2) ------------------------------------
    if attrs.get("GRIB_stepType") != _EXPECTED_STEP_TYPE:
        errors.append(
            f"stepType mismatch: expected {_EXPECTED_STEP_TYPE!r}, got "
            f"{attrs.get('GRIB_stepType')!r}"
        )

    # -- exact forecast lead/cycle/valid time (review finding 2) ---------
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
    raw_step = attrs.get("GRIB_step")
    try:
        step_matches = raw_step is not None and int(raw_step) == forecast_hour
    except (TypeError, ValueError):
        step_matches = False
    if not step_matches:
        errors.append(
            f"forecast lead mismatch: expected GRIB_step={forecast_hour!r}, got {raw_step!r}"
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

    # -- required uvRelativeToGrid shape/value (review finding 2) --------
    if assertion.canonical_variable_id in _WIND_VARIABLE_IDS:
        raw_uv = attrs.get("GRIB_uvRelativeToGrid")
        if raw_uv not in (0, 1):
            errors.append(
                f"uvRelativeToGrid mismatch for {assertion.canonical_variable_id!r}: "
                f"required exactly 0 or 1, got {raw_uv!r}"
            )

    # -- required projection/grid keys, dimensions/spacing/first point ---
    if attrs.get("GRIB_gridType") != "lambert":
        errors.append(f"gridType mismatch: expected 'lambert', got {attrs.get('GRIB_gridType')!r}")
    for int_key in ("GRIB_Nx", "GRIB_Ny"):
        raw_value = attrs.get(int_key)
        if not isinstance(raw_value, int) or raw_value <= 0:
            errors.append(
                f"required grid key {int_key!r} must be a positive integer, got {raw_value!r}"
            )
    for float_key in (
        "GRIB_DxInMetres",
        "GRIB_DyInMetres",
        "GRIB_latitudeOfFirstGridPointInDegrees",
        "GRIB_longitudeOfFirstGridPointInDegrees",
        "GRIB_LoVInDegrees",
        "GRIB_Latin1InDegrees",
        "GRIB_Latin2InDegrees",
        "GRIB_LaDInDegrees",
    ):
        _require_finite(attrs, float_key, errors)

    if errors:
        raise HrrrDecodeError(
            f"decoded message for {assertion.canonical_variable_id!r} failed semantic "
            f"assertion: {'; '.join(errors)}"
        )


@dataclass(frozen=True, slots=True)
class GridSignature:
    """The full set of projection/grid identity keys that must agree
    exactly across every decoded field within one lead, and across
    every lead within one run (plan Section 2.1/review finding 2:
    "grid consistency across U/V/temperature and all seven leads
    before artifact registration")."""

    grid_type: str
    nx: int
    ny: int
    dx_m: float
    dy_m: float
    first_lat_degrees: float
    first_lon_degrees: float
    lov_degrees: float
    latin1_degrees: float
    latin2_degrees: float
    lad_degrees: float

    @classmethod
    def from_data_array(cls, data_array: xr.DataArray) -> GridSignature:
        attrs = data_array.attrs
        return cls(
            grid_type=str(attrs.get("GRIB_gridType")),
            nx=int(attrs.get("GRIB_Nx", -1)),
            ny=int(attrs.get("GRIB_Ny", -1)),
            dx_m=float(attrs.get("GRIB_DxInMetres", math.nan)),
            dy_m=float(attrs.get("GRIB_DyInMetres", math.nan)),
            first_lat_degrees=float(attrs.get("GRIB_latitudeOfFirstGridPointInDegrees", math.nan)),
            first_lon_degrees=float(attrs.get("GRIB_longitudeOfFirstGridPointInDegrees", math.nan)),
            lov_degrees=float(attrs.get("GRIB_LoVInDegrees", math.nan)),
            latin1_degrees=float(attrs.get("GRIB_Latin1InDegrees", math.nan)),
            latin2_degrees=float(attrs.get("GRIB_Latin2InDegrees", math.nan)),
            lad_degrees=float(attrs.get("GRIB_LaDInDegrees", math.nan)),
        )

    def approximately_equal(self, other: GridSignature) -> bool:
        if self.grid_type != other.grid_type or self.nx != other.nx or self.ny != other.ny:
            return False
        for a, b in (
            (self.dx_m, other.dx_m),
            (self.dy_m, other.dy_m),
            (self.first_lat_degrees, other.first_lat_degrees),
            (self.first_lon_degrees, other.first_lon_degrees),
            (self.lov_degrees, other.lov_degrees),
            (self.latin1_degrees, other.latin1_degrees),
            (self.latin2_degrees, other.latin2_degrees),
            (self.lad_degrees, other.lad_degrees),
        ):
            if not math.isfinite(a) or not math.isfinite(b) or abs(a - b) > _GRID_FLOAT_TOLERANCE:
                return False
        return True


def assert_consistent_grid_signatures(signatures: dict[str, GridSignature]) -> None:
    """Raise ``HrrrDecodeError`` unless every signature in ``signatures``
    (keyed by canonical_variable_id, or by ``"lead{N}:{variable_id}"``
    for a cross-lead check) is identical to the first."""
    items = list(signatures.items())
    if len(items) < 2:
        return
    reference_key, reference_signature = items[0]
    mismatches = [
        key
        for key, signature in items[1:]
        if not signature.approximately_equal(reference_signature)
    ]
    if mismatches:
        raise HrrrDecodeError(
            f"grid signature is inconsistent across decoded messages: {reference_key!r} "
            f"disagrees with {mismatches!r}"
        )
