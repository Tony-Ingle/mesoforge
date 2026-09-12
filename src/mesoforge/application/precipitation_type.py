"""Native categorical evidence and a temporary agreement-only p-type baseline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import project_station_point
from mesoforge.application.spatial_coverage import point_in_grid, validate_coordinate

PTYPE = "precipitation_type"
TYPES = ("rain", "snow", "freezing_rain", "ice_pellets")
POLICY = {
    "id": "temporary-hrrr-gfs-native-type-agreement.v1",
    "status": "interim_not_verified_or_optimized",
    "required_sources": ["HRRR", "GFS"],
    "rule": "Both complete instantaneous flag sets must agree; no voting or weighted categories",
    "missing_rule": "No single-source fallback; zero flags do not establish dry weather",
    "shadow_rule": "Retain disagreement; shadows never replace the interim baseline",
}


@dataclass(frozen=True)
class TypeView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _classify(
    values: dict[str, float | None], event: dict[str, Any]
) -> tuple[str, list[str], list[str]]:
    encoding = event["encoding"]
    if any(value is None for value in values.values()):
        return "unavailable", [], ["One or more native p-type fields are missing"]
    if encoding == "binary_flags":
        if set(values) != set(TYPES) or any(value not in (0.0, 1.0) for value in values.values()):
            return "unknown", [], ["Native binary type flags are incomplete or outside {0,1}"]
        types = sorted(name for name, value in values.items() if value == 1)
        return ("classified" if types else "no_type_classified"), types, []
    if encoding == "category_code":
        code = values.get("native_code")
        if code == 255:
            return "unavailable", [], ["Native category is the missing code 255"]
        mapping = event.get("code_mapping", {})
        if code is None or not code.is_integer() or str(int(code)) not in mapping:
            return "unknown", [], [f"Unrecognized native precipitation-type code: {code}"]
        types = sorted(mapping[str(int(code))])
        return ("classified" if types else "no_type_classified"), types, []
    if encoding == "conditional_probabilities":
        if set(values) != set(TYPES) or any(
            value is None or not 0 <= value <= 100 for value in values.values()
        ):
            return "unknown", [], ["Native conditional type percentages are invalid or incomplete"]
        return (
            "probabilistic_evidence",
            [],
            [
                "Conditional type probabilities retained; no category-selection threshold "
                "or argmax applied"
            ],
        )
    return "unknown", [], [f"Unsupported native precipitation-type encoding: {encoding}"]


def extract_type_view(
    view: TypeView, *, latitude: float, longitude: float, valid_time: str
) -> dict[str, Any]:
    """Nearest native cell; neither categorical codes nor times are interpolated."""
    model = view.manifest["model"]
    row: dict[str, Any] = {
        "model": model,
        "role": "interim_baseline_evidence" if model in POLICY["required_sources"] else "shadow",
        "active_weight": None if model in POLICY["required_sources"] else 0.0,
        "status": "unavailable",
        "supported_types": [],
        "native_values": {},
        "valid_time": valid_time,
        "missing_reasons": [],
        "manifest_sha256": view.manifest.get("manifest_sha256"),
        "prepared_file": deepcopy(view.manifest.get("prepared_file")),
    }
    target = datetime.fromisoformat(valid_time)
    if target.tzinfo is None:
        raise ValueError("P-type target requires an explicit timezone")
    selected = [
        (i, event)
        for i, event in enumerate(view.manifest["events"])
        if datetime.fromisoformat(event["valid_time"]) == target
    ]
    if len(selected) != 1:
        row["missing_reasons"] = [
            "No unique native p-type evidence at this valid time; no temporal filling"
        ]
        return row
    index, event = selected[0]
    row.update(deepcopy(event))
    row["missing_reasons"] = list(event.get("missing_reasons", []))
    if row["missing_reasons"]:
        return row
    cycle = datetime.fromisoformat(event["source_cycle"])
    if (
        event.get("temporal_semantics") != "instantaneous"
        or cycle.tzinfo is None
        or cycle + timedelta(hours=event["source_lead_hours"]) != target
        or event.get("interval_start") is not None
        or event.get("interval_end") is not None
    ):
        row["missing_reasons"] = [
            "Native p-type time/interval is not compatible with this instantaneous target"
        ]
        return row
    ds = view.dataset
    if ds.sizes.get("event") != len(view.manifest["events"]) or not point_in_grid(
        latitude, longitude, view.crs, ds.x.values, ds.y.values
    ):
        row["missing_reasons"] = ["Coordinate is outside retained native p-type coverage"]
        return row
    x, y = ds.x.values, ds.y.values
    if any(
        not np.isfinite(axis).all() or len(axis) < 2 or not np.all(np.diff(axis) > 0)
        for axis in (x, y)
    ):
        raise ValueError("P-type extraction needs finite ascending native axes")
    px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
    # Axes from preparation use -180..180 and ascending coordinates. argmin chooses
    # the lower index for exact ties, preserving a deterministic categorical sample.
    xi, yi = int(np.argmin(abs(x - px))), int(np.argmin(abs(y - py)))
    values: dict[str, float | None] = {}
    for name, field in ds.data_vars.items():
        if field.dims != ("event", "y", "x") or field.attrs.get("units") != event["native_unit"]:
            raise ValueError("P-type native evidence dimensions/units disagree with metadata")
        value = float(field.values[index, yi, xi])
        values[str(name)] = value if np.isfinite(value) else None
    status, types, reasons = _classify(values, event)
    row.update(
        status=status,
        supported_types=types,
        native_values=values,
        missing_reasons=reasons,
        spatial_extraction={
            "method": "nearest_native_cell",
            "tie_rule": "lower_axis_index",
            "x_index": xi,
            "y_index": yi,
            "native_x": float(x[xi]),
            "native_y": float(y[yi]),
        },
    )
    if event["encoding"] == "conditional_probabilities":
        row["conditional_type_fractions"] = {
            name: None if value is None or not 0 <= value <= 100 else value / 100
            for name, value in values.items()
        }
    return row


def resolve_type_evidence(contributors: list[dict[str, Any]], *, valid_time: str) -> dict[str, Any]:
    """Temporary active-source agreement; do not turn model disagreement into a type."""
    active = [
        next(row for row in contributors if row["model"] == model)
        for model in POLICY["required_sources"]
    ]
    sets = [set(row["supported_types"]) for row in active]
    supported = sorted(set.union(*sets))
    reasons = []
    if all(row["status"] == "unavailable" for row in active):
        value = "unavailable"
        reasons.append("Neither required active source supplies eligible p-type evidence")
    elif any(row["status"] not in ("classified", "no_type_classified") for row in active):
        value = "unknown"
        reasons.append("Both complete active type classifications are required; no fallback")
    elif sets[0] != sets[1]:
        value = "ambiguous"
        reasons.append("HRRR/GFS native classifications disagree; no source winner selected")
    elif not supported:
        value = "unknown"
        reasons.append(
            "Neither active source classifies a type; this is not a dry-weather assertion"
        )
    else:
        value = "mixed" if len(supported) > 1 else supported[0]
    classified = [
        row for row in contributors if row["status"] in ("classified", "no_type_classified")
    ]
    disagree = len({tuple(row["supported_types"]) for row in classified}) > 1
    return {
        "value": value,
        "unit": "category",
        "valid_time": valid_time,
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "supported_types": supported,
        "status": "interim_baseline"
        if value not in ("unknown", "ambiguous", "unavailable")
        else value,
        "missing_reasons": reasons,
        "policy": deepcopy(POLICY),
        "contributor_disagreement": disagree,
        "disagreement_evidence": [
            {"model": row["model"], "types": row["supported_types"]} for row in classified
        ]
        if disagree
        else [],
        "note": "QPF, PoP and type are independent; no probability, amount, "
        "or surface-temperature inference",
    }


def extract_precipitation_type(
    views: list[TypeView], *, latitude: float, longitude: float, valid_time: str
) -> dict[str, Any]:
    validate_coordinate(latitude, longitude)
    grouped: dict[str, list[TypeView]] = {}
    for view in views:
        grouped.setdefault(view.manifest["model"], []).append(view)
    contributors = []
    for model in dict.fromkeys((*POLICY["required_sources"], "RAP", "IFS", "NBM", *grouped)):
        regions = grouped.get(model, [])
        if regions:
            view = next(
                (
                    v
                    for v in regions
                    if point_in_grid(
                        latitude, longitude, v.crs, v.dataset.x.values, v.dataset.y.values
                    )
                ),
                regions[0],
            )
            row = extract_type_view(
                view, latitude=latitude, longitude=longitude, valid_time=valid_time
            )
        else:
            row = {
                "model": model,
                "status": "unavailable",
                "supported_types": [],
                "native_values": {},
                "valid_time": valid_time,
                "role": "interim_baseline_evidence"
                if model in POLICY["required_sources"]
                else "shadow",
                "active_weight": None if model in POLICY["required_sources"] else 0.0,
                "missing_reasons": ["No retained p-type source guidance"],
            }
        contributors.append(row)
    return {
        "field": resolve_type_evidence(contributors, valid_time=valid_time),
        "contributors": contributors,
    }
