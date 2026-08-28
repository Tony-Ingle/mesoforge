"""Unit tests for mesoforge.guidance.decoding (plan Section 2.1, Task
5). Uses real, deterministic eccodes-generated GRIB2 fixture bytes --
no committed provider GRIB files, no live network."""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.catalog.sources import HrrrFieldAssertion, HrrrSourceSettings, RetryPolicy
from mesoforge.guidance.decoding import HrrrDecodeError, decode_selected_messages
from tests.fixtures.hrrr_grib import NX, NY, make_lead_grib_bytes

pytestmark = pytest.mark.scientific

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=1,
    backoff_seconds=(1.0,),
    retry_after_cap_seconds=60.0,
)

_ASSERTIONS = (
    HrrrFieldAssertion(
        canonical_variable_id="air_temperature_2m",
        inventory_selector=":TMP:2 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=0,
        parameter_number=0,
        type_of_level="heightAboveGround",
        level=2.0,
        expected_unit_id="K",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="eastward_wind_10m",
        inventory_selector=":UGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=2,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="northward_wind_10m",
        inventory_selector=":VGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=3,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
)

_SETTINGS = HrrrSourceSettings(
    forecast_hours=tuple(range(7)),
    file_template="hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2",
    endpoint_order=("aws", "nomads"),
    endpoint_url_templates={
        "aws": "https://aws.example/{FILE}",
        "nomads": "https://nomads.example/{FILE}",
    },
    field_assertions=_ASSERTIONS,
    read_keys=(
        "discipline",
        "parameterCategory",
        "parameterNumber",
        "typeOfLevel",
        "level",
        "stepType",
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
    ),
    retry_policy=_RETRY,
    cycle_availability_deadline_minutes=90.0,
)


def _payload(**overrides: np.ndarray) -> bytes:
    temperature = overrides.get("temperature_k", np.linspace(270.0, 285.0, NX * NY).reshape(NY, NX))
    eastward = overrides.get("eastward_wind_m_s", np.linspace(1.0, 5.0, NX * NY).reshape(NY, NX))
    northward = overrides.get("northward_wind_m_s", np.linspace(-2.0, 2.0, NX * NY).reshape(NY, NX))
    return make_lead_grib_bytes(
        forecast_hour=0,
        temperature_k=temperature,
        eastward_wind_m_s=eastward,
        northward_wind_m_s=northward,
    )


class TestDecodeSelectedMessages:
    def test_decodes_all_three_fields(self) -> None:
        decoded = decode_selected_messages(_payload(), settings=_SETTINGS)
        assert set(decoded) == {"air_temperature_2m", "eastward_wind_10m", "northward_wind_10m"}
        assert decoded["air_temperature_2m"].shape == (NY, NX)

    def test_decoded_temperature_values_match_fixture(self) -> None:
        temperature = np.linspace(270.0, 285.0, NX * NY).reshape(NY, NX)
        decoded = decode_selected_messages(_payload(temperature_k=temperature), settings=_SETTINGS)
        np.testing.assert_allclose(
            decoded["air_temperature_2m"].values, temperature.astype(np.float32), atol=1e-2
        )

    def test_decoded_wind_grib_uv_relative_to_grid_key_present(self) -> None:
        decoded = decode_selected_messages(_payload(), settings=_SETTINGS)
        assert decoded["eastward_wind_10m"].attrs.get("GRIB_uvRelativeToGrid") == 1
        assert decoded["northward_wind_10m"].attrs.get("GRIB_uvRelativeToGrid") == 1

    def test_grid_keys_present_for_projection_reconstruction(self) -> None:
        decoded = decode_selected_messages(_payload(), settings=_SETTINGS)
        temp = decoded["air_temperature_2m"]
        assert temp.attrs.get("GRIB_gridType") == "lambert"
        assert temp.attrs.get("GRIB_Nx") == NX
        assert temp.attrs.get("GRIB_Ny") == NY

    def test_rejects_message_with_wrong_parameter_number(self) -> None:
        # Field assertion expects parameterNumber=0 for temperature, but
        # cfgrib will still decode the fixture normally; simulate a
        # semantic mismatch by asserting against an intentionally wrong
        # settings object.
        wrong_assertions = (
            _ASSERTIONS[0].model_copy(update={"parameter_number": 99}),
            *_ASSERTIONS[1:],
        )
        wrong_settings = _SETTINGS.model_copy(update={"field_assertions": wrong_assertions})
        with pytest.raises(HrrrDecodeError, match="no decoded message matches"):
            decode_selected_messages(_payload(), settings=wrong_settings)
