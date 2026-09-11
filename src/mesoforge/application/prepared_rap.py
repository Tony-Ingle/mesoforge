"""Prepare a separate RAP temperature shadow snapshot for an existing control window.

This is an explicit pre-HTTP operation. Coordinates use the existing spatial planner;
one set of temperature messages supplies all regions. The active source snapshot is
never edited. Repeats and --from-raw do not construct a network transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.point_forecast import _verify_file
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _code_identity,
    _hour,
    _iso,
    _retain_input,
    _write_bytes,
    _write_prepared_file,
)
from mesoforge.application.spatial_coverage import UnsupportedCoordinateError, plan_regions
from mesoforge.catalog.contributors import RAP_MODEL_DEFINITION
from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.rap import (
    RAP_CAPABILITIES,
    acquire_rap_lead,
    decode_temperature_message,
    discover_rap_cycle,
)

RAP_CONFIGURATION = ContributorConfiguration(
    models=(*DEFAULT_CONFIGURATION.models, RAP_MODEL_DEFINITION),
    control_recipe=DEFAULT_CONFIGURATION.control_recipe,
    comparison_recipes=DEFAULT_CONFIGURATION.comparison_recipes,
)
_HOURS = tuple(range(1, 37))
_SOURCE_METADATA = {
    "model_definition": RAP_MODEL_DEFINITION.model_dump(mode="json"),
    "capabilities": json.loads(json.dumps(RAP_CAPABILITIES)),
    "adapter_version": "rap_temperature_v1",
    "product": "awp130pgrb",
    "scientific_version": "RAPv5",
    "documented_production_package": "rap.v5.1.24",
    "version_note": "Documented NOAA package, not a patch version asserted by each GRIB message.",
    "metadata_sources": [
        "https://www.nco.ncep.noaa.gov/pmb/products/rap/",
        "https://www.nco.ncep.noaa.gov/pmb/codes/nwprod/",
        "https://www.weather.gov/media/notification/pdf2/scn20-46rap_v5_hrrr_v4_aab.pdf",
    ],
}


def _identity() -> dict[str, Any]:
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_rap.py",
        "application/prepared_shadow.py",
        "guidance/sources/rap.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return identity


def _source(directory: Path) -> Path:
    coverage = directory / "coverage.json"
    return (
        Path(json.loads(coverage.read_text())["source_directory"])
        if coverage.is_file()
        else directory
    )


def _target(control_directory: Path) -> datetime:
    manifest = json.loads((_source(control_directory) / "manifest.json").read_text())
    if manifest.get("data_kind") != "real_prepared_guidance" or manifest.get(
        "target_horizon_hours"
    ) != list(_HOURS):
        raise ValueError("RAP shadow preparation requires an existing real 1..36 control snapshot")
    return _hour(datetime.fromisoformat(manifest["target_reference_time"]))


def _raw_inputs(source: Path, manifest: dict[str, Any]) -> dict[int, bytes]:
    """Check every retained byte before rebuilding or reporting offline reuse."""
    inputs: dict[int, bytes] = {}
    target = _hour(datetime.fromisoformat(manifest["target_reference_time"]))
    for row in manifest["inputs"]:
        lead = row["source_lead_hours"]
        cycle = _hour(datetime.fromisoformat(row["cycle"]))
        valid = cycle + timedelta(hours=lead)
        if (
            row["model"] != "RAP"
            or type(lead) is not int
            or lead in inputs
            or row["cycle"] != manifest["selected_cycle"]
            or row["valid_time"] != _iso(valid)
            or valid not in [target + timedelta(hours=hour) for hour in _HOURS]
        ):
            raise ValueError("RAP retained cycle, lead or valid time disagrees")
        for prefix, suffix in (("raw", "grib2"), ("index", "idx")):
            filename = row[f"{prefix}_file"]
            if filename != f"raw/RAP-f{lead:03d}.{suffix}":
                raise ValueError("Invalid retained RAP input path")
            _verify_file(source, filename, row[f"{prefix}_sha256"])
            if (source / filename).stat().st_size != row[f"{prefix}_bytes"]:
                raise ValueError("Retained RAP input byte count disagrees")
        inputs[lead] = (source / row["raw_file"]).read_bytes()
    return inputs


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def prepare_rap(
    locations: list[Any],
    control_directory: Path,
    output_directory: Path,
    *,
    from_raw: Path | None = None,
    cycle_override: datetime | None = None,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    """Acquire once, or rebuild/reuse retained RAP independently of the control files."""
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    target = _target(control_directory)
    areas = plan_regions([_coordinates(location) for location in locations])
    if not areas:
        raise ValueError("At least one coordinate is required")
    if from_raw is not None and cycle_override is not None:
        raise ValueError("Offline rebuilding uses retained cycles; do not override them")
    output_directory = output_directory.resolve()
    report_path = output_directory / "coverage.json"
    if report_path.is_file():
        if from_raw is not None:
            raise ValueError("Offline rebuild requires a new output directory")
        report = json.loads(report_path.read_text())
        source = Path(report["source_directory"])
        manifest = json.loads((source / "manifest.json").read_text())
        if (
            manifest["target_reference_time"] != _iso(target)
            or report["requested_areas"] != [area.model_dump() for area in areas]
            or (cycle_override is not None and manifest["selected_cycle"] != _iso(cycle_override))
        ):
            raise ValueError("Existing RAP snapshot differs; use a new output directory")
        _raw_inputs(source, manifest)
        for region in report["regions"]:
            directory = Path(region["directory"])
            view = json.loads((directory / "manifest.json").read_text())
            _raw_inputs(directory, view)
            for prepared in view["prepared_files"].values():
                _verify_file(directory, prepared["file"], prepared["sha256"])
        return {**report, "downloaded_bytes": 0, "mode": "reused_retained_shadow"}
    output_directory.mkdir(parents=True, exist_ok=True)
    if any(output_directory.iterdir()):
        raise ValueError("RAP preparation requires an empty directory; existing files are retained")
    source = output_directory / "source"
    (source / "raw").mkdir(parents=True)
    owned_transport = None
    missing: dict[int, str] = {}
    rows = []
    payloads: dict[int, bytes] = {}
    if from_raw is not None:
        retained = _source(from_raw)
        retained_payload = (retained / "manifest.json").read_bytes()
        manifest = json.loads(retained_payload)
        if (
            manifest.get("source_metadata") != _SOURCE_METADATA
            or manifest.get("target_reference_time") != _iso(target)
            or manifest.get("target_horizon_hours") != list(_HOURS)
        ):
            raise ValueError("Retained RAP capabilities or control window differ")
        payloads = _raw_inputs(retained, manifest)
        rows = manifest["inputs"]
        for row in rows:
            for prefix in ("raw", "index"):
                shutil.copyfile(retained / row[f"{prefix}_file"], source / row[f"{prefix}_file"])
        _write_bytes(source / "source-manifest.json", retained_payload)
        missing = {int(hour): reason for hour, reason in manifest["missing_hours"].items()}
        cycle = (
            datetime.fromisoformat(manifest["selected_cycle"])
            if manifest["selected_cycle"] is not None
            else None
        )
        selection = manifest["cycle_selection"]
        downloaded = 0
    else:
        if transport is None:
            owned_transport = BoundedHttpTransport()
            transport = owned_transport
        try:
            selection_result = discover_rap_cycle(
                target_reference_time=target,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_override=cycle_override,
            )
            selection = json.loads(json.dumps(asdict(selection_result), default=_json_default))
            cycle = selection_result.cycle
            missing.update(selection_result.missing_hours)
            if cycle is not None:
                for lead in selection_result.available_leads:
                    hour = int((cycle + timedelta(hours=lead) - target).total_seconds() / 3600)
                    try:
                        acquired = acquire_rap_lead(
                            cycle=cycle,
                            forecast_hour=lead,
                            transport=transport,
                            clock=clock,
                            sleeper=sleeper,
                            cycle_deadline=clock.now(),
                        )
                    except (MesoForgeError, ValueError) as exc:
                        missing[hour] = f"RAP acquisition unavailable: {type(exc).__name__}: {exc}"
                        continue
                    rows.append(_retain_input(source, acquired))
                    payloads[lead] = acquired.selected_messages[0].payload
            downloaded = getattr(
                transport,
                "downloaded_bytes",
                sum(row["raw_bytes"] + row["index_bytes"] for row in rows),
            )
        finally:
            if owned_transport is not None:
                owned_transport.close()
    decoded = {}
    for lead, payload in payloads.items():
        assert cycle is not None
        hour = int((cycle + timedelta(hours=lead) - target).total_seconds() / 3600)
        try:
            decoded[lead] = decode_temperature_message(payload, cycle=cycle, forecast_hour=lead)
        except (MesoForgeError, ValueError) as exc:
            missing[hour] = f"RAP decoding unavailable: {type(exc).__name__}: {exc}"
    supported = [
        hour
        for hour in _HOURS
        if cycle is not None and int((target - cycle).total_seconds() / 3600) + hour in decoded
    ]
    missing = {hour: reason for hour, reason in missing.items() if hour not in supported}
    for hour in _HOURS:
        if hour not in supported:
            missing.setdefault(hour, "RAP has no retained valid temperature message for this hour")
    manifest = {
        "data_kind": "real_prepared_guidance",
        "target_reference_time": _iso(target),
        "target_horizon_hours": list(_HOURS),
        "selected_cycle": _iso(cycle) if cycle is not None else None,
        "created_at": _iso(clock.now()),
        "inputs": rows,
        "prepared_files": {},
        "source_metadata": _SOURCE_METADATA,
        "cycle_selection": selection,
        "supported_hours": supported,
        "missing_hours": missing,
        "downloaded_bytes": downloaded,
        "code_identity": _identity(),
    }
    if from_raw is not None:
        manifest["source_manifest_sha256"] = hashlib.sha256(retained_payload).hexdigest()
    _write_bytes(source / "manifest.json", json.dumps(manifest, indent=2).encode())
    regions = []
    failures = []
    for area in areas:
        key = hashlib.sha256(area.model_dump_json().encode()).hexdigest()[:24]
        directory = output_directory / key
        (directory / "raw").mkdir(parents=True)
        for row in rows:
            for prefix in ("raw", "index"):
                shutil.copyfile(source / row[f"{prefix}_file"], directory / row[f"{prefix}_file"])
        prepared_files = {}
        if decoded:
            assert cycle is not None
            try:
                dataset = normalize_shadow_temperature(
                    decoded, model="RAP", cycle=cycle, target=target, area=area
                )
            except UnsupportedCoordinateError as exc:
                failures.append({"area": area.model_dump(), "reason": str(exc)})
            else:
                prepared_files["RAP"] = _write_prepared_file(directory, "RAP", dataset)
        view = {**manifest, "prepared_area": area.model_dump(), "prepared_files": prepared_files}
        _write_bytes(directory / "manifest.json", json.dumps(view, indent=2).encode())
        regions.append(
            {
                "area": area.model_dump(),
                "directory": str(directory),
                "prepared_bytes": (directory / "RAP.nc").stat().st_size if prepared_files else 0,
            }
        )
    _write_bytes(
        output_directory / "contributors.json", RAP_CONFIGURATION.model_dump_json(indent=2).encode()
    )
    report = {
        "source_directory": str(source),
        "target_reference_time": _iso(target),
        "selected_cycle": manifest["selected_cycle"],
        "supported_hours": supported,
        "missing_hours": missing,
        "cycle_selection": selection,
        "requested_areas": [area.model_dump() for area in areas],
        "regions": regions,
        "failures": failures,
        "downloaded_bytes": downloaded,
        "retained_raw_bytes": sum(row["raw_bytes"] for row in rows),
        "retained_index_bytes": sum(row["index_bytes"] for row in rows),
        "prepared_bytes": sum(row["prepared_bytes"] for row in regions),
        "mode": "rebuilt_offline" if from_raw is not None else "acquired_shadow",
    }
    _write_bytes(report_path, json.dumps(report, indent=2).encode())
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Existing locations JSON")
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Existing real control snapshot"
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="Shadow data outside Git")
    parser.add_argument("--from-raw", type=Path, help="Rebuild offline from retained RAP evidence")
    parser.add_argument("--rap-cycle", type=datetime.fromisoformat, help="Optional fixed UTC cycle")
    args = parser.parse_args(argv)
    report = prepare_rap(
        load_locations(args.config),
        args.data_dir,
        args.output_dir,
        from_raw=args.from_raw,
        cycle_override=args.rap_cycle,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
