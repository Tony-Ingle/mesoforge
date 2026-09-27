"""When configured forecasts are due: named-timezone slots, never fixed UTC offsets.

The external scheduler decides WHEN to invoke the forecast worker. This module only
answers whether an invocation falls inside a scheduled slot and when the next slot
is, so an hourly UTC trigger (for example GitHub Actions) or a timezone-aware host
timer behave identically across daylight-saving changes. It contains no meteorology.

A slot's acceptance window is clipped to the slot's UTC hour: every accepted
invocation therefore maps to the same issuance reference hour, and the existing
issuance guard turns duplicate triggers into skips instead of second issuances.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_TIMES = ("08:00", "20:00")
DEFAULT_WINDOW_MINUTES = 60
TIMEZONE_VARIABLE = "MESOFORGE_FORECAST_TIMEZONE"
TIMES_VARIABLE = "MESOFORGE_FORECAST_TIMES"
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Schedule times must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ForecastSchedule:
    timezone: str = DEFAULT_TIMEZONE
    times: tuple[str, ...] = DEFAULT_TIMES
    window_minutes: int = DEFAULT_WINDOW_MINUTES

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if not self.times or any(not _TIME.match(value) for value in self.times):
            raise ValueError("Schedule times must be HH:MM local times")
        if len(set(self.times)) != len(self.times):
            raise ValueError("Schedule times must be distinct")
        if type(self.window_minutes) is not int or not 1 <= self.window_minutes <= 60:
            raise ValueError("Schedule window must be 1..60 minutes")

    @classmethod
    def from_environment(cls) -> ForecastSchedule:
        """Both roles read the same optional settings; unset means the product default."""
        zone = os.environ.get(TIMEZONE_VARIABLE, "").strip() or DEFAULT_TIMEZONE
        raw = os.environ.get(TIMES_VARIABLE, "").strip()
        times = tuple(part.strip() for part in raw.split(",")) if raw else DEFAULT_TIMES
        return cls(timezone=zone, times=times)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def slots_on(self, local_date: date) -> list[datetime]:
        """UTC instants of the local slots on one local date.

        A local time skipped by a spring-forward gap maps to the instant after the gap;
        a repeated fall-back time uses its first occurrence (``fold=0``), so each slot
        fires once per local date.
        """
        slots = []
        for value in sorted(self.times):
            hour, minute = (int(part) for part in value.split(":"))
            local = datetime.combine(local_date, time(hour, minute), tzinfo=self.zone)
            slots.append(local.astimezone(UTC))
        return slots

    def window_end(self, slot: datetime) -> datetime:
        """Acceptance ends at the window length or the slot's UTC hour end, if sooner."""
        hour_end = slot.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return min(slot + timedelta(minutes=self.window_minutes), hour_end)

    def due_slot(self, now: datetime) -> datetime | None:
        """The slot whose acceptance window contains ``now``, if any."""
        instant = _aware(now)
        local_date = instant.astimezone(self.zone).date()
        for day in (local_date - timedelta(days=1), local_date):
            for slot in self.slots_on(day):
                if slot <= instant < self.window_end(slot):
                    return slot
        return None

    def next_run(self, now: datetime) -> datetime:
        """The first slot strictly after ``now``."""
        instant = _aware(now)
        local_date = instant.astimezone(self.zone).date()
        for offset in range(0, 3):
            for slot in self.slots_on(local_date + timedelta(days=offset)):
                if slot > instant:
                    return slot
        raise RuntimeError("No schedule slot within two days")

    def describe(self, now: datetime) -> dict[str, Any]:
        due = self.due_slot(now)
        upcoming = self.next_run(now)
        return {
            "timezone": self.timezone,
            "local_times": list(self.times),
            "window_minutes": self.window_minutes,
            "due_slot": _iso(due) if due else None,
            "due_slot_local": due.astimezone(self.zone).isoformat() if due else None,
            "next_run": _iso(upcoming),
            "next_run_local": upcoming.astimezone(self.zone).isoformat(),
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
