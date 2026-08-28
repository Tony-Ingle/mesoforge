"""Baseline forecast dataset contract (plan Section 3.6, Task 7).

Validates an already-assembled ``xarray.Dataset`` against the
``baseline-forecast.v1`` schema: exact station/lead set, dimensions,
units, derived speed/direction masks, and required lineage attributes.
Pure validation -- no NetCDF/storage import.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError

_EXPECTED_LOCATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_EXPECTED_LEAD_HOURS = tuple(range(7))
_REQUIRED_DATASET_ATTRS = (
    "schema_version",
    "forecast_issue_time",
    "hrrr_source_reference_time",
    "contributor_artifact_id",
    "extraction_report_artifact_id",
)
_DIRECTION_UNDEFINED_BIT = 1


class BaselineForecastValidationError(MesoForgeError):
    """Raised when a baseline forecast dataset violates the
    ``baseline-forecast.v1`` contract."""


def validate_baseline_forecast(dataset: xr.Dataset) -> None:
    errors: list[str] = []

    for attr in _REQUIRED_DATASET_ATTRS:
        if attr not in dataset.attrs:
            errors.append(f"missing required dataset attribute {attr!r}")
    if errors:
        raise BaselineForecastValidationError(
            f"baseline forecast validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )

    if dataset.attrs["schema_version"] != "baseline-forecast.v1":
        errors.append(
            f"schema_version must be 'baseline-forecast.v1', got "
            f"{dataset.attrs['schema_version']!r}"
        )

    if "location" not in dataset.dims:
        errors.append("missing 'location' dimension")
    else:
        locations = tuple(str(v) for v in dataset["location"].values)
        if locations != _EXPECTED_LOCATIONS:
            errors.append(
                f"location must be exactly {_EXPECTED_LOCATIONS!r} in canonical order, got "
                f"{locations!r}"
            )

    if "lead_time" not in dataset.dims:
        errors.append("missing 'lead_time' dimension")
    else:
        lead_hours = tuple(int(v / np.timedelta64(1, "h")) for v in dataset["lead_time"].values)
        if lead_hours != _EXPECTED_LEAD_HOURS:
            errors.append(
                f"lead_time must be exactly {_EXPECTED_LEAD_HOURS!r} hours, got {lead_hours!r}"
            )

    for variable_id in ("air_temperature_2m", "eastward_wind_10m", "northward_wind_10m"):
        if variable_id not in dataset.data_vars:
            errors.append(f"missing required data variable {variable_id!r}")
            continue
        values = dataset[variable_id].values
        if dataset[variable_id].dims != ("lead_time", "location"):
            errors.append(
                f"data variable {variable_id!r} must have dims (lead_time, location), got "
                f"{dataset[variable_id].dims!r}"
            )
        if not np.all(np.isfinite(values.astype(np.float64))):
            errors.append(
                f"data variable {variable_id!r} contains non-finite values; baseline "
                "generation must fail closed on any missing station/lead/variable"
            )

    if "wind_speed_10m" not in dataset.data_vars:
        errors.append("missing derived data variable 'wind_speed_10m'")
    else:
        speed = dataset["wind_speed_10m"].values
        if not np.all(np.isfinite(speed.astype(np.float64))) or np.any(speed < 0):
            errors.append("wind_speed_10m must be finite and nonnegative everywhere")

    if "wind_from_direction_10m" not in dataset.data_vars:
        errors.append("missing derived data variable 'wind_from_direction_10m'")
    elif "wind_speed_10m" in dataset.data_vars:
        direction = dataset["wind_from_direction_10m"].values
        speed = dataset["wind_speed_10m"].values
        calm = speed == 0
        if np.any(~calm & ~np.isfinite(direction.astype(np.float64))):
            errors.append(
                "wind_from_direction_10m must be finite/defined wherever wind_speed_10m > 0"
            )
        if np.any(calm & np.isfinite(direction.astype(np.float64))):
            errors.append(
                "wind_from_direction_10m must be NaN (direction-undefined) wherever "
                "wind_speed_10m == 0"
            )
        finite_directions = direction[np.isfinite(direction.astype(np.float64))]
        if finite_directions.size and (
            np.any(finite_directions < 0) or np.any(finite_directions >= 360)
        ):
            errors.append("wind_from_direction_10m must be in [0, 360) wherever defined")

    if errors:
        raise BaselineForecastValidationError(
            f"baseline forecast validation failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )
