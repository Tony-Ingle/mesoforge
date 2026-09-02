"""Generic deterministic HTTP retry/failover and byte-range fetch engine
(plan Section 2.2/2.3, Task 2).

Extracted from ``guidance/acquisition.py`` (HRRR's original Task 4
implementation, retained unchanged and still fully exercised by its own
tests) so NBM and GFS acquisition can share exactly the same bounded
deterministic backoff, endpoint failover, and range-integrity/GRIB2
framing validation instead of re-implementing it. Depends only on
``guidance.interfaces`` protocol shapes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from mesoforge.catalog.sources import RetryPolicy
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport, Sleeper

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(MesoForgeError):
    """Terminal fetch failure (all endpoints/attempts exhausted, a 404
    past the cycle deadline, or an unrecoverable integrity/range
    mismatch)."""

    def __init__(self, message: str, *, attempts: tuple[RequestAttempt, ...] = ()) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class RequestAttempt:
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


def header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def parse_retry_after(headers: dict[str, str], cap_seconds: float) -> float | None:
    raw = header(headers, "Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(int(raw))
    except ValueError:
        return None
    return min(seconds, cap_seconds)


def attempt_request(
    transport: HttpTransport,
    *,
    method: str,
    url: str,
    endpoint: str,
    timeout: tuple[float, float],
    request_headers: dict[str, str] | None = None,
) -> tuple[HttpResponse | None, RequestAttempt]:
    try:
        response = getattr(transport, method)(url, headers=request_headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- transport errors are all retryable here
        return None, RequestAttempt(
            endpoint=endpoint, url=url, status_code=None, error=str(exc), headers={}
        )
    headers = dict(response.headers)
    return response, RequestAttempt(
        endpoint=endpoint, url=url, status_code=response.status_code, error=None, headers=headers
    )


def fetch_with_retry(
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
    """Shared retry/failover engine for an index GET or a full-object
    HEAD (plan Section 2.3): bounded deterministic backoff per
    endpoint, then endpoint failover only on a retryable
    availability/transport failure -- never on selector ambiguity,
    decode error, or semantic key mismatch (the caller raises those
    after this function returns, and they are never retried here)."""
    all_attempts: list[RequestAttempt] = []
    last_status: int | None = None

    for endpoint, url in urls_by_endpoint:
        for attempt_index in range(retry_policy.attempts_per_endpoint):
            response, attempt = attempt_request(
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
                        raise FetchError(
                            f"404 for {url!r} past the cycle availability deadline "
                            f"{cycle_deadline!r}; terminal missing source data"
                        )
                elif response.status_code not in RETRYABLE_STATUS_CODES:
                    raise FetchError(f"non-retryable HTTP {response.status_code} for {url!r}")

            is_last_attempt = attempt_index == retry_policy.attempts_per_endpoint - 1
            if is_last_attempt:
                break

            retry_after = (
                parse_retry_after(dict(response.headers), retry_policy.retry_after_cap_seconds)
                if response is not None
                else None
            )
            backoff = retry_policy.backoff_seconds[
                min(attempt_index, len(retry_policy.backoff_seconds) - 1)
            ]
            sleeper.sleep(retry_after if retry_after is not None else backoff)

    raise FetchError(
        f"all endpoints/attempts exhausted for {method.upper()} across "
        f"{[e for e, _ in urls_by_endpoint]!r}; last_status={last_status!r}"
    )


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """Strictly parse a ``Content-Range: bytes start-end/total`` header
    value into ``(start, end, total)`` (end inclusive). Returns
    ``None`` for a missing or malformed value."""
    if value is None:
        return None
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value.strip())
    if match is None:
        return None
    start, end, total = (int(group) for group in match.groups())
    if end < start or total <= end:
        return None
    return start, end, total


_GRIB_MAGIC = b"GRIB"
_GRIB_TRAILER = b"7777"
_GRIB_EDITION_2 = 2
_GRIB2_SECTION0_LENGTH = 16


def validate_grib_message_boundaries(payload: bytes, *, url: str, range_header: str) -> None:
    """Section 2.1/2.3 GRIB2 message framing: every selected message is
    exactly one complete GRIB2 message (edition 2, Section 0's own
    encoded total-message-length equals the exact ranged payload
    length)."""
    if len(payload) < _GRIB2_SECTION0_LENGTH + len(_GRIB_TRAILER):
        raise FetchError(
            f"selected GRIB message for {url!r} (range {range_header!r}) is only "
            f"{len(payload)} bytes, too short to contain a GRIB2 Section 0 "
            f"({_GRIB2_SECTION0_LENGTH} octets) plus the '7777' end section"
        )
    if not payload.startswith(_GRIB_MAGIC):
        raise FetchError(
            f"selected GRIB message for {url!r} (range {range_header!r}) does not begin "
            f"with the GRIB2 {_GRIB_MAGIC!r} indicator section; boundary integrity failed"
        )
    edition = payload[7]
    if edition != _GRIB_EDITION_2:
        raise FetchError(
            f"selected GRIB message for {url!r} (range {range_header!r}) declares "
            f"GRIB edition {edition!r} in Section 0, expected edition "
            f"{_GRIB_EDITION_2!r} (GRIB2); boundary integrity failed"
        )
    section0_total_length = int.from_bytes(payload[8:16], byteorder="big", signed=False)
    if section0_total_length != len(payload):
        raise FetchError(
            f"selected GRIB message for {url!r} (range {range_header!r}) declares a "
            f"Section 0 total message length of {section0_total_length} octets, but the "
            f"exact ranged payload is {len(payload)} bytes; the range must contain "
            "exactly one complete GRIB2 message"
        )
    if not payload.endswith(_GRIB_TRAILER):
        raise FetchError(
            f"selected GRIB message for {url!r} (range {range_header!r}) does not end "
            f"with the GRIB2 {_GRIB_TRAILER!r} end section; boundary integrity failed"
        )


def fetch_with_range(
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    *,
    endpoint: str,
    url: str,
    range_header: str,
    byte_start: int,
    byte_end: int,
    retry_policy: RetryPolicy,
    cycle_deadline: datetime,
    expected_length: int,
    full_object_length: int,
) -> FetchedObject:
    """Ranged GET with one extra integrity-mismatch retry (plan Section
    2.3): sends the exact ``Range`` header, validates the response's
    ``Content-Range`` and GRIB2 message framing exactly, and retries
    once from a fresh connection on an integrity mismatch before
    failing closed."""
    integrity_retries_remaining = 1
    preserved_attempts: list[RequestAttempt] = []
    while True:
        all_attempts: list[RequestAttempt] = list(preserved_attempts)
        response: HttpResponse | None = None
        for attempt_index in range(retry_policy.attempts_per_endpoint):
            resp, attempt = attempt_request(
                transport,
                method="get",
                url=url,
                endpoint=endpoint,
                timeout=(retry_policy.connect_timeout_seconds, retry_policy.read_timeout_seconds),
                request_headers={"Range": range_header},
            )
            all_attempts.append(attempt)
            if resp is not None and resp.status_code in (200, 206):
                response = resp
                break
            if resp is not None and resp.status_code == 404 and clock.now() > cycle_deadline:
                raise FetchError(f"404 for {url!r} past the cycle availability deadline")
            if resp is not None and resp.status_code not in RETRYABLE_STATUS_CODES.union({404}):
                raise FetchError(f"non-retryable HTTP {resp.status_code} for {url!r}")

            is_last = attempt_index == retry_policy.attempts_per_endpoint - 1
            if is_last:
                break
            retry_after = (
                parse_retry_after(dict(resp.headers), retry_policy.retry_after_cap_seconds)
                if resp is not None
                else None
            )
            backoff = retry_policy.backoff_seconds[
                min(attempt_index, len(retry_policy.backoff_seconds) - 1)
            ]
            sleeper.sleep(retry_after if retry_after is not None else backoff)

        if response is None:
            raise FetchError(f"range GET exhausted retries for {url!r}")

        if response.status_code == 200:
            raise FetchError(
                f"provider ignored Range and returned full content (200) for {url!r}; "
                "rejecting to avoid accidentally retaining the full product"
            )

        content_range = header(dict(response.headers), "Content-Range")
        parsed_range = parse_content_range(content_range)
        payload = bytes(response.content)

        integrity_ok = (
            len(payload) == expected_length
            and parsed_range is not None
            and parsed_range[0] == byte_start
            and parsed_range[1] == byte_end - 1
            and parsed_range[2] == full_object_length
        )
        if not integrity_ok:
            if integrity_retries_remaining > 0:
                integrity_retries_remaining -= 1
                preserved_attempts = all_attempts
                continue
            raise FetchError(
                f"range integrity mismatch for {url!r} (range {range_header!r}): "
                f"expected {expected_length} bytes at [{byte_start}, {byte_end}) "
                f"(full_object_length={full_object_length!r}), got {len(payload)} bytes, "
                f"Content-Range={content_range!r}",
                attempts=tuple(all_attempts),
            )

        validate_grib_message_boundaries(payload, url=url, range_header=range_header)

        return FetchedObject(
            endpoint=endpoint,
            url=url,
            resolved_url=url,
            payload=payload,
            headers=dict(response.headers),
            attempts=tuple(all_attempts),
            completed_at=clock.now(),
        )
