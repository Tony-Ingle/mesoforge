"""Coordinate-derived footprints and native-grid coverage, without acquisition."""

from __future__ import annotations

import math
from numbers import Real

import numpy as np
import pyproj

from mesoforge.alignment.spatial import wgs84_to_native_transformer
from mesoforge.catalog.domains import BoundingBox

MODEL_BUFFER_KM = 50.0
CONTEXT_KM = 150.0

# The minimum radius of curvature of WGS84 (equatorial meridional radius).
# Its spherical angular-distance bound encloses an ellipsoidal geodesic circle;
# using an average Earth radius could make a nominal preparation buffer too small.
_MIN_WGS84_CURVATURE_M = 6_335_439.3272928195


class UnsupportedCoordinateError(ValueError):
    """The coordinate is invalid or outside an established source-model domain."""


class CoverageRequiredError(ValueError):
    """Prepared guidance must be expanded before serving this forecast point."""


def validate_coordinate(latitude: float, longitude: float) -> None:
    """Validate geographic input without imposing a prepared-region boundary."""
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, Real)
        or not isinstance(longitude, Real)
        or not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90.0 <= latitude <= 90.0
        or not -180.0 <= longitude <= 180.0
    ):
        raise UnsupportedCoordinateError(
            "Latitude and longitude must be finite numbers within [-90, 90] and [-180, 180]"
        )


def footprint(latitude: float, longitude: float, radius_km: float) -> BoundingBox:
    """Conservatively enclose a WGS84 distance circle around the exact point.

    A longitude-spanning box is used across the dateline or a pole because the
    retained BoundingBox contract cannot represent wrapped longitude intervals.
    That may prepare extra cells, but never invalidates an otherwise valid point.
    """
    validate_coordinate(latitude, longitude)
    if (
        isinstance(radius_km, bool)
        or not isinstance(radius_km, Real)
        or not math.isfinite(radius_km)
        or radius_km <= 0
    ):
        raise ValueError("Spatial footprint radius must be a positive finite number")
    angular_radius = min(math.pi, radius_km * 1000.0 / _MIN_WGS84_CURVATURE_M)
    latitude_radians = math.radians(latitude)
    south = max(-90.0, latitude - math.degrees(angular_radius))
    north = min(90.0, latitude + math.degrees(angular_radius))
    west, east = -180.0, 180.0
    if abs(latitude_radians) + angular_radius < math.pi / 2:
        longitude_radius = math.degrees(
            math.asin(min(1.0, math.sin(angular_radius) / math.cos(latitude_radians)))
        )
        if longitude - longitude_radius >= -180 and longitude + longitude_radius <= 180:
            west, east = longitude - longitude_radius, longitude + longitude_radius
    return BoundingBox(south=float(south), north=float(north), west=float(west), east=float(east))


def _overlap(left: BoundingBox, right: BoundingBox) -> bool:
    return (
        left.south <= right.north
        and right.south <= left.north
        and left.west <= right.east
        and right.west <= left.east
    )


def _union(left: BoundingBox, right: BoundingBox) -> BoundingBox:
    return BoundingBox(
        south=min(left.south, right.south),
        north=max(left.north, right.north),
        west=min(left.west, right.west),
        east=max(left.east, right.east),
    )


def plan_regions(locations: list[tuple[float, float]]) -> list[BoundingBox]:
    """Inspect the full collection and merge overlapping preparation envelopes.

    Preparing the context envelope also supplies the minimum model-data buffer.
    Sorting and merging to a fixed point makes the plan independent of input order.
    """
    radius = max(MODEL_BUFFER_KM, CONTEXT_KM)
    regions = [footprint(latitude, longitude, radius) for latitude, longitude in locations]
    regions.sort(key=lambda box: (box.south, box.west, box.north, box.east))
    changed = True
    while changed:
        changed = False
        for left_index, left in enumerate(regions):
            for right_index in range(left_index + 1, len(regions)):
                if _overlap(left, regions[right_index]):
                    regions[left_index] = _union(left, regions.pop(right_index))
                    changed = True
                    break
            if changed:
                break
    return sorted(regions, key=lambda box: (box.south, box.west, box.north, box.east))


def _axis_bounds(axis: np.ndarray) -> tuple[float, float] | None:
    if axis.ndim != 1 or len(axis) < 2 or not np.all(np.isfinite(axis)):
        return None
    difference = np.diff(axis)
    if not (np.all(difference > 0) or np.all(difference < 0)):
        return None
    return float(np.min(axis)), float(np.max(axis))


def _points_in_grid(
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    crs: pyproj.CRS,
    x: np.ndarray,
    y: np.ndarray,
) -> bool:
    x_bounds, y_bounds = _axis_bounds(x), _axis_bounds(y)
    if x_bounds is None or y_bounds is None:
        return False
    transformer = wgs84_to_native_transformer(crs)
    projected_x, projected_y = transformer.transform(longitudes, latitudes)
    projected_x, projected_y = np.asarray(projected_x), np.asarray(projected_y)
    if crs.is_geographic and x_bounds[0] >= 0.0:
        projected_x = np.where(projected_x < 0.0, projected_x + 360.0, projected_x)
    if not np.all(np.isfinite(projected_x)) or not np.all(np.isfinite(projected_y)):
        return False
    for values, bounds in ((projected_x, x_bounds), (projected_y, y_bounds)):
        tolerance = max(1e-8, (bounds[1] - bounds[0]) * 1e-12)
        if np.any(values < bounds[0] - tolerance) or np.any(values > bounds[1] + tolerance):
            return False
    return True


