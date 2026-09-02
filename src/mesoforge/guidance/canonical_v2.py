"""Canonical Phase 2 multi-model guidance assembly (plan Section 5.1,
Task 3/4/5): ``canonical-guidance.v2`` -- one provider/cycle native grid
per model with scalar ``forecast_reference_time``, one-dimensional
``source_lead_time``/``source_valid_time``, native ``y``/``x``, model
ID, exact interval bounds, and model-specific variables.

Shared by HRRR/NBM/GFS normalization so all three models assemble an
identically-shaped dataset differing only in native grid and which
variables/units are populated.
"""

from __future__ import annotations

import re
from typing import Literal

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError

ModelId = Literal["hrrr", "nbm", "gfs"]

_INSTANTANEOUS_VARIABLE_UNITS: dict[str, str] = {
    "air_temperature_2m": "K",
    "dew_point_temperature_2m": "K",
    "eastward_wind_10m": "m/s",
    "northward_wind_10m": "m/s",
    "wind_gust_10m": "m/s",
}
_INTERVAL_VARIABLE_UNITS: dict[str, str] = {
    "liquid_equivalent_precipitation_amount_1h": "kg/m^2",
    "probability_of_precipitation_1h": "1",
}


class CanonicalGuidanceV2Error(MesoForgeError):
    """Raised when a ``canonical-guidance.v2`` dataset fails assembly-
    time or validation-time invariants."""


def assemble_canonical_guidance_v2(
    *,
    model: ModelId,
    forecast_reference_time: np.datetime64,
    source_lead_hours: tuple[int, ...],
    x: np.ndarray,
    y: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    instantaneous_fields: dict[str, np.ndarray],
    interval_fields: dict[str, np.ndarray],
    interval_start_hours: dict[str, tuple[int, ...]],
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
    crs_wkt2: str | None = None,
) -> xr.Dataset:
    """Assemble ``canonical-guidance.v2`` (plan Section 5.1) for one
    model's one selected cycle.

    ``instantaneous_fields``/``interval_fields`` map canonical
    variable ID -> array shaped ``(source_lead_time, y, x)``.
    ``interval_start_hours`` maps each interval variable ID to its
    per-lead interval start hour (so ``APCP``'s ``(start, lead]``
    bound is retained exactly, including GFS bucket starts that are
    not simply ``lead - 1``).
    """
    n_lead = len(source_lead_hours)
    lead_time = np.array(
        [np.timedelta64(h, "h") for h in source_lead_hours], dtype="timedelta64[ns]"
    )
    valid_time = forecast_reference_time + lead_time

    data_vars: dict[str, tuple[object, ...]] = {}

    def _mask(shape: tuple[int, ...]) -> np.ndarray:
        return np.zeros(shape, dtype=np.uint16)

    for variable_id, array in instantaneous_fields.items():
        unit_id = _INSTANTANEOUS_VARIABLE_UNITS.get(variable_id)
        if unit_id is None:
            raise CanonicalGuidanceV2Error(
                f"unknown instantaneous canonical_variable_id {variable_id!r}"
            )
        if array.shape != (n_lead, *lat.shape):
            raise CanonicalGuidanceV2Error(
                f"instantaneous field {variable_id!r} has shape {array.shape!r}, expected "
                f"{(n_lead, *lat.shape)!r}"
            )
        data_vars[variable_id] = (
            ("source_lead_time", "y", "x"),
            array.astype(np.float32),
            {
                "unit_id": unit_id,
                "temporal_semantics": "instantaneous",
                "spatial_support": "point",
                "quality_mask": f"{variable_id}_quality_mask",
            },
        )
        data_vars[f"{variable_id}_quality_mask"] = (
            ("source_lead_time", "y", "x"),
            _mask((n_lead, *lat.shape)),
        )

    for variable_id, array in interval_fields.items():
        unit_id = _INTERVAL_VARIABLE_UNITS.get(variable_id)
        if unit_id is None:
            raise CanonicalGuidanceV2Error(
                f"unknown interval canonical_variable_id {variable_id!r}"
            )
        if array.shape != (n_lead, *lat.shape):
            raise CanonicalGuidanceV2Error(
                f"interval field {variable_id!r} has shape {array.shape!r}, expected "
                f"{(n_lead, *lat.shape)!r}"
            )
        starts = interval_start_hours.get(variable_id)
        if starts is None or len(starts) != n_lead:
            raise CanonicalGuidanceV2Error(
                f"interval field {variable_id!r} requires interval_start_hours for every lead"
            )
        interval_start = np.array(
            [forecast_reference_time + np.timedelta64(h, "h") for h in starts],
            dtype="datetime64[ns]",
        )
        interval_end = valid_time
        data_vars[variable_id] = (
            ("source_lead_time", "y", "x"),
            array.astype(np.float32),
            {
                "unit_id": unit_id,
                "temporal_semantics": "accumulation"
                if variable_id != "probability_of_precipitation_1h"
                else "probability",
                "spatial_support": "point",
                "interval_closure": "left_open_right_closed",
                "quality_mask": f"{variable_id}_quality_mask",
            },
        )
        data_vars[f"{variable_id}_quality_mask"] = (
            ("source_lead_time", "y", "x"),
            _mask((n_lead, *lat.shape)),
        )
        data_vars[f"{variable_id}_interval_bounds"] = (
            ("source_lead_time", "bounds"),
            np.stack([interval_start, interval_end.astype("datetime64[ns]")], axis=1),
        )

    dataset = xr.Dataset(
        data_vars=data_vars,
        coords={
            "forecast_reference_time": forecast_reference_time,
            "source_lead_time": lead_time,
            "source_valid_time": ("source_lead_time", valid_time),
            "y": y,
            "x": x,
            "latitude": (("y", "x"), lat),
            "longitude": (("y", "x"), lon),
        },
        attrs={
            "schema_version": "canonical-guidance.v2",
            "model": model,
            "time_encoding": "UTC",
            "grid_id": grid_id,
            "configuration_snapshot_id": configuration_snapshot_id,
            "variable_lineage_manifest_id": variable_lineage_manifest_id,
            **({"crs_wkt2": crs_wkt2} if crs_wkt2 is not None else {}),
        },
    )
    return dataset


