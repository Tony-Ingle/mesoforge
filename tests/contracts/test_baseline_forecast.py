"""Contract tests for mesoforge.contracts.forecasts.validate_baseline_forecast
(plan Section 3.6, Task 7)."""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.contracts.forecasts import (
    BaselineForecastValidationError,
    validate_baseline_forecast,
)
from mesoforge.forecasting.baseline import assemble_baseline_forecast


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


def _dataset():
    return assemble_baseline_forecast(
        station_values=_complete_station_values(),
        lead_hours=tuple(range(7)),
        forecast_issue_time=_issue_time(),
        hrrr_source_reference_time=_issue_time(),
        contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
        extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
    )


class TestValidateBaselineForecast:
    def test_accepts_valid_baseline(self) -> None:
        validate_baseline_forecast(_dataset())  # does not raise

    def test_rejects_wrong_station_set(self) -> None:
        dataset = _dataset()
        dataset = dataset.assign_coords(location=["station.a", "station.b", "station.c"])
        with pytest.raises(BaselineForecastValidationError, match="location"):
            validate_baseline_forecast(dataset)

    def test_rejects_incomplete_lead_set(self) -> None:
        values = _complete_station_values()
        dataset = assemble_baseline_forecast(
            station_values=values,
            lead_hours=(0, 1, 2),
            forecast_issue_time=_issue_time(),
            hrrr_source_reference_time=_issue_time(),
            contributor_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            extraction_report_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        with pytest.raises(BaselineForecastValidationError, match="lead_time"):
            validate_baseline_forecast(dataset)

    def test_rejects_missing_schema_version(self) -> None:
        dataset = _dataset()
        del dataset.attrs["schema_version"]
        with pytest.raises(BaselineForecastValidationError, match="schema_version"):
            validate_baseline_forecast(dataset)

    def test_rejects_nonfinite_temperature(self) -> None:
        dataset = _dataset()
        dataset["air_temperature_2m"].values[0, 0] = np.nan
        with pytest.raises(BaselineForecastValidationError, match="non-finite"):
            validate_baseline_forecast(dataset)
