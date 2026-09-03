"""Unit tests for mesoforge.observations.normalization_v2 (plan Section
6.1, Task 11): dew point/gust conversion and raw Prrrr parsing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationRecord
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.observations_v2 import RawMetarRecordV2
from mesoforge.observations.normalization_v2 import (
    MetarNormalizationError,
    convert_dew_point_c_to_k,
    convert_gust_knots_to_m_s,
    extract_hourly_precipitation,
    normalize_metar_record_v2,
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


_POLICY = ObservationNormalizationPolicy(policy_id="metar-normalization.v1")

_STATION = StationRecord(
    station_id="station.kcbg",
    provider_icao_id="KCBG",
    latitude=45.557,
    longitude=-93.264,
    elevation_m=285.0,
    site_name="Cambridge Muni",
    site_types=("METAR",),
)

_WINDOW_START = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
_WINDOW_END = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)


def _raw_v2(**overrides: object) -> RawMetarRecordV2:
    values: dict[str, object] = dict(
        icao_id="KCBG",
        obs_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
        report_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
        receipt_time=datetime(2026, 8, 28, 18, 1, tzinfo=UTC),
        temp=15.0,
        dewp=10.0,
        wdir=270.0,
        wspd=10.0,
        wgst=20.0,
        qc_field=0.0,
        metar_type="METAR",
        raw_ob="KCBG 281800Z 27010KT 10SM CLR 15/10 A3000 RMK AO2 P0012",
        lat=45.557,
        lon=-93.264,
        elev=285.0,
    )
    values.update(overrides)
    return RawMetarRecordV2(**values)  # type: ignore[arg-type]


def _normalize_v2(**overrides: object):
    return normalize_metar_record_v2(
        raw=_raw_v2(**overrides),
        station=_STATION,
        station_id="station.kcbg",
        policy=_POLICY,
        raw_artifact_id=ArtifactId.generate(),
        raw_record_index=0,
        station_snapshot_artifact_id=ArtifactId.generate(),
        ingested_at=datetime(2026, 8, 28, 18, 2, tzinfo=UTC),
        query_window_start=_WINDOW_START,
        query_window_end=_WINDOW_END,
    )


class TestNormalizeMetarRecordV2:
    def test_happy_path_converts_dew_point_gust_and_precipitation(self) -> None:
        result = _normalize_v2()
        assert result.dew_point_k == pytest.approx(convert_dew_point_c_to_k(10.0))
        assert result.wind_gust_m_s == pytest.approx(convert_gust_knots_to_m_s(20.0))
        assert result.precipitation_truth_status == "reported"
        assert result.precipitation_amount_kg_m2 == pytest.approx(12 * 0.254)
        assert result.mesoforge_qc_state == "eligible"

    def test_missing_dew_point_leaves_field_null(self) -> None:
        result = _normalize_v2(dewp=None)
        assert result.dew_point_k is None

    def test_missing_gust_leaves_field_null(self) -> None:
        result = _normalize_v2(wgst=None)
        assert result.wind_gust_m_s is None

    def test_missing_precipitation_is_missing_not_zero(self) -> None:
        result = _normalize_v2(raw_ob="KCBG 281800Z 27010KT 10SM CLR 15/10 A3000 RMK AO2")
        assert result.precipitation_truth_status == "missing"
        assert result.precipitation_amount_kg_m2 is None

    def test_wrong_station_raises(self) -> None:
        with pytest.raises(MetarNormalizationError):
            _normalize_v2(icao_id="KXYZ")

    def test_due_north_360_is_canonicalized_to_zero(self) -> None:
        """METAR reports due north as 360, but the canonical contract --
        and ``matched-pairs.v2``'s own validator -- is the half-open
        [0, 360) circle. A retained 360 makes every matched pair carrying
        a due north observation fail validation, which is exactly what
        the real KCBG/KJMR/KROS records produced.

        The canonicalization must be exact: ``math.radians(360.0)`` is
        not identically zero, so deriving the components from 360 leaks a
        spurious westward/eastward component into a due north wind.
        """
        result = _normalize_v2(wdir=360.0)
        assert result.wind_from_direction_degrees == 0.0
        assert result.eastward_wind_10m_m_s == 0.0
        # Due north wind blows FROM the north, i.e. toward the south.
        assert result.northward_wind_10m_m_s is not None
        assert result.northward_wind_10m_m_s < 0.0
        assert "wind_direction_out_of_range" not in result.quality_flags

    def test_due_north_360_matches_an_explicit_zero_exactly(self) -> None:
        from_360 = _normalize_v2(wdir=360.0)
        from_zero = _normalize_v2(wdir=0.0)
        assert from_360.wind_from_direction_degrees == from_zero.wind_from_direction_degrees
        assert from_360.eastward_wind_10m_m_s == from_zero.eastward_wind_10m_m_s
        assert from_360.northward_wind_10m_m_s == from_zero.northward_wind_10m_m_s
