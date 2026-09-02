"""GFS 0.25-degree pgrb2 GRIB2 decoding (plan Section 2.4, Task 4).

Reuses the same cfgrib-backed decode strategy as HRRR/NBM
(``guidance.decoding``/``guidance.sources.nbm_decoding``): a temp
file, ``indexpath=""`` (no persisted ``.idx`` cache), and strict
semantic-key assertion (level, unit, step, cycle/valid time, grid
identity/shape, ``uvRelativeToGrid`` for wind fields, and the exact
APCP accumulation window) before any decoded array is trusted.

Codex review (Phase 2 remediation, finding 2 sibling for GFS): the
only production GFS decode was the pure ``ApcpCandidateRecord``
bucket/duplicate selection in ``guidance.precipitation``, with no
actual GRIB2 message decoder wired to produce those records from
bytes. This module is that missing decoder.
"""

from __future__ import annotations

import math
import tempfile
import warnings
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from mesoforge.catalog.sources import GfsSourceSettings, Phase2FieldContract
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.precipitation import ApcpCandidateRecord

_BACKEND_KWARGS_TEMPLATE: dict[str, Any] = {"indexpath": "", "errors": "raise"}

_GRIB_UNITS_BY_EXPECTED_UNIT: dict[str, frozenset[str]] = {
    "K": frozenset({"k"}),  # compared case-insensitively (lowercased raw_units)
    "m/s": frozenset({"m s**-1", "m s-1"}),
    "kg/m^2": frozenset({"kg m**-2", "kg m-2"}),
}

_WIND_VARIABLE_IDS = frozenset({"eastward_wind_10m", "northward_wind_10m"})


class GfsDecodeError(MesoForgeError):
    """Raised when a decoded GFS message fails semantic assertion or
    is ambiguous."""


def cfgrib_backend_kwargs(read_keys: tuple[str, ...]) -> dict[str, Any]:
    kwargs = dict(_BACKEND_KWARGS_TEMPLATE)
    kwargs["read_keys"] = list(read_keys)
    return kwargs


def _decode_all(payload: bytes, *, read_keys: tuple[str, ...]) -> list[xr.Dataset]:
    backend_kwargs = cfgrib_backend_kwargs(read_keys)
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
    return datasets


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


def decode_instantaneous_message(
    payload: bytes,
    *,
    contract: Phase2FieldContract,
    settings: GfsSourceSettings,
    forecast_hour: int,
    cycle_date: date,
    cycle_hour: int,
) -> xr.DataArray:
    """Decode one selected instantaneous GFS message (temperature, dew
    point, wind, gust) and assert its keys against ``contract``."""
    datasets = _decode_all(payload, read_keys=settings.read_keys)
    candidates: list[xr.DataArray] = []
    for dataset in datasets:
        for data_var in dataset.data_vars.values():
            if _matches_contract(data_var, contract):
                candidates.append(_with_dataset_coords(data_var, dataset))
    if len(candidates) == 0:
        raise GfsDecodeError(
            f"no decoded GFS message matches field contract for {contract.canonical_variable_id!r}"
        )
    if len(candidates) > 1:
        raise GfsDecodeError(
            f"ambiguous decoded GFS messages for {contract.canonical_variable_id!r}: "
            f"{len(candidates)} candidates matched"
        )
    data_array = candidates[0]
    _assert_instantaneous(
        data_array,
        contract,
        forecast_hour=forecast_hour,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
    )
    return data_array


