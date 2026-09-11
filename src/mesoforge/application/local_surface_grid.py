"""Small reproducible surface baseline over shared, already-loaded model guidance.

This first measured geometry is a 3 by 3 WGS84 azimuthal-equidistant grid with
3 km projected spacing. It is not a permanent editable-domain policy. Each node
uses the existing surface calculation; its earth-relative U/V components are
not reinterpreted as projected-grid wind components. Only the exact center node
is extracted in this milestone, so no second interpolation changes diagnostics
or conceals spatially different fallback rows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pyproj

from mesoforge.application.spatial_coverage import (
    CoverageRequiredError,
    UnsupportedCoordinateError,
    validate_coordinate,
)
from mesoforge.contracts.serialization import canonical_json_digest

_VERSION = "mesoforge.local-surface-baseline.v1"
_POLICY = "experimental-centered-3x3-3km.v1"
_OFFSETS_M = (-3000.0, 0.0, 3000.0)


@lru_cache(maxsize=1)
def _transformation_identity() -> dict[str, Any]:
    """Identify this grid transform separately from earlier provider-decision code."""
    package = Path(__file__).resolve().parents[1]
    source_files = (
        "application/local_surface_grid.py",
        "application/point_forecast.py",
        "application/surface_forecast.py",
        "forecasting/surface.py",
        "forecasting/scalar_blend.py",
        "forecasting/vector_blend.py",
        "forecasting/gust_blend.py",
        "forecasting/baseline.py",
        "forecasting/recipes.py",
        "alignment/spatial.py",
        "alignment/station_frame.py",
        "alignment/temporal.py",
    )
    lock = package.parent.parent / "uv.lock"
    return {
        "source_sha256": {
            name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in source_files
        },
        "dependencies": {name: version(name) for name in ("numpy", "xarray", "pyproj")},
        "proj_version": pyproj.proj_version_str,
        "project_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()
        if lock.is_file()
        else None,
    }


def _missing_hours(hours: list[dict[str, Any]], reason: str) -> list[dict[str, Any]]:
    """Keep source/time identity, but never borrow a center value at a missing node."""
    result = deepcopy(hours)
    for hour in result:
        hour["temperature"]["value"] = None
        hour["missing_reasons"] = [reason]
        for source in [*hour.get("sources", []), *hour.get("shadow_sources", [])]:
            source["temperature"]["value"] = None
            source["missing_reasons"] = [reason]
        surface = hour.get("surface")
        if surface is None:
            continue
        for field in surface["fields"].values():
            field.update(value=None, missing_reasons=[reason], status="unavailable", weights={})
            field["row_id"] = field["row_sha256"] = None
            if "final_gust_epsilon_floor_applied" in field:
                field["final_gust_epsilon_floor_applied"] = False
        for contributor in surface.get("contributors", {}).values():
            for field in contributor["fields"].values():
                field.update(value=None, missing_reasons=[reason])
        surface["source_validation"] = {
            model: {"status": "unavailable", "missing_reasons": [reason]}
            for model in surface.get("source_validation", {})
        }
    return result


def build_local_surface_grid(
    *,
    latitude: float,
    longitude: float,
    calculate_column: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Derive one local grid from coordinates and the existing in-memory calculation.

    The callback receives only latitude/longitude. It must perform no acquisition
    or dataset loading. Center failures propagate normally; peripheral coverage
    failures remain explicit cells without invalidating a supported center.
    Shared forecast context and decision evidence are stored once for this grid.
    """
    validate_coordinate(latitude, longitude)
    center = calculate_column(latitude=latitude, longitude=longitude)
    context = {
        key: deepcopy(value)
        for key, value in center.items()
        if key not in ("latitude", "longitude", "hours")
    }
    crs = pyproj.CRS.from_proj4(
        f"+proj=aeqd +lat_0={latitude} +lon_0={longitude} +datum=WGS84 +units=m +no_defs"
    )
    transform = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    latitudes: list[list[float]] = []
    longitudes: list[list[float]] = []
    cells: list[dict[str, Any]] = []
    for y_index, y_m in enumerate(_OFFSETS_M):
        latitude_row: list[float] = []
        longitude_row: list[float] = []
        for x_index, x_m in enumerate(_OFFSETS_M):
            node_lon, node_lat = transform.transform(x_m, y_m)
            if x_index == y_index == 1:
                node_lat, node_lon = latitude, longitude
            node_lat, node_lon = float(node_lat), float(node_lon)
            latitude_row.append(node_lat)
            longitude_row.append(node_lon)
            cell: dict[str, Any] = {
                "x_index": x_index,
                "y_index": y_index,
                "latitude": node_lat,
                "longitude": node_lon,
                "status": "calculated",
                "missing_reasons": [],
            }
            if x_index == y_index == 1:
                column = center
            else:
                try:
                    column = calculate_column(latitude=node_lat, longitude=node_lon)
                except (CoverageRequiredError, UnsupportedCoordinateError) as exc:
                    reason = f"Local grid node has no prepared/native coverage: {exc}"
                    cell.update(
                        status="unavailable",
                        missing_reasons=[reason],
                        hours=_missing_hours(center["hours"], reason),
                    )
                    cells.append(cell)
                    continue
            cell["hours"] = deepcopy(column["hours"])
            cells.append(cell)
        latitudes.append(latitude_row)
        longitudes.append(longitude_row)
    return {
        "version": _VERSION,
        "policy": _POLICY,
        "transformation": deepcopy(_transformation_identity()),
        "geometry": {
            "center": {"latitude": float(latitude), "longitude": float(longitude)},
            "dimensions": {"x": 3, "y": 3},
            "spacing_m": {"x": 3000.0, "y": 3000.0},
            "x_m": list(_OFFSETS_M),
            "y_m": list(_OFFSETS_M),
            "latitude": latitudes,
            "longitude": longitudes,
            "crs_wkt2": crs.to_wkt(version="WKT2_2019"),
            "wind_reference": "earth_relative",
            "extent": {
                "south": min(value for row in latitudes for value in row),
                "north": max(value for row in latitudes for value in row),
                "west": min(value for row in longitudes for value in row),
                "east": max(value for row in longitudes for value in row),
            },
        },
        "forecast_context": context,
        "cells": cells,
    }


