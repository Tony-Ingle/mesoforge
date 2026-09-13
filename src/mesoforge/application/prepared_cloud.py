"""Retain shared native cloud inputs; new forecasts apply the approved NBM sky policy."""

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

from mesoforge.application.cloud_cover import CloudView
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
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.cloud import (
    SOURCES,
    acquire_cloud_lead,
    cloud_url,
    decode_cloud_lead,
)

# Historical preparation metadata remains readable. It described the policy at
# acquisition time, not a restriction on using those native inputs in a new issue.
LEGACY_POLICY = {
    "status": "unavailable",
    "weights": {},
    "reason": "No approved cloud-cover blend; native contributors remain zero-weight evidence",
}
POLICY = {
    "id": "native-total-cloud-preparation.v1",
    "status": "native_evidence",
    "weights": {},
    "reason": "Retain native contributors separately; issuance applies its versioned sky policy",
}
RetainedInput = tuple[dict[str, Any], dict[str, bytes], bytes]


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_cloud.py",
        "application/cloud_cover.py",
        "forecasting/cloud_cover.py",
        "guidance/sources/cloud.py",
        "guidance/sources/rap.py",
        "guidance/sources/probabilistic.py",
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
        raise ValueError("Retained cloud inventory or single-message identity mismatch")
    _write_bytes(directory / record["index_file"], inventory)
    message = record["messages"][0]
    payload = payloads[message["field"]]
    if (
        len(payload) != message["raw_bytes"]
        or message["byte_end"] - message["byte_start"] != len(payload)
        or hashlib.sha256(payload).hexdigest() != message["raw_sha256"]
    ):
        raise ValueError("Retained cloud message identity mismatch")
    _write_bytes(directory / message["raw_file"], payload)
    return deepcopy(record)


def prepare_cloud_run(
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
        raise ValueError("Cloud preparation requires a new directory outside Git")
    original_bytes = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_bytes)
    selection = original["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("Cloud evidence requires an existing surface preparation")
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    cycles = dict(selection["selected_cycles"])
    if original.get("pop_guidance", {}).get("selected_cycle"):
        cycles["NBM"] = original["pop_guidance"]["selected_cycle"]
    areas = _areas(original)
    previous = {row["model"]: row for row in original.get("cloud_guidance", {}).get("sources", [])}
    if from_raw and not previous:
        raise ValueError("Offline cloud replay requires retained source descriptors")
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
        for model, source in SOURCES.items():
            if model not in cycles:
                statuses[model]["missing_reason"] = "No selected cycle for native cloud guidance"
                continue
            cycle = _hour(datetime.fromisoformat(cycles[model]))
            if cycle > target:
                raise ValueError("Cloud cycle cannot follow the prepared reference time")
            offset = int((target - cycle).total_seconds() / 3600)
            leads = list(range(offset + 1, offset + 37))
            directory = output_directory / model
            (directory / "raw").mkdir(parents=True)
            replay: dict[int, RetainedInput] = {}
            failures: dict[str, str] = {}
            if from_raw:
                old = previous[model]
                old_directory = Path(old["directory"])
                old_manifest = json.loads(
                    _checked_file(old_directory, "manifest.json", old["manifest_sha256"])
                )
                if (
                    old_manifest["model"] != model
                    or old_manifest["source_cycle"] != _iso(cycle)
                    or old_manifest["target_reference_time"] != _iso(target)
                    or old_manifest["policy"] not in (POLICY, LEGACY_POLICY)
                ):
                    raise ValueError("Retained cloud source/cycle/target/policy mismatch")
                for record in old_manifest["inputs"]:
                    if record["lead"] in replay:
                        raise ValueError("Duplicate retained cloud source lead")
                    replay[record["lead"]] = (
                        record,
                        read_type_input(old_directory, record),
                        (old_directory / record["index_file"]).read_bytes(),
                    )
                failures = deepcopy(old_manifest["request_failures"])
                if set(replay) | {int(key) for key in failures} != set(leads) or set(replay) & {
                    int(key) for key in failures
                }:
                    raise ValueError("Retained cloud request set differs from target")
            inputs, native, native_axes = [], {}, None
            for lead in leads:
                row = replay.get(lead) if from_raw else (retained_records or {}).get((model, lead))
                if row is None and not from_raw:
                    try:
                        cloud_url(model, cycle, lead)
                    except ValueError as exc:
                        failures[str(lead)] = str(exc)
                        continue
                    assert active_transport is not None
                    try:
                        acquired = acquire_cloud_lead(
                            model,
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
                        ):
                            raise ValueError("Cloud acquisition identity mismatch")
                        record = retain_type_input(directory, acquired)
                        payloads = read_type_input(directory, record)
                    except (FetchError, GribIndexError) as exc:
                        failures[str(lead)] = f"Native total-cloud guidance unavailable: {exc}"
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
                    raise ValueError("Retained cloud source/cycle/lead/message mismatch")
                inputs.append(record)
                dataset, crs, metadata = decode_cloud_lead(
                    next(iter(payloads.values())), model, cycle, lead
                )
                axes = dataset.x.values, dataset.y.values
                if native_axes is None:
                    native_axes = *axes, crs
                elif crs != native_axes[2] or any(
                    not np.array_equal(a, b) for a, b in zip(axes, native_axes[:2], strict=True)
                ):
                    raise ValueError("Native cloud grid changed within cycle")
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
                    "event_id": f"{model}:{index}",
                    "source_cycle": _iso(cycle),
                    "source_lead_hours": lead,
                    "valid_time": _iso(cycle + timedelta(hours=lead)),
                    "temporal_semantics": "instantaneous",
                    "interval_start": None,
                    "interval_end": None,
                    "unit": "percent",
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
                statuses[model]["missing_reason"] = "No native total-cloud messages were retained"
                statuses[model]["request_failures"] = deepcopy(failures)
                statuses[model]["missing_reasons"] = list(dict.fromkeys(failures.values()))
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
        "cloud_guidance": {
            "sources": descriptors,
            "source_status": statuses,
            "policy": deepcopy(POLICY),
            "source_preparation_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "created_at": _iso(clock.now()),
            "note": "Separately acquired total-cloud evidence; not retroactively available "
            "at the original surface decision time",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def load_cloud_guidance(
    descriptor: dict[str, Any], *, target_reference_time: np.datetime64
) -> list[CloudView]:
    if descriptor["policy"] not in (POLICY, LEGACY_POLICY):
        raise ValueError("Unsupported native cloud preparation policy")
    result, seen = [], set()
    for source in descriptor["sources"]:
        model = source["model"]
        if model in seen:
            raise ValueError("Duplicate cloud model descriptor")
        seen.add(model)
        directory = Path(source["directory"])
        manifest = json.loads(_checked_file(directory, "manifest.json", source["manifest_sha256"]))
        if (
            manifest["model"] != model
            or manifest["policy"] not in (POLICY, LEGACY_POLICY)
            or np.datetime64(manifest["target_reference_time"].removesuffix("Z"), "ns")
            != target_reference_time
        ):
            raise ValueError("Cloud source/target/policy mismatch")
        for record in manifest["inputs"]:
            read_type_input(directory, record)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                ds = opened.load()
            if (
                ds.sizes["event"] != len(manifest["events"])
                or ds.cloud_cover.attrs.get("units") != "percent"
            ):
                raise ValueError("Cloud event axis or units mismatch")
            result.append(
                CloudView(
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
    result = prepare_cloud_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(json.dumps(result["cloud_guidance"], indent=2))


if __name__ == "__main__":
    main()