def point_in_grid(
    latitude: float, longitude: float, crs: pyproj.CRS, x: np.ndarray, y: np.ndarray
) -> bool:
    """Check the actual native interpolation rectangle, including descending axes."""
    validate_coordinate(latitude, longitude)
    return _points_in_grid(np.array([longitude]), np.array([latitude]), crs, x, y)


def _bbox_boundary(bbox: BoundingBox, crs: pyproj.CRS) -> tuple[np.ndarray, np.ndarray]:
    longitudes = np.linspace(bbox.west, bbox.east, 1025)
    latitudes = np.linspace(bbox.south, bbox.north, 1025)
    if crs.coordinate_operation is not None:
        parameters = {
            parameter.name.lower(): float(parameter.value)
            for parameter in crs.coordinate_operation.params
            if isinstance(parameter.value, Real)
        }
        for parameter in crs.coordinate_operation.params:
            if "longitude" in parameter.name.lower():
                central_longitude = float(parameter.value)
                if bbox.west <= central_longitude <= bbox.east:
                    longitudes = np.append(longitudes, central_longitude)
                # HRRR's spherical tangent Lambert grid has extrema at quarter
                # turns of its cone angle, not just the central meridian. Include
                # those exactly even for a dateline-spanning context envelope.
                first = parameters.get("latitude of 1st standard parallel")
                second = parameters.get("latitude of 2nd standard parallel")
                if first is not None and first == second and first != 0:
                    cone = math.sin(math.radians(first))
                    for turn in range(-4, 5):
                        critical = central_longitude + turn * 90.0 / cone
                        for candidate in (critical, (critical + 180) % 360 - 180):
                            if bbox.west <= candidate <= bbox.east:
                                longitudes = np.append(longitudes, candidate)
    boundary_lon = np.concatenate(
        (
            longitudes,
            longitudes,
            np.full_like(latitudes, bbox.west),
            np.full_like(latitudes, bbox.east),
        )
    )
    boundary_lat = np.concatenate(
        (
            np.full_like(longitudes, bbox.south),
            np.full_like(longitudes, bbox.north),
            latitudes,
            latitudes,
        )
    )
    return boundary_lon, boundary_lat


def native_bbox_bounds(bbox: BoundingBox, crs: pyproj.CRS) -> tuple[float, float, float, float]:
    """Enclose dense geographic edges as native xmin/xmax/ymin/ymax intervals.

    These intervals may retain extra native cells outside the geographic box. They
    let preparation and later coverage checks use the same physical geometry.
    """
    longitude, latitude = _bbox_boundary(bbox, crs)
    transformer = wgs84_to_native_transformer(crs)
    x, y = transformer.transform(longitude, latitude)
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        # A projection singularity is never evidence of absent model coverage.
        # Retaining the whole available native grid is the conservative bound;
        # the exact forecast point is still checked separately against the domain.
        return -math.inf, math.inf, -math.inf, math.inf
    return float(np.min(x)), float(np.max(x)), float(np.min(y)), float(np.max(y))


def bbox_in_grid(bbox: BoundingBox, crs: pyproj.CRS, x: np.ndarray, y: np.ndarray) -> bool:
    """Check curved geographic edges in native coordinates, including extrema."""
    longitude, latitude = _bbox_boundary(bbox, crs)
    return _points_in_grid(longitude, latitude, crs, x, y)


def bbox_within_prepared_domain(
    bbox: BoundingBox,
    crs: pyproj.CRS,
    x: np.ndarray,
    y: np.ndarray,
    source_x: np.ndarray,
    source_y: np.ndarray,
) -> bool:
    """Prove prepared axes cover the required native intervals clipped to source.

    A requested context can cross the physical source domain; no additional
    acquisition can extend that domain. Empty intersections need no preparation,
    while forecast-point support remains a separate check. No declared geographic
    coverage metadata is used as a substitute for actual retained axes.
    """
    prepared_bounds = (_axis_bounds(x), _axis_bounds(y))
    source_bounds = (_axis_bounds(source_x), _axis_bounds(source_y))
    if any(bounds is None for bounds in (*prepared_bounds, *source_bounds)):
        return False
    xmin, xmax, ymin, ymax = native_bbox_bounds(bbox, crs)
    if crs.is_geographic and float(np.min(source_x)) >= 0.0 and xmin < 0:
        if xmax < 0:
            xmin, xmax = xmin + 360, xmax + 360
        else:
            xmin, xmax = 0.0, 360.0
    clipped = []
    for requested, prepared, source in zip(
        ((xmin, xmax), (ymin, ymax)), prepared_bounds, source_bounds, strict=True
    ):
        assert prepared is not None and source is not None
        tolerance = max(1e-8, (source[1] - source[0]) * 1e-12)
        if prepared[0] < source[0] - tolerance or prepared[1] > source[1] + tolerance:
            return False
        low, high = max(requested[0], source[0]), min(requested[1], source[1])
        clipped.append((low, high, prepared, tolerance))
    if any(low > high for low, high, _, _ in clipped):
        return True
    return all(
        low >= prepared[0] - tolerance and high <= prepared[1] + tolerance
        for low, high, prepared, tolerance in clipped
    )
