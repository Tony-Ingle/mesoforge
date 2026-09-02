"""Unit tests for mesoforge.guidance.cycle_selection (plan Section 1.2,
Task 2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.guidance.cycle_selection import (
    CandidateCycle,
    generate_candidate_reference_times,
    select_model_cycle,
)

TARGET = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)


class TestGenerateCandidateReferenceTimes:
    def test_hourly_cadence_newest_first(self) -> None:
        candidates = generate_candidate_reference_times(
            target_reference_time=TARGET, cadence="hourly", max_lookback_hours=3
        )
        assert candidates == (
            TARGET,
            TARGET - timedelta(hours=1),
            TARGET - timedelta(hours=2),
            TARGET - timedelta(hours=3),
        )

    def test_hourly_cadence_rejects_explicit_allowed_hours(self) -> None:
        with pytest.raises(ValueError, match="must not pass allowed_hours"):
            generate_candidate_reference_times(
                target_reference_time=TARGET, cadence="hourly", allowed_hours=(0,)
            )

    def test_fixed_cadence_only_allowed_hours(self) -> None:
        candidates = generate_candidate_reference_times(
            target_reference_time=TARGET,
            cadence="fixed",
            allowed_hours=(0, 6, 12, 18),
            max_lookback_hours=30,
        )
        assert all(c.hour in (0, 6, 12, 18) for c in candidates)
        assert candidates[0] == TARGET  # 18Z is itself a candidate
        assert all(c <= TARGET for c in candidates)

    def test_fixed_cadence_requires_allowed_hours(self) -> None:
        with pytest.raises(ValueError, match="requires a non-empty"):
            generate_candidate_reference_times(target_reference_time=TARGET, cadence="fixed")

    def test_rejects_naive_target(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            generate_candidate_reference_times(
                target_reference_time=datetime(2026, 8, 30, 18, 0), cadence="hourly"
            )

    def test_no_duplicate_candidates(self) -> None:
        candidates = generate_candidate_reference_times(
            target_reference_time=TARGET,
            cadence="fixed",
            allowed_hours=(0, 6, 12, 18),
            max_lookback_hours=50,
        )
        assert len(candidates) == len(set(candidates))


def _complete(reference_time: datetime, *, completed_at: datetime | None = None) -> CandidateCycle:
    return CandidateCycle(
        reference_time=reference_time,
        is_complete=True,
        completed_at=completed_at
        if completed_at is not None
        else reference_time + timedelta(minutes=30),
    )


class TestSelectModelCycle:
    def test_selects_newest_complete_within_age(self) -> None:
        candidates = [
            _complete(TARGET),
            _complete(TARGET - timedelta(hours=6)),
        ]
        result = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=candidates,
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert result.selected is not None
        assert result.selected.reference_time == TARGET

    def test_rejects_future_cycle(self) -> None:
        future = CandidateCycle(
            reference_time=TARGET + timedelta(hours=1),
            is_complete=True,
            completed_at=TARGET + timedelta(hours=1, minutes=10),
        )
        result = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=[future],
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert result.selected is None
        assert "after target_reference_time" in result.rejected[0].reason

    def test_rejects_incomplete_cycle_and_falls_back_to_older(self) -> None:
        incomplete = CandidateCycle(reference_time=TARGET, is_complete=False, completed_at=None)
        older_complete = _complete(TARGET - timedelta(hours=1))
        result = select_model_cycle(
            model="nbm",
            target_reference_time=TARGET,
            candidates=[incomplete, older_complete],
            max_age_hours=3.0,
            completion_deadline_minutes=90.0,
        )
        assert result.selected is not None
        assert result.selected.reference_time == TARGET - timedelta(hours=1)
        assert "incomplete" in result.rejected[0].reason

    def test_rejects_never_completed_cycle(self) -> None:
        never_completed = CandidateCycle(reference_time=TARGET, is_complete=True, completed_at=None)
        result = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=[never_completed],
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert result.selected is None
        assert "never completed" in result.rejected[0].reason

    def test_rejects_cycle_completed_after_deadline(self) -> None:
        late = CandidateCycle(
            reference_time=TARGET, is_complete=True, completed_at=TARGET + timedelta(hours=3)
        )
        result = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=[late],
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert result.selected is None
        assert "completion deadline" in result.rejected[0].reason

    def test_rejects_cycle_exceeding_max_age(self) -> None:
        old = _complete(TARGET - timedelta(hours=10))
        result = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=[old],
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert result.selected is None
        assert "exceeds max_age_hours" in result.rejected[0].reason

    def test_no_candidates_yields_unavailable(self) -> None:
        result = select_model_cycle(
            model="gfs",
            target_reference_time=TARGET,
            candidates=[],
            max_age_hours=12.0,
            completion_deadline_minutes=360.0,
        )
        assert result.selected is None
        assert result.rejected == ()

    def test_never_splices_selects_exactly_one_cycle(self) -> None:
        """Section 1.2: 'A model cannot use different cycles by variable
        or target horizon' / 'Never splice cycles' -- the result always
        carries exactly one selected reference_time, never a set."""
        candidates = [_complete(TARGET), _complete(TARGET - timedelta(hours=1))]
        result = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=candidates,
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert isinstance(result.selected, CandidateCycle)

    def test_deterministic_ordering_independent_of_input_order(self) -> None:
        candidates_a = [_complete(TARGET - timedelta(hours=2)), _complete(TARGET)]
        candidates_b = list(reversed(candidates_a))
        result_a = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=candidates_a,
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        result_b = select_model_cycle(
            model="hrrr",
            target_reference_time=TARGET,
            candidates=candidates_b,
            max_age_hours=6.0,
            completion_deadline_minutes=120.0,
        )
        assert result_a.selected == result_b.selected
        assert result_a.selected is not None
        assert result_a.selected.reference_time == TARGET
