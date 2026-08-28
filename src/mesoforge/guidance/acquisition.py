"""Deterministic HRRR byte-range acquisition orchestration (plan Section
2.1-2.3, Task 4).

Depends only on ``guidance.interfaces`` protocol shapes (transport,
clock, sleeper) -- unit tests inject a scripted fake; production wiring
injects ``RequestsHrrrHttpTransport``/real clock/``time.sleep``. No
``ArtifactService``/storage import here: this module returns plain,
typed result objects that ``application/phase1.py`` registers as
source artifacts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from mesoforge.catalog.sources import HrrrSourceSettings, RetryPolicy
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport, Sleeper
from mesoforge.guidance.sources.hrrr import (
    HrrrIndexError,
    IndexRow,
    build_grib_url,
    build_index_url,
    check_lead_step_type,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
)

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HrrrAcquisitionError(MesoForgeError):
    """Terminal HRRR acquisition failure (all endpoints/attempts
    exhausted, a 404 past the cycle deadline, or an unrecoverable
    integrity/range mismatch)."""


@dataclass(frozen=True, slots=True)
class RequestAttempt:
    """One retained HTTP attempt for the acquisition manifest (plan
    Section 2.3: 'Preserve response Date, ETag, Last-Modified, and
    request-attempt history')."""

    endpoint: str
    url: str
    status_code: int | None
    error: str | None
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class FetchedObject:
    endpoint: str
    url: str
    resolved_url: str
    payload: bytes
    headers: dict[str, str]
    attempts: tuple[RequestAttempt, ...]
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


def _is_retryable_transport_error(exc: Exception) -> bool:
    # Any transport-raised exception (connect/read timeout, connection
    # error) is treated as retryable; HTTP status classification happens
    # separately once a response object is obtained.
    return True


def _attempt_request(
    transport: HttpTransport,
    *,
    method: str,
    url: str,
    endpoint: str,
    timeout: tuple[float, float],
) -> tuple[HttpResponse | None, RequestAttempt]:
    try:
        response = getattr(transport, method)(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- transport errors are all retryable here
        return None, RequestAttempt(
            endpoint=endpoint, url=url, status_code=None, error=str(exc), headers={}
        )
    headers = dict(response.headers)
    return response, RequestAttempt(
        endpoint=endpoint, url=url, status_code=response.status_code, error=None, headers=headers
    )


def _fetch_with_retry(
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    *,
    method: str,
    urls_by_endpoint: Sequence[tuple[str, str]],
    retry_policy: RetryPolicy,
    cycle_deadline: datetime,
    accept_status: frozenset[int] = frozenset({200}),
) -> FetchedObject:
    """Shared retry/failover engine for both the index GET and the
    ranged GRIB GET (plan Section 2.3): bounded deterministic backoff
    per endpoint, then endpoint failover only on a retryable
    availability/transport failure -- never on selector ambiguity,
    decode error, or semantic key mismatch (those are raised by the
    caller after this function returns, and are never retried here)."""
    all_attempts: list[RequestAttempt] = []
    last_status: int | None = None

    for endpoint, url in urls_by_endpoint:
        for attempt_index in range(retry_policy.attempts_per_endpoint):
            response, attempt = _attempt_request(
                transport,
                method=method,
                url=url,
                endpoint=endpoint,
                timeout=(retry_policy.connect_timeout_seconds, retry_policy.read_timeout_seconds),
            )
            all_attempts.append(attempt)

            if response is not None and response.status_code in accept_status:
                return FetchedObject(
                    endpoint=endpoint,
                    url=url,
                    resolved_url=url,
                    payload=bytes(response.content),
                    headers=dict(response.headers),
                    attempts=tuple(all_attempts),
                    completed_at=clock.now(),
                )

            if response is not None:
                last_status = response.status_code
                if response.status_code == 404:
                    if clock.now() > cycle_deadline:
                        raise HrrrAcquisitionError(
                            f"404 for {url!r} past the cycle availability deadline "
                            f"{cycle_deadline!r}; terminal missing source data"
                        )
                    # retryable until the deadline
                elif response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise HrrrAcquisitionError(
                        f"non-retryable HTTP {response.status_code} for {url!r}"
                    )

            is_last_attempt = attempt_index == retry_policy.attempts_per_endpoint - 1
            if is_last_attempt:
                break

            retry_after = (
                _parse_retry_after(dict(response.headers), retry_policy.retry_after_cap_seconds)
                if response is not None
                else None
            )
            backoff = retry_policy.backoff_seconds[
                min(attempt_index, len(retry_policy.backoff_seconds) - 1)
            ]
            sleeper.sleep(retry_after if retry_after is not None else backoff)

    raise HrrrAcquisitionError(
        f"all endpoints/attempts exhausted for {method.upper()} across "
        f"{[e for e, _ in urls_by_endpoint]!r}; last_status={last_status!r}"
    )


@dataclass(frozen=True, slots=True)
class SelectedMessage:
    canonical_variable_id: str
    row: IndexRow
    byte_start: int
    byte_end: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class HrrrLeadAcquisition:
    """Everything acquired for one forecast lead: the retained index
    bytes, the exact selected/concatenated GRIB bytes, and enough
    metadata to build both the acquisition manifest and register both
    as source artifacts."""

    cycle_date: date
    cycle_hour: int
    forecast_hour: int
    endpoint: str
    resolved_grib_url: str
    resolved_index_url: str
    index_payload: bytes
    index_attempts: tuple[RequestAttempt, ...]
    index_completed_at: datetime
    selected_grib_payload: bytes
    selected_messages: tuple[SelectedMessage, ...]
    grib_attempts: tuple[RequestAttempt, ...]
    grib_completed_at: datetime
    full_object_etag: str | None
    full_object_last_modified: str | None
    full_object_content_length: int | None


def acquire_hrrr_lead(
    settings: HrrrSourceSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
    cycle_deadline: datetime,
) -> HrrrLeadAcquisition:
    """Acquire one HRRR lead's pinned index + selected GRIB messages
    (plan Section 2.2, 8-step byte-range algorithm)."""
    index_urls = [
        (
            endpoint,
            build_index_url(
                settings,
                endpoint=endpoint,
                cycle_date=cycle_date,
                cycle_hour=cycle_hour,
                forecast_hour=forecast_hour,
            ),
        )
        for endpoint in settings.endpoint_order
    ]
    index_fetch = _fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="get",
        urls_by_endpoint=index_urls,
        retry_policy=settings.retry_policy,
        cycle_deadline=cycle_deadline,
    )

    index_text = index_fetch.payload.decode("utf-8")
    try:
        rows = parse_index_rows(index_text)
    except HrrrIndexError:
        raise

    selected_rows: list[tuple[str, IndexRow]] = []
    for assertion in settings.field_assertions:
        row = select_field_row(rows, assertion.inventory_selector)
        check_lead_step_type(row, forecast_hour=forecast_hour)
        selected_rows.append((assertion.canonical_variable_id, row))

    grib_url = build_grib_url(
        settings,
        endpoint=index_fetch.endpoint,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
    )

    ordered_selected = sorted(selected_rows, key=lambda item: item[1].byte_offset)
    needs_full_length = any(
        row.message_number == max(r.message_number for r in rows) for _cvid, row in ordered_selected
    )
    full_object_content_length: int | None = None
    full_object_etag: str | None = None
    full_object_last_modified: str | None = None
    if needs_full_length:
        head_fetch = _fetch_with_retry(
            transport,
            clock,
            sleeper,
            method="head",
            urls_by_endpoint=[(index_fetch.endpoint, grib_url)],
            retry_policy=settings.retry_policy,
            cycle_deadline=cycle_deadline,
        )
        full_object_content_length = (
            int(content_length_header)
            if (content_length_header := _header(head_fetch.headers, "Content-Length")) is not None
            else None
        )
        full_object_etag = _header(head_fetch.headers, "ETag")
        full_object_last_modified = _header(head_fetch.headers, "Last-Modified")

    all_grib_attempts: list[RequestAttempt] = []
    latest_grib_completed_at = index_fetch.completed_at
    selected_messages: list[SelectedMessage] = []
    for canonical_variable_id, row in ordered_selected:
        byte_start, byte_end = compute_message_byte_range(
            rows, selected=row, full_object_length=full_object_content_length
        )
        range_header = f"bytes={byte_start}-{byte_end - 1}"
        range_fetch = _fetch_with_range(
            transport,
            clock,
            sleeper,
            endpoint=index_fetch.endpoint,
            url=grib_url,
            range_header=range_header,
            retry_policy=settings.retry_policy,
            cycle_deadline=cycle_deadline,
            expected_length=byte_end - byte_start,
        )
        all_grib_attempts.extend(range_fetch.attempts)
        latest_grib_completed_at = range_fetch.completed_at
        if full_object_etag is None:
            full_object_etag = _header(range_fetch.headers, "ETag")
        if full_object_last_modified is None:
            full_object_last_modified = _header(range_fetch.headers, "Last-Modified")
        selected_messages.append(
            SelectedMessage(
                canonical_variable_id=canonical_variable_id,
                row=row,
                byte_start=byte_start,
                byte_end=byte_end,
                payload=range_fetch.payload,
            )
        )

    # Concatenate in original source-offset order (already sorted above).
    concatenated = b"".join(m.payload for m in selected_messages)

    return HrrrLeadAcquisition(
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
        endpoint=index_fetch.endpoint,
        resolved_grib_url=grib_url,
        resolved_index_url=index_fetch.resolved_url,
        index_payload=index_fetch.payload,
        index_attempts=index_fetch.attempts,
        index_completed_at=index_fetch.completed_at,
        selected_grib_payload=concatenated,
        selected_messages=tuple(selected_messages),
        grib_attempts=tuple(all_grib_attempts),
        grib_completed_at=latest_grib_completed_at,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
        full_object_content_length=full_object_content_length,
    )


def _fetch_with_range(
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    *,
    endpoint: str,
    url: str,
    range_header: str,
    retry_policy: RetryPolicy,
    cycle_deadline: datetime,
    expected_length: int,
) -> FetchedObject:
    """Ranged GET with one extra integrity-mismatch retry (plan Section
    2.3: 'Retry an integrity/range mismatch once from a fresh connection;
    then fail closed')."""
    integrity_retries_remaining = 1
    while True:
        all_attempts: list[RequestAttempt] = []
        response: HttpResponse | None = None
        for attempt_index in range(retry_policy.attempts_per_endpoint):
            resp, attempt = _attempt_request(
                transport,
                method="get",
                url=url,
                endpoint=endpoint,
                timeout=(retry_policy.connect_timeout_seconds, retry_policy.read_timeout_seconds),
            )
            all_attempts.append(attempt)
            if resp is not None and resp.status_code in (200, 206):
                response = resp
                break
            if resp is not None and resp.status_code == 404 and clock.now() > cycle_deadline:
                raise HrrrAcquisitionError(f"404 for {url!r} past the cycle availability deadline")
            if resp is not None and resp.status_code not in _RETRYABLE_STATUS_CODES.union({404}):
                raise HrrrAcquisitionError(f"non-retryable HTTP {resp.status_code} for {url!r}")

            is_last = attempt_index == retry_policy.attempts_per_endpoint - 1
            if is_last:
                break
            retry_after = (
                _parse_retry_after(dict(resp.headers), retry_policy.retry_after_cap_seconds)
                if resp is not None
                else None
            )
            backoff = retry_policy.backoff_seconds[
                min(attempt_index, len(retry_policy.backoff_seconds) - 1)
            ]
            sleeper.sleep(retry_after if retry_after is not None else backoff)

        if response is None:
            raise HrrrAcquisitionError(f"range GET exhausted retries for {url!r}")

        if response.status_code == 200:
            raise HrrrAcquisitionError(
                f"provider ignored Range and returned full content (200) for {url!r}; "
                "rejecting to avoid accidentally retaining the full product"
            )

        content_range = _header(dict(response.headers), "Content-Range")
        payload = bytes(response.content)
        if len(payload) != expected_length or content_range is None:
            if integrity_retries_remaining > 0:
                integrity_retries_remaining -= 1
                continue
            raise HrrrAcquisitionError(
                f"range integrity mismatch for {url!r} (range {range_header!r}): "
                f"expected {expected_length} bytes, got {len(payload)}, "
                f"Content-Range={content_range!r}"
            )

        return FetchedObject(
            endpoint=endpoint,
            url=url,
            resolved_url=url,
            payload=payload,
            headers=dict(response.headers),
            attempts=tuple(all_attempts),
            completed_at=clock.now(),
        )


def build_acquisition_manifest_payload(
    acquisitions: Sequence[HrrrLeadAcquisition],
    *,
    settings: HrrrSourceSettings,
    herbie_version: str,
    cfgrib_version: str,
    eccodes_version: str,
    xarray_version: str,
    numpy_version: str,
    pyproj_version: str,
) -> dict[str, object]:
    """Build the ``hrrr-acquisition-manifest.v1`` canonical-JSON payload
    (plan Section 4.1: source manifest for the reproducibility boundary
    -- Herbie template, product, cycle, forecast hour, provider,
    resolved URL, decoder/library versions, inventory rows, selected
    message numbers/keys, byte ranges, and request-attempt history)."""
    leads_payload = []
    for acquisition in acquisitions:
        leads_payload.append(
            {
                "forecast_hour": acquisition.forecast_hour,
                "cycle_date": acquisition.cycle_date.isoformat(),
                "cycle_hour": acquisition.cycle_hour,
                "endpoint": acquisition.endpoint,
                "resolved_grib_url": acquisition.resolved_grib_url,
                "resolved_index_url": acquisition.resolved_index_url,
                "full_object_etag": acquisition.full_object_etag,
                "full_object_last_modified": acquisition.full_object_last_modified,
                "full_object_content_length": acquisition.full_object_content_length,
                "index_completed_at": acquisition.index_completed_at.isoformat(),
                "grib_completed_at": acquisition.grib_completed_at.isoformat(),
                "index_attempts": [
                    {
                        "endpoint": a.endpoint,
                        "url": a.url,
                        "status_code": a.status_code,
                        "error": a.error,
                    }
                    for a in acquisition.index_attempts
                ],
                "grib_attempts": [
                    {
                        "endpoint": a.endpoint,
                        "url": a.url,
                        "status_code": a.status_code,
                        "error": a.error,
                    }
                    for a in acquisition.grib_attempts
                ],
                "selected_messages": [
                    {
                        "canonical_variable_id": m.canonical_variable_id,
                        "message_number": m.row.message_number,
                        "byte_start": m.byte_start,
                        "byte_end": m.byte_end,
                        "inventory_row": m.row.line,
                    }
                    for m in acquisition.selected_messages
                ],
            }
        )

    return {
        "schema_version": "hrrr-acquisition-manifest.v1",
        "model": settings.model,
        "product": settings.product,
        "sector": settings.sector,
        "file_template": settings.file_template,
        "endpoint_order": list(settings.endpoint_order),
        "field_assertions": [
            {
                "canonical_variable_id": a.canonical_variable_id,
                "inventory_selector": a.inventory_selector,
                "discipline": a.discipline,
                "parameter_category": a.parameter_category,
                "parameter_number": a.parameter_number,
                "type_of_level": a.type_of_level,
                "level": a.level,
                "expected_unit_id": a.expected_unit_id,
            }
            for a in settings.field_assertions
        ],
        "read_keys": list(settings.read_keys),
        "library_versions": {
            "herbie": herbie_version,
            "cfgrib": cfgrib_version,
            "eccodes": eccodes_version,
            "xarray": xarray_version,
            "numpy": numpy_version,
            "pyproj": pyproj_version,
        },
        "leads": leads_payload,
    }


class RealClock:
    """Concrete ``guidance.interfaces.Clock`` for production wiring."""

    def now(self) -> datetime:
        from datetime import UTC

        return datetime.now(UTC)


class RealSleeper:
    """Concrete ``guidance.interfaces.Sleeper`` for production wiring."""

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)
