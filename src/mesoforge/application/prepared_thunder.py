"""Attach shared native thunder probabilities while preserving each distinct event."""

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
from mesoforge.application.thunder import ThunderView
from mesoforge.forecasting.thunder import ACTIVE_POLICY as POLICY
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.thunder import (
    INSPECTED_GUIDANCE,
    SOURCES,
    acquire_thunder_lead,
    decode_thunder_lead,
    thunder_url,
)

RetainedInput = tuple[dict[str, Any], dict[str, bytes], bytes]


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_thunder.py",
        "application/thunder.py",
        "forecasting/thunder.py",
        "guidance/sources/thunder.py",
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
        raise ValueError("Retained thunder inventory or single-message identity mismatch")
    message = record["messages"][0]
    paths = [(directory / name).resolve() for name in (record["index_file"], message["raw_file"])]
    if any(not path.is_relative_to(directory.resolve()) for path in paths):
        raise ValueError("Retained thunder destination leaves its source directory")
    if message["field"] != "thunder_probability":
        raise ValueError("Retained thunder input must identify the native probability field")
    payload = payloads[message["field"]]
    if (
        len(payload) != message["raw_bytes"]
        or message["byte_end"] - message["byte_start"] != len(payload)
        or hashlib.sha256(payload).hexdigest() != message["raw_sha256"]
    ):
        raise ValueError("Retained thunder message identity mismatch")
    _write_bytes(paths[0], inventory)
    _write_bytes(paths[1], payload)
    return deepcopy(record)


def prepare_thunder_run(
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
        raise ValueError("Thunder preparation requires a new directory outside Git")
    original_bytes = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_bytes)
    selection = original["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("Thunder evidence requires an existing surface preparation")
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    cycles = dict(selection["selected_cycles"])
    if original.get("pop_guidance", {}).get("selected_cycle"):
        cycles["NBM"] = original["pop_guidance"]["selected_cycle"]
    areas = _areas(original)
    previous = {
        row["source_id"]: row for row in original.get("thunder_guidance", {}).get("sources", [])
    }
    if from_raw and not previous:
        raise ValueError("Offline thunder replay requires retained source descriptors")
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    owned = (
        BoundedHttpTransport(body_budget=256 * 1024 * 1024)
        if transport is None and not from_raw
        else None
    )
    active_transport = transport or owned
    before = int(getattr(active_transport, "downloaded_bytes", 0))
    output_directory.mkdir(parents=True)
    descriptors, statuses = [], deepcopy({**SOURCES, **INSPECTED_GUIDANCE})
    try:
        for source_id, source in SOURCES.items():
            model = source["model"]
            if not source["supported"]:
                continue
            if model not in cycles:
                statuses[source_id]["missing_reason"] = (
                    "No selected cycle for native thunder guidance"
                )
                continue
            cycle = _hour(datetime.fromisoformat(cycles[model]))
            if cycle > target:
                raise ValueError("Thunder cycle cannot follow the prepared reference time")
            offset = int((target - cycle).total_seconds() / 3600)
            leads = list(range(offset + 1, offset + 37))
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
                    raise ValueError("Retained thunder source/cycle/target/policy mismatch")
                for record in old_manifest["inputs"]:
                    if record["lead"] in replay:
                        raise ValueError("Duplicate retained thunder source lead")
                    replay[record["lead"]] = (
                        record,
                        read_type_input(old_directory, record),
                        (old_directory / record["index_file"]).read_bytes(),
                    )
                failures = deepcopy(old_manifest["request_failures"])
                if set(replay) | {int(key) for key in failures} != set(leads) or set(replay) & {
                    int(key) for key in failures
                }:
                    raise ValueError("Retained thunder request set differs from target")
            inputs, native, native_axes = [], {}, None
            for lead in leads:
                row = (
                    replay.get(lead)
                    if from_raw
                    else (retained_records or {}).get((source_id, lead))
                )
                if row is None and not from_raw:
                    try:
                        thunder_url(source_id, cycle, lead)
                    except ValueError as exc:
                        failures[str(lead)] = str(exc)
                        continue
                    assert active_transport is not None
                    try:
                        acquired = acquire_thunder_lead(
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
                            or acquired.selected_messages[0].canonical_variable_id
                            != "thunder_probability"
                        ):
                            raise ValueError("Thunder acquisition identity mismatch")
                        record = retain_type_input(directory, acquired)
                        payloads = read_type_input(directory, record)
                    except (FetchError, GribIndexError) as exc:
                        failures[str(lead)] = f"Native thunder guidance unavailable: {exc}"
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
                    raise ValueError("Retained thunder source/cycle/lead/message mismatch")
                inputs.append(record)
                dataset, crs, metadata = decode_thunder_lead(
                    next(iter(payloads.values())), source_id, cycle, lead
                )
                axes = dataset.x.values, dataset.y.values
                if native_axes is None:
                    native_axes = *axes, crs
                elif crs != native_axes[2] or any(
                    not np.array_equal(a, b) for a, b in zip(axes, native_axes[:2], strict=True)
                ):
                    raise ValueError("Native thunder grid changed within cycle")
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
            for index, lead in enumerate(leads):
                event = {
                    **deepcopy(source),
                    "source_id": source_id,
                    "event_id": f"{source_id}:{index}",
                    "source_cycle": _iso(cycle),
                    "source_lead_hours": lead,
                    "valid_time": _iso(cycle + timedelta(hours=lead)),
                    "temporal_semantics": "interval_probability",
                    "interval_closure": "left_open_right_closed",
                    "interval_start": _iso(
                        cycle + timedelta(hours=lead - source["duration_hours"])
                    ),
                    "interval_end": _iso(cycle + timedelta(hours=lead)),
                    "unit": "1",
                    "missing_reasons": [],
                }
                if lead in native:
                    views, metadata, record = native[lead]
                    event.update(deepcopy(metadata), provenance=deepcopy(record))
                    for region_fields, view in zip(fields, views, strict=True):
                        region_fields.append(view)
                else:
                    event["missing_reasons"] = [failures[str(lead)]]
                    if native:
                        for region_fields, template in zip(
                            fields, next(iter(native.values()))[0], strict=True
                        ):
                            region_fields.append(xr.full_like(template, np.nan))
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
                statuses[source_id]["missing_reason"] = "No native thunder messages were retained"
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
        "thunder_guidance": {
            "sources": descriptors,
            "source_status": statuses,
            "policy": deepcopy(POLICY),
            "source_preparation_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "created_at": _iso(clock.now()),
            "note": "Separately acquired thunder evidence; not retroactively available "
            "at the original surface decision time",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def load_thunder_guidance(
    descriptor: dict[str, Any], *, target_reference_time: np.datetime64
) -> list[ThunderView]:
    if descriptor["policy"] != POLICY:
        raise ValueError("Unrecognized native thunder baseline policy")
    result, seen = [], set()
    for source in descriptor["sources"]:
        source_id, model = source["source_id"], source["model"]
        if source_id in seen:
            raise ValueError("Duplicate thunder source descriptor")
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
            raise ValueError("Thunder source/target/policy mismatch")
        for record in manifest["inputs"]:
            read_type_input(directory, record)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                ds = opened.load()
            if (
                ds.sizes["event"] != len(manifest["events"])
                or ds.thunder_probability.attrs.get("units") != "1"
                or ds.native_probability.attrs.get("units")
                != manifest["source_metadata"]["native_unit"]
            ):
                raise ValueError("Thunder event axis or units mismatch")
            result.append(
                ThunderView(
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
    result = prepare_thunder_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(json.dumps(result["thunder_guidance"], indent=2))


if __name__ == "__main__":
    main()
