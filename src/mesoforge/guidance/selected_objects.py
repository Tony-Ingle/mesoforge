"""Pin existing acquisition adapters to the objects proved by current discovery."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.acquisition_v2 import parse_provider_availability
from mesoforge.guidance.http_fetch import header, parse_content_range
from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport
from mesoforge.guidance.sources.current_availability import QPF_FIELD
from mesoforge.guidance.sources.gfs import build_field_selector


class SelectedObjectError(MesoForgeError):
    """Preparation cannot prove that an input is the selected provider object."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Selection timestamps must be timezone aware")
    return result.astimezone(UTC)


def selected_messages(probe: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The optional preceding GFS object supplies only accumulation parents."""
    qpf = tuple(probe.get("qpf_messages", []))
    if probe.get("qpf_only"):
        if probe["model"] != "GFS" or not qpf:
            raise ValueError("QPF-only probe requires GFS accumulation messages")
        return qpf
    return (probe["selected_message"], *probe.get("extra_messages", []), *qpf)


@dataclass(slots=True)
class _Response:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class SelectedObjectTransport:
    """Allow only selected inventories and message ranges, with identity checks.

    Successful index GETs and object HEADs are cached within this preparation.
    Every GRIB range GET is conditional on the discovery ETag and checked again.
    Compound-field ranges are cached until their object is released; streaming
    preparation releases each fully decoded object. Acquisition owns raw retention.
    Identity failures remain fatal even if an adapter's retry engine catches them.
    """

    def __init__(
        self,
        transport: HttpTransport,
        probes: Sequence[Mapping[str, Any]],
        *,
        decision_time: datetime,
        clock: Clock,
        latch_failures: bool = True,
    ) -> None:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone aware")
        self.transport, self.clock = transport, clock
        self.decision_time = decision_time.astimezone(UTC)
        # Every object is validated individually either way. With latching (the
        # default for the active model set) one failure stops all later requests;
        # without it a zero-weight shadow keeps acquiring its other selected objects
        # and each failure stays recorded and explicit.
        self.latch_failures = latch_failures
        self.failed_reason: str | None = None
        self._records: list[dict[str, Any]] = []
        self._indexes: dict[str, dict[str, Any]] = {}
        self._gribs: dict[str, dict[str, Any]] = {}
        self._cache: dict[tuple[str, str], _Response] = {}
        self._ranges: dict[str, dict[str, dict[str, Any]]] = {}
        self._range_cache: dict[tuple[str, str], _Response] = {}
        self._acquired: set[tuple[str, str]] = set()
        self._released: set[str] = set()
        if not probes:
            raise ValueError("At least one selected temperature probe is required")
        for original in probes:
            probe = deepcopy(dict(original))
            if (
                probe["status"] != "available"
                or _instant(probe["decision_time"]) != self.decision_time
            ):
                raise ValueError("Probe is not available at the selection decision time")
            index, grib, message = probe["index"], probe["grib"], probe["selected_message"]
            for metadata in (index, grib):
                published = parse_provider_availability(metadata["last_modified"])
                if (
                    metadata["status_code"] != 200
                    or not metadata["url"].startswith("https://")
                    or published is None
                    or published > self.decision_time
                    or _instant(metadata["available_at"]) != published
                ):
                    raise ValueError(
                        "Discovery object lacks valid pre-decision publication evidence"
                    )
            if (
                re.fullmatch(r"[0-9a-f]{64}", index["sha256"]) is None
                or type(index["content_bytes"]) is not int
                or index["content_bytes"] <= 0
                or re.fullmatch(r'"[^"\x00-\x20\x7f]*"', grib["etag"] or "") is None
                or type(grib["content_length"]) is not int
                or type(message["byte_start"]) is not int
                or type(message["byte_end_exclusive"]) is not int
                or message["byte_start"] < 0
                or message["byte_end_exclusive"] - message["byte_start"] < 20
                or message["byte_end_exclusive"] > grib["content_length"]
                or message["content_bytes"] != message["byte_end_exclusive"] - message["byte_start"]
            ):
                raise ValueError("Discovery object identity or selected byte range is invalid")
            urls = (index["url"], grib["url"])
            if len(set(urls)) != 2 or any(
                url in self._indexes or url in self._gribs for url in urls
            ):
                raise ValueError("Duplicate or conflicting selected object URLs")
            self._indexes[urls[0]] = probe
            self._gribs[urls[1]] = probe
            ranges: dict[str, dict[str, Any]] = {}
            fields: set[str] = set()
            for entry in selected_messages(probe):
                duplicate_qpf = (
                    entry.get("canonical_variable_id") == QPF_FIELD
                    and probe["model"] == "GFS"
                    and 1 <= probe["source_lead_hours"] <= 6
                    and entry in probe.get("qpf_messages", [])
                    and len(probe["qpf_messages"]) <= 2
                    and re.search(
                        build_field_selector(QPF_FIELD, forecast_hour=probe["source_lead_hours"]),
                        entry.get("index_row", ""),
                    )
                    is not None
                )
                if (
                    not isinstance(entry.get("canonical_variable_id"), str)
                    or (entry["canonical_variable_id"] in fields and not duplicate_qpf)
                    or type(entry.get("byte_start")) is not int
                    or type(entry.get("byte_end_exclusive")) is not int
                    or entry["byte_start"] < 0
                    or entry["byte_end_exclusive"] - entry["byte_start"] < 20
                    or entry["byte_end_exclusive"] > grib["content_length"]
                    or entry.get("content_bytes")
                    != entry["byte_end_exclusive"] - entry["byte_start"]
                ):
                    raise ValueError("Discovery selected field identity or byte range is invalid")
                fields.add(entry["canonical_variable_id"])
                range_header = f"bytes={entry['byte_start']}-{entry['byte_end_exclusive'] - 1}"
                for previous in ranges.values():
                    if max(previous["byte_start"], entry["byte_start"]) < min(
                        previous["byte_end_exclusive"], entry["byte_end_exclusive"]
                    ) and (previous["byte_start"], previous["byte_end_exclusive"]) != (
                        entry["byte_start"],
                        entry["byte_end_exclusive"],
                    ):
                        raise ValueError("Selected message byte ranges partially overlap")
                ranges.setdefault(range_header, entry)
            self._ranges[urls[1]] = ranges

    @property
    def downloaded_bytes(self) -> int | None:
        value: int | None = getattr(self.transport, "downloaded_bytes", None)
        return value

    @property
    def validations(self) -> list[dict[str, Any]]:
        return deepcopy(self._records)

    @property
    def cached_range_bytes(self) -> int:
        """Bytes held solely for within-object shared-message reuse."""
        return sum(len(response.content) for response in self._range_cache.values())

    def release_completed_object(self, url: str) -> None:
        """Release one acquired object's bytes after all region decoders finish.

        Immutable validation digests and acquisition completeness remain. A
        released object cannot be requested again through this preparation;
        replay must use its retained raw file, never a new provider response.
        """
        if url not in self._gribs:
            self._fail("Cannot release an object absent from the pinned selection")
        expected = {(url, byte_range) for byte_range in self._ranges[url]}
        if not expected.issubset(self._acquired):
            raise SelectedObjectError("Cannot release an incompletely acquired selected object")
        for key in expected:
            self._range_cache.pop(key, None)
        self._cache.pop(("get", self._gribs[url]["index"]["url"]), None)
        self._cache.pop(("head", url), None)
        self._released.add(url)

    def _fail(self, reason: str) -> None:
        self.failed_reason = reason
        raise SelectedObjectError(reason)

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [deepcopy(record) for record in self._records if record["status"] == "failed"]

    def _publication(self, headers: Mapping[str, str], expected: dict[str, Any]) -> None:
        modified = header(dict(headers), "Last-Modified")
        published = parse_provider_availability(modified)
        if (
            modified != expected["last_modified"]
            or published is None
            or published > self.decision_time
        ):
            raise SelectedObjectError(
                "Provider Last-Modified differs from discovery or exceeds cutoff"
            )
        if header(dict(headers), "Content-Encoding") not in (None, "identity"):
            raise SelectedObjectError("Encoded response cannot prove selected byte-range identity")

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        timeout: tuple[float, float] | None,
    ) -> HttpResponse:
        if self.failed_reason is not None and self.latch_failures:
            raise SelectedObjectError(self.failed_reason)
        is_index = url in self._indexes
        if not is_index and url not in self._gribs:
            self._fail(f"URL was not selected during discovery: {url}")
        if is_index and method != "get":
            self._fail("Only index GET and GRIB HEAD/range GET are permitted")
        probe = (self._indexes if is_index else self._gribs)[url]
        if probe["grib"]["url"] in self._released:
            self._fail("Selected object was released; replay its retained raw evidence")
        expected = probe["index" if is_index else "grib"]
        message = probe["selected_message"]
        request_headers = dict(headers or {})
        requested_range = header(request_headers, "Range")
        if is_index or method == "head":
            if requested_range is not None:
                self._fail("A byte range is permitted only for a selected GRIB GET")
        else:
            if requested_range not in self._ranges[url]:
                self._fail(
                    "GRIB GET must request exactly the temperature range selected at discovery"
                )
            assert requested_range is not None
            message = self._ranges[url][requested_range]
            if ("get", probe["index"]["url"]) not in self._cache or (
                "head",
                url,
            ) not in self._cache:
                self._fail("Revalidate the selected index and GRIB HEAD before acquiring its range")
            supplied_match = header(request_headers, "If-Match")
            if supplied_match is not None and supplied_match != expected["etag"]:
                self._fail("Caller If-Match conflicts with the selected GRIB identity")
            request_headers = {
                key: value for key, value in request_headers.items() if key.lower() != "if-match"
            }
            request_headers["If-Match"] = expected["etag"]
        cached = self._cache.get((method, url))
        if cached is None and requested_range is not None:
            cached = self._range_cache.get((url, requested_range))
        if cached is not None:
            return replace(cached)
        record: dict[str, Any] = {
            "model": probe["model"],
            "cycle": probe["cycle"],
            "source_lead_hours": probe["source_lead_hours"],
            "valid_time": probe["valid_time"],
            "decision_time": _iso(self.decision_time),
            "method": method.upper(),
            "url": url,
            "started_at": _iso(self.clock.now()),
            "request_headers": request_headers,
            "status": "checking",
        }
        self._records.append(record)
        try:
            response: HttpResponse = getattr(self.transport, method)(
                url, headers=request_headers or None, timeout=timeout
            )
            record.update(status_code=response.status_code, headers=dict(response.headers))
            expected_status = 200 if is_index or method == "head" else 206
            if response.status_code != expected_status:
                raise SelectedObjectError(
                    f"Selected object returned HTTP {response.status_code}; "
                    f"expected {expected_status}"
                )
            self._publication(response.headers, expected)
            actual_etag = header(dict(response.headers), "ETag")
            if expected.get("etag") is not None and actual_etag != expected["etag"]:
                raise SelectedObjectError("Provider ETag differs from discovery")
            payload = bytes(response.content)
            if is_index:
                digest = hashlib.sha256(payload).hexdigest()
                if digest != expected["sha256"] or len(payload) != expected["content_bytes"]:
                    raise SelectedObjectError(
                        "Inventory bytes differ from discovery SHA-256/length"
                    )
                record.update(sha256=digest, content_bytes=len(payload))
            elif method == "head":
                if header(dict(response.headers), "Content-Length") != str(
                    expected["content_length"]
                ):
                    raise SelectedObjectError("Provider full-object length differs from discovery")
                record["content_length"] = expected["content_length"]
            else:
                actual_range = parse_content_range(header(dict(response.headers), "Content-Range"))
                expected_range = (
                    message["byte_start"],
                    message["byte_end_exclusive"] - 1,
                    expected["content_length"],
                )
                if (
                    actual_range != expected_range
                    or len(payload) != message["content_bytes"]
                    or header(dict(response.headers), "Content-Length") != str(len(payload))
                ):
                    raise SelectedObjectError("GRIB response range/length differs from discovery")
                record.update(
                    canonical_variable_id=message["canonical_variable_id"],
                    sha256=hashlib.sha256(payload).hexdigest(),
                    content_bytes=len(payload),
                    byte_start=message["byte_start"],
                    byte_end_exclusive=message["byte_end_exclusive"],
                    full_object_length=expected["content_length"],
                    if_match=expected["etag"],
                )
                assert requested_range is not None
                self._acquired.add((url, requested_range))
            record.update(status="matched", completed_at=_iso(self.clock.now()))
            result = _Response(
                response.status_code, MappingProxyType(dict(response.headers)), payload
            )
            if is_index or method == "head":
                self._cache[(method, url)] = result
            elif probe.get("extra_messages") or probe.get("qpf_messages"):
                assert requested_range is not None
                self._range_cache[(url, requested_range)] = result
            return replace(result)
        except Exception as exc:
            record.update(status="failed", reason=str(exc), completed_at=_iso(self.clock.now()))
            self.failed_reason = str(exc)
            raise SelectedObjectError(str(exc)) from exc

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse:
        return self._request("get", url, headers, timeout)

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse:
        return self._request("head", url, headers, timeout)

    def assert_complete(self) -> None:
        """Require every selected native message; expected non-native gaps are not probes."""
        if self.failed_reason is not None:
            raise SelectedObjectError(self.failed_reason)
        expected = {
            (url, byte_range) for url, ranges in self._ranges.items() for byte_range in ranges
        }
        missing = sorted(expected.difference(self._acquired))
        if missing:
            raise SelectedObjectError(f"Selected temperature messages were not acquired: {missing}")
