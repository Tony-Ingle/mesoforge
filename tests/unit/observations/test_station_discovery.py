"""Focused offline checks for the official coordinate-driven station query."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from pyproj import Geod

from mesoforge.observations.sources.stationinfo import (
    BoundedStationInfoTransport,
    StationDiscoveryError,
    build_stationinfo_bbox_url,
    discover_metar_stations,
    parse_stationinfo_candidates,
    station_query_bounds,
)
from tests.unit.observations.test_aviationweather_acquisition import (
    _SETTINGS,
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
)

LAT, LON = 36.7378, -119.7871
GEOD = Geod(ellps="WGS84")


def station(identifier="KFAT", *, distance_m=1000.0, azimuth=90.0, **overrides):
    lon, lat, _ = GEOD.fwd(LON, LAT, azimuth, distance_m)
    return {
        "icaoId": identifier,
        "lat": lat,
        "lon": lon,
        "elev": 101,
        "site": "Example airport",
        "siteType": ["METAR", "TAF"],
        "priority": 5,
        **overrides,
    }


def test_bbox_uses_official_lat_lon_order_and_contains_geodesic_circle():
    bounds = station_query_bounds(LAT, LON)
    assert len(bounds) == 1
    south, west, north, east = bounds[0]
    for azimuth in range(360):
        lon, lat, _ = GEOD.fwd(LON, LAT, azimuth, 50_000)
        assert south <= lat <= north
        assert west <= lon <= east
    query = parse_qs(urlparse(build_stationinfo_bbox_url(_SETTINGS, bounds[0])).query)
    assert list(map(float, query["bbox"][0].split(","))) == list(bounds[0])
    assert query == {"bbox": [",".join(str(value) for value in bounds[0])], "format": ["json"]}
    assert "ids" not in query


def test_radius_filter_keeps_inside_and_rejects_outside_and_non_metar():
    payload = json.dumps(
        [
            station("KBBB", distance_m=49_999.99),
            station("KAAA", distance_m=1000),
            station("KOUT", distance_m=50_000.01),
            station("KTAF", siteType=["TAF"]),
        ]
    ).encode()
    candidates, excluded = parse_stationinfo_candidates(payload, latitude=LAT, longitude=LON)
    assert [row["station_id"] for row in candidates] == ["KBBB", "KAAA"]
    assert candidates[0]["distance_km"] == pytest.approx(49.99999, abs=1e-8)
    assert candidates[1]["distance_km"] == pytest.approx(1.0, abs=1e-8)
    assert candidates[1]["network"] == "METAR"
    assert candidates[1]["elevation_m"] == 101.0
    assert candidates[1]["site_types"] == ["METAR", "TAF"]
    assert [(row["station_id"], row["reason"]) for row in excluded] == [
        ("KOUT", "outside_search_radius"),
        ("KTAF", "not_metar_capable"),
    ]


def test_optional_metadata_stays_explicitly_unknown():
    record = station()
    for key in ("elev", "site", "priority"):
        record.pop(key)
    candidates, _ = parse_stationinfo_candidates(
        json.dumps([record]).encode(), latitude=LAT, longitude=LON
    )
    assert candidates[0]["elevation_m"] is None
    assert candidates[0]["site_name"] is None
    assert candidates[0]["provider_priority"] is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{broken", "valid JSON"),
        (b'{"error":"provider failed"}', "JSON array"),
        (b"[null]", "entries must be objects"),
        (json.dumps([station(lat=None)]).encode(), "finite number"),
        (json.dumps([station(lat=True)]).encode(), "finite number"),
        (json.dumps([station(lon=181.0)]).encode(), "out-of-range"),
        (json.dumps([station(elev=float("nan"))]).encode(), "finite number"),
        (json.dumps([station(siteType="METAR")]).encode(), "siteType"),
        (json.dumps([station(icaoId="../")]).encode(), "ICAO"),
        (json.dumps([station()] * 400).encode(), "truncated"),
        (b" " * (2 * 1024 * 1024 + 1), "2 MiB"),
    ],
    ids=[
        "invalid_json",
        "error_object",
        "null_record",
        "null_latitude",
        "boolean_latitude",
        "invalid_longitude",
        "nonfinite_elevation",
        "bad_site_types",
        "bad_identifier",
        "provider_cap",
        "response_budget",
    ],
)
def test_invalid_or_potentially_truncated_metadata_fails_closed(payload, message):
    with pytest.raises(StationDiscoveryError, match=message):
        parse_stationinfo_candidates(payload, latitude=LAT, longitude=LON)


@pytest.mark.parametrize("latitude,longitude", [(91, 0), (0, -181), (True, 0), (0, float("inf"))])
def test_invalid_coordinate_never_queries_provider(latitude, longitude):
    transport = _FakeTransport()
    with pytest.raises(StationDiscoveryError):
        discover_metar_stations(
            _SETTINGS,
            latitude=latitude,
            longitude=longitude,
            transport=transport,
            clock=_FakeClock(datetime(2026, 9, 10, tzinfo=UTC)),
            sleeper=_FakeSleeper(),
        )
    assert transport.calls == []


def test_discovery_reuses_retry_engine_and_retains_exact_raw_metadata():
    transport = _FakeTransport()
    payload = json.dumps([station("KBBB", distance_m=2000), station("KAAA")]).encode()
    transport.get_queue = [
        _FakeResponse(429, headers={"Retry-After": "2"}),
        _FakeResponse(200, content=payload, headers={"ETag": '"metadata-revision"'}),
    ]
    clock = _FakeClock(datetime(2026, 9, 10, tzinfo=UTC))
    sleeper = _FakeSleeper(clock)
    result = discover_metar_stations(
        _SETTINGS, latitude=LAT, longitude=LON, transport=transport, clock=clock, sleeper=sleeper
    )
    assert len(transport.calls) == 2
    assert sleeper.sleeps == [2.0]
    assert [row["station_id"] for row in result.candidates] == ["KAAA", "KBBB"]
    assert result.responses[0].payload == payload
    assert result.responses[0].headers == {"ETag": '"metadata-revision"'}
    assert result.responses[0].url == transport.calls[-1]
    assert result.responses[0].completed_at == clock.now()


def test_dateline_is_two_small_queries_and_merges_without_duplicate_stations():
    latitude, longitude = 20.0, 179.9
    record = station("PXXX", lat=latitude, lon=180.0)
    payload = json.dumps([record]).encode()
    transport = _FakeTransport()
    transport.get_queue = [_FakeResponse(200, content=payload), _FakeResponse(200, content=payload)]
    clock = _FakeClock(datetime(2026, 9, 10, tzinfo=UTC))
    sleeper = _FakeSleeper(clock)
    result = discover_metar_stations(
        _SETTINGS,
        latitude=latitude,
        longitude=longitude,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
    )
    assert len(result.bounds) == len(result.responses) == len(transport.calls) == 2
    assert all(east - west < 1 for _, west, _, east in result.bounds)
    assert len(result.candidates) == 1
    assert sleeper.sleeps == [1.0]


def test_conflicting_station_revisions_are_not_silently_deduplicated():
    transport = _FakeTransport()
    transport.get_queue = [
        _FakeResponse(200, content=json.dumps([station(elev=100), station(elev=101)]).encode())
    ]
    with pytest.raises(StationDiscoveryError, match="Conflicting"):
        discover_metar_stations(
            _SETTINGS,
            latitude=LAT,
            longitude=LON,
            transport=transport,
            clock=_FakeClock(datetime(2026, 9, 10, tzinfo=UTC)),
            sleeper=_FakeSleeper(),
        )


def test_no_data_is_an_empty_candidate_list_with_retained_response():
    transport = _FakeTransport()
    transport.get_queue = [_FakeResponse(204)]
    result = discover_metar_stations(
        _SETTINGS,
        latitude=LAT,
        longitude=LON,
        transport=transport,
        clock=_FakeClock(datetime(2026, 9, 10, tzinfo=UTC)),
        sleeper=_FakeSleeper(),
    )
    assert result.candidates == result.excluded == ()
    assert result.responses[0].payload == b"[]"
    assert result.responses[0].status_code == 204


def test_transport_stops_reading_an_oversized_response(monkeypatch):
    class OversizedResponse:
        chunks_read = 0
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def iter_content(self, *, chunk_size):
            for _ in range(100):
                self.chunks_read += 1
                yield b"x" * chunk_size

    transport = BoundedStationInfoTransport()
    response = OversizedResponse()
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(transport._session, "get", get)
    try:
        with pytest.raises(StationDiscoveryError, match="2 MiB"):
            transport.get("https://aviationweather.gov/api/data/stationinfo")
    finally:
        transport.close()
    assert response.chunks_read == 33
    assert response.closed
    assert calls[0][1]["stream"] is True
