"""Bounded native NBM PoP discovery and identity-pinned, PoP-only acquisition."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from mesoforge.catalog.sources import NbmSourceSettings
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, acquire_nbm_lead
from mesoforge.guidance.cycle_selection import generate_candidate_reference_times
from mesoforge.guidance.http_fetch import FetchedObject, FetchError, fetch_with_retry, header
from mesoforge.guidance.index_parsing import (
    GribIndexError,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.selected_objects import SelectedObjectError, SelectedObjectTransport
from mesoforge.guidance.sources import nbm
from mesoforge.guidance.sources.current_availability import _metadata, _publication

POP = "probability_of_precipitation_1h"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("PoP reference times require a timezone")
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise ValueError("PoP reference times require an exact UTC hour")
    return value


def _probe(
    settings: NbmSourceSettings,
    *,
    cycle: datetime,
    lead: int,
    cutoff: datetime,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "model": "NBM",
        "cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(cycle + timedelta(hours=lead)),
        "decision_time": _iso(cutoff),
        "status": "unavailable",
        "endpoints": [],
    }
    try:
        for endpoint in settings.endpoint_order:
            url = nbm.build_grib_url(
                settings,
                endpoint=endpoint,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
            )
            attempt: dict[str, Any] = {"endpoint": endpoint}
            evidence["endpoints"].append(attempt)

            def fetch(method: str, request_url: str, endpoint: str = endpoint) -> FetchedObject:
                return fetch_with_retry(
                    transport,
                    clock,
                    sleeper,
                    method=method,
                    urls_by_endpoint=[(endpoint, request_url)],
                    retry_policy=settings.retry_policy,
                    cycle_deadline=clock.now() - timedelta(microseconds=1),
                    accept_status=frozenset({200, 404}),
                )

            index = fetch("get", url + settings.index_suffix)
            attempt["index"] = _metadata(index, method="GET")
            if index.attempts[-1].status_code != 200:
                attempt["reason"] = "PoP inventory unavailable (HTTP 404)"
                continue
            if _publication(attempt["index"]) > cutoff:
                attempt["reason"] = "PoP inventory was published after this discovery cutoff"
                continue
            rows = parse_index_rows(index.payload.decode("utf-8"))
            selector = nbm.build_field_selector(POP, forecast_hour=lead)
            try:
                row = select_field_row(rows, selector)
            except GribIndexError:
                # An absent exact interval is an explicit gap; duplicated rows are unsafe.
                if any(re.search(selector, entry.descriptor) for entry in rows):
                    raise
                attempt["reason"] = "No native one-hour PoP for the exact requested interval"
                continue
            if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
                raise ValueError("NBM PoP inventory cycle differs from the requested cycle")
            grib = fetch("head", url)
            attempt["grib"] = _metadata(grib, method="HEAD")
            if grib.attempts[-1].status_code != 200:
                attempt["reason"] = "PoP GRIB object unavailable (HTTP 404)"
                continue
            if _publication(attempt["grib"]) > cutoff:
                attempt["reason"] = "PoP GRIB object was published after this discovery cutoff"
                continue
            length = int(header(grib.headers, "Content-Length") or "")
            start, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
            attempt["grib"]["content_length"] = length
            evidence.update(
                status="available",
                selected_endpoint=endpoint,
                index=attempt["index"],
                grib=attempt["grib"],
                selected_message={
                    "canonical_variable_id": POP,
                    "index_row": row.line,
                    "byte_start": start,
                    "byte_end_exclusive": end,
                    "content_bytes": end - start,
                    "interval_start": _iso(cycle + timedelta(hours=lead - 1)),
                    "interval_end": _iso(cycle + timedelta(hours=lead)),
                    "threshold_kg_m2": settings.probability_threshold_kg_m2,
                    "threshold_comparator": ">",
                    "probability_type": 1,
                },
            )
            # The same identity validator constrains later acquisition, without I/O here.
            SelectedObjectTransport(transport, [evidence], decision_time=cutoff, clock=clock)
            return evidence
    except (FetchError, GribIndexError, UnicodeDecodeError, ValueError) as exc:
        evidence.update(status="error", reason=str(exc))
        return evidence
    evidence["reason"] = "; ".join(attempt["reason"] for attempt in evidence["endpoints"])
    return evidence


def select_pop_guidance(
    *,
    target_reference_time: datetime,
    horizons: tuple[int, ...],
    settings: NbmSourceSettings,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    explicit_cycle: datetime | None = None,
) -> dict[str, Any]:
    """Prefer newest complete native intervals, then freshest partial cycle with clear gaps.

    This separate discovery records its own actual availability cutoff. It does not
    claim PoP availability at an earlier HRRR/GFS/RAP/IFS selection decision.
    """
    target, cutoff = _hour(target_reference_time), clock.now().astimezone(UTC)
    if (
        not horizons
        or horizons != tuple(sorted(set(horizons)))
        or any(type(hour) is not int or not 1 <= hour <= 36 for hour in horizons)
    ):
        raise ValueError("PoP target horizons must be unique increasing hours within 1..36")
    if target > cutoff:
        raise ValueError("PoP target reference time cannot be after execution")
    cycles = (
        (_hour(explicit_cycle),)
        if explicit_cycle is not None
        else generate_candidate_reference_times(
            target_reference_time=target,
            cadence="hourly",
            max_lookback_hours=int(settings.max_age_hours),
        )
    )
    if any(
        not 0 <= (target - cycle).total_seconds() / 3600 <= settings.max_age_hours
        for cycle in cycles
    ):
        raise ValueError("NBM cycle must be at/before target within the configured age limit")
    starting_bytes = getattr(transport, "downloaded_bytes", 0)
    report: dict[str, Any] = {
        "model": "NBM",
        "field": POP,
        "status": "unavailable",
        "selected_cycle": None,
        "target_reference_time": _iso(target),
        "horizon_hours": list(horizons),
        "decision_time": _iso(cutoff),
        "candidates": [],
        "settings_sha256": hashlib.sha256(settings.model_dump_json().encode()).hexdigest(),
        "contract_profile": settings.contract_profile,
        "threshold_kg_m2": settings.probability_threshold_kg_m2,
        "threshold_comparator": ">",
        "temporal_resolution_hours": 1,
        "availability_note": (
            "PoP discovered separately at this actual cutoff; no earlier ingestion claim"
        ),
    }
    partial: dict[str, Any] | None = None
    chosen: dict[str, Any] | None = None
    for cycle in cycles:
        age = int((target - cycle).total_seconds() / 3600)
        candidate: dict[str, Any] = {"cycle": _iso(cycle), "probes": [], "status": "checking"}
        report["candidates"].append(candidate)
        for hour in horizons:
            probe = _probe(
                settings,
                cycle=cycle,
                lead=age + hour,
                cutoff=cutoff,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
            )
            candidate["probes"].append(probe)
            if probe["status"] == "error":
                candidate.update(status="error", reason=probe["reason"])
                report["reason"] = "Provider evidence could not be validated: " + probe["reason"]
                break
        if candidate["status"] == "error":
            break
        available = [p for p in candidate["probes"] if p["status"] == "available"]
        candidate["source_leads"] = [p["source_lead_hours"] for p in available]
        candidate["status"] = (
            "complete"
            if len(available) == len(horizons)
            else "partial"
            if available
            else "unavailable"
        )
        if candidate["status"] == "complete":
            chosen = candidate
            break
        if partial is None and available:
            partial = candidate
    else:
        chosen = partial
    if chosen is not None:
        gaps = [
            {
                "horizon_hours": p["source_lead_hours"]
                - int((target - datetime.fromisoformat(chosen["cycle"])).total_seconds() / 3600),
                "valid_time": p["valid_time"],
                "reason": p["reason"],
            }
            for p in chosen["probes"]
            if p["status"] != "available"
        ]
        report.update(
            status="selected" if not gaps else "partial",
            selected_cycle=chosen["cycle"],
            source_leads=chosen["source_leads"],
            missing_hours=gaps,
            reason="Newest complete native-hourly cycle"
            if not gaps
            else "No complete cycle; newest cycle with native hourly guidance, explicit gaps",
        )
    report.setdefault(
        "reason", "No native one-hour NBM PoP is available within the bounded cycle window"
    )
    report.update(
        completed_at=_iso(clock.now()),
        downloaded_metadata_bytes=getattr(transport, "downloaded_bytes", 0) - starting_bytes,
    )
    return report


def acquire_selected_pop(
    selection: dict[str, Any],
    *,
    settings: NbmSourceSettings,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> tuple[list[Phase2LeadAcquisition], dict[str, Any]]:
    """Acquire exact selected PoP messages, without other NBM fields or cycle substitution."""
    if (
        selection["status"] not in {"selected", "partial"}
        or selection["settings_sha256"]
        != hashlib.sha256(settings.model_dump_json().encode()).hexdigest()
    ):
        raise ValueError("PoP acquisition requires a selected cycle and unchanged settings")
    cycle = _hour(datetime.fromisoformat(selection["selected_cycle"]))
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    horizons = tuple(selection["horizon_hours"])
    if (
        not horizons
        or horizons != tuple(sorted(set(horizons)))
        or any(type(hour) is not int or not 1 <= hour <= 36 for hour in horizons)
    ):
        raise ValueError("PoP selection target horizons changed")
    age = int((target - cycle).total_seconds() / 3600)
    required = [age + hour for hour in horizons]
    candidate = next(
        row for row in selection["candidates"] if row["cycle"] == selection["selected_cycle"]
    )
    probes = [row for row in candidate["probes"] if row["status"] == "available"]
    if (
        not probes
        or not 0 <= age <= settings.max_age_hours
        or [p["source_lead_hours"] for p in candidate["probes"]] != required
        or len(required) != len(set(required))
        or [p["source_lead_hours"] for p in probes] != selection["source_leads"]
        or (selection["status"] == "selected" and len(probes) != len(required))
        or any(
            p["selected_message"]["canonical_variable_id"] != POP
            or p["model"] != "NBM"
            or p["cycle"] != _iso(cycle)
            or p["valid_time"] != _iso(cycle + timedelta(hours=p["source_lead_hours"]))
            or p["selected_message"]["interval_start"]
            != _iso(cycle + timedelta(hours=p["source_lead_hours"] - 1))
            or p["selected_message"]["interval_end"] != p["valid_time"]
            or p["selected_message"]["threshold_kg_m2"] != settings.probability_threshold_kg_m2
            or p["selected_message"]["threshold_comparator"] != ">"
            or p["selected_message"]["probability_type"] != 1
            or p.get("extra_messages")
            or p.get("qpf_messages")
            for p in probes
        )
    ):
        raise ValueError("PoP selection native intervals are incomplete or changed")
    pinned = SelectedObjectTransport(
        transport,
        probes,
        decision_time=datetime.fromisoformat(selection["decision_time"]),
        clock=clock,
    )
    starting_bytes = getattr(transport, "downloaded_bytes", 0)
    acquired = []
    for probe in probes:
        pinned_settings = settings.model_copy(
            update={"endpoint_order": (probe["selected_endpoint"],)}
        )
        try:
            row = acquire_nbm_lead(
                pinned_settings,
                transport=pinned,
                clock=clock,
                sleeper=sleeper,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=probe["source_lead_hours"],
                cycle_deadline=clock.now(),
                canonical_variables=(POP,),
            )
        except FetchError as exc:
            if pinned.failed_reason is not None:
                raise SelectedObjectError(pinned.failed_reason) from exc
            raise
        acquired.append(row)
    pinned.assert_complete()
    return acquired, {
        "selection": selection,
        "object_validation": pinned.validations,
        "downloaded_bytes": getattr(transport, "downloaded_bytes", 0) - starting_bytes,
    }
