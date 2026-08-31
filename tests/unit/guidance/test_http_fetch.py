"""Unit tests for mesoforge.guidance.http_fetch (plan Section 2.2/2.3,
Task 2): the shared HRRR/NBM/GFS deterministic retry/failover and
ranged-fetch engine extracted from ``guidance.acquisition``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.catalog.sources import RetryPolicy
from mesoforge.guidance.http_fetch import (
    FetchError,
    fetch_with_range,
    fetch_with_retry,
    validate_grib_message_boundaries,
)

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=2,
    backoff_seconds=(1.0, 2.0),
    retry_after_cap_seconds=60.0,
)

_DEADLINE = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)


@dataclass
class FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


class ScriptedTransport:
    def __init__(self) -> None:
        self.get_queue: dict[str, list[FakeResponse | Exception]] = {}
        self.get_calls: list[tuple[str, dict[str, str] | None]] = []

    def queue(self, url: str, response: FakeResponse | Exception) -> None:
        self.get_queue.setdefault(url, []).append(response)

    def get(self, url: str, *, headers: dict[str, str] | None = None, timeout=None):
        self.get_calls.append((url, dict(headers) if headers else None))
        queued = self.get_queue.get(url, [])
        if not queued:
            raise AssertionError(f"no scripted response left for {url!r}")
        item = queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def head(self, url: str, *, headers: dict[str, str] | None = None, timeout=None):
        return self.get(url, headers=headers, timeout=timeout)


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingSleeper:
    def __init__(self, clock: FixedClock) -> None:
        self._clock = clock
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._clock.advance(seconds)


def _grib_message(payload_body: bytes = b"\x00" * 20) -> bytes:
    body = payload_body
    total_length = 16 + len(body) + 4
    header = b"GRIB" + b"\x00\x00" + bytes([0]) + bytes([2]) + total_length.to_bytes(8, "big")
    return header + body + b"7777"


class TestFetchWithRetry:
    def test_returns_on_first_success(self) -> None:
        transport = ScriptedTransport()
        transport.queue("https://a/x.idx", FakeResponse(200, content=b"ok"))
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        result = fetch_with_retry(
            transport,
            clock,
            sleeper,
            method="get",
            urls_by_endpoint=[("a", "https://a/x.idx")],
            retry_policy=_RETRY,
            cycle_deadline=_DEADLINE,
        )
        assert result.payload == b"ok"
        assert sleeper.sleeps == []

    def test_retries_on_retryable_status_then_succeeds(self) -> None:
        transport = ScriptedTransport()
        transport.queue("https://a/x.idx", FakeResponse(503))
        transport.queue("https://a/x.idx", FakeResponse(200, content=b"ok"))
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        result = fetch_with_retry(
            transport,
            clock,
            sleeper,
            method="get",
            urls_by_endpoint=[("a", "https://a/x.idx")],
            retry_policy=_RETRY,
            cycle_deadline=_DEADLINE,
        )
        assert result.payload == b"ok"
        assert sleeper.sleeps == [1.0]

    def test_fails_over_to_second_endpoint(self) -> None:
        transport = ScriptedTransport()
        transport.queue("https://a/x.idx", FakeResponse(503))
        transport.queue("https://a/x.idx", FakeResponse(503))
        transport.queue("https://b/x.idx", FakeResponse(200, content=b"ok-b"))
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        result = fetch_with_retry(
            transport,
            clock,
            sleeper,
            method="get",
            urls_by_endpoint=[("a", "https://a/x.idx"), ("b", "https://b/x.idx")],
            retry_policy=_RETRY,
            cycle_deadline=_DEADLINE,
        )
        assert result.endpoint == "b"
        assert result.payload == b"ok-b"

    def test_non_retryable_status_raises_immediately(self) -> None:
        transport = ScriptedTransport()
        transport.queue("https://a/x.idx", FakeResponse(403))
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        with pytest.raises(FetchError, match="non-retryable"):
            fetch_with_retry(
                transport,
                clock,
                sleeper,
                method="get",
                urls_by_endpoint=[("a", "https://a/x.idx")],
                retry_policy=_RETRY,
                cycle_deadline=_DEADLINE,
            )

    def test_404_past_deadline_raises(self) -> None:
        transport = ScriptedTransport()
        transport.queue("https://a/x.idx", FakeResponse(404))
        clock = FixedClock(_DEADLINE + timedelta(minutes=1))
        sleeper = RecordingSleeper(clock)
        with pytest.raises(FetchError, match="past the cycle availability deadline"):
            fetch_with_retry(
                transport,
                clock,
                sleeper,
                method="get",
                urls_by_endpoint=[("a", "https://a/x.idx")],
                retry_policy=_RETRY,
                cycle_deadline=_DEADLINE,
            )

    def test_all_exhausted_raises(self) -> None:
        transport = ScriptedTransport()
        transport.queue("https://a/x.idx", FakeResponse(503))
        transport.queue("https://a/x.idx", FakeResponse(503))
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        with pytest.raises(FetchError, match="exhausted"):
            fetch_with_retry(
                transport,
                clock,
                sleeper,
                method="get",
                urls_by_endpoint=[("a", "https://a/x.idx")],
                retry_policy=_RETRY,
                cycle_deadline=_DEADLINE,
            )


class TestValidateGribMessageBoundaries:
    def test_accepts_valid_message(self) -> None:
        message = _grib_message()
        validate_grib_message_boundaries(message, url="u", range_header="r")

    def test_rejects_wrong_magic(self) -> None:
        message = b"XXXX" + _grib_message()[4:]
        with pytest.raises(FetchError, match="GRIB2.*indicator"):
            validate_grib_message_boundaries(message, url="u", range_header="r")

    def test_rejects_wrong_edition(self) -> None:
        body = b"\x00" * 20
        total_length = 16 + len(body) + 4
        header = b"GRIB" + b"\x00\x00" + bytes([0]) + bytes([1]) + total_length.to_bytes(8, "big")
        message = header + body + b"7777"
        with pytest.raises(FetchError, match="edition"):
            validate_grib_message_boundaries(message, url="u", range_header="r")

    def test_rejects_length_mismatch(self) -> None:
        message = _grib_message() + b"extra"
        with pytest.raises(FetchError, match="total message length"):
            validate_grib_message_boundaries(message, url="u", range_header="r")

    def test_rejects_missing_trailer(self) -> None:
        message = _grib_message()[:-4] + b"XXXX"
        with pytest.raises(FetchError, match="end section"):
            validate_grib_message_boundaries(message, url="u", range_header="r")

    def test_rejects_too_short(self) -> None:
        with pytest.raises(FetchError, match="too short"):
            validate_grib_message_boundaries(b"GRIB", url="u", range_header="r")


class TestFetchWithRange:
    def test_successful_ranged_fetch(self) -> None:
        message = _grib_message()
        transport = ScriptedTransport()
        transport.queue(
            "https://a/x.grib2",
            FakeResponse(
                206,
                headers={"Content-Range": f"bytes 0-{len(message) - 1}/{len(message)}"},
                content=message,
            ),
        )
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        result = fetch_with_range(
            transport,
            clock,
            sleeper,
            endpoint="a",
            url="https://a/x.grib2",
            range_header=f"bytes=0-{len(message) - 1}",
            byte_start=0,
            byte_end=len(message),
            retry_policy=_RETRY,
            cycle_deadline=_DEADLINE,
            expected_length=len(message),
            full_object_length=len(message),
        )
        assert result.payload == message

    def test_integrity_mismatch_retries_once_then_succeeds(self) -> None:
        message = _grib_message()
        transport = ScriptedTransport()
        # First attempt: wrong Content-Range (integrity failure)
        transport.queue(
            "https://a/x.grib2",
            FakeResponse(
                206,
                headers={"Content-Range": f"bytes 0-{len(message) - 2}/{len(message)}"},
                content=message[:-1],
            ),
        )
        # Retry: correct
        transport.queue(
            "https://a/x.grib2",
            FakeResponse(
                206,
                headers={"Content-Range": f"bytes 0-{len(message) - 1}/{len(message)}"},
                content=message,
            ),
        )
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        result = fetch_with_range(
            transport,
            clock,
            sleeper,
            endpoint="a",
            url="https://a/x.grib2",
            range_header=f"bytes=0-{len(message) - 1}",
            byte_start=0,
            byte_end=len(message),
            retry_policy=_RETRY,
            cycle_deadline=_DEADLINE,
            expected_length=len(message),
            full_object_length=len(message),
        )
        assert result.payload == message

    def test_provider_ignoring_range_raises(self) -> None:
        message = _grib_message()
        transport = ScriptedTransport()
        transport.queue("https://a/x.grib2", FakeResponse(200, content=message))
        clock = FixedClock(_DEADLINE - timedelta(hours=1))
        sleeper = RecordingSleeper(clock)
        with pytest.raises(FetchError, match="ignored Range"):
            fetch_with_range(
                transport,
                clock,
                sleeper,
                endpoint="a",
                url="https://a/x.grib2",
                range_header="bytes=0-1",
                byte_start=0,
                byte_end=len(message),
                retry_policy=_RETRY,
                cycle_deadline=_DEADLINE,
                expected_length=len(message),
                full_object_length=len(message),
            )
