"""Unit tests for mesoforge.forecasting.consistency (plan Section 4.6,
Task 8/9): probability/QPF tension flags without value mutation.
"""

from __future__ import annotations

import pytest

from mesoforge.forecasting.consistency import (
    check_pop_bounds,
    check_probability_deterministic_tension,
    check_qpf_nonnegative,
)


class TestCheckProbabilityDeterministicTension:
    def test_pop_zero_qpf_above_threshold_is_flagged(self) -> None:
        tension = check_probability_deterministic_tension(pop_fraction=0.0, qpf_kg_m2=1.0)
        assert tension is not None
        assert tension.kind == "pop_zero_qpf_above_threshold"

    def test_pop_one_qpf_zero_is_flagged(self) -> None:
        tension = check_probability_deterministic_tension(pop_fraction=1.0, qpf_kg_m2=0.0)
        assert tension is not None
        assert tension.kind == "pop_one_qpf_zero"

    def test_no_tension_for_consistent_values(self) -> None:
        assert check_probability_deterministic_tension(pop_fraction=0.5, qpf_kg_m2=0.5) is None

    def test_pop_zero_qpf_below_threshold_no_tension(self) -> None:
        assert check_probability_deterministic_tension(pop_fraction=0.0, qpf_kg_m2=0.1) is None

    def test_does_not_mutate_inputs(self) -> None:
        """The function only returns a flag/None -- values are read-only."""
        pop, qpf = 0.0, 1.0
        check_probability_deterministic_tension(pop_fraction=pop, qpf_kg_m2=qpf)
        assert pop == 0.0
        assert qpf == 1.0


class TestCheckPopBounds:
    def test_accepts_valid_range(self) -> None:
        check_pop_bounds(0.0)
        check_pop_bounds(1.0)
        check_pop_bounds(0.5)

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="0, 1"):
            check_pop_bounds(1.5)

    def test_rejects_nan(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            check_pop_bounds(float("nan"))


class TestCheckQpfNonnegative:
    def test_accepts_zero_and_positive(self) -> None:
        check_qpf_nonnegative(0.0)
        check_qpf_nonnegative(5.5)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="nonnegative"):
            check_qpf_nonnegative(-0.1)
