"""Attach shared native p-type evidence to an existing 36-hour prepared surface run."""

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

from mesoforge.application.precipitation_type import POLICY, TypeView
from mesoforge.application.prepared_pop import _checked_file
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
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition
from mesoforge.guidance.coverage import window_hours
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.precipitation_type import (
    SOURCES,
    acquire_type_lead,
    decode_type_lead,
    type_url,
)


def _identity() -> dict[str, Any]:
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_precipitation_type.py",
        "application/precipitation_type.py",
        "guidance/sources/precipitation_type.py",
        "guidance/sources/probabilistic.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return identity


def retain_type_input(directory: Path, acquired: Phase2LeadAcquisition) -> dict[str, Any]:
    """Retain the existing acquisition record and individual GRIB messages before decoding."""
    lead = acquired.forecast_hour
    index_file = f"raw/f{lead:03d}.index"
    record: dict[str, Any] = {
        "model": acquired.model,
        "lead": lead,
        "cycle": f"{acquired.cycle_date.isoformat()}T{acquired.cycle_hour:02d}:00:00Z",
        "source_grib_url": acquired.resolved_grib_url,
        "source_index_url": acquired.resolved_index_url,
        "endpoint": acquired.endpoint,
        "etag": acquired.full_object_etag,
        "full_object_content_length": acquired.full_object_content_length,
        "grib_last_modified": acquired.full_object_last_modified,
        "index_last_modified": acquired.index_last_modified,
        "grib_retrieved_at": _iso(acquired.grib_completed_at),
        "index_retrieved_at": _iso(acquired.index_completed_at),
        "grib_available_at": _iso(acquired.grib_available_at),
        "index_available_at": _iso(acquired.index_available_at),
        "index_file": index_file,
        "index_sha256": _write_bytes(directory / index_file, acquired.index_payload),
        "index_bytes": len(acquired.index_payload),
        "messages": [],
    }
    for message in acquired.selected_messages:
        filename = f"raw/f{lead:03d}-{message.canonical_variable_id}.grib2"
        record["messages"].append(
            {
                "field": message.canonical_variable_id,
                "byte_start": message.byte_start,
                "byte_end": message.byte_end,
                "selected_index_row": message.row.line,
                "raw_file": filename,
                "raw_sha256": _write_bytes(directory / filename, message.payload),
                "raw_bytes": len(message.payload),
            }
        )
    _write_bytes(directory / f"raw/f{lead:03d}.json", json.dumps(record, indent=2).encode())
    return record


def read_type_input(directory: Path, record: dict[str, Any]) -> dict[str, bytes]:
    _checked_file(directory, record["index_file"], record["index_sha256"], record["index_bytes"])
    result = {}
    for message in record["messages"]:
        if (
            message["field"] in result
            or message["byte_end"] - message["byte_start"] != message["raw_bytes"]
        ):
            raise ValueError("P-type raw field/range identity is invalid")
        result[message["field"]] = _checked_file(
            directory, message["raw_file"], message["raw_sha256"], message["raw_bytes"]
        )
    return result


