"""Prepare native NBM precipitation probability as attached shared guidance.

Only the retained PoP01 event is acquired. The control/shadow surface snapshots
are referenced unchanged; this module uses the existing GRIB, NetCDF, manifest,
and local-grid paths and never turns deterministic precipitation into probability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.application.prepared_shadow import _axis_slice
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _code_identity,
    _hour,
    _iso,
    _write_bytes,
)
from mesoforge.application.spatial_coverage import (
    bbox_in_grid,
    native_bbox_bounds,
    plan_regions,
    point_in_grid,
)
from mesoforge.catalog.configuration import _lists_to_tuples
from mesoforge.catalog.domains import BoundingBox
from mesoforge.catalog.sources import NbmSourceSettings
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition
from mesoforge.guidance.coverage import is_prepared_window
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.nbm_geometry import compute_nbm_grid
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.nbm import convert_pop_percent_to_fraction
from mesoforge.guidance.sources.nbm_decoding import NbmDecodeError, decode_selected_message

POP_VARIABLE = "probability_of_precipitation_1h"
NATIVE_PERCENT = "native_probability_percent"
THRESHOLD = {"comparison": "gt", "value": 0.254, "unit": "kg/m^2"}
_HOURS = tuple(range(1, 37))


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_pop.py",
        "application/pop_selection.py",
        "guidance/sources/nbm.py",
        "guidance/sources/nbm_decoding.py",
        "guidance/nbm_geometry.py",
    ):
        result["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return result


def _retain(directory: Path, acquired: Phase2LeadAcquisition) -> dict[str, Any]:
    if (
        acquired.model != "nbm"
        or len(acquired.selected_messages) != 1
        or acquired.selected_messages[0].canonical_variable_id != POP_VARIABLE
    ):
        raise ValueError("PoP preparation requires one NBM PoP-only message per acquired lead")
    message = acquired.selected_messages[0]
    lead = acquired.forecast_hour
    cycle = datetime.combine(acquired.cycle_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=acquired.cycle_hour
    )
    raw_file, index_file = f"raw/NBM-f{lead:03d}.grib2", f"raw/NBM-f{lead:03d}.idx"
    return {
        "model": "NBM",
        "canonical_variable_id": POP_VARIABLE,
        "cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(cycle + timedelta(hours=lead)),
        "raw_file": raw_file,
        "raw_sha256": _write_bytes(directory / raw_file, message.payload),
        "raw_bytes": len(message.payload),
        "index_file": index_file,
        "index_sha256": _write_bytes(directory / index_file, acquired.index_payload),
        "index_bytes": len(acquired.index_payload),
        "source_grib_url": acquired.resolved_grib_url,
        "source_index_url": acquired.resolved_index_url,
        "byte_start": message.byte_start,
        "byte_end": message.byte_end,
        "endpoint": acquired.endpoint,
        "etag": acquired.full_object_etag,
        "full_object_content_length": acquired.full_object_content_length,
        "grib_last_modified": acquired.full_object_last_modified,
        "index_last_modified": acquired.index_last_modified,
        "grib_retrieved_at": _iso(acquired.grib_completed_at),
        "index_retrieved_at": _iso(acquired.index_completed_at),
        "grib_available_at": _iso(acquired.grib_available_at),
        "index_available_at": _iso(acquired.index_available_at),
    }


def _checked_file(directory: Path, filename: str, digest: str, size: int | None = None) -> bytes:
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Retained PoP path leaves its source directory")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest or (size is not None and len(payload) != size):
        raise ValueError(f"Retained PoP checksum or byte count disagrees: {filename}")
    return payload


def _raw_inputs(directory: Path, manifest: dict[str, Any]) -> dict[int, bytes]:
    cycle = _hour(datetime.fromisoformat(manifest["selected_cycle"]))
    target = _hour(datetime.fromisoformat(manifest["target_reference_time"]))
    valid_times = {target + timedelta(hours=hour) for hour in manifest["target_horizon_hours"]}
    result = {}
    for row in manifest["inputs"]:
        lead = row["source_lead_hours"]
        if (
            type(lead) is not int
            or lead in result
            or row["model"] != "NBM"
            or row["canonical_variable_id"] != POP_VARIABLE
            or row["cycle"] != _iso(cycle)
            or cycle + timedelta(hours=lead) not in valid_times
            or row["valid_time"] != _iso(cycle + timedelta(hours=lead))
        ):
            raise ValueError("Retained NBM PoP cycle/lead/valid time disagrees")
        if (
            row["raw_file"] != f"raw/NBM-f{lead:03d}.grib2"
            or row["index_file"] != f"raw/NBM-f{lead:03d}.idx"
            or row["byte_end"] - row["byte_start"] != row["raw_bytes"]
        ):
            raise ValueError("Retained NBM PoP path or byte range disagrees")
        _checked_file(directory, row["index_file"], row["index_sha256"], row["index_bytes"])
        result[lead] = _checked_file(
            directory, row["raw_file"], row["raw_sha256"], row["raw_bytes"]
        )
    return result


def normalize_pop_messages(
    *,
    settings: NbmSourceSettings,
    target_reference_time: datetime,
    source_cycle: datetime,
    payloads_by_lead: dict[int, bytes],
    areas: tuple[BoundingBox | None, ...],
    target_horizon_hours: tuple[int, ...] = _HOURS,
) -> list[xr.Dataset]:
    """Decode each native probability once, then crop shared regional views."""
    target, cycle = _hour(target_reference_time), _hour(source_cycle)
    if cycle > target or (
        target_horizon_hours != (1, 2, 3) and not is_prepared_window(target_horizon_hours)
    ):
        raise ValueError("PoP requires an existing hourly target window and a cycle at/before it")
    age = int((target - cycle).total_seconds() / 3600)
    leads = tuple(age + hour for hour in target_horizon_hours)
    if set(payloads_by_lead) - set(leads):
        raise ValueError("PoP payloads include a source lead outside the requested window")
    contract = next(
        row for row in settings.field_contracts if row.canonical_variable_id == POP_VARIABLE
    )
    crs, x, y, lat, lon = compute_nbm_grid(settings.grid_profile)
    del lat, lon
    slices = []
    for area in areas:
        if area is None:
            slices.append((slice(None), slice(None)))
        else:
            xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
            slices.append((_axis_slice(y, ymin, ymax), _axis_slice(x, xmin, xmax)))
    native = [np.full((len(leads), len(y[ys]), len(x[xs])), np.nan) for ys, xs in slices]
    metadata: dict[str, Any] = {}
    missing: dict[str, list[str]] = {}
    for index, lead in enumerate(leads):
        start, end = cycle + timedelta(hours=lead - 1), cycle + timedelta(hours=lead)
        row = metadata[str(lead)] = {
            "status": "unavailable",
            "source_cycle": _iso(cycle),
            "source_lead_hours": lead,
            "interval_start": _iso(start),
            "interval_end": _iso(end),
            "interval_closure": "left_open_right_closed",
            "threshold": THRESHOLD,
            "native_unit": "percent",
            "normalization": "percent_divided_by_100_once",
        }
        payload = payloads_by_lead.get(lead)
        if payload is None:
            missing[str(lead)] = [
                f"NBM has no retained native one-hour PoP message at source lead {lead}"
            ]
            row["missing_reasons"] = missing[str(lead)]
            continue
        try:
            field = decode_selected_message(
                payload,
                contract=contract,
                settings=settings,
                forecast_hour=lead,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
            )
            expected = {
                "time": np.datetime64(cycle.replace(tzinfo=None), "ns"),
                "step": np.timedelta64(lead, "h"),
                "valid_time": np.datetime64(end.replace(tzinfo=None), "ns"),
            }
            if any(
                name not in field.coords or field[name].ndim != 0 or field[name].values[()] != value
                for name, value in expected.items()
            ):
                raise ValueError("NBM decoded cycle/lead/valid time disagrees")
            for array, (ys, xs) in zip(native, slices, strict=True):
                array[index] = np.asarray(field.values[ys, xs], dtype=np.float64)
            row.update(
                status="available",
                grib_reported_units=str(field.attrs["GRIB_units"]),
                grib_event_keys={
                    name: int(field.attrs[f"GRIB_{name}"])
                    for name in (
                        "probabilityType",
                        "scaleFactorOfUpperLimit",
                        "scaledValueOfUpperLimit",
                        "startStep",
                        "endStep",
                    )
                },
            )
        except (NbmDecodeError, ValueError) as exc:
            missing[str(lead)] = [str(exc)]
            row["missing_reasons"] = missing[str(lead)]
    source_time = np.datetime64(cycle.replace(tzinfo=None), "ns")
    durations = np.array(leads, dtype="timedelta64[h]").astype("timedelta64[ns]")
    ends = source_time + durations
    results = []
    for area, percentages, (ys, xs) in zip(areas, native, slices, strict=True):
        valid = np.isfinite(percentages) & (percentages >= 0) & (percentages <= 100)
        fractions = np.full(percentages.shape, np.nan)
        fractions[valid] = [
            convert_pop_percent_to_fraction(float(value)) for value in percentages[valid]
        ]
        regional_metadata = json.loads(json.dumps(metadata))
        for index, lead in enumerate(leads):
            regional_metadata[str(lead)]["missing_cell_count"] = int(
                np.count_nonzero(~np.isfinite(fractions[index]))
            )
            regional_metadata[str(lead)]["invalid_percent_cell_count"] = int(
                np.count_nonzero(np.isfinite(percentages[index]) & ~valid[index])
            )
        bounds = f"{POP_VARIABLE}_interval_bounds"
        results.append(
            xr.Dataset(
                data_vars={
                    POP_VARIABLE: (
                        ("source_lead_time", "y", "x"),
                        fractions,
                        {
                            "unit_id": "1",
                            "units": "1",
                            "temporal_semantics": "probability",
                            "interval_bounds": bounds,
                            "interval_closure": "left_open_right_closed",
                            "probability_threshold_kg_m2": 0.254,
                            "probability_comparison": "gt",
                            "probability_type": 1,
                        },
                    ),
                    NATIVE_PERCENT: (
                        ("source_lead_time", "y", "x"),
                        percentages,
                        {
                            "unit_id": "percent",
                            "units": "%",
                            "temporal_semantics": "probability",
                            "interval_bounds": bounds,
                        },
                    ),
                    bounds: (
                        ("source_lead_time", "bounds"),
                        np.stack((ends - np.timedelta64(1, "h"), ends), axis=1),
                    ),
                },
                coords={
                    "x": x[xs],
                    "y": y[ys],
                    "forecast_reference_time": source_time,
                    "source_lead_time": durations,
                    "source_valid_time": ("source_lead_time", ends),
                },
                attrs={
                    "model": "NBM",
                    "data_kind": "real_prepared_guidance",
                    "target_reference_time": _iso(target),
                    "crs_wkt2": crs.to_wkt(),
                    "grid_profile_id": settings.grid_profile.profile_id,
                    "source_x_min": float(x.min()),
                    "source_x_max": float(x.max()),
                    "source_y_min": float(y.min()),
                    "source_y_max": float(y.max()),
                    "grid_description": (
                        "Native NBM grid, coordinate-derived footprint plus clipped one-cell halo"
                    ),
                    "field_missing_reasons_json": json.dumps(
                        {POP_VARIABLE: missing}, sort_keys=True
                    ),
                    "pop_metadata_json": json.dumps(regional_metadata, sort_keys=True),
                    **({"prepared_area_json": area.model_dump_json()} if area is not None else {}),
                },
            )
        )
    return results


def _save_views(
    directory: Path,
    manifest: dict[str, Any],
    settings: NbmSourceSettings,
    payloads: dict[int, bytes],
    areas: tuple[BoundingBox | None, ...],
) -> None:
    views = normalize_pop_messages(
        settings=settings,
        target_reference_time=datetime.fromisoformat(manifest["target_reference_time"]),
        source_cycle=datetime.fromisoformat(manifest["selected_cycle"]),
        payloads_by_lead=payloads,
        areas=areas,
        target_horizon_hours=tuple(manifest["target_horizon_hours"]),
    )
    regions = []
    for index, (area, dataset) in enumerate(zip(areas, views, strict=True)):
        filename = "NBM.nc" if len(views) == 1 else f"regions/{index}/NBM.nc"
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            dataset.to_netcdf(output, engine="h5netcdf", format="NETCDF4")
        file = {
            "file": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
        regions.append(
            {
                "area": area.model_dump() if area is not None else None,
                "prepared_files": {"NBM": file},
            }
        )
    manifest["regions"] = regions
    manifest.pop("prepared_files", None)
    if len(regions) == 1:
        manifest["prepared_files"] = regions[0]["prepared_files"]


def prepare_pop_guidance(
    directory: Path,
    *,
    settings: NbmSourceSettings,
    target_reference_time: datetime,
    source_cycle: datetime,
    acquired_inputs: list[Phase2LeadAcquisition],
    area: BoundingBox | None,
    areas: tuple[BoundingBox, ...] = (),
    clock: Clock | None = None,
    selection_evidence: dict[str, Any] | None = None,
    target_horizon_hours: tuple[int, ...] = _HOURS,
) -> dict[str, Any]:
    """Retain PoP-only inputs once and prepare one or several shared native views."""
    directory = directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("PoP preparation requires an empty output directory")
    if len({row.forecast_hour for row in acquired_inputs}) != len(acquired_inputs):
        raise ValueError("Duplicate acquired NBM PoP leads")
    if any(
        row.cycle_date != source_cycle.date() or row.cycle_hour != source_cycle.hour
        for row in acquired_inputs
    ):
        raise ValueError("Acquired NBM cycle differs from selection")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw").mkdir()
    inputs = [_retain(directory, row) for row in acquired_inputs]
    manifest = {
        "data_kind": "real_prepared_guidance",
        "model": "NBM",
        "field": POP_VARIABLE,
        "target_reference_time": _iso(_hour(target_reference_time)),
        "target_horizon_hours": list(target_horizon_hours),
        "selected_cycle": _iso(_hour(source_cycle)),
        "created_at": _iso((clock or SystemClock()).now()),
        "threshold": THRESHOLD,
        "inputs": inputs,
        "source_settings": settings.model_dump(mode="json"),
        "code_identity": _identity(),
        "selection_evidence": selection_evidence,
        "source_metadata": {
            "model": "NBM",
            "provider": "NOAA",
            "model_family": "National Blend of Models",
            "role": "precipitation_probability_source",
            "contract_profile": settings.contract_profile,
            "grid_profile": settings.grid_profile.model_dump(mode="json"),
            "field": POP_VARIABLE,
            "native_interval_hours": 1,
            "threshold": THRESHOLD,
        },
        "downloaded_bytes": sum(row["raw_bytes"] + row["index_bytes"] for row in inputs),
        "retained_raw_bytes": sum(row["raw_bytes"] for row in inputs),
    }
    payloads = _raw_inputs(directory, manifest)
    _save_views(directory, manifest, settings, payloads, (area, *areas))
    digest = _write_bytes(
        directory / "manifest.json", json.dumps(manifest, indent=2, allow_nan=False).encode()
    )
    return _descriptor(directory, manifest, digest)


def _descriptor(directory: Path, manifest: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "status": "prepared",
        "directory": str(directory.resolve()),
        "manifest_sha256": digest,
        "selected_cycle": manifest["selected_cycle"],
        "threshold": THRESHOLD,
        "native_interval_hours": 1,
        "downloaded_bytes": manifest["downloaded_bytes"],
        "retained_raw_bytes": manifest["retained_raw_bytes"],
        "prepared_bytes": sum(row["prepared_files"]["NBM"]["bytes"] for row in manifest["regions"]),
    }


def rebuild_pop_guidance(
    source: Path,
    directory: Path,
    *,
    area: BoundingBox | None = None,
    areas: tuple[BoundingBox, ...] = (),
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Re-decode retained probability messages without acquiring model or observation data."""
    payload = (source / "manifest.json").read_bytes()
    manifest = json.loads(payload)
    raw = _raw_inputs(source, manifest)
    settings = NbmSourceSettings.model_validate(_lists_to_tuples(manifest["source_settings"]))
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Offline PoP rebuilding requires an empty output directory")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw").mkdir()
    for row in manifest["inputs"]:
        for prefix in ("raw", "index"):
            _write_bytes(
                directory / row[f"{prefix}_file"],
                _checked_file(
                    source, row[f"{prefix}_file"], row[f"{prefix}_sha256"], row[f"{prefix}_bytes"]
                ),
            )
    footprints = (
        (area, *areas)
        if area is not None or areas
        else tuple(
            BoundingBox.model_validate(row["area"]) if row["area"] else None
            for row in manifest["regions"]
        )
    )
    manifest.update(
        created_at=_iso((clock or SystemClock()).now()),
        downloaded_bytes=0,
        code_identity=_identity(),
        source_manifest_sha256=_write_bytes(directory / "source-manifest.json", payload),
    )
    _save_views(directory, manifest, settings, raw, footprints)
    digest = _write_bytes(
        directory / "manifest.json", json.dumps(manifest, indent=2, allow_nan=False).encode()
    )
    return _descriptor(directory, manifest, digest)


