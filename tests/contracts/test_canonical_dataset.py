"""Contract tests for mesoforge.contracts.datasets.validate_canonical_dataset
(Task 5, plan Section 4.6).

RED: written before contracts/datasets.py exists.
"""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.catalog.grids import GridDefinition
from mesoforge.contracts.datasets import CanonicalDatasetError, validate_canonical_dataset
from tests.fixtures.synthetic import (
    build_synthetic_dataset,
    build_synthetic_grid,
    build_synthetic_variable_definition,
)


def _validate(dataset, **overrides):
    grid = overrides.pop("grid", build_synthetic_grid())
    # build_synthetic_dataset() always produces air_temperature_2m in its
    # source unit degC; validate against the matching degC variant unless
    # the caller supplies its own variable definition (e.g. to validate a
    # transformed K output).
    variable = overrides.pop(
        "variable", build_synthetic_variable_definition(canonical_unit_id="degC")
    )
    return validate_canonical_dataset(
        dataset, grids={grid.grid_id: grid}, variables={variable.variable_id: variable}
    )


class TestCanonicalDatasetHappyPath:
    def test_valid_synthetic_dataset_passes(self) -> None:
        dataset = build_synthetic_dataset()
        _validate(dataset)

    def test_valid_time_matches_reference_plus_lead(self) -> None:
        dataset = build_synthetic_dataset()
        _validate(dataset)
        for i, lead in enumerate(dataset["lead_time"].values):
            expected = dataset["forecast_reference_time"].values + lead
            assert dataset["valid_time"].values[i] == expected


class TestCanonicalDatasetRejections:
    def test_rejects_missing_schema_version_attr(self) -> None:
        dataset = build_synthetic_dataset()
        del dataset.attrs["schema_version"]
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_wrong_grid_shape(self) -> None:
        dataset = build_synthetic_dataset()
        bad_grid = GridDefinition(
            **{
                **build_synthetic_grid().model_dump(exclude={"schema_version"}),
                "shape_x": 3,
                "x_coordinates": (-100.0, -99.0, -98.0),
            }
        )
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset, grid=bad_grid)

    def test_rejects_non_increasing_lead_time(self) -> None:
        dataset = build_synthetic_dataset()
        dataset = dataset.assign_coords(
            lead_time=("lead_time", np.array([0, 0], dtype="timedelta64[ns]"))
        )
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_negative_lead_time(self) -> None:
        dataset = build_synthetic_dataset()
        dataset = dataset.assign_coords(
            lead_time=(
                "lead_time",
                np.array([-3600_000_000_000, 0], dtype="timedelta64[ns]"),
            )
        )
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_valid_time_invariant_violation(self) -> None:
        dataset = build_synthetic_dataset()
        bad_valid_times = dataset["valid_time"].values.copy()
        bad_valid_times[0] = bad_valid_times[0] + np.timedelta64(1, "s")
        dataset = dataset.assign_coords(valid_time=("lead_time", bad_valid_times))
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_wrong_dtype(self) -> None:
        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"] = dataset["air_temperature_2m"].astype("float64")
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_unknown_quality_mask_bit(self) -> None:
        dataset = build_synthetic_dataset(quality_mask_bits=(8, 0, 0, 0, 0, 0, 0, 0))
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_accepts_all_known_quality_mask_bits(self) -> None:
        dataset = build_synthetic_dataset(quality_mask_bits=(1, 2, 4, 7, 0, 0, 0, 0))
        # missing/invalid values must be non-finite when masked; use nan for
        # bits 1/4 (missing, calculated_invalid) which imply invalidity.
        values = dataset["air_temperature_2m"].values.copy()
        flat_mask = (1, 2, 4, 7, 0, 0, 0, 0)
        flat_values = values.reshape(-1)
        for i, bit in enumerate(flat_mask):
            if bit & 1 or bit & 4:
                flat_values[i] = np.nan
        dataset["air_temperature_2m"].values[...] = flat_values.reshape(values.shape)
        _validate(dataset)

    def test_rejects_non_finite_unmasked_value(self) -> None:
        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"].values[0, 0, 0] = np.nan
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_undeclared_dimension(self) -> None:
        dataset = build_synthetic_dataset()
        dataset = dataset.expand_dims({"extra": [0]})
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_missing_grid_reference(self) -> None:
        dataset = build_synthetic_dataset()
        dataset.attrs["grid_id"] = "unknown-grid.v1"
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)

    def test_rejects_missing_variable_attribute(self) -> None:
        dataset = build_synthetic_dataset()
        del dataset["air_temperature_2m"].attrs["unit_id"]
        with pytest.raises(CanonicalDatasetError):
            _validate(dataset)


