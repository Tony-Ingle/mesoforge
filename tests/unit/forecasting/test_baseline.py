"""Unit tests for mesoforge.forecasting.baseline (plan Section 3.6,
Task 7): derived wind speed/direction, cardinal/intercardinal vectors,
zero calm, missing-source rejection."""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.forecasting.baseline import (
    BaselineAssemblyError,
    assemble_baseline_forecast,
    derive_wind_speed_and_direction,
)

pytestmark = pytest.mark.scientific


class TestDeriveWindSpeedAndDirection:
    def test_northerly_wind_direction_is_zero(self) -> None:
        # Wind FROM the north means it blows southward: u=0, v=-5
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([0.0]), northward_m_s=np.array([-5.0])
        )
        assert speed[0] == pytest.approx(5.0)
        assert direction[0] == pytest.approx(0.0, abs=1e-9)

    def test_easterly_wind_direction_is_90(self) -> None:
        # Wind FROM the east blows westward: u=-5, v=0
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([-5.0]), northward_m_s=np.array([0.0])
        )
        assert speed[0] == pytest.approx(5.0)
        assert direction[0] == pytest.approx(90.0, abs=1e-9)

    def test_southerly_wind_direction_is_180(self) -> None:
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([0.0]), northward_m_s=np.array([5.0])
        )
        assert speed[0] == pytest.approx(5.0)
        assert direction[0] == pytest.approx(180.0, abs=1e-9)

    def test_westerly_wind_direction_is_270(self) -> None:
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([5.0]), northward_m_s=np.array([0.0])
        )
        assert speed[0] == pytest.approx(5.0)
        assert direction[0] == pytest.approx(270.0, abs=1e-9)

    def test_northeasterly_wind_direction_is_45(self) -> None:
        # Wind FROM the northeast: u<0, v<0
        component = 5.0 / np.sqrt(2.0)
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([-component]), northward_m_s=np.array([-component])
        )
        assert speed[0] == pytest.approx(5.0)
        assert direction[0] == pytest.approx(45.0, abs=1e-6)

    def test_zero_wind_is_calm_with_undefined_direction(self) -> None:
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([0.0]), northward_m_s=np.array([0.0])
        )
        assert speed[0] == pytest.approx(0.0)
        assert np.isnan(direction[0])

    def test_signed_zero_components_are_calm(self) -> None:
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([-0.0]), northward_m_s=np.array([0.0])
        )
        assert speed[0] == pytest.approx(0.0)
        assert np.isnan(direction[0])

    def test_direction_always_in_0_360(self) -> None:
        rng = np.random.default_rng(7)
        u = rng.uniform(-20, 20, size=50)
        v = rng.uniform(-20, 20, size=50)
        speed, direction = derive_wind_speed_and_direction(eastward_m_s=u, northward_m_s=v)
        finite_direction = direction[speed > 0]
        assert np.all(finite_direction >= 0.0)
        assert np.all(finite_direction < 360.0)

    def test_float64_intermediates_regardless_of_input_dtype(self) -> None:
        speed, direction = derive_wind_speed_and_direction(
            eastward_m_s=np.array([3.0], dtype=np.float32),
            northward_m_s=np.array([4.0], dtype=np.float32),
        )
        assert speed.dtype == np.float64
        assert direction.dtype == np.float64


def _issue_time() -> np.datetime64:
    return np.datetime64("2026-08-28T18:00:00", "ns")


def _complete_station_values() -> dict[tuple[str, int, str], float]:
    values: dict[tuple[str, int, str], float] = {}
    for station in ("station.kcbg", "station.kjmr", "station.kros"):
        for lead in range(7):
            values[(station, lead, "air_temperature_2m")] = 280.0 + lead
            values[(station, lead, "eastward_wind_10m")] = 1.0
            values[(station, lead, "northward_wind_10m")] = 2.0
    return values


