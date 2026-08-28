"""Deterministic, in-process HTTP transport fixtures for HRRR and
AviationWeather.gov acquisition (plan Section 5.3: transport-level test
doubles, not a parallel domain-port implementation).

These implement exactly the ``guidance.interfaces.HttpTransport`` /
``observations.interfaces.HttpTransport`` structural protocols (``get``/
``head`` returning an object with ``status_code``/``headers``/``content``)
so ``mesoforge.application.phase1_adapters.Phase1ProductionAdapters`` --
the one production port implementation -- can be exercised end to end
without any real network access. Used by the acceptance proof (Codex
review t_09a43c6c finding 4: the acceptance test must inject a
deterministic fixture transport into the production adapters, not
reimplement the ports as a test-local double).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mesoforge.catalog.sources import HrrrSourceSettings
from tests.fixtures.hrrr_grib import make_temperature_message, make_wind_message

_FORECAST_HOUR_RE = re.compile(r"wrfsfcf(\d\d)")
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")


@dataclass
class FakeHttpResponse:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""


def _step_descriptor(forecast_hour: int) -> str:
    return "anl" if forecast_hour == 0 else f"{forecast_hour} hour fcst"


@dataclass(frozen=True, slots=True)
class HrrrLeadFixture:
    forecast_hour: int
    temperature_bytes: bytes
    eastward_bytes: bytes
    northward_bytes: bytes

    @property
    def full_bytes(self) -> bytes:
        return self.temperature_bytes + self.eastward_bytes + self.northward_bytes

    @property
    def full_length(self) -> int:
        return len(self.full_bytes)

    def index_text(self, *, cycle_date: str, cycle_hour: int) -> str:
        step = _step_descriptor(self.forecast_hour)
        date_tag = f"d={cycle_date}{cycle_hour:02d}"
        temperature_offset = 0
        eastward_offset = len(self.temperature_bytes)
        northward_offset = eastward_offset + len(self.eastward_bytes)
        return (
            f"1:{temperature_offset}:{date_tag}:TMP:2 m above ground:{step}:\n"
            f"2:{eastward_offset}:{date_tag}:UGRD:10 m above ground:{step}:\n"
            f"3:{northward_offset}:{date_tag}:VGRD:10 m above ground:{step}:\n"
        )

    def byte_range(self, canonical_variable_id: str) -> tuple[int, int]:
        if canonical_variable_id == "air_temperature_2m":
            return 0, len(self.temperature_bytes)
        if canonical_variable_id == "eastward_wind_10m":
            start = len(self.temperature_bytes)
            return start, start + len(self.eastward_bytes)
        if canonical_variable_id == "northward_wind_10m":
            start = len(self.temperature_bytes) + len(self.eastward_bytes)
            return start, start + len(self.northward_bytes)
        raise ValueError(f"unknown canonical_variable_id {canonical_variable_id!r}")


def build_hrrr_lead_fixture(
    *,
    forecast_hour: int,
    temperature_k,
    eastward_wind_m_s,
    northward_wind_m_s,
    cycle_date: str,
    cycle_hour: int,
    grid_relative_wind: bool = True,
) -> HrrrLeadFixture:
    return HrrrLeadFixture(
        forecast_hour=forecast_hour,
        temperature_bytes=make_temperature_message(
            forecast_hour=forecast_hour,
            values_k=temperature_k,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        ),
        eastward_bytes=make_wind_message(
            forecast_hour=forecast_hour,
            component="u",
            values_m_s=eastward_wind_m_s,
            grid_relative=grid_relative_wind,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        ),
        northward_bytes=make_wind_message(
            forecast_hour=forecast_hour,
            component="v",
            values_m_s=northward_wind_m_s,
            grid_relative=grid_relative_wind,
            cycle_date=cycle_date,
            cycle_hour=cycle_hour,
        ),
    )


class FixtureHrrrTransport:
    """Deterministic in-process ``HttpTransport`` serving pinned index/
    ranged-GRIB responses for every configured forecast lead, honoring
    the exact ``Range`` header sent by ``guidance.acquisition``
    (Codex review t_09a43c6c finding 1 regression coverage at the
    acceptance-test layer, not merely the unit-test layer)."""

    def __init__(
        self,
        *,
        settings: HrrrSourceSettings,
        leads: dict[int, HrrrLeadFixture],
        cycle_date: str,
        cycle_hour: int,
    ) -> None:
        self._settings = settings
        self._leads = leads
        self._cycle_date = cycle_date
        self._cycle_hour = cycle_hour
        self.get_calls: list[tuple[str, dict[str, str] | None]] = []
        self.head_calls: list[str] = []

    def _lead_for_url(self, url: str) -> HrrrLeadFixture:
        match = _FORECAST_HOUR_RE.search(url)
        if match is None:
            raise AssertionError(f"could not parse forecast hour from url {url!r}")
        forecast_hour = int(match.group(1))
        return self._leads[forecast_hour]

    def get(
        self, url: str, *, headers: dict[str, str] | None = None, timeout=None
    ) -> FakeHttpResponse:
        self.get_calls.append((url, dict(headers) if headers else None))
        lead = self._lead_for_url(url)

        if url.endswith(self._settings.index_suffix):
            body = lead.index_text(cycle_date=self._cycle_date, cycle_hour=self._cycle_hour)
            return FakeHttpResponse(status_code=200, content=body.encode())

        range_header = (headers or {}).get("Range")
        if range_header is None:
            raise AssertionError(f"ranged GRIB GET to {url!r} must carry a Range header")
        match = _RANGE_RE.fullmatch(range_header)
        if match is None:
            raise AssertionError(f"unexpected Range header format: {range_header!r}")
        start, end_inclusive = int(match.group(1)), int(match.group(2))
        payload = lead.full_bytes[start : end_inclusive + 1]
        return FakeHttpResponse(
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end_inclusive}/{lead.full_length}",
                "ETag": f'"lead-{lead.forecast_hour}"',
                "Last-Modified": "Fri, 28 Aug 2026 18:00:00 GMT",
            },
            content=payload,
        )

    def head(
        self, url: str, *, headers: dict[str, str] | None = None, timeout=None
    ) -> FakeHttpResponse:
        self.head_calls.append(url)
        lead = self._lead_for_url(url)
        return FakeHttpResponse(
            status_code=200,
            headers={
                "Content-Length": str(lead.full_length),
                "ETag": f'"lead-{lead.forecast_hour}"',
                "Last-Modified": "Fri, 28 Aug 2026 18:00:00 GMT",
            },
        )


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


class NoOpSleeper:
    def sleep(self, seconds: float) -> None:
        raise AssertionError(f"unexpected sleep({seconds!r}) on the happy path")
