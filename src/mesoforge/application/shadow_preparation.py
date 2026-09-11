"""Shared acquisition, retention and regional preparation for temperature shadow adapters.

Model adapters own discovery, decoding and capability policy. This is the existing
RAP preparation flow, shared without changing active model preparation or HTTP.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import xarray as xr

from mesoforge.application.batch_forecast import _coordinates
from mesoforge.application.point_forecast import _verify_file
from mesoforge.application.prepared_shadow import SURFACE_UNITS
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _hour,
    _iso,
    _raw_byte_count,
    _retain_input,
    _retained_extra_payloads,
    _write_bytes,
    _write_prepared_file,
)
from mesoforge.application.spatial_coverage import UnsupportedCoordinateError, plan_regions
from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.recipes import ContributorConfiguration
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper

_HOURS = tuple(range(1, 37))


@dataclass(frozen=True)
class ShadowAdapter:
    model: str
    source_metadata: dict[str, Any]
    configuration: ContributorConfiguration
    discover_cycle: Callable[..., Any]
    acquire_lead: Callable[..., Phase2LeadAcquisition]
    decode_message: Callable[..., xr.DataArray]
    normalize: Callable[..., xr.Dataset]
    code_identity: Callable[[], dict[str, Any]]
    transport_factory: Callable[[], BoundedHttpTransport] = BoundedHttpTransport
    decode_surface_message: Callable[..., xr.DataArray] | None = None
    surface_variables: tuple[str, ...] = ()


def _source(directory: Path) -> Path:
    coverage = directory / "coverage.json"
    return (
        Path(json.loads(coverage.read_text())["source_directory"])
        if coverage.is_file()
        else directory
    )


def retained_surface_mode(directory: Path) -> bool:
    """Use a saved snapshot's declared scope when selecting its adapter metadata."""
    manifest = _source(directory) / "manifest.json"
    return (
        bool(json.loads(manifest.read_text()).get("surface_fields"))
        if manifest.is_file()
        else False
    )


def _target(control_directory: Path, *, model: str) -> datetime:
    manifest = json.loads((_source(control_directory) / "manifest.json").read_text())
    if manifest.get("data_kind") != "real_prepared_guidance" or manifest.get(
        "target_horizon_hours"
    ) != list(_HOURS):
        raise ValueError(
            f"{model} shadow preparation requires an existing real 1..36 control snapshot"
        )
    return _hour(datetime.fromisoformat(manifest["target_reference_time"]))


