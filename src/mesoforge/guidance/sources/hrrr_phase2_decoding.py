"""HRRR Phase 2 (extended-cycle, 6-field) GRIB2 decoding (plan Section
2.2, Task 5).

Distinct from Phase 1's ``guidance.decoding`` (which is locked to the
exact 3-field, 0..6-lead Phase 1 ``HrrrSourceSettings`` contract) --
this module decodes against ``HrrrPhase2SourceSettings``'s six-field,
extended-lead ``Phase2FieldContract`` set, sharing the same cfgrib
strategy and Lambert-conformal grid-identity assertions as Phase 1's
decoder.
"""

from __future__ import annotations

import math
import tempfile
import warnings
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import xarray as xr

from mesoforge.catalog.sources import HrrrPhase2SourceSettings, Phase2FieldContract
from mesoforge.common.errors import MesoForgeError

_BACKEND_KWARGS_TEMPLATE: dict[str, Any] = {"indexpath": "", "errors": "raise"}

_GRIB_UNITS_BY_EXPECTED_UNIT: dict[str, frozenset[str]] = {
    "K": frozenset({"k"}),
    "m/s": frozenset({"m s**-1", "m s-1"}),
    "kg/m^2": frozenset({"kg m**-2", "kg m-2"}),
}

_WIND_VARIABLE_IDS = frozenset({"eastward_wind_10m", "northward_wind_10m"})


class HrrrPhase2DecodeError(MesoForgeError):
    """Raised when a decoded HRRR Phase 2 message fails semantic
    assertion or is ambiguous."""


def cfgrib_backend_kwargs(read_keys: tuple[str, ...]) -> dict[str, Any]:
    kwargs = dict(_BACKEND_KWARGS_TEMPLATE)
    kwargs["read_keys"] = list(read_keys)
    return kwargs


def decode_selected_message(
    payload: bytes,
    *,
    contract: Phase2FieldContract,
    settings: HrrrPhase2SourceSettings | None = None,
    read_keys: tuple[str, ...] | None = None,
    forecast_hour: int,
    cycle_date: date,
    cycle_hour: int,
) -> xr.DataArray:
    """Decode exactly one selected HRRR Phase 2 GRIB2 message and
    assert its keys/units/step/level/window/grid/cycle/valid-time
    against ``contract``."""
    # The same field/time/Lambert assertions also fit other projected models.
    # Explicit decoder keys avoid constructing unrelated HRRR source settings.
    if (settings is None) == (read_keys is None):
        raise ValueError("Supply exactly one of settings or read_keys")
    selected_read_keys = settings.read_keys if settings is not None else read_keys
    assert selected_read_keys is not None
    backend_kwargs = cfgrib_backend_kwargs(selected_read_keys)

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
        raise HrrrPhase2DecodeError(
            f"no decoded HRRR message matches field contract for {contract.canonical_variable_id!r}"
        )
    if len(candidates) > 1:
        raise HrrrPhase2DecodeError(
            f"ambiguous decoded HRRR messages for {contract.canonical_variable_id!r}: "
            f"{len(candidates)} candidates matched"
        )
    data_array = candidates[0]
    _assert_matches(
        data_array,
        contract,
        forecast_hour=forecast_hour,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
    )
    return data_array


def _matches_contract(data_array: xr.DataArray, contract: Phase2FieldContract) -> bool:
    attrs = data_array.attrs
    return (
        attrs.get("GRIB_discipline") == contract.discipline
        and attrs.get("GRIB_parameterCategory") == contract.parameter_category
        and attrs.get("GRIB_parameterNumber") == contract.parameter_number
        and attrs.get("GRIB_typeOfLevel") == contract.type_of_level
    )


def _with_dataset_coords(data_array: xr.DataArray, dataset: xr.Dataset) -> xr.DataArray:
    result = data_array.copy()
    for name in ("latitude", "longitude", "valid_time", "time", "step"):
        if name in dataset.coords and name not in result.coords:
            result = result.assign_coords({name: dataset.coords[name]})
    return result


def _require_finite(attrs: dict[str, Any], key: str, errors: list[str]) -> None:
    value = attrs.get(key)
    if value is None:
        errors.append(f"required grid key {key!r} is missing")
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        errors.append(f"required grid key {key!r} is not numeric, got {value!r}")
        return
    if not math.isfinite(numeric):
        errors.append(f"required grid key {key!r} is not finite, got {value!r}")


def _assert_matches(
    data_array: xr.DataArray,
    contract: Phase2FieldContract,
    *,
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

    raw_level = attrs.get("GRIB_level")
    try:
        level_matches = raw_level is not None and float(raw_level) == contract.level
    except (TypeError, ValueError):
        level_matches = False
    if not level_matches:
        errors.append(f"level mismatch: expected {contract.level!r}, got {raw_level!r}")

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
    else:
        step_type = attrs.get("GRIB_stepType")
        if step_type != "instant":
            errors.append(f"expected instantaneous stepType, got {step_type!r}")

    if contract.canonical_variable_id in _WIND_VARIABLE_IDS:
        raw_uv = attrs.get("GRIB_uvRelativeToGrid")
        if raw_uv not in (0, 1):
            errors.append(
                f"uvRelativeToGrid mismatch for {contract.canonical_variable_id!r}: "
                f"required exactly 0 or 1, got {raw_uv!r}"
            )

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
        raise HrrrPhase2DecodeError(
            f"decoded HRRR message for {contract.canonical_variable_id!r} failed semantic "
            f"assertion: {'; '.join(errors)}"
        )
