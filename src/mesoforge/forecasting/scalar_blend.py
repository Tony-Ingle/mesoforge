"""Deterministic scalar blend operator (plan Section 4.2/4.6, Task 8):
weighted-mean temperature and dew point, with the dew-point-cannot-
exceed-temperature consistency check.

Pure float64 arithmetic. Takes an ordered ``(model, value, weight)``
contributor sequence (already restricted to the exact approved
fallback row for this variable/point) and returns the blended value
plus the individual weighted contributions for the contribution
manifest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mesoforge.common.errors import MesoForgeError


class ScalarBlendError(MesoForgeError):
    """Raised when scalar blend inputs are non-finite or the weights
    do not sum to one within tolerance."""


class ConsistencyError(MesoForgeError):
    """Raised when a cross-variable consistency invariant is violated
    (plan Section 4.6): reject rather than clamp."""


_WEIGHT_SUM_TOLERANCE = 1e-12
_DEW_POINT_TOLERANCE_K = 1e-6


@dataclass(frozen=True, slots=True)
class Contribution:
    model: str
    value: float
    weight: float

    @property
    def weighted_value(self) -> float:
        return self.value * self.weight


@dataclass(frozen=True, slots=True)
class ScalarBlendResult:
    blended_value: float
    contributions: tuple[Contribution, ...]


def blend_scalar(contributions: tuple[Contribution, ...]) -> ScalarBlendResult:
    """Section 4.2: weighted mean with float64 intermediates. Requires
    every contributor's weight to sum to 1 within ``1e-12`` and every
    value to be finite."""
    if not contributions:
        raise ScalarBlendError("blend_scalar requires at least one contributor")

    total_weight = math.fsum(c.weight for c in contributions)
    if abs(total_weight - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ScalarBlendError(
            f"contributor weights must sum to exactly 1 within {_WEIGHT_SUM_TOLERANCE!r}, "
            f"got {total_weight!r}"
        )

    for c in contributions:
        if not math.isfinite(c.value):
            raise ScalarBlendError(f"contributor {c.model!r} has a non-finite value {c.value!r}")

    blended = math.fsum(c.weighted_value for c in contributions)
    return ScalarBlendResult(blended_value=blended, contributions=contributions)


def check_dew_point_consistency(*, temperature_k: float, dew_point_k: float) -> None:
    """Section 4.6: ``dew_point <= temperature + 1e-6 K``. Reject
    (raise) rather than clamp when violated."""
    if dew_point_k > temperature_k + _DEW_POINT_TOLERANCE_K:
        raise ConsistencyError(
            f"blended dew_point {dew_point_k!r} K exceeds blended temperature "
            f"{temperature_k!r} K by more than {_DEW_POINT_TOLERANCE_K!r} K tolerance"
        )
