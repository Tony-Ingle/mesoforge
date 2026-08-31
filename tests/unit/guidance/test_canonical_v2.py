"""Unit tests for mesoforge.guidance.canonical_v2 (plan Section 5.1,
Task 3/4/5): canonical-guidance.v2 assembly and validation.
"""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.guidance.canonical_v2 import (
    CanonicalGuidanceV2Error,
    assemble_canonical_guidance_v2,
    validate_canonical_guidance_v2,
)

_LAT = np.array([[45.0, 45.5], [46.0, 46.5]])
_LON = np.array([[-93.5, -93.0], [-93.5, -93.0]])
_X = np.array([0.0, 1.0])
_Y = np.array([0.0, 1.0])
_REF_TIME = np.datetime64("2026-08-30T12:00:00")


def _base_kwargs(**overrides):
    values = dict(
        model="nbm",
        forecast_reference_time=_REF_TIME,
        source_lead_hours=(1, 2, 3),
        x=_X,
        y=_Y,
        lat=_LAT,
        lon=_LON,
        instantaneous_fields={
            "air_temperature_2m": np.full((3, 2, 2), 280.0),
        },
        interval_fields={
            "liquid_equivalent_precipitation_amount_1h": np.full((3, 2, 2), 1.0),
        },
        interval_start_hours={
            "liquid_equivalent_precipitation_amount_1h": (0, 1, 2),
        },
        grid_id="nbm-conus.v1",
        configuration_snapshot_id="cfg_sha256_" + "0" * 64,
        variable_lineage_manifest_id="lineage-1",
    )
    values.update(overrides)
    return values


class TestAssembleCanonicalGuidanceV2:
    def test_assembles_valid_dataset(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        assert dataset.attrs["schema_version"] == "canonical-guidance.v2"
        assert dataset.attrs["model"] == "nbm"
        assert "air_temperature_2m" in dataset.data_vars
        assert "liquid_equivalent_precipitation_amount_1h_interval_bounds" in dataset.data_vars

    def test_rejects_unknown_instantaneous_variable(self) -> None:
        kwargs = _base_kwargs(instantaneous_fields={"not_a_variable": np.zeros((3, 2, 2))})
        with pytest.raises(CanonicalGuidanceV2Error, match="unknown instantaneous"):
            assemble_canonical_guidance_v2(**kwargs)

    def test_rejects_wrong_shape(self) -> None:
        kwargs = _base_kwargs(instantaneous_fields={"air_temperature_2m": np.zeros((2, 2, 2))})
        with pytest.raises(CanonicalGuidanceV2Error, match="shape"):
            assemble_canonical_guidance_v2(**kwargs)

    def test_requires_interval_start_hours_for_every_lead(self) -> None:
        kwargs = _base_kwargs(
            interval_start_hours={"liquid_equivalent_precipitation_amount_1h": (0, 1)}
        )
        with pytest.raises(CanonicalGuidanceV2Error, match="interval_start_hours"):
            assemble_canonical_guidance_v2(**kwargs)

    def test_interval_bounds_end_equals_valid_time(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        bounds = dataset["liquid_equivalent_precipitation_amount_1h_interval_bounds"].values
        valid_time = dataset["source_valid_time"].values
        assert np.array_equal(bounds[:, 1], valid_time.astype("datetime64[ns]"))


class TestValidateCanonicalGuidanceV2:
    def test_accepts_valid_dataset(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        validate_canonical_guidance_v2(dataset)  # does not raise

    def test_rejects_wrong_schema_version(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset.attrs["schema_version"] = "canonical-guidance.v1"
        with pytest.raises(CanonicalGuidanceV2Error, match="schema_version"):
            validate_canonical_guidance_v2(dataset)

    def test_rejects_unknown_model(self) -> None:
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        dataset.attrs["model"] = "rrfs"
        with pytest.raises(CanonicalGuidanceV2Error, match="model"):
            validate_canonical_guidance_v2(dataset)

    def test_never_accepted_by_v1_validator(self) -> None:
        """Section 5.1: canonical-guidance.v2 must never enter the v1
        validator path -- a v2 dataset's schema_version alone must
        differ enough that any caller routing by exact version cannot
        confuse the two."""
        dataset = assemble_canonical_guidance_v2(**_base_kwargs())
        assert dataset.attrs["schema_version"] != "canonical-guidance.v1"
