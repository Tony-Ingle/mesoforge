"""Independent examples for the documented Kuchera formula and profile completeness."""

import numpy as np
import pytest

from mesoforge.forecasting.snowfall_amount import kuchera_ratio

LEVELS = np.arange(500, 1001, 25)


def calculate(maximum, *, surface_pressure=101000):
    return kuchera_ratio(
        np.full((21, 1), maximum), LEVELS, np.array([maximum - 1]), np.array([surface_pressure])
    )


@pytest.mark.parametrize(
    "temperature,expected",
    [(271.16, 12), (273.15, 8.02), (261.16, 22), (277.16, 0), (278.16, -2), (221.16, 62)],
)
def test_documented_formula_has_no_constant_ratio_fallback_or_clamping(temperature, expected):
    result = calculate(temperature)
    assert result.ratio[0] == pytest.approx(expected, abs=1e-12)
    assert result.maximum_temperature_k[0] == temperature and result.valid_profile[0]


def test_warm_layer_aloft_controls_ratio_and_belowground_values_are_excluded():
    profile = np.full((21, 2), 260.0)
    profile[10] = [274.16, 262.16]  # 750 hPa warm layer controls each column independently.
    profile[LEVELS > 850] = [np.nan, 310.0]
    original = profile.copy()
    result = kuchera_ratio(profile, LEVELS, np.array([261.16, 261.16]), np.array([85000, 85000]))
    np.testing.assert_allclose(result.ratio, [6, 21], rtol=0, atol=1e-12)
    assert not result.aboveground_mask[LEVELS > 850].any()
    np.testing.assert_equal(profile, original)
    assert result.valid_profile.all()


def test_near_surface_air_is_part_of_maximum_and_missing_air_fails_closed():
    profile = np.full((21, 3), 260.0)
    profile[0, 1] = np.nan
    result = kuchera_ratio(profile, LEVELS, np.array([271.16, 271.16, np.nan]), np.full(3, 101000))
    assert result.ratio[0] == 12
    assert np.isnan(result.ratio[1:]).all() and not result.valid_profile[1:].any()
    assert "above-ground" in result.missing_reasons[1]
    assert "2 m" in result.missing_reasons[2]


@pytest.mark.parametrize("pressure", [np.nan, np.inf, 49999, -1])
def test_surface_pressure_is_required_to_define_atmospheric_column(pressure):
    result = calculate(260, surface_pressure=pressure)
    assert not result.valid_profile[0] and np.isnan(result.ratio[0])
    assert "surface pressure" in result.missing_reasons[0]


def test_pressure_slots_must_be_complete_but_their_order_is_not_assumed():
    with pytest.raises(ValueError, match="21 unique"):
        kuchera_ratio(np.full((20, 1), 260), LEVELS[:-1], np.array([260]), np.array([100000]))
    duplicate = LEVELS.copy()
    duplicate[-1] = 975
    with pytest.raises(ValueError, match="21 unique"):
        kuchera_ratio(np.full((21, 1), 260), duplicate, np.array([260]), np.array([100000]))
    one = calculate(261.16)
    reverse = kuchera_ratio(
        np.full((21, 1), 261.16), LEVELS[::-1], np.array([260.16]), np.array([101000])
    )
    np.testing.assert_equal(one.ratio, reverse.ratio)


def test_dimensions_and_physical_kelvin_are_explicit():
    with pytest.raises(ValueError, match="dimensions"):
        kuchera_ratio(np.full((21, 2), 260), LEVELS, np.array([260]), np.array([100000]))
    assert not calculate(0).valid_profile[0]
