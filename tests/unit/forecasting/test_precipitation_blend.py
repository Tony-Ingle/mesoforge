"""Unit tests for mesoforge.forecasting.precipitation_blend (plan
Section 4.4, Task 9): every precipitation fallback row and exact
interval equality (interval equality is enforced upstream by exact
temporal alignment; this module tests the arithmetic contract).
"""

from __future__ import annotations

import pytest

from mesoforge.forecasting.precipitation_blend import QpfBlendError, blend_qpf
from mesoforge.forecasting.scalar_blend import Contribution


class TestBlendQpf:
    def test_weighted_mean(self) -> None:
        contributions = (
            Contribution(model="HRRR", value=2.0, weight=0.45),
            Contribution(model="NBM", value=1.0, weight=0.40),
            Contribution(model="GFS", value=0.5, weight=0.15),
        )
        result = blend_qpf(contributions)
        expected = 2.0 * 0.45 + 1.0 * 0.40 + 0.5 * 0.15
        assert result == pytest.approx(expected)

    def test_single_contributor(self) -> None:
        contributions = (Contribution(model="HRRR", value=3.5, weight=1.0),)
        assert blend_qpf(contributions) == pytest.approx(3.5)

    def test_zero_is_valid(self) -> None:
        contributions = (Contribution(model="HRRR", value=0.0, weight=1.0),)
        assert blend_qpf(contributions) == 0.0

    def test_rejects_negative_contribution(self) -> None:
        contributions = (Contribution(model="HRRR", value=-1.0, weight=1.0),)
        with pytest.raises(QpfBlendError, match="nonnegative"):
            blend_qpf(contributions)

    def test_rejects_non_finite_contribution(self) -> None:
        contributions = (Contribution(model="HRRR", value=float("inf"), weight=1.0),)
        with pytest.raises(QpfBlendError, match="finite"):
            blend_qpf(contributions)

    def test_never_clips_except_upstream_tolerance(self) -> None:
        """This module performs no clipping of its own -- a negative
        input is always rejected, never silently zeroed (that floor
        happens only in guidance.precipitation's bounded GFS
        differencing tolerance, upstream of this function)."""
        contributions = (Contribution(model="HRRR", value=-0.0000001, weight=1.0),)
        with pytest.raises(QpfBlendError):
            blend_qpf(contributions)