def prepare_type_run(
    prepared_run: Path,
    output_directory: Path,
    *,
    from_raw: bool = False,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    retained_acquisitions: dict[tuple[str, int], Phase2LeadAcquisition] | None = None,
) -> dict[str, Any]:
    output_directory = output_directory.resolve()
    if (
        output_directory.is_relative_to(Path(__file__).resolve().parents[3])
        or output_directory.exists()
    ):
        raise ValueError("P-type preparation requires a new directory outside Git")
    original_bytes = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_bytes)
    selection = original["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("P-type attaches to an existing surface-grid preparation")
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    hours = window_hours(selection)
    cycles = dict(selection["selected_cycles"])
    if original.get("pop_guidance", {}).get("selected_cycle"):
        cycles["NBM"] = original["pop_guidance"]["selected_cycle"]
    areas = _areas(original)
    previous = {row["model"]: row for row in original.get("ptype_guidance", {}).get("sources", [])}
    if from_raw and not previous:
        raise ValueError("Offline p-type replay requires retained source descriptors")
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    owned = (
        BoundedHttpTransport(body_budget=256 * 1024 * 1024)
        if transport is None and not from_raw
        else None
    )
    active_transport = transport or owned
    before = int(getattr(active_transport, "downloaded_bytes", 0))
    output_directory.mkdir(parents=True)
    descriptors = []
    try:
        for model in SOURCES:
            directory = output_directory / model
            (directory / "raw").mkdir(parents=True)
            old_directory = None
            old_manifest = None
            if from_raw:
                descriptor = previous[model]
                old_directory = Path(descriptor["directory"])
                old_manifest = json.loads(
                    _checked_file(old_directory, "manifest.json", descriptor["manifest_sha256"])
                )
                if (
                    old_manifest["target_reference_time"] != _iso(target)
                    or old_manifest["model"] != model
                ):
                    raise ValueError("Retained p-type source target/model mismatch")
            cycle = _hour(datetime.fromisoformat(cycles[model])) if model in cycles else None
            if cycle is not None and cycle > target:
                raise ValueError("P-type source cycle cannot follow the prepared reference time")
            events, inputs, views = [], [], None
            native_axes = None
            slices = []
            for hour in hours:
                valid = target + timedelta(hours=hour)
                lead = int((valid - cycle).total_seconds() / 3600) if cycle else None
                event = {
                    **deepcopy(SOURCES[model]),
                    "event_id": f"{model}:{hour}",
                    "source_cycle": _iso(cycle) if cycle else None,
                    "source_lead_hours": lead,
                    "valid_time": _iso(valid),
                    "temporal_semantics": "instantaneous",
                    "interval_start": None,
                    "interval_end": None,
                    "missing_reasons": [],
                }
                record = None
                payloads = None
                if from_raw:
                    assert old_manifest is not None and old_directory is not None
                    old_event = old_manifest["events"][hour - 1]
                    if old_event["valid_time"] != _iso(valid):
                        raise ValueError("Retained p-type event valid time mismatch")
                    event["missing_reasons"] = list(old_event["missing_reasons"])
                    if old_event.get("provenance"):
                        record = deepcopy(old_event["provenance"])
                        if (
                            cycle is None
                            or record["model"] != model
                            or record["cycle"] != _iso(cycle)
                            or record["lead"] != lead
                        ):
                            raise ValueError("Retained p-type cycle/lead mismatch")
                        payloads = read_type_input(old_directory, record)
                        _write_bytes(
                            directory / record["index_file"],
                            (old_directory / record["index_file"]).read_bytes(),
                        )
                        for message in record["messages"]:
                            _write_bytes(
                                directory / message["raw_file"], payloads[message["field"]]
                            )
                elif cycle is None:
                    event["missing_reasons"] = ["No existing selected cycle for this type source"]
                else:
                    assert lead is not None and active_transport is not None
                    try:
                        type_url(model, cycle, lead)
                    except ValueError as exc:
                        event["missing_reasons"] = [str(exc)]
                    if not event["missing_reasons"]:
                        try:
                            acquired = (retained_acquisitions or {}).get((model, lead))
                            if acquired is None:
                                acquired = acquire_type_lead(
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
                            ):
                                raise ValueError(
                                    "Acquired p-type identity differs from prepared source"
                                )
                            record = retain_type_input(directory, acquired)
                            payloads = read_type_input(directory, record)
                        except (FetchError, GribIndexError) as exc:
                            event["missing_reasons"] = [
                                f"Native p-type acquisition unavailable: {exc}"
                            ]
                if record is not None and payloads is not None:
                    assert cycle is not None and lead is not None
                    # Decoder/identity failures are scientific blockers, not permission
                    # to silently hide invalid guidance behind a successful empty run.
                    dataset, crs, metadata = decode_type_lead(payloads, model, cycle, lead)
                    event.update(metadata, event_id=f"{model}:{hour}", provenance=record)
                    inputs.append(record)
                    x, y = dataset.x.values, dataset.y.values
                    if views is None:
                        native_axes = x, y, crs
                        for area in areas:
                            xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
                            slices.append((_axis_slice(y, ymin, ymax), _axis_slice(x, xmin, xmax)))
                        views = [
                            xr.Dataset(
                                {
                                    name: (
                                        ("event", "y", "x"),
                                        np.full((len(hours), len(y[ys]), len(x[xs])), np.nan),
                                        dict(field.attrs),
                                    )
                                    for name, field in dataset.data_vars.items()
                                },
                                coords={"event": list(range(len(hours))), "y": y[ys], "x": x[xs]},
                                attrs={"crs_wkt2": crs.to_wkt()},
                            )
                            for ys, xs in slices
                        ]
                    elif (
                        native_axes is None
                        or crs != native_axes[2]
                        or not np.array_equal(x, native_axes[0])
                        or not np.array_equal(y, native_axes[1])
                    ):
                        raise ValueError("P-type native grid changes within one source cycle")
                    for view, (ys, xs) in zip(views, slices, strict=True):
                        for name in dataset.data_vars:
                            view[name].values[hour - 1] = dataset[name].values[ys, xs]
                events.append(event)
            manifest: dict[str, Any] = {
                "model": model,
                "target_reference_time": _iso(target),
                "source_metadata": deepcopy(SOURCES[model]),
                "policy": deepcopy(POLICY),
                "created_at": _iso(clock.now()),
                "code_identity": _identity(),
                "events": events,
                "inputs": inputs,
                "regions": [],
            }
            for index, view in enumerate(views or []):
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
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "bytes": path.stat().st_size,
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
                    "available_hours": sum(not e["missing_reasons"] for e in events),
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
        "ptype_guidance": {
            "sources": descriptors,
            "policy": deepcopy(POLICY),
            "source_preparation_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "created_at": _iso(clock.now()),
            "note": "Native evidence acquired separately from original surface decision; "
            "no retroactive availability claim",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def load_type_guidance(
    descriptor: dict[str, Any], *, target_reference_time: np.datetime64
) -> list[TypeView]:
    if descriptor["policy"] != POLICY:
        raise ValueError("P-type policy differs from the supported temporary agreement rule")
    result = []
    seen = set()
    for source in descriptor["sources"]:
        model = source["model"]
        if model in seen:
            raise ValueError("Duplicate p-type source descriptor")
        seen.add(model)
        directory = Path(source["directory"])
        manifest = json.loads(_checked_file(directory, "manifest.json", source["manifest_sha256"]))
        if (
            manifest["model"] != model
            or np.datetime64(manifest["target_reference_time"].removesuffix("Z"), "ns")
            != target_reference_time
            or manifest["policy"] != POLICY
        ):
            raise ValueError("P-type source/target/policy mismatch")
        for record in manifest["inputs"]:
            read_type_input(directory, record)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                ds = opened.load()
            if ds.sizes["event"] != len(manifest["events"]):
                raise ValueError("P-type event axis differs from retained metadata")
            result.append(
                TypeView(
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
    result = prepare_type_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(json.dumps(result["ptype_guidance"], indent=2))


if __name__ == "__main__":
    main()
