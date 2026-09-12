"""Visibility distances are not capped, classified or confused with missing guidance."""

import math

import pytest

from mesoforge.forecasting.visibility import visibility_metres, visibility_miles


def test_visibility_unit_conversion_preserves_native_precision_and_zero():
    assert visibility_metres(12.3456789, "km") == pytest.approx(12345.6789, rel=1e-15)
    assert visibility_metres(1.0, "statute_mile") == 1609.344
    assert visibility_miles(1609.344) == 1.0
    assert visibility_metres(0) == visibility_miles(0) == 0
    assert visibility_metres(100001.123456789) == 100001.123456789
    assert visibility_miles(160934.4) == pytest.approx(100.0, rel=1e-15)


@pytest.mark.parametrize("value", [-0.00001, math.nan, math.inf, -math.inf])
def test_negative_and_nonfinite_visibility_is_not_replaced_with_zero_or_a_cap(value):
    with pytest.raises(ValueError, match="no clipping"):
        visibility_metres(value)
    with pytest.raises(ValueError, match="no clipping"):
        visibility_miles(value)


def test_unknown_visibility_units_are_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        visibility_metres(50, "percent")
