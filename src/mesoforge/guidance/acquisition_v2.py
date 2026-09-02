"""Phase 2 byte-range acquisition for HRRR (extended cycle), NBM, and
GFS (plan Sections 2.2-2.4).

Reuses the shared deterministic retry/failover/ranged-fetch engine
(``guidance.http_fetch``) and inventory parsing (``guidance.index_parsing``)
that Phase 1's HRRR acquisition (``guidance.acquisition``) established.
Each model differs only in URL/selector construction (via its
``guidance.sources.<model>``/``<model>_phase2`` module) and in GFS's
need to retain *both* candidate APCP rows when they are ambiguous
duplicates at early leads (``select_field_rows``, plural).

Every acquired lead returns exactly the selected-message payload(s)
per field, keyed so ``guidance.normalization_v2`` can decode without
re-touching the network -- this module owns all I/O; normalization is
pure computation over already-acquired bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

from mesoforge.catalog.sources import GfsSourceSettings, HrrrPhase2SourceSettings, NbmSourceSettings
from mesoforge.guidance.http_fetch import FetchedObject as FetchedObject
from mesoforge.guidance.http_fetch import FetchError, RequestAttempt
from mesoforge.guidance.http_fetch import fetch_with_range as _fetch_with_range
from mesoforge.guidance.http_fetch import fetch_with_retry as _fetch_with_retry
from mesoforge.guidance.http_fetch import header as _header
from mesoforge.guidance.index_parsing import (
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
    select_field_rows,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources import gfs as gfs_source
from mesoforge.guidance.sources import hrrr_phase2 as hrrr_phase2_source
from mesoforge.guidance.sources import nbm as nbm_source

Phase2AcquisitionError = FetchError


def parse_provider_availability(last_modified: str | None) -> datetime | None:
    """Parse a provider's ``Last-Modified`` HTTP-date into an aware UTC
    instant, or return ``None`` when it is absent or unparseable.

    This is the *authoritative* moment an object became publicly
    available, as asserted by the provider itself. It is categorically
    different from the local wall-clock moment this process happened to
    retrieve those bytes, and only the former may be compared against a
    run's information cutoff or a cycle's completion deadline.
    """
    if last_modified is None:
        return None
    try:
        parsed = parsedate_to_datetime(last_modified)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_available_at(last_modified: str | None, *, retrieved_at: datetime) -> datetime:
    """The authoritative availability instant for one fetched object.

    Prefers the provider's own ``Last-Modified`` assertion. When a
    provider supplies none, falls back to this process's retrieval time,
    which is necessarily no earlier than publication -- a conservative
    direction that can only make an object look *later* than it truly
    was, so a genuinely late input can never be admitted by the
    fallback.
    """
    published = parse_provider_availability(last_modified)
    if published is None:
        return retrieved_at
    return published


@dataclass(frozen=True, slots=True)
class SelectedMessage:
    canonical_variable_id: str
    row: IndexRow
    byte_start: int
    byte_end: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class Phase2LeadAcquisition:
    """Everything acquired for one model's one forecast lead: the
    retained index bytes, every selected message's exact bytes (kept
    separate -- never concatenated -- so ambiguous-identity fields
    like NBM's deterministic APCP/PoP01 or GFS's duplicate bucket/
    continuous APCP rows decode unambiguously), and enough metadata
    to register both as source artifacts and to reconstruct a
    manifest.

    Two distinct kinds of timestamp are retained, and conflating them
    is a correctness bug:

    ``index_completed_at``/``grib_completed_at``
        When *this process* finished retrieving the bytes -- local
        wall-clock provenance of the acquisition itself.
    ``index_available_at``/``grib_available_at``
        When the *provider* published the object, taken from its own
        ``Last-Modified`` assertion. This is the authoritative
        information-availability instant, and the only one that may be
        compared against a run's information cutoff or a cycle's
        completion deadline. Retrieving an already-published
        retrospective cycle later must not make that cycle look late.
    """

    model: str
    cycle_date: date
    cycle_hour: int
    forecast_hour: int
    endpoint: str
    resolved_grib_url: str
    resolved_index_url: str
    index_payload: bytes
    index_attempts: tuple[RequestAttempt, ...]
    index_completed_at: datetime
    selected_messages: tuple[SelectedMessage, ...]
    grib_attempts: tuple[RequestAttempt, ...]
    grib_completed_at: datetime
    full_object_etag: str | None
    full_object_last_modified: str | None
    full_object_content_length: int
    index_available_at: datetime
    grib_available_at: datetime
    index_last_modified: str | None = None

    def payloads_by_variable(self) -> dict[str, list[bytes]]:
        """Group this lead's selected messages by canonical variable
        ID, preserving selection order (used to build
        ``normalization_v2.FieldPayloads`` entries -- a single-element
        list collapses to one payload, multiple elements are GFS's
        ambiguous bucket/continuous duplicate rows)."""
        grouped: dict[str, list[bytes]] = {}
        for message in self.selected_messages:
            grouped.setdefault(message.canonical_variable_id, []).append(message.payload)
        return grouped


def _head_full_object_length(
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    endpoint: str,
    grib_url: str,
    retry_policy: object,
    cycle_deadline: datetime,
) -> tuple[int, str | None, str | None]:
    head_fetch = _fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="head",
        urls_by_endpoint=[(endpoint, grib_url)],
        retry_policy=retry_policy,  # type: ignore[arg-type]
        cycle_deadline=cycle_deadline,
    )
    content_length_header = _header(head_fetch.headers, "Content-Length")
    if content_length_header is None:
        raise Phase2AcquisitionError(
            f"HEAD {grib_url!r} did not return a Content-Length header; the exact "
            "full object length is required to validate every selected byte range"
        )
    try:
        full_object_content_length = int(content_length_header)
    except ValueError as exc:
        raise Phase2AcquisitionError(
            f"HEAD {grib_url!r} returned a non-integer Content-Length {content_length_header!r}"
        ) from exc
    if full_object_content_length <= 0:
        raise Phase2AcquisitionError(
            f"HEAD {grib_url!r} returned a non-positive Content-Length "
            f"{full_object_content_length!r}"
        )
    return (
        full_object_content_length,
        _header(head_fetch.headers, "ETag"),
        _header(head_fetch.headers, "Last-Modified"),
    )


def _fetch_selected(
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    endpoint: str,
    grib_url: str,
    rows: tuple[IndexRow, ...],
    selected_rows: list[tuple[str, IndexRow]],
    retry_policy: object,
    cycle_deadline: datetime,
    full_object_content_length: int,
    full_object_etag: str | None,
    full_object_last_modified: str | None,
) -> tuple[list[SelectedMessage], list[RequestAttempt], datetime, str | None, str | None]:
    ordered_selected = sorted(selected_rows, key=lambda item: item[1].byte_offset)
    all_grib_attempts: list[RequestAttempt] = []
    latest_completed_at = clock.now()
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
            endpoint=endpoint,
            url=grib_url,
            range_header=range_header,
            byte_start=byte_start,
            byte_end=byte_end,
            retry_policy=retry_policy,  # type: ignore[arg-type]
            cycle_deadline=cycle_deadline,
            expected_length=byte_end - byte_start,
            full_object_length=full_object_content_length,
        )
        all_grib_attempts.extend(range_fetch.attempts)
        latest_completed_at = range_fetch.completed_at
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
    return (
        selected_messages,
        all_grib_attempts,
        latest_completed_at,
        full_object_etag,
        full_object_last_modified,
    )


def acquire_nbm_lead(
    settings: NbmSourceSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
    cycle_deadline: datetime,
) -> Phase2LeadAcquisition:
    """Acquire one NBM lead's pinned index and every field contract's
    selected message (each field's own byte range, never concatenated
    -- Section 2.3)."""
    index_urls = [
        (
            endpoint,
            nbm_source.build_index_url(
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
    rows = parse_index_rows(index_fetch.payload.decode("utf-8"))

    selected_rows: list[tuple[str, IndexRow]] = []
    for contract in settings.field_contracts:
        selector = nbm_source.build_field_selector(
            contract.canonical_variable_id, forecast_hour=forecast_hour
        )
        row = select_field_row(rows, selector)
        selected_rows.append((contract.canonical_variable_id, row))

    grib_url = nbm_source.build_grib_url(
        settings,
        endpoint=index_fetch.endpoint,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
    )
    full_object_content_length, full_object_etag, full_object_last_modified = (
        _head_full_object_length(
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            endpoint=index_fetch.endpoint,
            grib_url=grib_url,
            retry_policy=settings.retry_policy,
            cycle_deadline=cycle_deadline,
        )
    )
    (
        selected_messages,
        grib_attempts,
        grib_completed_at,
        full_object_etag,
        full_object_last_modified,
    ) = _fetch_selected(
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        endpoint=index_fetch.endpoint,
        grib_url=grib_url,
        rows=rows,
        selected_rows=selected_rows,
        retry_policy=settings.retry_policy,
        cycle_deadline=cycle_deadline,
        full_object_content_length=full_object_content_length,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
    )
    index_last_modified = _header(index_fetch.headers, "Last-Modified")
    return Phase2LeadAcquisition(
        model="nbm",
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
        endpoint=index_fetch.endpoint,
        resolved_grib_url=grib_url,
        resolved_index_url=index_fetch.resolved_url,
        index_payload=index_fetch.payload,
        index_attempts=index_fetch.attempts,
        index_completed_at=index_fetch.completed_at,
        selected_messages=tuple(selected_messages),
        grib_attempts=tuple(grib_attempts),
        grib_completed_at=grib_completed_at,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
        full_object_content_length=full_object_content_length,
        index_last_modified=index_last_modified,
        index_available_at=resolve_available_at(
            index_last_modified, retrieved_at=index_fetch.completed_at
        ),
        grib_available_at=resolve_available_at(
            full_object_last_modified, retrieved_at=grib_completed_at
        ),
    )


def acquire_hrrr_phase2_lead(
    settings: HrrrPhase2SourceSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
    cycle_deadline: datetime,
) -> Phase2LeadAcquisition:
    """Acquire one HRRR Phase 2 lead's pinned index and every field
    contract's selected message."""
    index_urls = [
        (
            endpoint,
            hrrr_phase2_source.build_index_url(
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
    rows = parse_index_rows(index_fetch.payload.decode("utf-8"))

    selected_rows: list[tuple[str, IndexRow]] = []
    for contract in settings.field_contracts:
        selector = hrrr_phase2_source.build_field_selector(
            contract.canonical_variable_id, forecast_hour=forecast_hour
        )
        row = select_field_row(rows, selector)
        selected_rows.append((contract.canonical_variable_id, row))

    grib_url = hrrr_phase2_source.build_grib_url(
        settings,
        endpoint=index_fetch.endpoint,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
    )
    full_object_content_length, full_object_etag, full_object_last_modified = (
        _head_full_object_length(
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            endpoint=index_fetch.endpoint,
            grib_url=grib_url,
            retry_policy=settings.retry_policy,
            cycle_deadline=cycle_deadline,
        )
    )
    (
        selected_messages,
        grib_attempts,
        grib_completed_at,
        full_object_etag,
        full_object_last_modified,
    ) = _fetch_selected(
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        endpoint=index_fetch.endpoint,
        grib_url=grib_url,
        rows=rows,
        selected_rows=selected_rows,
        retry_policy=settings.retry_policy,
        cycle_deadline=cycle_deadline,
        full_object_content_length=full_object_content_length,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
    )
    index_last_modified = _header(index_fetch.headers, "Last-Modified")
    return Phase2LeadAcquisition(
        model="hrrr",
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
        endpoint=index_fetch.endpoint,
        resolved_grib_url=grib_url,
        resolved_index_url=index_fetch.resolved_url,
        index_payload=index_fetch.payload,
        index_attempts=index_fetch.attempts,
        index_completed_at=index_fetch.completed_at,
        selected_messages=tuple(selected_messages),
        grib_attempts=tuple(grib_attempts),
        grib_completed_at=grib_completed_at,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
        full_object_content_length=full_object_content_length,
        index_last_modified=index_last_modified,
        index_available_at=resolve_available_at(
            index_last_modified, retrieved_at=index_fetch.completed_at
        ),
        grib_available_at=resolve_available_at(
            full_object_last_modified, retrieved_at=grib_completed_at
        ),
    )


def acquire_gfs_lead(
    settings: GfsSourceSettings,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    cycle_date: date,
    cycle_hour: int,
    forecast_hour: int,
    cycle_deadline: datetime,
) -> Phase2LeadAcquisition:
    """Acquire one GFS lead's pinned index and every field contract's
    selected message(s). For the APCP field at ``forecast_hour <= 6``
    both the bucket and possibly-duplicate continuous-total inventory
    rows are retained (Section 2.4: caller responsibility, via
    ``select_field_rows`` plural) -- both are kept as distinct
    ``SelectedMessage`` entries so ``normalize_gfs_cycle`` can validate
    their dual-parent equivalence during decode."""
    index_urls = [
        (
            endpoint,
            gfs_source.build_index_url(
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
    rows = parse_index_rows(index_fetch.payload.decode("utf-8"))

    selected_rows: list[tuple[str, IndexRow]] = []
    for contract in settings.field_contracts:
        selector = gfs_source.build_field_selector(
            contract.canonical_variable_id, forecast_hour=forecast_hour
        )
        if contract.canonical_variable_id == "liquid_equivalent_precipitation_amount_1h":
            matched_rows = select_field_rows(rows, selector)
            for row in matched_rows:
                selected_rows.append((contract.canonical_variable_id, row))
        else:
            row = select_field_row(rows, selector)
            selected_rows.append((contract.canonical_variable_id, row))

    grib_url = gfs_source.build_grib_url(
        settings,
        endpoint=index_fetch.endpoint,
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
    )
    full_object_content_length, full_object_etag, full_object_last_modified = (
        _head_full_object_length(
            transport=transport,
            clock=clock,
            sleeper=sleeper,
            endpoint=index_fetch.endpoint,
            grib_url=grib_url,
            retry_policy=settings.retry_policy,
            cycle_deadline=cycle_deadline,
        )
    )
    (
        selected_messages,
        grib_attempts,
        grib_completed_at,
        full_object_etag,
        full_object_last_modified,
    ) = _fetch_selected(
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        endpoint=index_fetch.endpoint,
        grib_url=grib_url,
        rows=rows,
        selected_rows=selected_rows,
        retry_policy=settings.retry_policy,
        cycle_deadline=cycle_deadline,
        full_object_content_length=full_object_content_length,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
    )
    index_last_modified = _header(index_fetch.headers, "Last-Modified")
    return Phase2LeadAcquisition(
        model="gfs",
        cycle_date=cycle_date,
        cycle_hour=cycle_hour,
        forecast_hour=forecast_hour,
        endpoint=index_fetch.endpoint,
        resolved_grib_url=grib_url,
        resolved_index_url=index_fetch.resolved_url,
        index_payload=index_fetch.payload,
        index_attempts=index_fetch.attempts,
        index_completed_at=index_fetch.completed_at,
        selected_messages=tuple(selected_messages),
        grib_attempts=tuple(grib_attempts),
        grib_completed_at=grib_completed_at,
        full_object_etag=full_object_etag,
        full_object_last_modified=full_object_last_modified,
        full_object_content_length=full_object_content_length,
        index_last_modified=index_last_modified,
        index_available_at=resolve_available_at(
            index_last_modified, retrieved_at=index_fetch.completed_at
        ),
        grib_available_at=resolve_available_at(
            full_object_last_modified, retrieved_at=grib_completed_at
        ),
    )
