"""Discover complete current temperature guidance through actual bounded acquisition.

The Phase 2 provider's candidate ordering and acquisition adapters are reused, but
its all-field/NBM and historical completion-deadline policy do not govern this
temperature demonstration. A candidate is usable only after every needed message
has been acquired and decoded for the common valid-time window.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mesoforge.application.spatial_coverage import UnsupportedCoordinateError
from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.catalog.domains import BoundingBox
from mesoforge.catalog.sources import GfsSourceSettings, HrrrPhase2SourceSettings
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    acquire_gfs_lead,
    acquire_hrrr_phase2_lead,
)
from mesoforge.guidance.cycle_selection import generate_candidate_reference_times
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources import gfs, hrrr_phase2
from mesoforge.guidance.sources.gfs_decoding import GfsDecodeError, decode_instantaneous_message
from mesoforge.guidance.sources.hrrr_phase2_decoding import (
    HrrrPhase2DecodeError,
    decode_selected_message,
)

_VARIABLE = "air_temperature_2m"


class CurrentGuidanceUnavailableError(ValueError):
    """No complete current pair can provide the requested future forecast window."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_message(
    acquired: Phase2LeadAcquisition, settings: HrrrPhase2SourceSettings | GfsSourceSettings
) -> None:
    contract = next(c for c in settings.field_contracts if c.canonical_variable_id == _VARIABLE)
    if len(acquired.selected_messages) != 1:
        raise ValueError("Temperature discovery requires exactly one message per source lead")
    kwargs = {
        "contract": contract,
        "settings": settings,
        "forecast_hour": acquired.forecast_hour,
        "cycle_date": acquired.cycle_date,
        "cycle_hour": acquired.cycle_hour,
    }
    payload = acquired.selected_messages[0].payload
    if isinstance(settings, HrrrPhase2SourceSettings):
        decode_selected_message(payload, **kwargs)  # type: ignore[arg-type]
    else:
        decode_instantaneous_message(payload, **kwargs)  # type: ignore[arg-type]


