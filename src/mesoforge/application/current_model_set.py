"""Select a complete current temperature model set using provider metadata only.

One selection serves every coordinate. Acquiring and decoding the selected model
messages remains a separate preparation step; this command never issues forecasts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.application.prepared_temperature import BoundedHttpTransport, _code_identity, _iso
from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.guidance.cycle_selection import generate_candidate_reference_times
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.current_availability import (
    ProviderEvidenceError,
    probe_temperature,
)
from mesoforge.guidance.sources.ifs import IFS_CAPABILITIES
from mesoforge.guidance.sources.rap import maximum_lead

_ROOT = Path(__file__).resolve().parents[3]
_HOURS = tuple(range(1, 37))


def select_model_set(
    directory: Path,
    *,
    configuration: Phase2Configuration,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    decision_time: datetime | None = None,
    probe: Callable[..., Any] = probe_temperature,
) -> dict[str, Any]:
    """Newest metadata-complete cycles, or an explicit failure with retained evidence."""
    started = clock.now()
    decision = decision_time or started
    if any(value.tzinfo is None or value.utcoffset() is None for value in (started, decision)):
        raise ValueError("Decision and execution times must include timezones")
    started, decision = started.astimezone(UTC), decision.astimezone(UTC)
    target = decision.replace(minute=0, second=0, microsecond=0)
    first_valid = target + timedelta(hours=1)
    if decision > started or started >= first_valid:
        raise ValueError("Decision time must be at or before execution within the current UTC hour")
    directory = directory.resolve()
    if directory.is_relative_to(_ROOT):
        raise ValueError("Retain discovery evidence outside the repository")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "inventories").mkdir()
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/current_model_set.py",
        "guidance/sources/current_availability.py",
        "guidance/sources/hrrr_phase2.py",
        "guidance/sources/gfs.py",
        "guidance/sources/rap.py",
        "guidance/sources/ifs.py",
        "catalog/contributors.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    report: dict[str, Any] = {
        "status": "discovering",
        "decision_time": _iso(decision),
        "started_at": _iso(started),
        "target_reference_time": _iso(target),
        "first_valid_time": _iso(first_valid),
        "last_valid_time": _iso(target + timedelta(hours=36)),
        "expires_at": _iso(first_valid),
        "field": "air_temperature_2m",
        "horizon_hours": list(_HOURS),
        "selected_cycles": {},
        "models": {},
        "contributor_configuration": IFS_CONFIGURATION.model_dump(mode="json"),
        "source_configuration": configuration.model_dump(mode="json"),
        "code_identity": identity,
        "model_data_acquired": False,
        "selection_rule": (
            "Newest complete provider-metadata set at the fixed decision cutoff. "
            "HRRR/GFS/RAP require 36 hourly temperature messages; IFS requires every "
            "native three-hourly valid time. Older complete cycles may be selected only "
            "within existing adapter lead limits and bounded lookback, with rejection "
            "reasons retained. No cycles are spliced and no partial model set succeeds."
        ),
        "preparation_requirements": (
            "Acquire the exact selected URLs/messages outside HTTP, revalidate their "
            "recorded object/index identities and decision-time availability, then decode "
            "and validate units, native grids, model versions and valid times. Metadata "
            "discovery does not prove GRIB contents or local model ingestion at decision "
            "time. Preserve actual later acquisition and issuance timestamps."
        ),
    }

    def save() -> None:
        report["completed_at"] = _iso(clock.now())
        report["downloaded_metadata_bytes"] = getattr(transport, "downloaded_bytes", None)
        (directory / "selection.json").write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )

    def retain(evidence: dict[str, Any], payloads: dict[str, bytes]) -> dict[str, Any]:
        files = []
        for url, payload in payloads.items():
            digest = hashlib.sha256(payload).hexdigest()
            filename = f"inventories/{digest}.index"
            path = directory / filename
            if path.exists() and path.read_bytes() != payload:
                raise ValueError("Retained discovery checksum collision")
            path.write_bytes(payload)
            files.append({"url": url, "file": filename, "sha256": digest, "bytes": len(payload)})
        return {**evidence, "retained_indexes": files}

    def expired() -> bool:
        if clock.now().astimezone(UTC) < first_valid:
            return False
        report.update(
            status="unavailable",
            selected_cycles={},
            reason="The forecast reference hour expired during discovery; select again.",
        )
        save()
        return True

    save()
    for definition in IFS_CONFIGURATION.models:
        model = definition.model_id
        # Model registrations describe adapter envelopes, not actual availability.
        native_step = IFS_CAPABILITIES["native_step_hours"] if model == "IFS" else 1
        assert isinstance(native_step, int) and native_step > 0
        lookback = min(24, max(definition.supported_leads) - 36)
        model_report: dict[str, Any] = {
            "status": "unavailable",
            "selected_cycle": None,
            "candidate_lookback_hours": lookback,
            "candidates": [],
        }
        report["models"][model] = model_report
        cycles = generate_candidate_reference_times(
            target_reference_time=target,
            cadence="fixed",
            allowed_hours=definition.cycle_hours,
            max_lookback_hours=lookback,
        )
        for cycle in cycles:
            age = int((target - cycle).total_seconds() / 3600)
            if age > lookback:
                continue
            if expired():
                return report
            required = [age + hour for hour in _HOURS if (age + hour) % native_step == 0]
            supported_max = (
                maximum_lead(cycle) if model == "RAP" else max(definition.supported_leads)
            )
            candidate: dict[str, Any] = {
                "cycle": _iso(cycle),
                "source_leads": required,
                "status": "checking",
                "probes": [],
            }
            model_report["candidates"].append(candidate)
            if any(
                lead > supported_max or lead not in definition.supported_leads for lead in required
            ):
                candidate.update(
                    status="rejected", reason="Cycle cannot cover the required valid-time window"
                )
                continue
            for lead in (required[-1], *required[:-1]):
                if expired():
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
                    )
                except ProviderEvidenceError as exc:
                    candidate["probes"].append(retain(exc.evidence, exc.index_payloads))
                    candidate.update(status="error", reason=str(exc))
                    model_report.update(status="error", reason=str(exc))
                    report.update(status="error", selected_cycles={}, reason=str(exc))
                    save()
                    return report
                candidate["probes"].append(retain(result.evidence, result.index_payloads))
                if expired():
                    return report
                if not result.available:
                    candidate.update(status="rejected", reason=result.reason)
                    break
                save()
            else:
                candidate.update(
                    status="metadata_complete",
                    reason=(
                        "Every required temperature inventory and GRIB object passed "
                        "the fixed decision cutoff"
                    ),
                )
                model_report.update(
                    status="metadata_complete",
                    selected_cycle=_iso(cycle),
                    source_leads=required,
                    valid_times=[_iso(cycle + timedelta(hours=lead)) for lead in required],
                    expected_native_gaps=[
                        {
                            "horizon_hours": hour,
                            "valid_time": _iso(target + timedelta(hours=hour)),
                            "reason": "No native prediction at this valid time; no interpolation",
                        }
                        for hour in _HOURS
                        if age + hour not in required
                    ],
                )
                break
            save()
        save()
    if expired():
        return report
    missing = [
        model
        for model, result in report["models"].items()
        if result["status"] != "metadata_complete"
    ]
    if missing:
        report.update(
            status="unavailable", reason="No complete compatible model set: " + ", ".join(missing)
        )
    else:
        report.update(
            status="selected",
            selected_cycles={
                model: row["selected_cycle"] for model, row in report["models"].items()
            },
            reason=(
                "All four models are metadata-complete at the decision cutoff; "
                "preparation is required."
            ),
        )
    save()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--decision-time", type=datetime.fromisoformat)
    args = parser.parse_args(argv)
    configuration, _ = load_configuration_source(
        base_path=_ROOT / "configs/base.yaml",
        additional_overlay_paths=(
            _ROOT / "configs/phase1-grasston.yaml",
            _ROOT / "configs/phase2-grasston.yaml",
        ),
    )
    assert configuration.phase2 is not None
    clock = SystemClock()
    transport = BoundedHttpTransport()
    try:
        report = select_model_set(
            args.output_dir,
            configuration=configuration.phase2,
            decision_time=args.decision_time,
            transport=transport,
            clock=clock,
            sleeper=SystemSleeper(),
        )
    finally:
        transport.close()
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] == "selected" else 2


if __name__ == "__main__":
    raise SystemExit(main())
