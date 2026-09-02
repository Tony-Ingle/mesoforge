"""Unit tests for mesoforge.forecasting.baseline_v2 and
contracts.forecasts.validate_baseline_forecast_v2 (plan Section 5.1,
Task 10).
"""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.contracts.forecasts import (
    BaselineForecastValidationError,
    validate_baseline_forecast_v2,
)
from mesoforge.forecasting.baseline_v2 import (
    BaselineV2AssemblyError,
    assemble_baseline_forecast_v2,
)


def test_rejects_unknown_availability_state_directly() -> None:
    values, states = _complete_inputs()
    states[("air_temperature_2m", "station.kcbg", 1)] = "mystery"
    with pytest.raises(BaselineV2AssemblyError, match="unknown availability state"):
        assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64("2026-08-30T12:00:00"),
            forecast_issue_time=np.datetime64("2026-08-30T12:05:00"),
            uncorrected_blend_artifact_id="art_00000000-0000-0000-0000-000000000001",
            identity_correction_artifact_id="art_00000000-0000-0000-0000-000000000002",
        )


_LOCATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_HORIZONS = tuple(range(1, 37))
_VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "probability_of_precipitation_1h",
    "liquid_equivalent_precipitation_amount_1h",
)


def _complete_inputs() -> tuple[dict, dict]:
    values = {}
    states = {}
    for variable_id in _VARIABLES:
        for location in _LOCATIONS:
            for horizon in _HORIZONS:
                values[(variable_id, location, horizon)] = 1.0
                states[(variable_id, location, horizon)] = "complete"
    return values, states


class TestAssembleBaselineForecastV2:
    def test_produces_valid_dataset(self) -> None:
        values, states = _complete_inputs()
        dataset = assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64("2026-08-30T12:00:00"),
            forecast_issue_time=np.datetime64("2026-08-30T12:00:00"),
            uncorrected_blend_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            identity_correction_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        assert dataset.attrs["schema_version"] == "baseline-forecast.v2"
        assert dataset.sizes["target_horizon"] == 36
        assert dataset.sizes["location"] == 3
        validate_baseline_forecast_v2(dataset)  # does not raise

    def test_rejects_missing_state(self) -> None:
        values, states = _complete_inputs()
        del states[("air_temperature_2m", "station.kcbg", 1)]
        with pytest.raises(BaselineV2AssemblyError, match="missing"):
            assemble_baseline_forecast_v2(
                values=values,
                states=states,
                target_reference_time=np.datetime64("2026-08-30T12:00:00"),
                forecast_issue_time=np.datetime64("2026-08-30T12:00:00"),
                uncorrected_blend_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
                identity_correction_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
            )

    def test_unavailable_state_permits_missing_value(self) -> None:
        values, states = _complete_inputs()
        del values[("probability_of_precipitation_1h", "station.kcbg", 1)]
        states[("probability_of_precipitation_1h", "station.kcbg", 1)] = "unavailable"
        dataset = assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64("2026-08-30T12:00:00"),
            forecast_issue_time=np.datetime64("2026-08-30T12:00:00"),
            uncorrected_blend_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            identity_correction_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        pop = dataset["probability_of_precipitation_1h"].sel(
            location="station.kcbg", target_horizon=1
        )
        assert np.isnan(pop.values)


class TestValidateBaselineForecastV2:
    def test_rejects_wrong_schema_version(self) -> None:
        values, states = _complete_inputs()
        dataset = assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64("2026-08-30T12:00:00"),
            forecast_issue_time=np.datetime64("2026-08-30T12:00:00"),
            uncorrected_blend_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            identity_correction_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        dataset.attrs["schema_version"] = "baseline-forecast.v1"
        with pytest.raises(BaselineForecastValidationError, match="schema_version"):
            validate_baseline_forecast_v2(dataset)

    def test_v2_dataset_never_passes_as_v1(self) -> None:
        """Section 5.1: v2 must never enter the v1 validator path."""
        from mesoforge.contracts.forecasts import validate_baseline_forecast

        values, states = _complete_inputs()
        dataset = assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64("2026-08-30T12:00:00"),
            forecast_issue_time=np.datetime64("2026-08-30T12:00:00"),
            uncorrected_blend_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
            identity_correction_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",
        )
        with pytest.raises(BaselineForecastValidationError):
            validate_baseline_forecast(dataset)
