"""Prepare an exact current-model-set selection, then use existing batch issuance."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mesoforge.application.batch_forecast import (
    _coordinates,
    create_issuer,
    load_locations,
    run_batch,
)
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION, prepare_ifs
from mesoforge.application.prepared_rap import prepare_rap
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _hour,
    _iso,
    _retain_input,
    _write_bytes,
    prepare_temperature_guidance,
)
from mesoforge.application.spatial_coverage import plan_regions, validate_coordinate
from mesoforge.application.spatial_preparation import ensure_coverage
from mesoforge.catalog.configuration import Phase2Configuration, _lists_to_tuples
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    acquire_gfs_lead,
    acquire_hrrr_phase2_lead,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.selected_objects import SelectedObjectTransport
from mesoforge.guidance.sources.rap import maximum_lead

_ROOT = Path(__file__).resolve().parents[3]
_HOURS = list(range(1, 37))


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Selection timestamps must be timezone aware")
    return parsed.astimezone(UTC)


def load_selection(
    selection_path: Path, *, clock: Clock
) -> tuple[dict[str, Any], Phase2Configuration, list[dict[str, Any]]]:
    """Validate complete current evidence and every retained selected inventory."""
    report = json.loads(selection_path.read_bytes())
    try:
        decision = _time(report["decision_time"])
        target = _hour(_time(report["target_reference_time"]))
        first = target + timedelta(hours=1)
        models = IFS_CONFIGURATION.model_map()
        if (
            report["status"] != "selected"
            or report["field"] != "air_temperature_2m"
            or report["horizon_hours"] != _HOURS
            or report["contributor_configuration"] != IFS_CONFIGURATION.model_dump(mode="json")
            or set(report["selected_cycles"]) != set(models)
            or set(report["models"]) != set(models)
            or target != decision.replace(minute=0, second=0, microsecond=0)
            or _time(report["first_valid_time"]) != first
            or _time(report["expires_at"]) != first
            or _time(report["last_valid_time"]) != target + timedelta(hours=36)
            or not decision <= _time(report["started_at"]) <= _time(report["completed_at"])
            or not _time(report["completed_at"]) <= clock.now() < first
        ):
            raise ValueError("Selection must be complete, unchanged and unexpired; discover again")
        configuration = Phase2Configuration.model_validate(
            _lists_to_tuples(report["source_configuration"])
        )
        probes = []
        for model, definition in models.items():
            row = report["models"][model]
            cycle = _hour(_time(report["selected_cycles"][model]))
            age = int((target - cycle).total_seconds() / 3600)
            native_step = 3 if model == "IFS" else 1
            required = [age + hour for hour in _HOURS if (age + hour) % native_step == 0]
            maximum = maximum_lead(cycle) if model == "RAP" else max(definition.supported_leads)
            selected = [c for c in row["candidates"] if c["status"] == "metadata_complete"]
            if (
                row["status"] != "metadata_complete"
                or row["selected_cycle"] != _iso(cycle)
                or cycle.hour not in definition.cycle_hours
                or not 0 <= age <= min(24, max(definition.supported_leads) - 36)
                or any(
                    lead > maximum or lead not in definition.supported_leads for lead in required
                )
                or row["source_leads"] != required
                or row["valid_times"] != [_iso(cycle + timedelta(hours=lead)) for lead in required]
                or len(selected) != 1
                or selected[0]["cycle"] != _iso(cycle)
                or selected[0]["source_leads"] != required
            ):
                raise ValueError(f"{model}: selected cycle or native lead coverage disagrees")
            entries = selected[0]["probes"]
            if (
                len(entries) != len(required)
                or sorted(p["source_lead_hours"] for p in entries) != required
            ):
                raise ValueError(
                    f"{model}: selected inventory evidence is incomplete or duplicated"
                )
            for probe in entries:
                lead = probe["source_lead_hours"]
                if (
                    probe["status"] != "available"
                    or probe["model"] != model
                    or probe["cycle"] != _iso(cycle)
                    or probe["valid_time"] != _iso(cycle + timedelta(hours=lead))
                    or _time(probe["decision_time"]) != decision
                    or _time(probe["index"]["available_at"]) > decision
                    or _time(probe["grib"]["available_at"]) > decision
                ):
                    raise ValueError(f"{model}: probe identity disagrees with selection")
                inventory = probe["index"]
                retained = [p for p in probe["retained_indexes"] if p["url"] == inventory["url"]]
                if len(retained) != 1:
                    raise ValueError(f"{model}: original selected inventory is required")
                entry = retained[0]
                path = (selection_path.parent / entry["file"]).resolve()
                if not path.is_relative_to((selection_path.parent / "inventories").resolve()):
                    raise ValueError("Selected inventory path escapes its retained directory")
                payload = path.read_bytes()
                if (
                    hashlib.sha256(payload).hexdigest() != entry["sha256"]
                    or entry["sha256"] != inventory["sha256"]
                    or len(payload) != entry["bytes"]
                    or len(payload) != inventory["content_bytes"]
                ):
                    raise ValueError(f"{model}: retained inventory checksum or byte count differs")
                probes.append(probe)
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("Incomplete current-model-set selection evidence") from exc
    return report, configuration, probes


def _acquire_control(
    selection: dict[str, Any],
    configuration: Phase2Configuration,
    source: Path,
    transport: SelectedObjectTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, list[Phase2LeadAcquisition]]:
    """Use existing acquisition with each lead pinned to its discovered provider."""
    (source / "raw").mkdir(parents=True)
    acquired: dict[str, list[Phase2LeadAcquisition]] = {}
    for model in ("HRRR", "GFS"):
        result = acquired[model] = []
        cycle = _time(selection["selected_cycles"][model])
        candidate = next(
            c
            for c in selection["models"][model]["candidates"]
            if c["status"] == "metadata_complete"
        )
        for probe in sorted(candidate["probes"], key=lambda p: p["source_lead_hours"]):
            settings = configuration.hrrr if model == "HRRR" else configuration.gfs
            # A different mirror is a different object; discovery already chose this one.
            settings = settings.model_copy(update={"endpoint_order": (probe["selected_endpoint"],)})
            acquire = acquire_hrrr_phase2_lead if model == "HRRR" else acquire_gfs_lead
            row = acquire(
                settings,  # type: ignore[arg-type]
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=probe["source_lead_hours"],
                canonical_variables=("air_temperature_2m",),
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_deadline=clock.now(),
            )
            _retain_input(source, row)
            result.append(row)
    return acquired


def prepare_selected(
    locations: list[Any],
    selection_path: Path,
    output_directory: Path,
    *,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    """Prepare one selected source set and share regional views across the collection."""
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    selection, configuration, probes = load_selection(selection_path, clock=clock)
    selection_bytes = selection_path.read_bytes()
    if json.loads(selection_bytes) != selection:
        raise ValueError("Selection changed after validation; discover again")
    coordinates = []
    for location in locations:
        try:
            lat, lon = _coordinates(location)
            validate_coordinate(lat, lon)
        except ValueError:
            continue  # Existing batch command reports the original invalid entry.
        coordinates.append((lat, lon))
    areas = plan_regions(coordinates)
    if not areas:
        raise ValueError("At least one valid coordinate is required for preparation")
    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(_ROOT):
        raise ValueError("Prepared guidance and raw evidence must remain outside Git")
    output_directory.mkdir(parents=True, exist_ok=False)
    # Copy the exact discovery artifact and all its inventories before acquisition.
    discovery = output_directory / "discovery"
    discovery.mkdir()
    _write_bytes(discovery / "selection.json", selection_bytes)
    shutil.copytree(selection_path.parent / "inventories", discovery / "inventories")
    copied, _, _ = load_selection(discovery / "selection.json", clock=clock)
    if copied != selection:
        raise ValueError("Retained discovery copy differs from the validated selection")
    control = output_directory / "control"
    source = control / "source"
    target = _time(selection["target_reference_time"])
    owned = BoundedHttpTransport() if transport is None else None
    pinned = SelectedObjectTransport(
        transport if transport is not None else owned,  # type: ignore[arg-type]
        probes,
        decision_time=_time(selection["decision_time"]),
        clock=clock,
    )
    shadows = {}
    try:
        acquired = _acquire_control(
            selection, configuration, output_directory / "acquired", pinned, clock, sleeper
        )
        manifest = prepare_temperature_guidance(
            source,
            configuration=configuration,
            target_reference_time=target,
            hrrr_cycle=_time(selection["selected_cycles"]["HRRR"]),
            gfs_cycle=_time(selection["selected_cycles"]["GFS"]),
            transport=pinned,
            clock=clock,
            sleeper=sleeper,
            area=areas[0],
            fallback_areas=tuple(areas[1:]),
            acquired_inputs=acquired,
        )
        valid_locations = [{"lat": lat, "lon": lon} for lat, lon in coordinates]
        for model, prepare in (("RAP", prepare_rap), ("IFS", prepare_ifs)):
            report = prepare(
                valid_locations,
                source,
                output_directory / model,
                cycle_override=_time(selection["selected_cycles"][model]),
                transport=pinned,
                clock=clock,
                sleeper=sleeper,
            )
            expected = [
                int((_time(valid) - target).total_seconds() / 3600)
                for valid in selection["models"][model]["valid_times"]
            ]
            if (
                report["selected_cycle"] != selection["selected_cycles"][model]
                or report["supported_hours"] != expected
            ):
                raise ValueError(
                    f"{model}: preparation did not retain every selected native hour; no issuance"
                )
            shadows[model] = report
        pinned.assert_complete()
        if clock.now() >= _time(selection["expires_at"]):
            raise ValueError(
                "Selection expired during preparation; retain inputs and discover again"
            )
        evidence = {
            "selection_sha256": hashlib.sha256(selection_bytes).hexdigest(),
            "selection": selection,
            "object_validation": pinned.validations,
            "preparation_code_sha256": {
                name: hashlib.sha256((_ROOT / "src/mesoforge" / name).read_bytes()).hexdigest()
                for name in ("application/selected_forecast.py", "guidance/selected_objects.py")
            },
        }
        manifest["current_model_set"] = evidence
        manifest["availability_note"] = (
            "Exact provider objects revalidated against the current-model-set decision cutoff; "
            "actual acquisition and issuance occur later and retain their own timestamps."
        )
        # This source belongs to this new, unpublished preparation. Finalize its
        # evidence atomically only after all four models pass, before regional reuse.
        completed_manifest = source / "manifest.complete.json"
        _write_bytes(completed_manifest, json.dumps(manifest, indent=2).encode())
        completed_manifest.replace(source / "manifest.json")
        shadow_directories = {model: str(output_directory / model) for model in shadows}
        _, coverage = ensure_coverage(
            locations,
            source,
            cache_directory=control,
            contributor_configuration=IFS_CONFIGURATION,
            shadow_directories={model: Path(path) for model, path in shadow_directories.items()},
        )
        report = {
            "directory": str(control),
            "shadow_directories": shadow_directories,
            "current_model_set": evidence,
            "coverage": coverage,
            "shadows": shadows,
            "downloaded_bytes": pinned.downloaded_bytes,
            "retained_raw_bytes": sum(row["raw_bytes"] for row in manifest["inputs"])
            + sum(row["retained_raw_bytes"] for row in shadows.values()),
        }
        _write_bytes(output_directory / "preparation.json", json.dumps(report, indent=2).encode())
        return report
    except Exception as exc:
        _write_bytes(
            output_directory / "failure.json",
            json.dumps(
                {"error": str(exc), "object_validation": pinned.validations}, indent=2
            ).encode(),
        )
        raise
    finally:
        if owned is not None:
            owned.close()


def run_selected_batch(
    config_path: Path,
    selection_path: Path,
    output_directory: Path,
    *,
    issuer: ForecastIssuanceService | None = None,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    forecast_report_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """No manual cycles; existing shared preparation and immutable persistence paths."""
    locations = load_locations(config_path)
    issuer = issuer if issuer is not None else create_issuer()
    preparation = prepare_selected(
        locations,
        selection_path,
        output_directory,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
    )
    batch = run_batch(
        config_path,
        Path(preparation["directory"]),
        issuer=issuer,
        require_future_hours=True,
        contributor_configuration=IFS_CONFIGURATION,
        shadow_directories={
            model: Path(path) for model, path in preparation["shadow_directories"].items()
        },
        forecast_report_builder=forecast_report_builder,
    )
    return {**batch, "preparation": preparation}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--selection",
        type=Path,
        required=True,
        help="Current selection.json with retained inventories",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New prepared directory outside Git"
    )
    args = parser.parse_args(argv)
    try:
        result = run_selected_batch(args.config, args.selection, args.output_dir)
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "selected_batch_failed", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if any(row["status"] == "error" for row in result["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
