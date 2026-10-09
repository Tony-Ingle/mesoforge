"""Read-only exact-event validation and aggregation; never redistribute QPF."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any


def interval_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("QPF interval times must be timezone-aware")
    return result.astimezone(UTC)


def validate_qpf_intervals(rows: Any, *, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Require one ordered complete partition, with null amounts remaining missing."""
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("QPF partition requires aware increasing coverage bounds")
    if not isinstance(rows, list) or not rows:
        raise ValueError("QPF partition must contain explicit interval rows")
    cursor = start.astimezone(UTC)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("QPF interval row must be an object")
        left, right = (interval_time(row[key]) for key in ("interval_start", "interval_end"))
        if left != cursor or right <= left or right > end:
            raise ValueError("QPF partition has a gap, overlap, or out-of-coverage interval")
        if (right - left) % timedelta(hours=1):
            raise ValueError("QPF partition must preserve whole-hour native events")
        duration = row.get("accumulation_duration_hours")
        if duration is not None and (
            type(duration) not in (int, float) or duration != (right - left).total_seconds() / 3600
        ):
            raise ValueError("QPF declared accumulation duration contradicts its exact interval")
        if row.get("unit") != "kg/m^2" or row.get("interval_closure") != "left_open_right_closed":
            raise ValueError("QPF partition requires kg/m^2 and (start,end] semantics")
        if "value" not in row:
            raise ValueError("QPF partition requires an explicit amount or null")
        value = row["value"]
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
        ):
            raise ValueError("QPF interval amount must be nonnegative finite or null")
        cursor = right
    if cursor != end:
        raise ValueError("QPF partition does not cover the complete forecast horizon")
    return rows


def summarize_qpf_intervals(
    rows: list[dict[str, Any]], *, start: datetime, end: datetime
) -> dict[str, Any]:
    """Sum complete native events only; any crossing/missing interval defeats a total.

    Callers validate the full partition once before requesting bounded summaries.
    Boundary-crossing events remain visible in the original collection, never split.
    """
    contained = []
    crossing = 0
    for row in rows:
        left, right = (interval_time(row[key]) for key in ("interval_start", "interval_end"))
        if left >= end or right <= start:
            continue
        if left < start or right > end:
            crossing += 1
        else:
            contained.append(row)
    values = [row["value"] for row in contained if row["value"] is not None]
    covered = sum(
        (
            interval_time(row["interval_end"]) - interval_time(row["interval_start"])
            for row in contained
        ),
        timedelta(),
    )
    complete = not crossing and covered == end - start and len(values) == len(contained)
    return {
        "total_kg_m2": sum(values) if complete else None,
        "sum_of_available_contained_events_kg_m2": sum(values),
        "complete": complete,
        "contained_events": len(contained),
        "available_events": len(values),
        "boundary_crossing_events": crossing,
        "basis": "whole_native_events_only_no_splitting_or_redistribution",
    }
