"""Native snowfall water equivalent evidence, without an unapproved blend policy."""

from __future__ import annotations

import math
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

SNOW = "snowfall_water_equivalent_amount"
QUANTITY = "snowfall_water_equivalent"
UNIT = "kg/m^2"
CLOSURE = "left_open_right_closed"
_NATIVE_UNIT_FACTORS = {
    "kg/m^2": 1.0,
    "kg m**-2": 1.0,
    "kg m-2": 1.0,
    "mm": 1.0,
    "m": 1000.0,
    "m of water equivalent": 1000.0,
}


@dataclass(frozen=True)
class SnowView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Snowfall accumulation times require an explicit timezone")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _base_row(model: str, valid_time: str) -> dict[str, Any]:
    return {
        "model": model,
        "value": None,
        "unit": UNIT,
        "native_quantity": QUANTITY,
        "role": "shadow",
        "active_weight": 0.0,
        "status": "unavailable",
        "valid_time": valid_time,
        "temporal_semantics": "accumulation",
        "interval_start": None,
        "interval_end": None,
        "interval_closure": CLOSURE,
        "missing_reasons": [],
    }


def _extract(
    view: SnowView, *, latitude: float, longitude: float, valid_time: str
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
            "temporal_support",
            "documentation",
            "licence",
            "attribution",
        ):
            if key in source:
                row[key] = deepcopy(source[key])
    for key in ("manifest_sha256", "prepared_file", "provider", "product", "provenance"):
        if key in view.manifest:
            row[key] = deepcopy(view.manifest[key])
    events = view.manifest["events"]
    native_units = {event["native_unit"] for event in events if event.get("native_unit")}
    if "native_unit" not in row and len(native_units) == 1:
        row["native_unit"] = next(iter(native_units))
    target = _time(valid_time)
    selected = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("interval_end") is not None and _time(event["interval_end"]) == target
    ]
    if len(selected) != 1:
        row["missing_reasons"] = [
            "No unique native snowfall accumulation ending at this valid time; "
            "no splitting or temporal interpolation"
        ]
        row["native_intervals"] = [
            {
                key: deepcopy(event.get(key))
                for key in (
                    "interval_start",
                    "interval_end",
                    "source_cycle",
                    "source_lead_hours",
                    "native_unit",
                    "missing_reasons",
                )
            }
            for event in events
        ]
        return row
    index, event = selected[0]
    row.update(deepcopy(event))
    # Source metadata cannot opt itself into production or supply a computed value.
    row.update(value=None, unit=UNIT, status="unavailable", role="shadow", active_weight=0.0)
    row["missing_reasons"] = list(event.get("missing_reasons", []))
    if row["missing_reasons"]:
        return row
    try:
        start, end, cycle = (
            _time(event[key]) for key in ("interval_start", "interval_end", "source_cycle")
        )
        lead = event["source_lead_hours"]
        if (
            start >= end
            or start < cycle
            or cycle + timedelta(hours=lead) != end
            or event.get("valid_time") is not None
            and _time(event["valid_time"]) != end
            or event.get("interval_closure") != CLOSURE
            or event.get("temporal_semantics", "accumulation") != "accumulation"
            or event.get("native_quantity") != QUANTITY
        ):
            raise ValueError("Native snowfall quantity, interval, cycle or lead is incompatible")
        factor = event.get("unit_factor_to_kg_m2")
        if factor is None:
            factor = event["normalization"]["unit_factor_to_kg_m2"]
        expected_factor = _NATIVE_UNIT_FACTORS.get(event["native_unit"])
        if expected_factor is None or factor != expected_factor:
            raise ValueError("Native snowfall units/conversion factor are incompatible")
        ds = view.dataset
        amount = ds["amount"]
        if (
            ds.sizes.get("event") != len(events)
            or amount.dims != ("event", "y", "x")
            or amount.attrs.get("units") != UNIT
        ):
            raise ValueError("Prepared snowfall dimensions/units disagree with metadata")
        x, y = ds.x.values, ds.y.values
        if any(
            len(axis) < 2
            or not np.isfinite(axis).all()
            or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
            for axis in (x, y)
        ):
            raise ValueError("Snowfall extraction requires finite strictly monotonic native axes")
        px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
        extracted = bilinear_interpolate(
            field=amount.values[index], x=x, y=y, station_x=px, station_y=py
        )
        cell = extracted.cell
        indices = np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])
        corners = amount.values[index][indices]
        if np.any(corners < 0) or not math.isfinite(extracted.value) or extracted.value < 0:
            raise ValueError("Snowfall requires nonnegative finite amounts; no clipping")
        spatial = {
            "method": "native_grid_bilinear_accumulation",
            "source_y": [cell.y0, cell.y1],
            "source_x": [cell.x0, cell.x1],
            "weights": [
                extracted.weights.w00,
                extracted.weights.w01,
                extracted.weights.w10,
                extracted.weights.w11,
            ],
            "amount_kg_m2_at_source_corners": corners.ravel().tolist(),
        }
        native: dict[str, np.ndarray] = {}
        for name in ("native_end_amount", "native_start_amount"):
            if name not in ds:
                continue
            field = ds[name]
            if field.dims != amount.dims or field.attrs.get("units") != event["native_unit"]:
                raise ValueError("Native snowfall dimensions/units disagree with metadata")
            values = field.values[index][indices]
            if not np.isfinite(values).all() or np.any(values < 0):
                raise ValueError("Native snowfall parent accumulation corners must be nonnegative")
            native[name] = values
            spatial[f"{name}_at_source_corners"] = values.ravel().tolist()
        if native:
            if (
                "native_end_amount" not in native
                or not math.isfinite(factor)
                or factor <= 0
                or not np.allclose(
                    (native["native_end_amount"] - native.get("native_start_amount", 0)) * factor,
                    corners,
                    rtol=1e-12,
                    atol=1e-12,
                )
            ):
                raise ValueError(
                    "Native snowfall parents/units do not reproduce the interval amount"
                )
        row.update(value=extracted.value, status="available", spatial_extraction=spatial)
    except (KeyError, TypeError, ValueError, IndexError, PointExtractionError) as exc:
        row["missing_reasons"].append(f"Snowfall extraction unavailable: {exc}")
    return row


