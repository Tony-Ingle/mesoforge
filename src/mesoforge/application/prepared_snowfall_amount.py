"""Prepare native new-snow amounts, native SLR and profiles beside retained SWE.

This optional bounded attachment does not modify the completed SWE preparation or
assign an active snowfall policy. Raw retention and regional views use existing paths.
"""

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
from mesoforge.application.snowfall_amount_forecast import AmountView
from mesoforge.application.spatial_coverage import native_bbox_bounds
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.snowfall_amount import (
    SOURCES,
    acquire_amount_lead,
    amount_url,
    decode_amount_lead,
)

POLICY = {
    "status": "unavailable",
    "weights": {},
    "reason": "No approved snowfall-amount blend; native amounts, native SLR and "
    "Kuchera estimates remain separate zero-weight evidence",
}
RetainedInput = tuple[dict[str, Any], dict[str, bytes], bytes]


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_snowfall_amount.py",
        "application/snowfall_amount_forecast.py",
        "forecasting/snowfall_amount.py",
        "guidance/sources/snowfall_amount.py",
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
    ):
        raise ValueError("Retained snowfall-amount inventory identity mismatch")
    _write_bytes(directory / record["index_file"], inventory)
    for message in record["messages"]:
        payload = payloads[message["field"]]
        if (
            len(payload) != message["raw_bytes"]
            or message["byte_end"] - message["byte_start"] != len(payload)
            or hashlib.sha256(payload).hexdigest() != message["raw_sha256"]
        ):
            raise ValueError("Retained snowfall-amount message identity mismatch")
        _write_bytes(directory / message["raw_file"], payload)
    return deepcopy(record)


def _interval_amount(
    end: xr.Dataset,
    end_event: dict[str, Any],
    start: xr.Dataset | None,
    start_event: dict[str, Any] | None,
    interval_start: str,
) -> tuple[xr.Dataset, dict[str, Any]]:
    """Use an exact native interval or difference compatible new-snow accumulations.

    Snowpack SNOD is never accepted. Negative increments are missing, not clipped.
    Other end-time fields retain their own instantaneous metadata.
    """
    event = deepcopy(end_event)
    if (
        event["native_quantity"] != "new_snowfall_amount"
        or event["unit"] != "m"
        or event["unit_factor_to_m"] != 1
        or end.native_amount.attrs["units"] != "m"
    ):
        raise ValueError("Expected native new-snow amount in metres, not SWE or snowpack")
    result = end.drop_vars(["native_amount", "amount"]).copy(deep=True)
    native_end = end.native_amount.values
    values = native_end
    valid = np.isfinite(values) & (values >= 0)
    result["native_end_amount"] = end.native_amount.copy(deep=True)
    method = "native_exact_interval_amount"
    if event["interval_start"] != interval_start:
        if start is None or start_event is None:
            values = np.full(native_end.shape, np.nan)
            event["missing_reasons"] = [
                "Missing compatible native cumulative snowfall-amount parent"
            ]
        else:
            if (
                any(
                    start_event[key] != event[key]
                    for key in (
                        "source_cycle",
                        "native_quantity",
                        "unit",
                        "native_unit",
                        "unit_factor_to_m",
                        "interval_start",
                        "interval_closure",
                        "version",
                        "hydrometeor_scope",
                        "spatial_support",
                    )
                )
                or start_event["interval_end"] != interval_start
                or start.native_amount.attrs.get("units") != "m"
                or any(not np.array_equal(start[axis], end[axis]) for axis in ("x", "y"))
            ):
                raise ValueError(
                    "Native snowfall-amount cumulative parents differ in identity/window/grid"
                )
            values = native_end - start.native_amount.values
            valid &= np.isfinite(start.native_amount.values) & (start.native_amount.values >= 0)
            result["native_start_amount"] = start.native_amount.copy(deep=True)
        event["native_interval_start"] = event["interval_start"]
        event["interval_start"] = interval_start
        method = "native_cumulative_end_minus_start"
    values = np.where(valid & np.isfinite(values) & (values >= 0), values, np.nan)
    result["amount"] = xr.DataArray(
        values, dims=("y", "x"), coords=end.native_amount.coords, attrs={"units": "m"}
    )
    event["normalization"] = {
        "method": method,
        "unit_factor_to_m": 1.0,
        "invalid_cells": "negative/nonfinite amounts or parents are missing; no clipping",
    }
    return result, event


