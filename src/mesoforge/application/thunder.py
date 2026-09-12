"""Native thunder event evidence and an explicit temporary NBM hourly baseline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
from math import hypot
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import (
    PointExtractionError,
    find_enclosing_cell,
    project_station_point,
)
from mesoforge.application.spatial_coverage import point_in_grid, validate_coordinate
from mesoforge.forecasting.thunder import (
    ACTIVE_POLICY,
    NATIVE_FRACTION_FACTORS,
    THUNDER,
    UNIT,
    _time,
    thunder_event_comparison_reasons,
    thunder_fraction,
    thunder_percent,
    validate_thunder_event,
)
from mesoforge.guidance.sources.thunder import SOURCES

__all__ = ["THUNDER", "ThunderView", "extract_thunder_contributors"]


@dataclass(frozen=True)
class ThunderView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _base_row(source_id: str, valid_time: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "value": None,
        "unit": UNIT,
        "native_value": None,
        "display_percent": None,
        "temporal_semantics": "interval_probability",
        "interval_start": None,
        "interval_end": None,
        "valid_time": valid_time,
        "role": "shadow",
        "active_weight": 0.0,
        "status": "unavailable",
        "missing_reasons": [],
    }


def _extract(
    view: ThunderView, *, latitude: float, longitude: float, valid_time: str
) -> dict[str, Any]:
    row = _base_row(view.manifest["source_id"], valid_time)
    for source in (view.manifest.get("source_metadata", {}), view.manifest):
        for key in (
            "model",
            "source_cycle",
            "provider",
            "product",
            "native_unit",
            "native_parameter",
            "event_definition",
            "spatial_support",
            "temporal_support",
            "duration_hours",
            "documentation",
            "licence",
            "attribution",
            "manifest_sha256",
            "prepared_file",
            "provenance",
            "definition_note",
        ):
            if key in source:
                row[key] = deepcopy(source[key])
    try:
        events, target = view.manifest["events"], _time(valid_time)
        selected = [
            (index, event)
            for index, event in enumerate(events)
            if event.get("valid_time") is not None and _time(event["valid_time"]) == target
        ]
        if len(selected) != 1:
            raise ValueError("No unique native thunder event ending now; no temporal filling")
        index, event = selected[0]
        row.update(deepcopy(event))
        row.update(
            source_id=view.manifest["source_id"],
            value=None,
            native_value=None,
            display_percent=None,
            unit=UNIT,
            role="shadow",
            active_weight=0.0,
            status="unavailable",
        )
        row["missing_reasons"] = list(event.get("missing_reasons", []))
        if row["missing_reasons"]:
            return row
        validate_thunder_event(event)
        expected_duration = {"NBM_1H": 1, "NBM_3H": 3, "NBM_6H": 6}.get(row["source_id"])
        if expected_duration is not None and event["duration_hours"] != expected_duration:
            raise ValueError("Native thunder source ID and event duration disagree")
        factor, native_unit = event["native_factor_to_fraction"], event["native_unit"]
        if event.get("unit") != UNIT or factor != NATIVE_FRACTION_FACTORS.get(native_unit):
            raise ValueError("Native thunder probability units/conversion factor incompatible")
        ds = view.dataset
        field, native = ds["thunder_probability"], ds["native_probability"]
        if (
            ds.sizes.get("event") != len(events)
            or field.dims != ("event", "y", "x")
            or native.dims != field.dims
            or field.attrs.get("units") != UNIT
            or native.attrs.get("units") != native_unit
        ):
            raise ValueError("Prepared thunder probability dimensions/units disagree with metadata")
        x, y = ds.x.values, ds.y.values
        if any(
            len(axis) < 2
            or not np.isfinite(axis).all()
            or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
            for axis in (x, y)
        ):
            raise ValueError("Thunder extraction requires finite strictly monotonic native axes")
        px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
        find_enclosing_cell(x=x, y=y, station_x=px, station_y=py)
        xi, yi = int(np.argmin(abs(x - px))), int(np.argmin(abs(y - py)))
        value = thunder_fraction(float(field.values[index, yi, xi]))
        native_value = float(native.values[index, yi, xi])
        if not np.isclose(thunder_fraction(native_value, native_unit), value, rtol=0, atol=1e-15):
            raise ValueError(
                "Native thunder probability does not reproduce the normalized fraction"
            )
        row.update(
            value=value,
            native_value=native_value,
            display_percent=thunder_percent(value),
            status="available",
            spatial_extraction={
                "method": "nearest_native_grid_cell",
                "policy": "thunder-nearest-native.v1",
                "tie_break": "lower_stored_axis_index",
                "source_y": [yi],
                "source_x": [xi],
                "weights": [1.0],
                "source_coordinate": {"x": float(x[xi]), "y": float(y[yi])},
                "native_coordinate_distance": hypot(float(x[xi] - px), float(y[yi] - py)),
                "native_coordinate_unit": view.crs.axis_info[0].unit_name,
                "fraction_at_source_cell": value,
                "native_probability_at_source_cell": native_value,
                "interpretation": "Native probability and event support retained at source cell",
            },
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        OverflowError,
        PointExtractionError,
    ) as exc:
        row["missing_reasons"].append(f"Thunder extraction unavailable: {exc}")
    return row


def _active_field(contributors: list[dict[str, Any]], valid_time: str) -> dict[str, Any]:
    source = next(row for row in contributors if row["source_id"] == ACTIVE_POLICY["source_id"])
    field = deepcopy(source)
    field.update(policy=deepcopy(ACTIVE_POLICY), weights={}, role="temporary_active_baseline")
    if source["status"] != "available":
        return field
    start, end = validate_thunder_event(source)
    expected = SOURCES[ACTIVE_POLICY["source_id"]]
    if (
        source.get("model") != expected["model"]
        or source.get("provider") != expected["provider"]
        or source.get("product") != expected["product"]
        or end - start != timedelta(hours=1)
        or end != _time(valid_time)
        or source["event_definition"] != expected["event_definition"]
        or any(
            source["spatial_support"].get(key) != expected["spatial_support"].get(key)
            for key in ("kind", "geometry_status", "radius_km")
        )
    ):
        field.update(
            value=None,
            native_value=None,
            display_percent=None,
            status="unavailable",
            missing_reasons=[
                "Temporary baseline requires the exact native NBM hourly thunder event"
            ],
        )
        field.pop("spatial_extraction", None)
        return field
    source.update(role="active", active_weight=1.0)
    field.update(active_weight=1.0, weights={"NBM": 1.0})
    return field


def extract_thunder_contributors(
    views: list[ThunderView],
    *,
    latitude: float,
    longitude: float,
    valid_time: str,
    source_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep native periods separate and pass only an eligible NBM hourly probability through."""
    validate_coordinate(latitude, longitude)
    _time(valid_time)
    grouped: dict[str, list[ThunderView]] = {}
    for view in views:
        grouped.setdefault(view.manifest["source_id"], []).append(view)
    sources = source_status or {}
    contributors = []
    for source_id in dict.fromkeys(("NBM_1H", "NBM_3H", "NBM_6H", *grouped, *sources)):
        regions = grouped.get(source_id, [])
        if regions:
            view = next(
                (
                    candidate
                    for candidate in regions
                    if point_in_grid(
                        latitude,
                        longitude,
                        candidate.crs,
                        candidate.dataset.x.values,
                        candidate.dataset.y.values,
                    )
                ),
                regions[0],
            )
            row = _extract(view, latitude=latitude, longitude=longitude, valid_time=valid_time)
        else:
            row = _base_row(source_id, valid_time)
            row.update(deepcopy(sources.get(source_id, {})))
            unsupported = row.get("supported") is False or row.get("status") == "unsupported"
            row.update(
                source_id=source_id,
                value=None,
                native_value=None,
                display_percent=None,
                unit=UNIT,
                role="shadow",
                active_weight=0.0,
                status="unsupported" if unsupported else "unavailable",
            )
            row["missing_reasons"] = row.get("missing_reasons") or [
                row.get("missing_reason") or "No retained native thunder probability event"
            ]
        row["extraction_coordinate"] = {"latitude": latitude, "longitude": longitude}
        contributors.append(row)
    field = _active_field(contributors, valid_time)
    comparisons = []
    for left, right in combinations(contributors, 2):
        reasons = (
            ["Both native thunder probabilities must be available"]
            if any(row["status"] != "available" for row in (left, right))
            else thunder_event_comparison_reasons(left, right)
        )
        comparisons.append(
            {
                "source_ids": [left["source_id"], right["source_id"]],
                "status": "incompatible" if reasons else "comparable",
                "difference_left_minus_right": None if reasons else left["value"] - right["value"],
                "unit": UNIT,
                "missing_reasons": reasons,
                "interpretation": "Only matching thunder events and established spatial support "
                "are comparable; no skill or multisource blend inference",
            }
        )
    return {"field": field, "contributors": contributors, "comparisons": comparisons}
