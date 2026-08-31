"""PoP (probability of precipitation) passthrough operator (plan
Section 4.5, Task 9). NBM PoP01 is the sole contributor with weight
1.0 -- no combination with any other model, and no synthesis from
deterministic HRRR/GFS precipitation.
"""

from __future__ import annotations

import math

from mesoforge.common.errors import MesoForgeError

_POP_MIN = 0.0
_POP_MAX = 1.0


class PopBlendError(MesoForgeError):
    """Raised when the NBM PoP01 passthrough value is non-finite or
    outside [0, 1]."""


def pop_passthrough(nbm_pop_fraction: float) -> float:
    """Section 4.5: PoP is NBM PoP01 passthrough with weight
    ``NBM=1.0``. Never derives probability from deterministic HRRR/GFS
    precipitation and never combines incompatible event definitions --
    this function accepts exactly one input, the already-converted
    NBM PoP01 fraction."""
    if not math.isfinite(nbm_pop_fraction) or not (_POP_MIN <= nbm_pop_fraction <= _POP_MAX):
        raise PopBlendError(
            f"NBM PoP01 fraction must be finite and in [0, 1], got {nbm_pop_fraction!r}"
        )
    return nbm_pop_fraction
