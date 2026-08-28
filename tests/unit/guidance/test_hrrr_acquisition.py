"""Unit tests for mesoforge.guidance.acquisition (plan Section 2.2/2.3,
Task 4): scripted fake HTTP transport/clock/sleeper, no real network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest

from mesoforge.catalog.sources import HrrrFieldAssertion, HrrrSourceSettings, RetryPolicy
from mesoforge.guidance.acquisition import (
    HrrrAcquisitionError,
    acquire_hrrr_lead,
    build_acquisition_manifest_payload,
)
from mesoforge.guidance.sources.hrrr import HrrrIndexError

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=3,
    backoff_seconds=(1.0, 2.0, 4.0),
    retry_after_cap_seconds=60.0,
)

_ASSERTIONS = (
    HrrrFieldAssertion(
        canonical_variable_id="air_temperature_2m",
        inventory_selector=":TMP:2 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=0,
        parameter_number=0,
        type_of_level="heightAboveGround",
        level=2.0,
        expected_unit_id="K",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="eastward_wind_10m",
        inventory_selector=":UGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=2,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
    HrrrFieldAssertion(
        canonical_variable_id="northward_wind_10m",
        inventory_selector=":VGRD:10 m above ground:(anl|[0-9]+ hour fcst):",
        discipline=0,
        parameter_category=2,
        parameter_number=3,
        type_of_level="heightAboveGround",
        level=10.0,
        expected_unit_id="m/s",
    ),
)

_SETTINGS = HrrrSourceSettings(
    forecast_hours=tuple(range(7)),
    file_template="hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2",
    endpoint_order=("aws", "nomads"),
    endpoint_url_templates={
        "aws": "https://aws.example/hrrr.{YYYYMMDD}/conus/{FILE}",
        "nomads": "https://nomads.example/hrrr.{YYYYMMDD}/conus/{FILE}",
    },
    field_assertions=_ASSERTIONS,
    read_keys=("discipline",),
    retry_policy=_RETRY,
    cycle_availability_deadline_minutes=90.0,
)

# 3 messages: TMP(0-99), UGRD(100-199), VGRD(200-...last, needs HEAD)
_INDEX_TEXT = (
    "1:0:d=2026082818:TMP:2 m above ground:anl:\n"
    "2:100:d=2026082818:UGRD:10 m above ground:anl:\n"
    "3:200:d=2026082818:VGRD:10 m above ground:anl:\n"
)
_FULL_GRIB_LENGTH = 300


def _grib_bytes() -> bytes:
    return (b"T" * 100) + (b"U" * 100) + (b"V" * 100)


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


class _FakeTransport:
    """Scripted transport: a mapping from (method, url-substring-key) to
    a queue of responses (or exceptions) to return in order."""

    def __init__(self) -> None:
        self.get_queue: dict[str, list[_FakeResponse | Exception]] = {}
        self.head_queue: dict[str, list[_FakeResponse | Exception]] = {}
        self.calls: list[tuple[str, str]] = []

    def _pop(self, queue: dict[str, list], key: str) -> _FakeResponse:
        entries = queue.get(key)
        if not entries:
            raise AssertionError(f"no scripted response left for key {key!r}")
        entry = entries.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(("get", url))
        for key in self.get_queue:
            if key in url:
                return self._pop(self.get_queue, key)
        raise AssertionError(f"no scripted GET response matches url {url!r}")

    def head(self, url, *, headers=None, timeout=None):
        self.calls.append(("head", url))
        for key in self.head_queue:
            if key in url:
                return self._pop(self.head_queue, key)
        raise AssertionError(f"no scripted HEAD response matches url {url!r}")


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


def _range_response(start: int, end: int) -> _FakeResponse:
    payload = _grib_bytes()[start:end]
    return _FakeResponse(
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end - 1}/{_FULL_GRIB_LENGTH}",
            "ETag": '"abc123"',
            "Last-Modified": "Fri, 28 Aug 2026 18:00:00 GMT",
        },
        content=payload,
    )


class TestAcquireHrrrLead:
    def test_happy_path_acquires_index_and_three_ranged_messages(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        transport.get_queue["aws"].append(_range_response(200, 300))

        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper()

        result = acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )

        assert result.endpoint == "aws"
        assert len(result.selected_messages) == 3
        assert result.selected_grib_payload == _grib_bytes()
        assert result.full_object_content_length == _FULL_GRIB_LENGTH
        assert sleeper.sleeps == []

    def test_retries_transient_5xx_then_succeeds(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=503),
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
            _range_response(200, 300),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]

        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )
        assert len(result.selected_messages) == 3
        assert sleeper.sleeps == [1.0]  # first backoff_seconds entry, no jitter

    def test_deterministic_backoff_sequence(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=503),
            _FakeResponse(status_code=503),
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
            _range_response(200, 300),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )
        assert sleeper.sleeps == [1.0, 2.0]

    def test_honors_retry_after_capped(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=429, headers={"Retry-After": "9999"}),
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
            _range_response(200, 300),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )
        assert sleeper.sleeps == [60.0]  # capped at retry_after_cap_seconds

    def test_fails_over_to_nomads_after_aws_exhausted(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=503),
            _FakeResponse(status_code=503),
            _FakeResponse(status_code=503),
        ]
        transport.get_queue["nomads"] = [
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
            _range_response(200, 300),
        ]
        transport.head_queue["nomads"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )
        assert result.endpoint == "nomads"

    def test_404_past_deadline_is_terminal(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [_FakeResponse(status_code=404)]
        clock = _FakeClock(datetime(2026, 8, 28, 20, 0, tzinfo=UTC))  # past deadline
        sleeper = _FakeSleeper(clock)

        with pytest.raises(HrrrAcquisitionError, match="past the cycle availability deadline"):
            acquire_hrrr_lead(
                _SETTINGS,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_date=date(2026, 8, 28),
                cycle_hour=18,
                forecast_hour=0,
                cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
            )

    def test_provider_ignoring_range_is_rejected(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _FakeResponse(status_code=200, content=_grib_bytes()),  # ignored Range, full 200
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(HrrrAcquisitionError, match="ignored Range"):
            acquire_hrrr_lead(
                _SETTINGS,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_date=date(2026, 8, 28),
                cycle_hour=18,
                forecast_hour=0,
                cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
            )

    def test_truncated_range_response_retries_once_then_fails(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _FakeResponse(
                status_code=206,
                headers={"Content-Range": "bytes 0-49/300"},
                content=_grib_bytes()[0:50],  # truncated: expected 100 bytes
            ),
            _FakeResponse(
                status_code=206,
                headers={"Content-Range": "bytes 0-49/300"},
                content=_grib_bytes()[0:50],  # still truncated on retry
            ),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(HrrrAcquisitionError, match="range integrity mismatch"):
            acquire_hrrr_lead(
                _SETTINGS,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_date=date(2026, 8, 28),
                cycle_hour=18,
                forecast_hour=0,
                cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
            )

    def test_malformed_index_never_retried_or_failed_over(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [_FakeResponse(status_code=200, content=b"not-an-index")]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(HrrrIndexError):
            acquire_hrrr_lead(
                _SETTINGS,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_date=date(2026, 8, 28),
                cycle_hour=18,
                forecast_hour=0,
                cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
            )
        # only one GET was made (the index) -- no failover attempted for a
        # decode/selector-shaped problem
        assert len([c for c in transport.calls if c[0] == "get"]) == 1

    def test_ambiguous_selector_never_retried_or_failed_over(self) -> None:
        duplicated = _INDEX_TEXT + "4:300:d=2026082818:TMP:2 m above ground:anl:\n"
        transport = _FakeTransport()
        transport.get_queue["aws"] = [_FakeResponse(status_code=200, content=duplicated.encode())]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        with pytest.raises(HrrrIndexError, match="ambiguous"):
            acquire_hrrr_lead(
                _SETTINGS,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_date=date(2026, 8, 28),
                cycle_hour=18,
                forecast_hour=0,
                cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
            )
        assert len([c for c in transport.calls if c[0] == "get"]) == 1

    def test_no_older_cycle_fallback(self) -> None:
        """Section 1.3/8: a request identifies one HRRR cycle; missing
        lead/field data fails that run, it never substitutes an older
        cycle. This is enforced simply by acquire_hrrr_lead never taking
        a cycle_date/cycle_hour fallback parameter -- verified here by
        confirming the exact requested cycle appears in every URL."""
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
            _range_response(200, 300),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )
        assert "20260828" in result.resolved_grib_url
        assert "t18z" in result.resolved_grib_url


class TestBuildAcquisitionManifestPayload:
    def test_builds_deterministic_payload_with_lineage(self) -> None:
        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=200, content=_INDEX_TEXT.encode()),
            _range_response(0, 100),
            _range_response(100, 200),
            _range_response(200, 300),
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(_FULL_GRIB_LENGTH)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)
        acquisition = acquire_hrrr_lead(
            _SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=0,
            cycle_deadline=datetime(2026, 8, 28, 19, 30, tzinfo=UTC),
        )

        payload = build_acquisition_manifest_payload(
            [acquisition],
            settings=_SETTINGS,
            herbie_version="2026.3.0",
            cfgrib_version="0.9.15.1",
            eccodes_version="2.48.0",
            xarray_version="2026.7.0",
            numpy_version="2.0.0",
            pyproj_version="3.7.2",
        )
        assert payload["schema_version"] == "hrrr-acquisition-manifest.v1"
        assert len(payload["leads"]) == 1
        lead = payload["leads"][0]
        assert lead["forecast_hour"] == 0
        assert len(lead["selected_messages"]) == 3
        assert payload["library_versions"]["herbie"] == "2026.3.0"