def _comparisons(contributors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for left, right in combinations(contributors, 2):
        reasons = []
        if any(row["status"] != "available" for row in (left, right)):
            reasons.append("Both native snowfall contributors must be available")
        elif (
            _time(left["interval_start"]) != _time(right["interval_start"])
            or _time(left["interval_end"]) != _time(right["interval_end"])
            or left["interval_closure"] != right["interval_closure"]
            or left["unit"] != right["unit"]
            or left["native_quantity"] != right["native_quantity"]
        ):
            reasons.append("Native snowfall accumulation windows, units or quantities differ")
        results.append(
            {
                "models": [left["model"], right["model"]],
                "status": "incompatible" if reasons else "comparable",
                "difference_left_minus_right": None if reasons else left["value"] - right["value"],
                "unit": UNIT,
                "interval_start": None if reasons else left["interval_start"],
                "interval_end": None if reasons else left["interval_end"],
                "missing_reasons": reasons,
                "interpretation": "Descriptive native model disagreement; "
                "no skill or blend inference",
            }
        )
    return results


def extract_snowfall_contributors(
    views: list[SnowView],
    *,
    latitude: float,
    longitude: float,
    valid_time: str,
    source_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract native intervals ending now; no active snowfall rule is approved."""
    validate_coordinate(latitude, longitude)
    target = _time(valid_time)
    grouped: dict[str, list[SnowView]] = {}
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
            row.update(value=None, unit=UNIT, role="shadow", active_weight=0.0)
            if row["status"] == "available":
                row["status"] = "unavailable"
            row["missing_reasons"] = row.get("missing_reasons") or [
                row.get("missing_reason") or "No retained native snowfall water equivalent guidance"
            ]
        row["extraction_coordinate"] = {"latitude": latitude, "longitude": longitude}
        contributors.append(row)
    return {
        "field": {
            "value": None,
            "unit": UNIT,
            "native_quantity": QUANTITY,
            "valid_time": valid_time,
            "temporal_semantics": "accumulation",
            "interval_start": _iso(target - timedelta(hours=1)),
            "interval_end": _iso(target),
            "interval_closure": CLOSURE,
            "interval_role": "nominal_requested_hour_not_a_native_value",
            "weights": {},
            "status": "policy_unavailable",
            "missing_reasons": [
                "No approved snowfall-water-equivalent blend rule; native contributors "
                "remain shadows without an active baseline"
            ],
        },
        "contributors": contributors,
        "comparisons": _comparisons(contributors),
    }


def aggregate_snowfall_intervals(
    contributors: list[dict[str, Any]], *, start_valid_time: str, end_valid_time: str
) -> dict[str, Any]:
    """Sum a complete single-source interval partition, retaining every component."""
    start, end = _time(start_valid_time), _time(end_valid_time)
    if start >= end:
        raise ValueError("Snowfall aggregation start must precede end")
    components = deepcopy(contributors)
    result: dict[str, Any] = {
        "value": None,
        "unit": UNIT,
        "native_quantity": QUANTITY,
        "temporal_semantics": "accumulation",
        "interval_start": _iso(start),
        "interval_end": _iso(end),
        "interval_closure": CLOSURE,
        "status": "unavailable",
        "method": "sum_contiguous_nonoverlapping_native_intervals",
        "components": components,
        "missing_reasons": [],
    }
    try:
        if not components:
            raise ValueError("No native snowfall intervals were provided")
        if any(
            row.get("status") != "available"
            or row.get("value") is None
            or not math.isfinite(row["value"])
            or row["value"] < 0
            for row in components
        ):
            raise ValueError("Missing or invalid component snowfall accumulation")
        identities = {
            (
                row["model"],
                _time(row["source_cycle"]),
                row["native_quantity"],
                row["extraction_coordinate"]["latitude"],
                row["extraction_coordinate"]["longitude"],
            )
            for row in components
        }
        if len(identities) != 1 or any(
            row["unit"] != UNIT
            or row["native_quantity"] != QUANTITY
            or row["interval_closure"] != CLOSURE
            or row.get("temporal_semantics") != "accumulation"
            for row in components
        ):
            raise ValueError(
                "Cannot mix snowfall sources, cycles, coordinates, quantities or units"
            )
        components.sort(key=lambda row: _time(row["interval_start"]))
        cursor = start
        for row in components:
            lower, upper = _time(row["interval_start"]), _time(row["interval_end"])
            if lower != cursor or upper <= lower or upper > end:
                raise ValueError(
                    "Snowfall intervals contain a gap, overlap or incompatible boundary"
                )
            cursor = upper
        if cursor != end:
            raise ValueError("Snowfall intervals do not cover the complete requested period")
        result.update(
            value=math.fsum(row["value"] for row in components),
            status="available",
            model=components[0]["model"],
            source_cycle=components[0]["source_cycle"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        result["missing_reasons"].append(str(exc))
    return result
