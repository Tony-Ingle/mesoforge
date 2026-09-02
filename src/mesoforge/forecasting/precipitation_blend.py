"""Deterministic QPF blend operator (plan Section 4.4, Task 9).

Weighted mean of one-hour QPF contributions from the explicit
precipitation fallback rows. Blends only identical one-hour intervals
(the caller guarantees this by construction via exact temporal
alignment, plan Section 3.3); inputs and output must be finite and
nonnegative.
"""

from __future__ import annotations

import math

from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar

_QPF_MAX_KG_M2 = 1000.0  # generous upper sanity bound; not a physical hard limit


class QpfBlendError(MesoForgeError):
    """Raised when a QPF contribution is non-finite/negative, or the
    blended output is non-finite/negative."""


def blend_qpf(contributions: tuple[Contribution, ...]) -> float:
    """Section 4.4: blend only identical one-hour intervals. Inputs and
    output must be finite and nonnegative. Does not clip negatives
    except the explicitly bounded GFS differencing tolerance, which is
    already applied upstream (``guidance.precipitation``) before this
    function ever sees the value."""
    for c in contributions:
        if not math.isfinite(c.value) or c.value < 0:
            raise QpfBlendError(
                f"QPF contribution from {c.model!r} must be finite and nonnegative, got {c.value!r}"
            )

    result = blend_scalar(contributions)
    blended = result.blended_value
    if not math.isfinite(blended) or blended < 0:
        raise QpfBlendError(f"blended QPF {blended!r} kg/m2 must be finite and nonnegative")
    return blended
