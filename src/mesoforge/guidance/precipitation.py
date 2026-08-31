"""GFS precipitation (APCP) bucket/continuous disambiguation and
same-bucket differencing (plan Section 2.4, Task 4).

GFS publishes both six-hour-bucket and continuous accumulation
records. Phase 2 deliberately uses the bucket series: it yields
bounded one-hour differences across every required source lead. Pure
arithmetic/selection logic -- no I/O, no GRIB decoding (that happens
in ``gfs_decoding.py``); this module receives already-decoded bucket
values and applies the exact selection/differencing contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from mesoforge.common.errors import MesoForgeError

_NEGATIVE_TOLERANCE_KG_M2 = -1e-6


class GfsPrecipitationError(MesoForgeError):
    """Raised when GFS APCP bucket selection or differencing violates
    the Section 2.4 contract (wrong bucket boundary, non-monotonic
    difference below tolerance, or a bucket/continuous mismatch)."""


def compute_bucket_start(forecast_hour: int) -> int:
    """Section 2.4: ``bucket_start = 6 * floor((lead - 1) / 6)`` for
    GFS source forecast hour ``lead`` (1-indexed, <=48)."""
    if forecast_hour < 1:
        raise ValueError(f"forecast_hour must be >= 1, got {forecast_hour!r}")
    return 6 * ((forecast_hour - 1) // 6)


def is_bucket_reset_hour(forecast_hour: int) -> bool:
    """``True`` when ``forecast_hour`` is the first hour after a bucket
    reset (``f = 6k+1``), i.e. the one-hour QPF is the bucket value
    itself rather than a difference."""
    return (forecast_hour - 1) % 6 == 0


@dataclass(frozen=True, slots=True)
class BucketPrecipitationResult:
    one_hour_qpf_kg_m2: float
    bucket_start_hour: int
    is_reset_passthrough: bool
    finite_precision_floor_applied: bool
    parent_bucket_value_current: float
    parent_bucket_value_previous: float | None


def compute_one_hour_qpf(
    *,
    forecast_hour: int,
    bucket_value_current_kg_m2: float,
    bucket_value_previous_kg_m2: float | None,
) -> BucketPrecipitationResult:
    """Section 2.4: compute the exact one-hour QPF for GFS source lead
    ``forecast_hour`` from its selected bucket-accumulation value(s).

    For ``f = 6k+1`` (a reset hour), the one-hour value is the bucket
    value itself (``bucket_start = f - 1``, so the bucket contains
    exactly one hour). Otherwise it is
    ``bucket[f] - bucket[f-1]`` where both values come from the *same*
    ``startStep`` bucket. A difference in ``[-1e-6, 0)`` kg/m2 is
    floored to zero with ``finite_precision_floor_applied=True``; a
    larger decrease is a terminal ``GfsPrecipitationError``.
    """
    bucket_start = compute_bucket_start(forecast_hour)
    reset = is_bucket_reset_hour(forecast_hour)

    if reset:
        if bucket_value_previous_kg_m2 is not None:
            raise GfsPrecipitationError(
                f"forecast_hour={forecast_hour!r} is a bucket reset hour (f=6k+1); "
                "no previous-bucket value should be supplied for passthrough"
            )
        return BucketPrecipitationResult(
            one_hour_qpf_kg_m2=bucket_value_current_kg_m2,
            bucket_start_hour=bucket_start,
            is_reset_passthrough=True,
            finite_precision_floor_applied=False,
            parent_bucket_value_current=bucket_value_current_kg_m2,
            parent_bucket_value_previous=None,
        )

    if bucket_value_previous_kg_m2 is None:
        raise GfsPrecipitationError(
            f"forecast_hour={forecast_hour!r} requires a previous-bucket value for "
            "same-bucket differencing (not a reset hour)"
        )

    difference = bucket_value_current_kg_m2 - bucket_value_previous_kg_m2
    floor_applied = False
    if difference < 0:
        if difference >= _NEGATIVE_TOLERANCE_KG_M2:
            difference = 0.0
            floor_applied = True
        else:
            raise GfsPrecipitationError(
                f"forecast_hour={forecast_hour!r} bucket difference {difference!r} kg/m2 "
                f"is below the finite-precision tolerance {_NEGATIVE_TOLERANCE_KG_M2!r}; "
                "non-monotonic GFS bucket accumulation is a terminal validation error"
            )

    return BucketPrecipitationResult(
        one_hour_qpf_kg_m2=difference,
        bucket_start_hour=bucket_start,
        is_reset_passthrough=False,
        finite_precision_floor_applied=floor_applied,
        parent_bucket_value_current=bucket_value_current_kg_m2,
        parent_bucket_value_previous=bucket_value_previous_kg_m2,
    )


@dataclass(frozen=True, slots=True)
class ApcpCandidateRecord:
    """One decoded APCP inventory candidate for a GFS lead: its exact
    decoded start/end step and statistical process, used to select
    between the required bucket record and a possible duplicate
    continuous-total record at early leads."""

    start_step: int
    end_step: int
    is_accumulation: bool
    values_kg_m2: tuple[float, ...]


def select_bucket_record(
    candidates: tuple[ApcpCandidateRecord, ...], *, forecast_hour: int
) -> ApcpCandidateRecord:
    """Section 2.4: select the record whose decoded ``startStep`` equals
    the computed bucket start and ``endStep`` equals ``forecast_hour``,
    with an accumulation statistical process. For ``f=1..6`` the bucket
    and continuous inventory descriptions may be duplicates; both
    candidates are validated for equivalence by
    ``validate_dual_parent_equivalence`` when present, and this
    function returns only the bucket-boundary match, never selecting
    by inventory message order."""
    expected_start = compute_bucket_start(forecast_hour)
    matches = [
        c
        for c in candidates
        if c.is_accumulation and c.start_step == expected_start and c.end_step == forecast_hour
    ]
    if len(matches) == 0:
        raise GfsPrecipitationError(
            f"no APCP candidate with startStep={expected_start!r}, endStep={forecast_hour!r}, "
            "accumulation statistical process found for the required GFS bucket record"
        )
    if len(matches) > 1:
        raise GfsPrecipitationError(
            f"multiple APCP candidates share startStep={expected_start!r}, "
            f"endStep={forecast_hour!r}; ambiguous bucket selection"
        )
    return matches[0]


_EQUIVALENCE_TOLERANCE_KG_M2 = 1e-6


def validate_dual_parent_equivalence(
    bucket_record: ApcpCandidateRecord, continuous_record: ApcpCandidateRecord
) -> bool:
    """Section 2.4: for ``f=1..6`` the bucket and continuous
    accumulation records are duplicates (``startStep=0``). Canonicalize
    only when full arrays, start/end steps, and statistical process
    are equivalent; return the equivalence result (never silently
    assume equivalence -- the caller records both parents plus this
    boolean in lineage)."""
    if bucket_record.start_step != continuous_record.start_step:
        return False
    if bucket_record.end_step != continuous_record.end_step:
        return False
    if bucket_record.is_accumulation != continuous_record.is_accumulation:
        return False
    if len(bucket_record.values_kg_m2) != len(continuous_record.values_kg_m2):
        return False
    return all(
        abs(a - b) <= _EQUIVALENCE_TOLERANCE_KG_M2
        for a, b in zip(bucket_record.values_kg_m2, continuous_record.values_kg_m2, strict=True)
    )