class TestCanonicalDatasetScientificMutations:
    """Finding 1 (Codex review t_9bb13e2b): validate_canonical_dataset must
    reject wrong units, semantics, vertical definitions, and dimension
    order -- not just dtype and dataset-level attributes."""

    def test_rejects_wrong_unit_id(self) -> None:
        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"].attrs["unit_id"] = "K"
        with pytest.raises(CanonicalDatasetError, match="unit_id"):
            _validate(dataset)  # variable def declares degC; data claims K

    def test_rejects_wrong_temporal_semantics(self) -> None:
        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"].attrs["temporal_semantics"] = "average"
        with pytest.raises(CanonicalDatasetError, match="temporal_semantics"):
            _validate(dataset)

    def test_rejects_wrong_spatial_support(self) -> None:
        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"].attrs["spatial_support"] = "point"
        with pytest.raises(CanonicalDatasetError, match="spatial_support"):
            _validate(dataset)

    def test_rejects_wrong_vertical_definition_id(self) -> None:
        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"].attrs["vertical_definition_id"] = "surface"
        with pytest.raises(CanonicalDatasetError, match="vertical_definition_id"):
            _validate(dataset)

    def test_rejects_reordered_variable_dimensions(self) -> None:
        dataset = build_synthetic_dataset()
        transposed = dataset.copy()
        transposed["air_temperature_2m"] = dataset["air_temperature_2m"].transpose(
            "y", "lead_time", "x"
        )
        with pytest.raises(CanonicalDatasetError, match="dimension order"):
            _validate(transposed)

    def test_rejects_reordered_quality_mask_dimensions(self) -> None:
        dataset = build_synthetic_dataset()
        transposed = dataset.copy()
        transposed["air_temperature_2m_quality_mask"] = dataset[
            "air_temperature_2m_quality_mask"
        ].transpose("x", "y", "lead_time")
        with pytest.raises(CanonicalDatasetError):
            _validate(transposed)

    def test_rejects_dimension_variant_not_in_allowlist(self) -> None:
        from mesoforge.catalog.variables import VariableDefinition

        dataset = build_synthetic_dataset()
        variable = VariableDefinition(
            variable_id="air_temperature_2m",
            standard_name="air_temperature",
            canonical_unit_id="degC",
            dtype="float32",
            temporal_semantics="instantaneous",
            spatial_support="cell_mean",
            vertical_definition_id="height-agl-2m",
            allowed_dimension_variants=(("lead_time", "location"),),
            missing_value_policy="nan_with_quality_mask",
            interval_required=False,
        )
        with pytest.raises(CanonicalDatasetError, match="dimension order"):
            _validate(dataset, variable=variable)

    def test_missing_value_policy_not_applicable_rejects_masked_missing_bit(self) -> None:
        from mesoforge.catalog.variables import VariableDefinition

        dataset = build_synthetic_dataset(quality_mask_bits=(1, 0, 0, 0, 0, 0, 0, 0))
        values = dataset["air_temperature_2m"].values
        values.reshape(-1)[0] = float("nan")
        variable = VariableDefinition(
            variable_id="air_temperature_2m",
            standard_name="air_temperature",
            canonical_unit_id="degC",
            dtype="float32",
            temporal_semantics="instantaneous",
            spatial_support="cell_mean",
            vertical_definition_id="height-agl-2m",
            allowed_dimension_variants=(("lead_time", "y", "x"),),
            missing_value_policy="not_applicable",
            interval_required=False,
        )
        with pytest.raises(CanonicalDatasetError, match="missing_value_policy"):
            _validate(dataset, variable=variable)

    def test_missing_value_policy_nan_with_quality_mask_rejects_finite_masked_value(self) -> None:
        # Bit 1 (missing) is set but the value was never NaN'd out: the
        # policy says missing must be represented as non-finite.
        dataset = build_synthetic_dataset(quality_mask_bits=(1, 0, 0, 0, 0, 0, 0, 0))
        with pytest.raises(CanonicalDatasetError, match="missing_value_policy"):
            _validate(dataset)

    def test_interval_required_rejects_missing_bounds_variable(self) -> None:
        from mesoforge.catalog.variables import VariableDefinition

        dataset = build_synthetic_dataset()
        dataset["air_temperature_2m"].attrs["temporal_semantics"] = "average"
        variable = VariableDefinition(
            variable_id="air_temperature_2m",
            standard_name="air_temperature",
            canonical_unit_id="degC",
            dtype="float32",
            temporal_semantics="average",
            spatial_support="cell_mean",
            vertical_definition_id="height-agl-2m",
            allowed_dimension_variants=(("lead_time", "y", "x"),),
            missing_value_policy="nan_with_quality_mask",
            interval_required=True,
        )
        with pytest.raises(CanonicalDatasetError, match="interval"):
            _validate(dataset, variable=variable)
