"""Bounded, coordinate-driven discovery using official AWC station metadata.

The provider supports bounding boxes, not radius searches. Query a conservative
envelope, then apply the same WGS84 distance used by observation matching.
See https://aviationweather.gov/data/schema/openapi.yaml and /data/api/.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from pyproj import Geod
from requests import Response

from mesoforge.catalog.sources import AviationWeatherSettings
from mesoforge.common.errors import MesoForgeError
from mesoforge.observations.acquisition import (
    FetchedResponse,
    RequestRateLimiter,
    _fetch_with_retry,
)
from mesoforge.observations.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.observations.sources.aviationweather import (
    RequestsAviationWeatherHttpTransport,
    request_headers,
)

_GEOD = Geod(ellps="WGS84")
# WGS84's minimum meridional radius gives a conservative spherical envelope.
_MIN_CURVATURE_RADIUS_M = 6_335_439.327
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# AWC documents a 400-entry cap for most endpoints. Reaching it cannot prove
# completeness; do not save a possibly truncated local candidate list.
_PROVIDER_RESULT_LIMIT = 400
Bounds = tuple[float, float, float, float]


class StationDiscoveryError(MesoForgeError):
    """Station metadata cannot establish a complete, valid candidate list."""


class BoundedStationInfoTransport(RequestsAviationWeatherHttpTransport):
    """Reuse the AWC session with a 2 MiB response ceiling for local metadata."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Response:
        with self._session.get(
            url, headers=dict(headers or {}), timeout=timeout, stream=True
        ) as response:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise StationDiscoveryError(
                        "Station metadata response exceeds the 2 MiB local query limit"
                    )
                chunks.append(chunk)
            response._content = b"".join(chunks)
            return response

    def close(self) -> None:
        self._session.close()


@dataclass(frozen=True, slots=True)
class StationDiscovery:
    bounds: tuple[Bounds, ...]
    responses: tuple[FetchedResponse, ...]
    candidates: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StationDiscoveryError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise StationDiscoveryError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise StationDiscoveryError(f"{field} must be a finite number")
    return number


def station_query_bounds(
    latitude: float, longitude: float, *, radius_km: float = 50.0
) -> tuple[Bounds, ...]:
    """Return south/west/north/east envelopes; split at the date line."""
    latitude = _number(latitude, field="latitude")
    longitude = _number(longitude, field="longitude")
    radius_km = _number(radius_km, field="radius_km")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise StationDiscoveryError("Latitude/longitude are outside valid geographic ranges")
    if radius_km <= 0:
        raise StationDiscoveryError("radius_km must be positive")
    angle = radius_km * 1000 / _MIN_CURVATURE_RADIUS_M
    delta_lat = math.degrees(angle)
    south, north = max(-90.0, latitude - delta_lat), min(90.0, latitude + delta_lat)
    if south == -90 or north == 90:
        return ((south, -180.0, north, 180.0),)
    delta_lon = math.degrees(
        math.asin(min(1.0, math.sin(angle) / math.cos(math.radians(latitude))))
    )
    west, east = longitude - delta_lon, longitude + delta_lon
    if west < -180:
        return ((south, west + 360, north, 180.0), (south, -180.0, north, east))
    if east > 180:
        return ((south, west, north, 180.0), (south, -180.0, north, east - 360))
    return ((south, west, north, east),)


def build_stationinfo_bbox_url(settings: AviationWeatherSettings, bounds: Bounds) -> str:
    params = {"bbox": ",".join(str(value) for value in bounds), "format": "json"}
    return f"{settings.base_url}{settings.stationinfo_path}?{urlencode(params)}"


