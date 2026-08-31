"""Shared Phase 2 source-settings factories for unit/property tests
(plan Section 1.2/2). Keeps every test file from re-declaring the full
retry policy / field contract boilerplate for HRRR/NBM/GFS.
"""

from __future__ import annotations

from mesoforge.catalog.sources import (
    GfsSourceSettings,
    HrrrPhase2SourceSettings,
    NbmSourceSettings,
    Phase2FieldContract,
    RetryPolicy,
)

RETRY_POLICY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=3,
    backoff_seconds=(1.0, 2.0, 4.0),
    retry_after_cap_seconds=60.0,
)

_HRRR_FIELD_IDS = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "liquid_equivalent_precipitation_amount_1h",
)

# Real per-variable GRIB identity, matching configs/phase2-grasston.yaml,
# used so decode-layer tests (which assert exact discipline/category/
# number/typeOfLevel routing) exercise realistic contracts rather than
# a placeholder shared shape.
_HRRR_FIELD_SPECS: dict[str, dict[str, object]] = {
    "air_temperature_2m": {
        "discipline": 0,
        "parameter_category": 0,
        "parameter_number": 0,
        "type_of_level": "heightAboveGround",
        "level": 2.0,
        "expected_unit_id": "K",
    },
    "dew_point_temperature_2m": {
        "discipline": 0,
        "parameter_category": 0,
        "parameter_number": 6,
        "type_of_level": "heightAboveGround",
        "level": 2.0,
        "expected_unit_id": "K",
    },
    "eastward_wind_10m": {
        "discipline": 0,
        "parameter_category": 2,
        "parameter_number": 2,
        "type_of_level": "heightAboveGround",
        "level": 10.0,
        "expected_unit_id": "m/s",
    },
    "northward_wind_10m": {
        "discipline": 0,
        "parameter_category": 2,
        "parameter_number": 3,
        "type_of_level": "heightAboveGround",
        "level": 10.0,
        "expected_unit_id": "m/s",
    },
    "wind_gust_10m": {
        "discipline": 0,
        "parameter_category": 2,
        "parameter_number": 22,
        "type_of_level": "surface",
        "level": 0.0,
        "expected_unit_id": "m/s",
    },
    "liquid_equivalent_precipitation_amount_1h": {
        "discipline": 0,
        "parameter_category": 1,
        "parameter_number": 8,
        "type_of_level": "surface",
        "level": 0.0,
        "expected_unit_id": "kg/m^2",
        "is_accumulation": True,
    },
}

_NBM_FIELD_IDS = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "wind_speed_10m",
    "wind_from_direction_10m",
    "wind_gust_10m",
    "liquid_equivalent_precipitation_amount_1h",
    "probability_of_precipitation_1h",
)

_NBM_FIELD_SPECS: dict[str, dict[str, object]] = {
    "air_temperature_2m": {
        "discipline": 0,
        "parameter_category": 0,
        "parameter_number": 0,
        "type_of_level": "heightAboveGround",
        "level": 2.0,
        "expected_unit_id": "K",
    },
    "dew_point_temperature_2m": {
        "discipline": 0,
        "parameter_category": 0,
        "parameter_number": 6,
        "type_of_level": "heightAboveGround",
        "level": 2.0,
        "expected_unit_id": "K",
    },
    "wind_speed_10m": {
        "discipline": 0,
        "parameter_category": 2,
        "parameter_number": 1,
        "type_of_level": "heightAboveGround",
        "level": 10.0,
        "expected_unit_id": "m/s",
    },
    "wind_from_direction_10m": {
        "discipline": 0,
        "parameter_category": 2,
        "parameter_number": 0,
        "type_of_level": "heightAboveGround",
        "level": 10.0,
        "expected_unit_id": "degree",
    },
    "wind_gust_10m": {
        "discipline": 0,
        "parameter_category": 2,
        "parameter_number": 22,
        "type_of_level": "heightAboveGround",
        "level": 10.0,
        "expected_unit_id": "m/s",
    },
    "liquid_equivalent_precipitation_amount_1h": {
        "discipline": 0,
        "parameter_category": 1,
        "parameter_number": 8,
        "type_of_level": "surface",
        "level": 0.0,
        "expected_unit_id": "kg/m^2",
        "is_accumulation": True,
    },
    "probability_of_precipitation_1h": {
        "discipline": 0,
        "parameter_category": 1,
        "parameter_number": 8,
        "type_of_level": "surface",
        "level": 0.0,
        "expected_unit_id": "percent",
        "is_accumulation": True,
        "is_probability": True,
    },
}


