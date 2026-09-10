"""AviationWeather.gov acquisition orchestration (plan Section 2.4,
Task 8).

Depends only on ``observations.interfaces`` protocol shapes; unit
tests inject a scripted fake transport/clock/sleeper. No storage
import here -- returns plain typed result objects for the application
layer to register as source artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mesoforge.catalog.sources import AviationWeatherSettings
from mesoforge.common.errors import MesoForgeError
from mesoforge.observations.interfaces import Clock, HttpResponse, HttpTransport, Sleeper
from mesoforge.observations.sources.aviationweather import (
    build_metar_url,
    build_stationinfo_url,
    request_headers,
)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 504})
_TERMINAL_STATUS_CODES = frozenset({400, 403, 404})

# Section 2.4/Codex review t_09a43c6c finding 6: AviationWeather.gov
# documents a 204 as "no data" for the query window -- the exact raw
# body a 204 carries is not contractually specified (some responses are
# empty bytes, others may include an incidental whitespace body), so a
# 204 is always canonicalized to the empty JSON array. This keeps the
# retained source artifact deterministic/content-addressed regardless
# of provider-side incidental byte variance, and lets downstream
# parsing (``parse_raw_metar_response``) treat 200-with-``[]`` and
# canonicalized-204 identically.
_CANONICAL_EMPTY_RESPONSE = b"[]"


class AviationWeatherAcquisitionError(MesoForgeError):
    """Terminal AviationWeather.gov acquisition failure: a
    400/403/404 request/configuration error, or all retries exhausted."""


@dataclass(frozen=True, slots=True)
class FetchedResponse:
    url: str
    status_code: int
    payload: bytes
    headers: dict[str, str]
    completed_at: datetime


class RequestRateLimiter:
    """Enforces at least ``min_interval_seconds`` between successive
    requests sharing this limiter instance (plan Section 2.4/Codex
    review t_09a43c6c finding 6: ``min_request_interval_seconds`` is
    validated configuration but must actually be enforced -- at
    minimum -- across the stationinfo and METAR calls of one process,
    using the injected ``Clock``/``Sleeper`` so unit tests remain
    deterministic (no real wall-clock sleep)."""

    def __init__(self, min_interval_seconds: float) -> None:
        if min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be positive")
        self._min_interval_seconds = min_interval_seconds
        self._last_request_at: datetime | None = None

    def wait(self, *, clock: Clock, sleeper: Sleeper) -> None:
        now = clock.now()
        if self._last_request_at is not None:
            elapsed_seconds = (now - self._last_request_at).total_seconds()
            remaining = self._min_interval_seconds - elapsed_seconds
            if remaining > 0:
                sleeper.sleep(remaining)
        self._last_request_at = clock.now()


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _parse_retry_after(headers: dict[str, str], cap_seconds: float) -> float | None:
    raw = _header(headers, "Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(int(raw))
    except ValueError:
        return None
    return min(seconds, cap_seconds)


def _fetch_with_retry(
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    *,
    url: str,
    headers: dict[str, str],
    settings: AviationWeatherSettings,
    rate_limiter: RequestRateLimiter | None = None,
) -> FetchedResponse:
    """Section 2.4 HTTP handling: 200 retains the exact body even if
    empty; 204 retains a canonical empty-response artifact; 400/403/404
    are terminal; 429/500/502/504 and transport timeouts retry with the
    shared 1/2/4/8s policy and Retry-After cap.

    Residual review finding (Codex review t_30309949): ``rate_limiter``
    -- when supplied -- is invoked immediately before *every* actual
    ``transport.get`` attempt, including the first request and every
    retry, not merely once before this function is entered. This keeps
    the limiter's last-attempt state accurate across the whole retry
    loop, so a provider ``Retry-After`` (even ``Retry-After: 0``) or a
    configured backoff step shorter than ``min_request_interval_seconds``
    can never let two actual attempts fire closer together than the
    configured minimum: the limiter tops up any shortfall after the
    provider-directed sleep, on top of -- never instead of -- honoring
    Retry-After/backoff.
    """
    retry_policy = settings.retry_policy
    last_status: int | None = None

    for attempt_index in range(retry_policy.attempts_per_endpoint):
        if rate_limiter is not None:
            rate_limiter.wait(clock=clock, sleeper=sleeper)
        try:
            response: HttpResponse = transport.get(
                url,
                headers=headers,
                timeout=(retry_policy.connect_timeout_seconds, retry_policy.read_timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001 -- any transport error is retryable here
            is_last = attempt_index == retry_policy.attempts_per_endpoint - 1
            if is_last:
                raise AviationWeatherAcquisitionError(
                    f"transport error fetching {url!r} after all retries: {exc}"
                ) from exc
            backoff = retry_policy.backoff_seconds[
                min(attempt_index, len(retry_policy.backoff_seconds) - 1)
            ]
            sleeper.sleep(backoff)
            continue

        if response.status_code in (200, 204):
            payload = (
                _CANONICAL_EMPTY_RESPONSE
                if response.status_code == 204
                else bytes(response.content)
            )
            return FetchedResponse(
                url=url,
                status_code=response.status_code,
                payload=payload,
                headers=dict(response.headers),
                completed_at=clock.now(),
            )

        last_status = response.status_code
        if response.status_code in _TERMINAL_STATUS_CODES:
            raise AviationWeatherAcquisitionError(
                f"terminal HTTP {response.status_code} for {url!r}"
            )
        if response.status_code not in _RETRYABLE_STATUS_CODES:
            raise AviationWeatherAcquisitionError(
                f"unexpected non-retryable HTTP {response.status_code} for {url!r}"
            )

        is_last = attempt_index == retry_policy.attempts_per_endpoint - 1
        if is_last:
            break
        retry_after = _parse_retry_after(
            dict(response.headers), retry_policy.retry_after_cap_seconds
        )
        backoff = retry_policy.backoff_seconds[
            min(attempt_index, len(retry_policy.backoff_seconds) - 1)
        ]
        sleeper.sleep(retry_after if retry_after is not None else backoff)

    raise AviationWeatherAcquisitionError(
        f"all retries exhausted for {url!r}; last_status={last_status!r}"
    )


def acquire_stationinfo(
    settings: AviationWeatherSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    station_ids: tuple[str, ...],
    rate_limiter: RequestRateLimiter | None = None,
) -> FetchedResponse:
    url = build_stationinfo_url(settings, station_ids=station_ids)
    return _fetch_with_retry(
        transport,
        clock,
        sleeper,
        url=url,
        headers=dict(request_headers(settings)),
        settings=settings,
        rate_limiter=rate_limiter,
    )


def acquire_metar_batch(
    settings: AviationWeatherSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    station_ids: tuple[str, ...],
    query_date: datetime,
    rate_limiter: RequestRateLimiter | None = None,
) -> FetchedResponse:
    url = build_metar_url(settings, station_ids=station_ids, query_date=query_date)
    return _fetch_with_retry(
        transport,
        clock,
        sleeper,
        url=url,
        headers=dict(request_headers(settings)),
        settings=settings,
        rate_limiter=rate_limiter,
    )
