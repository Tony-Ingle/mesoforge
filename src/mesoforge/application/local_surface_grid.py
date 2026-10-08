"""Small reproducible surface baseline over shared, already-loaded model guidance.

One WGS84 azimuthal-equidistant lattice covers the context and its nested editable
domain. The measured defaults are replaceable implementation choices. Each node
uses the existing surface calculation; its earth-relative U/V components are
not reinterpreted as projected-grid wind components. Only the exact center node
is extracted in this milestone, so no second interpolation changes diagnostics
or conceals spatially different fallback rows.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
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
from mesoforge.common.horizon import horizon_for
from mesoforge.contracts.serialization import canonical_json_digest

_VERSION = "mesoforge.local-surface-baseline.v2"
_LEGACY_VERSION = "mesoforge.local-surface-baseline.v1"
_POLICY = "experimental-nested-context-editable-squares.v1"


@dataclass(frozen=True)
class SurfaceGridGeometry:
    """Internal geometry, independent of the coordinate-only locations format.

    Widths count node intervals from the center; extents describe outer node
    centers, not raster cell edges. An editable boundary has context on every side.
    """

    context_half_width_cells: int = 3
    editable_half_width_cells: int = 1
    spacing_m: float = 6000.0

    def __post_init__(self) -> None:
        if (
            type(self.context_half_width_cells) is not int
            or type(self.editable_half_width_cells) is not int
            or not 1 <= self.editable_half_width_cells < self.context_half_width_cells
        ):
            raise ValueError("Positive integer editable half-width must be smaller than context")
        if (
            isinstance(self.spacing_m, bool)
            or not isinstance(self.spacing_m, (int, float))
            or not math.isfinite(self.spacing_m)
            or self.spacing_m <= 0
        ):
            raise ValueError("Grid spacing must be finite positive meters")


def _extent(latitudes: list[float], longitudes: list[float]) -> dict[str, float]:
    return {
        "south": min(latitudes),
        "north": max(latitudes),
        "west": min(longitudes),
        "east": max(longitudes),
    }


def derive_grid_geometry(
    *, latitude: float, longitude: float, geometry: SurfaceGridGeometry | None = None
) -> dict[str, Any]:
    """Derive reproducible nested domains without model I/O or forecast adjustment."""
    validate_coordinate(latitude, longitude)
    parameters = geometry if geometry is not None else SurfaceGridGeometry()
    half = parameters.context_half_width_cells
    editable_half = parameters.editable_half_width_cells
    offsets = [float(i * parameters.spacing_m) for i in range(-half, half + 1)]
    crs = pyproj.CRS.from_proj4(
        f"+proj=aeqd +lat_0={latitude} +lon_0={longitude} +datum=WGS84 +units=m +no_defs"
    )
    transform = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    latitudes, longitudes = [], []
    for y_index, y_m in enumerate(offsets):
        lat_row, lon_row = [], []
        for x_index, x_m in enumerate(offsets):
            node_lon, node_lat = transform.transform(x_m, y_m)
            if x_index == y_index == half:
                node_lat, node_lon = latitude, longitude
            lat_row.append(float(node_lat))
            lon_row.append(float(node_lon))
        latitudes.append(lat_row)
        longitudes.append(lon_row)
    domains = {}
    for name, width in (("context", half), ("editable", editable_half)):
        indices = range(half - width, half + width + 1)
        extent = _extent(
            [latitudes[y][x] for y in indices for x in indices],
            [longitudes[y][x] for y in indices for x in indices],
        )
        edge = float(width * parameters.spacing_m)
        domains[name] = {
            "shape": "projected_square",
            "boundary_included": True,
            "dimensions": {"x": 2 * width + 1, "y": 2 * width + 1},
            "node_count": (2 * width + 1) ** 2,
            "bounds_m": {"x_min": -edge, "x_max": edge, "y_min": -edge, "y_max": edge},
            "extent": extent,
        }
    return {
        "center": {"latitude": float(latitude), "longitude": float(longitude)},
        "dimensions": domains["context"]["dimensions"],
        "spacing_m": {"x": float(parameters.spacing_m), "y": float(parameters.spacing_m)},
        "parameters": asdict(parameters),
        "point_target": {"x_index": half, "y_index": half, "x_m": 0.0, "y_m": 0.0},
        "node_location": "sample_at_node_center",
        "x_m": offsets,
        "y_m": offsets,
        "latitude": latitudes,
        "longitude": longitudes,
        "crs_wkt2": crs.to_wkt(version="WKT2_2019"),
        "wind_reference": "earth_relative",
        "extent": domains["context"]["extent"],
        "domains": domains,
        "editable_boundary": {
            "distance_metric": "signed_euclidean_distance_in_projected_meters",
            "sign_convention": "positive_inside_zero_on_boundary_negative_outside",
            "purpose": "Support future smooth boundary taper; no taper or edit is applied",
            "taper_policy": "not_implemented",
        },
    }


def _node_domain(x_m: float, y_m: float, editable_edge_m: float) -> dict[str, Any]:
    dx, dy = abs(x_m) - editable_edge_m, abs(y_m) - editable_edge_m
    inside = dx <= 0 and dy <= 0
    distance = -max(dx, dy) if inside else -math.hypot(max(dx, 0), max(dy, 0))
    return {
        "inside_editable_domain": inside,
        "context_only": not inside,
        "is_forecast_point": x_m == y_m == 0,
        "signed_distance_to_editable_boundary_m": 0.0 if distance == 0 else distance,
    }


@lru_cache(maxsize=1)
def _transformation_identity() -> dict[str, Any]:
    """Identify this grid transform separately from earlier provider-decision code."""
    package = Path(__file__).resolve().parents[1]
    source_files = (
        "application/local_surface_grid.py",
        "application/point_forecast.py",
        "application/native_surface_inputs.py",
        "application/surface_forecast.py",
        "application/precipitation_forecast.py",
        "application/probability_forecast.py",
        "application/probability_contributors.py",
        "application/precipitation_type.py",
        "application/snowfall_forecast.py",
        "application/snowfall_amount_forecast.py",
        "application/cloud_cover.py",
        "application/visibility.py",
        "application/thunder.py",
        "application/ice.py",
        "forecasting/surface.py",
        "forecasting/field_blend.py",
        "forecasting/provisional_policy.py",
        "forecasting/coherence.py",
        "forecasting/snowfall_amount.py",
        "forecasting/cloud_cover.py",
        "forecasting/visibility.py",
        "forecasting/thunder.py",
        "forecasting/ice.py",
        "forecasting/scalar_blend.py",
        "forecasting/vector_blend.py",
        "forecasting/gust_blend.py",
        "forecasting/precipitation_blend.py",
        "forecasting/pop_blend.py",
        "forecasting/baseline.py",
        "forecasting/recipes.py",
        "alignment/spatial.py",
        "alignment/station_frame.py",
        "alignment/temporal.py",
        "alignment/state_interpolation.py",
        "catalog/native_horizons.py",
        "guidance/precipitation.py",
        "common/horizon.py",
        "common/qpf_intervals.py",
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
            field.pop("spatial_extraction", None)
            field["row_id"] = field["row_sha256"] = None
            if "final_gust_epsilon_floor_applied" in field:
                field["final_gust_epsilon_floor_applied"] = False
        for contributor in surface.get("contributors", {}).values():
            for field in contributor["fields"].values():
                field.update(value=None, missing_reasons=[reason])
                field.pop("spatial_extraction", None)
                field.get("normalization", {}).pop("finite_precision_floor_at_source_corners", None)
        guidance = surface.get("probability_guidance", {})
        for contributor in guidance.get("contributors", []):
            contributor.update(value=None, status="unavailable", missing_reasons=[reason])
            contributor.pop("spatial_extraction", None)
        for comparison in guidance.get("comparisons", []):
            comparison.update(delta=None, status="unavailable", reasons=[reason])
            comparison["left"]["value"] = comparison["right"]["value"] = None
        type_guidance = surface.get("precipitation_type_guidance")
        if type_guidance is not None:
            type_guidance["field"].update(
                value="unavailable",
                status="unavailable",
                supported_types=[],
                missing_reasons=[reason],
                contributor_disagreement=False,
                disagreement_evidence=[],
            )
            surface["fields"]["precipitation_type"] = deepcopy(type_guidance["field"])
            for contributor in type_guidance["contributors"]:
                contributor.update(
                    status="unavailable",
                    supported_types=[],
                    native_values={},
                    missing_reasons=[reason],
                )
                contributor.pop("conditional_type_fractions", None)
                contributor.pop("spatial_extraction", None)
        surface["source_validation"] = {
            model: {"status": "unavailable", "missing_reasons": [reason]}
            for model in surface.get("source_validation", {})
        }
        snowfall = surface.get("snowfall_guidance")
        if snowfall is not None:
            snowfall["field"].update(value=None, status="unavailable", missing_reasons=[reason])
            surface["fields"]["snowfall_water_equivalent_amount"] = deepcopy(snowfall["field"])
            for contributor in snowfall["contributors"]:
                contributor.update(value=None, status="unavailable", missing_reasons=[reason])
                contributor.pop("spatial_extraction", None)
                contributor.pop("extraction_coordinate", None)
            for comparison in snowfall["comparisons"]:
                comparison.update(
                    difference_left_minus_right=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
        amounts = surface.get("snowfall_amount_guidance")
        if amounts is not None:
            amounts["field"].update(value=None, status="unavailable", missing_reasons=[reason])
            surface["fields"]["snowfall_amount"] = deepcopy(amounts["field"])
            for group in ("native_contributors", "native_slr", "derived_contributors"):
                for contributor in amounts[group]:
                    contributor.update(value=None, status="unavailable", missing_reasons=[reason])
                    contributor.pop("spatial_extraction", None)
                    contributor.pop("diagnostic_ratio", None)
                    contributor.pop("diagnostic_ratio_status", None)
                    contributor.pop("extraction_coordinate", None)
            for comparison in amounts["comparisons"]:
                comparison.update(
                    difference_left_minus_right=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
        cloud = surface.get("cloud_guidance")
        if cloud is not None:
            if "field" in cloud:
                cloud["field"].update(
                    value=None,
                    native_value=None,
                    cloud_percentage=None,
                    sky_category=None,
                    status="unavailable",
                    missing_reasons=[reason],
                    weights={},
                    active_weight=0.0,
                )
                cloud["field"].pop("spatial_extraction", None)
                cloud["field"].pop("extraction_coordinate", None)
                surface["fields"]["cloud_area_fraction"] = deepcopy(cloud["field"])
            for contributor in cloud["contributors"]:
                contributor.update(
                    value=None,
                    native_value=None,
                    sky_category=None,
                    active_weight=0.0,
                    status="unavailable",
                    missing_reasons=[reason],
                )
                contributor.pop("spatial_extraction", None)
                contributor.pop("extraction_coordinate", None)
            for comparison in cloud["comparisons"]:
                comparison.update(
                    difference_left_minus_right=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
        visibility = surface.get("visibility_guidance")
        if visibility is not None:
            visibility["field"].update(value=None, status="unavailable", missing_reasons=[reason])
            surface["fields"]["visibility"] = deepcopy(visibility["field"])
            for contributor in visibility["contributors"]:
                contributor.update(
                    value=None,
                    native_value=None,
                    display_miles=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
                contributor.pop("spatial_extraction", None)
                contributor.pop("extraction_coordinate", None)
            for comparison in visibility["comparisons"]:
                comparison.update(
                    difference_left_minus_right=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
        thunder = surface.get("thunder_guidance")
        if thunder is not None:
            thunder["field"].update(
                value=None,
                native_value=None,
                display_percent=None,
                active_weight=0.0,
                status="unavailable",
                missing_reasons=[reason],
                weights={},
            )
            thunder["field"].pop("spatial_extraction", None)
            thunder["field"].pop("extraction_coordinate", None)
            surface["fields"]["probability_of_thunder_1h"] = deepcopy(thunder["field"])
            for contributor in thunder["contributors"]:
                contributor.update(
                    value=None,
                    native_value=None,
                    display_percent=None,
                    active_weight=0.0,
                    status="unavailable",
                    missing_reasons=[reason],
                )
                contributor.pop("spatial_extraction", None)
                contributor.pop("extraction_coordinate", None)
            for comparison in thunder["comparisons"]:
                comparison.update(
                    difference_left_minus_right=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
        ice = surface.get("ice_guidance")
        if ice is not None:
            for name, field in ice["fields"].items():
                field.update(value=None, status="unavailable", missing_reasons=[reason], weights={})
                field.pop("spatial_extraction", None)
                field.pop("extraction_coordinate", None)
                surface["fields"][name] = deepcopy(field)
            for contributor in ice["contributors"]:
                contributor.update(
                    value=None,
                    native_value=None,
                    active_weight=0.0,
                    status="unavailable",
                    missing_reasons=[reason],
                )
                contributor.pop("spatial_extraction", None)
                contributor.pop("extraction_coordinate", None)
            for comparison in ice["comparisons"]:
                comparison.update(
                    difference_left_minus_right=None,
                    status="unavailable",
                    missing_reasons=[reason],
                )
    return result


def build_local_surface_grid(
    *,
    latitude: float,
    longitude: float,
    calculate_column: Callable[..., dict[str, Any]],
    geometry: SurfaceGridGeometry | None = None,
    columns_owned: bool = False,
) -> dict[str, Any]:
    """Derive one local grid from coordinates and the existing in-memory calculation.

    The callback receives only latitude/longitude. It must perform no acquisition
    or dataset loading. Center failures propagate normally; peripheral coverage
    failures remain explicit cells without invalidating a supported center.
    Shared forecast context and decision evidence are stored once for this grid.

    ``columns_owned`` states that the callback returns a freshly built column that
    no one else retains, so the grid may take its hours instead of copying them.
    It changes no value; it only skips a defensive copy. Leave it False for a
    callback that may return shared or reused structures.
    """
    layout = derive_grid_geometry(latitude=latitude, longitude=longitude, geometry=geometry)
    center_x = layout["point_target"]["x_index"]
    center_y = layout["point_target"]["y_index"]
    editable_edge = layout["domains"]["editable"]["bounds_m"]["x_max"]
    center = calculate_column(latitude=latitude, longitude=longitude)
    horizon = horizon_for(center)
    if "forecast_horizon" in center:
        horizon.validate_hour_rows(
            center["hours"], datetime.fromisoformat(center["target_reference_time"])
        )
    context = {
        key: deepcopy(value)
        for key, value in center.items()
        if key not in ("latitude", "longitude", "hours", "qpf_intervals")
    }
    cells: list[dict[str, Any]] = []
    for y_index, y_m in enumerate(layout["y_m"]):
        for x_index, x_m in enumerate(layout["x_m"]):
            node_lat = layout["latitude"][y_index][x_index]
            node_lon = layout["longitude"][y_index][x_index]
            cell: dict[str, Any] = {
                "x_index": x_index,
                "y_index": y_index,
                "latitude": node_lat,
                "longitude": node_lon,
                "status": "calculated",
                "missing_reasons": [],
                **_node_domain(x_m, y_m, editable_edge),
            }
            if x_index == center_x and y_index == center_y:
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
                    if "qpf_intervals" in center:
                        cell["qpf_intervals"] = [
                            {
                                "interval_start": row["interval_start"],
                                "interval_end": row["interval_end"],
                                "interval_closure": "left_open_right_closed",
                                "unit": "kg/m^2",
                                "value": None,
                                "weights": {},
                                "status": "unavailable",
                                "missing_reasons": [reason],
                            }
                            for row in center["qpf_intervals"]
                        ]
                    cells.append(cell)
                    continue
            if "forecast_horizon" in center or "forecast_horizon" in column:
                if horizon_for(column) != horizon:
                    raise ValueError("Local grid columns have inconsistent forecast horizons")
                horizon.validate_hour_rows(
                    column["hours"], datetime.fromisoformat(center["target_reference_time"])
                )
            cell["hours"] = column["hours"] if columns_owned else deepcopy(column["hours"])
            if "qpf_intervals" in column:
                cell["qpf_intervals"] = (
                    column["qpf_intervals"] if columns_owned else deepcopy(column["qpf_intervals"])
                )
            cells.append(cell)
    return {
        "version": _VERSION,
        **({"forecast_horizon": horizon.payload()} if "forecast_horizon" in center else {}),
        "policy": _POLICY,
        "transformation": deepcopy(_transformation_identity()),
        "geometry": layout,
        "forecast_context": context,
        "cells": cells,
    }


def extract_grid_point(
    grid: dict[str, Any], *, latitude: float, longitude: float, copy_grid: bool = True
) -> dict[str, Any]:
    """Read the exact center baseline, retaining the complete grid for later replay.

    Other coordinates require their own coordinate-derived grid. This intentionally
    does not introduce off-node interpolation across differing fallback policies.

    ``copy_grid=False`` transfers ownership of an ephemeral grid the caller drops
    immediately, avoiding a second full copy of the largest object in the result.
    The returned hours, geometry and context stay independent copies either way.
    A retained grid that outlives this call must keep the default.
    """
    validate_coordinate(latitude, longitude)
    if grid["version"] not in (_VERSION, _LEGACY_VERSION):
        raise ValueError("Unsupported local surface-grid version")
    if horizon_for(grid) != horizon_for(grid["forecast_context"]):
        raise ValueError("Local grid horizon differs from its forecast context")
    center = grid["geometry"]["center"]
    if latitude != center["latitude"] or longitude != center["longitude"]:
        raise CoverageRequiredError(
            "This experimental local grid supports exact center extraction only; "
            "build the coordinate-derived grid for the requested point"
        )
    # Keep retained v1 artifacts readable without adding domains or changing their hashes.
    target = grid["geometry"].get("point_target", {"x_index": 1, "y_index": 1})
    x_index, y_index = target["x_index"], target["y_index"]
    cell = next(
        row for row in grid["cells"] if row["x_index"] == x_index and row["y_index"] == y_index
    )
    if cell["latitude"] != latitude or cell["longitude"] != longitude:
        raise ValueError("Local grid point target does not match the configured coordinate")
    if cell["status"] != "calculated":
        raise CoverageRequiredError("Local grid center baseline is unavailable")
    if "forecast_horizon" in grid:
        horizon_for(grid).validate_hour_rows(
            cell["hours"], datetime.fromisoformat(grid["forecast_context"]["target_reference_time"])
        )
    return {
        **deepcopy(grid["forecast_context"]),
        "latitude": latitude,
        "longitude": longitude,
        "hours": deepcopy(cell["hours"]),
        **({"qpf_intervals": deepcopy(cell["qpf_intervals"])} if "qpf_intervals" in cell else {}),
        "local_grid": {
            "version": grid["version"],
            "policy": grid["policy"],
            "geometry": deepcopy(grid["geometry"]),
            "sha256": str(canonical_json_digest(grid)),
            "point_extraction": {
                "method": "exact_center_node",
                "x_index": x_index,
                "y_index": y_index,
            },
        },
        "local_grid_baseline": deepcopy(grid) if copy_grid else grid,
    }
