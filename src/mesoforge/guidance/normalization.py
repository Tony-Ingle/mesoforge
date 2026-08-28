"""HRRR canonical guidance normalization (plan Section 3.5, Task 5):
projection/grid construction, grid-to-earth wind rotation, bbox+halo
subsetting, and canonical ``xarray.Dataset`` assembly.

Pure NumPy/xarray/pyproj computation -- no storage/network import.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pyproj
import xarray as xr

from mesoforge.catalog.domains import BoundingBox
from mesoforge.common.errors import MesoForgeError

_BASIS_NORM_TOLERANCE = 1e-12
_BASIS_DOT_TOLERANCE = 1e-3


class WindRotationError(MesoForgeError):
    """Raised when a wind-rotation basis fails its norm/orthogonality
    validation, or when U/V disagree on ``uvRelativeToGrid``."""


class SubsettingError(MesoForgeError):
    """Raised when the requested bbox+halo cannot be formed from the
    source grid (e.g. domain extends outside the retained source
    extent)."""


def build_lambert_conformal_crs(
    *,
    lov_degrees: float,
    lad_degrees: float,
    latin1_degrees: float,
    latin2_degrees: float,
    earth_radius_m: float = 6371229.0,
) -> pyproj.CRS:
    """Construct the HRRR native Lambert Conformal Conic projection from
    the GRIB2 keys asserted in the acquisition manifest
    (``LoVInDegrees``, ``LaDInDegrees``, ``Latin1InDegrees``,
    ``Latin2InDegrees``). HRRR's GRIB2 encodes a spherical earth of
    radius 6371229 m (NCEP convention)."""
    lon_0 = lov_degrees - 360.0 if lov_degrees > 180.0 else lov_degrees
    proj4 = (
        f"+proj=lcc +lat_1={latin1_degrees} +lat_2={latin2_degrees} +lat_0={lad_degrees} "
        f"+lon_0={lon_0} +x_0=0 +y_0=0 +R={earth_radius_m} +units=m +no_defs"
    )
    return pyproj.CRS.from_proj4(proj4)


def compute_projected_coordinates(
    crs: pyproj.CRS,
    *,
    first_lat_degrees: float,
    first_lon_degrees: float,
    dx_m: float,
    dy_m: float,
    nx: int,
    ny: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Project the GRIB first-grid-point lat/lon into the native CRS and
    build the 1-D strictly increasing ``x``/``y`` projected coordinate
    arrays (HRRR scans west-to-east, south-to-north for the surface
    product)."""
    first_lon = first_lon_degrees - 360.0 if first_lon_degrees > 180.0 else first_lon_degrees
    transformer = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x0, y0 = transformer.transform(first_lon, first_lat_degrees)
    x = x0 + dx_m * np.arange(nx, dtype=np.float64)
    y = y0 + dy_m * np.arange(ny, dtype=np.float64)
    return x, y


