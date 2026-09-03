"""Deterministic gust blend operator (plan Section 4.3, Task 9).

1. At each aligned source point, compare the source gust against the
   source sustained speed. A shortfall in [0, 0.1] m/s floors that
   source gust to sustained speed and records
   ``source_gust_floor_applied``; a larger shortfall disqualifies that
   model's coupled wind/gust tuple *at that station/horizon* (U, V and
   gust are rejected atomically, because dropping gust alone would
   break gust-versus-blended-wind consistency). The source value is
   never clamped or repaired, and the model's independent variables at
   that point stay valid.
2. Weighted mean of validated/floored source gusts using the same
   contributor row as U/V.
3. Blended sustained speed from blended U/V; convexity guarantees
   gust >= sustained speed. A finite-precision shortfall <=1e-6 m/s
   floors to sustained speed (``final_gust_epsilon_floor``); larger is
   an invariant failure.
4. Enforce [0, 100] m/s without clipping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar

_SHORTFALL_FLOOR_TOLERANCE_M_S = 0.1
_FINAL_EPSILON_FLOOR_M_S = 1e-6
_VALID_MAX_M_S = 100.0

#: The public spelling of the source-gust floor tolerance, so callers that
#: must *report* the tolerance a disqualification was judged against
#: (contribution/availability provenance) cite the same literal the
#: operator enforces rather than re-declaring their own copy.
SOURCE_GUST_SHORTFALL_FLOOR_TOLERANCE_M_S = _SHORTFALL_FLOOR_TOLERANCE_M_S


class GustDisqualificationError(MesoForgeError):
    """Raised when a source gust is materially below sustained speed
    (shortfall > the configured floor tolerance).

    The operator states the *fact* of the inconsistency; it does not
    choose the rejection scope. The caller applies the smallest
    scientifically coupled scope: gust and its own U/V are one atomic
    tuple, so all three are rejected together at the affected
    station/horizon, and coarser model/variable-family/cycle rejection
    is reserved for coverage, geometry, time-identity, lineage, or a
    documented widespread-quality failure (Codex review ``t_1564b30c``).
    Repairing or clamping the source value is never permitted.
    """


class GustInvariantError(MesoForgeError):
    """Raised when the blended gust is materially below the blended
    sustained speed beyond the finite-precision epsilon floor, or the
    result falls outside [0, 100] m/s."""


@dataclass(frozen=True, slots=True)
class SourceGustValidation:
    validated_gust_m_s: float
    source_gust_floor_applied: bool


def validate_source_gust(
    *,
    gust_m_s: float,
    sustained_speed_m_s: float,
    shortfall_floor_tolerance_m_s: float = _SHORTFALL_FLOOR_TOLERANCE_M_S,
) -> SourceGustValidation:
    """Step 1: validate/floor one source model's gust against its own
    sustained speed. Raises ``GustDisqualificationError`` for a
    material shortfall."""
    shortfall = sustained_speed_m_s - gust_m_s
    if shortfall <= 0:
        return SourceGustValidation(validated_gust_m_s=gust_m_s, source_gust_floor_applied=False)
    if shortfall <= shortfall_floor_tolerance_m_s:
        return SourceGustValidation(
            validated_gust_m_s=sustained_speed_m_s, source_gust_floor_applied=True
        )
    raise GustDisqualificationError(
        f"source gust {gust_m_s!r} m/s is below sustained speed {sustained_speed_m_s!r} m/s "
        f"by {shortfall!r} m/s, exceeding the floor tolerance "
        f"{shortfall_floor_tolerance_m_s!r} m/s; this model's coupled wind/gust tuple is "
        "disqualified at this point"
    )


@dataclass(frozen=True, slots=True)
class GustBlendResult:
    blended_gust_m_s: float
    final_gust_epsilon_floor_applied: bool
    contributions: tuple[Contribution, ...]


def blend_gust(
    *,
    contributions: tuple[Contribution, ...],
    blended_sustained_speed_m_s: float,
    final_epsilon_floor_m_s: float = _FINAL_EPSILON_FLOOR_M_S,
    valid_max_m_s: float = _VALID_MAX_M_S,
) -> GustBlendResult:
    """Steps 2-4: weighted mean of already-validated/floored source
    gusts, then enforce the convexity floor against blended sustained
    speed, then the [0, valid_max_m_s] bound."""
    result = blend_scalar(contributions)
    blended = result.blended_value

    epsilon_floor_applied = False
    shortfall = blended_sustained_speed_m_s - blended
    if shortfall > 0:
        if shortfall <= final_epsilon_floor_m_s:
            blended = blended_sustained_speed_m_s
            epsilon_floor_applied = True
        else:
            raise GustInvariantError(
                f"blended gust {blended!r} m/s is below blended sustained speed "
                f"{blended_sustained_speed_m_s!r} m/s by {shortfall!r} m/s, exceeding the "
                f"finite-precision epsilon floor {final_epsilon_floor_m_s!r} m/s; convexity "
                "invariant violated"
            )

    if not math.isfinite(blended) or not (0.0 <= blended <= valid_max_m_s):
        raise GustInvariantError(
            f"blended gust {blended!r} m/s is outside the valid bound [0, {valid_max_m_s!r}]"
        )

    return GustBlendResult(
        blended_gust_m_s=blended,
        final_gust_epsilon_floor_applied=epsilon_floor_applied,
        contributions=contributions,
    )
