"""Compose retained point extraction with the approved surface scientific operators."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import StationAlignmentError, align_station_to_model
from mesoforge.application.precipitation_forecast import QPF, extract_precipitation_contributors
from mesoforge.catalog.contributors import SURFACE_MODEL_FIELDS
from mesoforge.forecasting.baseline import derive_wind_speed_and_direction
from mesoforge.forecasting.field_blend import BlendState
from mesoforge.forecasting.surface import (
    SurfaceBlendError,
    relative_humidity_percent,
)

FIELD_UNITS = {
    "air_temperature_2m": "K",
    "dew_point_temperature_2m": "K",
    "eastward_wind_10m": "m/s",
    "northward_wind_10m": "m/s",
    "wind_gust_10m": "m/s",
}


def extract_surface_inputs(
    *,
    datasets: dict[str, tuple[xr.Dataset, pyproj.CRS, dict[str, Any] | None]],
    temperature_sources: list[dict[str, Any]],
    latitude: float,
    longitude: float,
    horizon: int,
    target_reference_time: np.datetime64,
    selection: dict[str, Any] | None,
) -> tuple[BlendState, dict[str, dict[str, Any]]]:
    """Extract each available native field at the same exact coordinate/valid time.

    All datasets are already loaded. Native IFS gaps remain gaps; this function
    never calls a provider, changes a source value, or interpolates across time.
    """
    valid = target_reference_time + np.timedelta64(horizon, "h")
    contributors: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, float | None]] = {}
    for temperature_source in temperature_sources:
        model = temperature_source["model"]
        fields: dict[str, Any] = {
            "air_temperature_2m": {
                **deepcopy(temperature_source["temperature"]),
                "missing_reasons": list(temperature_source["missing_reasons"]),
            }
        }
        contributor = contributors[model] = {
            "model": model,
            "cycle": temperature_source["cycle"],
            "source_lead_hours": temperature_source["source_lead_hours"],
            "role": "active" if temperature_source["weight"] else "shadow",
            "fields": fields,
        }
        entry = datasets.get(model)
        for variable, unit in FIELD_UNITS.items():
            if variable == "air_temperature_2m":
                continue
            field = fields[variable] = {"value": None, "unit": unit, "missing_reasons": []}
            reasons = field["missing_reasons"]
            if entry is None:
                reasons.append(f"{model}: no prepared guidance covers this coordinate")
                continue
            dataset, crs, manifest = entry
            cycle = dataset["forecast_reference_time"].values.astype("datetime64[ns]")[()]
            lead = int((valid - cycle) / np.timedelta64(1, "h"))
            if not np.any(dataset["source_valid_time"].values == valid):
                reasons.append(f"{model}: no native guidance at this valid time; no interpolation")
                continue
            if selection:
                candidates = selection["models"][model]["candidates"]
                candidate = next(c for c in candidates if c["status"] == "metadata_complete")
                probe = next(p for p in candidate["probes"] if p["source_lead_hours"] == lead)
                if variable in probe.get("missing_fields", {}):
                    reasons.append(probe["missing_fields"][variable])
            if reasons:
                continue
            decoded_reasons = json.loads(dataset.attrs.get("field_missing_reasons_json", "{}"))
            reason = decoded_reasons.get(variable, {}).get(str(lead), [])
            reasons.extend([reason] if isinstance(reason, str) else reason)
            if reasons:
                continue
            if variable not in dataset:
                reasons.append(f"{model}: {variable} was not prepared under a supported contract")
                continue
            if manifest is not None:
                row = next(
                    r
                    for r in manifest["inputs"]
                    if r["source_lead_hours"] == lead and r["model"] == model
                )
                evidence = next(
                    (
                        r
                        for r in row.get("extra_messages", [])
                        if r["canonical_variable_id"] == variable
                    ),
                    None,
                )
                if evidence is None:
                    reasons.append(f"{model}: retained source evidence for {variable} is missing")
                    continue
                field["provenance"] = {
                    **{k: deepcopy(v) for k, v in row.items() if k != "extra_messages"},
                    **deepcopy(evidence),
                    "prepared_sha256": manifest["prepared_files"][model]["sha256"],
                    "wind_reference": dataset.attrs.get("wind_reference"),
                }
            try:
                aligned = align_station_to_model(
                    dataset,
                    crs=crs,
                    station_latitude=latitude,
                    station_longitude=longitude,
                    canonical_variable_id=variable,
                    target_horizon_hours=(horizon,),
                    target_reference_time=target_reference_time,
                )
                value = aligned[horizon].value
                if not math.isfinite(value):
                    raise ValueError("Non-finite aligned value")
            except (StationAlignmentError, KeyError, ValueError):
                reasons.append(f"{model}: {variable} requires finite native grid corners")
            else:
                field["value"] = value
        model_values = values[model] = {key: row["value"] for key, row in fields.items()}
        temp, dew = model_values["air_temperature_2m"], model_values["dew_point_temperature_2m"]
        humidity = fields["relative_humidity_2m"] = {
            "value": None,
            "unit": "percent",
            "missing_reasons": [],
            "derived_from": ["air_temperature_2m", "dew_point_temperature_2m"],
        }
        if temp is None or dew is None:
            humidity["missing_reasons"].append("Temperature and dew point are both required")
        else:
            try:
                humidity["value"] = relative_humidity_percent(temperature_k=temp, dew_point_k=dew)
            except SurfaceBlendError as exc:
                humidity["missing_reasons"].append(str(exc))
        u, v = model_values["eastward_wind_10m"], model_values["northward_wind_10m"]
        speed, direction = None, None
        if u is not None and v is not None:
            speed_array, direction_array = derive_wind_speed_and_direction(
                eastward_m_s=np.array([u]), northward_m_s=np.array([v])
            )
            speed = float(speed_array[0])
            direction = float(direction_array[0]) if math.isfinite(direction_array[0]) else None
        for variable, derived_value, unit in (
            ("wind_speed_10m", speed, "m/s"),
            ("wind_from_direction_10m", direction, "degree"),
        ):
            fields[variable] = {
                "value": derived_value,
                "unit": unit,
                "derived_from": ["eastward_wind_10m", "northward_wind_10m"],
                "missing_reasons": []
                if derived_value is not None
                else [
                    "Calm wind has no direction"
                    if speed == 0
                    else "Both earth-relative wind components are required"
                ],
            }
        contributor["native_supported_fields"] = list(SURFACE_MODEL_FIELDS.get(model, ()))
    native_qpf = extract_precipitation_contributors(
        datasets=datasets,
        models=list(contributors),
        latitude=latitude,
        longitude=longitude,
        horizon=horizon,
        target_reference_time=target_reference_time,
    )
    state = BlendState(horizon=horizon, contributors=values, precipitation=native_qpf)
    for model, contributor in contributors.items():
        contributor["fields"][QPF] = native_qpf[model]
        if model in ("HRRR", "GFS") and model in datasets and QPF in datasets[model][0]:
            contributor["native_supported_fields"].append(QPF)
    return state, contributors
