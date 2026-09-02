"""Unit tests for mesoforge.guidance.sources.nbm (plan Section 2.3, Task 3)."""

from __future__ import annotations

from datetime import date

import pytest

from mesoforge.guidance.sources.nbm import (
    build_field_selector,
    build_grib_url,
    build_index_url,
    convert_pop_percent_to_fraction,
    convert_speed_direction_to_components,
    format_grib_filename,
)
from tests.support.phase2_source_settings import make_nbm_settings

_SETTINGS = make_nbm_settings()


class TestFormatGribFilename:
    def test_formats_exact_filename(self) -> None:
        assert (
            format_grib_filename(_SETTINGS, cycle_hour=12, forecast_hour=6)
            == "blend.t12z.core.f006.co.grib2"
        )

    def test_pads_three_digit_lead(self) -> None:
        assert (
            format_grib_filename(_SETTINGS, cycle_hour=0, forecast_hour=36)
            == "blend.t00z.core.f036.co.grib2"
        )


class TestBuildUrls:
    def test_builds_noaa_s3_url(self) -> None:
        url = build_grib_url(
            _SETTINGS,
            endpoint="noaa_s3",
            cycle_date=date(2026, 8, 30),
            cycle_hour=12,
            forecast_hour=1,
        )
        assert "blend.20260830" in url
        assert url.endswith("blend.t12z.core.f001.co.grib2")

    def test_index_url_appends_suffix(self) -> None:
        url = build_index_url(
            _SETTINGS,
            endpoint="noaa_s3",
            cycle_date=date(2026, 8, 30),
            cycle_hour=12,
            forecast_hour=1,
        )
        assert url.endswith(".grib2.idx")

    def test_rejects_unknown_endpoint(self) -> None:
        with pytest.raises(ValueError, match="unknown endpoint"):
            build_grib_url(
                _SETTINGS,
                endpoint="ftp",  # type: ignore[arg-type]
                cycle_date=date(2026, 8, 30),
                cycle_hour=12,
                forecast_hour=1,
            )


class TestBuildFieldSelector:
    def test_instantaneous_selector(self) -> None:
        selector = build_field_selector("air_temperature_2m", forecast_hour=6)
        assert selector == ":TMP:2 m above ground:6 hour fcst:$"

    def test_deterministic_apcp_selector(self) -> None:
        selector = build_field_selector(
            "liquid_equivalent_precipitation_amount_1h", forecast_hour=6
        )
        assert selector == ":APCP:surface:5-6 hour acc fcst:$"

    def test_pop_selector(self) -> None:
        selector = build_field_selector("probability_of_precipitation_1h", forecast_hour=6)
        assert "prob >0\\.254" in selector
        assert "probability forecast" in selector

    def test_rejects_lead_zero(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            build_field_selector("air_temperature_2m", forecast_hour=0)

    def test_rejects_unknown_variable(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            build_field_selector("not_a_variable", forecast_hour=1)

    def test_deterministic_and_probability_selectors_are_mutually_exclusive(self) -> None:
        """Section 2.3: NBM must never select ensemble std-dev/percentile/
        QMD records or conflate deterministic APCP with PoP01 -- the two
        selector patterns must not match the same descriptor text."""
        import re

        det = build_field_selector("liquid_equivalent_precipitation_amount_1h", forecast_hour=6)
        pop = build_field_selector("probability_of_precipitation_1h", forecast_hour=6)
        pop_descriptor = (
            ":APCP:surface:5-6 hour acc fcst:prob >0.254:0-6 hour acc fcst:probability forecast:"
        )
        det_descriptor = ":APCP:surface:5-6 hour acc fcst:"
        assert re.search(det, pop_descriptor) is None
        assert re.search(det, det_descriptor) is not None
        assert re.search(pop, pop_descriptor) is not None
        assert re.search(pop, det_descriptor) is None


class TestConvertSpeedDirectionToComponents:
    def test_north_wind_blows_south(self) -> None:
        # Meteorological "from north" (0 deg) blows toward south: u=0, v=-speed
        u, v = convert_speed_direction_to_components(speed_m_s=10.0, direction_degrees=0.0)
        assert u == pytest.approx(0.0, abs=1e-9)
        assert v == pytest.approx(-10.0)

    def test_east_wind_blows_west(self) -> None:
        u, v = convert_speed_direction_to_components(speed_m_s=5.0, direction_degrees=90.0)
        assert u == pytest.approx(-5.0)
        assert v == pytest.approx(0.0, abs=1e-9)

    def test_calm_yields_zero_regardless_of_direction(self) -> None:
        u, v = convert_speed_direction_to_components(speed_m_s=0.0, direction_degrees=999.0)
        assert (u, v) == (0.0, 0.0)

    def test_rejects_negative_speed(self) -> None:
        with pytest.raises(ValueError, match="nonnegative"):
            convert_speed_direction_to_components(speed_m_s=-1.0, direction_degrees=0.0)

    def test_rejects_out_of_range_direction_at_nonzero_speed(self) -> None:
        with pytest.raises(ValueError, match="0, 360"):
            convert_speed_direction_to_components(speed_m_s=1.0, direction_degrees=360.0)

    def test_rejects_negative_direction(self) -> None:
        with pytest.raises(ValueError, match="0, 360"):
            convert_speed_direction_to_components(speed_m_s=1.0, direction_degrees=-1.0)


class TestConvertPopPercentToFraction:
    def test_converts_exactly_once(self) -> None:
        assert convert_pop_percent_to_fraction(45.0) == pytest.approx(0.45)

    def test_zero_and_hundred_bounds(self) -> None:
        assert convert_pop_percent_to_fraction(0.0) == 0.0
        assert convert_pop_percent_to_fraction(100.0) == 1.0

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="0, 100"):
            convert_pop_percent_to_fraction(101.0)
