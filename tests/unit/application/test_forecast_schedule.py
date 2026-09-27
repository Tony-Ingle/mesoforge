"""Named-timezone forecast slots stay correct across CST, CDT and both DST changes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from mesoforge.application.forecast_schedule import ForecastSchedule
from mesoforge.application.prepared_snapshot import derive_reference_time

CHICAGO = ZoneInfo("America/Chicago")
SCHEDULE = ForecastSchedule()


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


@pytest.mark.parametrize(
    ("local_date", "expected"),
    [
        # CDT (UTC-5)
        (date(2026, 7, 1), [utc(2026, 7, 1, 13), utc(2026, 7, 2, 1)]),
        # CST (UTC-6)
        (date(2026, 1, 15), [utc(2026, 1, 15, 14), utc(2026, 1, 16, 2)]),
        # Spring forward happens at 02:00 on 2026-03-08: both slots are already CDT.
        (date(2026, 3, 8), [utc(2026, 3, 8, 13), utc(2026, 3, 9, 1)]),
        (date(2026, 3, 7), [utc(2026, 3, 7, 14), utc(2026, 3, 8, 2)]),
        # Fall back happens at 02:00 on 2026-11-01: both slots are already CST.
        (date(2026, 11, 1), [utc(2026, 11, 1, 14), utc(2026, 11, 2, 2)]),
        (date(2026, 10, 31), [utc(2026, 10, 31, 13), utc(2026, 11, 1, 1)]),
    ],
)
def test_slots_follow_local_clock_not_a_fixed_utc_offset(
    local_date: date, expected: list[datetime]
) -> None:
    slots = SCHEDULE.slots_on(local_date)
    assert slots == expected
    assert [slot.astimezone(CHICAGO).strftime("%H:%M") for slot in slots] == ["08:00", "20:00"]


def test_due_slot_window_is_clipped_to_the_slot_utc_hour() -> None:
    slot = utc(2026, 7, 1, 13)
    assert SCHEDULE.due_slot(slot) == slot
    assert SCHEDULE.due_slot(slot + timedelta(minutes=59, seconds=59)) == slot
    assert SCHEDULE.due_slot(slot + timedelta(hours=1)) is None
    assert SCHEDULE.due_slot(slot - timedelta(seconds=1)) is None
    half_past = ForecastSchedule(times=("08:30",), window_minutes=60)
    start = utc(2026, 7, 1, 13, 30)
    assert half_past.window_end(start) == utc(2026, 7, 1, 14)
    assert half_past.due_slot(utc(2026, 7, 1, 13, 59)) == start
    assert half_past.due_slot(utc(2026, 7, 1, 14, 5)) is None
    # Every accepted instant maps to the slot's own issuance reference hour.
    assert derive_reference_time(utc(2026, 7, 1, 13, 59, 59)) == slot


def test_evening_slot_on_previous_local_date_is_found_after_utc_midnight() -> None:
    # 20:00 CDT on 2026-07-01 is 01:00Z on 2026-07-02 (a different UTC date).
    assert SCHEDULE.due_slot(utc(2026, 7, 2, 1, 10)) == utc(2026, 7, 2, 1)


def test_next_run_crosses_days_and_dst_changes() -> None:
    assert SCHEDULE.next_run(utc(2026, 7, 1, 12)) == utc(2026, 7, 1, 13)
    assert SCHEDULE.next_run(utc(2026, 7, 1, 13)) == utc(2026, 7, 2, 1)
    assert SCHEDULE.next_run(utc(2026, 3, 8, 3)) == utc(2026, 3, 8, 13)
    assert SCHEDULE.next_run(utc(2026, 10, 31, 20)) == utc(2026, 11, 1, 1)
    assert SCHEDULE.next_run(utc(2026, 11, 1, 2)) == utc(2026, 11, 1, 14)


def test_nonexistent_and_repeated_local_times_fire_once() -> None:
    gap = ForecastSchedule(times=("02:30",))
    # 02:30 does not exist on 2026-03-08; it maps to the instant after the gap.
    assert gap.slots_on(date(2026, 3, 8)) == [utc(2026, 3, 8, 8, 30)]
    repeated = ForecastSchedule(times=("01:30",))
    # 01:30 occurs twice on 2026-11-01; only the first (CDT) occurrence is a slot.
    assert repeated.slots_on(date(2026, 11, 1)) == [utc(2026, 11, 1, 6, 30)]
    assert repeated.due_slot(utc(2026, 11, 1, 6, 45)) == utc(2026, 11, 1, 6, 30)
    assert repeated.due_slot(utc(2026, 11, 1, 7, 45)) is None


@pytest.mark.parametrize("minute", [0, 7, 31, 59])
def test_hourly_utc_trigger_is_due_exactly_twice_per_local_day_all_year(minute: int) -> None:
    """An hourly UTC scheduler (GitHub Actions cron) needs no seasonal edits."""
    due: dict[date, list[datetime]] = {}
    instant = utc(2026, 1, 1, 0, minute)
    while instant < utc(2027, 1, 1):
        slot = SCHEDULE.due_slot(instant)
        if slot is not None:
            due.setdefault(slot.astimezone(CHICAGO).date(), []).append(slot)
        instant += timedelta(hours=1)
    days = [day for day in due if date(2026, 1, 2) <= day <= date(2026, 12, 30)]
    assert len(days) == 363
    for day in days:
        slots = due[day]
        assert len(slots) == len(set(slots)) == 2, day
        assert [s.astimezone(CHICAGO).strftime("%H:%M") for s in slots] == ["08:00", "20:00"]


def test_duplicate_triggers_in_one_window_resolve_to_one_slot_and_reference_hour() -> None:
    triggers = [utc(2026, 1, 15, 14, 0, 2), utc(2026, 1, 15, 14, 0, 3), utc(2026, 1, 15, 14, 40)]
    slots = {SCHEDULE.due_slot(value) for value in triggers}
    references = {derive_reference_time(value) for value in triggers}
    assert slots == {utc(2026, 1, 15, 14)}
    assert references == {utc(2026, 1, 15, 14)}


def test_describe_and_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    described = SCHEDULE.describe(utc(2026, 7, 1, 13, 5))
    assert described["due_slot"] == "2026-07-01T13:00:00Z"
    assert described["next_run"] == "2026-07-02T01:00:00Z"
    assert described["next_run_local"] == "2026-07-01T20:00:00-05:00"
    monkeypatch.delenv("MESOFORGE_FORECAST_TIMEZONE", raising=False)
    monkeypatch.delenv("MESOFORGE_FORECAST_TIMES", raising=False)
    assert ForecastSchedule.from_environment() == ForecastSchedule()
    monkeypatch.setenv("MESOFORGE_FORECAST_TIMEZONE", "America/Denver")
    monkeypatch.setenv("MESOFORGE_FORECAST_TIMES", "06:00, 18:00")
    assert ForecastSchedule.from_environment() == ForecastSchedule(
        timezone="America/Denver", times=("06:00", "18:00")
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"times": ("8:00",)},
        {"times": ("24:00",)},
        {"times": ("08:00", "08:00")},
        {"times": ()},
        {"window_minutes": 0},
        {"window_minutes": 61},
    ],
)
def test_invalid_schedules_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        ForecastSchedule(**kwargs)


def test_unknown_timezone_and_naive_times_are_rejected() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        ForecastSchedule(timezone="America/Nowhere")
    with pytest.raises(ValueError):
        SCHEDULE.due_slot(datetime(2026, 7, 1, 13))
