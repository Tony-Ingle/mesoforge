"""Unit tests for mesoforge.forecasting.scalar_blend (plan Section
4.2/4.6, Task 8).
"""

from __future__ import annotations

import pytest

from mesoforge.forecasting.scalar_blend import (
    ConsistencyError,
    Contribution,
    ScalarBlendError,
    blend_scalar,
    check_dew_point_consistency,
)


class TestBlendScalar:
    @pytest.mark.parametrize("weight", [-0.1, float("nan"), float("inf"), float("-inf")])
    def test_rejects_negative_or_non_finite_weight(self, weight: float) -> None:
        with pytest.raises(ScalarBlendError, match="weight"):
            blend_scalar((Contribution(model="HRRR", value=280.0, weight=weight),))

    def test_weighted_mean_of_two_contributors(self) -> None:
        contributions = (
            Contribution(model="HRRR", value=280.0, weight=0.6),
            Contribution(model="NBM", value=290.0, weight=0.4),
        )
        result = blend_scalar(contributions)
        assert result.blended_value == pytest.approx(284.0)

    def test_single_contributor_returns_exact_value(self) -> None:
        contributions = (Contribution(model="HRRR", value=273.15, weight=1.0),)
        result = blend_scalar(contributions)
        assert result.blended_value == pytest.approx(273.15)

    def test_rejects_weights_not_summing_to_one(self) -> None:
        contributions = (
            Contribution(model="HRRR", value=280.0, weight=0.5),
            Contribution(model="NBM", value=290.0, weight=0.4),
        )
        with pytest.raises(ScalarBlendError, match="sum to exactly 1"):
            blend_scalar(contributions)

    def test_rejects_non_finite_value(self) -> None:
        contributions = (Contribution(model="HRRR", value=float("nan"), weight=1.0),)
        with pytest.raises(ScalarBlendError, match="non-finite"):
            blend_scalar(contributions)

    def test_rejects_empty_contributions(self) -> None:
        with pytest.raises(ScalarBlendError, match="at least one"):
            blend_scalar(())

    def test_convex_bound_never_exceeds_max_contributor(self) -> None:
        """Property: a weighted mean is a convex combination -- the
        result must lie within [min(values), max(values)]."""
        contributions = (
            Contribution(model="HRRR", value=270.0, weight=0.5),
            Contribution(model="NBM", value=300.0, weight=0.3),
            Contribution(model="GFS", value=280.0, weight=0.2),
        )
        result = blend_scalar(contributions)
        assert 270.0 <= result.blended_value <= 300.0


class TestCheckDewPointConsistency:
    def test_accepts_dew_point_below_temperature(self) -> None:
        check_dew_point_consistency(temperature_k=290.0, dew_point_k=280.0)  # does not raise

    def test_accepts_within_tolerance(self) -> None:
        check_dew_point_consistency(temperature_k=290.0, dew_point_k=290.0000005)

    def test_rejects_dew_point_exceeding_temperature(self) -> None:
        with pytest.raises(ConsistencyError, match="exceeds blended temperature"):
            check_dew_point_consistency(temperature_k=280.0, dew_point_k=285.0)

    def test_never_clamps(self) -> None:
        """The function only raises or passes -- it never mutates/clamps
        the dew point value (there is nothing to clamp: no return value)."""
        result = check_dew_point_consistency(temperature_k=290.0, dew_point_k=280.0)
        assert result is None
