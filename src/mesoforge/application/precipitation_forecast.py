"""Exact-interval QPF extraction and composition with retained Phase 2 rules."""

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

QPF = "liquid_equivalent_precipitation_amount_1h"
_ACTIVE = ("HRRR", "GFS")


def _iso(value: np.datetime64) -> str:
    return str(np.datetime_as_string(value, unit="s")) + "Z"


def _extract(
    model: str,
    entry: tuple[xr.Dataset, pyproj.CRS, dict[str, Any] | None] | None,
    *,
    latitude: float,
    longitude: float,
    horizon: int,
    target: np.datetime64,
) -> dict[str, Any]:
    end = target + np.timedelta64(horizon, "h")
    start = end - np.timedelta64(1, "h")
    field: dict[str, Any] = {
        "value": None,
        "unit": "kg/m^2",
        "temporal_semantics": "accumulation",
        "interval_start": _iso(start),
        "interval_end": _iso(end),
        "interval_closure": "left_open_right_closed",
        "missing_reasons": [],
    }
    reasons = field["missing_reasons"]
    if model not in _ACTIVE:
        reasons.append(f"{model}: precipitation adapter is not enabled; no shadow value invented")
        return field
    if entry is None or QPF not in entry[0]:
        reasons.append(f"{model}: liquid precipitation guidance was not prepared")
        return field
    dataset, crs, manifest = entry
    cycle = dataset.forecast_reference_time.values.astype("datetime64[ns]")[()]
    lead = int((end - cycle) / np.timedelta64(1, "h"))
    field.update(source_cycle=_iso(cycle), source_lead_hours=lead)
    metadata = json.loads(dataset.attrs.get("qpf_metadata_json", "{}"))
    field["normalization"] = deepcopy(metadata.get(str(lead), {}))
    parents = field["normalization"].get("parents", [])
    parent_leads = {parent["source_lead_hours"] for parent in parents} or {lead}
    if manifest is not None:
        field["provenance"] = {
            "prepared_sha256": manifest["prepared_files"][model]["sha256"],
            "source_inputs": deepcopy(
                [
                    row
                    for row in manifest.get("qpf_inputs", [])
                    if row["model"] == model and row["source_lead_hours"] in parent_leads
                ]
            ),
        }
    if (
        dataset[QPF].attrs.get("unit_id") != "kg/m^2"
        or dataset[QPF].attrs.get("units", "kg/m^2") != "kg/m^2"
        or dataset[QPF].attrs.get("temporal_semantics") != "accumulation"
        or dataset[QPF].attrs.get("interval_closure") != "left_open_right_closed"
        or dataset[QPF].dims != ("source_lead_time", "y", "x")
    ):
        reasons.append(f"{model}: QPF requires canonical kg/m^2 and accumulation semantics")
        return field
    missing = json.loads(dataset.attrs.get("field_missing_reasons_json", "{}"))
    declared = missing.get(QPF, {}).get(str(lead), [])
    reasons.extend([declared] if isinstance(declared, str) else declared)
    try:
        bounds = dataset[f"{QPF}_interval_bounds"].values
        index = find_exact_interval_index(
            bounds[:, 0], bounds[:, 1], target_start=start, target_end=end
        )
        if dataset.source_valid_time.values[index] != end:
            raise ValueError("QPF interval end differs from its source valid time")
    except (KeyError, IndexError, ValueError, TemporalAlignmentError):
        reasons.append(
            f"{model}: no exact one-hour accumulation interval ({_iso(start)}, {_iso(end)}]"
        )
        return field
    if reasons:
        return field
    if manifest is not None:
        evidence = field["provenance"]["source_inputs"]
        if (
            len(evidence) != len(parent_leads)
            or {row["source_lead_hours"] for row in evidence} != parent_leads
            or any(row.get("cycle", _iso(cycle)) != _iso(cycle) for row in evidence)
        ):
            reasons.append(
                f"{model}: retained QPF source parent evidence is missing or inconsistent"
            )
            return field
    try:
        aligned = align_station_to_model(
            dataset,
            crs=crs,
            station_latitude=latitude,
            station_longitude=longitude,
            canonical_variable_id=QPF,
            target_horizon_hours=(horizon,),
            target_reference_time=target,
        )[horizon]
        if aligned.source_lead_hour != lead:
            raise ValueError("Source lead and accumulation end disagree")
        corners = dataset[QPF].values[index][
            np.ix_(
                [aligned.source_y0, aligned.source_y1],
                [aligned.source_x0, aligned.source_x1],
            )
        ]
        if not np.all(np.isfinite(corners)) or np.any(corners < 0):
            raise ValueError("QPF requires finite nonnegative native grid corners")
        if not math.isfinite(aligned.value) or aligned.value < 0:
            raise ValueError("QPF must be finite and nonnegative")
    except (KeyError, ValueError, StationAlignmentError) as exc:
        reasons.append(f"{model}: QPF extraction unavailable: {exc}")
        return field
    field["value"] = aligned.value
    field["spatial_extraction"] = {
        "method": "native_grid_bilinear_depth",
        "source_y": [aligned.source_y0, aligned.source_y1],
        "source_x": [aligned.source_x0, aligned.source_x1],
        "weights": list(aligned.weights),
        "conservation_scope": "temporal accumulation; not area-integrated spatial mass",
    }
    if "qpf_finite_precision_floor_applied" in dataset:
        floors = dataset.qpf_finite_precision_floor_applied.values[index]
        field["normalization"]["finite_precision_floor_at_source_corners"] = [
            bool(floors[y, x])
            for y, x in (
                (aligned.source_y0, aligned.source_x0),
                (aligned.source_y0, aligned.source_x1),
                (aligned.source_y1, aligned.source_x0),
                (aligned.source_y1, aligned.source_x1),
            )
        ]
    return field


def extract_precipitation_contributors(
    *,
    datasets: dict[str, tuple[xr.Dataset, pyproj.CRS, dict[str, Any] | None]],
    models: list[str],
    latitude: float,
    longitude: float,
    horizon: int,
    target_reference_time: np.datetime64,
) -> dict[str, dict[str, Any]]:
    """Extract matching hourly depths and evidence; field_blend owns blending."""
    native = {
        model: _extract(
            model,
            datasets.get(model),
            latitude=latitude,
            longitude=longitude,
            horizon=horizon,
            target=target_reference_time,
        )
        for model in dict.fromkeys((*_ACTIVE, *models))
    }
    return native
