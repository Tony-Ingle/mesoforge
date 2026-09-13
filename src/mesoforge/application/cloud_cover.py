"""Native total-cloud contributors and the temporary NBM-only active baseline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import (
    PointExtractionError,
    bilinear_interpolate,
    project_station_point,
)
from mesoforge.application.spatial_coverage import point_in_grid, validate_coordinate
from mesoforge.forecasting.cloud_cover import (
    CLOUD_ACTIVE_POLICY,
    NATIVE_PERCENT_FACTORS,
    SKY_CATEGORY_POLICY,
    cloud_percentage,
    sky_category,
    validate_active_cloud_field,
)

UNIT = "percent"


@dataclass(frozen=True)
class CloudView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Cloud-cover times require an explicit timezone")
    return result.astimezone(UTC)


def _base_row(model: str, valid_time: str) -> dict[str, Any]:
    return {
        "model": model,
        "value": None,
        "unit": UNIT,
        "native_value": None,
        "cloud_definition": "total_cloud_cover",
        "vertical_extent": "entire_atmosphere",
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "valid_time": valid_time,
        "role": "shadow",
        "active_weight": 0.0,
        "status": "unavailable",
        "sky_category": None,
        "missing_reasons": [],
    }


def _extract(
    view: CloudView, *, latitude: float, longitude: float, valid_time: str
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
            raise ValueError("No unique native cloud valid time; no temporal interpolation")
        index, event = selected[0]
        row.update(deepcopy(event))
        row.update(
            value=None,
            native_value=None,
            sky_category=None,
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
            or event.get("cloud_definition") != "total_cloud_cover"
            or event.get("vertical_extent") != "entire_atmosphere"
            or event.get("unit") != UNIT
        ):
            raise ValueError("Cloud definition, vertical extent, native time or units incompatible")
        factor = event["native_factor_to_percent"]
        native_unit = event["native_unit"]
        if factor != NATIVE_PERCENT_FACTORS.get(native_unit):
            raise ValueError("Native cloud units/conversion factor incompatible")
        ds = view.dataset
        field, native = ds["cloud_cover"], ds["native_cloud_cover"]
        if (
            ds.sizes.get("event") != len(events)
            or field.dims != ("event", "y", "x")
            or native.dims != field.dims
            or field.attrs.get("units") != UNIT
            or native.attrs.get("units") != native_unit
        ):
            raise ValueError("Prepared cloud dimensions/units disagree with metadata")
        x, y = ds.x.values, ds.y.values
        if any(
            len(axis) < 2
            or not np.isfinite(axis).all()
            or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
            for axis in (x, y)
        ):
            raise ValueError("Cloud extraction requires finite strictly monotonic native axes")
        px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
        extracted = bilinear_interpolate(
            field=field.values[index], x=x, y=y, station_x=px, station_y=py
        )
        cell = extracted.cell
        indices = np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])
        corners, native_corners = field.values[index][indices], native.values[index][indices]
        for value in corners.ravel():
            cloud_percentage(float(value))
        for value in native_corners.ravel():
            cloud_percentage(float(value), native_unit)
        if not np.allclose(native_corners * factor, corners, rtol=1e-12, atol=1e-12):
            raise ValueError("Native cloud values/units do not reproduce normalized percentages")
        # The equivalent four-term weighted sum can round a constant 100% field
        # above 100. Nested linear interpolation preserves constant endpoints;
        # invalid native values still fail strict range checks above, without clipping.
        tx = float((px - x[cell.x0]) / (x[cell.x1] - x[cell.x0]))
        ty = float((py - y[cell.y0]) / (y[cell.y1] - y[cell.y0]))
        value = cloud_percentage(_nested_bilinear(corners, tx, ty))
        native_value = _nested_bilinear(native_corners, tx, ty)
        row.update(
            value=value,
            native_value=native_value,
            status="available",
            sky_category=sky_category(value),
            spatial_extraction={
                "method": "native_grid_bilinear_cloud_percentage",
                "calculation": "nested_linear_interpolation.v1",
                "source_y": [cell.y0, cell.y1],
                "source_x": [cell.x0, cell.x1],
                "weights": [
                    extracted.weights.w00,
                    extracted.weights.w01,
                    extracted.weights.w10,
                    extracted.weights.w11,
                ],
                "cloud_percent_at_source_corners": corners.ravel().tolist(),
                "native_cloud_cover_at_source_corners": native_corners.ravel().tolist(),
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
        row["missing_reasons"].append(f"Cloud extraction unavailable: {exc}")
    return row


def _nested_bilinear(corners: np.ndarray, tx: float, ty: float) -> float:
    a, b, c, d = (float(value) for value in corners.ravel())
    lower = a + tx * (b - a)
    upper = c + tx * (d - c)
    return lower + ty * (upper - lower)


def _comparisons(contributors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for left, right in combinations(contributors, 2):
        reasons = []
        if any(row["status"] != "available" for row in (left, right)):
            reasons.append("Both native cloud contributors must be available")
        elif any(
            left.get(key) != right.get(key)
            for key in (
                "cloud_definition",
                "vertical_extent",
                "temporal_semantics",
                "interval_start",
                "interval_end",
                "unit",
            )
        ) or _time(left["valid_time"]) != _time(right["valid_time"]):
            reasons.append("Cloud definitions, vertical extent, native times or units differ")
        results.append(
            {
                "models": [left["model"], right["model"]],
                "status": "incompatible" if reasons else "comparable",
                "difference_left_minus_right": None if reasons else left["value"] - right["value"],
                "unit": "percentage_point",
                "missing_reasons": reasons,
                "interpretation": "Descriptive model disagreement at the same location/valid time; "
                "native resolution and parameterizations remain distinct; no skill inference.",
            }
        )
    return results


def extract_cloud_contributors(
    views: list[CloudView],
    *,
    latitude: float,
    longitude: float,
    valid_time: str,
    source_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Retain all native evidence, activating only eligible same-time NBM total cloud."""
    validate_coordinate(latitude, longitude)
    _time(valid_time)
    grouped: dict[str, list[CloudView]] = {}
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
            row.update(
                value=None,
                native_value=None,
                sky_category=None,
                unit=UNIT,
                role="shadow",
                active_weight=0.0,
                status="unavailable",
            )
            row["missing_reasons"] = row.get("missing_reasons") or [
                row.get("missing_reason") or "No retained native total cloud-cover guidance"
            ]
        row["extraction_coordinate"] = {"latitude": latitude, "longitude": longitude}
        contributors.append(row)
    active = _active_field(contributors, valid_time)
    return {
        "field": active,
        "contributors": contributors,
        "comparisons": _comparisons(contributors),
        "sky_category_policy": deepcopy(SKY_CATEGORY_POLICY),
        "active_policy": deepcopy(CLOUD_ACTIVE_POLICY),
    }


