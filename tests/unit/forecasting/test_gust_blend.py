"""Unit tests for mesoforge.forecasting.gust_blend (plan Section 4.3,
Task 9).
"""

from __future__ import annotations

import pytest

from mesoforge.forecasting.gust_blend import (
    GustDisqualificationError,
    GustInvariantError,
    blend_gust,
    validate_source_gust,
)
from mesoforge.forecasting.scalar_blend import Contribution


class TestValidateSourceGust:
    def test_gust_at_or_above_sustained_speed_passes_unchanged(self) -> None:
        result = validate_source_gust(gust_m_s=15.0, sustained_speed_m_s=10.0)
        assert result.validated_gust_m_s == pytest.approx(15.0)
        assert result.source_gust_floor_applied is False

    def test_small_shortfall_is_floored(self) -> None:
        result = validate_source_gust(gust_m_s=9.95, sustained_speed_m_s=10.0)
        assert result.validated_gust_m_s == pytest.approx(10.0)
        assert result.source_gust_floor_applied is True

    def test_shortfall_exactly_at_tolerance_is_floored(self) -> None:
        result = validate_source_gust(gust_m_s=9.9, sustained_speed_m_s=10.0)
        assert result.source_gust_floor_applied is True

    def test_material_shortfall_disqualifies_model(self) -> None:
        with pytest.raises(GustDisqualificationError, match="disqualified"):
            validate_source_gust(gust_m_s=8.0, sustained_speed_m_s=10.0)


class TestBlendGust:
    def test_weighted_mean_above_sustained_speed(self) -> None:
        contributions = (
            Contribution(model="HRRR", value=15.0, weight=0.5),
            Contribution(model="NBM", value=17.0, weight=0.5),
        )
        result = blend_gust(contributions=contributions, blended_sustained_speed_m_s=10.0)
        assert result.blended_gust_m_s == pytest.approx(16.0)
        assert result.final_gust_epsilon_floor_applied is False

    def test_epsilon_shortfall_is_floored(self) -> None:
        contributions = (Contribution(model="HRRR", value=9.9999995, weight=1.0),)
        result = blend_gust(contributions=contributions, blended_sustained_speed_m_s=10.0)
        assert result.blended_gust_m_s == pytest.approx(10.0)
        assert result.final_gust_epsilon_floor_applied is True

    def test_material_shortfall_raises_invariant_error(self) -> None:
        contributions = (Contribution(model="HRRR", value=8.0, weight=1.0),)
        with pytest.raises(GustInvariantError, match="convexity invariant"):
            blend_gust(contributions=contributions, blended_sustained_speed_m_s=10.0)

    def test_rejects_result_above_valid_max(self) -> None:
        contributions = (Contribution(model="HRRR", value=150.0, weight=1.0),)
        with pytest.raises(GustInvariantError, match="valid bound"):
            blend_gust(contributions=contributions, blended_sustained_speed_m_s=100.0)

    def test_never_clips_silently(self) -> None:
        """An out-of-bound result raises rather than being silently
        clipped to the valid range."""
        contributions = (Contribution(model="HRRR", value=-5.0, weight=1.0),)
        with pytest.raises(GustInvariantError):
            blend_gust(contributions=contributions, blended_sustained_speed_m_s=0.0)
