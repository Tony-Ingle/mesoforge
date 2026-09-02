"""Deterministic vector (wind) blend operator (plan Section 4.2/4.6,
Task 8): weighted-mean earth-relative U/V, then derive speed/direction
from the blended vector -- never average direction angles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar

_WEIGHT_SUM_TOLERANCE = 1e-12


class VectorBlendError(MesoForgeError):
    """Raised when vector blend inputs are inconsistent (mismatched
    contributor models between U and V, or non-finite components)."""


@dataclass(frozen=True, slots=True)
class VectorBlendResult:
    eastward_m_s: float
    northward_m_s: float
    speed_m_s: float
    direction_degrees: float | None  # None (undefined) at exactly zero speed
    eastward_contributions: tuple[Contribution, ...]
    northward_contributions: tuple[Contribution, ...]


def blend_vector(
    *,
    eastward_contributions: tuple[Contribution, ...],
    northward_contributions: tuple[Contribution, ...],
) -> VectorBlendResult:
    """Section 4.2: blend U and V independently as weighted means, then
    derive speed/direction from the *blended* U/V -- direction is never
    obtained by averaging per-model direction angles."""
    eastward_models = tuple(c.model for c in eastward_contributions)
    northward_models = tuple(c.model for c in northward_contributions)
    if eastward_models != northward_models:
        raise VectorBlendError(
            f"U and V contributor models must match exactly and in order: "
            f"U={eastward_models!r}, V={northward_models!r}"
        )

    eastward_result = blend_scalar(eastward_contributions)
    northward_result = blend_scalar(northward_contributions)

    u = eastward_result.blended_value
    v = northward_result.blended_value
    speed = math.hypot(u, v)
    direction: float | None
    if speed == 0.0:
        direction = None
    else:
        direction = math.degrees(math.atan2(-u, -v)) % 360.0

    return VectorBlendResult(
        eastward_m_s=u,
        northward_m_s=v,
        speed_m_s=speed,
        direction_degrees=direction,
        eastward_contributions=eastward_contributions,
        northward_contributions=northward_contributions,
    )
