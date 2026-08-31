"""NBM CONUS core GRIB2 decoding (plan Section 2.3, Task 3).

Reuses the same cfgrib-backed decode strategy as HRRR
(``guidance.decoding``): a temp file, ``indexpath=""`` (no persisted
``.idx`` cache), and strict semantic-key assertion before any decoded
array is trusted. NBM decoding additionally must reject any decoded
ensemble standard-deviation/percentile/QMD-shaped record and any
non-deterministic APCP record other than the exact PoP01 probability
contract.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path
from typing import Any

import xarray as xr

from mesoforge.catalog.sources import NbmSourceSettings, Phase2FieldContract
from mesoforge.common.errors import MesoForgeError

_BACKEND_KWARGS_TEMPLATE: dict[str, Any] = {"indexpath": "", "errors": "raise"}


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
) -> xr.DataArray:
    """Decode exactly one selected NBM GRIB2 message and assert its
    keys/units/step against ``contract``. Never decodes an ensemble
    std-dev/percentile/QMD record: ``probabilityType``/percentile keys
    that indicate a non-deterministic, non-PoP01 record are rejected."""
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
    _assert_matches(data_array, contract, forecast_hour=forecast_hour)
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


def _assert_matches(
    data_array: xr.DataArray, contract: Phase2FieldContract, *, forecast_hour: int
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

    if contract.is_accumulation:
        step_type = attrs.get("GRIB_stepType")
        if step_type != "accum":
            errors.append(f"expected accumulation stepType, got {step_type!r}")

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

    if errors:
        raise NbmDecodeError(
            f"decoded NBM message for {contract.canonical_variable_id!r} failed semantic "
            f"assertion: {'; '.join(errors)}"
        )