def _raw_inputs(source: Path, manifest: dict[str, Any], *, model: str) -> dict[int, bytes]:
    """Check every retained byte before rebuilding or reporting offline reuse."""
    inputs: dict[int, bytes] = {}
    target = _hour(datetime.fromisoformat(manifest["target_reference_time"]))
    requested = manifest.get("requested_target_horizons", list(_HOURS))
    for row in manifest["inputs"]:
        lead = row["source_lead_hours"]
        cycle = _hour(datetime.fromisoformat(row["cycle"]))
        valid = cycle + timedelta(hours=lead)
        if (
            row["model"] != model
            or type(lead) is not int
            or lead in inputs
            or row["cycle"] != manifest["selected_cycle"]
            or row["valid_time"] != _iso(valid)
            or valid not in [target + timedelta(hours=hour) for hour in requested]
        ):
            raise ValueError(f"{model} retained cycle, lead or valid time disagrees")
        for prefix, suffix in (("raw", "grib2"), ("index", "idx")):
            filename = row[f"{prefix}_file"]
            if filename != f"raw/{model}-f{lead:03d}.{suffix}":
                raise ValueError(f"Invalid retained {model} input path")
            _verify_file(source, filename, row[f"{prefix}_sha256"])
            if (source / filename).stat().st_size != row[f"{prefix}_bytes"]:
                raise ValueError(f"Retained {model} input byte count disagrees")
        inputs[lead] = (source / row["raw_file"]).read_bytes()
        _retained_extra_payloads(source, row)
    return inputs


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def prepare_shadow(
    locations: list[Any],
    control_directory: Path,
    output_directory: Path,
    *,
    adapter: ShadowAdapter,
    target_horizons: tuple[int, ...] = _HOURS,
    from_raw: Path | None = None,
    cycle_override: datetime | None = None,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    surface_fields: bool = False,
    canonical_variables_by_lead: dict[int, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Acquire, rebuild or reuse shadow guidance independently of the control files."""
    model = adapter.model
    if (
        not target_horizons
        or any(type(hour) is not int or hour not in _HOURS for hour in target_horizons)
        or tuple(sorted(set(target_horizons))) != target_horizons
    ):
        raise ValueError("target_horizons must be a sorted unique nonempty subset of 1..36")
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    target = _target(control_directory, model=model)
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
            manifest.get("source_metadata") != adapter.source_metadata
            or manifest["target_reference_time"] != _iso(target)
            or manifest.get("requested_target_horizons", list(_HOURS)) != list(target_horizons)
            or report["requested_areas"] != [area.model_dump() for area in areas]
            or (cycle_override is not None and manifest["selected_cycle"] != _iso(cycle_override))
            or bool(manifest.get("surface_fields")) != surface_fields
        ):
            raise ValueError(f"Existing {model} snapshot differs; use a new output directory")
        _raw_inputs(source, manifest, model=model)
        for region in report["regions"]:
            directory = Path(region["directory"])
            view = json.loads((directory / "manifest.json").read_text())
            _raw_inputs(directory, view, model=model)
            for prepared in view["prepared_files"].values():
                _verify_file(directory, prepared["file"], prepared["sha256"])
        return {**report, "downloaded_bytes": 0, "mode": "reused_retained_shadow"}
    output_directory.mkdir(parents=True, exist_ok=True)
    if any(output_directory.iterdir()):
        raise ValueError(
            f"{model} preparation requires an empty directory; existing files are retained"
        )
    source = output_directory / "source"
    (source / "raw").mkdir(parents=True)
    owned_transport = None
    missing: dict[int, str] = {}
    rows = []
    payloads: dict[int, bytes] = {}
    surface_payloads: dict[str, dict[int, bytes]] = {name: {} for name in SURFACE_UNITS}
    if from_raw is not None:
        retained = _source(from_raw)
        retained_payload = (retained / "manifest.json").read_bytes()
        manifest = json.loads(retained_payload)
        if (
            manifest.get("source_metadata") != adapter.source_metadata
            or manifest.get("target_reference_time") != _iso(target)
            or manifest.get("target_horizon_hours") != list(_HOURS)
            or manifest.get("requested_target_horizons", list(_HOURS)) != list(target_horizons)
        ):
            raise ValueError(f"Retained {model} capabilities or control window differ")
        payloads = _raw_inputs(retained, manifest, model=model)
        surface_fields = bool(manifest.get("surface_fields"))
        rows = manifest["inputs"]
        for row in rows:
            for prefix in ("raw", "index"):
                shutil.copyfile(retained / row[f"{prefix}_file"], source / row[f"{prefix}_file"])
            for variable, payload in _retained_extra_payloads(retained, row).items():
                surface_payloads[variable][row["source_lead_hours"]] = payload
            for filename in {message["raw_file"] for message in row.get("extra_messages", [])}:
                shutil.copyfile(retained / filename, source / filename)
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
            owned_transport = adapter.transport_factory()
            transport = owned_transport
        try:
            selection_result = adapter.discover_cycle(
                target_reference_time=target,
                transport=transport,
                clock=clock,
                sleeper=sleeper,
                cycle_override=cycle_override,
                target_horizons=target_horizons,
            )
            selection = json.loads(json.dumps(asdict(selection_result), default=_json_default))
            cycle = selection_result.cycle
            missing.update(selection_result.missing_hours)
            if cycle is not None:
                for lead in selection_result.available_leads:
                    hour = int((cycle + timedelta(hours=lead) - target).total_seconds() / 3600)
                    if hour not in target_horizons:
                        raise ValueError(f"{model} discovery returned an unrequested target hour")
                    try:
                        acquired = adapter.acquire_lead(
                            cycle=cycle,
                            forecast_hour=lead,
                            transport=transport,
                            clock=clock,
                            sleeper=sleeper,
                            cycle_deadline=clock.now(),
                            **(
                                {
                                    "canonical_variables": canonical_variables_by_lead[lead]
                                    if canonical_variables_by_lead is not None
                                    else adapter.surface_variables
                                }
                                if surface_fields
                                else {}
                            ),
                        )
                    except (MesoForgeError, ValueError) as exc:
                        missing[hour] = (
                            f"{model} acquisition unavailable: {type(exc).__name__}: {exc}"
                        )
                        continue
                    rows.append(_retain_input(source, acquired))
                    for message in acquired.selected_messages:
                        if message.canonical_variable_id == "air_temperature_2m":
                            payloads[lead] = message.payload
                        else:
                            surface_payloads[message.canonical_variable_id][lead] = message.payload
            downloaded = getattr(
                transport,
                "downloaded_bytes",
                _raw_byte_count(rows) + sum(row["index_bytes"] for row in rows),
            )
        finally:
            if owned_transport is not None:
                owned_transport.close()
    decoded = {}
    decoded_surface: dict[str, dict[int, xr.DataArray]] = {name: {} for name in SURFACE_UNITS}
    field_missing: dict[str, dict[str, list[str]]] = {name: {} for name in SURFACE_UNITS}
    for lead, payload in payloads.items():
        assert cycle is not None
        hour = int((cycle + timedelta(hours=lead) - target).total_seconds() / 3600)
        try:
            decoded[lead] = adapter.decode_message(payload, cycle=cycle, forecast_hour=lead)
        except (MesoForgeError, ValueError) as exc:
            missing[hour] = f"{model} decoding unavailable: {type(exc).__name__}: {exc}"
        if surface_fields:
            if adapter.decode_surface_message is None:
                raise ValueError(f"{model} adapter has no surface decoder")
            for variable, by_lead in surface_payloads.items():
                if lead not in by_lead:
                    continue
                try:
                    decoded_surface[variable][lead] = adapter.decode_surface_message(
                        by_lead[lead],
                        canonical_variable_id=variable,
                        cycle=cycle,
                        forecast_hour=lead,
                    )
                except (MesoForgeError, ValueError) as exc:
                    field_missing[variable][str(lead)] = [
                        f"{model} {variable} decoding unavailable: {type(exc).__name__}: {exc}"
                    ]
    supported = [
        hour
        for hour in _HOURS
        if cycle is not None and int((target - cycle).total_seconds() / 3600) + hour in decoded
    ]
    missing = {hour: reason for hour, reason in missing.items() if hour not in supported}
    for hour in _HOURS:
        if hour not in supported:
            missing.setdefault(
                hour,
                f"{model} target hour was not requested in this bounded preparation"
                if hour not in target_horizons
                else f"{model} has no retained valid temperature message for this hour",
            )
    manifest = {
        "data_kind": "real_prepared_guidance",
        "target_reference_time": _iso(target),
        "target_horizon_hours": list(_HOURS),
        "requested_target_horizons": list(target_horizons),
        "selected_cycle": _iso(cycle) if cycle is not None else None,
        "created_at": _iso(clock.now()),
        "inputs": rows,
        "prepared_files": {},
        "source_metadata": adapter.source_metadata,
        "cycle_selection": selection,
        "supported_hours": supported,
        "missing_hours": missing,
        "downloaded_bytes": downloaded,
        "code_identity": adapter.code_identity(),
        **(
            {"surface_fields": True, "field_missing_reasons": field_missing}
            if surface_fields
            else {}
        ),
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
            for filename in {message["raw_file"] for message in row.get("extra_messages", [])}:
                shutil.copyfile(source / filename, directory / filename)
        prepared_files = {}
        if decoded:
            assert cycle is not None
            try:
                dataset = adapter.normalize(
                    decoded,
                    model=model,
                    cycle=cycle,
                    target=target,
                    area=area,
                    **(
                        {"decoded_surface": decoded_surface, "field_missing_reasons": field_missing}
                        if surface_fields
                        else {}
                    ),
                )
            except UnsupportedCoordinateError as exc:
                failures.append({"area": area.model_dump(), "reason": str(exc)})
            else:
                prepared_files[model] = _write_prepared_file(directory, model, dataset)
        view = {**manifest, "prepared_area": area.model_dump(), "prepared_files": prepared_files}
        _write_bytes(directory / "manifest.json", json.dumps(view, indent=2).encode())
        regions.append(
            {
                "area": area.model_dump(),
                "directory": str(directory),
                "prepared_bytes": (directory / f"{model}.nc").stat().st_size
                if prepared_files
                else 0,
            }
        )
    _write_bytes(
        output_directory / "contributors.json",
        adapter.configuration.model_dump_json(indent=2).encode(),
    )
    report = {
        "source_directory": str(source),
        "target_reference_time": _iso(target),
        "requested_target_horizons": list(target_horizons),
        "selected_cycle": manifest["selected_cycle"],
        "supported_hours": supported,
        "missing_hours": missing,
        "cycle_selection": selection,
        "requested_areas": [area.model_dump() for area in areas],
        "regions": regions,
        "failures": failures,
        "downloaded_bytes": downloaded,
        "retained_raw_bytes": _raw_byte_count(rows),
        "retained_index_bytes": sum(row["index_bytes"] for row in rows),
        "prepared_bytes": sum(row["prepared_bytes"] for row in regions),
        "mode": "rebuilt_offline" if from_raw is not None else "acquired_shadow",
    }
    _write_bytes(report_path, json.dumps(report, indent=2).encode())
    return report
