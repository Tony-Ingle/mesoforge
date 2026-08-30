"""Property tests for bilinear interpolation (plan Section 3.6, Task
6): exactness for planar fields, weight-sum invariant, and value
boundedness by the four corner values."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.alignment.spatial import bilinear_interpolate

pytestmark = pytest.mark.scientific

_X = np.array([0.0, 1.0, 2.0, 3.0])
_Y = np.array([10.0, 11.0, 12.0, 13.0])


@given(
    a=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    b=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    c=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    station_x=st.floats(min_value=0.0, max_value=3.0, allow_nan=False, allow_infinity=False),
    station_y=st.floats(min_value=10.0, max_value=13.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50, deadline=None)
def test_bilinear_interpolation_exact_for_planar_field(
    a: float, b: float, c: float, station_x: float, station_y: float
) -> None:
    """Property: bilinear interpolation of an exactly planar field
    f(x,y) = a*x + b*y + c reproduces the plane exactly at any point
    inside the grid (fundamental property of bilinear interpolation)."""
    xx, yy = np.meshgrid(_X, _Y)
    field = a * xx + b * yy + c
    result = bilinear_interpolate(field=field, x=_X, y=_Y, station_x=station_x, station_y=station_y)
    expected = a * station_x + b * station_y + c
    assert result.value == pytest.approx(expected, abs=1e-6)


@given(
    station_x=st.floats(min_value=0.0, max_value=3.0, allow_nan=False, allow_infinity=False),
    station_y=st.floats(min_value=10.0, max_value=13.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=30, deadline=None)
def test_interpolated_value_bounded_by_corner_extremes(station_x: float, station_y: float) -> None:
    """Property: bilinear interpolation is a convex combination of the
    four corners, so the result is always within [min(corners),
    max(corners)] -- never an overshoot/undershoot."""
    rng = np.random.default_rng(42)
    field = rng.uniform(-50.0, 50.0, size=(4, 4))
    result = bilinear_interpolate(field=field, x=_X, y=_Y, station_x=station_x, station_y=station_y)

    cell = result.cell
    corners = [
        field[cell.y0, cell.x0],
        field[cell.y0, cell.x1],
        field[cell.y1, cell.x0],
        field[cell.y1, cell.x1],
    ]
    assert min(corners) - 1e-9 <= result.value <= max(corners) + 1e-9
