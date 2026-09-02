"""Unit tests for mesoforge.guidance.acquisition_v2 (plan Section
2.2-2.4): scripted fake HTTP transport/clock/sleeper, no real network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from mesoforge.guidance.acquisition_v2 import (
    acquire_gfs_lead,
    acquire_hrrr_phase2_lead,
    acquire_nbm_lead,
)
from tests.support.phase2_source_settings import (
    make_gfs_settings,
    make_hrrr_phase2_settings,
    make_nbm_settings,
)


def _grib2_section0(*, total_length: int) -> bytes:
    return b"GRIB" + b"\x00\x00" + b"\x00" + b"\x02" + total_length.to_bytes(8, "big")


_GRIB_TRAILER = b"7777"


def _grib2_message(fill: bytes, *, total_length: int = 100) -> bytes:
    section0 = _grib2_section0(total_length=total_length)
    interior_length = total_length - len(section0) - len(_GRIB_TRAILER)
    assert interior_length >= 0
    return section0 + fill * interior_length + _GRIB_TRAILER


@dataclass
class _FakeResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


class _FakeTransport:
    def __init__(self) -> None:
        self.get_queue: dict[str, list] = {}
        self.head_queue: dict[str, list] = {}
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


def _range_response(payload: bytes, start: int, end: int, total: int) -> _FakeResponse:
    return _FakeResponse(
        status_code=206,
        headers={"Content-Range": f"bytes {start}-{end - 1}/{total}"},
        content=payload[start:end],
    )


_NBM_SETTINGS = make_nbm_settings()


class TestAcquireNbmLead:
    def test_happy_path_acquires_index_and_seven_selected_messages(self) -> None:
        segments = [_grib2_message(bytes([i])) for i in range(7)]
        full = b"".join(segments)
        rows_text_lines = [
            "1:0:d=2026083012:TMP:2 m above ground:6 hour fcst:",
            "2:100:d=2026083012:DPT:2 m above ground:6 hour fcst:",
            "3:200:d=2026083012:WIND:10 m above ground:6 hour fcst:",
            "4:300:d=2026083012:WDIR:10 m above ground:6 hour fcst:",
            "5:400:d=2026083012:GUST:10 m above ground:6 hour fcst:",
            "6:500:d=2026083012:APCP:surface:5-6 hour acc fcst:",
            r"7:600:d=2026083012:APCP:surface:5-6 hour acc fcst:prob >0.254:"
            r"some probability forecast:",
        ]
        index_text = "\n".join(rows_text_lines) + "\n"
        total_length = len(full)

        transport = _FakeTransport()
        transport.get_queue["noaa-nbm"] = [
            _FakeResponse(status_code=200, content=index_text.encode()),
            *[_range_response(full, i * 100, (i + 1) * 100, total_length) for i in range(7)],
        ]
        transport.head_queue["noaa-nbm"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(total_length)})
        ]
        clock = _FakeClock(datetime(2026, 8, 30, 12, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_nbm_lead(
            _NBM_SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 30),
            cycle_hour=12,
            forecast_hour=6,
            cycle_deadline=datetime(2026, 8, 30, 14, 0, tzinfo=UTC),
        )
        assert result.model == "nbm"
        assert len(result.selected_messages) == 7
        grouped = result.payloads_by_variable()
        assert set(grouped) == {
            "air_temperature_2m",
            "dew_point_temperature_2m",
            "wind_speed_10m",
            "wind_from_direction_10m",
            "wind_gust_10m",
            "liquid_equivalent_precipitation_amount_1h",
            "probability_of_precipitation_1h",
        }
        # deterministic APCP and PoP01 kept as distinct payloads
        assert (
            grouped["liquid_equivalent_precipitation_amount_1h"]
            != grouped["probability_of_precipitation_1h"]
        )


_HRRR_SETTINGS = make_hrrr_phase2_settings()


class TestAcquireHrrrPhase2Lead:
    def test_happy_path_acquires_six_selected_messages(self) -> None:
        segments = [_grib2_message(bytes([i])) for i in range(6)]
        full = b"".join(segments)
        rows_text_lines = [
            "1:0:d=2026082818:TMP:2 m above ground:6 hour fcst:",
            "2:100:d=2026082818:DPT:2 m above ground:6 hour fcst:",
            "3:200:d=2026082818:UGRD:10 m above ground:6 hour fcst:",
            "4:300:d=2026082818:VGRD:10 m above ground:6 hour fcst:",
            "5:400:d=2026082818:GUST:surface:6 hour fcst:",
            "6:500:d=2026082818:APCP:surface:5-6 hour acc fcst:",
        ]
        index_text = "\n".join(rows_text_lines) + "\n"
        total_length = len(full)

        transport = _FakeTransport()
        transport.get_queue["aws"] = [
            _FakeResponse(status_code=200, content=index_text.encode()),
            *[_range_response(full, i * 100, (i + 1) * 100, total_length) for i in range(6)],
        ]
        transport.head_queue["aws"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(total_length)})
        ]
        clock = _FakeClock(datetime(2026, 8, 28, 18, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_hrrr_phase2_lead(
            _HRRR_SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 28),
            cycle_hour=18,
            forecast_hour=6,
            cycle_deadline=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        )
        assert result.model == "hrrr"
        assert len(result.selected_messages) == 6


_GFS_SETTINGS = make_gfs_settings()


class TestAcquireGfsLead:
    def test_dual_parent_apcp_retains_both_rows_at_early_lead(self) -> None:
        segments = [_grib2_message(bytes([i])) for i in range(7)]
        full = b"".join(segments)
        rows_text_lines = [
            "1:0:d=2026083000:TMP:2 m above ground:3 hour fcst:",
            "2:100:d=2026083000:DPT:2 m above ground:3 hour fcst:",
            "3:200:d=2026083000:UGRD:10 m above ground:3 hour fcst:",
            "4:300:d=2026083000:VGRD:10 m above ground:3 hour fcst:",
            "5:400:d=2026083000:GUST:surface:3 hour fcst:",
            "6:500:d=2026083000:APCP:surface:0-3 hour acc fcst:",
            "7:600:d=2026083000:APCP:surface:0-3 hour acc fcst:",
        ]
        index_text = "\n".join(rows_text_lines) + "\n"
        total_length = len(full)

        transport = _FakeTransport()
        transport.get_queue["gcs.example"] = [
            _FakeResponse(status_code=200, content=index_text.encode()),
            *[_range_response(full, i * 100, (i + 1) * 100, total_length) for i in range(7)],
        ]
        transport.head_queue["gcs.example"] = [
            _FakeResponse(status_code=200, headers={"Content-Length": str(total_length)})
        ]
        clock = _FakeClock(datetime(2026, 8, 30, 0, 5, tzinfo=UTC))
        sleeper = _FakeSleeper(clock)

        result = acquire_gfs_lead(
            _GFS_SETTINGS,
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            cycle_date=date(2026, 8, 30),
            cycle_hour=0,
            forecast_hour=3,
            cycle_deadline=datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
        )
        assert result.model == "gfs"
        grouped = result.payloads_by_variable()
        assert len(grouped["liquid_equivalent_precipitation_amount_1h"]) == 2
        apcp = [
            message
            for message in result.selected_messages
            if message.canonical_variable_id == "liquid_equivalent_precipitation_amount_1h"
        ]
        assert [message.row.message_number for message in apcp] == [6, 7]
        assert [message.byte_start for message in apcp] == [500, 600]
