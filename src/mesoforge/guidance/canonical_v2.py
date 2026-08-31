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
        },
    )
    return dataset


_EXPECTED_MODELS = frozenset({"hrrr", "nbm", "gfs"})


def validate_canonical_guidance_v2(dataset: xr.Dataset) -> None:
    """Section 5.1: distinct validator from ``validate_canonical_dataset``
    (v1); dispatched exactly by schema version, never applied to a v1
    dataset."""
    errors: list[str] = []

    if dataset.attrs.get("schema_version") != "canonical-guidance.v2":
        errors.append(
            f"schema_version must be 'canonical-guidance.v2', got "
            f"{dataset.attrs.get('schema_version')!r}"
        )
    if dataset.attrs.get("model") not in _EXPECTED_MODELS:
        errors.append(
            f"model must be one of {sorted(_EXPECTED_MODELS)!r}, got {dataset.attrs.get('model')!r}"
        )
    if "forecast_reference_time" not in dataset.coords:
        errors.append("missing forecast_reference_time coordinate")
    elif dataset["forecast_reference_time"].ndim != 0:
        errors.append("forecast_reference_time must be scalar")
    for required_coord in ("source_lead_time", "source_valid_time", "latitude", "longitude"):
        if required_coord not in dataset.coords:
            errors.append(f"missing required coordinate {required_coord!r}")

    if errors:
        raise CanonicalGuidanceV2Error(
            f"canonical-guidance.v2 validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )

    lead_values = dataset["source_lead_time"].values
    if len(lead_values) == 0:
        errors.append("source_lead_time must be non-empty")
    elif np.any(lead_values < np.timedelta64(0, "ns")):
        errors.append("source_lead_time must be nonnegative")
    elif len(lead_values) > 1 and not np.all(np.diff(lead_values) > np.timedelta64(0, "ns")):
        errors.append("source_lead_time must be strictly increasing")

    for name in dataset.data_vars:
        variable_name = str(name)
        if variable_name.endswith("_quality_mask") or variable_name.endswith("_interval_bounds"):
            continue
        data_array = dataset[name]
        values = data_array.values
        mask_name = data_array.attrs.get("quality_mask")
        if mask_name is None or mask_name not in dataset.data_vars:
            errors.append(f"data variable {variable_name!r} missing a registered quality mask")
            continue
        mask_values = dataset[mask_name].values
        finite = np.isfinite(values.astype(np.float64))
        masked_missing = mask_values != 0
        if np.any((~finite) & (~masked_missing)):
            errors.append(
                f"data variable {variable_name!r} has non-finite values not marked missing"
            )

    if errors:
        raise CanonicalGuidanceV2Error(
            f"canonical-guidance.v2 validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )
