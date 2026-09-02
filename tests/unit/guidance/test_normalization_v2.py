"""Unit tests for mesoforge.guidance.normalization_v2 (Phase 2
remediation finding 1/5): production per-model per-cycle GRIB2
decoding into canonical-guidance.v2, including HRRR/GFS grid-relative
wind rotation and NBM cornerwise speed/direction-to-U/V conversion.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from mesoforge.guidance.canonical_v2 import validate_canonical_guidance_v2
from mesoforge.guidance.normalization_v2 import (
    GuidanceNormalizationV2Error,
    normalize_gfs_cycle,
    normalize_hrrr_phase2_cycle,
    normalize_nbm_cycle,
)
from tests.fixtures.gfs_grib import NX as GFS_NX
from tests.fixtures.gfs_grib import NY as GFS_NY
from tests.fixtures.gfs_grib import make_apcp_message as make_gfs_apcp_message
from tests.fixtures.gfs_grib import make_instantaneous_message as make_gfs_message
from tests.fixtures.hrrr_grib import NX as HRRR_NX
from tests.fixtures.hrrr_grib import NY as HRRR_NY
from tests.fixtures.hrrr_grib import (
    make_apcp_message as make_hrrr_apcp_message,
)
from tests.fixtures.hrrr_grib import (
    make_dew_point_message,
    make_gust_message,
    make_temperature_message,
    make_wind_message,
)
from tests.fixtures.nbm_grib import NX as NBM_NX
from tests.fixtures.nbm_grib import NY as NBM_NY
from tests.fixtures.nbm_grib import (
    make_apcp_deterministic_message,
    make_pop01_message,
)
from tests.fixtures.nbm_grib import (
    make_instantaneous_message as make_nbm_message,
)
from tests.support.phase2_source_settings import (
    make_gfs_settings,
    make_hrrr_phase2_settings,
    make_nbm_settings,
)

_HRRR_SETTINGS = make_hrrr_phase2_settings(
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

_GFS_SETTINGS = make_gfs_settings(
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

_NBM_SETTINGS = make_nbm_settings()

_CFG_SNAPSHOT_ID = "cfg_sha256_" + "0" * 64
_LINEAGE_ID = "art_00000000-0000-0000-0000-000000000001"


def _hrrr_field_payloads(lead: int, *, grid_relative_wind: bool = True) -> dict:
    return {
        "air_temperature_2m": {
            lead: make_temperature_message(
                forecast_hour=lead, values_k=np.full((HRRR_NY, HRRR_NX), 280.0)
            )
        },
        "dew_point_temperature_2m": {
            lead: make_dew_point_message(
                forecast_hour=lead, values_k=np.full((HRRR_NY, HRRR_NX), 275.0)
            )
        },
        "eastward_wind_10m": {
            lead: make_wind_message(
                forecast_hour=lead,
                component="u",
                values_m_s=np.full((HRRR_NY, HRRR_NX), 3.0),
                grid_relative=grid_relative_wind,
            )
        },
        "northward_wind_10m": {
            lead: make_wind_message(
                forecast_hour=lead,
                component="v",
                values_m_s=np.full((HRRR_NY, HRRR_NX), 2.0),
                grid_relative=grid_relative_wind,
            )
        },
        "wind_gust_10m": {
            lead: make_gust_message(forecast_hour=lead, values_m_s=np.full((HRRR_NY, HRRR_NX), 8.0))
        },
        "liquid_equivalent_precipitation_amount_1h": {
            lead: make_hrrr_apcp_message(
                forecast_hour=lead, values_kg_m2=np.full((HRRR_NY, HRRR_NX), 1.0)
            )
        },
    }


class TestNormalizeHrrrPhase2Cycle:
    def test_assembles_valid_dataset_and_rotates_grid_relative_wind(self) -> None:
        reference = datetime(2026, 8, 28, 18, tzinfo=UTC)
        leads = (5, 6)
        field_payloads: dict = {}
        for lead in leads:
            for variable_id, by_lead in _hrrr_field_payloads(lead).items():
                field_payloads.setdefault(variable_id, {}).update(by_lead)
        dataset = normalize_hrrr_phase2_cycle(
            settings=_HRRR_SETTINGS,
            forecast_reference_time=reference,
            source_lead_hours=leads,
            field_payloads=field_payloads,
            grid_id="fixture-hrrr.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
        )
        validate_canonical_guidance_v2(dataset)
        assert dataset.attrs["model"] == "hrrr"
        assert dataset["eastward_wind_10m"].shape == (2, HRRR_NY, HRRR_NX)

    def test_rejects_disagreeing_uv_relative_to_grid(self) -> None:
        lead = 6
        field_payloads = _hrrr_field_payloads(lead, grid_relative_wind=True)
        field_payloads["northward_wind_10m"] = {
            lead: make_wind_message(
                forecast_hour=lead,
                component="v",
                values_m_s=np.full((HRRR_NY, HRRR_NX), 2.0),
                grid_relative=False,
            )
        }
        with pytest.raises(GuidanceNormalizationV2Error, match="wind rotation failed"):
            normalize_hrrr_phase2_cycle(
                settings=_HRRR_SETTINGS,
                forecast_reference_time=datetime(2026, 8, 28, 18, tzinfo=UTC),
                source_lead_hours=(lead,),
                field_payloads=field_payloads,
                grid_id="fixture-hrrr.v1",
                configuration_snapshot_id=_CFG_SNAPSHOT_ID,
                variable_lineage_manifest_id=_LINEAGE_ID,
            )

    def test_missing_field_payload_raises(self) -> None:
        lead = 6
        field_payloads = _hrrr_field_payloads(lead)
        del field_payloads["wind_gust_10m"]
        with pytest.raises(GuidanceNormalizationV2Error, match="no selected-message payload"):
            normalize_hrrr_phase2_cycle(
                settings=_HRRR_SETTINGS,
                forecast_reference_time=datetime(2026, 8, 28, 18, tzinfo=UTC),
                source_lead_hours=(lead,),
                field_payloads=field_payloads,
                grid_id="fixture-hrrr.v1",
                configuration_snapshot_id=_CFG_SNAPSHOT_ID,
                variable_lineage_manifest_id=_LINEAGE_ID,
            )


def _gfs_field_payloads(lead: int, *, grid_relative_wind: bool = False) -> dict:
    return {
        "air_temperature_2m": {
            lead: make_gfs_message(
                canonical_variable_id="air_temperature_2m",
                forecast_hour=lead,
                values=np.full((GFS_NY, GFS_NX), 280.0),
            )
        },
        "dew_point_temperature_2m": {
            lead: make_gfs_message(
                canonical_variable_id="dew_point_temperature_2m",
                forecast_hour=lead,
                values=np.full((GFS_NY, GFS_NX), 275.0),
            )
        },
        "eastward_wind_10m": {
            lead: make_gfs_message(
                canonical_variable_id="eastward_wind_10m",
                forecast_hour=lead,
                values=np.full((GFS_NY, GFS_NX), 3.0),
                grid_relative_wind=grid_relative_wind,
            )
        },
        "northward_wind_10m": {
            lead: make_gfs_message(
                canonical_variable_id="northward_wind_10m",
                forecast_hour=lead,
                values=np.full((GFS_NY, GFS_NX), 2.0),
                grid_relative_wind=grid_relative_wind,
            )
        },
        "wind_gust_10m": {
            lead: make_gfs_message(
                canonical_variable_id="wind_gust_10m",
                forecast_hour=lead,
                values=np.full((GFS_NY, GFS_NX), 8.0),
            )
        },
        "liquid_equivalent_precipitation_amount_1h": {
            lead: make_gfs_apcp_message(
                start_step=0, end_step=lead, values_kg_m2=np.full((GFS_NY, GFS_NX), 3.0)
            )
        },
    }


class TestNormalizeGfsCycle:
    def test_two_real_equivalent_apcp_payloads_canonicalize(self) -> None:
        lead = 3
        field_payloads = _gfs_field_payloads(lead)
        first = make_gfs_apcp_message(
            start_step=0,
            end_step=lead,
            values_kg_m2=np.full((GFS_NY, GFS_NX), 3.0),
        )
        second = make_gfs_apcp_message(
            start_step=0,
            end_step=lead,
            values_kg_m2=np.full((GFS_NY, GFS_NX), 3.0),
        )
        field_payloads["liquid_equivalent_precipitation_amount_1h"][lead] = (first, second)
        # Non-reset lead 3 also needs its previous bucket dependency.
        field_payloads["liquid_equivalent_precipitation_amount_1h"][2] = make_gfs_apcp_message(
            start_step=0,
            end_step=2,
            values_kg_m2=np.full((GFS_NY, GFS_NX), 2.0),
        )
        dataset, lineage = normalize_gfs_cycle(
            settings=_GFS_SETTINGS,
            forecast_reference_time=datetime(2026, 8, 30, 0, tzinfo=UTC),
            source_lead_hours=(lead,),
            field_payloads=field_payloads,
            grid_id="fixture-gfs.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
        )
        validate_canonical_guidance_v2(dataset)
        assert lineage[0].duplicate is not None
        assert lineage[0].equivalent is True
        assert dataset["liquid_equivalent_precipitation_amount_1h"].values[0, 0, 0] == 1.0

    def test_assembles_valid_dataset_with_dual_parent_equivalence(self) -> None:
        reference = datetime(2026, 8, 30, 0, tzinfo=UTC)
        lead = 1
        dataset, lineage = normalize_gfs_cycle(
            settings=_GFS_SETTINGS,
            forecast_reference_time=reference,
            source_lead_hours=(lead,),
            field_payloads=_gfs_field_payloads(lead),
            grid_id="fixture-gfs.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
        )
        validate_canonical_guidance_v2(dataset)
        assert len(lineage) == 1
        assert lineage[0].result.is_reset_passthrough is True
        assert dataset["eastward_wind_10m"].values[0, 0, 0] == pytest.approx(3.0)
        assert np.all(dataset["x"].values >= -180.0)
        assert np.all(dataset["x"].values < 180.0)
        assert np.all(np.diff(dataset["x"].values) > 0)
        assert np.all(dataset["longitude"].values >= 180.0)

    def test_non_reset_lead_decodes_previous_bucket_for_differencing(self) -> None:
        reference = datetime(2026, 8, 30, 0, tzinfo=UTC)
        field_payloads: dict[str, dict[int, bytes | tuple[bytes, ...]]] = {
            "air_temperature_2m": {},
            "dew_point_temperature_2m": {},
            "eastward_wind_10m": {},
            "northward_wind_10m": {},
            "wind_gust_10m": {},
            "liquid_equivalent_precipitation_amount_1h": {},
        }
        for lead in (1, 2):
            leg = _gfs_field_payloads(lead)
            for key in field_payloads:
                field_payloads[key][lead] = leg[key][lead]
        # lead=2 accumulates 5.0 kg/m^2 total (vs. 3.0 at lead=1); one-hour = 2.0
        field_payloads["liquid_equivalent_precipitation_amount_1h"][2] = make_gfs_apcp_message(
            start_step=0, end_step=2, values_kg_m2=np.full((GFS_NY, GFS_NX), 5.0)
        )
        dataset, lineage = normalize_gfs_cycle(
            settings=_GFS_SETTINGS,
            forecast_reference_time=reference,
            source_lead_hours=(1, 2),
            field_payloads=field_payloads,
            grid_id="fixture-gfs.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
        )
        validate_canonical_guidance_v2(dataset)
        assert len(lineage) == 2
        assert lineage[0].result.is_reset_passthrough is True
        assert lineage[1].result.is_reset_passthrough is False
        assert dataset["liquid_equivalent_precipitation_amount_1h"].values[
            1, 0, 0
        ] == pytest.approx(2.0)


class TestNormalizeNbmCycle:
    def test_assembles_valid_dataset_and_converts_wind_cornerwise(self) -> None:
        reference = datetime(2026, 8, 30, 12, tzinfo=UTC)
        lead = 6
        field_payloads = {
            "air_temperature_2m": {
                lead: make_nbm_message(
                    canonical_variable_id="air_temperature_2m",
                    forecast_hour=lead,
                    values=np.full((NBM_NY, NBM_NX), 280.0),
                )
            },
            "dew_point_temperature_2m": {
                lead: make_nbm_message(
                    canonical_variable_id="dew_point_temperature_2m",
                    forecast_hour=lead,
                    values=np.full((NBM_NY, NBM_NX), 275.0),
                )
            },
            "wind_speed_10m": {
                lead: make_nbm_message(
                    canonical_variable_id="wind_speed_10m",
                    forecast_hour=lead,
                    values=np.full((NBM_NY, NBM_NX), 5.0),
                )
            },
            "wind_from_direction_10m": {
                lead: make_nbm_message(
                    canonical_variable_id="wind_from_direction_10m",
                    forecast_hour=lead,
                    values=np.full((NBM_NY, NBM_NX), 180.0),
                )
            },
            "wind_gust_10m": {
                lead: make_nbm_message(
                    canonical_variable_id="wind_gust_10m",
                    forecast_hour=lead,
                    values=np.full((NBM_NY, NBM_NX), 8.0),
                )
            },
            "liquid_equivalent_precipitation_amount_1h": {
                lead: make_apcp_deterministic_message(
                    forecast_hour=lead, values_kg_m2=np.full((NBM_NY, NBM_NX), 1.0)
                )
            },
            "probability_of_precipitation_1h": {
                lead: make_pop01_message(
                    forecast_hour=lead, values_percent=np.full((NBM_NY, NBM_NX), 40.0)
                )
            },
        }
        dataset = normalize_nbm_cycle(
            settings=_NBM_SETTINGS,
            forecast_reference_time=reference,
            source_lead_hours=(lead,),
            field_payloads=field_payloads,
            grid_id="fixture-nbm.v1",
            configuration_snapshot_id=_CFG_SNAPSHOT_ID,
            variable_lineage_manifest_id=_LINEAGE_ID,
        )
        validate_canonical_guidance_v2(dataset)
        # speed=5, direction=180 -> u=-5*sin(180)=0, v=-5*cos(180)=5
        assert dataset["eastward_wind_10m"].values[0, 0, 0] == pytest.approx(0.0, abs=1e-9)
        assert dataset["northward_wind_10m"].values[0, 0, 0] == pytest.approx(5.0)
        assert dataset["probability_of_precipitation_1h"].values[0, 0, 0] == pytest.approx(0.4)