_EXPECTED_MODELS = frozenset({"hrrr", "nbm", "gfs"})

# Codex review (Phase 2 remediation, finding 3): the required
# model-specific canonical variable set. HRRR/GFS never contribute
# probability_of_precipitation_1h (Section 4.5: NBM is the sole PoP
# contributor); NBM additionally requires it.
_REQUIRED_INSTANTANEOUS_VARIABLES: frozenset[str] = frozenset(
    {
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_gust_10m",
    }
)
_REQUIRED_VARIABLES_BY_MODEL: dict[str, frozenset[str]] = {
    "hrrr": _REQUIRED_INSTANTANEOUS_VARIABLES | {"liquid_equivalent_precipitation_amount_1h"},
    "gfs": _REQUIRED_INSTANTANEOUS_VARIABLES | {"liquid_equivalent_precipitation_amount_1h"},
    "nbm": _REQUIRED_INSTANTANEOUS_VARIABLES
    | {"liquid_equivalent_precipitation_amount_1h", "probability_of_precipitation_1h"},
}

_EXPECTED_TEMPORAL_SEMANTICS: dict[str, str] = {
    **{variable_id: "instantaneous" for variable_id in _INSTANTANEOUS_VARIABLE_UNITS},
    "liquid_equivalent_precipitation_amount_1h": "accumulation",
    "probability_of_precipitation_1h": "probability",
}
_EXPECTED_SPATIAL_SUPPORT = "point"

# Physically plausible bounds for canonical-guidance.v2 values (finding
# 3: validation must reject an unbounded/implausible value, not merely
# a non-finite one).
_VALUE_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "air_temperature_2m": (180.0, 340.0),
    "dew_point_temperature_2m": (150.0, 340.0),
    "eastward_wind_10m": (None, None),
    "northward_wind_10m": (None, None),
    "wind_gust_10m": (0.0, 100.0),
    "liquid_equivalent_precipitation_amount_1h": (0.0, None),
    "probability_of_precipitation_1h": (0.0, 1.0),
}

