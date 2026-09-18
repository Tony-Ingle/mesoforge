"""Prepared-window envelope shared by discovery, preparation and snapshot consumption.

Every delivered forecast still covers exactly hours 1..36 after its reference time.
A prepared window may extend past that so one prepared snapshot can serve later
reference hours; the extension is bounded so adapter lead envelopes stay explicit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REQUIRED_HOURS = 36
MAXIMUM_PREPARED_HOURS = 42
COVERAGE_POLICY = {
    "id": "mesoforge-prepared-coverage-policy.v1",
    "required_hours": REQUIRED_HOURS,
    "target_hours": MAXIMUM_PREPARED_HOURS,
    "rule": (
        "A prepared window is hours 1..N after its reference time, 36 <= N <= 42. Model "
        "cycles are selected on the first 36 hours exactly as before; hours 37..N are "
        "acquired only where the selected cycle already publishes them, and N is the largest "
        "hour every deterministic contributor reaches. A snapshot serves a later reference "
        "time R only while its prepared valid times still cover R+1..R+36 for the active "
        "deterministic contributors; NBM-based products report their own coverage."
    ),
}


def prepared_horizons(count: int) -> tuple[int, ...]:
    """Hours 1..count for one prepared window; the count is bounded, never inferred."""
    if type(count) is not int or not REQUIRED_HOURS <= count <= MAXIMUM_PREPARED_HOURS:
        raise ValueError(
            f"Prepared windows cover {REQUIRED_HOURS}..{MAXIMUM_PREPARED_HOURS} hours, "
            f"not {count!r}"
        )
    return tuple(range(1, count + 1))


def window_hours(selection: Mapping[str, Any]) -> tuple[int, ...]:
    """The prepared window a selection declares; selections without one are 1..36."""
    horizons = selection.get("horizon_hours", list(range(1, REQUIRED_HOURS + 1)))
    if not is_prepared_window(horizons):
        raise ValueError("Selection horizon_hours must be a 1..36 to 1..42 window")
    return tuple(horizons)


def is_prepared_window(horizons: object) -> bool:
    """True for exactly (1, ..., N) with N in the supported envelope."""
    if not isinstance(horizons, (tuple, list)) or not horizons:
        return False
    if any(type(hour) is not int for hour in horizons):
        return False
    return REQUIRED_HOURS <= len(horizons) <= MAXIMUM_PREPARED_HOURS and list(horizons) == list(
        range(1, len(horizons) + 1)
    )
