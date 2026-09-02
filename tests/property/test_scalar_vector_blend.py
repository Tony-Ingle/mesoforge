"""Property tests for Phase 2 scalar/vector blend operators (plan
Section 4.2/4.6, Task 8): convex scalar bounds and vector
reconstruction.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar
from mesoforge.forecasting.vector_blend import blend_vector

pytestmark = pytest.mark.scientific


def _weight_triple() -> st.SearchStrategy[tuple[float, float, float]]:
    return st.tuples(
        st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
        st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
        st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    ).map(lambda t: tuple(w / sum(t) for w in t))


@given(
    values=st.tuples(
        st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    ),
    weights=_weight_triple(),
)
@settings(max_examples=50, deadline=None)
def test_blend_scalar_is_convex(
    values: tuple[float, float, float], weights: tuple[float, float, float]
) -> None:
    """Property: a weighted mean with weights summing to 1 always lies
    within [min(values), max(values)] (convex combination)."""
    contributions = tuple(
        Contribution(model=m, value=v, weight=w)
        for m, v, w in zip(("HRRR", "NBM", "GFS"), values, weights, strict=True)
    )
    result = blend_scalar(contributions)
    assert min(values) - 1e-9 <= result.blended_value <= max(values) + 1e-9


@given(
    eastward_values=st.tuples(
        st.floats(min_value=-30.0, max_value=30.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-30.0, max_value=30.0, allow_nan=False, allow_infinity=False),
    ),
    northward_values=st.tuples(
        st.floats(min_value=-30.0, max_value=30.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=-30.0, max_value=30.0, allow_nan=False, allow_infinity=False),
    ),
    weight_a=st.floats(min_value=0.05, max_value=0.95, allow_nan=False),
)
@settings(max_examples=50, deadline=None)
def test_blend_vector_reconstructs_from_speed_direction(
    eastward_values: tuple[float, float],
    northward_values: tuple[float, float],
    weight_a: float,
) -> None:
    """Property: whenever the blended vector is non-calm, decomposing
    its own reported speed/direction back to U/V must reproduce the
    blended U/V within floating tolerance."""
    weight_b = 1.0 - weight_a
    eastward = (
        Contribution(model="HRRR", value=eastward_values[0], weight=weight_a),
        Contribution(model="NBM", value=eastward_values[1], weight=weight_b),
    )
    northward = (
        Contribution(model="HRRR", value=northward_values[0], weight=weight_a),
        Contribution(model="NBM", value=northward_values[1], weight=weight_b),
    )
    result = blend_vector(eastward_contributions=eastward, northward_contributions=northward)
    assume(result.speed_m_s > 1e-9)
    assert result.direction_degrees is not None
    radians = math.radians(result.direction_degrees)
    reconstructed_u = -result.speed_m_s * math.sin(radians)
    reconstructed_v = -result.speed_m_s * math.cos(radians)
    assert reconstructed_u == pytest.approx(result.eastward_m_s, abs=1e-6)
    assert reconstructed_v == pytest.approx(result.northward_m_s, abs=1e-6)
