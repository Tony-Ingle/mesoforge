"""Unit tests for mesoforge.observations.acquisition (plan Section
2.4, Task 8): scripted fake transport/clock/sleeper, no real network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.catalog.sources import AviationWeatherSettings, RetryPolicy
from mesoforge.observations.acquisition import (
    AviationWeatherAcquisitionError,
    RequestRateLimiter,
    acquire_metar_batch,
    acquire_stationinfo,
)

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=3,
    backoff_seconds=(1.0, 2.0, 4.0),
    retry_after_cap_seconds=60.0,
)

_SETTINGS = AviationWeatherSettings(
    base_url="https://aviationweather.gov",
    stationinfo_path="/api/data/stationinfo",
    metar_path="/api/data/metar",
    query_parameter_order=("ids", "format", "date", "hours"),
    user_agent="MesoForge/0.1",
    max_requests_per_minute=60,
    min_request_interval_seconds=1.0,
    metar_window_hours=6.5,
    metar_completion_offset_minutes=15.0,
    retry_policy=_RETRY,
)


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


class _FakeTransport:
    def __init__(self) -> None:
        self.get_queue: list[_FakeResponse | Exception] = []
        self.calls: list[str] = []

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(url)
        if not self.get_queue:
            raise AssertionError("no scripted response left")
        entry = self.get_queue.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def head(self, url, *, headers=None, timeout=None):
        raise AssertionError("head should not be called for AviationWeather acquisition")


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _FakeSleeper:
    def __init__(self, clock: _FakeClock | None = None) -> None:
        self.sleeps: list[float] = []
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


class TestAcquireStationinfo:
    def test_happy_path(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [_FakeResponse(status_code=200, content=b'{"ok":true}')]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper()

        result = acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG", "KJMR", "KROS"),
        )
        assert result.status_code == 200
        assert result.payload == b'{"ok":true}'

    def test_retries_429_then_succeeds(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [
            _FakeResponse(status_code=429, headers={"Retry-After": "2"}),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_stationinfo(
            _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
        )
        assert result.status_code == 200
        assert sleeper.sleeps == [2.0]

    def test_404_is_terminal(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [_FakeResponse(status_code=404)]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(AviationWeatherAcquisitionError, match="terminal"):
            acquire_stationinfo(
                _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
            )
        assert len(transport.calls) == 1  # no retry on a terminal status

    def test_400_is_terminal(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [_FakeResponse(status_code=400)]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(AviationWeatherAcquisitionError, match="terminal"):
            acquire_stationinfo(
                _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
            )

    def test_204_retains_empty_response(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [_FakeResponse(status_code=204, content=b"")]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_stationinfo(
            _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
        )
        assert result.status_code == 204
        assert result.payload == b"[]"

    def test_204_canonicalizes_incidental_body_bytes(self) -> None:
        """Review finding 6: 204 responses must be canonicalized
        regardless of any incidental raw body bytes the provider
        actually sends, so the retained source artifact is
        deterministic across provider-side variance."""
        transport = _FakeTransport()
        transport.get_queue = [_FakeResponse(status_code=204, content=b"   ")]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_stationinfo(
            _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
        )
        assert result.status_code == 204
        assert result.payload == b"[]"

    def test_all_retries_exhausted_raises(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [
            _FakeResponse(status_code=500),
            _FakeResponse(status_code=500),
            _FakeResponse(status_code=500),
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(AviationWeatherAcquisitionError, match="exhausted"):
            acquire_stationinfo(
                _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
            )

    def test_retry_after_capped(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [
            _FakeResponse(status_code=429, headers={"Retry-After": "9999"}),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        acquire_stationinfo(
            _SETTINGS, transport=transport, clock=clock, sleeper=sleeper, station_ids=("KCBG",)
        )
        assert sleeper.sleeps == [60.0]

    def test_no_credentials_used(self) -> None:
        """Section: 'no credentials are used' -- request_headers only
        ever includes User-Agent, never an auth header."""
        from mesoforge.observations.sources.aviationweather import request_headers

        headers = request_headers(_SETTINGS)
        assert set(headers.keys()) == {"User-Agent"}


class TestAcquireMetarBatch:
    def test_happy_path(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [_FakeResponse(status_code=200, content=b"[]")]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
        sleeper = _FakeSleeper()

        result = acquire_metar_batch(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG", "KJMR", "KROS"),
            query_date=datetime(2026, 8, 28, 18, 15, tzinfo=UTC),
        )
        assert result.status_code == 200
        assert "ids=KCBG,KJMR,KROS" in transport.calls[0]


class TestRequestRateLimiter:
    """MEDIUM review finding 6: max_requests_per_minute/
    min_request_interval_seconds are validated config but must
    actually be enforced across stationinfo/METAR calls, using the
    injected Clock/Sleeper (never real wall-clock sleep) so tests stay
    deterministic."""

    def test_back_to_back_stationinfo_calls_sleep_the_configured_interval(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [
            _FakeResponse(status_code=200, content=b"[]"),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)
        limiter = RequestRateLimiter(min_interval_seconds=1.0)

        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )
        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )
        # First call: no prior request, no sleep. Second call arrives
        # immediately after (clock unchanged by the first call besides
        # its own bookkeeping), so it must sleep the full configured
        # interval before issuing its GET.
        assert sleeper.sleeps == [1.0]
        assert len(transport.calls) == 2

    def test_no_sleep_when_interval_already_elapsed(self) -> None:
        transport = _FakeTransport()
        transport.get_queue = [
            _FakeResponse(status_code=200, content=b"[]"),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC))
        sleeper = _FakeSleeper()
        limiter = RequestRateLimiter(min_interval_seconds=1.0)

        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )
        clock.advance(2.0)  # well past the configured interval
        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )
        assert sleeper.sleeps == []

    def test_shared_limiter_enforced_across_stationinfo_and_metar_calls(self) -> None:
        """The limiter must be enforced across *both* stationinfo and
        METAR calls when the caller shares one instance across the
        whole process, per review finding 6 ('across stationinfo/METAR
        calls')."""
        transport = _FakeTransport()
        transport.get_queue = [
            _FakeResponse(status_code=200, content=b"[]"),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)
        limiter = RequestRateLimiter(min_interval_seconds=1.0)

        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )
        acquire_metar_batch(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            query_date=datetime(2026, 8, 28, 18, 15, tzinfo=UTC),
            rate_limiter=limiter,
        )
        assert sleeper.sleeps == [1.0]

    def test_rejects_nonpositive_interval(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RequestRateLimiter(min_interval_seconds=0.0)

    def test_retry_after_zero_does_not_bypass_minimum_interval(self) -> None:
        """Codex review t_30309949: a valid ``Retry-After: 0`` (or any
        value below ``min_request_interval_seconds``) must never let
        the retried attempt fire sooner than the configured minimum --
        the shared limiter, invoked before every actual transport
        attempt, tops up the gap on top of the provider's Retry-After.
        """
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC))
        attempt_times: list[datetime] = []

        class _RecordingTransport(_FakeTransport):
            def get(self, url, *, headers=None, timeout=None):
                attempt_times.append(clock.now())
                return super().get(url, headers=headers, timeout=timeout)

        transport = _RecordingTransport()
        transport.get_queue = [
            _FakeResponse(status_code=429, headers={"Retry-After": "0"}),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        sleeper = _FakeSleeper(clock)
        limiter = RequestRateLimiter(min_interval_seconds=1.0)

        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )

        assert len(attempt_times) == 2
        delta_seconds = (attempt_times[1] - attempt_times[0]).total_seconds()
        assert delta_seconds >= _SETTINGS.min_request_interval_seconds

    def test_subsequent_call_after_a_retry_still_respects_minimum_interval(self) -> None:
        """Codex review t_30309949: after a retried acquisition
        completes, the very next acquisition call sharing the same
        limiter must still be spaced by at least
        ``min_request_interval_seconds`` from the last *actual*
        attempt (the retry), not merely from the call that returned
        the retryable status.
        """
        clock = _FakeClock(datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC))
        attempt_times: list[datetime] = []

        class _RecordingTransport(_FakeTransport):
            def get(self, url, *, headers=None, timeout=None):
                attempt_times.append(clock.now())
                return super().get(url, headers=headers, timeout=timeout)

        transport = _RecordingTransport()
        transport.get_queue = [
            _FakeResponse(status_code=429, headers={"Retry-After": "0"}),
            _FakeResponse(status_code=200, content=b"[]"),
            _FakeResponse(status_code=200, content=b"[]"),
        ]
        sleeper = _FakeSleeper(clock)
        limiter = RequestRateLimiter(min_interval_seconds=1.0)

        acquire_stationinfo(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            rate_limiter=limiter,
        )
        acquire_metar_batch(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            station_ids=("KCBG",),
            query_date=datetime(2026, 8, 28, 18, 15, tzinfo=UTC),
            rate_limiter=limiter,
        )

        assert len(attempt_times) == 3
        for earlier, later in zip(attempt_times, attempt_times[1:], strict=False):
            delta_seconds = (later - earlier).total_seconds()
            assert delta_seconds >= _SETTINGS.min_request_interval_seconds