def _field_contract(
    variable_id: str, *, specs: dict[str, dict[str, object]]
) -> Phase2FieldContract:
    spec = specs[variable_id]
    return Phase2FieldContract(
        canonical_variable_id=variable_id,
        discipline=spec["discipline"],
        parameter_category=spec["parameter_category"],
        parameter_number=spec["parameter_number"],
        type_of_level=spec["type_of_level"],
        level=spec["level"],
        expected_unit_id=spec["expected_unit_id"],
        is_accumulation=bool(spec.get("is_accumulation", False)),
        is_probability=bool(spec.get("is_probability", False)),
    )


def make_hrrr_phase2_settings(**overrides: object) -> HrrrPhase2SourceSettings:
    values: dict[str, object] = {
        "file_template": "hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2",
        "endpoint_order": ("aws", "nomads"),
        "endpoint_url_templates": {
            "aws": "https://aws.example/hrrr.{YYYYMMDD}/conus/{FILE}",
            "nomads": "https://nomads.example/hrrr.{YYYYMMDD}/conus/{FILE}",
        },
        "field_contracts": tuple(
            _field_contract(v, specs=_HRRR_FIELD_SPECS) for v in _HRRR_FIELD_IDS
        ),
        "read_keys": ("discipline",),
        "retry_policy": RETRY_POLICY,
        "allowed_cycle_hours": (0, 6, 12, 18),
        "max_age_hours": 6.0,
        "cycle_completion_deadline_minutes": 120.0,
        "max_source_lead_hours": 48,
    }
    values.update(overrides)
    return HrrrPhase2SourceSettings(**values)  # type: ignore[arg-type]


def make_nbm_settings(**overrides: object) -> NbmSourceSettings:
    values: dict[str, object] = {
        "file_template": "blend.t{HH:02d}z.core.f{FFF:03d}.co.grib2",
        "endpoint_order": ("noaa_s3", "nomads"),
        "endpoint_url_templates": {
            "noaa_s3": "https://noaa-nbm.example/blend.{YYYYMMDD}/{HH:02d}/core/{FILE}",
            "nomads": "https://nomads.example/blend.{YYYYMMDD}/{HH:02d}/core/{FILE}",
        },
        "field_contracts": tuple(
            _field_contract(v, specs=_NBM_FIELD_SPECS) for v in _NBM_FIELD_IDS
        ),
        "read_keys": ("discipline",),
        "retry_policy": RETRY_POLICY,
        "max_age_hours": 3.0,
        "cycle_completion_deadline_minutes": 90.0,
        "probability_threshold_kg_m2": 0.254,
    }
    values.update(overrides)
    return NbmSourceSettings(**values)  # type: ignore[arg-type]


def make_gfs_settings(**overrides: object) -> GfsSourceSettings:
    values: dict[str, object] = {
        "file_template": "gfs.t{HH:02d}z.pgrb2.0p25.f{FFF:03d}",
        "endpoint_order": ("gcs_archive", "aws_archive", "nomads"),
        "endpoint_url_templates": {
            "gcs_archive": "https://gcs.example/gfs.{YYYYMMDD}/{HH:02d}/atmos/{FILE}",
            "aws_archive": "https://aws.example/gfs.{YYYYMMDD}/{HH:02d}/atmos/{FILE}",
            "nomads": "https://nomads.example/gfs.{YYYYMMDD}/{HH:02d}/atmos/{FILE}",
        },
        "field_contracts": tuple(
            _field_contract(v, specs=_HRRR_FIELD_SPECS) for v in _HRRR_FIELD_IDS
        ),
        "read_keys": ("discipline",),
        "retry_policy": RETRY_POLICY,
        "allowed_cycle_hours": (0, 6, 12, 18),
        "max_age_hours": 12.0,
        "cycle_completion_deadline_minutes": 360.0,
        "max_source_lead_hours": 48,
    }
    values.update(overrides)
    return GfsSourceSettings(**values)  # type: ignore[arg-type]
