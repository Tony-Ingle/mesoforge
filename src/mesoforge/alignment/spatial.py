"""Bilinear native-grid station point extraction (plan Section 3.6,
Task 6).

Pure NumPy computation over the canonical HRRR guidance dataset's
projected ``x``/``y`` coordinates. No extrapolation: a station must sit
inside the rectangle formed by the four enclosing cell centers, and all
four corner values must be finite and unmasked, or extraction fails
closed with ``PointExtractionError``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyproj

from mesoforge.common.errors import MesoForgeError

_WEIGHT_SUM_TOLERANCE = 1e-12


class PointExtractionError(MesoForgeError):
    """Raised when a station point cannot be bilinearly interpolated
    without extrapolation (outside coverage, non-finite/masked corner,
    or a weight-sum invariant violation)."""


@dataclass(frozen=True, slots=True)
class CellIndices:
    y0: int
    y1: int
    x0: int
    x1: int


def find_enclosing_cell(
    *, x: np.ndarray, y: np.ndarray, station_x: float, station_y: float
) -> CellIndices:
    """Locate the four grid cell centers enclosing ``(station_x,
    station_y)``. ``x``/``y`` may be ascending or descending
    (monotonic-coordinate-safe search per plan Section 3.6). Raises
    ``PointExtractionError`` when the station lies outside the
    coverage formed by the innermost enclosing rectangle (no
    extrapolation)."""
    x_ascending = x[0] < x[-1]
    y_ascending = y[0] < y[-1]

    x_sorted = x if x_ascending else x[::-1]
    y_sorted = y if y_ascending else y[::-1]

    if station_x < x_sorted[0] or station_x > x_sorted[-1]:
        raise PointExtractionError(
            f"station x={station_x!r} is outside the retained grid's x coverage "
            f"[{x_sorted[0]!r}, {x_sorted[-1]!r}] (no extrapolation)"
        )
    if station_y < y_sorted[0] or station_y > y_sorted[-1]:
        raise PointExtractionError(
            f"station y={station_y!r} is outside the retained grid's y coverage "
            f"[{y_sorted[0]!r}, {y_sorted[-1]!r}] (no extrapolation)"
        )

    x_index = int(np.searchsorted(x_sorted, station_x, side="right")) - 1
    x_index = min(max(x_index, 0), len(x_sorted) - 2)
    y_index = int(np.searchsorted(y_sorted, station_y, side="right")) - 1
    y_index = min(max(y_index, 0), len(y_sorted) - 2)

    if station_x == x_sorted[-1]:
        x_index = len(x_sorted) - 2
    if station_y == y_sorted[-1]:
        y_index = len(y_sorted) - 2

    x0_sorted, x1_sorted = x_index, x_index + 1
    y0_sorted, y1_sorted = y_index, y_index + 1

    x0 = x0_sorted if x_ascending else len(x) - 1 - x0_sorted
    x1 = x1_sorted if x_ascending else len(x) - 1 - x1_sorted
    y0 = y0_sorted if y_ascending else len(y) - 1 - y0_sorted
    y1 = y1_sorted if y_ascending else len(y) - 1 - y1_sorted

    return CellIndices(y0=y0, y1=y1, x0=x0, x1=x1)


@dataclass(frozen=True, slots=True)
class BilinearWeights:
    w00: float
    w01: float
    w10: float
    w11: float


def compute_bilinear_weights(
    *, x: np.ndarray, y: np.ndarray, cell: CellIndices, station_x: float, station_y: float
) -> BilinearWeights:
    """Double-precision bilinear weights for the four corners
    ``(y0,x0), (y0,x1), (y1,x0), (y1,x1)``. Each weight must be in
    ``[0, 1]`` and the four must sum to ``1`` within ``1e-12`` (plan
    Section 3.6)."""
    x0v, x1v = float(x[cell.x0]), float(x[cell.x1])
    y0v, y1v = float(y[cell.y0]), float(y[cell.y1])

    tx = (station_x - x0v) / (x1v - x0v) if x1v != x0v else 0.0
    ty = (station_y - y0v) / (y1v - y0v) if y1v != y0v else 0.0

    w00 = (1 - tx) * (1 - ty)
    w01 = tx * (1 - ty)
    w10 = (1 - tx) * ty
    w11 = tx * ty

    weights = BilinearWeights(w00=w00, w01=w01, w10=w10, w11=w11)
    total = w00 + w01 + w10 + w11
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise PointExtractionError(
            f"bilinear weights do not sum to 1 within tolerance: got {total!r}"
        )
    for name, w in (("w00", w00), ("w01", w01), ("w10", w10), ("w11", w11)):
        if not (0.0 - _WEIGHT_SUM_TOLERANCE <= w <= 1.0 + _WEIGHT_SUM_TOLERANCE):
            raise PointExtractionError(f"bilinear weight {name}={w!r} is outside [0, 1]")
    return weights


@dataclass(frozen=True, slots=True)
class ExtractedValue:
    value: float
    cell: CellIndices
    weights: BilinearWeights


def bilinear_interpolate(
    *,
    field: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    station_x: float,
    station_y: float,
) -> ExtractedValue:
    """Interpolate a single ``(y, x)`` 2-D field at one station point.
    Requires all four corner values finite (no masked/nonfinite corner
    per plan Section 3.6)."""
    cell = find_enclosing_cell(x=x, y=y, station_x=station_x, station_y=station_y)
    weights = compute_bilinear_weights(
        x=x, y=y, cell=cell, station_x=station_x, station_y=station_y
    )

    v00 = float(field[cell.y0, cell.x0])
    v01 = float(field[cell.y0, cell.x1])
    v10 = float(field[cell.y1, cell.x0])
    v11 = float(field[cell.y1, cell.x1])

    for name, v in (("(y0,x0)", v00), ("(y0,x1)", v01), ("(y1,x0)", v10), ("(y1,x1)", v11)):
        if not np.isfinite(v):
            raise PointExtractionError(
                f"corner {name} is non-finite ({v!r}); extraction fails closed "
                "(all four corners must be finite and unmasked)"
            )

    value = weights.w00 * v00 + weights.w01 * v01 + weights.w10 * v10 + weights.w11 * v11
    return ExtractedValue(value=value, cell=cell, weights=weights)


def project_station_point(
    crs: pyproj.CRS, *, latitude: float, longitude: float
) -> tuple[float, float]:
    """Transform a station's WGS84 coordinate into the source
    projection (plan Section 3.6 step 1)."""
    lon = longitude - 360.0 if longitude > 180.0 else longitude
    transformer = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = transformer.transform(lon, latitude)
    return float(x), float(y)
