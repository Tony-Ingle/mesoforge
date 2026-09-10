"""Shared deterministic HTTP response, METAR transport, and clock fixtures.

Phase 2 provider and acceptance tests use these without network access or real
wall-clock sleeps. The module path is retained for its existing consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class FakeHttpResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


class FixtureAviationWeatherTransport:
    """Deterministic in-process ``HttpTransport`` for AviationWeather.gov:
    routes by URL path (stationinfo vs metar) to a scripted response
    queue per path, so tests can script a 204-then-200 sequence, etc."""

    def __init__(self) -> None:
        self.stationinfo_queue: list[FakeHttpResponse] = []
        self.metar_queue: list[FakeHttpResponse] = []
        self.get_calls: list[str] = []

    def get(
        self, url: str, *, headers: dict[str, str] | None = None, timeout=None
    ) -> FakeHttpResponse:
        self.get_calls.append(url)
        if "stationinfo" in url:
            if not self.stationinfo_queue:
                raise AssertionError("no scripted stationinfo response left")
            return self.stationinfo_queue.pop(0)
        if "metar" in url:
            if not self.metar_queue:
                raise AssertionError("no scripted metar response left")
            return self.metar_queue.pop(0)
        raise AssertionError(f"no scripted response for url {url!r}")

    def head(
        self, url: str, *, headers: dict[str, str] | None = None, timeout=None
    ) -> FakeHttpResponse:
        raise AssertionError("head should not be called for AviationWeather acquisition")


class FixedClock:
    """A clock that starts at ``now`` and advances only when its bound
    sleeper's ``sleep`` is called (mirrors the unit-test fake clock/
    sleeper pairing so acceptance-level rate-limiter/backoff behavior is
    exercised deterministically, without a real wall-clock sleep)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingSleeper:
    """Advances the bound clock and records every sleep duration,
    instead of performing a real wall-clock sleep or asserting no sleep
    ever happens -- acceptance-level exercise of throttling/backoff
    (Codex review t_09a43c6c finding 6) legitimately sleeps."""

    def __init__(self, clock: FixedClock) -> None:
        self._clock = clock
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._clock.advance(seconds)
