"""Unit tests for mesoforge.verification.matching (plan Section 3.8,
Task 10): as-of revision selection, tie-break, calm-direction
exclusion, and the mutually-exclusive field-status precedence order."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from mesoforge.catalog.configuration import MatchingPolicy
from mesoforge.contracts.observations import NormalizedObservation
from mesoforge.forecasting.baseline import assemble_baseline_forecast
from mesoforge.verification.matching import match_baseline_to_observations

_STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_LEADS = tuple(range(7))
_ISSUE = np.datetime64("2026-08-28T12:00:00", "ns")
_POLICY = MatchingPolicy(matching_policy_id="metar-nearest-15m.v1")
_ARTIFACT_A = "art_00000000-0000-0000-0000-000000000000"
_ARTIFACT_B = "art_00000000-0000-0000-0000-000000000001"
_CUTOFF = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


def _baseline(*, valid: bool = True) -> object:
    values: dict[tuple[str, int, str], float] = {}
    for lead in _LEADS:
        for station in _STATIONS:
            values[(station, lead, "air_temperature_2m")] = 288.0 + lead
            values[(station, lead, "eastward_wind_10m")] = 1.0
            values[(station, lead, "northward_wind_10m")] = -2.0
    return assemble_baseline_forecast(
        station_values=values,
        lead_hours=_LEADS,
        forecast_issue_time=_ISSUE,
        hrrr_source_reference_time=_ISSUE,
        contributor_artifact_id=_ARTIFACT_A,
        extraction_report_artifact_id=_ARTIFACT_B,
    )


def _observation(
    *,
    station_id: str = "station.kcbg",
    event_time: datetime = datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    provider_available_at: datetime = datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
    revision_digest: str = "sha256:" + "b" * 64,
    logical_observation_digest: str = "sha256:" + "a" * 64,
    temperature_k: float | None = 289.0,
    wind_speed_m_s: float | None = 5.0,
    wind_from_direction_degrees: float | None = 270.0,
    eastward_wind_10m_m_s: float | None = 5.0,
    northward_wind_10m_m_s: float | None = 0.0,
    quality_flags: tuple[str, ...] = (),
    mesoforge_qc_state: str = "eligible",
) -> NormalizedObservation:
    return NormalizedObservation(
        logical_observation_digest=logical_observation_digest,  # type: ignore[arg-type]
        revision_digest=revision_digest,  # type: ignore[arg-type]
        station_id=station_id,  # type: ignore[arg-type]
        provider_station_id="KCBG",
        event_time=event_time,
        report_time=event_time,
        provider_available_at=provider_available_at,
        ingested_at=provider_available_at,
        metar_type="METAR",
        raw_observation="raw",
        raw_record_digest="sha256:" + "c" * 64,  # type: ignore[arg-type]
        raw_artifact_id=_ARTIFACT_A,  # type: ignore[arg-type]
        raw_record_index=0,
        station_snapshot_artifact_id=_ARTIFACT_B,  # type: ignore[arg-type]
        latitude_degrees=45.557,
        longitude_degrees=-93.264,
        elevation_m=285.0,
        temperature_k=temperature_k,
        wind_speed_m_s=wind_speed_m_s,
        wind_from_direction_degrees=wind_from_direction_degrees,
        eastward_wind_10m_m_s=eastward_wind_10m_m_s,
        northward_wind_10m_m_s=northward_wind_10m_m_s,
        mesoforge_qc_state=mesoforge_qc_state,  # type: ignore[arg-type]
        quality_flags=quality_flags,
    )


def _match(observations: list[NormalizedObservation], **overrides: object) -> list:
    kwargs: dict[str, object] = dict(
        baseline=_baseline(),
        station_ids=_STATIONS,
        lead_hours=_LEADS,
        observations=observations,
        matching_policy=_POLICY,
        verification_cutoff=_CUTOFF,
        baseline_artifact_id=_ARTIFACT_A,
        observations_artifact_id=_ARTIFACT_B,
    )
    kwargs.update(overrides)
    return match_baseline_to_observations(**kwargs)  # type: ignore[arg-type]


class TestExpectedRowCount:
    def test_exactly_21_rows_for_3_stations_7_leads(self) -> None:
        rows = _match([])
        assert len(rows) == 21

    def test_one_row_per_station_lead_combination(self) -> None:
        rows = _match([])
        combos = {(row.station_id, row.lead_hours) for row in rows}
        assert len(combos) == 21


class TestNoReportWithinTolerance:
    def test_no_candidates_gives_no_report_within_tolerance(self) -> None:
        rows = _match([])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "no_report_within_tolerance"
        assert row.row_status == "matched_no_fields"

    def test_candidate_outside_tolerance_window_excluded(self) -> None:
        far_observation = _observation(event_time=datetime(2026, 8, 28, 13, 0, tzinfo=UTC))
        rows = _match([far_observation])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "no_report_within_tolerance"


class TestRevisionAfterCutoff:
    def test_all_candidates_after_cutoff_gives_revision_after_cutoff(self) -> None:
        late_observation = _observation(
            provider_available_at=datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
        )
        rows = _match(
            [late_observation], verification_cutoff=datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
        )
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "revision_after_cutoff"


class TestAsOfRevisionSelection:
    def test_selects_latest_eligible_revision_as_of_cutoff(self) -> None:
        early = _observation(
            revision_digest="sha256:" + "1" * 64,
            provider_available_at=datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
            temperature_k=288.0,
        )
        later_correction = _observation(
            revision_digest="sha256:" + "2" * 64,
            provider_available_at=datetime(2026, 8, 28, 12, 5, tzinfo=UTC),
            temperature_k=290.0,
        )
        rows = _match([early, later_correction])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.observed_temperature_k == pytest.approx(290.0)

    def test_ignores_revision_after_cutoff_even_if_closer_to_valid_time(self) -> None:
        eligible = _observation(
            revision_digest="sha256:" + "1" * 64,
            provider_available_at=datetime(2026, 8, 28, 12, 1, tzinfo=UTC),
            temperature_k=288.0,
        )
        ineligible = _observation(
            revision_digest="sha256:" + "2" * 64,
            provider_available_at=datetime(2026, 8, 28, 20, 1, tzinfo=UTC),
            temperature_k=999.0,
        )
        rows = _match([eligible, ineligible])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.observed_temperature_k == pytest.approx(288.0)


class TestSmallestDeltaAcrossLogicalObservations:
    def test_selects_closest_logical_observation_by_time_delta(self) -> None:
        close = _observation(
            logical_observation_digest="sha256:" + "1" * 64,
            revision_digest="sha256:" + "11" + "0" * 62,
            event_time=datetime(2026, 8, 28, 12, 5, tzinfo=UTC),
            provider_available_at=datetime(2026, 8, 28, 12, 6, tzinfo=UTC),
            temperature_k=288.0,
        )
        far = _observation(
            logical_observation_digest="sha256:" + "2" * 64,
            revision_digest="sha256:" + "22" + "0" * 62,
            event_time=datetime(2026, 8, 28, 12, 12, tzinfo=UTC),
            provider_available_at=datetime(2026, 8, 28, 12, 13, tzinfo=UTC),
            temperature_k=290.0,
        )
        rows = _match([far, close])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.observed_temperature_k == pytest.approx(288.0)

    def test_tie_break_prefers_earlier_event_time(self) -> None:
        earlier = _observation(
            logical_observation_digest="sha256:" + "1" * 64,
            revision_digest="sha256:" + "11" + "0" * 62,
            event_time=datetime(2026, 8, 28, 11, 55, tzinfo=UTC),
            provider_available_at=datetime(2026, 8, 28, 11, 56, tzinfo=UTC),
            temperature_k=287.0,
        )
        later = _observation(
            logical_observation_digest="sha256:" + "2" * 64,
            revision_digest="sha256:" + "22" + "0" * 62,
            event_time=datetime(2026, 8, 28, 12, 5, tzinfo=UTC),
            provider_available_at=datetime(2026, 8, 28, 12, 6, tzinfo=UTC),
            temperature_k=290.0,
        )
        rows = _match([earlier, later])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.observed_temperature_k == pytest.approx(287.0)


class TestStationMetadataConflict:
    def test_conflict_flag_rejects_all_fields(self) -> None:
        conflicting = _observation(quality_flags=("station_metadata_conflict",))
        rows = _match([conflicting])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "station_metadata_conflict"
        assert row.wind_speed_status == "station_metadata_conflict"
        assert row.row_status == "matched_no_fields"


class TestObservationQcRejected:
    def test_rejected_qc_state_rejects_all_fields(self) -> None:
        rejected = _observation(
            mesoforge_qc_state="rejected", temperature_k=None, wind_speed_m_s=None
        )
        rows = _match([rejected])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "observation_qc_rejected"
        assert row.row_status == "matched_no_fields"


class TestPerFieldStatus:
    def test_missing_temperature_only_still_matches_wind(self) -> None:
        observation = _observation(temperature_k=None)
        rows = _match([observation])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "temperature_missing"
        assert row.wind_speed_status == "matched"
        assert row.row_status == "matched_any_field"

    def test_missing_wind_speed_nulls_all_wind_fields(self) -> None:
        observation = _observation(
            wind_speed_m_s=None,
            wind_from_direction_degrees=None,
            eastward_wind_10m_m_s=None,
            northward_wind_10m_m_s=None,
        )
        rows = _match([observation])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.wind_speed_status == "wind_speed_missing"
        assert row.eastward_component_status == "wind_speed_missing"
        assert row.wind_direction_status == "wind_speed_missing"

    def test_missing_direction_but_present_speed_matches_speed_not_direction(self) -> None:
        observation = _observation(
            wind_from_direction_degrees=None,
            eastward_wind_10m_m_s=None,
            northward_wind_10m_m_s=None,
        )
        rows = _match([observation])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.wind_speed_status == "matched"
        assert row.wind_direction_status == "wind_direction_missing_or_variable"
        assert row.eastward_component_status == "wind_direction_missing_or_variable"


class TestCalmDirectionExclusion:
    def test_calm_observed_speed_excludes_direction_but_matches_speed(self) -> None:
        # baseline forecast wind speed at lead 0 is hypot(1,-2) ~= 2.236 m/s (above calm)
        calm_observation = _observation(
            wind_speed_m_s=0.5,
            wind_from_direction_degrees=270.0,
            eastward_wind_10m_m_s=0.0,
            northward_wind_10m_m_s=0.0,
        )
        rows = _match([calm_observation])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.wind_speed_status == "matched"
        assert row.wind_direction_status == "calm_direction_excluded"

    def test_values_exactly_at_threshold_are_eligible(self) -> None:
        threshold_observation = _observation(
            wind_speed_m_s=_POLICY.calm_threshold_m_s,
            wind_from_direction_degrees=270.0,
        )
        rows = _match([threshold_observation])
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.wind_direction_status == "matched"


class TestForecastMissingOrInvalid:
    def test_nan_forecast_value_produces_forecast_missing_status(self) -> None:
        values: dict[tuple[str, int, str], float] = {}
        for lead in _LEADS:
            for station in _STATIONS:
                values[(station, lead, "air_temperature_2m")] = 288.0
                values[(station, lead, "eastward_wind_10m")] = 1.0
                values[(station, lead, "northward_wind_10m")] = -2.0
        baseline = assemble_baseline_forecast(
            station_values=values,
            lead_hours=_LEADS,
            forecast_issue_time=_ISSUE,
            hrrr_source_reference_time=_ISSUE,
            contributor_artifact_id=_ARTIFACT_A,
            extraction_report_artifact_id=_ARTIFACT_B,
        )
        baseline["air_temperature_2m"].values[0, 0] = float("nan")
        rows = _match([], baseline=baseline)
        row = next(r for r in rows if r.station_id == "station.kcbg" and r.lead_hours == 0)
        assert row.temperature_status == "forecast_missing_or_invalid"
        assert row.wind_speed_status == "forecast_missing_or_invalid"
        assert row.row_status == "matched_no_fields"
