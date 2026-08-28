"""Unit tests for mesoforge.catalog.units (Task 4, plan Section 4.3).

RED: written before src/mesoforge/catalog/units.py exists.
"""

from __future__ import annotations

import math

import pytest

from mesoforge.catalog.units import assert_compatible, convert, parse_registered_unit
from mesoforge.common.errors import InvalidIdentifier


class TestParseRegisteredUnit:
    @pytest.mark.parametrize(
        "unit_id",
        ["K", "degC", "m/s", "kg/m^2", "percent", "dimensionless", "m", "Pa", "degree"],
    )
    def test_accepts_controlled_unit_ids(self, unit_id: str) -> None:
        parse_registered_unit(unit_id)

    def test_rejects_free_form_unit_string(self) -> None:
        with pytest.raises(InvalidIdentifier):
            parse_registered_unit("kelvin")  # not a controlled ID, even if Pint knows it

    def test_rejects_unknown_unit_id(self) -> None:
        with pytest.raises(InvalidIdentifier):
            parse_registered_unit("furlongs_per_fortnight")


class TestAssertCompatible:
    def test_temperature_units_are_compatible(self) -> None:
        assert_compatible("K", "degC")

    def test_length_units_are_compatible(self) -> None:
        assert_compatible("m", "m")

    def test_incompatible_units_raise(self) -> None:
        with pytest.raises(ValueError, match="incompatible"):
            assert_compatible("K", "m")

    def test_incompatible_pressure_and_speed_raise(self) -> None:
        with pytest.raises(ValueError, match="incompatible"):
            assert_compatible("Pa", "m/s")


class TestConvert:
    def test_celsius_to_kelvin_offset(self) -> None:
        result = convert(0.0, "degC", "K")
        assert math.isclose(result, 273.15, rel_tol=1e-9)

    def test_kelvin_to_celsius_offset(self) -> None:
        result = convert(273.15, "K", "degC")
        assert math.isclose(result, 0.0, abs_tol=1e-9)

    def test_celsius_boiling_point_to_kelvin(self) -> None:
        result = convert(100.0, "degC", "K")
        assert math.isclose(result, 373.15, rel_tol=1e-9)

    def test_identity_conversion(self) -> None:
        result = convert(42.0, "m", "m")
        assert math.isclose(result, 42.0)

    def test_incompatible_conversion_raises(self) -> None:
        with pytest.raises(ValueError, match="incompatible"):
            convert(1.0, "K", "m")

    def test_sequence_conversion(self) -> None:
        results = convert([0.0, 100.0], "degC", "K")
        assert list(results) == pytest.approx([273.15, 373.15])
