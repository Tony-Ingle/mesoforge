"""Property-based tests for mesoforge.catalog.units.convert (Task 4).

RED: written before src/mesoforge/catalog/units.py exists.
"""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.catalog.units import convert

_finite_temperatures = st.floats(
    min_value=-200.0, max_value=200.0, allow_nan=False, allow_infinity=False
)


@given(celsius=_finite_temperatures)
@settings(max_examples=100)
def test_celsius_to_kelvin_round_trip(celsius: float) -> None:
    kelvin = convert(celsius, "degC", "K")
    back = convert(kelvin, "K", "degC")
    assert math.isclose(back, celsius, abs_tol=1e-6)


@given(celsius=_finite_temperatures)
@settings(max_examples=100)
def test_celsius_to_kelvin_offset_is_273_15(celsius: float) -> None:
    kelvin = convert(celsius, "degC", "K")
    assert math.isclose(kelvin - celsius, 273.15, abs_tol=1e-6)


@given(value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False))
@settings(max_examples=50)
def test_identity_conversion_is_exact(value: float) -> None:
    assert convert(value, "m", "m") == value