def extract_grid_point(
    grid: dict[str, Any], *, latitude: float, longitude: float
) -> dict[str, Any]:
    """Read the exact center baseline, retaining the complete grid for later replay.

    Other coordinates require their own coordinate-derived grid. This intentionally
    does not introduce off-node interpolation across differing fallback policies.
    """
    validate_coordinate(latitude, longitude)
    if grid["version"] != _VERSION:
        raise ValueError("Unsupported local surface-grid version")
    center = grid["geometry"]["center"]
    if latitude != center["latitude"] or longitude != center["longitude"]:
        raise CoverageRequiredError(
            "This experimental local grid supports exact center extraction only; "
            "build the coordinate-derived grid for the requested point"
        )
    cell = next(row for row in grid["cells"] if row["x_index"] == row["y_index"] == 1)
    if cell["status"] != "calculated":
        raise CoverageRequiredError("Local grid center baseline is unavailable")
    return {
        **deepcopy(grid["forecast_context"]),
        "latitude": latitude,
        "longitude": longitude,
        "hours": deepcopy(cell["hours"]),
        "local_grid": {
            "version": grid["version"],
            "policy": grid["policy"],
            "geometry": deepcopy(grid["geometry"]),
            "sha256": str(canonical_json_digest(grid)),
            "point_extraction": {
                "method": "exact_center_node",
                "x_index": 1,
                "y_index": 1,
            },
        },
        "local_grid_baseline": deepcopy(grid),
    }
