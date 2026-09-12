"""Separate native visibility contributors on the local forecast grid."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from mesoforge.forecasting.visibility import (
    NATIVE_METRE_FACTORS,
    UNIT,
    VISIBILITY,
    visibility_metres,
    visibility_miles,
)

__all__ = ["VISIBILITY", "VisibilityView", "extract_visibility_contributors"]


@dataclass(frozen=True)
class VisibilityView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Visibility times require an explicit timezone")
    return result.astimezone(UTC)


def _base_row(model: str, valid_time: str) -> dict[str, Any]:
    return {
        "model": model,
        "value": None,
        "unit": UNIT,
        "native_value": None,
        "display_miles": None,
        "visibility_definition": "horizontal_visibility",
        "vertical_extent": "surface",
        "spatial_support": "native_model_grid",
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "valid_time": valid_time,
        "role": "shadow",
        "active_weight": 0.0,
        "status": "unavailable",
        "missing_reasons": [],
    }


def _extract(
    view: VisibilityView, *, latitude: float, longitude: float, valid_time: str
) -> dict[str, Any]:
    row = _base_row(view.manifest["model"], valid_time)
    for source in (view.manifest.get("source_metadata", {}), view.manifest):
        for key in (
            "source_cycle",
            "provider",
            "product",
            "native_unit",
            "native_parameter",
            "native_semantics",
            "spatial_support",
            "temporal_support",
            "documentation",
            "licence",
            "attribution",
            "manifest_sha256",
            "prepared_file",
            "provenance",
            "definition_note",
            "censoring",
            "source_limits",
        ):
            if key in source:
                row[key] = deepcopy(source[key])
    try:
        events = view.manifest["events"]
        target = _time(valid_time)
        selected = [
            (index, event)
            for index, event in enumerate(events)
            if event.get("valid_time") is not None and _time(event["valid_time"]) == target
        ]
        if len(selected) != 1:
            raise ValueError("No unique native visibility valid time; no temporal interpolation")
        index, event = selected[0]
        row.update(deepcopy(event))
        row.update(
            value=None,
            native_value=None,
            display_miles=None,
            unit=UNIT,
            status="unavailable",
            role="shadow",
            active_weight=0.0,
        )
        row["missing_reasons"] = list(event.get("missing_reasons", []))
        if row["missing_reasons"]:
            return row
        cycle = _time(event["source_cycle"])
        lead = event["source_lead_hours"]
        if (
            not np.isfinite(lead)
            or lead < 0
            or cycle + timedelta(hours=lead) != target
            or event.get("temporal_semantics") != "instantaneous"
            or event.get("interval_start") is not None
            or event.get("interval_end") is not None
            or event.get("visibility_definition") != "horizontal_visibility"
            or event.get("vertical_extent") != "surface"
            or event.get("spatial_support") != "native_model_grid"
            or event.get("unit") != UNIT
        ):
            raise ValueError(
                "Visibility definition, spatial support, native time or units incompatible"
            )
        factor, native_unit = event["native_factor_to_m"], event["native_unit"]
        if factor != NATIVE_METRE_FACTORS.get(native_unit):
            raise ValueError("Native visibility units/conversion factor incompatible")
        ds = view.dataset
        field, native = ds[VISIBILITY], ds["native_visibility"]
        if (
            ds.sizes.get("event") != len(events)
            or field.dims != ("event", "y", "x")
            or native.dims != field.dims
            or field.attrs.get("units") != UNIT
            or native.attrs.get("units") != native_unit
        ):
            raise ValueError("Prepared visibility dimensions/units disagree with metadata")
        x, y = ds.x.values, ds.y.values
        if any(
            len(axis) < 2
            or not np.isfinite(axis).all()
            or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
            for axis in (x, y)
        ):
            raise ValueError("Visibility extraction requires finite strictly monotonic native axes")
        px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
        # Reuse the existing no-extrapolation bound check, then select the nearest
        # native cell instead of smoothing sharp visibility restrictions.
        find_enclosing_cell(x=x, y=y, station_x=px, station_y=py)
        xi, yi = int(np.argmin(abs(x - px))), int(np.argmin(abs(y - py)))
        value = visibility_metres(float(field.values[index, yi, xi]))
        native_value = float(native.values[index, yi, xi])
        normalized_native = visibility_metres(native_value, native_unit)
        if not np.isclose(normalized_native, value, rtol=1e-12, atol=1e-12):
            raise ValueError("Native visibility values/units do not reproduce normalized metres")
        row.update(
            value=value,
            native_value=native_value,
            display_miles=visibility_miles(value),
            status="available",
            spatial_extraction={
                "method": "nearest_native_grid_cell",
                "policy": "visibility-nearest-native-cell.v1",
                "tie_break": "lower_stored_axis_index",
                "source_y": [yi],
                "source_x": [xi],
                "weights": [1.0],
                "source_coordinate": {"x": float(x[xi]), "y": float(y[yi])},
                "native_coordinate_distance": hypot(float(x[xi] - px), float(y[yi] - py)),
                "native_coordinate_unit": view.crs.axis_info[0].unit_name,
                "visibility_m_at_source_cell": value,
                "native_visibility_at_source_cell": native_value,
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
        row["missing_reasons"].append(f"Visibility extraction unavailable: {exc}")
    return row


def _comparisons(contributors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for left, right in combinations(contributors, 2):
        reasons = []
        if any(row["status"] != "available" for row in (left, right)):
            reasons.append("Both native visibility contributors must be available")
        elif any(
            left.get(key) != right.get(key)
            for key in (
                "visibility_definition",
                "vertical_extent",
                "spatial_support",
                "temporal_semantics",
                "interval_start",
                "interval_end",
                "unit",
            )
        ) or _time(left["valid_time"]) != _time(right["valid_time"]):
            reasons.append("Visibility definitions, spatial support, native times or units differ")
        result.append(
            {
                "models": [left["model"], right["model"]],
                "status": "incompatible" if reasons else "comparable",
                "difference_left_minus_right": None if reasons else left["value"] - right["value"],
                "unit": UNIT,
                "missing_reasons": reasons,
                "interpretation": "Descriptive native surface visibility disagreement; model "
                "diagnostics and resolution differ. No cause, intensity or skill inference.",
            }
        )
    return result


def extract_visibility_contributors(
    views: list[VisibilityView],
    *,
    latitude: float,
    longitude: float,
    valid_time: str,
    source_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract native visibility evidence without choosing an unapproved delivered source."""
    validate_coordinate(latitude, longitude)
    _time(valid_time)
    grouped: dict[str, list[VisibilityView]] = {}
    for view in views:
        grouped.setdefault(view.manifest["model"], []).append(view)
    sources = source_status or {}
    contributors = []
    for model in dict.fromkeys(("HRRR", "GFS", "RAP", "IFS", "NBM", *grouped, *sources)):
        regions = grouped.get(model, [])
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
            row = _base_row(model, valid_time)
            row.update(deepcopy(sources.get(model, {})))
            unsupported = row.get("supported") is False or row.get("status") == "unsupported"
            row.update(
                value=None,
                native_value=None,
                display_miles=None,
                unit=UNIT,
                role="shadow",
                active_weight=0.0,
                status="unsupported" if unsupported else "unavailable",
            )
            row["missing_reasons"] = row.get("missing_reasons") or [
                row.get("missing_reason") or "No retained native surface visibility guidance"
            ]
        row["extraction_coordinate"] = {"latitude": latitude, "longitude": longitude}
        contributors.append(row)
    return {
        "field": {
            "value": None,
            "unit": UNIT,
            "status": "policy_unavailable",
            "weights": {},
            "valid_time": valid_time,
            "temporal_semantics": "instantaneous",
            "interval_start": None,
            "interval_end": None,
            "policy": "no-approved-visibility-blend-policy",
            "missing_reasons": [
                "No approved visibility blend or active source policy; "
                "native contributors remain separate zero-weight evidence"
            ],
        },
        "contributors": contributors,
        "comparisons": _comparisons(contributors),
    }