def parse_stationinfo_candidates(
    payload: bytes,
    *,
    latitude: float,
    longitude: float,
    radius_km: float = 50.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strictly parse one local response, recording spatial/capability exclusions."""
    station_query_bounds(latitude, longitude, radius_km=radius_km)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise StationDiscoveryError("Station metadata response exceeds the 2 MiB local query limit")
    try:
        records = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StationDiscoveryError("Station metadata response is not valid JSON") from exc
    if not isinstance(records, list):
        raise StationDiscoveryError("Station metadata response must be a JSON array")
    if len(records) >= _PROVIDER_RESULT_LIMIT:
        raise StationDiscoveryError(
            "Station metadata may be truncated at the provider's 400-row cap"
        )
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise StationDiscoveryError("Station metadata entries must be objects")
        identifier = record.get("icaoId")
        site_types = record.get("siteType")
        if not isinstance(site_types, list) or not all(
            isinstance(item, str) for item in site_types
        ):
            raise StationDiscoveryError(f"Station {identifier!r} has malformed siteType metadata")
        if "METAR" not in site_types:
            excluded.append({"station_id": identifier, "reason": "not_metar_capable"})
            continue
        if (
            not isinstance(identifier, str)
            or not 3 <= len(identifier) <= 4
            or not identifier.isascii()
            or not identifier.isalnum()
            or identifier != identifier.upper()
        ):
            raise StationDiscoveryError("METAR station metadata has an invalid ICAO identifier")
        lat = _number(record.get("lat"), field=f"{identifier}.lat")
        lon = _number(record.get("lon"), field=f"{identifier}.lon")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise StationDiscoveryError(f"Station {identifier} has out-of-range coordinates")
        distance = float(_GEOD.inv(longitude, latitude, lon, lat)[2]) / 1000
        if distance > radius_km:
            excluded.append(
                {
                    "station_id": identifier,
                    "distance_km": distance,
                    "reason": "outside_search_radius",
                }
            )
            continue
        elevation = record.get("elev")
        priority = record.get("priority")
        site_name = record.get("site")
        if site_name is not None and not isinstance(site_name, str):
            raise StationDiscoveryError(f"Station {identifier} has malformed site name")
        candidates.append(
            {
                "station_id": identifier,
                "network": "METAR",
                "lat": lat,
                "lon": lon,
                "elevation_m": None
                if elevation is None
                else _number(elevation, field=f"{identifier}.elev"),
                "distance_km": distance,
                "site_name": site_name,
                "site_types": site_types,
                "provider_priority": None
                if priority is None
                else _number(priority, field=f"{identifier}.priority"),
            }
        )
    return candidates, excluded


def discover_metar_stations(
    settings: AviationWeatherSettings,
    *,
    latitude: float,
    longitude: float,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    radius_km: float = 50.0,
    rate_limiter: RequestRateLimiter | None = None,
) -> StationDiscovery:
    """Acquire at most two local stationinfo envelopes using the retained retry policy."""
    bounds = station_query_bounds(latitude, longitude, radius_km=radius_km)
    limiter = rate_limiter or RequestRateLimiter(settings.min_request_interval_seconds)
    responses: list[FetchedResponse] = []
    candidates: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    for query_bounds in bounds:
        response = _fetch_with_retry(
            transport,
            clock,
            sleeper,
            url=build_stationinfo_bbox_url(settings, query_bounds),
            headers=dict(request_headers(settings)),
            settings=settings,
            rate_limiter=limiter,
        )
        responses.append(response)
        parsed, rejected = parse_stationinfo_candidates(
            response.payload, latitude=latitude, longitude=longitude, radius_km=radius_km
        )
        for candidate in parsed:
            identifier = candidate["station_id"]
            if identifier in candidates and candidates[identifier] != candidate:
                raise StationDiscoveryError(f"Conflicting station metadata for {identifier}")
            candidates[identifier] = candidate
        excluded.extend(rejected)
    return StationDiscovery(
        bounds=bounds,
        responses=tuple(responses),
        candidates=tuple(
            sorted(candidates.values(), key=lambda row: (row["distance_km"], row["station_id"]))
        ),
        excluded=tuple(excluded),
    )
