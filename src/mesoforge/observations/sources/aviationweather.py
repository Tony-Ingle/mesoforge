"""AviationWeather.gov provider adapter (plan Section 2.4, Task 8).

Pure URL/query construction with no I/O; the real transport is
``RequestsHrrrHttpTransport``-equivalent, wired in production only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from mesoforge.catalog.sources import AviationWeatherSettings
from mesoforge.common.errors import MesoForgeError
from mesoforge.contracts.observations import RawMetarRecord
from mesoforge.contracts.observations_v2 import RawMetarRecordV2


class AviationWeatherParseError(MesoForgeError):
    """Raised when a retained AviationWeather.gov METAR response cannot
    be strictly mapped into ``RawMetarRecord`` (plan Section 2.4/Codex
    review t_09a43c6c finding 6: the production parser must map the
    provider's camelCase JSON schema strictly, never silently coerce
    or drop an unrecognized shape)."""


# The provider's exact camelCase METAR JSON keys, mapped to
# ``RawMetarRecord``'s snake_case field names (plan Section 2.4). Any
# record missing a required key, or of the wrong type, fails closed
# via ``RawMetarRecord``'s own strict Pydantic validation once mapped.
_REQUIRED_CAMEL_KEYS: tuple[str, ...] = (
    "icaoId",
    "obsTime",
    "reportTime",
    "receiptTime",
    "metarType",
    "rawOb",
    "lat",
    "lon",
    "elev",
)
_OPTIONAL_CAMEL_KEYS: tuple[str, ...] = ("temp", "dewp", "wdir", "wspd", "wgst", "qcField")


def _parse_epoch_seconds_instant(value: Any, *, field: str) -> datetime:
    """The provider encodes ``obsTime`` as exact Unix epoch seconds
    (int), per the Phase 1 plan ('pinning obsTime to Unix seconds').
    Residual review finding 4: this is now strict and fail-closed --
    only an ``int`` (never ``bool``, ``float``, or ``str``, even a
    numeric-looking one) is accepted."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AviationWeatherParseError(
            f"{field} must be a Unix epoch integer (seconds), got {value!r} ({type(value)!r})"
        )
    return datetime.fromtimestamp(value, tz=UTC)


def _parse_iso8601_utc_instant(value: Any, *, field: str) -> datetime:
    """``reportTime``/``receiptTime`` must be an explicit-UTC ISO 8601
    string (per the Phase 1 plan: malformed/non-UTC timestamps are
    rejected, never silently coerced). Residual review finding 4: a
    naive (offset-less) string is no longer defaulted to UTC, and a
    non-``Z``/non-``+00:00`` offset (e.g. ``+01:00``) is rejected
    rather than converted -- the provider's own field representation
    must already be explicit UTC."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise AviationWeatherParseError(
            f"{field} must be an explicit-UTC ISO 8601 string, got {value!r} ({type(value)!r})"
        )
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AviationWeatherParseError(
            f"{field} is not a valid ISO 8601 string: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise AviationWeatherParseError(
            f"{field} must carry an explicit UTC offset ('Z' or '+00:00'), got a "
            f"naive (offset-less) value: {value!r}"
        )
    if parsed.utcoffset() != timedelta(0):
        raise AviationWeatherParseError(
            f"{field} must be explicit UTC ('Z' or '+00:00'), got a non-UTC offset: {value!r}"
        )
    return parsed.replace(tzinfo=UTC)


def parse_raw_metar_record(record: Mapping[str, Any]) -> RawMetarRecord:
    """Strictly map one provider camelCase METAR JSON record into a
    ``RawMetarRecord``. Raises ``AviationWeatherParseError`` for any
    missing required key; delegates all further shape/finiteness
    validation to ``RawMetarRecord`` itself (strict Pydantic model)."""
    missing = [key for key in _REQUIRED_CAMEL_KEYS if key not in record]
    if missing:
        raise AviationWeatherParseError(
            f"METAR record is missing required provider key(s) {missing!r}: {dict(record)!r}"
        )

    mapped: dict[str, Any] = {
        "icao_id": record["icaoId"],
        "obs_time": _parse_epoch_seconds_instant(record["obsTime"], field="obsTime"),
        "report_time": _parse_iso8601_utc_instant(record["reportTime"], field="reportTime"),
        "receipt_time": _parse_iso8601_utc_instant(record["receiptTime"], field="receiptTime"),
        "metar_type": record["metarType"],
        "raw_ob": record["rawOb"],
        "lat": record["lat"],
        "lon": record["lon"],
        "elev": record["elev"],
    }
    for camel_key, snake_key in (
        ("temp", "temp"),
        ("dewp", "dewp"),
        ("wdir", "wdir"),
        ("wspd", "wspd"),
        ("wgst", "wgst"),
        ("qcField", "qc_field"),
    ):
        if camel_key in record:
            mapped[snake_key] = record[camel_key]

    try:
        return RawMetarRecordV2.model_validate(mapped, strict=True)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a domain error with context
        raise AviationWeatherParseError(
            f"METAR record failed strict RawMetarRecord validation after camelCase mapping: {exc}"
        ) from exc


def parse_raw_metar_response(payload: bytes) -> list[RawMetarRecord]:
    """Strictly parse a retained AviationWeather.gov ``/api/data/metar``
    JSON response body (a JSON array of records, or an empty body for a
    canonicalized 204) into ``RawMetarRecord`` values, in provider
    order."""
    if not payload.strip():
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AviationWeatherParseError(f"METAR response body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise AviationWeatherParseError(
            f"METAR response body must be a JSON array, got {type(parsed)!r}"
        )
    return [parse_raw_metar_record(record) for record in parsed]


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
