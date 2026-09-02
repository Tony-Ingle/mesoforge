"""Projected NBM CONUS grid geometry (plan Section 2.3/2.5; Codex
re-review finding 2).

The operational NBM CONUS core product is a Lambert conformal conic
*projected* grid, not a geographic mesh. Treating its grid indices as if
they were degrees -- as Phase 2 normalization previously did, building
``x``/``y`` from ``latitudeOfFirstGridPointInDegrees`` plus a degree
increment -- silently mislocates every station: the resulting "lat/lon"
mesh is not the grid the provider published, so bilinear extraction
samples the wrong cells.

This module builds the real projected CRS from an approved
``NbmGridProfile`` and derives the projected ``x``/``y`` coordinate axes
plus the true 2-D latitude/longitude mesh, reusing the same pyproj
helpers HRRR normalization already uses. It is shared by
``guidance.sources.nbm_decoding`` (which asserts a decoded message's
declared geometry against the approved profile) and
``guidance.normalization_v2`` (which normalizes on that geometry).
"""

from __future__ import annotations

import numpy as np
import pyproj

from mesoforge.catalog.grid_profiles import NbmGridProfile
from mesoforge.guidance.normalization import (
    build_lambert_conformal_crs,
    compute_latlon_grid,
    compute_projected_coordinates,
)

# Tolerances for comparing a decoded message's declared projection
# against the approved profile. Projection angles are published to at
# least 4 decimal places; the earth radius is an exact integer metre
# value in the GRIB2 encoding.
GRID_ANGLE_TOLERANCE_DEGREES = 1e-4
EARTH_RADIUS_TOLERANCE_M = 1.0


def wrap_longitude_0_360(longitude_degrees: float) -> float:
    """Normalize a longitude into the provider's ``[0, 360)`` GRIB
    convention, which is how approved profiles pin their coverage."""
    return float(longitude_degrees) % 360.0


def build_nbm_crs(profile: NbmGridProfile) -> pyproj.CRS:
    """Build the projected CRS an approved NBM grid profile declares."""
    return build_lambert_conformal_crs(
        lov_degrees=profile.lov_degrees,
        lad_degrees=profile.lad_degrees,
        latin1_degrees=profile.latin1_degrees,
        latin2_degrees=profile.latin2_degrees,
        earth_radius_m=profile.earth_radius_metres,
    )


def compute_nbm_grid(
    profile: NbmGridProfile,
    *,
    first_latitude_degrees: float | None = None,
    first_longitude_degrees: float | None = None,
) -> tuple[pyproj.CRS, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(crs, x, y, lat, lon)`` for an approved NBM grid profile.

    ``x``/``y`` are projected metres (not degrees); ``lat``/``lon`` are
    the true 2-D geographic mesh obtained by inverse-projecting every
    ``(y, x)`` node. The first grid point defaults to the profile's own
    pinned origin; a decoded message's declared origin may be passed to
    derive that message's implied geometry.
    """
    crs = build_nbm_crs(profile)
    first_lat = (
        profile.first_latitude_degrees
        if first_latitude_degrees is None
        else float(first_latitude_degrees)
    )
    first_lon = (
        profile.first_longitude_degrees
        if first_longitude_degrees is None
        else float(first_longitude_degrees)
    )
    x, y = compute_projected_coordinates(
        crs,
        first_lat_degrees=first_lat,
        first_lon_degrees=first_lon,
        dx_m=profile.dx_metres,
        dy_m=profile.dy_metres,
        nx=profile.nx,
        ny=profile.ny,
    )
    lat, lon = compute_latlon_grid(crs, x=x, y=y)
    return crs, x, y, lat, lon


def compute_last_grid_point(
    profile: NbmGridProfile,
    *,
    nx: int,
    ny: int,
    dx_metres: float,
    dy_metres: float,
    first_latitude_degrees: float,
    first_longitude_degrees: float,
) -> tuple[float, float]:
    """Return the ``(latitude, longitude_0_360)`` of the far corner
    implied by a *decoded message's own* declared shape, increments, and
    origin under the approved profile's projection.

    This is the coverage clause of the grid contract: a message whose
    shape, increment, origin, or projection has been mutated lands its
    far corner somewhere other than the approved profile's pinned
    last point, even when every individual key still looks plausible.
    """
    if nx <= 0 or ny <= 0:
        raise ValueError(f"nx/ny must be positive to derive coverage, got {(nx, ny)!r}")
    if not (dx_metres > 0 and dy_metres > 0):
        raise ValueError(
            f"dx/dy must be positive to derive coverage, got {(dx_metres, dy_metres)!r}"
        )
    crs = build_nbm_crs(profile)
    x, y = compute_projected_coordinates(
        crs,
        first_lat_degrees=first_latitude_degrees,
        first_lon_degrees=first_longitude_degrees,
        dx_m=dx_metres,
        dy_m=dy_metres,
        nx=nx,
        ny=ny,
    )
    inverse = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    last_lon, last_lat = inverse.transform(float(x[-1]), float(y[-1]))
    return float(last_lat), wrap_longitude_0_360(last_lon)
