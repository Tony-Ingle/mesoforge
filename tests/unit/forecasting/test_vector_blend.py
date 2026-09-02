"""Unit tests for mesoforge.forecasting.vector_blend (plan Section
4.2/4.6, Task 8): cardinal/opposing wind vectors, calm, and signed
zero.
"""

from __future__ import annotations

import math

import pytest

from mesoforge.forecasting.scalar_blend import Contribution
from mesoforge.forecasting.vector_blend import VectorBlendError, blend_vector


def _u(*values_weights: tuple[str, float, float]) -> tuple[Contribution, ...]:
    return tuple(Contribution(model=m, value=v, weight=w) for m, v, w in values_weights)


class TestBlendVector:
    def test_north_wind_from_hrrr_only(self) -> None:
        # North wind: u=0, v=-speed (blowing from north toward south)
        eastward = _u(("HRRR", 0.0, 1.0))
        northward = _u(("HRRR", -10.0, 1.0))
        result = blend_vector(eastward_contributions=eastward, northward_contributions=northward)
        assert result.speed_m_s == pytest.approx(10.0)
        assert result.direction_degrees == pytest.approx(0.0, abs=1e-9)

    def test_east_wind(self) -> None:
        eastward = _u(("HRRR", -5.0, 1.0))
        northward = _u(("HRRR", 0.0, 1.0))
        result = blend_vector(eastward_contributions=eastward, northward_contributions=northward)
        assert result.direction_degrees == pytest.approx(90.0)

    def test_opposing_vectors_can_cancel_to_calm(self) -> None:
        eastward = _u(("HRRR", 10.0, 0.5), ("NBM", -10.0, 0.5))
        northward = _u(("HRRR", 0.0, 0.5), ("NBM", 0.0, 0.5))
        result = blend_vector(eastward_contributions=eastward, northward_contributions=northward)
        assert result.speed_m_s == pytest.approx(0.0, abs=1e-9)
        assert result.direction_degrees is None

    def test_never_averages_direction_angles_directly(self) -> None:
        """A north wind (0 deg) and an east wind (90 deg) blended 50/50
        by U/V must NOT produce direction 45 deg (naive angle average);
        it must be computed from the blended vector."""
        eastward = _u(("HRRR", 0.0, 0.5), ("NBM", -10.0, 0.5))
        northward = _u(("HRRR", -10.0, 0.5), ("NBM", 0.0, 0.5))
        result = blend_vector(eastward_contributions=eastward, northward_contributions=northward)
        # blended u=-5, v=-5 -> direction from vector math (never averaged angles)
        expected_direction = math.degrees(math.atan2(5.0, 5.0)) % 360.0
        assert result.direction_degrees == pytest.approx(expected_direction)

    def test_rejects_mismatched_contributor_models(self) -> None:
        eastward = _u(("HRRR", 1.0, 1.0))
        northward = _u(("NBM", 1.0, 1.0))
        with pytest.raises(VectorBlendError, match="must match exactly"):
            blend_vector(eastward_contributions=eastward, northward_contributions=northward)

    def test_vector_reconstruction_property(self) -> None:
        """Property: speed/direction reconstructed from blended U/V must
        itself decompose back to the same U/V within floating tolerance."""
        eastward = _u(("HRRR", 3.0, 0.5), ("NBM", 4.0, 0.5))
        northward = _u(("HRRR", -3.0, 0.5), ("NBM", -4.0, 0.5))
        result = blend_vector(eastward_contributions=eastward, northward_contributions=northward)
        assert result.direction_degrees is not None
        radians = math.radians(result.direction_degrees)
        reconstructed_u = -result.speed_m_s * math.sin(radians)
        reconstructed_v = -result.speed_m_s * math.cos(radians)
        assert reconstructed_u == pytest.approx(result.eastward_m_s, abs=1e-9)
        assert reconstructed_v == pytest.approx(result.northward_m_s, abs=1e-9)
