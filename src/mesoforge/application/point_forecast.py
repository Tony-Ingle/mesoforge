"""Small prepared-input temperature demonstration, separate from operational guidance.

The files contain invented fields on a latitude/longitude grid. Model labels identify
demonstration contributors; they do not describe real HRRR or GFS native grids.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import StationAlignmentError, align_station_to_model
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar

_DATA_KIND = "synthetic_demonstration"
_NOTICE = "Synthetic demonstration data; not a current weather forecast."
_VARIABLE = "air_temperature_2m"
_WEIGHTS = {"HRRR": 0.7, "GFS": 0.3}
_CRS = pyproj.CRS.from_epsg(4326)


class UnsupportedCoordinateError(ValueError):
    """The requested coordinate is outside the demonstration's supported rectangle."""


def _iso(value: np.datetime64) -> str:
    return str(np.datetime_as_string(value, unit="s")) + "Z"


def _target_time(value: object) -> np.datetime64:
    if not isinstance(value, str):
        raise ValueError("target_reference_time must be an explicit UTC timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("target_reference_time must be an explicit UTC timestamp")
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("target_reference_time must identify an exact hour")
    return np.datetime64(parsed.replace(tzinfo=None), "ns")


def prepare_demo_files(directory: Path) -> None:
    """Populate an empty directory with deterministic synthetic files, without overwrites."""
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        return
    target = np.datetime64("2026-08-30T12:00:00", "ns")
    x = np.array([-93.5, -93.25, -93.0], dtype=np.float64)
    y = np.array([45.5, 45.75, 46.0], dtype=np.float64)
    longitude, latitude = np.meshgrid(x, y)
    for model, age, base, lat_factor, lon_factor in (
        ("HRRR", 0, 280.0, 2.0, 4.0),
        ("GFS", 6, 290.0, 4.0, 2.0),
    ):
        cycle = target - np.timedelta64(age, "h")
        leads = np.array([1 + age, 2 + age, 3 + age], dtype="timedelta64[h]").astype(
            "timedelta64[ns]"
        )
        values = np.stack(
            [
                base + hour + lat_factor * (latitude - 45.5) + lon_factor * (longitude + 93.5)
                for hour in (1, 2, 3)
            ]
        )
        dataset = xr.Dataset(
            data_vars={_VARIABLE: (("source_lead_time", "y", "x"), values, {"unit_id": "K"})},
            coords={
                "x": x,
                "y": y,
                "forecast_reference_time": cycle,
                "source_lead_time": leads,
                "source_valid_time": ("source_lead_time", cycle + leads),
            },
            attrs={
                "model": model,
                "data_kind": _DATA_KIND,
                "notice": _NOTICE,
                "target_reference_time": _iso(target),
                "grid_description": "Synthetic latitude/longitude demonstration grid",
            },
        )
        with (directory / f"{model}.nc").open("x+b") as output:
            dataset.to_netcdf(output, engine="h5netcdf", format="NETCDF4")


def _validate_guidance(dataset: xr.Dataset, model: str) -> np.datetime64:
    if dataset.attrs.get("model") != model or dataset.attrs.get("data_kind") != _DATA_KIND:
        raise ValueError(f"{model}: file must identify the matching synthetic contributor")
    target = _target_time(dataset.attrs.get("target_reference_time"))
    required = {
        _VARIABLE,
        "x",
        "y",
        "forecast_reference_time",
        "source_lead_time",
        "source_valid_time",
    }
    if not required.issubset(dataset.variables):
        raise ValueError(f"{model}: prepared guidance is missing required fields or coordinates")
    field = dataset[_VARIABLE]
    if field.attrs.get("unit_id") != "K" or field.attrs.get("units", "K") != "K":
        raise ValueError(f"{model}: prepared temperature must use K")
    if field.dims != ("source_lead_time", "y", "x") or field.dtype.kind != "f":
        raise ValueError(f"{model}: temperature must have floating source_lead_time/y/x values")
    for axis in ("x", "y"):
        values = dataset[axis].values
        if (
            dataset[axis].dims != (axis,)
            or len(values) < 2
            or not np.all(np.isfinite(values))
            or not (np.all(np.diff(values) > 0) or np.all(np.diff(values) < 0))
        ):
            raise ValueError(f"{model}: {axis} must be a finite, strictly monotonic grid axis")
    cycle = dataset["forecast_reference_time"]
    leads = dataset["source_lead_time"]
    valid = dataset["source_valid_time"]
    if (
        cycle.ndim != 0
        or cycle.dtype.kind != "M"
        or leads.dims != ("source_lead_time",)
        or leads.dtype.kind != "m"
        or valid.dims != ("source_lead_time",)
        or valid.dtype.kind != "M"
    ):
        raise ValueError(f"{model}: source cycle, leads and valid times have invalid metadata")
    cycle_value = cast(np.datetime64, cycle.values.astype("datetime64[ns]")[()])
    lead_values = leads.values.astype("timedelta64[ns]")
    if (
        np.isnat(cycle_value)
        or cycle_value > target
        or cycle_value.astype("datetime64[h]") != cycle_value
        or np.any(np.isnat(lead_values))
        or np.any(lead_values < np.timedelta64(0, "h"))
        or np.any(lead_values % np.timedelta64(1, "h") != np.timedelta64(0, "ns"))
        or len(np.unique(lead_values)) != len(lead_values)
        or not np.array_equal(valid.values, cycle_value + lead_values)
    ):
        raise ValueError(f"{model}: source cycle, leads and valid times disagree")
    return target


@dataclass(frozen=True)
class PreparedPointForecast:
    """Eagerly loaded demonstration guidance; request-time calculation does no I/O."""

    _guidance: dict[str, xr.Dataset]
    _target_reference_time: np.datetime64

    @classmethod
    def from_directory(cls, directory: Path) -> PreparedPointForecast:
        guidance: dict[str, xr.Dataset] = {}
        target: np.datetime64 | None = None
        for model in _WEIGHTS:
            path = directory / f"{model}.nc"
            if not path.exists():
                continue
            with xr.open_dataset(path, engine="h5netcdf") as opened:
                dataset = opened.load()
            model_target = _validate_guidance(dataset, model)
            if target is not None and model_target != target:
                raise ValueError("Prepared model files disagree on target_reference_time")
            target = model_target
            guidance[model] = dataset
        if target is None:
            raise ValueError("No prepared demonstration guidance: HRRR.nc and GFS.nc are missing")
        return cls(guidance, target)

    def forecast(self, *, latitude: float, longitude: float) -> dict[str, Any]:
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not 45.5 <= latitude <= 46.0
            or not -93.5 <= longitude <= -93.0
        ):
            raise UnsupportedCoordinateError(
                "Demonstration coordinates must be within latitude 45.5..46.0, "
                "longitude -93.5..-93.0"
            )
        hours: list[dict[str, Any]] = []
        for horizon in (1, 2, 3):
            valid_time = self._target_reference_time + np.timedelta64(horizon, "h")
            sources: list[dict[str, Any]] = []
            contributions: list[Contribution] = []
            reasons: list[str] = []
            for model, weight in _WEIGHTS.items():
                source: dict[str, Any] = {
                    "model": model,
                    "cycle": None,
                    "source_lead_hours": None,
                    "weight": weight,
                }
                sources.append(source)
                dataset = self._guidance.get(model)
                if dataset is None:
                    reasons.append(f"{model}: prepared guidance file is missing")
                    continue
                cycle = cast(
                    np.datetime64,
                    dataset["forecast_reference_time"].values.astype("datetime64[ns]")[()],
                )
                source["cycle"] = _iso(cycle)
                if not np.any(dataset["source_valid_time"].values == valid_time):
                    reasons.append(f"{model}: no guidance for this valid time")
                    continue
                source["source_lead_hours"] = int((valid_time - cycle) / np.timedelta64(1, "h"))
                try:
                    aligned = align_station_to_model(
                        dataset,
                        crs=_CRS,
                        station_latitude=latitude,
                        station_longitude=longitude,
                        canonical_variable_id=_VARIABLE,
                        target_horizon_hours=(horizon,),
                        target_reference_time=self._target_reference_time,
                    )
                except StationAlignmentError:
                    reasons.append(
                        f"{model}: grid coverage or finite corner values are unavailable"
                    )
                    continue
                if horizon not in aligned or not math.isfinite(aligned[horizon].value):
                    reasons.append(f"{model}: no finite temperature for this valid time")
                    continue
                contributions.append(
                    Contribution(model=model, value=aligned[horizon].value, weight=weight)
                )
            temperature = None if reasons else blend_scalar(tuple(contributions)).blended_value
            hours.append(
                {
                    "horizon_hours": horizon,
                    "valid_time": _iso(valid_time),
                    "temperature": {"value": temperature, "unit": "K"},
                    "sources": sources,
                    "missing_reasons": reasons,
                }
            )
        return {
            "data_kind": _DATA_KIND,
            "notice": _NOTICE,
            "latitude": latitude,
            "longitude": longitude,
            "target_reference_time": _iso(self._target_reference_time),
            "hours": hours,
        }