def _assert_instantaneous(
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

    if attrs.get("GRIB_stepType") != "instant":
        errors.append(f"stepType mismatch: expected 'instant', got {attrs.get('GRIB_stepType')!r}")

    if contract.canonical_variable_id in _WIND_VARIABLE_IDS:
        raw_uv = attrs.get("GRIB_uvRelativeToGrid")
        if raw_uv not in (0, 1):
            errors.append(
                f"uvRelativeToGrid mismatch for {contract.canonical_variable_id!r}: "
                f"required exactly 0 or 1, got {raw_uv!r}"
            )

    errors.extend(
        _grid_and_cycle_errors(
            attrs, forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour
        )
    )

    if errors:
        raise GfsDecodeError(
            f"decoded GFS message for {contract.canonical_variable_id!r} failed semantic "
            f"assertion: {'; '.join(errors)}"
        )


def _grid_and_cycle_errors(
    attrs: dict[str, Any], *, forecast_hour: int, cycle_date: date, cycle_hour: int
) -> list[str]:
    errors: list[str] = []
    if attrs.get("GRIB_gridType") != "regular_ll":
        errors.append(
            f"gridType mismatch: expected 'regular_ll', got {attrs.get('GRIB_gridType')!r}"
        )
    for int_key in ("GRIB_Ni", "GRIB_Nj"):
        raw_value = attrs.get(int_key)
        if not isinstance(raw_value, int) or raw_value <= 0:
            errors.append(
                f"required grid key {int_key!r} must be a positive integer, got {raw_value!r}"
            )
    for float_key in (
        "GRIB_iDirectionIncrementInDegrees",
        "GRIB_jDirectionIncrementInDegrees",
        "GRIB_latitudeOfFirstGridPointInDegrees",
        "GRIB_longitudeOfFirstGridPointInDegrees",
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
    return errors


def decode_apcp_candidates(
    payloads: bytes | tuple[bytes, ...],
    *,
    contract: Phase2FieldContract,
    settings: GfsSourceSettings,
    forecast_hour: int,
    cycle_date: date,
    cycle_hour: int,
) -> tuple[ApcpCandidateRecord, ...]:
    """Decode every APCP candidate message into ``ApcpCandidateRecord``
    values, asserting level/unit/grid/cycle/valid-time semantics on
    each before trusting its values.

    ``payloads`` is either a single selected-message payload, or (for
    ``forecast_hour <= 6``, where the bucket and continuous-total
    inventory rows can be genuinely distinct byte ranges pointing at
    otherwise-identical decoded parameter identity) a tuple of the
    separately byte-range-fetched candidate payloads -- concatenating
    truly-identical GRIB2 messages into one payload is not decodable
    as distinct candidates (cfgrib collapses duplicate parameter
    identity within one file), so the caller must supply each
    selected row's payload as its own decode unit."""
    payload_tuple = (payloads,) if isinstance(payloads, bytes) else payloads
    records: list[ApcpCandidateRecord] = []
    for payload in payload_tuple:
        datasets = _decode_all(payload, read_keys=settings.read_keys)
        candidates: list[xr.DataArray] = []
        for dataset in datasets:
            for data_var in dataset.data_vars.values():
                if _matches_contract(data_var, contract):
                    candidates.append(_with_dataset_coords(data_var, dataset))
        if len(candidates) == 0:
            raise GfsDecodeError(
                "no decoded GFS message matches field contract for "
                f"{contract.canonical_variable_id!r}"
            )
        if len(candidates) > 1:
            raise GfsDecodeError(
                f"ambiguous decoded GFS APCP candidates within one payload for "
                f"{contract.canonical_variable_id!r}: {len(candidates)} candidates matched"
            )
        data_array = candidates[0]
        attrs = data_array.attrs
        errors: list[str] = []

        raw_level = attrs.get("GRIB_level")
        try:
            level_matches = raw_level is not None and float(raw_level) == contract.level
        except (TypeError, ValueError):
            level_matches = False
        if not level_matches:
            errors.append(f"level mismatch: expected {contract.level!r}, got {raw_level!r}")

        expected_units = _GRIB_UNITS_BY_EXPECTED_UNIT.get(contract.expected_unit_id)
        raw_units = attrs.get("GRIB_units")
        if expected_units is not None and (
            not isinstance(raw_units, str) or raw_units.strip().lower() not in expected_units
        ):
            errors.append(
                f"units mismatch: expected one of {sorted(expected_units)!r} for "
                f"{contract.expected_unit_id!r}, got GRIB_units={raw_units!r}"
            )
        step_type = attrs.get("GRIB_stepType")
        if step_type != "accum":
            errors.append(f"expected accumulation stepType, got {step_type!r}")

        raw_start = attrs.get("GRIB_startStep")
        raw_end = attrs.get("GRIB_endStep")
        start_step = end_step = -1
        try:
            if raw_start is None or raw_end is None:
                raise TypeError("missing startStep/endStep")
            start_step = int(raw_start)
            end_step = int(raw_end)
        except (TypeError, ValueError):
            errors.append(f"non-integer startStep/endStep: {raw_start!r}/{raw_end!r}")
        if end_step != forecast_hour:
            errors.append(
                f"accumulation window end mismatch: expected endStep={forecast_hour!r}, "
                f"got {raw_end!r}"
            )

        errors.extend(
            _grid_and_cycle_errors(
                attrs, forecast_hour=forecast_hour, cycle_date=cycle_date, cycle_hour=cycle_hour
            )
        )
        if errors:
            raise GfsDecodeError(
                f"decoded GFS APCP candidate failed semantic assertion: {'; '.join(errors)}"
            )

        values = np.asarray(data_array.values, dtype=np.float64)
        records.append(
            ApcpCandidateRecord(
                start_step=start_step,
                end_step=end_step,
                is_accumulation=(step_type == "accum"),
                values_kg_m2=tuple(float(v) for v in values.flat),
                unit_id=contract.expected_unit_id,
                grid_shape=(int(values.shape[0]), int(values.shape[1])),
                # Real decoded quality mask: 1 marks a point eccodes could
                # not represent as a finite value (bitmap/missing), 0 marks
                # a usable point. Two candidate records that disagree on
                # which points are usable are never dual-parent equivalent
                # even when their finite values happen to coincide.
                mask=tuple(0 if bool(flag) else 1 for flag in np.isfinite(values).flat),
            )
        )
    return tuple(records)
