"""Read-only native ice/FRZR interval evidence sampled on the shared local grid."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
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
from mesoforge.forecasting.ice import (
    CLOSURE,
    FLAT_ICE,
    FREEZING_RAIN,
    FRZR,
    POLICY,
    UNIT,
    _time,
    ice_amount,
    ice_comparison_reasons,
    validate_ice_event,
)
from mesoforge.guidance.sources.ice import SOURCES


@dataclass(frozen=True)
class IceView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _base(source_id: str, valid_time: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "value": None,
        "native_value": None,
        "unit": UNIT,
        "valid_time": valid_time,
        "interval_start": None,
        "interval_end": None,
        "temporal_semantics": "accumulation",
        "role": "shadow",
        "active_weight": 0.0,
        "status": "unavailable",
        "missing_reasons": [],
    }


def _extract(
    view: IceView, *, latitude: float, longitude: float, valid_time: str
) -> dict[str, Any]:
    row = _base(view.manifest["source_id"], valid_time)
    for source in (view.manifest.get("source_metadata", {}), view.manifest):
        for key in (
            "model",
            "provider",
            "product",
            "source_cycle",
            "native_unit",
            "native_parameter",
            "quantity_kind",
            "accretion_geometry",
            "method",
            "spatial_support",
            "temporal_support",
            "definition_note",
            "documentation",
            "parameter_documentation",
            "method_documentation",
            "provenance",
            "manifest_sha256",
            "prepared_file",
        ):
            if key in source:
                row[key] = deepcopy(source[key])
    try:
        events, target = view.manifest["events"], _time(valid_time)
        selected = [
            (i, e)
            for i, e in enumerate(events)
            if e.get("valid_time") is not None and _time(e["valid_time"]) == target
        ]
        if len(selected) != 1:
            raise ValueError("No unique ice interval ending now; no temporal filling")
        index, event = selected[0]
        row.update(deepcopy(event))
        row.update(
            source_id=view.manifest["source_id"],
            value=None,
            native_value=None,
            unit=UNIT,
            role="shadow",
            active_weight=0.0,
            status="unavailable",
        )
        row["missing_reasons"] = list(event.get("missing_reasons", []))
        if row["missing_reasons"]:
            return row
        validate_ice_event(event)
        known = SOURCES.get(row["source_id"])
        if known and any(
            event.get(key) != known.get(key)
            for key in ("model", "quantity_kind", "native_parameter", "accretion_geometry")
        ):
            raise ValueError("Native ice source identity and quantity disagree")
        if known and event["duration_hours"] != (known.get("duration_hours") or 1):
            raise ValueError("Native ice source period or normalized hourly FRZR duration disagree")
        ds, native_unit = view.dataset, event["native_unit"]
        field, native_end = ds["amount"], ds["native_end_amount"]
        if (
            ds.sizes.get("event") != len(events)
            or field.dims != ("event", "y", "x")
            or field.attrs.get("units") != UNIT
            or native_end.dims != field.dims
            or native_end.attrs.get("units") != native_unit
        ):
            raise ValueError("Prepared ice dimensions/units disagree with metadata")
        x, y = ds.x.values, ds.y.values
        if any(
            len(axis) < 2
            or not np.isfinite(axis).all()
            or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
            for axis in (x, y)
        ):
            raise ValueError("Ice extraction requires finite strictly monotonic native axes")
        px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
        find_enclosing_cell(x=x, y=y, station_x=px, station_y=py)
        xi, yi = int(np.argmin(abs(x - px))), int(np.argmin(abs(y - py)))
        quantity = event["quantity_kind"]
        value = ice_amount(float(field.values[index, yi, xi]), quantity_kind=quantity)
        end_value = float(native_end.values[index, yi, xi])
        ice_amount(end_value, quantity_kind=quantity, native_unit=native_unit)
        start_value = None
        if event["normalization"]["method"] == "native_cumulative_end_minus_start_then_unit_factor":
            native_start = ds["native_start_amount"]
            if native_start.dims != field.dims or native_start.attrs.get("units") != native_unit:
                raise ValueError("Native cumulative start-parent dimensions/units disagree")
            start_value = float(native_start.values[index, yi, xi])
            ice_amount(start_value, quantity_kind=quantity, native_unit=native_unit)
        native_value = end_value - (start_value if start_value is not None else 0.0)
        reconstructed = ice_amount(native_value, quantity_kind=quantity, native_unit=native_unit)
        if reconstructed != value:
            raise ValueError("Native accumulation parents do not reproduce the normalized amount")
        row.update(
            value=value,
            native_value=native_value,
            status="available",
            spatial_extraction={
                "method": "nearest_native_grid_cell",
                "policy": "ice-nearest-native.v1",
                "tie_break": "lower_stored_axis_index",
                "source_y": [yi],
                "source_x": [xi],
                "weights": [1.0],
                "source_coordinate": {"x": float(x[xi]), "y": float(y[yi])},
                "native_coordinate_distance": math.hypot(float(x[xi] - px), float(y[yi] - py)),
                "native_coordinate_unit": view.crs.axis_info[0].unit_name,
                "native_end_amount": end_value,
                "native_start_amount": start_value,
                "native_interval_amount": native_value,
                "canonical_interval_amount": value,
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
        row["missing_reasons"].append(f"Ice extraction unavailable: {exc}")
    return row


def extract_ice_contributors(
    views: list[IceView],
    *,
    latitude: float,
    longitude: float,
    valid_time: str,
    source_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep all ice sources as evidence; no active blend or local accretion algorithm."""
    validate_coordinate(latitude, longitude)
    target = _time(valid_time)
    grouped: dict[str, list[IceView]] = {}
    for view in views:
        grouped.setdefault(view.manifest["source_id"], []).append(view)
    statuses, contributors = source_status or {}, []
    for source_id in dict.fromkeys((*SOURCES, *grouped, *statuses)):
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
            row = {
                **_base(source_id, valid_time),
                **deepcopy(SOURCES.get(source_id, {})),
                **deepcopy(statuses.get(source_id, {})),
            }
            unsupported = row.get("supported") is False or row.get("status") == "unsupported"
            row.update(
                source_id=source_id,
                value=None,
                native_value=None,
                unit=UNIT,
                role="shadow",
                active_weight=0.0,
                status="unsupported" if unsupported else "unavailable",
            )
            row["missing_reasons"] = row.get("missing_reasons") or [
                row.get("missing_reason") or "No retained native ice/freezing-rain interval"
            ]
        row["extraction_coordinate"] = {"latitude": latitude, "longitude": longitude}
        contributors.append(row)
    fields = {}
    for field, quantity in ((FLAT_ICE, FLAT_ICE), (FRZR, FREEZING_RAIN)):
        fields[field] = {
            "value": None,
            "unit": UNIT,
            "quantity_kind": quantity,
            "valid_time": valid_time,
            "temporal_semantics": "accumulation",
            "interval_start": (target - timedelta(hours=1)).isoformat(),
            "interval_end": target.isoformat(),
            "interval_closure": CLOSURE,
            "interval_role": "nominal_requested_hour_not_a_native_value",
            "weights": {},
            "status": "policy_unavailable",
            "policy": deepcopy(POLICY),
            "missing_reasons": [
                "No approved blend rule for this quantity; native evidence remains shadow"
            ],
        }
    comparisons = []
    for left, right in combinations(contributors, 2):
        reasons = (
            ["Both native amounts must be available"]
            if any(row["status"] != "available" for row in (left, right))
            else ice_comparison_reasons(left, right)
        )
        comparisons.append(
            {
                "source_ids": [left["source_id"], right["source_id"]],
                "status": "incompatible" if reasons else "comparable",
                "difference_left_minus_right": None if reasons else left["value"] - right["value"],
                "unit": UNIT,
                "interval_start": None if reasons else left["interval_start"],
                "interval_end": None if reasons else left["interval_end"],
                "missing_reasons": reasons,
                "interpretation": "Descriptive same-quantity/interval native disagreement; "
                "nearest samples may use different native grids. No skill or blend inference.",
            }
        )
    return {"fields": fields, "contributors": contributors, "comparisons": comparisons}


