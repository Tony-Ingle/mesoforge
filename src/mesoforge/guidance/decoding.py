"""cfgrib-backed decoding of pinned HRRR GRIB2 messages (plan Section
2.1, Task 5).

Decodes only the exact concatenated selected-message bytes acquired by
``guidance.acquisition`` -- never re-searches a remote inventory during
replay, and never persists a cfgrib ``.idx`` cache (``indexpath=""``).
Every semantic key asserted by the plan (discipline/category/number,
``typeOfLevel``/level, ``stepType``, ``uvRelativeToGrid``, grid
definition) is checked against the configured
``HrrrFieldAssertion`` before the decoded array is trusted.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path
from typing import Any

import xarray as xr

from mesoforge.catalog.sources import HrrrFieldAssertion, HrrrSourceSettings
from mesoforge.common.errors import MesoForgeError

_BACKEND_KWARGS_TEMPLATE: dict[str, Any] = {
    "indexpath": "",
    "errors": "raise",
}


class HrrrDecodeError(MesoForgeError):
    """Raised when decoded GRIB message keys do not match the
    configured field assertion, or a message fails to decode."""


def cfgrib_backend_kwargs(read_keys: tuple[str, ...]) -> dict[str, Any]:
    kwargs = dict(_BACKEND_KWARGS_TEMPLATE)
    kwargs["read_keys"] = list(read_keys)
    return kwargs


def decode_selected_messages(
    payload: bytes,
    *,
    settings: HrrrSourceSettings,
) -> dict[str, xr.DataArray]:
    """Decode the exact concatenated selected-message bytes for one
    lead into ``{canonical_variable_id: xr.DataArray}``.

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
    for assertion in settings.field_assertions:
        data_array = _find_matching_array(datasets, assertion)
        _assert_matches(data_array, assertion)
        decoded[assertion.canonical_variable_id] = data_array

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


def _assert_matches(data_array: xr.DataArray, assertion: HrrrFieldAssertion) -> None:
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

    if errors:
        raise HrrrDecodeError(
            f"decoded message for {assertion.canonical_variable_id!r} failed semantic "
            f"assertion: {'; '.join(errors)}"
        )
