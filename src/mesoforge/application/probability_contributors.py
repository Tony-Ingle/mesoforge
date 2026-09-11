"""Read native probability contributors without changing the active hourly PoP.

Products retain their own events and intervals. Pairwise differences are only
descriptive disagreements between compatible probabilities, never skill scores.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any, TypeGuard, cast

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import (
    PointExtractionError,
    bilinear_interpolate,
    project_station_point,
)
from mesoforge.application.spatial_coverage import point_in_grid, validate_coordinate


@dataclass(frozen=True, slots=True)
class ProbabilityView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"Probability metadata is not JSON-compatible: {type(value).__name__}")


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Probability interval/source times require an explicit timezone")
    return result.astimezone(UTC)


def _number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def _threshold(row: dict[str, Any]) -> tuple[float, str, str] | None:
    threshold = row.get("threshold", {})
    if (
        not isinstance(threshold, dict)
        or not _number(threshold.get("value"))
        or threshold["value"] < 0
        or threshold.get("unit") not in ("kg/m^2", "kg/m²")
        or threshold.get("comparison") not in ("gt", "ge", "lt", "le")
    ):
        return None
    return float(threshold["value"]), "kg/m^2", threshold["comparison"]


def _support_known(support: Any) -> bool:
    if not isinstance(support, dict):
        return False
    if support.get("kind") == "grid_point":
        return True
    if support.get("kind") != "neighborhood":
        return False
    radius = support.get("radius_km")
    operator = support.get("operator")
    return (
        _number(radius)
        and radius > 0
        and isinstance(operator, str)
        and bool(operator.strip())
        and operator != "unknown"
    )


def _interval(row: dict[str, Any]) -> tuple[datetime, datetime, str] | None:
    try:
        start, end = _time(row["interval_start"]), _time(row["interval_end"])
    except (KeyError, TypeError, ValueError):
        return None
    closure = row.get("interval_closure")
    if start >= end or closure not in ("left_open_right_closed", "closed", "open"):
        return None
    return start, end, closure


def _native_intervals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: _json_safe(event.get(key))
            for key in (
                "event_id",
                "interval_start",
                "interval_end",
                "interval_closure",
                "threshold",
                "spatial_support",
                "source_cycle",
                "source_lead_hours",
            )
        }
        for event in events
    ]


def _source_metadata(manifest: dict[str, Any]) -> Any:
    return _json_safe(manifest.get("source_metadata", manifest.get("metadata", {})))


def _extract(
    view: ProbabilityView, event: dict[str, Any], index: int, *, latitude: float, longitude: float
) -> dict[str, Any]:
    source_id = view.manifest["source_id"]
    row = {
        **_json_safe(event),
        "source_id": source_id,
        "event_id": event.get("event_id", f"{source_id}:event:{index}"),
        "role": "shadow",
        "active_weight": 0.0,
        "value": None,
        "unit": "1",
        "status": "unavailable",
        "source_metadata": _source_metadata(view.manifest),
        "manifest_sha256": view.manifest.get("manifest_sha256"),
        "prepared_sha256": view.manifest.get("prepared_sha256")
        or view.manifest.get("prepared_file", {}).get("sha256"),
        "missing_reasons": list(event.get("missing_reasons", [])),
    }
    if row["missing_reasons"]:
        return row
    try:
        field = view.dataset["probability"]
        if (
            field.dims != ("event", "y", "x")
            or field.attrs.get("units") != "1"
            or field.attrs.get("unit_id", "1") != "1"
            or field.attrs.get("temporal_semantics") != "probability"
        ):
            raise ValueError(
                "Native probability needs explicit fraction units and event-grid dimensions"
            )
        interval = _interval(row)
        if interval is None or _threshold(row) is None:
            raise ValueError("Native probability event threshold/interval metadata is invalid")
        cycle, lead = _time(row["source_cycle"]), row["source_lead_hours"]
        if not _number(lead) or lead < 0 or cycle + timedelta(hours=lead) != interval[1]:
            raise ValueError("Native probability cycle/source lead disagrees with its interval end")
        x, y = view.dataset.x.values, view.dataset.y.values
        for axis in (x, y):
            if (
                len(axis) < 2
                or not np.isfinite(axis).all()
                or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
            ):
                raise ValueError("Native probability axes must be finite and strictly monotonic")
        px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
        if view.crs.is_geographic and x.min() >= 0 and px < 0:
            px += 360
        result = bilinear_interpolate(
            field=field.values[index], x=x, y=y, station_x=px, station_y=py
        )
        cell = result.cell
        corners = field.values[index][np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])]
        if not np.isfinite(corners).all() or np.any(corners < 0) or np.any(corners > 1):
            raise ValueError("Native probability requires four finite corners within [0,1]")
        if not math.isfinite(result.value) or not 0 <= result.value <= 1:
            raise ValueError("Interpolated native probability is outside [0,1]; no clipping")
        native_percent = None
        if "native_probability" in view.dataset:
            native = view.dataset["native_probability"]
            if native.dims != field.dims or native.attrs.get("units") not in ("%", "percent"):
                raise ValueError("Native probability percentages require explicit percent units")
            native_percent = native.values[index][np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])]
            if not np.allclose(native_percent / 100, corners, rtol=0, atol=1e-15, equal_nan=False):
                raise ValueError("Native percentages disagree with prepared probability fractions")
        row.update(
            value=result.value,
            status="available",
            spatial_extraction={
                "method": "native_grid_bilinear_probability",
                "source_y": [cell.y0, cell.y1],
                "source_x": [cell.x0, cell.x1],
                "weights": [
                    result.weights.w00,
                    result.weights.w01,
                    result.weights.w10,
                    result.weights.w11,
                ],
                "native_fraction_at_source_corners": corners.ravel().tolist(),
            },
        )
        if native_percent is not None:
            row["spatial_extraction"]["native_percent_at_source_corners"] = (
                native_percent.ravel().tolist()
            )
    except (KeyError, IndexError, TypeError, ValueError, PointExtractionError) as exc:
        row["missing_reasons"].append(str(exc))
    return cast(dict[str, Any], _json_safe(row))


def _comparison(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    left_threshold, right_threshold = _threshold(left), _threshold(right)
    if left_threshold is None or right_threshold is None:
        reasons.append("Unknown or invalid normalized precipitation threshold/comparator")
    elif left_threshold != right_threshold:
        reasons.append("Different precipitation threshold or comparator")
    left_interval, right_interval = _interval(left), _interval(right)
    if left_interval is None or right_interval is None:
        reasons.append("Unknown or invalid native accumulation interval")
    elif left_interval != right_interval:
        reasons.append("Different native accumulation interval or interval closure")
    left_support, right_support = left.get("spatial_support"), right.get("spatial_support")
    if not _support_known(left_support) or not _support_known(right_support):
        if any(
            isinstance(support, dict) and support.get("kind") == "grid_box_mean"
            for support in (left_support, right_support)
        ):
            reasons.append(
                "Native grid-box-mean precipitation has no established common spatial support "
                "with the comparison target; interpolating probabilities does not change support"
            )
        else:
            reasons.append("Unknown event spatial support prevents comparison")
    elif left_support != right_support:
        reasons.append("Different event spatial support")
    compatible = not reasons
    for item in (left, right):
        if not _number(item.get("value")) or not 0 <= item["value"] <= 1 or item.get("unit") != "1":
            reasons.append(f"{item['source_id']}: finite native probability is unavailable")
    return {
        "left": {key: left.get(key) for key in ("source_id", "event_id", "value", "role")},
        "right": {key: right.get(key) for key in ("source_id", "event_id", "value", "role")},
        "status": "comparable" if not reasons else "unavailable" if compatible else "incompatible",
        "delta": left["value"] - right["value"] if not reasons else None,
        "unit": "1",
        "definition": "left_probability_minus_right_probability",
        "reasons": reasons,
    }


def extract_probability_contributors(
    views: list[ProbabilityView] | tuple[ProbabilityView, ...],
    *,
    latitude: float,
    longitude: float,
    valid_time: str,
    active: dict[str, Any],
) -> dict[str, Any]:
    """Extract native endpoint events and descriptive, strictly comparable differences.

    There is no temporal interpolation, period splitting/combining, ensemble
    fraction calculation, recipe weighting, observation matching or scoring here.
    """
    validate_coordinate(latitude, longitude)
    target = _time(valid_time)
    grouped: dict[str, list[ProbabilityView]] = {}
    for view in views:
        source_id = view.manifest["source_id"]
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("Probability source_id must be a nonblank string")
        grouped.setdefault(source_id, []).append(view)
    contributors: list[dict[str, Any]] = []
    for source_id, regions in grouped.items():
        view = next(
            (
                entry
                for entry in regions
                if point_in_grid(
                    latitude, longitude, entry.crs, entry.dataset.x.values, entry.dataset.y.values
                )
            ),
            regions[0],
        )
        events = view.manifest["events"]
        if view.dataset.sizes.get("event") != len(events):
            raise ValueError("Native probability events differ from the prepared event axis")
        selected = [
            (index, event)
            for index, event in enumerate(events)
            if _time(event["interval_end"]) == target
        ]
        if selected:
            contributors.extend(
                _extract(view, event, index, latitude=latitude, longitude=longitude)
                for index, event in selected
            )
        else:
            contributors.append(
                {
                    "source_id": source_id,
                    "event_id": None,
                    "role": "shadow",
                    "active_weight": 0.0,
                    "value": None,
                    "unit": "1",
                    "status": "unavailable",
                    "valid_time": valid_time,
                    "source_metadata": _source_metadata(view.manifest),
                    "manifest_sha256": view.manifest.get("manifest_sha256"),
                    "available_native_intervals": _native_intervals(events),
                    "missing_reasons": [
                        "No retained native probability interval ends at this valid time; "
                        "no temporal filling"
                    ],
                }
            )
    reference = {
        **_json_safe(active),
        "source_id": "NBM_HOURLY_CONTROL",
        "event_id": "hourly_control",
        "role": "active_reference",
    }
    comparisons = [
        _comparison(left, right) for left, right in combinations([reference, *contributors], 2)
    ]
    return cast(
        dict[str, Any], _json_safe({"contributors": contributors, "comparisons": comparisons})
    )
