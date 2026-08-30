"""Unit tests for Phase1ProductionAdapters production throttling
(residual review finding 3, Codex re-review t_791d2841).

Composition, not the individual acquisition helpers, is the actual
production wiring surface: ``Phase1ProductionAdapters`` is the sole
``Phase1SourcePort`` implementation, so it -- not
``observations.acquisition`` alone -- must guarantee that a real
process-wide rate limiter is always in effect, never silently
defaulting to unthrottled when the caller omits
``aviationweather_rate_limiter``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.phase1 import Phase1Request
from mesoforge.application.phase1_adapters import Phase1ProductionAdapters
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.observations.acquisition import RequestRateLimiter
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2030, 8, 28, 18, tzinfo=UTC)


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class _RecordingAviationWeatherTransport:
    """Deterministic in-process transport that always returns 200
    immediately -- exists solely to observe how many GET calls are
    made and to let a wired-in rate limiter/sleeper prove it actually
    gates them."""

    def __init__(self, *, stations, metar_records) -> None:
        self._stationinfo_body = json.dumps(
            [
                {
                    "icaoId": s.provider_icao_id,
                    "lat": s.expected_latitude,
                    "lon": s.expected_longitude,
                    "elev": s.expected_elevation_m,
                    "site": s.site_name,
                    "siteType": {"METAR": True},
                }
                for s in stations
            ]
        ).encode()
        self._metar_body = json.dumps(metar_records).encode()
        self.get_calls: list[str] = []

    def get(self, url, *, headers=None, timeout=None):
        self.get_calls.append(url)
        if "stationinfo" in url:
            return _FakeResponse(200, content=self._stationinfo_body)
        if "metar" in url:
            return _FakeResponse(200, content=self._metar_body)
        raise AssertionError(f"unexpected url {url!r}")

    def head(self, url, *, headers=None, timeout=None):
        raise AssertionError("head should not be called for AviationWeather acquisition")


class _ImmediateClock:
    """A clock that never moves on its own -- only ``advance()``
    (invoked by a real ``Sleeper.sleep``) changes it -- so a missing
    rate-limiter sleep is directly observable as zero elapsed time
    between two immediately-adjacent calls."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _RecordingSleeper:
    def __init__(self, clock: _ImmediateClock) -> None:
        self._clock = clock
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._clock.advance(seconds)


def _metar_records(stations, cycle):
    out = []
    for s in stations:
        out.append(
            {
                "icaoId": s.provider_icao_id,
                "obsTime": int(cycle.timestamp()),
                "reportTime": cycle.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "receiptTime": (cycle + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "temp": 10.0,
                "wdir": 0.0,
                "wspd": 0.0,
                "qcField": 0.0,
                "metarType": "METAR",
                "rawOb": s.provider_icao_id + " synthetic",
                "lat": s.expected_latitude,
                "lon": s.expected_longitude,
                "elev": s.expected_elevation_m,
            }
        )
    return out


def _load_phase1_configuration():
    cfg, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
    )
    assert cfg.phase1 is not None
    return cfg


def _infrastructure_and_request(cfg):
    """Build in-memory infrastructure, register the configuration
    snapshot through the real ConfigurationService, and return a
    matching Phase1Request -- mirroring how the acceptance test wires
    the same production adapters against real PostgreSQL/MinIO."""
    object_store = InMemoryObjectStore()
    uow_factory = InMemoryUnitOfWorkFactory()
    lock = InMemoryIdempotencyLock()
    service = ArtifactService(
        unit_of_work_factory=uow_factory, object_store=object_store, idempotency_lock=lock
    )
    snap = ConfigurationService(unit_of_work_factory=uow_factory).register(cfg)
    request = Phase1Request(
        run_id="run_00000000-0000-0000-0000-000000000001",
        configuration_snapshot_id=snap.configuration_snapshot_id,
        configuration_digest=snap.configuration_digest,
        code_revision="d" * 40,
        environment_digest="sha256:" + "e" * 64,
        lockfile_digest="sha256:" + "f" * 64,
        cycle=NOW,
        forecast_issue_time=NOW,
        information_cutoff=NOW,
        verification_cutoff=NOW + timedelta(hours=7),
    )
    return service, request


class TestDefaultAviationWeatherRateLimiterIsNeverUnthrottled:
    """MEDIUM residual review finding 3: Phase1ProductionAdapters must
    construct/use a shared limiter from the configured
    min_request_interval_seconds whenever the caller does not
    explicitly inject one -- production composition must never
    silently default to unthrottled."""

    def test_default_construction_still_throttles_stationinfo_then_metar(self) -> None:
        cfg = _load_phase1_configuration()
        service, request = _infrastructure_and_request(cfg)

        transport = _RecordingAviationWeatherTransport(
            stations=cfg.phase1.stations,
            metar_records=_metar_records(cfg.phase1.stations, NOW),
        )
        # Deliberately omit aviationweather_rate_limiter -- this is the
        # exact production-defaulting path the residual finding flags.
        adapters = Phase1ProductionAdapters(
            configuration=cfg.phase1,
            hrrr_transport=transport,  # unused by these two calls
            aviationweather_transport=transport,
        )
        clock = _ImmediateClock(NOW)
        sleeper = _RecordingSleeper(clock)

        station_catalog = adapters.register_station_catalog(
            request, artifact_service=service, clock=clock, sleeper=sleeper
        )
        adapters.register_metar_observations(
            request, station_catalog, artifact_service=service, clock=clock, sleeper=sleeper
        )

        # Two AviationWeather.gov calls were made back-to-back with an
        # unmoving clock (no real wall time passed) -- a genuinely
        # enforced shared limiter must have slept at least once for
        # (approximately) the configured minimum interval between
        # them. Previously, a bare ``None`` default meant zero sleeps.
        assert len(transport.get_calls) == 2
        assert sleeper.sleeps, (
            "default-constructed Phase1ProductionAdapters must still enforce "
            "min_request_interval_seconds across stationinfo/METAR calls"
        )
        assert sum(sleeper.sleeps) >= cfg.phase1.aviationweather.min_request_interval_seconds

    def test_explicitly_injected_limiter_is_used_verbatim(self) -> None:
        """An explicitly injected limiter (e.g. the acceptance/test
        fixture's scripted one) must still be the one actually used,
        not silently replaced by a second, independently constructed
        default limiter."""
        cfg = _load_phase1_configuration()

        transport = _RecordingAviationWeatherTransport(
            stations=cfg.phase1.stations,
            metar_records=_metar_records(cfg.phase1.stations, NOW),
        )
        injected_limiter = RequestRateLimiter(min_interval_seconds=1.0)
        adapters = Phase1ProductionAdapters(
            configuration=cfg.phase1,
            hrrr_transport=transport,
            aviationweather_transport=transport,
            aviationweather_rate_limiter=injected_limiter,
        )
        assert adapters._aviationweather_rate_limiter is injected_limiter
