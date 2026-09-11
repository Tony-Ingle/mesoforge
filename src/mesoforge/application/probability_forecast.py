"""Extract native NBM hourly event probabilities without using deterministic QPF."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import StationAlignmentError, align_station_to_model
from mesoforge.alignment.temporal import TemporalAlignmentError, find_exact_interval_index
from mesoforge.catalog.configuration import PopBlendPolicy
from mesoforge.forecasting.pop_blend import pop_passthrough

POP = "probability_of_precipitation_1h"
THRESHOLD = {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}


def _iso(value: np.datetime64) -> str:
    return str(np.datetime_as_string(value, unit="s")) + "Z"


def extract_probability_hour(
    entry: tuple[xr.Dataset, pyproj.CRS, dict[str, Any]] | None,
    *,
    latitude: float,
    longitude: float,
    horizon: int,
    target_reference_time: np.datetime64,
    policy: PopBlendPolicy,
    unavailable_reason: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One identical interval/event, native bilinear extraction, approved passthrough.

    Invalid or unavailable native probabilities remain missing. This calculation
    neither combines periods nor changes any deterministic forecast quantity.
    """
    end = target_reference_time + np.timedelta64(horizon, "h")
    start = end - np.timedelta64(1, "h")
    native: dict[str, Any] = {
        "value": None,
        "unit": "1",
        "temporal_semantics": "probability",
        "interval_start": _iso(start),
        "interval_end": _iso(end),
        "interval_closure": "left_open_right_closed",
        "threshold": deepcopy(THRESHOLD),
        "model": "NBM",
        "missing_reasons": [],
    }
    reasons = native["missing_reasons"]

    def finish() -> tuple[dict[str, Any], dict[str, Any]]:
        field = deepcopy(native)
        field.update(
            weights={policy.sole_contributor: policy.weight} if native["value"] is not None else {},
            policy=policy.model_dump(mode="json"),
            status="available" if native["value"] is not None else "unavailable",
        )
        return field, native

    if entry is None:
        reasons.append(unavailable_reason or "NBM: no prepared probability guidance")
        return finish()
    dataset, crs, manifest = entry
    if POP not in dataset:
        reasons.append("NBM: native hourly probability was not prepared")
        return finish()
    cycle = dataset.forecast_reference_time.values.astype("datetime64[ns]")[()]
    lead = int((end - cycle) / np.timedelta64(1, "h"))
    native.update(source_cycle=_iso(cycle), source_lead_hours=lead)
    attrs = dataset[POP].attrs
    source_event = {
        "threshold_kg_m2": attrs.get("probability_threshold_kg_m2"),
        "comparison": attrs.get("probability_comparison"),
        "probability_type": attrs.get("probability_type"),
    }
    native["source_event"] = {
        key: value.item() if isinstance(value, np.generic) else value
        for key, value in source_event.items()
    }
    native["normalization"] = deepcopy(
        json.loads(dataset.attrs.get("pop_metadata_json", "{}")).get(str(lead), {})
    )
    inputs = [row for row in manifest.get("inputs", []) if row.get("source_lead_hours") == lead]
    native["provenance"] = {
        "manifest_sha256": manifest.get("manifest_sha256"),
        "prepared_sha256": manifest.get("prepared_files", {}).get("NBM", {}).get("sha256"),
        "source_inputs": deepcopy(inputs),
    }
    if (
        attrs.get("unit_id") != "1"
        or attrs.get("units") != "1"
        or attrs.get("temporal_semantics") != "probability"
        or attrs.get("interval_closure") != "left_open_right_closed"
        or attrs.get("probability_threshold_kg_m2") != THRESHOLD["value"]
        or attrs.get("probability_comparison") != "gt"
        or attrs.get("probability_type") != 1
        or dataset[POP].dims != ("source_lead_time", "y", "x")
    ):
        reasons.append("NBM: incompatible probability units, event threshold or interval semantics")
        return finish()
    try:
        bounds = dataset[f"{POP}_interval_bounds"].values
        index = find_exact_interval_index(
            bounds[:, 0], bounds[:, 1], target_start=start, target_end=end
        )
        if dataset.source_valid_time.values[index] != end:
            raise ValueError("PoP interval end differs from its source valid time")
    except (KeyError, IndexError, ValueError, TemporalAlignmentError):
        reasons.append(
            f"NBM: no exact native one-hour probability interval ({_iso(start)}, {_iso(end)}]"
        )
        return finish()
    declared = native["normalization"].get("missing_reasons", [])
    reasons.extend([declared] if isinstance(declared, str) else declared)
    if reasons:
        return finish()
    if (
        len(inputs) != 1
        or inputs[0].get("model") != "NBM"
        or inputs[0].get("cycle") != _iso(cycle)
        or inputs[0].get("valid_time") != _iso(end)
        or not native["provenance"]["prepared_sha256"]
    ):
        reasons.append(
            "NBM: retained probability source cycle/lead/valid-time evidence "
            "is missing or inconsistent"
        )
        return finish()
    try:
        aligned = align_station_to_model(
            dataset,
            crs=crs,
            station_latitude=latitude,
            station_longitude=longitude,
            canonical_variable_id=POP,
            target_horizon_hours=(horizon,),
            target_reference_time=target_reference_time,
        )[horizon]
        if aligned.source_lead_hour != lead:
            raise ValueError("Source lead and probability interval end disagree")
        indices = np.ix_(
            [aligned.source_y0, aligned.source_y1], [aligned.source_x0, aligned.source_x1]
        )
        corners = dataset[POP].values[index][indices]
        if not np.all(np.isfinite(corners)) or np.any(corners < 0) or np.any(corners > 1):
            raise ValueError("PoP requires four finite native probability corners in [0,1]")
        if not math.isfinite(aligned.value) or not 0 <= aligned.value <= 1:
            raise ValueError("PoP extraction must be in [0,1]; no clipping is allowed")
        native["value"] = pop_passthrough(aligned.value)
        native["spatial_extraction"] = {
            "method": "native_grid_bilinear_probability",
            "source_y": [aligned.source_y0, aligned.source_y1],
            "source_x": [aligned.source_x0, aligned.source_x1],
            "weights": list(aligned.weights),
            "native_fraction_at_source_corners": corners.ravel().tolist(),
        }
        if "native_probability_percent" in dataset:
            percent = dataset.native_probability_percent.values[index][indices]
            if not np.allclose(percent / 100, corners, rtol=0, atol=1e-15, equal_nan=False):
                raise ValueError("Native percentages disagree with the prepared fractions")
            native["spatial_extraction"]["native_percent_at_source_corners"] = (
                percent.ravel().tolist()
            )
    except (KeyError, ValueError, StationAlignmentError) as exc:
        native["value"] = None
        native.pop("spatial_extraction", None)
        reasons.append(f"NBM: probability extraction unavailable: {exc}")
    return finish()
