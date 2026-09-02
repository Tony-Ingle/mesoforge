"""Unit tests for mesoforge.guidance.sources.hrrr_phase2_decoding (plan
Section 2.2, Task 5): synthetic HRRR-like GRIB2 fixtures decoded
through cfgrib for the Phase 2 six-field extended-cycle contract.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from mesoforge.guidance.sources.hrrr_phase2_decoding import (
    HrrrPhase2DecodeError,
    decode_selected_message,
)
from tests.fixtures.hrrr_grib import (
    DX_M,
    DY_M,
    FIRST_LAT_DEGREES,
    FIRST_LON_DEGREES,
    LAD_DEGREES,
    LATIN1_DEGREES,
    LATIN2_DEGREES,
    LOV_DEGREES,
    NX,
    NY,
)
from tests.support.phase2_source_settings import make_hrrr_phase2_settings

_CYCLE_DATE = date(2026, 8, 28)
_CYCLE_HOUR = 18

_SETTINGS = make_hrrr_phase2_settings(
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
        "gridType",
        "Nx",
        "Ny",
        "DxInMetres",
        "DyInMetres",
        "latitudeOfFirstGridPointInDegrees",
        "longitudeOfFirstGridPointInDegrees",
        "LoVInDegrees",
        "Latin1InDegrees",
        "Latin2InDegrees",
        "LaDInDegrees",
        "units",
        "step",
        "dataDate",
        "dataTime",
        "validityDate",
        "validityTime",
    )
)


def _contract(variable_id: str):
    return next(fc for fc in _SETTINGS.field_contracts if fc.canonical_variable_id == variable_id)


def _lambert_message(
    *,
    forecast_hour: int,
    category: int,
    number: int,
    type_of_level: str,
    level: float,
    units_key: str | None,
    step_type: str,
    values: np.ndarray,
    start_step: int | None = None,
    end_step: int | None = None,
    uv_relative_to_grid: int | None = None,
) -> bytes:
    import eccodes

    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    eccodes.codes_set(gid, "centre", "kwbc")
    eccodes.codes_set(gid, "gridType", "lambert")
    eccodes.codes_set(gid, "Nx", NX)
    eccodes.codes_set(gid, "Ny", NY)
    eccodes.codes_set(gid, "DxInMetres", DX_M)
    eccodes.codes_set(gid, "DyInMetres", DY_M)
    eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", FIRST_LAT_DEGREES)
    eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", FIRST_LON_DEGREES)
    eccodes.codes_set(gid, "LoVInDegrees", LOV_DEGREES)
    eccodes.codes_set(gid, "Latin1InDegrees", LATIN1_DEGREES)
    eccodes.codes_set(gid, "Latin2InDegrees", LATIN2_DEGREES)
    eccodes.codes_set(gid, "LaDInDegrees", LAD_DEGREES)
    eccodes.codes_set(gid, "discipline", 0)
    eccodes.codes_set(gid, "dataDate", int(_CYCLE_DATE.strftime("%Y%m%d")))
    eccodes.codes_set(gid, "dataTime", _CYCLE_HOUR * 100)
    eccodes.codes_set(gid, "step", forecast_hour)
    try:
        eccodes.codes_set(gid, "typeOfLevel", type_of_level)
        eccodes.codes_set(gid, "level", level)
        eccodes.codes_set(gid, "parameterCategory", category)
        eccodes.codes_set(gid, "parameterNumber", number)
        eccodes.codes_set(gid, "stepType", step_type)
        if start_step is not None:
            eccodes.codes_set(gid, "startStep", start_step)
        if end_step is not None:
            eccodes.codes_set(gid, "endStep", end_step)
        if uv_relative_to_grid is not None:
            eccodes.codes_set(gid, "uvRelativeToGrid", uv_relative_to_grid)
        eccodes.codes_set_array(gid, "values", values.astype(np.float64).ravel())
        return bytes(eccodes.codes_get_message(gid))
    finally:
        eccodes.codes_release(gid)


class TestDecodeInstantaneous:
    def test_decodes_dew_point(self) -> None:
        values = np.full((NY, NX), 275.0)
        payload = _lambert_message(
            forecast_hour=6,
            category=0,
            number=6,
            type_of_level="heightAboveGround",
            level=2.0,
            units_key="K",
            step_type="instant",
            values=values,
        )
        result = decode_selected_message(
            payload,
            contract=_contract("dew_point_temperature_2m"),
            settings=_SETTINGS,
            forecast_hour=6,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert float(result.values.ravel()[0]) == pytest.approx(275.0)

    def test_rejects_wrong_lead(self) -> None:
        values = np.full((NY, NX), 275.0)
        payload = _lambert_message(
            forecast_hour=6,
            category=0,
            number=6,
            type_of_level="heightAboveGround",
            level=2.0,
            units_key="K",
            step_type="instant",
            values=values,
        )
        with pytest.raises(HrrrPhase2DecodeError, match="lead mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("dew_point_temperature_2m"),
                settings=_SETTINGS,
                forecast_hour=7,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_decodes_wind_with_uv_relative_to_grid(self) -> None:
        values = np.full((NY, NX), 3.0)
        payload = _lambert_message(
            forecast_hour=6,
            category=2,
            number=2,
            type_of_level="heightAboveGround",
            level=10.0,
            units_key="m/s",
            step_type="instant",
            values=values,
            uv_relative_to_grid=1,
        )
        result = decode_selected_message(
            payload,
            contract=_contract("eastward_wind_10m"),
            settings=_SETTINGS,
            forecast_hour=6,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert result.attrs["GRIB_uvRelativeToGrid"] == 1


class TestDecodeApcp:
    def test_decodes_qpf(self) -> None:
        values = np.full((NY, NX), 2.0)
        payload = _lambert_message(
            forecast_hour=6,
            category=1,
            number=8,
            type_of_level="surface",
            level=0.0,
            units_key="kg/m^2",
            step_type="accum",
            values=values,
            start_step=5,
            end_step=6,
        )
        result = decode_selected_message(
            payload,
            contract=_contract("liquid_equivalent_precipitation_amount_1h"),
            settings=_SETTINGS,
            forecast_hour=6,
            cycle_date=_CYCLE_DATE,
            cycle_hour=_CYCLE_HOUR,
        )
        assert float(result.values.ravel()[0]) == pytest.approx(2.0)

    def test_rejects_wrong_window(self) -> None:
        values = np.full((NY, NX), 2.0)
        payload = _lambert_message(
            forecast_hour=6,
            category=1,
            number=8,
            type_of_level="surface",
            level=0.0,
            units_key="kg/m^2",
            step_type="accum",
            values=values,
            start_step=5,
            end_step=6,
        )
        with pytest.raises(HrrrPhase2DecodeError, match="interval mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("liquid_equivalent_precipitation_amount_1h"),
                settings=_SETTINGS,
                forecast_hour=7,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_rejects_wrong_cycle(self) -> None:
        values = np.full((NY, NX), 2.0)
        payload = _lambert_message(
            forecast_hour=6,
            category=1,
            number=8,
            type_of_level="surface",
            level=0.0,
            units_key="kg/m^2",
            step_type="accum",
            values=values,
            start_step=5,
            end_step=6,
        )
        with pytest.raises(HrrrPhase2DecodeError, match="cycle reference date mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("liquid_equivalent_precipitation_amount_1h"),
                settings=_SETTINGS,
                forecast_hour=6,
                cycle_date=date(2026, 8, 29),
                cycle_hour=_CYCLE_HOUR,
            )
