"""Mass-equivalent ice is neither ice thickness nor freezing-rain liquid."""

import math

import pytest

from mesoforge.forecasting.ice import FLAT_ICE, FREEZING_RAIN, ice_amount, validate_ice_event
from tests.unit.application.test_ice import ice_view


@pytest.mark.parametrize("quantity", [FLAT_ICE, FREEZING_RAIN])
@pytest.mark.parametrize("unit", ["kg/m^2", "kg m**-2", "kg m-2", "kg m^-2"])
def test_native_mass_per_area_units_remain_exact(quantity, unit):
    assert ice_amount(0.123456789, quantity_kind=quantity, native_unit=unit) == 0.123456789
    assert ice_amount(0, quantity_kind=quantity, native_unit=unit) == 0


@pytest.mark.parametrize("value", [-0.001, math.nan, math.inf])
def test_invalid_amount_is_not_zero_or_clipped(value):
    with pytest.raises(ValueError, match="nonnegative"):
        ice_amount(value, quantity_kind=FLAT_ICE)


@pytest.mark.parametrize(
    "quantity,unit",
    [
        (FLAT_ICE, "mm"),
        (FLAT_ICE, "m"),
        (FLAT_ICE, "in"),
        ("snowfall_water_equivalent", "kg/m^2"),
        (FREEZING_RAIN, "ice thickness"),
    ],
)
def test_no_silent_density_ice_ratio_or_other_field_conversion(quantity, unit):
    with pytest.raises(ValueError, match="quantity/unit"):
        ice_amount(1, quantity_kind=quantity, native_unit=unit)


def test_exact_native_interval_and_cumulative_normalization_have_separate_semantics():
    native = ice_view("NBM_FICEAC_6H").manifest["events"][0]
    liquid = ice_view("HRRR_FRZR").manifest["events"][0]
    native_start, native_end = validate_ice_event(native)
    liquid_start, liquid_end = validate_ice_event(liquid)
    assert (native_end - native_start).total_seconds() == 6 * 3600
    assert (liquid_end - liquid_start).total_seconds() == 3600
    liquid["native_interval_start"] = "2026-09-11T13:00:00Z"
    with pytest.raises(ValueError, match="cumulative"):
        validate_ice_event(liquid)
