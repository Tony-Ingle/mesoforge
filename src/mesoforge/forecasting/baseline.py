"""HRRR-only identity baseline assembly (plan Section 3.6, Task 7).

Assembles the ``baseline-forecast.v1`` dataset from the point-extraction
report's per-station/lead/variable values. Derives wind speed/direction
with float64 intermediates; direction is undefined (NaN, with a quality
bit set) at exactly zero speed. Pure NumPy/xarray computation.
"""

from __future__ import annotations

import math

import numpy as np
import xarray as xr

from mesoforge.common.errors import MesoForgeError

_EXPECTED_LOCATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_DIRECTION_UNDEFINED_BIT = np.uint8(1)


class BaselineAssemblyError(MesoForgeError):
    """Raised when the extraction report is missing a required
    station/lead/variable value; baseline generation fails closed
    rather than producing a partial artifact."""


def derive_wind_speed_and_direction(
    *, eastward_m_s: np.ndarray, northward_m_s: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Section 3.6: ``wind_speed_10m = hypot(u,v)``; direction
    ``mod(degrees(atan2(-u,-v)),360)`` when speed > 0, else NaN. Uses
    float64 intermediates regardless of input dtype."""
    u = eastward_m_s.astype(np.float64)
    v = northward_m_s.astype(np.float64)
    speed = np.hypot(u, v)
    with np.errstate(invalid="ignore"):
        direction = np.degrees(np.arctan2(-u, -v)) % 360.0
    direction = np.where(speed > 0, direction, np.nan)
    return speed, direction


def assemble_baseline_forecast(
    *,
    station_values: dict[tuple[str, int, str], float],
    lead_hours: tuple[int, ...],
    forecast_issue_time: np.datetime64,
    hrrr_source_reference_time: np.datetime64,
    contributor_artifact_id: str,
    extraction_report_artifact_id: str,
) -> xr.Dataset:
    """Assemble ``baseline-forecast.v1`` (plan Section 3.6).
    ``station_values`` maps ``(station_id, lead_hours,
    canonical_variable_id) -> value`` for exactly the three fields
    (``air_temperature_2m``, ``eastward_wind_10m``,
    ``northward_wind_10m``); any missing key raises
    ``BaselineAssemblyError`` before an xarray.Dataset is constructed."""
    n_lead = len(lead_hours)
    n_location = len(_EXPECTED_LOCATIONS)

    missing: list[str] = []
    temperature = np.full((n_lead, n_location), np.nan, dtype=np.float64)
    eastward = np.full((n_lead, n_location), np.nan, dtype=np.float64)
    northward = np.full((n_lead, n_location), np.nan, dtype=np.float64)

    for lead_index, lead in enumerate(lead_hours):
        for location_index, station_id in enumerate(_EXPECTED_LOCATIONS):
            for variable_id, target in (
                ("air_temperature_2m", temperature),
                ("eastward_wind_10m", eastward),
                ("northward_wind_10m", northward),
            ):
                key = (station_id, lead, variable_id)
                if key not in station_values:
                    missing.append(f"{key!r}")
                    continue
                value = station_values[key]
                if not math.isfinite(value):
                    missing.append(f"{key!r} (non-finite value {value!r})")
                    continue
                target[lead_index, location_index] = value

    if missing:
        raise BaselineAssemblyError(
            f"baseline assembly is missing {len(missing)} required station/lead/variable "
            f"value(s): {'; '.join(missing)}"
        )

    speed, direction = derive_wind_speed_and_direction(
        eastward_m_s=eastward, northward_m_s=northward
    )

    direction_quality_mask = np.where(speed == 0, _DIRECTION_UNDEFINED_BIT, np.uint8(0))

    lead_time = np.array([np.timedelta64(h, "h") for h in lead_hours], dtype="timedelta64[ns]")
    valid_time = forecast_issue_time + lead_time

    dataset = xr.Dataset(
        data_vars={
            "air_temperature_2m": (("lead_time", "location"), temperature.astype(np.float32)),
            "eastward_wind_10m": (("lead_time", "location"), eastward.astype(np.float32)),
            "northward_wind_10m": (("lead_time", "location"), northward.astype(np.float32)),
            "wind_speed_10m": (("lead_time", "location"), speed.astype(np.float32)),
            "wind_from_direction_10m": (("lead_time", "location"), direction.astype(np.float32)),
            "wind_from_direction_10m_quality_mask": (
                ("lead_time", "location"),
                direction_quality_mask,
            ),
        },
        coords={
            "lead_time": lead_time,
            "location": list(_EXPECTED_LOCATIONS),
            "valid_time": ("lead_time", valid_time),
        },
        attrs={
            "schema_version": "baseline-forecast.v1",
            "forecast_issue_time": str(forecast_issue_time),
            "hrrr_source_reference_time": str(hrrr_source_reference_time),
            "contributor_artifact_id": contributor_artifact_id,
            "extraction_report_artifact_id": extraction_report_artifact_id,
        },
    )
    return dataset
