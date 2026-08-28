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

Codex review (t_9bb13e2b, finding 1) found this module validated only
dtype and a handful of dataset-level attributes -- never variable-level
scientific identity (units, temporal semantics, spatial support, vertical
definition), exact per-variable dimension order against
``allowed_dimension_variants``, missing-value policy, or interval
bounds/closure. All of those are now enforced below.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError
from mesoforge.common.identifiers import GridId


@runtime_checkable
class GridLike(Protocol):
    grid_id: GridId
    shape_y: int
    shape_x: int
    x_coordinates: tuple[float, ...]
    y_coordinates: tuple[float, ...]


@runtime_checkable
class VariableLike(Protocol):
    variable_id: str
    dtype: str
    canonical_unit_id: str
    temporal_semantics: str
    spatial_support: str
    vertical_definition_id: str
    allowed_dimension_variants: tuple[tuple[str, ...], ...]
    missing_value_policy: str
    interval_required: bool


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
_KNOWN_MISSING_VALUE_POLICIES = frozenset(
    {"nan_with_quality_mask", "explicit_fill_with_quality_mask", "not_applicable"}
)
_KNOWN_INTERVAL_CLOSURES = frozenset(
    {"left_closed_right_open", "closed", "open", "left_open_right_closed"}
)


class CanonicalDatasetError(MesoForgeError):
    """Raised when an xarray.Dataset violates the canonical guidance
    dataset contract (plan Section 4.6)."""


def _fail(errors: list[str]) -> None:
    raise CanonicalDatasetError(
        f"canonical dataset validation failed with {len(errors)} problem(s): " + "; ".join(errors)
    )


def _validate_missing_value_policy(
    *,
    variable_id: str,
    policy: str,
    mask_values: np.ndarray,
    data_values: np.ndarray,
    finite: np.ndarray,
    errors: list[str],
) -> None:
    if policy not in _KNOWN_MISSING_VALUE_POLICIES:
        errors.append(
            f"data variable {variable_id!r} declares unknown missing_value_policy {policy!r}; "
            f"expected one of {sorted(_KNOWN_MISSING_VALUE_POLICIES)}"
        )
        return

    is_invalid_masked = (mask_values & np.uint16(_INVALID_VALUE_BITS)) != 0
    if policy == "not_applicable":
        # No missing/invalid semantics apply: the mask must never mark
        # missing/calculated-invalid, and every value must be finite.
        if np.any(is_invalid_masked):
            errors.append(
                f"data variable {variable_id!r} declares missing_value_policy='not_applicable' "
                "but its quality mask marks missing/calculated_invalid values"
            )
        if np.any(~finite):
            errors.append(
                f"data variable {variable_id!r} declares missing_value_policy='not_applicable' "
                "but contains non-finite values"
            )
    elif policy == "nan_with_quality_mask":
        # Missing/invalid values must be represented as non-finite; the
        # reverse also holds -- a value marked missing/calculated_invalid
        # must actually be non-finite, not merely flagged.
        finite_but_marked = is_invalid_masked & finite
        if np.any(finite_but_marked):
            errors.append(
                f"data variable {variable_id!r} declares missing_value_policy="
                "'nan_with_quality_mask' but has finite values at positions marked "
                "missing/calculated_invalid in the quality mask"
            )
    # 'explicit_fill_with_quality_mask' permits an arbitrary finite fill
    # value at masked positions; no additional value-shape constraint is
    # declared by VariableDefinition (Section 4.5) for Phase 0.


def _validate_interval_bounds(
    *,
    dataset: xr.Dataset,
    variable_id: str,
    data_array: xr.DataArray,
    errors: list[str],
) -> None:
    bounds_name = f"{variable_id}_interval_bounds"
    if bounds_name not in dataset.variables:
        errors.append(
            f"data variable {variable_id!r} requires an interval and is missing "
            f"its {bounds_name!r} bounds variable"
        )
        return

    bounds_array = dataset[bounds_name]
    shape_ok = True
    if bounds_array.dims != ("lead_time", "bounds"):
        errors.append(
            f"{bounds_name!r} must have dimensions ('lead_time', 'bounds'), got "
            f"{bounds_array.dims!r}"
        )
        shape_ok = False
    if dataset.sizes.get("bounds") != 2:
        errors.append(f"{bounds_name!r} 'bounds' dimension must have length 2")
        shape_ok = False

    dtype_ok = str(bounds_array.dtype) == "datetime64[ns]"
    if not dtype_ok:
        errors.append(f"{bounds_name!r} must be datetime64[ns], got {bounds_array.dtype}")

    closure = data_array.attrs.get("interval_closure")
    if closure is None:
        errors.append(
            f"data variable {variable_id!r} requires an interval and is missing the "
            "'interval_closure' attribute"
        )
    elif closure not in _KNOWN_INTERVAL_CLOSURES:
        errors.append(
            f"data variable {variable_id!r} has unknown interval_closure {closure!r}; "
            f"expected one of {sorted(_KNOWN_INTERVAL_CLOSURES)}"
        )

    # --- per-lead finite/non-NaT start < end, and required relationship
    # to valid_time (Codex review t_f569c45c finding 2: interval bounds
    # validation accepted reversed, zero-length, NaT, and unrelated
    # bounds -- fail-closed on all four below). ---
    if not (shape_ok and dtype_ok):
        return

    bounds_values = bounds_array.values
    starts = bounds_values[:, 0]
    ends = bounds_values[:, 1]
    is_nat = np.isnat(starts) | np.isnat(ends)
    if np.any(is_nat):
        errors.append(
            f"{bounds_name!r} contains NaT bound(s); every interval start/end must be a "
            "finite, non-NaT datetime64[ns] value"
        )

    finite_rows = ~is_nat
    if np.any(finite_rows) and np.any(starts[finite_rows] >= ends[finite_rows]):
        errors.append(
            f"{bounds_name!r} must have start < end for every lead; found a reversed or "
            "zero-length interval"
        )

    if "valid_time" in dataset.coords or "valid_time" in dataset.variables:
        valid_time_values = dataset["valid_time"].values
        if len(valid_time_values) == len(ends):
            related_rows = finite_rows & (ends != valid_time_values)
            if np.any(related_rows):
                errors.append(
                    f"{bounds_name!r} interval end must equal this variable's valid_time "
                    "for every lead; found an interval unrelated to the variable's valid/"
                    "lead time"
                )


