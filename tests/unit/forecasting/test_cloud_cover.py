"""Independent boundary and unit checks for cloud display categories."""

import math

import pytest

from mesoforge.forecasting.cloud_cover import cloud_percentage, sky_category


@pytest.mark.parametrize(
    "value, expected",
    [
        (0.0, "clear"),
        (5.0, "clear"),
        (5.0000001, "mostly_clear"),
        (25.0, "mostly_clear"),
        (25.0000001, "partly_cloudy"),
        (50.0, "partly_cloudy"),
        (50.0000001, "mostly_cloudy"),
        (87.0, "mostly_cloudy"),
        (87.0000001, "cloudy"),
        (100.0, "cloudy"),
    ],
)
def test_categories_use_unrounded_percentages_and_inclusive_upper_bounds(value, expected):
    assert sky_category(value) == expected


@pytest.mark.parametrize("value", [-0.0001, 100.0001, math.nan, math.inf, -math.inf])
def test_invalid_cloud_values_are_not_clamped_or_given_categories(value):
    with pytest.raises(ValueError, match="no clipping"):
        sky_category(value)


def test_fraction_and_percentage_convert_without_changing_the_underlying_precision():
    assert cloud_percentage(0.254321, "1") == pytest.approx(25.4321, rel=1e-15)
    assert cloud_percentage(0.5, "(0 - 1)") == 50.0
    assert cloud_percentage(12.3456789, "%") == 12.3456789
    with pytest.raises(ValueError, match="Unsupported"):
        cloud_percentage(2.0, "okta")
    with pytest.raises(ValueError, match="no clipping"):
        cloud_percentage(1.01, "1")
