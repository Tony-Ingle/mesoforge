"""Synthetic Phase 0 fixture builders: a 2x2 grid, two-lead canonical
dataset used across contract, serializer, and acceptance tests.

This module is Python-generated test scaffolding, not a test module
itself -- no binary NetCDF file is committed (plan Task 5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import xarray as xr

from mesoforge.catalog.grids import GridDefinition
from mesoforge.catalog.units import VerticalDefinition
from mesoforge.catalog.variables import VariableDefinition

SYNTHETIC_GRID_ID = "synthetic-grid.v1"
SYNTHETIC_VARIABLE_ID = "air_temperature_2m"
SYNTHETIC_VERTICAL_ID = "height-agl-2m"
SYNTHETIC_CONFIGURATION_SNAPSHOT_ID = "cfg_sha256_" + "0" * 64
SYNTHETIC_LINEAGE_MANIFEST_ID = "lineage_sha256_" + "0" * 64

_WGS84_WKT2 = (
    'GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",'
    'ELLIPSOID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],'
    'CS[ellipsoidal,2],AXIS["geodetic latitude",north],'
    'AXIS["geodetic longitude",east],ANGLEUNIT["degree",0.0174532925199433],'
    'ID["EPSG",4326]]'
)


def build_synthetic_grid() -> GridDefinition:
    return GridDefinition(
        grid_id=SYNTHETIC_GRID_ID,
        crs_wkt2=_WGS84_WKT2,
        shape_y=2,
        shape_x=2,
        x_coordinates=(-100.0, -99.0),
        y_coordinates=(39.0, 40.0),
        coordinate_reference="cell_center",
        longitude_convention="minus_180_to_180",
        orientation="x_east_y_north",
        spatial_support="cell_mean",
    )


def build_synthetic_vertical_definition() -> VerticalDefinition:
    return VerticalDefinition(
        vertical_definition_id=SYNTHETIC_VERTICAL_ID,
        coordinate_type="height_above_ground",
        value=2.0,
        unit_id="m",
    )


def build_synthetic_variable_definition(*, canonical_unit_id: str = "K") -> VariableDefinition:
    return VariableDefinition(
        variable_id=SYNTHETIC_VARIABLE_ID,
        standard_name="air_temperature",
        canonical_unit_id=canonical_unit_id,
        dtype="float32",
        temporal_semantics="instantaneous",
        spatial_support="cell_mean",
        vertical_definition_id=SYNTHETIC_VERTICAL_ID,
        allowed_dimension_variants=(("lead_time", "y", "x"),),
        missing_value_policy="nan_with_quality_mask",
        interval_required=False,
    )


def build_synthetic_reference_time() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def build_synthetic_lead_times() -> tuple[timedelta, ...]:
    return (timedelta(hours=0), timedelta(hours=1))


def build_synthetic_dataset(
    *,
    values_celsius: tuple[float, float, float, float, float, float, float, float] | None = None,
    quality_mask_bits: tuple[int, ...] | None = None,
    configuration_snapshot_id: str = SYNTHETIC_CONFIGURATION_SNAPSHOT_ID,
    variable_lineage_manifest_id: str = SYNTHETIC_LINEAGE_MANIFEST_ID,
) -> xr.Dataset:
    """Build the canonical Phase 0 synthetic dataset: 2x2 grid, two leads,
    ``air_temperature_2m`` in source unit degC (float32), with a uint16
    quality mask. All values are finite and unmasked by default."""
    grid = build_synthetic_grid()
    reference_time = build_synthetic_reference_time()
    lead_times = build_synthetic_lead_times()
    valid_times = np.array(
        [np.datetime64(reference_time.replace(tzinfo=None) + lead, "ns") for lead in lead_times]
    )

    shape = (len(lead_times), grid.shape_y, grid.shape_x)
    if values_celsius is None:
        values_celsius = (10.0, 11.0, 12.0, 13.0, 20.0, 21.0, 22.0, 23.0)
    data = np.asarray(values_celsius, dtype=np.float32).reshape(shape)

    if quality_mask_bits is None:
        mask = np.zeros(shape, dtype=np.uint16)
    else:
        mask = np.asarray(quality_mask_bits, dtype=np.uint16).reshape(shape)

    dataset = xr.Dataset(
        data_vars={
            "air_temperature_2m": (
                ("lead_time", "y", "x"),
                data,
                {
                    "unit_id": "degC",
                    "temporal_semantics": "instantaneous",
                    "spatial_support": "cell_mean",
                    "vertical_definition_id": SYNTHETIC_VERTICAL_ID,
                    "quality_mask": "air_temperature_2m_quality_mask",
                },
            ),
            "air_temperature_2m_quality_mask": (("lead_time", "y", "x"), mask, {}),
        },
        coords={
            "forecast_reference_time": np.datetime64(reference_time.replace(tzinfo=None), "ns"),
            "lead_time": (
                "lead_time",
                np.array(
                    [np.timedelta64(int(lead.total_seconds()), "s") for lead in lead_times]
                ).astype("timedelta64[ns]"),
            ),
            "valid_time": ("lead_time", valid_times),
            "y": ("y", np.array(grid.y_coordinates)),
            "x": ("x", np.array(grid.x_coordinates)),
        },
        attrs={
            "schema_version": "canonical-guidance.v1",
            "time_encoding": "UTC",
            "grid_id": grid.grid_id,
            "configuration_snapshot_id": configuration_snapshot_id,
            "variable_lineage_manifest_id": variable_lineage_manifest_id,
        },
    )
    return dataset