def _active_field(contributors: list[dict[str, Any]], valid_time: str) -> dict[str, Any]:
    """Convert eligible NBM percent to the existing fraction field; never select a fallback."""
    nbm = next((row for row in contributors if row["model"] == "NBM"), None)
    if nbm is None:
        nbm = _base_row("NBM", valid_time)
        nbm["missing_reasons"] = ["No retained contributor has the required NBM identity"]
    field = deepcopy(nbm)
    field.update(
        value=None,
        cloud_percentage=None,
        unit="1",
        weights={},
        policy=deepcopy(CLOUD_ACTIVE_POLICY),
        role="temporary_active_baseline",
        active_weight=0.0,
        sky_category_policy=deepcopy(SKY_CATEGORY_POLICY),
    )
    if nbm["status"] != "available":
        return field
    field.update(
        value=nbm["value"] / 100,
        cloud_percentage=nbm["value"],
        weights={"NBM": 1.0},
        active_weight=1.0,
    )
    try:
        validate_active_cloud_field(field, valid_time=valid_time)
    except ValueError as exc:
        field.update(
            value=None,
            cloud_percentage=None,
            native_value=None,
            sky_category=None,
            status="unavailable",
            weights={},
            active_weight=0.0,
            missing_reasons=[f"Active NBM total-cloud baseline unavailable: {exc}"],
        )
        field.pop("spatial_extraction", None)
    else:
        nbm.update(role="active", active_weight=1.0)
    return field
