"""Unit tests for mesoforge.guidance.sources.gfs (plan Section 2.4/2.5,
Task 4).
"""

from __future__ import annotations

from datetime import date

import pytest

from mesoforge.guidance.sources.gfs import (
    build_field_selector,
    build_grib_url,
    build_index_url,
    format_grib_filename,
    normalize_longitude_to_minus180_180,
)
from tests.support.phase2_source_settings import make_gfs_settings

_SETTINGS = make_gfs_settings()


class TestFormatGribFilename:
    def test_formats_exact_filename(self) -> None:
        assert (
            format_grib_filename(_SETTINGS, cycle_hour=12, forecast_hour=6)
            == "gfs.t12z.pgrb2.0p25.f006"
        )


class TestBuildUrls:
    def test_builds_gcs_url(self) -> None:
        url = build_grib_url(
            _SETTINGS,
            endpoint="gcs_archive",
            cycle_date=date(2026, 8, 30),
            cycle_hour=12,
            forecast_hour=6,
        )
        assert url.endswith("gfs.t12z.pgrb2.0p25.f006")

    def test_index_url_appends_suffix(self) -> None:
        url = build_index_url(
            _SETTINGS,
            endpoint="gcs_archive",
            cycle_date=date(2026, 8, 30),
            cycle_hour=12,
            forecast_hour=6,
        )
        assert url.endswith(".idx")

    def test_rejects_unknown_endpoint(self) -> None:
        with pytest.raises(ValueError, match="unknown endpoint"):
            build_grib_url(
                _SETTINGS,
                endpoint="ftp",  # type: ignore[arg-type]
                cycle_date=date(2026, 8, 30),
                cycle_hour=12,
                forecast_hour=6,
            )


class TestBuildFieldSelector:
    def test_instantaneous_selector(self) -> None:
        selector = build_field_selector("air_temperature_2m", forecast_hour=6)
        assert selector == ":TMP:2 m above ground:6 hour fcst:$"

    def test_apcp_selector_uses_bucket_start(self) -> None:
        selector = build_field_selector(
            "liquid_equivalent_precipitation_amount_1h", forecast_hour=8
        )
        assert selector == ":APCP:surface:6-8 hour acc fcst:$"

    def test_apcp_selector_reset_hour(self) -> None:
        selector = build_field_selector(
            "liquid_equivalent_precipitation_amount_1h", forecast_hour=7
        )
        assert selector == ":APCP:surface:6-7 hour acc fcst:$"

    def test_rejects_lead_zero(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            build_field_selector("air_temperature_2m", forecast_hour=0)

    def test_rejects_unknown_variable(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            build_field_selector("not_a_variable", forecast_hour=1)


class TestNormalizeLongitude:
    def test_zero_stays_zero(self) -> None:
        assert normalize_longitude_to_minus180_180(0.0) == 0.0

    def test_positive_under_180_unchanged(self) -> None:
        assert normalize_longitude_to_minus180_180(90.0) == pytest.approx(90.0)

    def test_wraps_above_180(self) -> None:
        assert normalize_longitude_to_minus180_180(266.0) == pytest.approx(-94.0)

    def test_359_wraps_to_negative_one(self) -> None:
        assert normalize_longitude_to_minus180_180(359.0) == pytest.approx(-1.0)

    def test_exactly_180_wraps_to_negative_180(self) -> None:
        assert normalize_longitude_to_minus180_180(180.0) == pytest.approx(-180.0)
