"""AviationWeather.gov provider adapter (plan Section 2.4, Task 8).

Pure URL/query construction with no I/O; the real transport is
``RequestsHrrrHttpTransport``-equivalent, wired in production only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlencode

from mesoforge.catalog.sources import AviationWeatherSettings


def build_stationinfo_url(
    settings: AviationWeatherSettings, *, station_ids: tuple[str, ...]
) -> str:
    params = {"ids": ",".join(station_ids), "format": "json"}
    return f"{settings.base_url}{settings.stationinfo_path}?{urlencode(params)}"


def build_metar_url(
    settings: AviationWeatherSettings,
    *,
    station_ids: tuple[str, ...],
    query_date: datetime,
) -> str:
    """Section 2.4: canonical query ordering is ``ids``, ``format``,
    ``date``, ``hours``. ``query_date`` is the cycle-plus-6h-plus-15m
    ISO8601 UTC instant supplied by the caller (the completion time of
    the six-hour forecast window)."""
    params: dict[str, str] = {}
    for key in settings.query_parameter_order:
        if key == "ids":
            params["ids"] = ",".join(station_ids)
        elif key == "format":
            params["format"] = "json"
        elif key == "date":
            params["date"] = query_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif key == "hours":
            params["hours"] = str(settings.metar_window_hours)
    query = "&".join(f"{key}={params[key]}" for key in settings.query_parameter_order)
    return f"{settings.base_url}{settings.metar_path}?{query}"


def request_headers(settings: AviationWeatherSettings) -> Mapping[str, str]:
    return {"User-Agent": settings.user_agent}


class RequestsAviationWeatherHttpTransport:
    """Concrete, real-network HTTP transport for AviationWeather.gov.
    Used only for production wiring and opt-in live smoke tests; never
    imported by default-CI tests (kept in its own module for the same
    import-isolation reason as
    ``guidance.sources.hrrr_transport.RequestsHrrrHttpTransport``)."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> object:
        return self._session.get(url, headers=dict(headers or {}), timeout=timeout)

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> object:
        return self._session.head(url, headers=dict(headers or {}), timeout=timeout)
