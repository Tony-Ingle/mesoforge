"""``baseline-forecast.v2`` assembly (plan Section 5.1, Task 10).

Assembles the exact-once identity-corrected baseline dataset from
already-blended per-(variable, location, target_horizon) values plus
their availability state. Pure xarray/NumPy computation.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError

_EXPECTED_LOCATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_EXPECTED_TARGET_HORIZONS = tuple(range(1, 37))
_FORECAST_VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "probability_of_precipitation_1h",
    "liquid_equivalent_precipitation_amount_1h",
)
_STATE_CODES = {"complete": 0, "fallback": 1, "unavailable": 2, "inconsistent": 3}


class BaselineV2AssemblyError(MesoForgeError):
    """Raised when Section 5.1 baseline-forecast.v2 assembly inputs are
    incomplete or malformed."""


def assemble_baseline_forecast_v2(
    *,
    values: dict[tuple[str, str, int], float],
    states: dict[tuple[str, str, int], str],
    target_reference_time: np.datetime64,
    forecast_issue_time: np.datetime64,
    uncorrected_blend_artifact_id: str,
    identity_correction_artifact_id: str,
) -> xr.Dataset:
    """Assemble ``baseline-forecast.v2``. ``values``/``states`` map
    ``(variable_id, location, target_horizon) -> value/state``. A
    missing value for an ``unavailable``/``inconsistent`` state is
    represented as NaN; every other combination requires a finite
    value."""
    n_horizon = len(_EXPECTED_TARGET_HORIZONS)
    n_location = len(_EXPECTED_LOCATIONS)

    data_vars: dict[str, tuple[tuple[str, ...], np.ndarray]] = {}
    missing: list[str] = []

    for variable_id in _FORECAST_VARIABLES:
        value_array = np.full((n_horizon, n_location), np.nan, dtype=np.float64)
        state_array = np.zeros((n_horizon, n_location), dtype=np.uint8)
        for h_index, horizon in enumerate(_EXPECTED_TARGET_HORIZONS):
            for l_index, location in enumerate(_EXPECTED_LOCATIONS):
                key = (variable_id, location, horizon)
                state = states.get(key)
                if state is None:
                    missing.append(f"{key!r} missing state")
                    continue
                if state not in _STATE_CODES:
                    raise BaselineV2AssemblyError(
                        f"{key!r} has unknown availability state {state!r}; expected one of "
                        f"{sorted(_STATE_CODES)!r}"
                    )
                state_array[h_index, l_index] = _STATE_CODES[state]
                if state in ("unavailable", "inconsistent"):
                    continue
                value = values.get(key)
                if value is None or not np.isfinite(value):
                    missing.append(f"{key!r} missing/non-finite value for state {state!r}")
                    continue
                value_array[h_index, l_index] = value

        data_vars[variable_id] = (("target_horizon", "location"), value_array.astype(np.float32))
        data_vars[f"{variable_id}_state"] = (("target_horizon", "location"), state_array)

    if missing:
        raise BaselineV2AssemblyError(
            f"baseline-forecast.v2 assembly is missing {len(missing)} required value(s): "
            + "; ".join(missing)
        )

    valid_time = target_reference_time + np.array(
        [np.timedelta64(h, "h") for h in _EXPECTED_TARGET_HORIZONS]
    )

    dataset = xr.Dataset(
        data_vars=data_vars,
        coords={
            "target_horizon": list(_EXPECTED_TARGET_HORIZONS),
            "location": list(_EXPECTED_LOCATIONS),
            "target_valid_time": ("target_horizon", valid_time),
        },
        attrs={
            "schema_version": "baseline-forecast.v2",
            "target_reference_time": str(target_reference_time),
            "forecast_issue_time": str(forecast_issue_time),
            "uncorrected_blend_artifact_id": uncorrected_blend_artifact_id,
            "identity_correction_artifact_id": identity_correction_artifact_id,
        },
    )
    return dataset
