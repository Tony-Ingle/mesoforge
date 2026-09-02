"""Unit tests for mesoforge.alignment.temporal (plan Section 3.3,
Task 6): exact valid-time and interval-bound matching.
"""

from __future__ import annotations

import numpy as np
import pytest

from mesoforge.alignment.temporal import (
    TemporalAlignmentError,
    find_exact_interval_index,
    find_exact_valid_time_index,
)


class TestFindExactValidTimeIndex:
    def test_finds_exact_match(self) -> None:
        times = np.array(
            ["2026-08-30T13:00", "2026-08-30T14:00", "2026-08-30T15:00"], dtype="datetime64[ns]"
        )
        index = find_exact_valid_time_index(
            times, target_valid_time=np.datetime64("2026-08-30T14:00")
        )
        assert index == 1

    def test_rejects_no_match(self) -> None:
        times = np.array(["2026-08-30T13:00", "2026-08-30T14:00"], dtype="datetime64[ns]")
        with pytest.raises(TemporalAlignmentError, match="no source valid time"):
            find_exact_valid_time_index(times, target_valid_time=np.datetime64("2026-08-30T13:30"))

    def test_rejects_duplicate_match(self) -> None:
        times = np.array(["2026-08-30T13:00", "2026-08-30T13:00"], dtype="datetime64[ns]")
        with pytest.raises(TemporalAlignmentError, match="multiple source valid times"):
            find_exact_valid_time_index(times, target_valid_time=np.datetime64("2026-08-30T13:00"))

    def test_never_nearest_neighbor(self) -> None:
        """A near-but-not-exact target must fail, not silently pick the
        closest source time."""
        times = np.array(["2026-08-30T13:00"], dtype="datetime64[ns]")
        with pytest.raises(TemporalAlignmentError):
            find_exact_valid_time_index(
                times, target_valid_time=np.datetime64("2026-08-30T13:00:01")
            )


class TestFindExactIntervalIndex:
    def test_finds_exact_match(self) -> None:
        starts = np.array(["2026-08-30T12:00", "2026-08-30T13:00"], dtype="datetime64[ns]")
        ends = np.array(["2026-08-30T13:00", "2026-08-30T14:00"], dtype="datetime64[ns]")
        index = find_exact_interval_index(
            starts,
            ends,
            target_start=np.datetime64("2026-08-30T13:00"),
            target_end=np.datetime64("2026-08-30T14:00"),
        )
        assert index == 1

    def test_rejects_mismatched_start(self) -> None:
        starts = np.array(["2026-08-30T11:00"], dtype="datetime64[ns]")
        ends = np.array(["2026-08-30T14:00"], dtype="datetime64[ns]")
        with pytest.raises(TemporalAlignmentError, match="no source interval"):
            find_exact_interval_index(
                starts,
                ends,
                target_start=np.datetime64("2026-08-30T13:00"),
                target_end=np.datetime64("2026-08-30T14:00"),
            )

    def test_rejects_duplicate_interval(self) -> None:
        starts = np.array(["2026-08-30T13:00", "2026-08-30T13:00"], dtype="datetime64[ns]")
        ends = np.array(["2026-08-30T14:00", "2026-08-30T14:00"], dtype="datetime64[ns]")
        with pytest.raises(TemporalAlignmentError, match="multiple source intervals"):
            find_exact_interval_index(
                starts,
                ends,
                target_start=np.datetime64("2026-08-30T13:00"),
                target_end=np.datetime64("2026-08-30T14:00"),
            )
