"""Metadata-only temperature availability evidence for current model selection.

This does not acquire or validate GRIB contents. Preparation must still download,
decode, and compare the selected object identities before issuing a forecast.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.catalog.sources import RetryPolicy
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.acquisition_v2 import parse_provider_availability
from mesoforge.guidance.http_fetch import FetchedObject, FetchError, fetch_with_retry, header
from mesoforge.guidance.index_parsing import (
    GribIndexError,
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
    select_field_rows,
)
from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport, Sleeper
from mesoforge.guidance.precipitation import compute_bucket_start
from mesoforge.guidance.sources import gfs, hrrr_phase2, ifs, nbm, rap

SURFACE_FIELDS = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
)
QPF_FIELD = "liquid_equivalent_precipitation_amount_1h"


@dataclass(frozen=True, slots=True)
class TemperatureProbeResult:
    available: bool
    reason: str | None
    evidence: dict[str, Any]
    index_payload: bytes | None
    index_payloads: dict[str, bytes]


class ProviderEvidenceError(MesoForgeError):
    """The provider evidence is unsafe to use or silently bypass."""

    def __init__(
        self, reason: str, *, evidence: dict[str, Any], index_payloads: dict[str, bytes]
    ) -> None:
        super().__init__(reason)
        self.evidence = evidence
        self.index_payloads = index_payloads
        self.index_payload = next(reversed(index_payloads.values()), None)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _RecordingTransport:
    """Keep failed attempt evidence too; the shared HTTP engine owns retry policy."""

    def __init__(self, transport: HttpTransport, clock: Clock, records: list[dict[str, Any]]):
        self.transport, self.clock, self.records = transport, clock, records

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        timeout: tuple[float, float] | None,
    ) -> HttpResponse:
        record: dict[str, Any] = {
            "method": method.upper(),
            "url": url,
            "started_at": _iso(self.clock.now()),
        }
        self.records.append(record)
        try:
            response: HttpResponse = getattr(self.transport, method)(
                url, headers=headers, timeout=timeout
            )
        except Exception as exc:
            record.update(error=str(exc), completed_at=_iso(self.clock.now()))
            raise
        record.update(
            status_code=response.status_code,
            headers=dict(response.headers),
            completed_at=_iso(self.clock.now()),
        )
        return response

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


def _endpoints(
    model: str, cycle: datetime, lead: int, configuration: Phase2Configuration
) -> tuple[tuple[str, str, str, RetryPolicy], ...]:
    if model == "NBM":
        nbm_settings = configuration.nbm
        return tuple(
            (
                endpoint,
                nbm.build_index_url(
                    nbm_settings,
                    endpoint=endpoint,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                    forecast_hour=lead,
                ),
                nbm.build_grib_url(
                    nbm_settings,
                    endpoint=endpoint,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                    forecast_hour=lead,
                ),
                nbm_settings.retry_policy,
            )
            for endpoint in nbm_settings.endpoint_order
        )
    if model == "IFS":
        return (
            (
                ifs.IFS_ENDPOINT,
                ifs.build_index_url(cycle=cycle, forecast_hour=lead),
                ifs.build_grib_url(cycle=cycle, forecast_hour=lead),
                ifs.IFS_RETRY_POLICY,
            ),
        )
    if model == "RAP":
        url = rap.build_grib_url(cycle=cycle, forecast_hour=lead)
        return ((rap.RAP_ENDPOINT, url + ".idx", url, rap.RAP_RETRY_POLICY),)
    if model == "HRRR":
        settings = configuration.hrrr
        return tuple(
            (
                endpoint,
                hrrr_phase2.build_index_url(
                    settings,
                    endpoint=endpoint,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                    forecast_hour=lead,
                ),
                hrrr_phase2.build_grib_url(
                    settings,
                    endpoint=endpoint,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                    forecast_hour=lead,
                ),
                settings.retry_policy,
            )
            for endpoint in settings.endpoint_order
        )
    if model == "GFS":
        gfs_settings = configuration.gfs
        return tuple(
            (
                endpoint,
                gfs.build_index_url(
                    gfs_settings,
                    endpoint=endpoint,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                    forecast_hour=lead,
                ),
                gfs.build_grib_url(
                    gfs_settings,
                    endpoint=endpoint,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                    forecast_hour=lead,
                ),
                gfs_settings.retry_policy,
            )
            for endpoint in gfs_settings.endpoint_order
        )
    raise ValueError(f"Unsupported current temperature model {model!r}")


class _TemperatureAbsentError(ValueError):
    pass


def _select(
    model: str, payload: bytes, cycle: datetime, lead: int
) -> tuple[tuple[IndexRow, ...], IndexRow, int | None]:
    if model == "IFS":
        try:
            row, end = ifs.selected_temperature(payload, cycle=cycle, forecast_hour=lead)
        except ifs.IfsTemperatureUnavailableError as exc:
            raise _TemperatureAbsentError(str(exc)) from exc
        return (), row, end
    if model == "RAP":
        try:
            rows, row = rap._selected_temperature(payload, cycle=cycle, forecast_hour=lead)
        except rap.RapTemperatureUnavailableError as exc:
            if ":TMP:2 m above ground:" in payload.decode("utf-8"):
                raise GribIndexError(
                    "RAP inventory temperature lead does not match request"
                ) from exc
            raise _TemperatureAbsentError(str(exc)) from exc
        return rows, row, None
    rows = parse_index_rows(payload.decode("utf-8"))
    source = hrrr_phase2 if model == "HRRR" else nbm if model == "NBM" else gfs
    selector = source.build_field_selector("air_temperature_2m", forecast_hour=lead)
    if not any(re.search(selector, row.descriptor) for row in rows):
        if any(":TMP:2 m above ground:" in row.descriptor for row in rows):
            raise GribIndexError(f"{model} inventory temperature lead does not match request")
        raise _TemperatureAbsentError(f"{model} inventory contains no two-metre temperature")
    row = select_field_row(rows, selector)
    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise GribIndexError(f"{model} inventory temperature cycle does not match request")
    return rows, row, None


def _metadata(fetched: FetchedObject, *, method: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "method": method,
        "url": fetched.resolved_url,
        "status_code": fetched.attempts[-1].status_code,
        "retrieved_at": _iso(fetched.completed_at),
        "headers": fetched.headers,
        "last_modified": header(fetched.headers, "Last-Modified"),
        "etag": header(fetched.headers, "ETag"),
    }
    if method == "GET":
        result.update(
            sha256=hashlib.sha256(fetched.payload).hexdigest(), content_bytes=len(fetched.payload)
        )
    return result


def _surface_messages(
    model: str, payload: bytes, cycle: datetime, lead: int, length: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Select optional instantaneous fields from the same proved provider object.

    Temperature retains its existing cycle-completeness policy. Optional fields
    never silently change the selected temperature cycle or their own semantics.
    """
    messages: list[dict[str, Any]] = []
    missing: dict[str, str] = {}
    for variable in SURFACE_FIELDS[1:]:
        if model == "IFS" and variable == "wind_gust_10m":
            missing[variable] = ifs.GUST_TEMPORAL_MISMATCH
            continue
        try:
            if model == "IFS":
                row, end = ifs.selected_surface_field(
                    payload, canonical_variable_id=variable, cycle=cycle, forecast_hour=lead
                )
                start = row.byte_offset
            else:
                if model == "RAP":
                    rows, row = rap.selected_surface_field(
                        payload, canonical_variable_id=variable, cycle=cycle, forecast_hour=lead
                    )
                else:
                    rows = parse_index_rows(payload.decode("utf-8"))
                    source = hrrr_phase2 if model == "HRRR" else gfs
                    selector = source.build_field_selector(variable, forecast_hour=lead)
                    if not any(re.search(selector, entry.descriptor) for entry in rows):
                        raise _TemperatureAbsentError(
                            f"{model} inventory has no instantaneous {variable} "
                            f"at source lead {lead}"
                        )
                    row = select_field_row(rows, selector)
                    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
                        raise GribIndexError(f"{model} {variable} cycle does not match request")
                start, end = compute_message_byte_range(
                    rows, selected=row, full_object_length=length
                )
            if start < 0 or end - start < 20 or end > length:
                raise ValueError(f"Selected {variable} range is outside the published GRIB object")
        except (
            _TemperatureAbsentError,
            rap.RapTemperatureUnavailableError,
            ifs.IfsTemperatureUnavailableError,
        ) as exc:
            missing[variable] = str(exc)
            continue
        messages.append(
            {
                "canonical_variable_id": variable,
                "index_row": row.line,
                "byte_start": start,
                "byte_end_exclusive": end,
                "content_bytes": end - start,
            }
        )
    return messages, missing


