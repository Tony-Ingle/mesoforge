"""Attach distinct native flat-ice and liquid freezing-rain accumulations."""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.application.ice import IceView
from mesoforge.application.prepared_pop import _checked_file
from mesoforge.application.prepared_precipitation_type import read_type_input, retain_type_input
from mesoforge.application.prepared_probability_sources import _areas
from mesoforge.application.prepared_shadow import _axis_slice
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _code_identity,
    _hour,
    _iso,
    _write_bytes,
)
from mesoforge.application.spatial_coverage import native_bbox_bounds
from mesoforge.forecasting.ice import NATIVE_FACTORS, POLICY
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.ice import (
    SOURCES,
    acquire_ice_lead,
    decode_ice_lead,
    ice_url,
)

RetainedInput = tuple[dict[str, Any], dict[str, bytes], bytes]


def _normalized_interval(
    end: xr.Dataset,
    end_event: dict[str, Any],
    start: xr.Dataset | None,
    start_event: dict[str, Any] | None,
    interval_start: str,
) -> tuple[xr.Dataset, dict[str, Any]]:
    """Normalize native accumulations, without converting liquid precipitation to ice.

    As in the retained SWE/new-snow path, difference compatible cumulative parents
    on the native grid. Their quantity-specific helpers cannot accept ice/FRZR, and
    the GFS APCP bucket reset/clipping policy does not apply to these products.
    """
    event = deepcopy(end_event)
    factor = event["native_factor_to_canonical"]
    event["native_end_full_grid_counts"] = {
        key: event.pop(key) for key in ("invalid_cell_count", "missing_cell_count") if key in event
    }
    if (
        event["quantity_kind"]
        not in ("flat_ice_accretion_mass_equivalent", "freezing_rain_liquid_equivalent")
        or end.native_amount.attrs.get("units") != event["native_unit"]
        or factor != NATIVE_FACTORS.get(event["native_unit"])
    ):
        raise ValueError("Native ice accumulation quantity or units disagree")
    native_end = end.native_amount.values
    values = native_end
    valid = np.isfinite(values) & (values >= 0)
    result = xr.Dataset({"native_end_amount": end.native_amount.copy(deep=True)}, attrs=end.attrs)
    method = "native_interval_amount_times_unit_factor"
    if event["interval_start"] != interval_start:
        event["native_interval_start"] = event["interval_start"]
        if start is None or start_event is None:
            values = np.full(native_end.shape, np.nan)
            event["missing_reasons"] = ["Missing compatible native cumulative freezing-rain parent"]
        else:
            if (
                event["quantity_kind"] != "freezing_rain_liquid_equivalent"
                or any(
                    start_event[key] != event[key]
                    for key in (
                        "source_id",
                        "model",
                        "source_cycle",
                        "quantity_kind",
                        "native_unit",
                        "native_factor_to_canonical",
                        "interval_start",
                        "interval_closure",
                        "version",
                        "spatial_support",
                    )
                )
                or start_event["interval_end"] != interval_start
                or start.native_amount.attrs.get("units") != event["native_unit"]
                or any(not np.array_equal(start[axis], end[axis]) for axis in ("x", "y"))
            ):
                raise ValueError(
                    "Native freezing-rain parents differ in quantity/identity/window/grid"
                )
            native_start = start.native_amount.values
            values = native_end - native_start
            valid &= np.isfinite(native_start) & (native_start >= 0)
            result["native_start_amount"] = start.native_amount.copy(deep=True)
        method = "native_cumulative_end_minus_start_then_unit_factor"
        event["interval_start"] = interval_start
        event["duration_hours"] = 1
    amount = values * factor
    result["amount"] = xr.DataArray(
        np.where(valid & np.isfinite(amount) & (amount >= 0), amount, np.nan),
        dims=("y", "x"),
        coords=end.native_amount.coords,
        attrs={"units": "kg/m^2"},
    )
    event["normalization"] = {
        "method": method,
        "unit_factor_to_kg_m2": factor,
        "invalid_cells": (
            "Negative/nonfinite native parents or increments remain missing; no clipping"
        ),
    }
    return result, event


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_ice.py",
        "application/ice.py",
        "forecasting/ice.py",
        "guidance/sources/ice.py",
        "guidance/sources/cloud.py",
        "guidance/sources/rap.py",
        "guidance/sources/probabilistic.py",
        "guidance/sources/precipitation_type.py",
        "guidance/sources/snowfall.py",
    ):
        result["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return result


def _copy_input(directory: Path, row: RetainedInput) -> dict[str, Any]:
    record, payloads, inventory = row
    if (
        len(inventory) != record["index_bytes"]
        or hashlib.sha256(inventory).hexdigest() != record["index_sha256"]
        or len(record["messages"]) != 1
    ):
        raise ValueError("Retained ice inventory or single-message identity mismatch")
    message = record["messages"][0]
    paths = [(directory / name).resolve() for name in (record["index_file"], message["raw_file"])]
    if any(not path.is_relative_to(directory.resolve()) for path in paths):
        raise ValueError("Retained ice destination leaves its source directory")
    if message["field"] != "ice_amount":
        raise ValueError("Retained ice input must identify the native amount field")
    payload = payloads[message["field"]]
    if (
        len(payload) != message["raw_bytes"]
        or message["byte_end"] - message["byte_start"] != len(payload)
        or hashlib.sha256(payload).hexdigest() != message["raw_sha256"]
    ):
        raise ValueError("Retained ice message identity mismatch")
    _write_bytes(paths[0], inventory)
    _write_bytes(paths[1], payload)
    return deepcopy(record)


def prepare_ice_run(
    prepared_run: Path,
    output_directory: Path,
    *,
    from_raw: bool = False,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    retained_records: dict[tuple[str, int], RetainedInput] | None = None,
) -> dict[str, Any]:
    """Use selected cycles, one native acquisition per lead, and shared regional views."""
    output_directory = output_directory.resolve()
    if (
        output_directory.is_relative_to(Path(__file__).resolve().parents[3])
        or output_directory.exists()
    ):
        raise ValueError("Ice preparation requires a new directory outside Git")
    original_bytes = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_bytes)
    selection = original["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("Ice evidence requires an existing surface preparation")
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    cycles = dict(selection["selected_cycles"])
    if original.get("pop_guidance", {}).get("selected_cycle"):
        cycles["NBM"] = original["pop_guidance"]["selected_cycle"]
    areas = _areas(original)
    previous = {
        row["source_id"]: row for row in original.get("ice_guidance", {}).get("sources", [])
    }
    if from_raw and not previous:
        raise ValueError("Offline ice replay requires retained source descriptors")
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    owned = (
        BoundedHttpTransport(body_budget=256 * 1024 * 1024)
        if transport is None and not from_raw
        else None
    )
    active_transport = transport or owned
    before = int(getattr(active_transport, "downloaded_bytes", 0))
    output_directory.mkdir(parents=True)
    descriptors, statuses = [], deepcopy(SOURCES)
    try:
        for source_id, source in SOURCES.items():
            model = source["model"]
            if not source["supported"]:
                continue
            if model not in cycles:
                statuses[source_id]["missing_reason"] = "No selected cycle for native ice guidance"
                continue
            cycle = _hour(datetime.fromisoformat(cycles[model]))
            if cycle > target:
                raise ValueError("Ice cycle cannot follow the prepared reference time")
            offset = int((target - cycle).total_seconds() / 3600)
            ends = list(range(offset + 1, offset + 37))
            cumulative = source["temporal_support"] == "native_cycle_cumulative_accumulation"
            leads = ([offset] if cumulative and offset > 0 else []) + ends
            directory = output_directory / source_id
            (directory / "raw").mkdir(parents=True)
            replay: dict[int, RetainedInput] = {}
            failures: dict[str, str] = {}
            if from_raw:
                old = previous[source_id]
                old_directory = Path(old["directory"])
                old_manifest = json.loads(
                    _checked_file(old_directory, "manifest.json", old["manifest_sha256"])
                )
                if (
                    old_manifest["source_id"] != source_id
                    or old_manifest["model"] != model
                    or old_manifest["source_cycle"] != _iso(cycle)
                    or old_manifest["target_reference_time"] != _iso(target)
                    or old_manifest["policy"] != POLICY
                ):
                    raise ValueError("Retained ice source/cycle/target/policy mismatch")
                for record in old_manifest["inputs"]:
                    if record["lead"] in replay:
                        raise ValueError("Duplicate retained ice source lead")
                    replay[record["lead"]] = (
                        record,
                        read_type_input(old_directory, record),
                        (old_directory / record["index_file"]).read_bytes(),
                    )
                failures = deepcopy(old_manifest["request_failures"])
                if set(replay) | {int(key) for key in failures} != set(leads) or set(replay) & {
                    int(key) for key in failures
                }:
                    raise ValueError("Retained ice request set differs from target")
            inputs, native, native_axes = [], {}, None
            for lead in leads:
                row = (
                    replay.get(lead)
                    if from_raw
                    else (retained_records or {}).get((source_id, lead))
                )
                if row is None and not from_raw:
                    try:
                        ice_url(source_id, cycle, lead)
                    except ValueError as exc:
                        failures[str(lead)] = str(exc)
                        continue
                    assert active_transport is not None
                    try:
                        acquired = acquire_ice_lead(
                            source_id,
                            cycle,
                            lead,
                            transport=active_transport,
                            clock=clock,
                            sleeper=sleeper,
                        )
                        if (
                            acquired.model != model
                            or acquired.forecast_hour != lead
                            or acquired.cycle_date != cycle.date()
                            or acquired.cycle_hour != cycle.hour
                            or len(acquired.selected_messages) != 1
                            or acquired.selected_messages[0].canonical_variable_id != "ice_amount"
                        ):
                            raise ValueError("Ice acquisition identity mismatch")
                        record = retain_type_input(directory, acquired)
                        payloads = read_type_input(directory, record)
                    except (FetchError, GribIndexError) as exc:
                        failures[str(lead)] = f"Native ice guidance unavailable: {exc}"
                        continue
                elif row is not None:
                    record, payloads = _copy_input(directory, row), row[1]
                else:
                    continue
                if (
                    record["model"] != model
                    or record["cycle"] != _iso(cycle)
                    or record["lead"] != lead
                    or len(payloads) != 1
                ):
                    raise ValueError("Retained ice source/cycle/lead/message mismatch")
                inputs.append(record)
                dataset, crs, metadata = decode_ice_lead(
                    next(iter(payloads.values())), source_id, cycle, lead
                )
                axes = dataset.x.values, dataset.y.values
                if native_axes is None:
                    native_axes = *axes, crs
                elif crs != native_axes[2] or any(
                    not np.array_equal(a, b) for a, b in zip(axes, native_axes[:2], strict=True)
                ):
                    raise ValueError("Native ice grid changed within cycle")
                views = []
                for area in areas:
                    xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
                    views.append(
                        dataset.isel(
                            y=_axis_slice(axes[1], ymin, ymax), x=_axis_slice(axes[0], xmin, xmax)
                        ).copy(deep=True)
                    )
                native[lead] = views, metadata, record
            events = []
            fields: list[list[xr.Dataset]] = [[] for _ in areas]
            for index, lead in enumerate(ends):
                event = {
                    **deepcopy(source),
                    "source_id": source_id,
                    "event_id": f"{source_id}:{index}",
                    "source_cycle": _iso(cycle),
                    "source_lead_hours": lead,
                    "valid_time": _iso(cycle + timedelta(hours=lead)),
                    "temporal_semantics": "accumulation",
                    "interval_closure": "left_open_right_closed",
                    "interval_start": _iso(
                        cycle + timedelta(hours=lead - (source.get("duration_hours") or 1))
                    ),
                    "interval_end": _iso(cycle + timedelta(hours=lead)),
                    "unit": "kg/m^2",
                    "missing_reasons": [],
                }
                if lead in native:
                    views, metadata, record = native[lead]
                    parent = native.get(lead - 1) if cumulative else None
                    for area_index, region_fields in enumerate(fields):
                        normalized, normalized_event = _normalized_interval(
                            views[area_index],
                            metadata,
                            parent[0][area_index] if parent else None,
                            parent[1] if parent else None,
                            event["interval_start"],
                        )
                        region_fields.append(normalized)
                        event.update(normalized_event)
                    needed = (
                        [parent, native[lead]]
                        if parent and metadata["interval_start"] != event["interval_start"]
                        else [native[lead]]
                    )
                    event["provenance"] = {
                        "parents": [
                            {**deepcopy(item[2]), "native_event": deepcopy(item[1])}
                            for item in needed
                        ]
                    }
                else:
                    event["missing_reasons"] = [failures[str(lead)]]
                    if native:
                        for region_fields, template in zip(
                            fields, next(iter(native.values()))[0], strict=True
                        ):
                            region_fields.append(
                                xr.Dataset({"amount": xr.full_like(template.amount, np.nan)})
                            )
                events.append(event)
            manifest: dict[str, Any] = {
                "source_id": source_id,
                "model": model,
                "source_cycle": _iso(cycle),
                "target_reference_time": _iso(target),
                "source_metadata": deepcopy(source),
                "policy": deepcopy(POLICY),
                "created_at": _iso(clock.now()),
                "code_identity": _identity(),
                "inputs": inputs,
                "events": events,
                "request_failures": failures,
                "regions": [],
            }
            if not native:
                statuses[source_id]["missing_reason"] = "No native ice messages were retained"
                statuses[source_id]["request_failures"] = deepcopy(failures)
                statuses[source_id]["missing_reasons"] = list(dict.fromkeys(failures.values()))
            for index, region_fields in enumerate(fields):
                if not region_fields:
                    continue
                view = xr.concat(region_fields, dim="event", coords="minimal", compat="equals")
                view = view.assign_coords(event=list(range(36)))
                assert native_axes is not None
                view.attrs = {"crs_wkt2": native_axes[2].to_wkt()}
                filename = f"regions/{index}.nc"
                path = directory / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                view.to_netcdf(
                    path,
                    engine="h5netcdf",
                    encoding={name: {"zlib": True, "complevel": 4} for name in view.data_vars},
                )
                manifest["regions"].append(
                    {
                        "file": filename,
                        "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            digest = _write_bytes(
                directory / "manifest.json",
                json.dumps(manifest, indent=2, allow_nan=False).encode(),
            )
            descriptors.append(
                {
                    "source_id": source_id,
                    "model": model,
                    "directory": str(directory),
                    "manifest_sha256": digest,
                    "available_times": sum(not event["missing_reasons"] for event in events),
                    "raw_bytes": sum(m["raw_bytes"] for r in inputs for m in r["messages"]),
                    "index_bytes": sum(r["index_bytes"] for r in inputs),
                    "prepared_bytes": sum(r["bytes"] for r in manifest["regions"]),
                }
            )
    finally:
        if owned is not None:
            owned.close()
    result = {
        **original,
        "ice_guidance": {
            "sources": descriptors,
            "source_status": statuses,
            "policy": deepcopy(POLICY),
            "source_preparation_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "created_at": _iso(clock.now()),
            "note": "Separately acquired ice evidence; not retroactively available "
            "at the original surface decision time",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def load_ice_guidance(
    descriptor: dict[str, Any], *, target_reference_time: np.datetime64
) -> list[IceView]:
    if descriptor["policy"] != POLICY:
        raise ValueError("No approved active ice blend policy")
    result, seen = [], set()
    for source in descriptor["sources"]:
        source_id, model = source["source_id"], source["model"]
        if source_id in seen:
            raise ValueError("Duplicate ice source descriptor")
        seen.add(source_id)
        directory = Path(source["directory"])
        manifest = json.loads(_checked_file(directory, "manifest.json", source["manifest_sha256"]))
        if (
            manifest["source_id"] != source_id
            or manifest["model"] != model
            or manifest["policy"] != POLICY
            or np.datetime64(manifest["target_reference_time"].removesuffix("Z"), "ns")
            != target_reference_time
        ):
            raise ValueError("Ice source/target/policy mismatch")
        for record in manifest["inputs"]:
            read_type_input(directory, record)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                ds = opened.load()
            if (
                ds.sizes["event"] != len(manifest["events"])
                or ds.amount.attrs.get("units") != "kg/m^2"
            ):
                raise ValueError("Ice event axis or units mismatch")
            result.append(
                IceView(
                    ds,
                    pyproj.CRS.from_wkt(ds.attrs["crs_wkt2"]),
                    {
                        **manifest,
                        "manifest_sha256": source["manifest_sha256"],
                        "prepared_file": region,
                    },
                )
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--from-raw", action="store_true")
    args = parser.parse_args()
    result = prepare_ice_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(json.dumps(result["ice_guidance"], indent=2))


if __name__ == "__main__":
    main()
