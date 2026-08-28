"""Unit tests for mesoforge.observations.sources.aviationweather (plan
Section 2.4, Task 8): exact URL construction and query ordering."""

from __future__ import annotations

from datetime import UTC, datetime

from mesoforge.catalog.sources import AviationWeatherSettings, RetryPolicy
from mesoforge.observations.sources.aviationweather import (
    build_metar_url,
    build_stationinfo_url,
    request_headers,
)

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=4,
    backoff_seconds=(1.0, 2.0, 4.0, 8.0),
    retry_after_cap_seconds=60.0,
)

_SETTINGS = AviationWeatherSettings(
    base_url="https://aviationweather.gov",
    stationinfo_path="/api/data/stationinfo",
    metar_path="/api/data/metar",
    query_parameter_order=("ids", "format", "date", "hours"),
    user_agent="MesoForge/0.1 (+https://example.invalid)",
    max_requests_per_minute=60,
    min_request_interval_seconds=1.0,
    metar_window_hours=6.5,
    metar_completion_offset_minutes=15.0,
    retry_policy=_RETRY,
)


class TestBuildStationinfoUrl:
    def test_builds_exact_url(self) -> None:
        url = build_stationinfo_url(_SETTINGS, station_ids=("KCBG", "KJMR", "KROS"))
        assert url == (
            "https://aviationweather.gov/api/data/stationinfo?ids=KCBG%2CKJMR%2CKROS&format=json"
        )


class TestBuildMetarUrl:
    def test_builds_exact_url_with_canonical_parameter_order(self) -> None:
        url = build_metar_url(
            _SETTINGS,
            station_ids=("KCBG", "KJMR", "KROS"),
            query_date=datetime(2026, 8, 29, 0, 15, tzinfo=UTC),
        )
        assert url == (
            "https://aviationweather.gov/api/data/metar?ids=KCBG,KJMR,KROS&format=json"
            "&date=2026-08-29T00:15:00Z&hours=6.5"
        )

    def test_parameter_order_matches_settings_exactly(self) -> None:
        url = build_metar_url(
            _SETTINGS,
            station_ids=("KCBG",),
            query_date=datetime(2026, 8, 29, 0, 15, tzinfo=UTC),
        )
        query = url.split("?", 1)[1]
        keys_in_order = [pair.split("=")[0] for pair in query.split("&")]
        assert keys_in_order == ["ids", "format", "date", "hours"]


class TestRequestHeaders:
    def test_includes_descriptive_user_agent(self) -> None:
        headers = request_headers(_SETTINGS)
        assert headers["User-Agent"] == _SETTINGS.user_agent