class TestAssembleBaselineForecast:
    def test_produces_complete_dataset(self) -> None:
        dataset = assemble_baseline_forecast(
            station_values=_complete_station_values(),
            lead_hours=tuple(range(7)),
            forecast_issue_time=_issue_time(),
            hrrr_source_reference_time=_issue_time(),
            contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        assert dataset.sizes["lead_time"] == 7
        assert dataset.sizes["location"] == 3
        assert list(dataset["location"].values) == [
            "station.kcbg",
            "station.kjmr",
            "station.kros",
        ]

    def test_rejects_missing_station_value(self) -> None:
        values = _complete_station_values()
        del values[("station.kros", 3, "air_temperature_2m")]
        with pytest.raises(BaselineAssemblyError, match="missing"):
            assemble_baseline_forecast(
                station_values=values,
                lead_hours=tuple(range(7)),
                forecast_issue_time=_issue_time(),
                hrrr_source_reference_time=_issue_time(),
                contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
            )

    def test_rejects_nonfinite_station_value(self) -> None:
        values = _complete_station_values()
        values[("station.kcbg", 0, "eastward_wind_10m")] = float("nan")
        with pytest.raises(BaselineAssemblyError, match="non-finite"):
            assemble_baseline_forecast(
                station_values=values,
                lead_hours=tuple(range(7)),
                forecast_issue_time=_issue_time(),
                hrrr_source_reference_time=_issue_time(),
                contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
            )

    def test_valid_time_equals_hrrr_source_reference_time_plus_lead(self) -> None:
        """Codex review t_09a43c6c finding 3 regression: valid times
        must track hrrr_source_reference_time, not forecast_issue_time,
        even when the two diverge."""
        issue_time = np.datetime64("2026-08-28T18:30:00", "ns")
        source_reference_time = np.datetime64("2026-08-28T18:00:00", "ns")
        dataset = assemble_baseline_forecast(
            station_values=_complete_station_values(),
            lead_hours=tuple(range(7)),
            forecast_issue_time=issue_time,
            hrrr_source_reference_time=source_reference_time,
            contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        expected = source_reference_time + np.array([np.timedelta64(h, "h") for h in range(7)])
        np.testing.assert_array_equal(dataset["valid_time"].values, expected)
        # Explicitly not equal to the issue-time-anchored computation --
        # proves valid times are not silently shifting with issue time.
        wrong = issue_time + np.array([np.timedelta64(h, "h") for h in range(7)])
        assert not np.array_equal(dataset["valid_time"].values, wrong)

    def test_valid_time_unaffected_by_issue_time_when_source_time_fixed(self) -> None:
        """A later issue_time (e.g. run issued after the HRRR cycle it
        consumes) must not shift any lead's valid time -- METAR matching
        targets the HRRR source valid time, not issuance identity."""
        source_reference_time = np.datetime64("2026-08-28T12:00:00", "ns")
        early_issue = source_reference_time
        late_issue = source_reference_time + np.timedelta64(45, "m")

        early_dataset = assemble_baseline_forecast(
            station_values=_complete_station_values(),
            lead_hours=tuple(range(7)),
            forecast_issue_time=early_issue,
            hrrr_source_reference_time=source_reference_time,
            contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        late_dataset = assemble_baseline_forecast(
            station_values=_complete_station_values(),
            lead_hours=tuple(range(7)),
            forecast_issue_time=late_issue,
            hrrr_source_reference_time=source_reference_time,
            contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        np.testing.assert_array_equal(
            early_dataset["valid_time"].values, late_dataset["valid_time"].values
        )

    def test_calm_station_has_undefined_direction(self) -> None:
        values = _complete_station_values()
        values[("station.kcbg", 0, "eastward_wind_10m")] = 0.0
        values[("station.kcbg", 0, "northward_wind_10m")] = 0.0
        dataset = assemble_baseline_forecast(
            station_values=values,
            lead_hours=tuple(range(7)),
            forecast_issue_time=_issue_time(),
            hrrr_source_reference_time=_issue_time(),
            contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        direction = dataset["wind_from_direction_10m"].sel(location="station.kcbg").values[0]
        assert np.isnan(direction)
        mask = (
            dataset["wind_from_direction_10m_quality_mask"].sel(location="station.kcbg").values[0]
        )
        assert mask == 1