def load_pop_guidance(
    descriptor: dict[str, Any], target_reference_time: datetime | np.datetime64
) -> list[tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]]:
    """Load and validate the attached shared probability views once, before calculation."""
    if descriptor.get("status") == "unavailable":
        return []
    directory = Path(descriptor["directory"])
    payload = _checked_file(directory, "manifest.json", descriptor["manifest_sha256"])
    manifest = json.loads(payload)
    target = (
        np.datetime64(_hour(target_reference_time).replace(tzinfo=None), "ns")
        if isinstance(target_reference_time, datetime)
        else target_reference_time.astype("datetime64[ns]")
    )
    if (
        manifest["data_kind"] != "real_prepared_guidance"
        or manifest["model"] != "NBM"
        or manifest["field"] != POP_VARIABLE
        or manifest["threshold"] != THRESHOLD
        or np.datetime64(
            datetime.fromisoformat(manifest["target_reference_time"]).replace(tzinfo=None), "ns"
        )
        != target
    ):
        raise ValueError("Attached NBM probability source identity/target/event disagrees")
    _raw_inputs(directory, manifest)
    views = []
    for region in manifest["regions"]:
        file = region["prepared_files"]["NBM"]
        _checked_file(directory, file["file"], file["sha256"], file["bytes"])
        with xr.open_dataset(directory / file["file"], engine="h5netcdf") as opened:
            dataset = opened.load()
        variable = dataset[POP_VARIABLE]
        attrs = variable.attrs
        if (
            variable.dims != ("source_lead_time", "y", "x")
            or attrs.get("unit_id") != "1"
            or attrs.get("units") != "1"
            or attrs.get("temporal_semantics") != "probability"
            or attrs.get("probability_threshold_kg_m2") != 0.254
            or attrs.get("probability_comparison") != "gt"
            or attrs.get("probability_type") != 1
            or attrs.get("interval_closure") != "left_open_right_closed"
        ):
            raise ValueError("Attached NBM probability has invalid units or event semantics")
        source_cycle = np.datetime64(
            datetime.fromisoformat(manifest["selected_cycle"]).replace(tzinfo=None), "ns"
        )
        ends = target + np.array(manifest["target_horizon_hours"], dtype="timedelta64[h]")
        expected_bounds = np.stack((ends - np.timedelta64(1, "h"), ends), axis=1)
        if (
            dataset.forecast_reference_time.values != source_cycle
            or not np.array_equal(dataset.source_valid_time.values, ends)
            or not np.array_equal(source_cycle + dataset.source_lead_time.values, ends)
            or not np.array_equal(
                dataset[f"{POP_VARIABLE}_interval_bounds"].values, expected_bounds
            )
        ):
            raise ValueError("Attached NBM probability source cycle/lead/intervals disagree")
        crs = pyproj.CRS.from_wkt(dataset.attrs["crs_wkt2"])
        if not crs.is_projected:
            raise ValueError("NBM probability must retain its projected native grid")
        for axis in (dataset.x.values, dataset.y.values):
            if len(axis) < 2 or not np.all(np.isfinite(axis)) or not np.all(np.diff(axis) > 0):
                raise ValueError("NBM probability grid axes must be finite increasing coordinates")
        native = dataset[NATIVE_PERCENT].values
        valid = np.isfinite(native) & (native >= 0) & (native <= 100)
        expected = np.full(native.shape, np.nan)
        expected[valid] = native[valid] / 100.0
        if not np.array_equal(variable.values, expected, equal_nan=True):
            raise ValueError("Prepared NBM probability differs from retained native percentages")
        views.append(
            (
                dataset,
                crs,
                {
                    **manifest,
                    "prepared_files": region["prepared_files"],
                    "manifest_sha256": descriptor["manifest_sha256"],
                },
            )
        )
    return views


def _requested_areas(
    preparation: dict[str, Any], locations: list[Any] | None, settings: NbmSourceSettings
) -> tuple[BoundingBox, ...]:
    if locations is not None:
        from mesoforge.application.batch_forecast import _coordinates

        crs, x, y, _, _ = compute_nbm_grid(settings.grid_profile)
        supported = []
        for location in locations:
            try:
                latitude, longitude = _coordinates(location)
                if point_in_grid(latitude, longitude, crs, x, y):
                    supported.append((latitude, longitude))
            except ValueError:
                continue
        return tuple(plan_regions(supported))
    source = Path(preparation["directory"])
    coverage_path = source / "coverage.json"
    if coverage_path.exists():
        coverage = json.loads(coverage_path.read_bytes())
        return tuple(BoundingBox.model_validate(row["area"]) for row in coverage["regions"])
    manifest = json.loads((source / "manifest.json").read_bytes())
    if manifest.get("prepared_area") is None:
        raise ValueError(
            "PoP preparation needs configured coordinates or a retained regional footprint"
        )
    return (BoundingBox.model_validate(manifest["prepared_area"]),)


def prepare_pop_attachment(
    preparation: dict[str, Any],
    output_directory: Path,
    *,
    locations: list[Any] | None = None,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    source_cycle: datetime | None = None,
) -> dict[str, Any]:
    """Discover/prepare one shared PoP source and attach it to a selected surface run.

    The four-model decision evidence and all surface/QPF datasets remain unchanged.
    PoP has its own actual discovery/acquisition times and native probability event.
    """
    from mesoforge.application.pop_selection import acquire_selected_pop, select_pop_guidance

    selection = preparation["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("PoP attachment requires an existing selected surface forecast")
    settings = NbmSourceSettings.model_validate(
        _lists_to_tuples(selection["source_configuration"]["nbm"])
    )
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    areas = _requested_areas(preparation, locations, settings)
    if not areas:
        return {
            "status": "unavailable",
            "reason": "No configured coordinate is supported by the native NBM domain",
            "downloaded_bytes": 0,
        }
    previous = preparation.get("pop_guidance")
    if previous and previous.get("status") == "prepared":
        views = load_pop_guidance(previous, target)
        cycle_matches = source_cycle is None or all(
            _hour(datetime.fromisoformat(manifest["selected_cycle"])) == _hour(source_cycle)
            for _, _, manifest in views
        )
        if cycle_matches and all(
            any(bbox_in_grid(area, crs, ds.x.values, ds.y.values) for ds, crs, _ in views)
            for area in areas
        ):
            return {**previous, "reused": True, "downloaded_bytes": 0}
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    owned_transport = BoundedHttpTransport() if transport is None else None
    active_transport = transport if transport is not None else owned_transport
    assert active_transport is not None
    before = int(getattr(active_transport, "downloaded_bytes", 0))
    try:
        selected = select_pop_guidance(
            target_reference_time=target,
            horizons=tuple(selection["horizon_hours"]),
            settings=settings,
            transport=active_transport,
            clock=clock,
            sleeper=sleeper,
            explicit_cycle=source_cycle,
        )
        if selected["status"] == "unavailable":
            output_directory.mkdir(parents=True, exist_ok=True)
            _write_bytes(
                output_directory / "selection.json",
                json.dumps(selected, indent=2, allow_nan=False).encode(),
            )
            return {
                "status": "unavailable",
                "reason": selected["reason"],
                "selection": selected,
                "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            }
        acquired, evidence = acquire_selected_pop(
            selected, settings=settings, transport=active_transport, clock=clock, sleeper=sleeper
        )
        descriptor = prepare_pop_guidance(
            output_directory,
            settings=settings,
            target_reference_time=target,
            source_cycle=datetime.fromisoformat(selected["selected_cycle"]),
            acquired_inputs=acquired,
            area=areas[0],
            areas=areas[1:],
            clock=clock,
            selection_evidence=evidence,
            target_horizon_hours=tuple(selection["horizon_hours"]),
        )
        manifest = json.loads((Path(descriptor["directory"]) / "manifest.json").read_bytes())
        return {
            **descriptor,
            "selected_cycle": selected["selected_cycle"],
            "threshold": THRESHOLD,
            "native_interval_hours": 1,
            "missing_hours": selected.get("missing_hours", []),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "retained_raw_bytes": manifest["retained_raw_bytes"],
            "prepared_bytes": sum(
                row["prepared_files"]["NBM"]["bytes"] for row in manifest["regions"]
            ),
        }
    finally:
        if owned_transport is not None:
            owned_transport.close()


def prepare_pop_run(
    prepared_run: Path,
    output_directory: Path,
    *,
    config_path: Path | None = None,
    source_cycle: datetime | None = None,
    from_raw: bool = False,
) -> dict[str, Any]:
    """Write a new prepared-run wrapper; retain the original numerical guidance by reference."""
    from mesoforge.application.batch_forecast import load_locations

    output_directory = output_directory.resolve()
    repository = Path(__file__).resolve().parents[3]
    if output_directory.is_relative_to(repository):
        raise ValueError("Retained probability data must be outside the repository")
    if output_directory.exists():
        raise ValueError("PoP wrapper output directory must not already exist")
    if from_raw and (source_cycle is not None or config_path is not None):
        raise ValueError("Offline replay uses its retained cycle and footprints")
    source_path = prepared_run / "preparation.json"
    source_bytes = source_path.read_bytes()
    original = json.loads(source_bytes)
    locations = load_locations(config_path) if config_path is not None else None
    if from_raw:
        descriptor = original.get("pop_guidance", {})
        if descriptor.get("status") != "prepared":
            raise ValueError("Offline replay requires an already prepared PoP attachment")
        manifest = json.loads(
            _checked_file(
                Path(descriptor["directory"]), "manifest.json", descriptor["manifest_sha256"]
            )
        )
        if (
            manifest["target_reference_time"]
            != original["current_model_set"]["selection"]["target_reference_time"]
        ):
            raise ValueError("Retained PoP target differs from the selected surface forecast")
        attached = rebuild_pop_guidance(Path(descriptor["directory"]), output_directory / "NBM")
        attached["downloaded_bytes"] = 0
    else:
        attached = prepare_pop_attachment(
            original, output_directory / "NBM", locations=locations, source_cycle=source_cycle
        )
    result = {
        **original,
        "pop_guidance": attached,
        "source_preparation": {
            "file": str(source_path.resolve()),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "downloaded_bytes": attached.get("downloaded_bytes", 0),
    }
    included = original.get("pop_bytes_in_totals", {})
    result["pop_bytes_in_totals"] = {}
    for key in ("retained_raw_bytes", "prepared_bytes"):
        if key in original:
            result[key] = original[key] - included.get(key, 0) + attached.get(key, 0)
            result["pop_bytes_in_totals"][key] = attached.get(key, 0)
    output_directory.mkdir(parents=True, exist_ok=True)
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--nbm-cycle", type=datetime.fromisoformat)
    parser.add_argument("--from-raw", action="store_true")
    args = parser.parse_args()
    result = prepare_pop_run(
        args.prepared_run,
        args.output_dir,
        config_path=args.config,
        source_cycle=args.nbm_cycle,
        from_raw=args.from_raw,
    )
    print(
        json.dumps(
            {
                "preparation_file": str((args.output_dir / "preparation.json").resolve()),
                "control_directory": result["directory"],
                "pop_guidance": result["pop_guidance"],
            },
            indent=2,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