_CONFIGURATION_SNAPSHOT_ID_RE = re.compile(r"^cfg_sha256_[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_GRID_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_INTERVAL_WIDTH_NS = np.timedelta64(1, "h").astype("timedelta64[ns]")
_INTERVAL_WIDTH_TOLERANCE_NS = np.timedelta64(0, "ns")


def validate_canonical_guidance_v2(dataset: xr.Dataset) -> None:
    """Section 5.1: distinct validator from ``validate_canonical_dataset``
    (v1); dispatched exactly by schema version, never applied to a v1
    dataset.

    Enforces the full canonical contract (Codex review remediation,
    finding 3): required model-specific fields/dimensions, exact
    units/semantics/support, ``source_valid_time = reference + lead``,
    interval bounds/width/closure, finite and physically bounded
    values, grid identity/coordinates/shape, quality masks, and
    lineage/configuration identities.
    """
    errors: list[str] = []

    if dataset.attrs.get("schema_version") != "canonical-guidance.v2":
        errors.append(
            f"schema_version must be 'canonical-guidance.v2', got "
            f"{dataset.attrs.get('schema_version')!r}"
        )
    model = dataset.attrs.get("model")
    if model not in _EXPECTED_MODELS:
        errors.append(f"model must be one of {sorted(_EXPECTED_MODELS)!r}, got {model!r}")
    if "forecast_reference_time" not in dataset.coords:
        errors.append("missing forecast_reference_time coordinate")
    elif dataset["forecast_reference_time"].ndim != 0:
        errors.append("forecast_reference_time must be scalar")
    for required_coord in ("source_lead_time", "source_valid_time", "latitude", "longitude"):
        if required_coord not in dataset.coords:
            errors.append(f"missing required coordinate {required_coord!r}")
    for required_dim in ("y", "x"):
        if required_dim not in dataset.dims:
            errors.append(f"missing required dimension {required_dim!r}")

    # -- lineage/config identities (finding 3) -----------------------
    grid_id = dataset.attrs.get("grid_id")
    if not isinstance(grid_id, str) or not _GRID_ID_RE.match(grid_id):
        errors.append(
            f"grid_id must be a non-empty lowercase kebab/dot identifier, got {grid_id!r}"
        )
    configuration_snapshot_id = dataset.attrs.get("configuration_snapshot_id")
    if not isinstance(configuration_snapshot_id, str) or not _CONFIGURATION_SNAPSHOT_ID_RE.match(
        configuration_snapshot_id
    ):
        errors.append(
            "configuration_snapshot_id must be 'cfg_sha256_' followed by 64 lowercase hex "
            f"characters, got {configuration_snapshot_id!r}"
        )
    variable_lineage_manifest_id = dataset.attrs.get("variable_lineage_manifest_id")
    if not isinstance(variable_lineage_manifest_id, str) or not _ARTIFACT_ID_RE.match(
        variable_lineage_manifest_id
    ):
        errors.append(
            "variable_lineage_manifest_id must be a real artifact ID, got "
            f"{variable_lineage_manifest_id!r}"
        )

    if errors:
        raise CanonicalGuidanceV2Error(
            f"canonical-guidance.v2 validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )

    # -- source_lead_time / source_valid_time identity (finding 3) ---
    lead_values = dataset["source_lead_time"].values
    if len(lead_values) == 0:
        errors.append("source_lead_time must be non-empty")
    elif np.any(lead_values < np.timedelta64(0, "ns")):
        errors.append("source_lead_time must be nonnegative")
    elif len(lead_values) > 1 and not np.all(np.diff(lead_values) > np.timedelta64(0, "ns")):
        errors.append("source_lead_time must be strictly increasing")

    reference_time = dataset["forecast_reference_time"].values
    valid_time = dataset["source_valid_time"].values
    expected_valid_time = (reference_time + lead_values).astype("datetime64[ns]")
    if not np.array_equal(valid_time.astype("datetime64[ns]"), expected_valid_time):
        errors.append(
            "source_valid_time must equal forecast_reference_time + source_lead_time exactly; "
            f"expected {expected_valid_time!r}, got {valid_time.astype('datetime64[ns]')!r}"
        )

    # -- grid identity/coordinates/shape (finding 3) ------------------
    x_values = dataset["x"].values if "x" in dataset.coords else None
    y_values = dataset["y"].values if "y" in dataset.coords else None
    for name, values in (("x", x_values), ("y", y_values)):
        if values is None:
            continue
        if len(values) == 0:
            errors.append(f"{name!r} coordinate must be non-empty")
            continue
        finite = np.isfinite(values.astype(np.float64))
        if not np.all(finite):
            errors.append(f"{name!r} coordinate must be entirely finite")
            continue
        if len(values) > 1:
            diffs = np.diff(values.astype(np.float64))
            if not (np.all(diffs > 0) or np.all(diffs < 0)):
                errors.append(f"{name!r} coordinate must be strictly monotonic")

    latitude = dataset["latitude"].values if "latitude" in dataset.coords else None
    longitude = dataset["longitude"].values if "longitude" in dataset.coords else None
    if latitude is not None and longitude is not None:
        if latitude.shape != longitude.shape:
            errors.append(
                f"latitude shape {latitude.shape!r} must match longitude shape {longitude.shape!r}"
            )
        if y_values is not None and x_values is not None:
            expected_shape = (len(y_values), len(x_values))
            if latitude.shape != expected_shape:
                errors.append(
                    f"latitude/longitude shape {latitude.shape!r} must be (len(y), len(x)) = "
                    f"{expected_shape!r}"
                )
        finite_lat = np.isfinite(latitude.astype(np.float64))
        if not np.all(finite_lat):
            errors.append("latitude must be entirely finite")
        elif np.any(latitude < -90.0) or np.any(latitude > 90.0):
            errors.append("latitude must be in [-90, 90]")
        if not np.all(np.isfinite(longitude.astype(np.float64))):
            errors.append("longitude must be entirely finite")

    # -- required model-specific fields (finding 3) -------------------
    required_variables = _REQUIRED_VARIABLES_BY_MODEL.get(str(model), frozenset())
    present_variables = {
        str(name)
        for name in dataset.data_vars
        if not str(name).endswith("_quality_mask") and not str(name).endswith("_interval_bounds")
    }
    missing_variables = required_variables - present_variables
    if missing_variables:
        errors.append(
            f"model {model!r} is missing required canonical variable(s) "
            f"{sorted(missing_variables)!r}"
        )
    extra_variables = present_variables - required_variables
    if extra_variables:
        errors.append(
            f"model {model!r} has unapproved canonical variable(s) {sorted(extra_variables)!r}"
        )

    expected_data_vars = (
        required_variables
        | {f"{name}_quality_mask" for name in required_variables}
        | {
            f"{name}_interval_bounds"
            for name in required_variables
            if name in _INTERVAL_VARIABLE_UNITS
        }
    )
    actual_data_vars = {str(name) for name in dataset.data_vars}
    if actual_data_vars != expected_data_vars:
        errors.append(
            "data variables must be exactly the approved fields, masks, and interval bounds; "
            f"missing={sorted(expected_data_vars - actual_data_vars)!r}, "
            f"extra={sorted(actual_data_vars - expected_data_vars)!r}"
        )

    expected_dims = {"source_lead_time", "y", "x", "bounds"}
    if set(dataset.dims) != expected_dims:
        errors.append(
            f"dimensions must be exactly {sorted(expected_dims)!r}, got "
            f"{sorted(str(name) for name in dataset.dims)!r}"
        )

    n_lead = len(lead_values)
    for raw_name in dataset.data_vars:
        variable_name = str(raw_name)
        if variable_name.endswith("_quality_mask") or variable_name.endswith("_interval_bounds"):
            continue
        data_array = dataset[raw_name]
        values = data_array.values
        attrs = data_array.attrs
        if data_array.dims != ("source_lead_time", "y", "x"):
            errors.append(
                f"data variable {variable_name!r} dimensions/order must be exactly "
                "('source_lead_time', 'y', 'x')"
            )

        # -- exact units/semantics/support (finding 3) -----------------
        expected_unit = _INSTANTANEOUS_VARIABLE_UNITS.get(
            variable_name, _INTERVAL_VARIABLE_UNITS.get(variable_name)
        )
        if expected_unit is not None and attrs.get("unit_id") != expected_unit:
            errors.append(
                f"data variable {variable_name!r} unit_id must be exactly {expected_unit!r}, "
                f"got {attrs.get('unit_id')!r}"
            )
        expected_semantics = _EXPECTED_TEMPORAL_SEMANTICS.get(variable_name)
        if expected_semantics is not None and attrs.get("temporal_semantics") != expected_semantics:
            errors.append(
                f"data variable {variable_name!r} temporal_semantics must be exactly "
                f"{expected_semantics!r}, got {attrs.get('temporal_semantics')!r}"
            )
        if attrs.get("spatial_support") != _EXPECTED_SPATIAL_SUPPORT:
            errors.append(
                f"data variable {variable_name!r} spatial_support must be exactly "
                f"{_EXPECTED_SPATIAL_SUPPORT!r}, got {attrs.get('spatial_support')!r}"
            )

        mask_name = attrs.get("quality_mask")
        if mask_name is None or mask_name not in dataset.data_vars:
            errors.append(f"data variable {variable_name!r} missing a registered quality mask")
            continue
        mask_values = dataset[mask_name].values
        if dataset[mask_name].values.dtype != np.uint16:
            errors.append(f"quality mask {mask_name!r} must have dtype uint16")
        if mask_values.shape != values.shape:
            errors.append(
                f"quality mask {mask_name!r} shape {mask_values.shape!r} must match data "
                f"variable {variable_name!r} shape {values.shape!r}"
            )
        if dataset[mask_name].dims != data_array.dims:
            errors.append(
                f"quality mask {mask_name!r} dimensions/order must match {variable_name!r}"
            )
        if not np.all(np.isin(mask_values, (0, 1))):
            errors.append(f"quality mask {mask_name!r} contains an unapproved mask value")
        finite = np.isfinite(values.astype(np.float64))
        masked_missing = mask_values != 0
        if np.any((~finite) & (~masked_missing)):
            errors.append(
                f"data variable {variable_name!r} has non-finite values not marked missing"
            )

        # -- finite and physically bounded values (finding 3) ----------
        bounds = _VALUE_BOUNDS.get(variable_name)
        if bounds is not None:
            lower, upper = bounds
            unmasked_finite = finite & ~masked_missing
            checked = values.astype(np.float64)[unmasked_finite]
            below = lower is not None and np.any(checked < lower)
            above = upper is not None and np.any(checked > upper)
            if checked.size and (below or above):
                errors.append(
                    f"data variable {variable_name!r} has value(s) outside the physically "
                    f"plausible bound [{lower!r}, {upper!r}]"
                )

        # -- interval bounds/width/closure (finding 3) ------------------
        bounds_name = f"{variable_name}_interval_bounds"
        is_interval = variable_name in _INTERVAL_VARIABLE_UNITS
        if is_interval and bounds_name not in dataset.data_vars:
            errors.append(f"interval variable {variable_name!r} is missing mandatory bounds")
        if not is_interval and bounds_name in dataset.data_vars:
            errors.append(f"instantaneous variable {variable_name!r} must not have interval bounds")
        if bounds_name in dataset.data_vars:
            if dataset[bounds_name].dims != ("source_lead_time", "bounds"):
                errors.append(
                    f"{bounds_name!r} dimensions/order must be exactly "
                    "('source_lead_time', 'bounds')"
                )
            interval_bounds = dataset[bounds_name].values
            if interval_bounds.shape != (n_lead, 2):
                errors.append(
                    f"{bounds_name!r} must have shape ({n_lead}, 2), got {interval_bounds.shape!r}"
                )
            else:
                starts = interval_bounds[:, 0].astype("datetime64[ns]")
                ends = interval_bounds[:, 1].astype("datetime64[ns]")
                if not np.array_equal(ends, expected_valid_time):
                    errors.append(
                        f"{bounds_name!r} interval end must equal source_valid_time exactly"
                    )
                if np.any(ends <= starts):
                    errors.append(
                        f"{bounds_name!r} interval end must be strictly after interval start"
                    )
                widths = ends - starts
                if np.any(widths != _INTERVAL_WIDTH_NS):
                    errors.append(
                        f"{bounds_name!r} interval width must be exactly 1 hour for every lead"
                    )
            if attrs.get("interval_closure") != "left_open_right_closed":
                errors.append(
                    f"data variable {variable_name!r} interval_closure must be exactly "
                    f"'left_open_right_closed', got {attrs.get('interval_closure')!r}"
                )

    required_thermodynamic = {
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "air_temperature_2m_quality_mask",
        "dew_point_temperature_2m_quality_mask",
    }
    if required_thermodynamic.issubset(dataset.data_vars):
        temperature = dataset["air_temperature_2m"].values.astype(np.float64)
        dew_point = dataset["dew_point_temperature_2m"].values.astype(np.float64)
        usable = (
            (dataset["air_temperature_2m_quality_mask"].values == 0)
            & (dataset["dew_point_temperature_2m_quality_mask"].values == 0)
            & np.isfinite(temperature)
            & np.isfinite(dew_point)
        )
        if np.any(dew_point[usable] > temperature[usable] + 1e-6):
            errors.append("dew_point_temperature_2m must not exceed air_temperature_2m + 1e-6 K")

    if errors:
        raise CanonicalGuidanceV2Error(
            f"canonical-guidance.v2 validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )
