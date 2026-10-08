"""Explicit 120-hour discovery using nominal native schedules and pinned evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mesoforge.application.prepared_temperature import _code_identity, _iso
from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.catalog.native_horizons import NATIVE_HORIZON_CONTRACT, native_field_contract
from mesoforge.common.horizon import ForecastHorizon
from mesoforge.forecasting.recipes import PROVISIONAL_CONFIGURATION
from mesoforge.guidance.cycle_selection import generate_candidate_reference_times
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources.current_availability import ProviderEvidenceError, probe_temperature

NATIVE_PREPARATION_POLICY = "mesoforge.native-preparation.120h.v1"
NATIVE_SOURCES = ("HRRR", "RAP", "GFS", "IFS", "NBM")
_ROOT = Path(__file__).resolve().parents[3]


def native_window_usable(target: datetime, coverage_hours: int, now: datetime) -> bool:
    """A full prospective reference view must remain after background work."""
    if any(value.tzinfo is None or value.utcoffset() is None for value in (target, now)):
        raise ValueError("Native prepared-window clocks must be timezone aware")
    reference = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if now > reference:
        reference += timedelta(hours=1)
    return reference + timedelta(hours=120) <= target + timedelta(hours=coverage_hours)


def required_native_leads(
    model: str, cycle: datetime, target: datetime, coverage_hours: int
) -> tuple[int, ...]:
    """Include native brackets and the previous QPF parent, never extrapolate.

    A short source's disappearance is normal. Long-range sources must bracket
    the complete requested state window. Lead zero is retained when a state
    interpolation bracket needs it, but it is not a precipitation event.
    """
    contract = native_field_contract(model, "air_temperature_2m")
    wanted: set[int] = set()
    for hour in range(1, coverage_hours + 1):
        wanted.update(contract.plan(cycle, target + timedelta(hours=hour)).source_leads)
    if not wanted:
        return ()
    native = contract.native_leads(cycle)
    first = min(wanted)
    preceding = [lead for lead in native if 0 < lead < first]
    if preceding:
        wanted.add(preceding[-1])
    return tuple(sorted(wanted))


def select_native_model_set(
    directory: Path,
    *,
    configuration: Phase2Configuration,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    decision_time: datetime | None = None,
    coverage_hours: int = 126,
    probe: Callable[..., Any] = probe_temperature,
) -> dict[str, Any]:
    """Pin every selected object before acquisition, with bounded per-source lookback."""
    if type(coverage_hours) is not int or not 120 <= coverage_hours <= 126:
        raise ValueError("Native five-day preparation covers 120..126 target hours")
    started = clock.now()
    decision = decision_time or started
    if any(value.tzinfo is None or value.utcoffset() is None for value in (started, decision)):
        raise ValueError("Discovery clock and cutoff must be timezone aware")
    started, decision = started.astimezone(UTC), decision.astimezone(UTC)
    target = decision.replace(minute=0, second=0, microsecond=0)
    first = target + timedelta(hours=1)
    if decision > started or started >= first:
        raise ValueError("Discovery cutoff must belong to the current runtime reference hour")
    directory = directory.resolve()
    if directory.is_relative_to(_ROOT):
        raise ValueError("Retain native discovery outside Git")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "inventories").mkdir()
    report: dict[str, Any] = {
        "status": "discovering",
        "native_preparation_policy": NATIVE_PREPARATION_POLICY,
        "native_horizon_contract": NATIVE_HORIZON_CONTRACT,
        "forecast_horizon": ForecastHorizon(120).payload(),
        "horizon_hours": list(range(1, coverage_hours + 1)),
        "decision_time": _iso(decision),
        "started_at": _iso(started),
        "target_reference_time": _iso(target),
        "first_valid_time": _iso(first),
        "last_valid_time": _iso(target + timedelta(hours=coverage_hours)),
        "expires_at": _iso(target + timedelta(hours=coverage_hours - 120)),
        "expiry_semantics": "last reference supporting all 120 prospective hours",
        "field": "air_temperature_2m",
        "surface_fields": True,
        "qpf_fields": True,
        "source_configuration": configuration.model_dump(mode="json"),
        "contributor_configuration": PROVISIONAL_CONFIGURATION.model_dump(mode="json"),
        "code_identity": _code_identity(),
        "selected_cycles": {},
        "models": {},
        "model_data_acquired": False,
        "coverage": {
            "policy": NATIVE_PREPARATION_POLICY,
            "requested_hours": coverage_hours,
            "prepared_hours": coverage_hours,
            "models": {},
        },
        "source_shortfalls": {},
    }

    def save() -> None:
        report["completed_at"] = _iso(clock.now())
        (directory / "selection.json").write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )

    def retain(evidence: dict[str, Any], payloads: dict[str, bytes]) -> dict[str, Any]:
        retained = []
        for url, payload in payloads.items():
            digest = hashlib.sha256(payload).hexdigest()
            filename = f"inventories/{digest}.index"
            path = directory / filename
            if path.exists() and path.read_bytes() != payload:
                raise ValueError("Discovery inventory digest collision")
            path.write_bytes(payload)
            retained.append({"url": url, "file": filename, "sha256": digest, "bytes": len(payload)})
        return {**evidence, "retained_indexes": retained}

    save()
    for model in NATIVE_SOURCES:
        allowed = tuple(range(24)) if model in {"HRRR", "RAP", "NBM"} else (0, 6, 12, 18)
        row: dict[str, Any] = {"status": "unavailable", "selected_cycle": None, "candidates": []}
        report["models"][model] = row
        for cycle in generate_candidate_reference_times(
            target_reference_time=target,
            cadence="fixed",
            allowed_hours=allowed,
            max_lookback_hours=24,
        ):
            leads = required_native_leads(model, cycle, target, coverage_hours)
            contract = native_field_contract(model, "air_temperature_2m")
            if not leads or (
                model in {"GFS", "IFS", "NBM"}
                and contract.plan(cycle, target + timedelta(hours=coverage_hours)).status
                == "outside_native_horizon"
            ):
                continue
            candidate: dict[str, Any] = {
                "cycle": _iso(cycle),
                "source_leads": list(leads),
                "probes": [],
                "status": "checking",
            }
            row["candidates"].append(candidate)
            for lead in (leads[-1], *leads[:-1]):
                if not native_window_usable(target, coverage_hours, clock.now()):
                    report.update(
                        status="unavailable",
                        reason="Reference hour expired during native discovery",
                    )
                    save()
                    return report
                try:
                    result = probe(
                        model=model,
                        cycle=cycle,
                        lead=lead,
                        configuration=configuration,
                        transport=transport,
                        clock=clock,
                        sleeper=sleeper,
                        decision_time=decision,
                        native_fields=True,
                    )
                except ProviderEvidenceError as exc:
                    candidate["probes"].append(retain(exc.evidence, exc.index_payloads))
                    candidate.update(status="error", reason=str(exc))
                    break
                candidate["probes"].append(retain(result.evidence, result.index_payloads))
                if not result.available:
                    candidate.update(status="rejected", reason=result.reason)
                    break
            else:
                candidate["status"] = "metadata_complete"
                row.update(
                    status="metadata_complete",
                    selected_cycle=_iso(cycle),
                    source_leads=list(leads),
                    valid_times=[_iso(cycle + timedelta(hours=lead)) for lead in leads],
                )
                report["selected_cycles"][model] = _iso(cycle)
                report["coverage"]["models"][model] = {
                    "native_source_leads": list(leads),
                    "expires_at": row["valid_times"][-1],
                }
                break
            if candidate["status"] == "error":
                # Unknown provider identity/availability is not permission to pick
                # an older source. Preserve the failed contributor explicitly.
                break
        if row["status"] != "metadata_complete":
            report["source_shortfalls"][model] = {
                "stage": "discovery",
                "reason": row["candidates"][-1].get("reason", "No eligible cycle")
                if row["candidates"]
                else "No native source window",
            }
        save()
    if not {"GFS", "IFS", "NBM"}.intersection(report["selected_cycles"]):
        report.update(
            status="unavailable", reason="No eligible long-range native state contributor"
        )
    else:
        report["status"] = "selected"
    save()
    return report


def load_native_selection(
    path: Path, *, clock: Clock
) -> tuple[dict[str, Any], Phase2Configuration, list[dict[str, Any]]]:
    """Validate retained metadata, exact schedules and raw index identities offline."""
    from mesoforge.catalog.configuration import _lists_to_tuples
    from mesoforge.guidance.index_parsing import compute_message_byte_range
    from mesoforge.guidance.sources.current_availability import _select
    from mesoforge.guidance.sources.native_fields import selected_native_fields

    report = json.loads(path.read_bytes())
    if (
        report.get("native_preparation_policy") != NATIVE_PREPARATION_POLICY
        or report.get("forecast_horizon") != ForecastHorizon(120).payload()
        or report.get("status") != "selected"
        or report.get("contributor_configuration")
        != PROVISIONAL_CONFIGURATION.model_dump(mode="json")
    ):
        raise ValueError("Invalid native preparation selection contract")
    target = datetime.fromisoformat(report["target_reference_time"])
    cutoff = datetime.fromisoformat(report["decision_time"])
    coverage = report["coverage"]["prepared_hours"]
    if (
        not 120 <= coverage <= 126
        or report["horizon_hours"] != list(range(1, coverage + 1))
        or target != cutoff.replace(minute=0, second=0, microsecond=0)
        or not cutoff <= clock.now()
        or not native_window_usable(target, coverage, clock.now())
    ):
        raise ValueError("Native selection is expired or has contradictory coverage")
    probes = []
    omitted = set(NATIVE_SOURCES) - set(report["selected_cycles"])
    if set(report["models"]) != set(NATIVE_SOURCES) or set(report["source_shortfalls"]) != omitted:
        raise ValueError("Native missing contributors require explicit discovery shortfalls")
    for model, cycle_text in report["selected_cycles"].items():
        if model not in NATIVE_SOURCES:
            raise ValueError("Unregistered native source")
        cycle = datetime.fromisoformat(cycle_text)
        if not timedelta(0) <= target - cycle <= timedelta(hours=24):
            raise ValueError("Native source cycle exceeds the bounded freshness window")
        row = report["models"][model]
        required = required_native_leads(model, cycle, target, coverage)
        candidates = [item for item in row["candidates"] if item["status"] == "metadata_complete"]
        if (
            len(candidates) != 1
            or row["source_leads"] != list(required)
            or candidates[0]["cycle"] != cycle_text
        ):
            raise ValueError("Native selected cycle or schedule changed")
        records = candidates[0]["probes"]
        if sorted(item["source_lead_hours"] for item in records) != list(required):
            raise ValueError("Native selected object set is incomplete")
        for record in records:
            if (
                record["model"] != model
                or record["cycle"] != cycle_text
                or record["decision_time"] != report["decision_time"]
                or record["valid_time"]
                != _iso(cycle + timedelta(hours=record["source_lead_hours"]))
            ):
                raise ValueError("Native selected object source/time identity changed")
            index = next(
                item for item in record["retained_indexes"] if item["url"] == record["index"]["url"]
            )
            file = (path.parent / index["file"]).resolve()
            if not file.is_relative_to(path.parent.resolve()):
                raise ValueError("Native inventory path escapes retention directory")
            payload = file.read_bytes()
            if (
                hashlib.sha256(payload).hexdigest() != index["sha256"]
                or index["sha256"] != record["index"]["sha256"]
                or len(payload) != index["bytes"]
            ):
                raise ValueError("Native retained inventory checksum mismatch")
            rows, selected, explicit_end = _select(
                model, payload, cycle, record["source_lead_hours"]
            )
            start, end = (
                (selected.byte_offset, explicit_end)
                if explicit_end is not None
                else compute_message_byte_range(
                    rows, selected=selected, full_object_length=record["grib"]["content_length"]
                )
            )
            expected = {
                "canonical_variable_id": "air_temperature_2m",
                "index_row": selected.line,
                "byte_start": start,
                "byte_end_exclusive": end,
                "content_bytes": end - start,
            }
            if record["selected_message"] != expected:
                raise ValueError("Native selected temperature differs from retained inventory")
            messages, missing = selected_native_fields(
                model, payload, cycle, record["source_lead_hours"], record["grib"]["content_length"]
            )
            if messages != record["extra_messages"] or missing != record["missing_fields"]:
                raise ValueError("Native inventory messages changed since discovery")
            probes.append(record)
    if not {"GFS", "IFS", "NBM"}.intersection(report["selected_cycles"]):
        raise ValueError("Native selection lacks a complete long-range state source")
    return (
        report,
        Phase2Configuration.model_validate(_lists_to_tuples(report["source_configuration"])),
        probes,
    )
