"""Unit tests for mesoforge.observations.normalization_v2 (plan Section
6.1, Task 11): dew point/gust conversion and raw Prrrr parsing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mesoforge.observations.normalization_v2 import (
    convert_dew_point_c_to_k,
    convert_gust_knots_to_m_s,
    extract_hourly_precipitation,
)

_REPORT_TIME = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)


class TestConversions:
    def test_dew_point_conversion(self) -> None:
        assert convert_dew_point_c_to_k(0.0) == pytest.approx(273.15)

    def test_gust_conversion(self) -> None:
        assert convert_gust_knots_to_m_s(10.0) == pytest.approx(5.144444444444445)


class TestExtractHourlyPrecipitation:
    def test_reports_valid_prrrr_group(self) -> None:
        raw_ob = "KCBG 301853Z 18010KT 10SM CLR 25/15 A3000 RMK AO2 P0012 T02500150"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="METAR", report_time=_REPORT_TIME
        )
        assert result.status == "reported"
        assert result.amount_kg_m2 == pytest.approx(12 * 0.254)
        assert result.interval_end == _REPORT_TIME

    def test_missing_prrrr_is_missing_not_zero(self) -> None:
        raw_ob = "KCBG 301853Z 18010KT 10SM CLR 25/15 A3000 RMK AO2 T02500150"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="METAR", report_time=_REPORT_TIME
        )
        assert result.status == "missing"
        assert result.amount_kg_m2 is None

    def test_zero_precipitation_p0000_is_reported_zero(self) -> None:
        """A genuine P0000 group is a reported (non-missing) zero."""
        raw_ob = "KCBG 301853Z 18010KT 10SM CLR 25/15 A3000 RMK AO2 P0000"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="METAR", report_time=_REPORT_TIME
        )
        assert result.status == "reported"
        assert result.amount_kg_m2 == 0.0

    def test_speci_never_contributes_precipitation_truth(self) -> None:
        raw_ob = "SPECI KCBG 301853Z 18010KT 10SM CLR 25/15 A3000 RMK AO2 P0012"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="SPECI", report_time=_REPORT_TIME
        )
        assert result.status == "missing"

    def test_malformed_duplicate_groups(self) -> None:
        raw_ob = "KCBG 301853Z 18010KT 10SM CLR 25/15 A3000 RMK AO2 P0012 P0005"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="METAR", report_time=_REPORT_TIME
        )
        assert result.status == "malformed"

    def test_interval_is_the_one_hour_ending_at_report_time(self) -> None:
        raw_ob = "KCBG 301853Z 18010KT 10SM CLR 25/15 A3000 RMK AO2 P0025"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="METAR", report_time=_REPORT_TIME
        )
        assert result.interval_start is not None
        assert (result.interval_end - result.interval_start).total_seconds() == 3600.0

    def test_does_not_match_embedded_digits_in_other_groups(self) -> None:
        """A token like 'TP0012X' must not be mistaken for a standalone
        Prrrr group (word-boundary discipline)."""
        raw_ob = "KCBG 301853Z 18010KT 10SM CLR TP0012X 25/15 A3000 RMK AO2"
        result = extract_hourly_precipitation(
            raw_ob=raw_ob, metar_type="METAR", report_time=_REPORT_TIME
        )
        assert result.status == "missing"
