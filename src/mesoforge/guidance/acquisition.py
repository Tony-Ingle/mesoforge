"""Deterministic HRRR byte-range acquisition orchestration (plan Section
2.1-2.3, Task 4; Task 2 generalization).

Depends only on ``guidance.interfaces`` protocol shapes (transport,
clock, sleeper) -- unit tests inject a scripted fake; production wiring
injects ``RequestsHrrrHttpTransport``/real clock/``time.sleep``. No
``ArtifactService``/storage import here: this module returns plain,
typed result objects that ``application/phase1.py`` registers as
source artifacts.

Task 2 extracted the shared deterministic retry/failover/ranged-fetch
engine to ``guidance.http_fetch`` so NBM/GFS acquisition (Tasks 3/4)
reuse it directly; this module re-exports ``RequestAttempt``,
``FetchedObject``, and ``HrrrAcquisitionError`` under their original
Phase 1 names for full backward compatibility.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from mesoforge.catalog.sources import HrrrSourceSettings
from mesoforge.guidance.http_fetch import FetchedObject as FetchedObject
from mesoforge.guidance.http_fetch import FetchError, RequestAttempt
from mesoforge.guidance.http_fetch import fetch_with_range as _fetch_with_range
from mesoforge.guidance.http_fetch import fetch_with_retry as _fetch_with_retry
from mesoforge.guidance.http_fetch import header as _header
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources.hrrr import (
    IndexRow,
    build_grib_url,
    build_index_url,
    check_lead_step_type,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
)

# Backward-compatible alias: HrrrAcquisitionError was this module's own
# exception class in Phase 1; Task 2 generalized the shared retry/fetch
# engine to guidance.http_fetch.FetchError (used by HRRR/NBM/GFS alike).
# Both names refer to the exact same exception class.
HrrrAcquisitionError = FetchError

__all__ = [
    "FetchedObject",
    "HrrrAcquisitionError",
    "HrrrLeadAcquisition",
    "RealClock",
    "RealSleeper",
    "RequestAttempt",
    "SelectedMessage",
    "acquire_hrrr_lead",
    "build_acquisition_manifest_payload",
]


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
    full_object_content_length: int


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
    rows = parse_index_rows(index_text)

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

    # Review finding 1 (residual): the full object length must be
    # established for *every* lead -- via HEAD -- regardless of whether
    # a selected row happens to be the inventory's final message. It is
    # required both to compute the last message's byte range (when
    # applicable) and, critically, to validate the exact Content-Range
    # ``total`` on *every* selected ranged GET, including rows that are
    # followed by later (unselected) inventory rows. A missing/invalid
    # Content-Length is a terminal acquisition failure, never silently
    # skipped validation.
    head_fetch = _fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="head",
        urls_by_endpoint=[(index_fetch.endpoint, grib_url)],
        retry_policy=settings.retry_policy,
        cycle_deadline=cycle_deadline,
    )
    content_length_header = _header(head_fetch.headers, "Content-Length")
    if content_length_header is None:
        raise HrrrAcquisitionError(
            f"HEAD {grib_url!r} did not return a Content-Length header; the exact "
            "full object length is required to validate every selected byte range"
        )
    try:
        full_object_content_length = int(content_length_header)
    except ValueError as exc:
        raise HrrrAcquisitionError(
            f"HEAD {grib_url!r} returned a non-integer Content-Length {content_length_header!r}"
        ) from exc
    if full_object_content_length <= 0:
        raise HrrrAcquisitionError(
            f"HEAD {grib_url!r} returned a non-positive Content-Length "
            f"{full_object_content_length!r}"
        )
    full_object_etag: str | None = _header(head_fetch.headers, "ETag")
    full_object_last_modified: str | None = _header(head_fetch.headers, "Last-Modified")

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
            byte_start=byte_start,
            byte_end=byte_end,
            retry_policy=settings.retry_policy,
            cycle_deadline=cycle_deadline,
            expected_length=byte_end - byte_start,
            full_object_length=full_object_content_length,
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
