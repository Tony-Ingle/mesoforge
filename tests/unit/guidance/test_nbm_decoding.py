"""Unit tests for mesoforge.guidance.sources.nbm_decoding (plan Section
2.3, Task 3): synthetic NBM-like GRIB2 fixtures decoded through cfgrib.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from mesoforge.guidance.sources.nbm_decoding import NbmDecodeError, decode_selected_message
from tests.fixtures.nbm_grib import (
    NX,
    NY,
    make_apcp_deterministic_message,
    make_confounding_temperature_stddev_message,
    make_instantaneous_message,
    make_pop01_message,
)
from tests.support.phase2_source_settings import make_nbm_settings

_CYCLE_DATE = date(2026, 8, 30)
_CYCLE_HOUR = 12

_SETTINGS = make_nbm_settings(
    read_keys=(
        "discipline",
        "parameterCategory",
        "parameterNumber",
        "typeOfLevel",
        "level",
        "stepType",
        "startStep",
        "endStep",
        "probabilityType",
        "derivedForecast",
        "typeOfStatisticalProcessing",
        "percentileValue",
        "step",
        "dataDate",
        "dataTime",
        "units",
        "gridType",
        "Nx",
        "Ny",
        "Ni",
        "Nj",
        "latitudeOfFirstGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees",
        "validityDate",
        "validityTime",
        "scaledValueOfUpperLimit",
        "scaleFactorOfUpperLimit",
    )
)


def _contract(variable_id: str):
    return next(fc for fc in _SETTINGS.field_contracts if fc.canonical_variable_id == variable_id)


def _decode(payload: bytes, *, contract, forecast_hour: int):
    return decode_selected_message(
        payload,
        contract=contract,
        settings=_SETTINGS,
        forecast_hour=forecast_hour,
        cycle_date=_CYCLE_DATE,
        cycle_hour=_CYCLE_HOUR,
    )


class TestDecodeInstantaneous:
    def test_decodes_temperature(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        result = _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)
        assert result.attrs["GRIB_typeOfLevel"] == "heightAboveGround"
        assert float(result.values.ravel()[0]) == pytest.approx(280.0)

    def test_rejects_wrong_lead(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        with pytest.raises(NbmDecodeError, match="lead mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=7)

    def test_confounding_stddev_record_is_rejected(self) -> None:
        """A decoy record sharing temperature's discipline/category/
        number/typeOfLevel but tagged as an ensemble derived-forecast
        (standard-deviation-shaped) product must never be accepted for
        the deterministic ``air_temperature_2m`` contract, even though
        it matches on discipline/category/number/typeOfLevel alone."""
        decoy = make_confounding_temperature_stddev_message(
            forecast_hour=6, values=np.full((NY, NX), 1.5)
        )
        with pytest.raises(NbmDecodeError, match="ensemble standard-deviation"):
            _decode(decoy, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_level(self) -> None:
        """A mutated contract expecting the wrong level (e.g. 10 m for a
        2 m temperature record) must be rejected, not silently accepted."""
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        mutated = _contract("air_temperature_2m").model_copy(update={"level": 10.0})
        with pytest.raises(NbmDecodeError, match="level mismatch"):
            decode_selected_message(
                payload,
                contract=mutated,
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_unit(self) -> None:
        """A mutated contract expecting the wrong unit (e.g. 'degree' for
        a Kelvin temperature record) must be rejected."""
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        mutated = _contract("air_temperature_2m").model_copy(update={"expected_unit_id": "degree"})
        with pytest.raises(NbmDecodeError, match="units mismatch"):
            decode_selected_message(
                payload,
                contract=mutated,
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_cycle(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        with pytest.raises(NbmDecodeError, match="cycle reference date mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("air_temperature_2m"),
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=date(2026, 8, 31),
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_valid_time(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        with pytest.raises(NbmDecodeError, match="valid date mismatch|valid time mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("air_temperature_2m"),
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=_CYCLE_DATE,
                cycle_hour=6,
            )


class TestDecodeApcpDeterministic:
    def test_decodes_apcp(self) -> None:
        values = np.full((NY, NX), 2.5)
        payload = make_apcp_deterministic_message(forecast_hour=6, values_kg_m2=values)
        result = _decode(
            payload,
            contract=_contract("liquid_equivalent_precipitation_amount_1h"),
            forecast_hour=6,
        )
        assert result.attrs["GRIB_stepType"] == "accum"
        assert float(result.values.ravel()[0]) == pytest.approx(2.5)

    def test_does_not_select_pop01_for_deterministic_contract(self) -> None:
        """Section 2.3: deterministic APCP and PoP01 must never be
        conflated -- decoding the deterministic contract against only a
        probability-typed record must fail (zero matches)."""
        pop_payload = make_pop01_message(forecast_hour=6, values_percent=np.full((NY, NX), 40.0))
        with pytest.raises(NbmDecodeError, match="no decoded"):
            _decode(
                pop_payload,
                contract=_contract("liquid_equivalent_precipitation_amount_1h"),
                forecast_hour=6,
            )

    def test_rejects_wrong_window(self) -> None:
        """A mutated forecast_hour changes the expected start/end
        interval; the actual decoded startStep/endStep must not match
        the wrong window."""
        values = np.full((NY, NX), 2.5)
        payload = make_apcp_deterministic_message(forecast_hour=6, values_kg_m2=values)
        with pytest.raises(NbmDecodeError, match="interval mismatch"):
            _decode(
                payload,
                contract=_contract("liquid_equivalent_precipitation_amount_1h"),
                forecast_hour=7,
            )


class TestDecodePop01:
    def test_decodes_pop01(self) -> None:
        values = np.full((NY, NX), 40.0)
        payload = make_pop01_message(forecast_hour=6, values_percent=values)
        result = _decode(
            payload, contract=_contract("probability_of_precipitation_1h"), forecast_hour=6
        )
        assert float(result.values.ravel()[0]) == pytest.approx(40.0)

    def test_does_not_select_deterministic_apcp_for_pop_contract(self) -> None:
        det_payload = make_apcp_deterministic_message(
            forecast_hour=6, values_kg_m2=np.full((NY, NX), 2.5)
        )
        with pytest.raises(NbmDecodeError, match="no decoded"):
            _decode(
                det_payload, contract=_contract("probability_of_precipitation_1h"), forecast_hour=6
            )

    def test_rejects_wrong_threshold(self) -> None:
        """A mutated settings.probability_threshold_kg_m2 must not match
        the actual decoded 0.254 kg/m^2 scaled threshold."""
        values = np.full((NY, NX), 40.0)
        payload = make_pop01_message(forecast_hour=6, values_percent=values)
        mutated_settings = _SETTINGS.model_copy(update={"probability_threshold_kg_m2": 0.508})
        with pytest.raises(NbmDecodeError, match="PoP01 probability identity mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("probability_of_precipitation_1h"),
                settings=mutated_settings,
                forecast_hour=6,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )
