"""Bounded NOAA MRMS fixed-hour acquisition; no discovery or polling."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from mesoforge.observations.interfaces import Clock, HttpTransport
from mesoforge.observations.mrms import PRODUCT_CONTRACTS

BASE_URL = "https://mrms.ncep.noaa.gov/2D"


def source_url(product: str, product_time: datetime) -> str:
    if product not in PRODUCT_CONTRACTS:
        raise ValueError(f"Unsupported MRMS product: {product}")
    if product_time.tzinfo is None:
        raise ValueError("MRMS product time must be timezone-aware")
    instant = product_time.astimezone(UTC)
    if instant.minute or instant.second or instant.microsecond:
        raise ValueError("MRMS hourly product time must be on the UTC hour")
    return f"{BASE_URL}/{product}/MRMS_{product}_00.00_{instant:%Y%m%d-%H%M%S}.grib2.gz"


def fetch_product(
    product: str,
    product_time: datetime,
    *,
    transport: HttpTransport,
    clock: Clock,
) -> tuple[bytes, dict[str, Any]]:
    """One fixed object, retaining response identity and conservative availability."""
    url = source_url(product, product_time)
    response = transport.get(
        url,
        headers={"User-Agent": "MesoForge bounded MRMS source-contract ingestion"},
        timeout=(10.0, 120.0),
    )
    if response.status_code != 200:
        raise ValueError(f"MRMS fixed object returned HTTP {response.status_code}: {url}")
    raw = response.content
    if not raw.startswith(b"\x1f\x8b") or len(raw) > 64 * 1024 * 1024:
        raise ValueError("MRMS response must be one bounded original gzip source")
    acquired = clock.now()
    if acquired.tzinfo is None or acquired < product_time:
        raise ValueError("MRMS acquisition time must be aware and not precede product time")
    headers = {key.lower(): value for key, value in response.headers.items()}
    return raw, {
        "url": url,
        "filename": url.rsplit("/", 1)[1],
        "acquired_at": acquired.astimezone(UTC).isoformat(),
        "response_identity": {
            key: headers[key]
            for key in ("etag", "last-modified", "content-length", "content-type")
            if key in headers
        },
        "availability_method": "local-acquisition-complete",
    }


class RequestsMRMSHttpTransport:
    """The only MRMS network boundary; offline normalization never constructs it."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Any:
        return self._session.get(url, headers=dict(headers or {}), timeout=timeout)

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Any:
        return self._session.head(url, headers=dict(headers or {}), timeout=timeout)


def default_transport() -> HttpTransport:
    return cast(HttpTransport, RequestsMRMSHttpTransport())
