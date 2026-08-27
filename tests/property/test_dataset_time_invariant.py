"""Property tests for the canonical dataset's valid-time invariant
(Task 5, plan Section 4.6).

RED: written before contracts/datasets.py exists.
"""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.contracts.datasets import CanonicalDatasetError, validate_canonical_dataset
from tests.fixtures.synthetic import (
    build_synthetic_dataset,
    build_synthetic_grid,
    build_synthetic_variable_definition,
)

_grid = build_synthetic_grid()
_variable = build_synthetic_variable_definition(canonical_unit_id="degC")


@given(offset_ns=st.integers(min_value=1, max_value=10**9))
@settings(max_examples=50)
def test_any_nonzero_valid_time_perturbation_is_rejected(offset_ns: int) -> None:
    dataset = build_synthetic_dataset()
    valid_times = dataset["valid_time"].values.copy()
    valid_times[0] = valid_times[0] + np.timedelta64(offset_ns, "ns")
    dataset = dataset.assign_coords(valid_time=("lead_time", valid_times))

    try:
        validate_canonical_dataset(
            dataset, grids={_grid.grid_id: _grid}, variables={_variable.variable_id: _variable}
        )
    except CanonicalDatasetError:
        return
    raise AssertionError("expected CanonicalDatasetError for perturbed valid_time")


def test_exact_valid_time_always_passes() -> None:
    dataset = build_synthetic_dataset()
    validate_canonical_dataset(
        dataset, grids={_grid.grid_id: _grid}, variables={_variable.variable_id: _variable}
    )
