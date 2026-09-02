"""Cross-variable/invariant consistency checks (plan Section 4.6, Task
8/9). Flags tension conditions without mutating values, and enforces
hard invariants that reject the run when violated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_POP_QPF_EVENT_THRESHOLD_KG_M2 = 0.254


@dataclass(frozen=True, slots=True)
class ProbabilityDeterministicTension:
    kind: str  # "pop_zero_qpf_above_threshold" | "pop_one_qpf_zero"


def check_probability_deterministic_tension(
    *, pop_fraction: float, qpf_kg_m2: float
) -> ProbabilityDeterministicTension | None:
    """Section 4.6: flag, but do not alter, ``PoP == 0 and QPF >=
    0.254`` or ``PoP == 1 and QPF == 0`` as
    ``probability_deterministic_tension``; these are not mathematical
    contradictions."""
    if pop_fraction == 0.0 and qpf_kg_m2 >= _POP_QPF_EVENT_THRESHOLD_KG_M2:
        return ProbabilityDeterministicTension(kind="pop_zero_qpf_above_threshold")
    if pop_fraction == 1.0 and qpf_kg_m2 == 0.0:
        return ProbabilityDeterministicTension(kind="pop_one_qpf_zero")
    return None


def check_pop_bounds(pop_fraction: float) -> None:
    if not math.isfinite(pop_fraction) or not (0.0 <= pop_fraction <= 1.0):
        raise ValueError(f"PoP must be finite and in [0, 1], got {pop_fraction!r}")


def check_qpf_nonnegative(qpf_kg_m2: float) -> None:
    if not math.isfinite(qpf_kg_m2) or qpf_kg_m2 < 0:
        raise ValueError(f"QPF must be finite and nonnegative, got {qpf_kg_m2!r}")
