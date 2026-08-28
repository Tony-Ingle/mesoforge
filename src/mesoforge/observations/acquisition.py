"""AviationWeather.gov acquisition orchestration (plan Section 2.4,
Task 8).

Depends only on ``observations.interfaces`` protocol shapes; unit
tests inject a scripted fake transport/clock/sleeper. No storage
import here -- returns plain typed result objects that
``application/phase1.py`` registers as source artifacts.
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
) -> FetchedResponse:
    """Section 2.4 HTTP handling: 200 retains the exact body even if
    empty; 204 retains a canonical empty-response artifact; 400/403/404
    are terminal; 429/500/502/504 and transport timeouts retry with the
    shared 1/2/4/8s policy and Retry-After cap."""
    retry_policy = settings.retry_policy
    last_status: int | None = None

    for attempt_index in range(retry_policy.attempts_per_endpoint):
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
            return FetchedResponse(
                url=url,
                status_code=response.status_code,
                payload=bytes(response.content),
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
) -> FetchedResponse:
    url = build_stationinfo_url(settings, station_ids=station_ids)
    return _fetch_with_retry(
        transport,
        clock,
        sleeper,
        url=url,
        headers=dict(request_headers(settings)),
        settings=settings,
    )


def acquire_metar_batch(
    settings: AviationWeatherSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    station_ids: tuple[str, ...],
    query_date: datetime,
) -> FetchedResponse:
    url = build_metar_url(settings, station_ids=station_ids, query_date=query_date)
    return _fetch_with_retry(
        transport,
        clock,
        sleeper,
        url=url,
        headers=dict(request_headers(settings)),
        settings=settings,
    )