def _publication(metadata: dict[str, Any]) -> datetime:
    published = parse_provider_availability(metadata["last_modified"])
    if published is None:
        raise ValueError(
            "Missing or invalid Last-Modified cannot prove publication by decision time"
        )
    metadata.update(available_at=_iso(published), availability_basis="provider Last-Modified")
    return published


def _qpf_messages(
    model: str, payload: bytes, cycle: datetime, lead: int, length: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Retain exact native accumulation candidates; decoding resolves GFS duplicates."""
    if model not in {"HRRR", "GFS"}:
        return [], "Precipitation preparation is not enabled for this shadow model"
    rows = parse_index_rows(payload.decode("utf-8"))
    source = hrrr_phase2 if model == "HRRR" else gfs
    selector = source.build_field_selector(QPF_FIELD, forecast_hour=lead)
    if not any(re.search(selector, row.descriptor) for row in rows):
        return [], f"{model} inventory has no compatible QPF accumulation at source lead {lead}"
    selected = select_field_rows(rows, selector)
    if len(selected) > (2 if model == "GFS" and lead <= 6 else 1):
        raise GribIndexError(f"{model}: unexpected duplicate QPF accumulation candidates")
    start_lead = lead - 1 if model == "HRRR" else compute_bucket_start(lead)
    messages = []
    for row in selected:
        if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
            raise GribIndexError(f"{model}: QPF inventory cycle does not match request")
        start, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
        if start < 0 or end - start < 20 or end > length:
            raise ValueError("Selected QPF range is outside the published GRIB object")
        messages.append(
            {
                "canonical_variable_id": QPF_FIELD,
                "index_row": row.line,
                "byte_start": start,
                "byte_end_exclusive": end,
                "content_bytes": end - start,
                "accumulation_start": _iso(cycle + timedelta(hours=start_lead)),
                "accumulation_end": _iso(cycle + timedelta(hours=lead)),
            }
        )
    return messages, None


def probe_temperature(
    *,
    model: str,
    cycle: datetime,
    lead: int,
    configuration: Phase2Configuration,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    decision_time: datetime,
    surface_fields: bool = False,
    qpf_fields: bool = False,
    native_fields: bool = False,
) -> TemperatureProbeResult:
    """Check one native lead with index GET + GRIB HEAD, never a GRIB GET.

    Only missing objects/fields or publication after the cutoff allow a candidate
    to be treated as unavailable. A final mirror's HTTP 403 can also reject this
    candidate when an earlier independent mirror explicitly returned HTTP 404:
    no endpoint proved usable, and the denied mirror remains unknown, not absent.
    Any older candidate must pass the complete checks independently. Access denial
    without that missing-object evidence, rate/transport errors, and malformed or
    unprovable evidence still fail closed rather than selecting an older run.
    """
    for name, value in (("cycle", cycle), ("decision_time", decision_time)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone aware")
    cycle, decision_time = cycle.astimezone(UTC), decision_time.astimezone(UTC)
    if cycle.minute or cycle.second or cycle.microsecond or cycle > decision_time:
        raise ValueError("cycle must identify an exact UTC hour at/before decision time")
    minimum_lead = 0 if native_fields and model == "IFS" else 1
    if type(lead) is not int or lead < minimum_lead:
        raise ValueError("lead must be a positive integer")
    endpoints = _endpoints(model, cycle, lead, configuration)
    evidence: dict[str, Any] = {
        "model": model,
        "cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(cycle + timedelta(hours=lead)),
        "decision_time": _iso(decision_time),
        "status": "checking",
        "endpoints": [],
        "limitation": (
            "Provider metadata availability only; GRIB content is not acquired or decoded"
        ),
    }
    payloads: dict[str, bytes] = {}
    last_reason = "No configured endpoint supplied the required guidance"
    try:
        for endpoint, index_url, grib_url, policy in endpoints:
            attempt: dict[str, Any] = {"endpoint": endpoint, "requests": []}
            evidence["endpoints"].append(attempt)
            recording = _RecordingTransport(transport, clock, attempt["requests"])

            def fetch(
                method: str,
                url: str,
                recording: _RecordingTransport = recording,
                endpoint: str = endpoint,
                policy: RetryPolicy = policy,
            ) -> FetchedObject:
                sleeper.sleep(0.5)
                return fetch_with_retry(
                    recording,
                    clock,
                    sleeper,
                    method=method,
                    urls_by_endpoint=[(endpoint, url)],
                    retry_policy=policy,
                    cycle_deadline=clock.now() - timedelta(microseconds=1),
                    accept_status=frozenset({200, 404}),
                )

            index = fetch("get", index_url)
            attempt["index"] = _metadata(index, method="GET")
            if index.attempts[-1].status_code == 404:
                last_reason = "Required temperature inventory is not published (HTTP 404)"
                attempt.update(status="unavailable", reason=last_reason)
                continue
            payloads[index_url] = index.payload
            if _publication(attempt["index"]) > decision_time:
                last_reason = "Temperature inventory was published after decision time"
                attempt.update(status="unavailable", reason=last_reason)
                continue
            try:
                rows, row, explicit_end = _select(model, index.payload, cycle, lead)
            except _TemperatureAbsentError as exc:
                last_reason = str(exc)
                attempt.update(status="unavailable", reason=last_reason)
                continue
            grib = fetch("head", grib_url)
            attempt["grib"] = _metadata(grib, method="HEAD")
            if grib.attempts[-1].status_code == 404:
                last_reason = "Required GRIB object is not published (HTTP 404)"
                attempt.update(status="unavailable", reason=last_reason)
                continue
            if _publication(attempt["grib"]) > decision_time:
                last_reason = "GRIB object was published after decision time"
                attempt.update(status="unavailable", reason=last_reason)
                continue
            etag = header(grib.headers, "ETag")
            if etag is None or re.fullmatch(r'"[^"\x00-\x20\x7f]*"', etag) is None:
                raise ValueError(
                    "GRIB HEAD must include a strong ETag for subsequent object identity checks"
                )
            attempt["grib"]["identity_basis"] = "provider strong ETag and content length"
            length_header = header(grib.headers, "Content-Length")
            if length_header is None or not re.fullmatch(r"[0-9]+", length_header):
                raise ValueError("GRIB HEAD must include a positive integer Content-Length")
            length = int(length_header)
            start, end = (
                (row.byte_offset, explicit_end)
                if explicit_end is not None
                else compute_message_byte_range(rows, selected=row, full_object_length=length)
            )
            if length <= 0 or start < 0 or end - start < 20 or end > length:
                raise ValueError("Selected temperature range is outside the published GRIB object")
            attempt["grib"]["content_length"] = length
            attempt["selected_message"] = {
                "canonical_variable_id": "air_temperature_2m",
                "index_row": row.line,
                "byte_start": start,
                "byte_end_exclusive": end,
                "content_bytes": end - start,
            }
            attempt.update(status="available")
            evidence.update(
                status="available",
                selected_endpoint=endpoint,
                index=attempt["index"],
                grib=attempt["grib"],
                selected_message=attempt["selected_message"],
            )
            if surface_fields:
                extra_messages, missing_fields = _surface_messages(
                    model, index.payload, cycle, lead, length
                )
                attempt.update(extra_messages=extra_messages, missing_fields=missing_fields)
                evidence.update(extra_messages=extra_messages, missing_fields=missing_fields)
            if qpf_fields:
                qpf_messages, missing_qpf = _qpf_messages(model, index.payload, cycle, lead, length)
                attempt.update(qpf_messages=qpf_messages, missing_qpf=missing_qpf)
                evidence.update(qpf_messages=qpf_messages, missing_qpf=missing_qpf)
            if native_fields:
                from mesoforge.guidance.sources.native_fields import selected_native_fields

                extra_messages, missing_fields = selected_native_fields(
                    model, index.payload, cycle, lead, length
                )
                attempt.update(extra_messages=extra_messages, missing_fields=missing_fields)
                evidence.update(extra_messages=extra_messages, missing_fields=missing_fields)
            return TemperatureProbeResult(True, None, evidence, index.payload, payloads)
    except (FetchError, GribIndexError, ifs.IfsIndexError, UnicodeDecodeError, ValueError) as exc:
        attempted = evidence["endpoints"]
        last = attempted[-1]
        independently_missing = any(
            prior.get("status") == "unavailable"
            and any(prior.get(kind, {}).get("status_code") == 404 for kind in ("index", "grib"))
            for prior in attempted[:-1]
        )
        if (
            isinstance(exc, FetchError)
            and len(attempted) == len(endpoints)
            and last["requests"]
            and last["requests"][-1].get("status_code") == 403
            and independently_missing
        ):
            last.update(
                status="access_denied",
                availability="unknown",
                reason=str(exc),
            )
            reason = (
                "No usable endpoint proved for this candidate: an independent mirror "
                "returned HTTP 404 and the final mirror denied access (HTTP 403). "
                "The denied mirror's availability is unknown; older candidates require "
                "their own complete provider evidence."
            )
            evidence.update(status="unavailable", reason=reason)
            return TemperatureProbeResult(
                False, reason, evidence, next(reversed(payloads.values()), None), payloads
            )
        evidence.update(status="error", reason=str(exc))
        raise ProviderEvidenceError(str(exc), evidence=evidence, index_payloads=payloads) from exc
    evidence.update(status="unavailable", reason=last_reason)
    return TemperatureProbeResult(
        False, last_reason, evidence, next(reversed(payloads.values()), None), payloads
    )
