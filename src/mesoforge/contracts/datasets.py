"""Canonical xarray dataset contract (plan Section 4.6).

Validates an already-constructed ``xarray.Dataset`` against grid/variable
definitions. Not coupled to NetCDF serialization -- this module never
imports h5netcdf.

Layering note: the Phase 0 plan's Section 3 dependency direction places
``contracts -> common`` only, with ``catalog -> contracts``. Section 4.6
nonetheless requires dataset validation against ``GridDefinition`` and
``VariableDefinition``, which the plan's own Section 3 file layout puts in
``catalog``. Importing ``catalog`` from ``contracts`` would invert that
direction. This module resolves the tension with structural typing:
``GridLike``/``VariableLike`` protocols below declare only the attributes
validation actually needs; ``catalog.grids.GridDefinition`` and
``catalog.variables.VariableDefinition`` satisfy them structurally with no
import in either direction. Flagged for Codex review rather than silently
choosing a dependency direction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError


@runtime_checkable
class GridLike(Protocol):
    grid_id: str
    shape_y: int
    shape_x: int
    x_coordinates: tuple[float, ...]
    y_coordinates: tuple[float, ...]


@runtime_checkable
class VariableLike(Protocol):
    variable_id: str
    dtype: str


_DTYPE_MAP: dict[str, np.dtype] = {
    "float32": np.dtype("float32"),
    "float64": np.dtype("float64"),
    "int16": np.dtype("int16"),
    "int32": np.dtype("int32"),
    "uint8": np.dtype("uint8"),
    "bool": np.dtype("bool"),
}

_REQUIRED_ATTRS = (
    "schema_version",
    "time_encoding",
    "grid_id",
    "configuration_snapshot_id",
    "variable_lineage_manifest_id",
)
_REQUIRED_VARIABLE_ATTRS = (
    "unit_id",
    "temporal_semantics",
    "spatial_support",
    "vertical_definition_id",
    "quality_mask",
)
_KNOWN_QUALITY_BITS = 1 | 2 | 4  # missing | outside_coverage | calculated_invalid
_INVALID_VALUE_BITS = 1 | 4  # missing or calculated_invalid implies the value is non-finite
_KNOWN_TOP_LEVEL_DIMS = {"lead_time", "y", "x", "member", "level", "location"}


class CanonicalDatasetError(MesoForgeError):
    """Raised when an xarray.Dataset violates the canonical guidance
    dataset contract (plan Section 4.6)."""


def _fail(errors: list[str]) -> None:
    raise CanonicalDatasetError(
        f"canonical dataset validation failed with {len(errors)} problem(s): " + "; ".join(errors)
    )


def validate_canonical_dataset(
    dataset: xr.Dataset,
    *,
    grids: dict[str, GridLike],
    variables: dict[str, VariableLike],
) -> None:
    """Validate ``dataset`` in place. Raises CanonicalDatasetError with all
    problems found (not just the first) if any invariant is violated."""
    errors: list[str] = []

    for attr in _REQUIRED_ATTRS:
        if attr not in dataset.attrs:
            errors.append(f"missing required dataset attribute {attr!r}")
    if errors:
        _fail(errors)

    if dataset.attrs["schema_version"] != "canonical-guidance.v1":
        errors.append(
            f"schema_version must be 'canonical-guidance.v1', got "
            f"{dataset.attrs['schema_version']!r}"
        )
    if dataset.attrs["time_encoding"] != "UTC":
        errors.append(f"time_encoding must be 'UTC', got {dataset.attrs['time_encoding']!r}")

    grid_id = dataset.attrs["grid_id"]
    grid = grids.get(grid_id)
    if grid is None:
        errors.append(f"referenced grid_id {grid_id!r} is not in the supplied grid catalog")

    for required_coord in ("forecast_reference_time", "lead_time", "valid_time"):
        if required_coord not in dataset.coords and required_coord not in dataset.variables:
            errors.append(f"missing required coordinate {required_coord!r}")
    if errors:
        _fail(errors)

    if dataset["forecast_reference_time"].ndim != 0:
        errors.append("forecast_reference_time must be scalar")
    if str(dataset["forecast_reference_time"].dtype) != "datetime64[ns]":
        errors.append(
            f"forecast_reference_time must be datetime64[ns], got "
            f"{dataset['forecast_reference_time'].dtype}"
        )

    if dataset["lead_time"].ndim != 1:
        errors.append("lead_time must be one-dimensional")
    if str(dataset["lead_time"].dtype) != "timedelta64[ns]":
        errors.append(f"lead_time must be timedelta64[ns], got {dataset['lead_time'].dtype}")
    else:
        lead_values = dataset["lead_time"].values
        if np.any(lead_values < np.timedelta64(0, "ns")):
            errors.append("lead_time must be nonnegative")
        if len(lead_values) > 1 and not np.all(np.diff(lead_values) > np.timedelta64(0, "ns")):
            errors.append("lead_time must be strictly increasing")

    if str(dataset["valid_time"].dtype) != "datetime64[ns]":
        errors.append(f"valid_time must be datetime64[ns], got {dataset['valid_time'].dtype}")
    elif "lead_time" in dataset.coords or "lead_time" in dataset.variables:
        try:
            expected_valid_times = (
                dataset["forecast_reference_time"].values + dataset["lead_time"].values
            )
            if not np.array_equal(dataset["valid_time"].values, expected_valid_times):
                errors.append("valid_time must exactly equal forecast_reference_time + lead_time")
        except (ValueError, TypeError):
            errors.append(
                "valid_time could not be compared against forecast_reference_time + lead_time"
            )

    declared_dims = set(dataset.dims) - {"lead_time", "y", "x"}
    if grid is not None:
        if "y" in dataset.dims and dataset.sizes.get("y") != grid.shape_y:
            errors.append(f"dataset 'y' dimension size does not match grid shape_y={grid.shape_y}")
        if "x" in dataset.dims and dataset.sizes.get("x") != grid.shape_x:
            errors.append(f"dataset 'x' dimension size does not match grid shape_x={grid.shape_x}")
        if "y" in dataset.coords:
            if not np.array_equal(dataset["y"].values, np.asarray(grid.y_coordinates)):
                errors.append("dataset 'y' coordinates do not match the referenced grid")
        if "x" in dataset.coords:
            if not np.array_equal(dataset["x"].values, np.asarray(grid.x_coordinates)):
                errors.append("dataset 'x' coordinates do not match the referenced grid")

    for extra_dim in declared_dims:
        extra_dim_name = str(extra_dim)
        if extra_dim_name not in _KNOWN_TOP_LEVEL_DIMS and not extra_dim_name.endswith("bounds"):
            errors.append(f"undeclared dimension {extra_dim_name!r}")

    data_var_names = [name for name in dataset.data_vars if not str(name).endswith("_quality_mask")]
    for name in data_var_names:
        data_array = dataset[name]
        variable_id = str(name)
        variable_def = variables.get(variable_id)
        if variable_def is None:
            errors.append(f"data variable {variable_id!r} is not in the supplied variable catalog")
            continue

        for attr in _REQUIRED_VARIABLE_ATTRS:
            if attr not in data_array.attrs:
                errors.append(f"data variable {variable_id!r} missing required attribute {attr!r}")
        if any(attr not in data_array.attrs for attr in _REQUIRED_VARIABLE_ATTRS):
            continue

        expected_dtype = _DTYPE_MAP[variable_def.dtype]
        if data_array.dtype != expected_dtype:
            errors.append(
                f"data variable {variable_id!r} has dtype {data_array.dtype}, expected "
                f"{expected_dtype} per its VariableDefinition"
            )

        mask_name = data_array.attrs["quality_mask"]
        if mask_name not in dataset.data_vars:
            errors.append(f"quality mask {mask_name!r} referenced by {variable_id!r} not found")
            continue
        mask_array = dataset[mask_name]
        if mask_array.dims != data_array.dims:
            errors.append(f"quality mask {mask_name!r} dimensions do not match {variable_id!r}")
        if mask_array.dtype != np.dtype("uint16"):
            errors.append(f"quality mask {mask_name!r} must be uint16, got {mask_array.dtype}")

        mask_values = mask_array.values
        unknown_bits = mask_values & ~np.uint16(_KNOWN_QUALITY_BITS)
        if np.any(unknown_bits != 0):
            errors.append(f"quality mask {mask_name!r} has unknown set bits")

        data_values = data_array.values
        is_invalid_masked = (mask_values & np.uint16(_INVALID_VALUE_BITS)) != 0
        finite = (
            np.isfinite(data_values.astype("float64"))
            if np.issubdtype(data_values.dtype, np.floating)
            else np.ones_like(data_values, dtype=bool)
        )
        bad = (~finite) & (~is_invalid_masked)
        if np.any(bad):
            errors.append(
                f"data variable {variable_id!r} has non-finite values not marked "
                "missing/invalid in its quality mask"
            )

    if errors:
        _fail(errors)
