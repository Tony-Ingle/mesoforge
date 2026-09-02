"""Unit tests for mesoforge.guidance.sources.gfs_decoding (plan Section
2.4, Task 4): synthetic GFS-like GRIB2 fixtures decoded through cfgrib.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from mesoforge.guidance.sources.gfs_decoding import (
    GfsDecodeError,
    decode_apcp_candidates,
    decode_instantaneous_message,
)
from tests.fixtures.gfs_grib import NX, NY, make_apcp_message, make_instantaneous_message
from tests.support.phase2_source_settings import make_gfs_settings

_CYCLE_DATE = date(2026, 8, 30)
_CYCLE_HOUR = 0

_SETTINGS = make_gfs_settings(
    read_keys=(
        "discipline",
        "parameterCategory",
        "parameterNumber",
        "typeOfLevel",
        "level",
        "stepType",
        "startStep",
        "endStep",
        "uvRelativeToGrid",
        "step",
        "dataDate",
        "dataTime",
        "units",
        "gridType",
        "Ni",
        "Nj",
        "iDirectionIncrementInDegrees",
        "jDirectionIncrementInDegrees",
        "latitudeOfFirstGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees",
        "validityDate",
        "validityTime",
    )
)


def _contract(variable_id: str):
    return next(fc for fc in _SETTINGS.field_contracts if fc.canonical_variable_id == variable_id)


class TestDecodeInstantaneous:
    def test_decodes_temperature(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=values,
            cycle_date="20260830",
            cycle_hour=0,
        )
        result = decode_instantaneous_message(
            payload,
            contract=_contract("air_temperature_2m"),
            settings=_SETTINGS,
            forecast_hour=6,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert float(result.values.ravel()[0]) == pytest.approx(280.0)

    def test_rejects_wrong_lead(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        with pytest.raises(GfsDecodeError, match="lead mismatch"):
            decode_instantaneous_message(
                payload,
                contract=_contract("air_temperature_2m"),
                settings=_SETTINGS,
                forecast_hour=7,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_unit(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        mutated = _contract("air_temperature_2m").model_copy(update={"expected_unit_id": "m/s"})
        with pytest.raises(GfsDecodeError, match="units mismatch"):
            decode_instantaneous_message(
                payload,
                contract=mutated,
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_level(self) -> None:
        values = np.full((NY, NX), 280.0)
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m", forecast_hour=6, values=values
        )
        mutated = _contract("air_temperature_2m").model_copy(update={"level": 10.0})
        with pytest.raises(GfsDecodeError, match="level mismatch"):
            decode_instantaneous_message(
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
        with pytest.raises(GfsDecodeError, match="cycle reference date mismatch"):
            decode_instantaneous_message(
                payload,
                contract=_contract("air_temperature_2m"),
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=date(2026, 8, 31),
                cycle_hour=_CYCLE_HOUR,
            )

    def test_decodes_wind_with_uv_relative_to_grid(self) -> None:
        values = np.full((NY, NX), 3.0)
        payload = make_instantaneous_message(
            canonical_variable_id="eastward_wind_10m",
            forecast_hour=6,
            values=values,
            grid_relative_wind=True,
        )
        result = decode_instantaneous_message(
            payload,
            contract=_contract("eastward_wind_10m"),
            settings=_SETTINGS,
            forecast_hour=6,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert result.attrs["GRIB_uvRelativeToGrid"] == 1


class TestDecodeApcpCandidates:
    def test_decodes_single_bucket_record(self) -> None:
        values = np.full((NY, NX), 2.5)
        payload = make_apcp_message(start_step=0, end_step=6, values_kg_m2=values)
        records = decode_apcp_candidates(
            payload,
            contract=_contract("liquid_equivalent_precipitation_amount_1h"),
            settings=_SETTINGS,
            forecast_hour=6,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert len(records) == 1
        assert records[0].start_step == 0
        assert records[0].end_step == 6
        assert records[0].values_kg_m2[0] == pytest.approx(2.5)

    def test_rejects_wrong_window(self) -> None:
        values = np.full((NY, NX), 2.5)
        payload = make_apcp_message(start_step=0, end_step=6, values_kg_m2=values)
        with pytest.raises(GfsDecodeError, match="accumulation window end mismatch"):
            decode_apcp_candidates(
                payload,
                contract=_contract("liquid_equivalent_precipitation_amount_1h"),
                settings=_SETTINGS,
                forecast_hour=7,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_grid(self) -> None:
        values = np.full((NY, NX), 2.5)
        payload = make_apcp_message(start_step=0, end_step=6, values_kg_m2=values)
        with pytest.raises(GfsDecodeError, match="cycle reference date mismatch"):
            decode_apcp_candidates(
                payload,
                contract=_contract("liquid_equivalent_precipitation_amount_1h"),
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=date(2026, 9, 1),
                cycle_hour=_CYCLE_HOUR,
            )

    def test_two_duplicate_records_validate_as_equivalent(self) -> None:
        from mesoforge.guidance.precipitation import validate_dual_parent_equivalence

        values = np.full((NY, NX), 2.5)
        bucket_payload = make_apcp_message(start_step=0, end_step=3, values_kg_m2=values)
        continuous_payload = make_apcp_message(start_step=0, end_step=3, values_kg_m2=values)
        records = decode_apcp_candidates(
            (bucket_payload, continuous_payload),
            contract=_contract("liquid_equivalent_precipitation_amount_1h"),
            settings=_SETTINGS,
            forecast_hour=3,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert len(records) == 2
        assert validate_dual_parent_equivalence(records[0], records[1]) is True