def validate_canonical_dataset(
    dataset: xr.Dataset,
    *,
    grids: dict[GridId, GridLike],
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

    # Dimension-set enforcement is deferred until after the per-variable
    # loop below: the exact allowed dimension set is derived from the
    # dimensions actually declared/used by real data variables, quality
    # masks, and interval-bounds variables -- not merely "any known
    # top-level dimension name" or "anything ending in the literal
    # substring 'bounds'" (Codex review t_f569c45c finding 2, which
    # found an unused 'member' dimension and an arbitrary '*bounds'
    # dimension were both silently accepted). ``used_extra_dims``
    # accumulates the dimensions that are genuinely earned by a
    # declared variable/mask/bounds pairing.
    used_extra_dims: set[str] = set()

    data_var_names = [
        name
        for name in dataset.data_vars
        if not str(name).endswith("_quality_mask") and not str(name).endswith("_interval_bounds")
    ]
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

        used_extra_dims.update(
            str(d) for d in data_array.dims if str(d) not in {"lead_time", "y", "x"}
        )

        expected_dtype = _DTYPE_MAP[variable_def.dtype]
        if data_array.dtype != expected_dtype:
            errors.append(
                f"data variable {variable_id!r} has dtype {data_array.dtype}, expected "
                f"{expected_dtype} per its VariableDefinition"
            )

        # --- exact dimension order against allowed_dimension_variants ---
        actual_dims = tuple(str(d) for d in data_array.dims)
        if actual_dims not in variable_def.allowed_dimension_variants:
            errors.append(
                f"data variable {variable_id!r} has dimension order {actual_dims!r}, which is "
                f"not one of its VariableDefinition's allowed_dimension_variants "
                f"{variable_def.allowed_dimension_variants!r}"
            )

        # --- variable attrs vs the referenced VariableDefinition ---
        actual_unit_id = data_array.attrs.get("unit_id")
        if actual_unit_id != variable_def.canonical_unit_id:
            errors.append(
                f"data variable {variable_id!r} has unit_id {actual_unit_id!r}, expected "
                f"{variable_def.canonical_unit_id!r} per its VariableDefinition"
            )
        actual_temporal_semantics = data_array.attrs.get("temporal_semantics")
        if actual_temporal_semantics != variable_def.temporal_semantics:
            errors.append(
                f"data variable {variable_id!r} has temporal_semantics "
                f"{actual_temporal_semantics!r}, expected {variable_def.temporal_semantics!r} "
                "per its VariableDefinition"
            )
        actual_spatial_support = data_array.attrs.get("spatial_support")
        if actual_spatial_support != variable_def.spatial_support:
            errors.append(
                f"data variable {variable_id!r} has spatial_support {actual_spatial_support!r}, "
                f"expected {variable_def.spatial_support!r} per its VariableDefinition"
            )
        actual_vertical_definition_id = data_array.attrs.get("vertical_definition_id")
        if actual_vertical_definition_id != variable_def.vertical_definition_id:
            errors.append(
                f"data variable {variable_id!r} has vertical_definition_id "
                f"{actual_vertical_definition_id!r}, expected "
                f"{variable_def.vertical_definition_id!r} per its VariableDefinition"
            )

        mask_name = data_array.attrs["quality_mask"]
        if mask_name not in dataset.data_vars:
            errors.append(f"quality mask {mask_name!r} referenced by {variable_id!r} not found")
            continue
        mask_array = dataset[mask_name]
        used_extra_dims.update(
            str(d) for d in mask_array.dims if str(d) not in {"lead_time", "y", "x"}
        )
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

        # --- missing-value policy ---
        _validate_missing_value_policy(
            variable_id=variable_id,
            policy=variable_def.missing_value_policy,
            mask_values=mask_values,
            data_values=data_values,
            finite=finite,
            errors=errors,
        )

        # --- interval bounds/closure ---
        if variable_def.interval_required:
            used_extra_dims.add("bounds")
            _validate_interval_bounds(
                dataset=dataset, variable_id=variable_id, data_array=data_array, errors=errors
            )

    # A dimension is permitted only if it is exactly one of the fixed
    # baseline dims (lead_time/y/x, exempted above) or was actually
    # earned by a declared data variable, its quality mask, or an
    # interval-bounds variable in the loop above -- never merely because
    # its name happens to be in the global known-dimension vocabulary
    # (member/level/location) or happens to end in the substring
    # "bounds".
    for extra_dim in declared_dims:
        extra_dim_name = str(extra_dim)
        if extra_dim_name not in _KNOWN_TOP_LEVEL_DIMS and extra_dim_name != "bounds":
            errors.append(f"undeclared dimension {extra_dim_name!r}")
        elif extra_dim_name not in used_extra_dims:
            errors.append(
                f"dimension {extra_dim_name!r} is declared on the dataset but not used by any "
                "data variable, quality mask, or interval-bounds variable"
            )

    if errors:
        _fail(errors)
