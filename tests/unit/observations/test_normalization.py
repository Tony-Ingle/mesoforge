"""Unit tests for mesoforge.observations.normalization (plan Section
3.7, Task 9): exact conversions, wind edge cases, digest stability,
and QC state derivation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mesoforge.catalog.configuration import ObservationNormalizationPolicy
from mesoforge.catalog.stations import StationRecord
from mesoforge.contracts.observations import RawMetarRecord
from mesoforge.observations.normalization import (
    MetarNormalizationError,
    compute_logical_observation_digest,
    compute_revision_digest,
    convert_temperature_c_to_k,
    convert_wind_speed_knots_to_m_s,
    derive_wind_components,
    normalize_metar_record,
)

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


def _raw(**overrides: object) -> RawMetarRecord:
    values: dict[str, object] = dict(
        icao_id="KCBG",
        obs_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
        report_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
        receipt_time=datetime(2026, 8, 28, 18, 1, tzinfo=UTC),
        temp=15.0,
        wdir=270.0,
        wspd=10.0,
        qc_field=0.0,
        metar_type="METAR",
        raw_ob="KCBG 281800Z 27010KT 10SM CLR 15/10 A3000",
        lat=45.557,
        lon=-93.264,
        elev=285.0,
    )
    values.update(overrides)
    return RawMetarRecord(**values)  # type: ignore[arg-type]


def _normalize(**overrides: object):
    return normalize_metar_record(
        raw=_raw(**overrides),
        station=_STATION,
        station_id="station.kcbg",
        policy=_POLICY,
        raw_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
        raw_record_index=0,
        station_snapshot_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        ingested_at=datetime(2026, 8, 28, 18, 2, tzinfo=UTC),
        query_window_start=_WINDOW_START,
        query_window_end=_WINDOW_END,
    )


class TestUnitConversions:
    def test_celsius_to_kelvin_known_case(self) -> None:
        assert convert_temperature_c_to_k(0.0) == pytest.approx(273.15)

    def test_celsius_to_kelvin_negative(self) -> None:
        assert convert_temperature_c_to_k(-40.0) == pytest.approx(233.15)

    def test_knots_to_m_s_known_case(self) -> None:
        assert convert_wind_speed_knots_to_m_s(10.0) == pytest.approx(5.144444444444445)

    def test_knots_to_m_s_zero(self) -> None:
        assert convert_wind_speed_knots_to_m_s(0.0) == pytest.approx(0.0)


class TestDeriveWindComponents:
    def test_wind_from_north(self) -> None:
        u, v = derive_wind_components(wind_speed_m_s=5.0, direction_degrees=0.0)
        assert u == pytest.approx(0.0, abs=1e-9)
        assert v == pytest.approx(-5.0)

    def test_wind_from_east(self) -> None:
        u, v = derive_wind_components(wind_speed_m_s=5.0, direction_degrees=90.0)
        assert u == pytest.approx(-5.0)
        assert v == pytest.approx(0.0, abs=1e-9)

    def test_wind_from_south(self) -> None:
        u, v = derive_wind_components(wind_speed_m_s=5.0, direction_degrees=180.0)
        assert u == pytest.approx(0.0, abs=1e-9)
        assert v == pytest.approx(5.0)

    def test_wind_from_west(self) -> None:
        u, v = derive_wind_components(wind_speed_m_s=5.0, direction_degrees=270.0)
        assert u == pytest.approx(5.0)
        assert v == pytest.approx(0.0, abs=1e-9)


class TestDigests:
    def test_logical_digest_is_deterministic(self) -> None:
        event_time = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
        d1 = compute_logical_observation_digest(station_id="station.kcbg", event_time=event_time)
        d2 = compute_logical_observation_digest(station_id="station.kcbg", event_time=event_time)
        assert d1 == d2

    def test_logical_digest_differs_by_station(self) -> None:
        event_time = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
        d1 = compute_logical_observation_digest(station_id="station.kcbg", event_time=event_time)
        d2 = compute_logical_observation_digest(station_id="station.kjmr", event_time=event_time)
        assert d1 != d2

    def test_revision_digest_differs_by_receipt_time(self) -> None:
        logical = compute_logical_observation_digest(
            station_id="station.kcbg", event_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC)
        )
        r1 = compute_revision_digest(
            logical_observation_digest=logical,
            receipt_time=datetime(2026, 8, 28, 18, 1, tzinfo=UTC),
            canonical_provider_record={"temp": 15.0},
        )
        r2 = compute_revision_digest(
            logical_observation_digest=logical,
            receipt_time=datetime(2026, 8, 28, 18, 5, tzinfo=UTC),
            canonical_provider_record={"temp": 15.0},
        )
        assert r1 != r2


class TestNormalizeMetarRecord:
    def test_eligible_full_record(self) -> None:
        observation = _normalize()
        assert observation.mesoforge_qc_state == "eligible"
        assert observation.temperature_k == pytest.approx(288.15)
        assert observation.wind_speed_m_s == pytest.approx(5.144444444444445)
        assert observation.wind_from_direction_degrees == pytest.approx(270.0)

    def test_calm_wind_zero_speed_zero_components_null_direction(self) -> None:
        observation = _normalize(wspd=0.0, wdir=270.0)
        assert observation.wind_speed_m_s == pytest.approx(0.0)
        assert observation.eastward_wind_10m_m_s == pytest.approx(0.0)
        assert observation.northward_wind_10m_m_s == pytest.approx(0.0)
        assert observation.wind_from_direction_degrees is None

    def test_vrb_direction_with_positive_speed_retains_speed_nulls_direction(self) -> None:
        observation = _normalize(wdir="VRB", wspd=10.0)
        assert observation.wind_speed_m_s is not None
        assert observation.wind_from_direction_degrees is None
        assert "variable_wind_direction" in observation.quality_flags

    def test_missing_direction_with_positive_speed_is_partial_behavior(self) -> None:
        observation = _normalize(wdir=None, wspd=10.0)
        assert observation.wind_speed_m_s is not None
        assert observation.wind_from_direction_degrees is None
        assert "wind_direction_missing" in observation.quality_flags

    def test_missing_speed_nulls_all_wind_fields(self) -> None:
        observation = _normalize(wspd=None, wdir=270.0)
        assert observation.wind_speed_m_s is None
        assert observation.eastward_wind_10m_m_s is None
        assert observation.northward_wind_10m_m_s is None

    def test_missing_temperature_nulls_temperature(self) -> None:
        observation = _normalize(temp=None)
        assert observation.temperature_k is None

    def test_temperature_out_of_range_rejected_as_null_with_flag(self) -> None:
        observation = _normalize(temp=200.0)  # 473.15 K, way above valid_max
        assert observation.temperature_k is None
        assert "temperature_out_of_range" in observation.quality_flags

    def test_does_not_reject_merely_because_qc_field_nonzero(self) -> None:
        observation = _normalize(qc_field=99.0)
        assert observation.mesoforge_qc_state == "eligible"
        assert observation.provider_qc_field == 99.0

    def test_rejects_receipt_before_event_by_more_than_five_minutes(self) -> None:
        with pytest.raises(MetarNormalizationError, match="precedes"):
            _normalize(
                obs_time=datetime(2026, 8, 28, 18, 10, tzinfo=UTC),
                receipt_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
            )

    def test_rejects_event_outside_query_window(self) -> None:
        with pytest.raises(MetarNormalizationError, match="outside the raw query window"):
            _normalize(
                obs_time=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
                receipt_time=datetime(2026, 8, 28, 20, 1, tzinfo=UTC),
            )

    def test_rejects_icao_mismatch(self) -> None:
        with pytest.raises(MetarNormalizationError, match="does not match"):
            _normalize(icao_id="KJMR")

    def test_station_coordinate_conflict_rejects_observation(self) -> None:
        observation = _normalize(lat=10.0)
        assert observation.mesoforge_qc_state == "rejected"
        assert observation.quality_flags == ("station_metadata_conflict",)

    def test_elevation_conflict_rejects_observation(self) -> None:
        observation = _normalize(elev=1000.0)
        assert observation.mesoforge_qc_state == "rejected"

    def test_wholly_absent_temp_and_wind_is_rejected(self) -> None:
        observation = _normalize(temp=None, wspd=None, wdir=None)
        assert observation.mesoforge_qc_state == "rejected"

    def test_quality_flags_are_sorted(self) -> None:
        observation = _normalize(temp=None, wspd=None, wdir=None)
        assert list(observation.quality_flags) == sorted(observation.quality_flags)