def select_current_guidance(
    directory: Path,
    *,
    configuration: Phase2Configuration,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    areas: tuple[BoundingBox, ...],
) -> tuple[dict[str, list[Phase2LeadAcquisition]], dict[str, Any]]:
    """Retain discovery evidence and return the newest fully decoded cycle per model.

    The reference is the execution UTC hour, so hours 1..36 are future valid times.
    The existing 48-hour source-lead boundary permits at most 12 hours of cycle age.
    Acquisition of a candidate starts with its final required lead, then checks all
    remaining leads; publication of just the final product never proves completeness.
    """
    from mesoforge.application.prepared_temperature import (
        _leads,
        _retain_input,
        normalize_temperature_messages,
    )

    execution = clock.now()
    if execution.tzinfo is None:
        raise ValueError("Cycle discovery requires a timezone-aware execution clock")
    execution = execution.astimezone(UTC)
    target = execution.replace(minute=0, second=0, microsecond=0)
    first_valid = target + timedelta(hours=1)
    if not areas:
        raise ValueError("Cycle discovery requires at least one derived spatial footprint")
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError(
            "Cycle discovery requires an empty directory; retained evidence is preserved"
        )
    report: dict[str, Any] = {
        "mode": "automatic",
        "status": "discovering",
        "execution_time": _iso(execution),
        "target_reference_time": _iso(target),
        "first_valid_time": _iso(first_valid),
        "last_valid_time": _iso(target + timedelta(hours=36)),
        "selection_rule": (
            "Newest 00/06/12/18Z cycle per model with all 36 valid-time-aligned "
            "temperature messages acquired and strictly decoded; source leads <=48. "
            "Availability is observed during this discovery, not inferred from run time."
        ),
        "selected_cycles": {},
        "candidates": {"HRRR": [], "GFS": []},
    }

    def save() -> None:
        report["downloaded_bytes"] = getattr(transport, "downloaded_bytes", None)
        (directory / "selection.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8", newline="\n"
        )

    def check_time() -> None:
        if clock.now().astimezone(UTC) >= first_valid:
            report.update(
                status="unavailable",
                reason=(
                    "Reference hour expired during preparation; rerun for a future 36-hour window"
                ),
            )
            save()
            raise CurrentGuidanceUnavailableError(report["reason"])

    save()
    selected: dict[str, list[Phase2LeadAcquisition]] = {}
    for model, settings, source in (
        ("HRRR", configuration.hrrr, hrrr_phase2),
        ("GFS", configuration.gfs, gfs),
    ):
        candidates = generate_candidate_reference_times(
            target_reference_time=target,
            cadence="fixed",
            allowed_hours=settings.allowed_cycle_hours,
            max_lookback_hours=12,
        )
        for cycle in candidates:
            if (target - cycle).total_seconds() > 12 * 3600:
                continue
            check_time()
            leads = _leads(target, cycle)
            candidate: dict[str, Any] = {
                "cycle": _iso(cycle),
                "source_lead_hours": list(leads),
                "status": "checking",
                "attempts": [],
                "inputs": [],
            }
            report["candidates"][model].append(candidate)
            candidate_directory = directory / model / cycle.strftime("%Y%m%dT%HZ")
            (candidate_directory / "raw").mkdir(parents=True)
            save()
            acquisitions: dict[int, Phase2LeadAcquisition] = {}
            rejected = False
            for lead in (leads[-1], *leads[:-1]):
                check_time()
                acquired = None
                for endpoint in settings.endpoint_order:
                    kwargs: dict[str, Any] = {
                        "transport": transport,
                        "clock": clock,
                        "sleeper": sleeper,
                        "cycle_date": cycle.date(),
                        "cycle_hour": cycle.hour,
                        "forecast_hour": lead,
                        # A missing object is unavailable now; never wait for publication.
                        "cycle_deadline": clock.now() - timedelta(microseconds=1),
                        "canonical_variables": (_VARIABLE,),
                    }
                    attempt: dict[str, Any] = {
                        "lead": lead,
                        "endpoint": endpoint,
                        "index_url": source.build_index_url(
                            settings,
                            endpoint=endpoint,
                            cycle_date=cycle.date(),
                            cycle_hour=cycle.hour,
                            forecast_hour=lead,
                        ),
                        "grib_url": source.build_grib_url(
                            settings,
                            endpoint=endpoint,
                            cycle_date=cycle.date(),
                            cycle_hour=cycle.hour,
                            forecast_hour=lead,
                        ),
                    }
                    candidate["attempts"].append(attempt)
                    single_endpoint = settings.model_copy(update={"endpoint_order": (endpoint,)})
                    try:
                        if isinstance(single_endpoint, HrrrPhase2SourceSettings):
                            acquired = acquire_hrrr_phase2_lead(single_endpoint, **kwargs)
                        else:
                            acquired = acquire_gfs_lead(single_endpoint, **kwargs)
                    except FetchError as exc:
                        attempt.update(status="unavailable", reason=str(exc))
                        save()
                        continue
                    except (GribIndexError, UnicodeDecodeError) as exc:
                        attempt.update(status="invalid_inventory", reason=str(exc))
                        candidate.update(status="rejected", reason=str(exc))
                        rejected = True
                        break
                    attempt.update(
                        status="acquired",
                        index_attempts=[asdict(a) for a in acquired.index_attempts],
                        grib_attempts=[asdict(a) for a in acquired.grib_attempts],
                    )
                    entry = _retain_input(candidate_directory, acquired)
                    candidate["inputs"].append(entry)
                    save()
                    try:
                        _validate_message(acquired, settings)
                    except (HrrrPhase2DecodeError, GfsDecodeError, ValueError) as exc:
                        attempt.update(status="invalid_message", reason=str(exc))
                        candidate.update(status="rejected", reason=str(exc))
                        rejected = True
                    break
                if acquired is None and not rejected:
                    candidate.update(
                        status="rejected",
                        reason=f"Required source lead {lead} unavailable on all endpoints",
                    )
                    rejected = True
                if rejected:
                    save()
                    break
                assert acquired is not None
                acquisitions[lead] = acquired
            if rejected:
                continue
            try:
                for area in areas:
                    try:
                        normalized = normalize_temperature_messages(
                            model=model,
                            settings=settings,
                            target_reference_time=target,
                            source_cycle=cycle,
                            payloads_by_lead={
                                lead: a.selected_messages[0].payload
                                for lead, a in acquisitions.items()
                            },
                            area=area,
                        )
                    except UnsupportedCoordinateError:
                        if area == areas[-1]:
                            raise
                        continue
                    normalized.close()
                    break
            except UnsupportedCoordinateError:
                report.update(
                    status="unsupported_coordinate",
                    reason=f"Requested footprints do not intersect {model}",
                )
                save()
                raise
            except (HrrrPhase2DecodeError, GfsDecodeError, ValueError) as exc:
                candidate.update(
                    status="rejected", reason=f"Prepared-grid validation failed: {exc}"
                )
                save()
                continue
            check_time()
            candidate.update(
                status="selected",
                reason="Newest candidate with every required temperature lead acquired and decoded",
            )
            selected[model] = [acquisitions[lead] for lead in leads]
            report["selected_cycles"][model] = _iso(cycle)
            save()
            break
        if model not in selected:
            report.update(
                status="unavailable",
                reason=f"No complete usable {model} cycle supports this future 36-hour window",
            )
            save()
            raise CurrentGuidanceUnavailableError(report["reason"])
    check_time()
    report.update(status="selected", completed_at=_iso(clock.now()))
    save()
    return selected, report
