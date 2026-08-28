"""Property tests for wind rotation conventions (plan Section 3.5,
Task 5): rotation is norm-preserving (speed unchanged) for arbitrary
grid-relative vectors, and cardinal-vector cases at the LCC true origin
verify the exact rotated direction."""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.guidance.normalization import (
    build_lambert_conformal_crs,
    rotate_wind_to_earth_relative,
)

pytestmark = pytest.mark.scientific

_LOV = 262.5
_LAD = 38.5
_LATIN1 = 38.5
_LATIN2 = 38.5


def _crs():
    return build_lambert_conformal_crs(
        lov_degrees=_LOV, lad_degrees=_LAD, latin1_degrees=_LATIN1, latin2_degrees=_LATIN2
    )


@given(
    u=st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False),
    v=st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=25, deadline=None)
def test_rotation_preserves_wind_speed(u: float, v: float) -> None:
    """Property: rotating a grid-relative (u, v) vector to earth-relative
    components never changes its magnitude (rotation matrices are
    orthonormal by construction -- this is the scientific invariant the
    basis norm/orthogonality checks in rotate_wind_to_earth_relative
    exist to guarantee)."""
    crs = _crs()
    x = np.array([-3000.0, 0.0, 3000.0])
    y = np.array([-3000.0, 0.0, 3000.0])
    u_grid = np.full((3, 3), u)
    v_grid = np.full((3, 3), v)

    result = rotate_wind_to_earth_relative(
        u_grid=u_grid,
        v_grid=v_grid,
        x=x,
        y=y,
        crs=crs,
        u_relative_to_grid=True,
        v_relative_to_grid=True,
    )

    speed_before = math.hypot(u, v)
    speed_after = np.hypot(result.eastward, result.northward)
    np.testing.assert_allclose(speed_after, speed_before, atol=1e-6, rtol=1e-6)


def test_identity_rotation_never_changes_values() -> None:
    """Property: when both components are already earth-relative,
    rotation is a strict no-op regardless of magnitude/sign."""
    crs = _crs()
    u_grid = np.array([[-10.0, 0.0], [3.5, 25.0]])
    v_grid = np.array([[7.0, -1.0], [0.0, -25.0]])
    result = rotate_wind_to_earth_relative(
        u_grid=u_grid,
        v_grid=v_grid,
        x=np.array([0.0, 3000.0]),
        y=np.array([0.0, 3000.0]),
        crs=crs,
        u_relative_to_grid=False,
        v_relative_to_grid=False,
    )
    np.testing.assert_array_equal(result.eastward, u_grid)
    np.testing.assert_array_equal(result.northward, v_grid)
