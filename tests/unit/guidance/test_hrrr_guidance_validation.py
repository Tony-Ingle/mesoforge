"""Unit tests for mesoforge.guidance.validation (plan Section 3.5/3.6,
Task 5)."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from mesoforge.guidance.normalization import assemble_canonical_hrrr_dataset
from mesoforge.guidance.validation import (
    Phase1GuidanceValidationError,
    validate_phase1_hrrr_guidance,
)


def _dataset(*, lead_hours: tuple[int, ...] = tuple(range(7))) -> xr.Dataset:
    n_lead = len(lead_hours)
    x = np.array([0.0, 3000.0, 6000.0])
    y = np.array([0.0, 3000.0, 6000.0])
    lat = np.full((3, 3), 44.5)
    lon = np.full((3, 3), -93.0)
    return assemble_canonical_hrrr_dataset(
        forecast_reference_time=np.datetime64("2026-08-28T18:00:00", "ns"),
        lead_hours=lead_hours,
        x=x,
        y=y,
        lat=lat,
        lon=lon,
        temperature_k=np.full((n_lead, 3, 3), 280.0),
        eastward_wind_m_s=np.full((n_lead, 3, 3), 1.0),
        northward_wind_m_s=np.full((n_lead, 3, 3), 2.0),
        grid_id="hrrr-conus-grasston-subset.v1",
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        variable_lineage_manifest_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",
    )


class TestValidatePhase1HrrrGuidance:
    def test_accepts_complete_dataset(self) -> None:
        validate_phase1_hrrr_guidance(_dataset())  # does not raise

    def test_rejects_incomplete_lead_axis(self) -> None:
        with pytest.raises(Phase1GuidanceValidationError, match="lead_time"):
            validate_phase1_hrrr_guidance(_dataset(lead_hours=(0, 1, 2)))

    def test_rejects_missing_data_variable(self) -> None:
        dataset = _dataset()
        dataset = dataset.drop_vars("air_temperature_2m")
        with pytest.raises(Phase1GuidanceValidationError, match="missing required data variable"):
            validate_phase1_hrrr_guidance(dataset)

    def test_rejects_nonfinite_values(self) -> None:
        dataset = _dataset()
        values = dataset["air_temperature_2m"].values.copy()
        values[0, 0, 0] = np.nan
        dataset["air_temperature_2m"].values[:] = values
        with pytest.raises(Phase1GuidanceValidationError, match="non-finite"):
            validate_phase1_hrrr_guidance(dataset)

    def test_rejects_nonzero_quality_mask(self) -> None:
        dataset = _dataset()
        dataset["air_temperature_2m_quality_mask"].values[0, 0, 0] = 1
        with pytest.raises(Phase1GuidanceValidationError, match="quality mask"):
            validate_phase1_hrrr_guidance(dataset)
