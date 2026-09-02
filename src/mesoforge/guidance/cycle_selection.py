"""Deterministic Phase 2 model-cycle selection (plan Section 1.2, Task 2).

Pure selection logic over an already-discovered set of candidate
cycles for one model family: newest-first ordering, completeness,
information-cutoff/completion-deadline, and max-age rejection. Never
splices cycles -- exactly one ``reference_time`` (or none) is selected
per model family per run, and only that cycle's leads are ever
acquired downstream.

Candidate discovery (which cycles actually exist on the remote, and
whether every required source group/target horizon was successfully
retrieved) is an acquisition-layer concern; this module consumes an
already-built ``CandidateCycle`` sequence and applies only the
deterministic Section 1.2 selection algorithm.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal


@dataclass(frozen=True, slots=True)
class CandidateCycle:
    """One considered cycle for one model family (plan Section 1.2
    steps 1-3): its reference time, whether every required source
    group and target horizon was retrieved successfully (``is_complete``),
    and when the cycle's data became fully available (``completed_at``,
    ``None`` if it never completed)."""

    reference_time: datetime
    is_complete: bool
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    candidate: CandidateCycle
    reason: str


@dataclass(frozen=True, slots=True)
class CycleSelectionResult:
    """``model-cycle-selection.v1`` in-memory result (plan Section 5.1):
    every considered cycle and its rejection reason, plus the single
    selected cycle (``None`` if the model family is unavailable for
    this run)."""

    model: Literal["hrrr", "nbm", "gfs"]
    target_reference_time: datetime
    selected: CandidateCycle | None
    rejected: tuple[RejectedCandidate, ...]
    considered: tuple[CandidateCycle, ...]


def generate_candidate_reference_times(
    *,
    target_reference_time: datetime,
    cadence: Literal["hourly", "fixed"],
    allowed_hours: tuple[int, ...] = (),
    max_lookback_hours: int = 72,
) -> tuple[datetime, ...]:
    """Section 1.2 step 1: candidate cycles at the model cadence,
    newest first, with ``source_reference_time <= target_reference_time``.

    ``hourly`` cadence (NBM) considers every UTC hour going back
    ``max_lookback_hours``. ``fixed`` cadence (HRRR/GFS extended
    cycles) considers only ``allowed_hours`` UTC hours on each day
    going back far enough to cover ``max_lookback_hours``.
    """
    if target_reference_time.tzinfo is None:
        raise ValueError("target_reference_time must be timezone-aware")
    target = target_reference_time.astimezone(UTC)

    if cadence == "hourly":
        if allowed_hours:
            raise ValueError("cadence='hourly' must not pass allowed_hours")
        candidates = [target - timedelta(hours=h) for h in range(max_lookback_hours + 1)]
    else:
        if not allowed_hours:
            raise ValueError("cadence='fixed' requires a non-empty allowed_hours tuple")
        candidates = []
        days_back = (max_lookback_hours // 24) + 2
        for day_offset in range(days_back + 1):
            day = (target - timedelta(days=day_offset)).date()
            for hour in allowed_hours:
                candidate = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
                if candidate <= target:
                    candidates.append(candidate)

    unique_sorted = sorted(set(candidates), reverse=True)
    return tuple(unique_sorted)


def select_model_cycle(
    *,
    model: Literal["hrrr", "nbm", "gfs"],
    target_reference_time: datetime,
    candidates: Sequence[CandidateCycle],
    max_age_hours: float,
    completion_deadline_minutes: float,
) -> CycleSelectionResult:
    """Section 1.2 steps 1-5: apply the deterministic cycle-selection
    algorithm over ``candidates`` (already newest-first, or re-sorted
    here defensively) and return exactly one selected cycle or none.

    A cycle is rejected (in order of the checks below) for:

    1. ``reference_time > target_reference_time`` (future cycle);
    2. incomplete source coverage (``is_complete=False``);
    3. never completing (``completed_at is None``);
    4. completing after its cycle-completion deadline
       (``completed_at > reference_time + completion_deadline_minutes``);
    5. exceeding the configured ``max_age_hours``.

    The first remaining candidate (newest first) is selected.
    """
    if target_reference_time.tzinfo is None:
        raise ValueError("target_reference_time must be timezone-aware")
    target = target_reference_time.astimezone(UTC)

    ordered = tuple(sorted(candidates, key=lambda c: c.reference_time, reverse=True))
    rejected: list[RejectedCandidate] = []
    selected: CandidateCycle | None = None

    for candidate in ordered:
        reference_time = candidate.reference_time
        if reference_time.tzinfo is None:
            raise ValueError(f"candidate reference_time must be timezone-aware: {candidate!r}")
        reference_time = reference_time.astimezone(UTC)

        if reference_time > target:
            rejected.append(
                RejectedCandidate(candidate, "reference_time is after target_reference_time")
            )
            continue
        if not candidate.is_complete:
            rejected.append(
                RejectedCandidate(
                    candidate,
                    "incomplete: not every required source group/target horizon was retrieved",
                )
            )
            continue
        if candidate.completed_at is None:
            rejected.append(RejectedCandidate(candidate, "cycle never completed"))
            continue
        deadline = reference_time + timedelta(minutes=completion_deadline_minutes)
        completed_at = candidate.completed_at.astimezone(UTC)
        if completed_at > deadline:
            rejected.append(
                RejectedCandidate(
                    candidate,
                    f"completed at {completed_at!r} after the cycle completion deadline "
                    f"{deadline!r}",
                )
            )
            continue
        age_hours = (target - reference_time).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            rejected.append(
                RejectedCandidate(
                    candidate,
                    f"cycle age {age_hours!r} hours exceeds max_age_hours {max_age_hours!r}",
                )
            )
            continue

        selected = candidate
        break

    # Every candidate strictly after the selected one (older) is not
    # separately evaluated -- Section 1.2 selects the *first* remaining
    # candidate, so once one is selected the remainder are simply
    # unconsidered, not rejected. Record only the actually-rejected
    # newer candidates plus the selected one (if any) in "considered".
    considered_index = ordered.index(selected) + 1 if selected is not None else len(rejected)
    considered = (
        ordered[:considered_index] if selected is not None else tuple(r.candidate for r in rejected)
    )

    return CycleSelectionResult(
        model=model,
        target_reference_time=target,
        selected=selected,
        rejected=tuple(rejected),
        considered=considered,
    )
