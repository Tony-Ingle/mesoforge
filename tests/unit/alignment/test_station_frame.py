"""Unit tests for mesoforge.alignment.station_frame (plan Section
3.1-3.3, Task 6): bilinear extraction + exact temporal matching over
a canonical-guidance.v2-shaped dataset.
"""

from __future__ import annotations

import numpy as np
import pyproj
import pytest

from mesoforge.alignment.station_frame import align_station_to_model
from mesoforge.guidance.canonical_v2 import assemble_canonical_guidance_v2

_CRS = pyproj.CRS.from_epsg(4326)  # plain lat/lon "projection" for test simplicity


def _lat_lon_grid() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    x = np.array([-94.0, -93.0, -92.0])
    y = np.array([45.0, 46.0, 47.0])
    lon, lat = np.meshgrid(x, y)
    return x, y, lat, lon


class TestAlignStationToModelInstantaneous:
    def test_extracts_exact_valid_time(self) -> None:
        x, y, lat, lon = _lat_lon_grid()
        ref_time = np.datetime64("2026-08-30T12:00:00")
        temps = np.stack([np.full(lat.shape, 280.0 + h) for h in (1, 2, 3)])  # one array per lead
        dataset = assemble_canonical_guidance_v2(
            model="hrrr",
            forecast_reference_time=ref_time,
            source_lead_hours=(1, 2, 3),
            x=x,
            y=y,
            lat=lat,
            lon=lon,
            instantaneous_fields={"air_temperature_2m": temps},
            interval_fields={},
            interval_start_hours={},
            grid_id="test-grid.v1",
            configuration_snapshot_id="cfg_sha256_" + "0" * 64,
            variable_lineage_manifest_id="lineage-1",
        )
        results = align_station_to_model(
            dataset,
            crs=_CRS,
            station_latitude=45.8,
            station_longitude=-93.1,
            canonical_variable_id="air_temperature_2m",
            target_horizon_hours=(1, 2, 3),
            target_reference_time=ref_time,
        )
        assert set(results) == {1, 2, 3}
        assert results[1].value == pytest.approx(281.0)
        assert results[1].source_lead_hour == 1

    def test_missing_source_hour_is_absent_from_result(self) -> None:
        x, y, lat, lon = _lat_lon_grid()
        ref_time = np.datetime64("2026-08-30T12:00:00")
        temps = np.stack([np.full(lat.shape, 280.0)])
        dataset = assemble_canonical_guidance_v2(
            model="hrrr",
            forecast_reference_time=ref_time,
            source_lead_hours=(1,),
            x=x,
            y=y,
            lat=lat,
            lon=lon,
            instantaneous_fields={"air_temperature_2m": temps},
            interval_fields={},
            interval_start_hours={},
            grid_id="test-grid.v1",
            configuration_snapshot_id="cfg_sha256_" + "0" * 64,
            variable_lineage_manifest_id="lineage-1",
        )
        results = align_station_to_model(
            dataset,
            crs=_CRS,
            station_latitude=45.8,
            station_longitude=-93.1,
            canonical_variable_id="air_temperature_2m",
            target_horizon_hours=(1, 2),
            target_reference_time=ref_time,
        )
        assert set(results) == {1}


class TestAlignStationToModelInterval:
    def test_extracts_exact_interval_match(self) -> None:
        x, y, lat, lon = _lat_lon_grid()
        ref_time = np.datetime64("2026-08-30T12:00:00")
        qpf = np.stack([np.full(lat.shape, 1.0), np.full(lat.shape, 2.0)])
        dataset = assemble_canonical_guidance_v2(
            model="hrrr",
            forecast_reference_time=ref_time,
            source_lead_hours=(1, 2),
            x=x,
            y=y,
            lat=lat,
            lon=lon,
            instantaneous_fields={},
            interval_fields={"liquid_equivalent_precipitation_amount_1h": qpf},
            interval_start_hours={"liquid_equivalent_precipitation_amount_1h": (0, 1)},
            grid_id="test-grid.v1",
            configuration_snapshot_id="cfg_sha256_" + "0" * 64,
            variable_lineage_manifest_id="lineage-1",
        )
        results = align_station_to_model(
            dataset,
            crs=_CRS,
            station_latitude=45.8,
            station_longitude=-93.1,
            canonical_variable_id="liquid_equivalent_precipitation_amount_1h",
            target_horizon_hours=(1, 2),
            target_reference_time=ref_time,
        )
        assert results[1].value == pytest.approx(1.0)
        assert results[2].value == pytest.approx(2.0)
