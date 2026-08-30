"""Property tests for verification metric formulas (plan Section 3.9,
Task 11): RMSE >= MAE >= |bias| holds for any nonempty finite error
sample (a basic mathematical invariant of these three aggregations)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.verification.metrics import _compute_scalar_metrics

pytestmark = pytest.mark.scientific

_finite_floats = st.floats(
    min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False
)


@given(errors=st.lists(_finite_floats, min_size=1, max_size=50))
@settings(max_examples=100)
def test_rmse_at_least_mae_at_least_abs_bias(errors: list[float]) -> None:
    bias, mae, rmse = _compute_scalar_metrics(errors)
    assert bias is not None and mae is not None and rmse is not None
    assert mae >= abs(bias) - 1e-9
    assert rmse >= mae - 1e-9
