"""Unit tests for mesoforge.guidance.sources.nbm_decoding (plan Section
2.3, Task 3): synthetic NBM-like GRIB2 fixtures decoded through cfgrib.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from mesoforge.catalog.sources import NbmSourceSettings
from mesoforge.guidance.sources.nbm_decoding import NbmDecodeError, decode_selected_message
from tests.fixtures.nbm_grib import (
    NX,
    NY,
    make_apcp_deterministic_message,
    make_confounding_temperature_stddev_message,
    make_instantaneous_message,
    make_pop01_message,
    mutate_grid_keys,
)
from tests.support.phase2_source_settings import make_nbm_settings

_CYCLE_DATE = date(2026, 8, 30)
_CYCLE_HOUR = 12

_SETTINGS = make_nbm_settings()


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


class TestApprovedGridContract:
    """Codex re-review finding 2: the approved operational NBM grid
    contract must be enforced exactly. Each probe below mutates exactly
    one independent clause -- projection, shape, increment, scan order,
    or geographic coverage -- and each must be rejected on its own, not
    merely as a side effect of another check.
    """

    def test_accepts_the_exact_approved_grid(self) -> None:
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
        )
        result = _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)
        profile = _SETTINGS.grid_profile
        assert result.attrs["GRIB_gridType"] == "lambert"
        assert result.shape == profile.shape
        assert result.attrs["GRIB_DxInMetres"] == pytest.approx(profile.dx_metres)
        assert result.attrs["GRIB_LoVInDegrees"] == pytest.approx(profile.lov_degrees)
        assert result.attrs["GRIB_jScansPositively"] == profile.j_scans_positively

    def test_rejects_wrong_projection(self) -> None:
        """A geographic (regular_ll) mesh is not the NBM contract at all;
        the previous validator accepted any nonempty gridType."""
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
            grid_keys=mutate_grid_keys(gridType="regular_ll"),
        )
        with pytest.raises(NbmDecodeError, match="grid type mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_shape(self) -> None:
        """A grid one column narrower than the approved profile."""
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX - 1), 280.0),
            grid_keys=mutate_grid_keys(Nx=NX - 1),
        )
        with pytest.raises(NbmDecodeError, match="grid shape mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_increment(self) -> None:
        """Correct projection, shape, and origin, but the wrong grid
        spacing -- a different physical domain entirely."""
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
            grid_keys=mutate_grid_keys(DxInMetres=13000.0, DyInMetres=13000.0),
        )
        with pytest.raises(NbmDecodeError, match="grid increment mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_scan_order(self) -> None:
        """Reversed j scan order means row 0 is the north edge, not the
        south edge; every value would be vertically mirrored."""
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
            grid_keys=mutate_grid_keys(jScansPositively=0),
        )
        with pytest.raises(NbmDecodeError, match="scan flag mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_first_point_coverage(self) -> None:
        """A shifted origin covers a different domain."""
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
            grid_keys=mutate_grid_keys(
                latitudeOfFirstGridPointInDegrees=40.0,
                longitudeOfFirstGridPointInDegrees=260.0,
            ),
        )
        with pytest.raises(NbmDecodeError, match="first grid point mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_projection_parameters(self) -> None:
        """The standard parallels/central meridian define where every
        projected index actually lands on the earth."""
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
            grid_keys=mutate_grid_keys(
                Latin1InDegrees=38.5, Latin2InDegrees=38.5, LaDInDegrees=38.5
            ),
        )
        with pytest.raises(NbmDecodeError, match="projection parameter mismatch"):
            _decode(payload, contract=_contract("air_temperature_2m"), forecast_hour=6)

    def test_rejects_wrong_last_point_coverage_from_declared_geometry(self) -> None:
        """The coverage clause is an independent cross-check, not a
        restatement of the per-key comparisons: a message whose every
        individual grid key matches must still be rejected when the
        geometry those keys imply does not reach the profile's pinned
        far corner.

        ``model_construct`` deliberately bypasses the approved-profile
        registry gate so this proves the *decoder's* coverage clause
        fires on its own. The registry gate refusing the same mutated
        profile at configuration time is proved separately by
        ``test_configuration_rejects_an_unapproved_grid_profile``.
        """
        payload = make_instantaneous_message(
            canonical_variable_id="air_temperature_2m",
            forecast_hour=6,
            values=np.full((NY, NX), 280.0),
        )
        shifted_profile = _SETTINGS.grid_profile.model_copy(
            update={"last_latitude_degrees": _SETTINGS.grid_profile.last_latitude_degrees + 5.0}
        )
        settings = NbmSourceSettings.model_construct(
            **{**_SETTINGS.model_dump(), "grid_profile": shifted_profile}
        )
        with pytest.raises(NbmDecodeError, match="grid coverage mismatch"):
            decode_selected_message(
                payload,
                contract=_contract("air_temperature_2m"),
                settings=settings,
                forecast_hour=6,
                cycle_date=_CYCLE_DATE,
                cycle_hour=_CYCLE_HOUR,
            )

    def test_configuration_rejects_an_unapproved_grid_profile(self) -> None:
        """A settings object may not introduce an unreviewed grid at
        all: the approved-profile registry is the configuration-time
        half of the same fail-closed contract."""
        mutated = _SETTINGS.grid_profile.model_copy(update={"nx": NX + 1})
        with pytest.raises(ValueError, match="does not match the approved profile"):
            make_nbm_settings(grid_profile=mutated)

    def test_configuration_rejects_read_keys_missing_a_grid_key(self) -> None:
        """An unrequested key decodes as absent; the settings model must
        refuse a read-key set that would make the assertion fail open."""
        thinned = tuple(key for key in _SETTINGS.read_keys if key != "LoVInDegrees")
        with pytest.raises(ValueError, match="must request every key"):
            make_nbm_settings(read_keys=thinned)

    def test_every_asserted_grid_key_is_requested_from_eccodes(self) -> None:
        """An unrequested read key decodes as absent, which would make
        the grid assertion silently fail open again."""
        required = (
            "gridType",
            "Nx",
            "Ny",
            "DxInMetres",
            "DyInMetres",
            "LoVInDegrees",
            "LaDInDegrees",
            "Latin1InDegrees",
            "Latin2InDegrees",
            "latitudeOfFirstGridPointInDegrees",
            "longitudeOfFirstGridPointInDegrees",
            "iScansNegatively",
            "jScansPositively",
            "jPointsAreConsecutive",
            "radius",
        )
        assert set(required) <= set(_SETTINGS.read_keys)