def prepare_snowfall_amount_run(
    prepared_run: Path,
    output_directory: Path,
    *,
    from_raw: bool = False,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    retained_records: dict[tuple[str, int], RetainedInput] | None = None,
) -> dict[str, Any]:
    output_directory = output_directory.resolve()
    if (
        output_directory.is_relative_to(Path(__file__).resolve().parents[3])
        or output_directory.exists()
    ):
        raise ValueError("Snowfall-amount preparation requires a new directory outside Git")
    original_bytes = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_bytes)
    selection = original["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("Snowfall amounts attach to an existing surface preparation")
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    cycles = dict(selection["selected_cycles"])
    if original.get("pop_guidance", {}).get("selected_cycle"):
        cycles["NBM"] = original["pop_guidance"]["selected_cycle"]
    areas = _areas(original)
    previous = {
        row["model"]: row for row in original.get("snowfall_amount_guidance", {}).get("sources", [])
    }
    if from_raw and not previous:
        raise ValueError("Offline snowfall-amount replay requires retained source descriptors")
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
            if not source["supported"] or model not in cycles:
                statuses[model].setdefault(
                    "missing_reason", "No selected cycle for native snowfall amount"
                )
                continue
            cycle = _hour(datetime.fromisoformat(cycles[model]))
            if cycle > target:
                raise ValueError("Snowfall-amount cycle cannot follow the prepared reference time")
            offset = int((target - cycle).total_seconds() / 3600)
            ends = list(range(offset + 1, offset + 37))
            cumulative = source["temporal_support"] == "cumulative_hourly"
            leads = ([offset] if cumulative and offset > 0 else []) + ends
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
                    or old_manifest["policy"] != POLICY
                ):
                    raise ValueError(
                        "Retained snowfall-amount identity differs from selected preparation"
                    )
                for record in old_manifest["inputs"]:
                    payloads = read_type_input(old_directory, record)
                    replay[record["lead"]] = (
                        record,
                        payloads,
                        (old_directory / record["index_file"]).read_bytes(),
                    )
                failures = deepcopy(old_manifest["request_failures"])
                if set(replay) | {int(k) for k in failures} != set(leads):
                    raise ValueError("Retained snowfall-amount request set differs from target")
            inputs, native, native_axes = [], {}, None
            for lead in leads:
                row = replay.get(lead) if from_raw else (retained_records or {}).get((model, lead))
                if row is None and not from_raw:
                    try:
                        amount_url(model, cycle, lead)
                    except ValueError as exc:
                        failures[str(lead)] = str(exc)
                        continue
                    assert active_transport is not None
                    try:
                        acquired = acquire_amount_lead(
                            model,
                            cycle,
                            lead,
                            include_profile=model == "RAP" and lead in ends,
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
                            raise ValueError("Snowfall-amount acquisition identity mismatch")
                        record = retain_type_input(directory, acquired)
                        payloads = read_type_input(directory, record)
                    except (FetchError, GribIndexError) as exc:
                        failures[str(lead)] = f"Native snowfall-amount guidance unavailable: {exc}"
                        continue
                elif row is not None:
                    record = _copy_input(directory, row)
                    payloads = row[1]
                else:
                    continue
                if (
                    record["model"] != model
                    or record["cycle"] != _iso(cycle)
                    or record["lead"] != lead
                ):
                    raise ValueError("Retained snowfall-amount source/cycle/lead mismatch")
                inputs.append(record)
                dataset, crs, metadata = decode_amount_lead(payloads, model, cycle, lead)
                axes = dataset.x.values, dataset.y.values
                if native_axes is None:
                    native_axes = *axes, crs
                elif crs != native_axes[2] or any(
                    not np.array_equal(a, b) for a, b in zip(axes, native_axes[:2], strict=True)
                ):
                    raise ValueError("Native snowfall-amount grid changed within cycle")
                views = []
                for area in areas:
                    xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
                    views.append(
                        dataset.isel(
                            y=_axis_slice(axes[1], ymin, ymax), x=_axis_slice(axes[0], xmin, xmax)
                        ).copy(deep=True)
                    )
                native[lead] = views, metadata, record
            events: list[dict[str, Any]] = []
            fields: list[list[xr.Dataset]] = [[] for _ in areas]
            for index, lead in enumerate(ends):
                start = _iso(cycle + timedelta(hours=lead - 1))
                end = _iso(cycle + timedelta(hours=lead))
                event = {
                    **deepcopy(source),
                    "event_id": f"{model}:{index}",
                    "source_cycle": _iso(cycle),
                    "source_lead_hours": lead,
                    "valid_time": end,
                    "interval_start": start,
                    "interval_end": end,
                    "interval_closure": "left_open_right_closed",
                    "temporal_semantics": "accumulation",
                    "native_quantity": "new_snowfall_amount",
                    "unit": "m",
                    "missing_reasons": [],
                }
                if lead not in native:
                    event["missing_reasons"] = [
                        failures.get(str(lead), "Missing native snowfall-amount endpoint")
                    ]
                    if native:
                        for region_fields, template in zip(
                            fields, next(iter(native.values()))[0], strict=True
                        ):
                            region_fields.append(
                                xr.Dataset({"amount": xr.full_like(template.amount, np.nan)})
                            )
                else:
                    views, metadata, record = native[lead]
                    parent = native.get(lead - 1) if cumulative else None
                    for area_index, region_fields in enumerate(fields):
                        view, normalized = _interval_amount(
                            views[area_index],
                            metadata,
                            parent[0][area_index] if parent else None,
                            parent[1] if parent else None,
                            start,
                        )
                        region_fields.append(view)
                        event.update(normalized)
                    needed = (
                        [parent[2], record]
                        if parent and metadata["interval_start"] != start
                        else [record]
                    )
                    event["provenance"] = {"parents": deepcopy(needed)}
                    if "profile" in event:
                        event["profile"]["provenance"] = deepcopy(record)
                    if "native_slr" in event:
                        event["native_slr"]["provenance"] = deepcopy(record)
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
                statuses[model]["missing_reasons"] = list(dict.fromkeys(failures.values()))
            for index, region_fields in enumerate(fields):
                if not region_fields:
                    continue
                view = xr.concat(
                    region_fields, dim="event", coords="minimal", compat="equals"
                ).assign_coords(event=list(range(36)))
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
                    "available_intervals": sum(not e["missing_reasons"] for e in events),
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
        "snowfall_amount_guidance": {
            "sources": descriptors,
            "source_status": statuses,
            "policy": deepcopy(POLICY),
            "source_preparation_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "created_at": _iso(clock.now()),
            "note": "Separately acquired native snowfall amount/SLR/profile evidence; "
            "not retroactively available at the original surface decision time",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def load_snowfall_amount_guidance(
    descriptor: dict[str, Any], *, target_reference_time: np.datetime64
) -> list[AmountView]:
    if descriptor["policy"] != POLICY:
        raise ValueError("No active snowfall-amount blend policy is approved")
    result, seen = [], set()
    for source in descriptor["sources"]:
        model = source["model"]
        if model in seen:
            raise ValueError("Duplicate snowfall-amount model descriptor")
        seen.add(model)
        directory = Path(source["directory"])
        manifest = json.loads(_checked_file(directory, "manifest.json", source["manifest_sha256"]))
        if (
            manifest["model"] != model
            or manifest["policy"] != POLICY
            or np.datetime64(manifest["target_reference_time"].removesuffix("Z"), "ns")
            != target_reference_time
        ):
            raise ValueError("Snowfall-amount source/target/policy mismatch")
        for record in manifest["inputs"]:
            read_type_input(directory, record)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                ds = opened.load()
            if ds.sizes["event"] != len(manifest["events"]) or ds.amount.attrs.get("units") != "m":
                raise ValueError("Snowfall-amount event axis or units mismatch")
            result.append(
                AmountView(
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
    result = prepare_snowfall_amount_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(json.dumps(result["snowfall_amount_guidance"], indent=2))


if __name__ == "__main__":
    main()