def aggregate_ice_intervals(
    contributors: list[dict[str, Any]],
    *,
    start_valid_time: str,
    end_valid_time: str,
) -> dict[str, Any]:
    """Sum only a complete, nonoverlapping single-source/cycle/quantity/location partition."""
    start, end = _time(start_valid_time), _time(end_valid_time)
    if start >= end:
        raise ValueError("Ice aggregation start must precede end")
    components = deepcopy(contributors)
    result: dict[str, Any] = {
        "value": None,
        "unit": UNIT,
        "interval_start": start.isoformat(),
        "interval_end": end.isoformat(),
        "interval_closure": CLOSURE,
        "temporal_semantics": "accumulation",
        "status": "unavailable",
        "method": "sum_contiguous_nonoverlapping_native_intervals",
        "components": components,
        "missing_reasons": [],
    }
    try:
        if not components:
            raise ValueError("No native ice intervals provided")
        first = components[0]
        for row in components:
            validate_ice_event(row)
            if row.get("status") != "available" or row.get("value") is None:
                raise ValueError("Missing component ice accumulation")
            ice_amount(row["value"], quantity_kind=row["quantity_kind"])
            if any(
                row.get(key) != first.get(key)
                for key in (
                    "source_id",
                    "model",
                    "source_cycle",
                    "quantity_kind",
                    "unit",
                    "spatial_support",
                    "accretion_geometry",
                    "extraction_coordinate",
                    "version",
                )
            ):
                raise ValueError(
                    "Cannot mix ice sources, cycles, quantities, versions, coordinates or support"
                )
        components.sort(key=lambda row: _time(row["interval_start"]))
        cursor = start
        for row in components:
            lower, upper = validate_ice_event(row)
            if lower != cursor or upper > end:
                raise ValueError("Ice intervals contain a gap, overlap or incompatible boundary")
            cursor = upper
        if cursor != end:
            raise ValueError("Ice intervals do not cover the complete requested period")
        total = ice_amount(
            math.fsum(row["value"] for row in components), quantity_kind=first["quantity_kind"]
        )
        result.update(
            value=total,
            status="available",
            quantity_kind=first["quantity_kind"],
            source_id=first["source_id"],
            source_cycle=first["source_cycle"],
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["missing_reasons"].append(str(exc))
    return result