def compute_latlon_grid(
    crs: pyproj.CRS, *, x: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Inverse-project the full ``(y, x)`` coordinate mesh to 2-D
    latitude/longitude arrays (degrees_north / degrees_east, WGS84
    ``[-180, 180]`` convention)."""
    inverse = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xx, yy = np.meshgrid(x, y)
    lon, lat = inverse.transform(xx, yy)
    lon = np.where(lon > 180.0, lon - 360.0, lon)
    return np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64)


@dataclass(frozen=True, slots=True)
class WindRotationResult:
    eastward: np.ndarray
    northward: np.ndarray
    policy: str  # "identity" | "grid-to-earth-pyproj.v1"


def rotate_wind_to_earth_relative(
    *,
    u_grid: np.ndarray,
    v_grid: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    crs: pyproj.CRS,
    u_relative_to_grid: bool,
    v_relative_to_grid: bool,
) -> WindRotationResult:
    """Section 3.5 grid-to-earth rotation algorithm.

    1. Inspect ``uvRelativeToGrid`` on both U and V; require equality.
    2. If already earth-relative, retain values (identity rotation).
    3. If grid-relative, derive local projected-grid unit bases
       deterministically with pyproj and rotate every grid point.
    """
    if u_relative_to_grid != v_relative_to_grid:
        raise WindRotationError(
            "U and V disagree on uvRelativeToGrid "
            f"(U={u_relative_to_grid!r}, V={v_relative_to_grid!r}); comparison is not "
            "permitted when orientation metadata is contradictory"
        )

    if not u_relative_to_grid:
        return WindRotationResult(eastward=u_grid, northward=v_grid, policy="identity")

    if crs.ellipsoid is None:
        raise WindRotationError("CRS has no ellipsoid definition; cannot compute rotation basis")
    geod = pyproj.Geod(a=crs.ellipsoid.semi_major_metre, f=0.0)
    inverse = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    ny, nx = u_grid.shape
    x_east = np.empty((ny, nx), dtype=np.float64)
    x_north = np.empty((ny, nx), dtype=np.float64)
    y_east = np.empty((ny, nx), dtype=np.float64)
    y_north = np.empty((ny, nx), dtype=np.float64)

    dx = float(x[1] - x[0]) if len(x) > 1 else 1.0
    dy = float(y[1] - y[0]) if len(y) > 1 else 1.0

    for j in range(ny):
        for i in range(nx):
            x0, y0 = x[i], y[j]
            lon0, lat0 = inverse.transform(x0, y0)
            lon_x, lat_x = inverse.transform(x0 + dx, y0)
            lon_y, lat_y = inverse.transform(x0, y0 + dy)

            az_x, _back_x, _dist_x = geod.inv(lon0, lat0, lon_x, lat_x)
            az_y, _back_y, _dist_y = geod.inv(lon0, lat0, lon_y, lat_y)

            az_x_rad = math.radians(az_x)
            az_y_rad = math.radians(az_y)
            x_east[j, i] = math.sin(az_x_rad)
            x_north[j, i] = math.cos(az_x_rad)
            y_east[j, i] = math.sin(az_y_rad)
            y_north[j, i] = math.cos(az_y_rad)

    x_norm = np.sqrt(x_east**2 + x_north**2)
    y_norm = np.sqrt(y_east**2 + y_north**2)
    if np.any(np.abs(x_norm - 1.0) > _BASIS_NORM_TOLERANCE) or np.any(
        np.abs(y_norm - 1.0) > _BASIS_NORM_TOLERANCE
    ):
        raise WindRotationError(
            "grid-to-earth rotation basis norm exceeds tolerance "
            f"{_BASIS_NORM_TOLERANCE!r}; nonfinite or degenerate projection"
        )
    dot = x_east * y_east + x_north * y_north
    if np.any(np.abs(dot) > _BASIS_DOT_TOLERANCE):
        raise WindRotationError(
            f"grid-to-earth rotation basis dot product exceeds tolerance "
            f"{_BASIS_DOT_TOLERANCE!r}; bases are not sufficiently orthogonal"
        )
    if not (np.all(np.isfinite(x_east)) and np.all(np.isfinite(y_east))):
        raise WindRotationError("grid-to-earth rotation basis contains nonfinite values")

    u_east = u_grid * x_east + v_grid * y_east
    v_north = u_grid * x_north + v_grid * y_north
    return WindRotationResult(eastward=u_east, northward=v_north, policy="grid-to-earth-pyproj.v1")


@dataclass(frozen=True, slots=True)
class SubsetIndices:
    y_start: int
    y_end: int
    x_start: int
    x_end: int


def compute_bbox_halo_subset_indices(
    *,
    x: np.ndarray,
    y: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    bbox: BoundingBox,
    halo_cells: int = 1,
) -> SubsetIndices:
    """Section 3.6: the smallest source-grid rectangle covering the
    inclusive domain bbox plus exactly one complete source cell of halo
    on every side. Raises ``SubsettingError`` if the halo would extend
    outside the retained source grid."""
    inside = (lat >= bbox.south) & (lat <= bbox.north) & (lon >= bbox.west) & (lon <= bbox.east)
    if not np.any(inside):
        raise SubsettingError("bbox does not intersect the source grid")

    y_indices, x_indices = np.where(inside)
    y_min, y_max = int(y_indices.min()), int(y_indices.max())
    x_min, x_max = int(x_indices.min()), int(x_indices.max())

    y_start = y_min - halo_cells
    y_end = y_max + halo_cells + 1
    x_start = x_min - halo_cells
    x_end = x_max + halo_cells + 1

    ny, nx = lat.shape
    if y_start < 0 or x_start < 0 or y_end > ny or x_end > nx:
        raise SubsettingError(
            f"bbox+halo subset [{y_start}:{y_end}, {x_start}:{x_end}] extends outside "
            f"the retained source grid shape ({ny}, {nx})"
        )
    return SubsetIndices(y_start=y_start, y_end=y_end, x_start=x_start, x_end=x_end)


def assemble_canonical_hrrr_dataset(
    *,
    forecast_reference_time: np.datetime64,
    lead_hours: tuple[int, ...],
    x: np.ndarray,
    y: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    temperature_k: np.ndarray,
    eastward_wind_m_s: np.ndarray,
    northward_wind_m_s: np.ndarray,
    grid_id: str,
    configuration_snapshot_id: str,
    variable_lineage_manifest_id: str,
) -> xr.Dataset:
    """Assemble the ``canonical-guidance.v1`` dataset (plan Section 3.5):
    dimensions ``(lead_time, y, x)``, projected ``x``/``y``, 2-D
    ``latitude``/``longitude``, temperature/U/V each float32 with a
    ``uint16`` quality mask (all-zero: Phase 1 requires every field
    present and valid before registration -- missingness is a rejection,
    never a partial artifact)."""
    n_lead = len(lead_hours)
    lead_time = np.array([np.timedelta64(h, "h") for h in lead_hours], dtype="timedelta64[ns]")
    valid_time = forecast_reference_time + lead_time

    def _mask(shape: tuple[int, ...]) -> np.ndarray:
        return np.zeros(shape, dtype=np.uint16)

    dataset = xr.Dataset(
        data_vars={
            "air_temperature_2m": (
                ("lead_time", "y", "x"),
                temperature_k.astype(np.float32),
                {
                    "unit_id": "K",
                    "temporal_semantics": "instantaneous",
                    "spatial_support": "point",
                    "vertical_definition_id": "height-agl-2m",
                    "quality_mask": "air_temperature_2m_quality_mask",
                },
            ),
            "air_temperature_2m_quality_mask": (
                ("lead_time", "y", "x"),
                _mask((n_lead, *lat.shape)),
            ),
            "eastward_wind_10m": (
                ("lead_time", "y", "x"),
                eastward_wind_m_s.astype(np.float32),
                {
                    "unit_id": "m/s",
                    "temporal_semantics": "instantaneous",
                    "spatial_support": "point",
                    "vertical_definition_id": "height-agl-10m",
                    "quality_mask": "eastward_wind_10m_quality_mask",
                },
            ),
            "eastward_wind_10m_quality_mask": (
                ("lead_time", "y", "x"),
                _mask((n_lead, *lat.shape)),
            ),
            "northward_wind_10m": (
                ("lead_time", "y", "x"),
                northward_wind_m_s.astype(np.float32),
                {
                    "unit_id": "m/s",
                    "temporal_semantics": "instantaneous",
                    "spatial_support": "point",
                    "vertical_definition_id": "height-agl-10m",
                    "quality_mask": "northward_wind_10m_quality_mask",
                },
            ),
            "northward_wind_10m_quality_mask": (
                ("lead_time", "y", "x"),
                _mask((n_lead, *lat.shape)),
            ),
        },
        coords={
            "forecast_reference_time": forecast_reference_time,
            "lead_time": lead_time,
            "valid_time": ("lead_time", valid_time),
            "y": y,
            "x": x,
            "latitude": (("y", "x"), lat),
            "longitude": (("y", "x"), lon),
        },
        attrs={
            "schema_version": "canonical-guidance.v1",
            "time_encoding": "UTC",
            "grid_id": grid_id,
            "configuration_snapshot_id": configuration_snapshot_id,
            "variable_lineage_manifest_id": variable_lineage_manifest_id,
        },
    )
    return dataset
