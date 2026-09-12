"""Retain native snowfall water equivalent beside an existing surface preparation.

This optional, bounded step runs before grid/HTTP calculation. It reuses the existing
raw-message and prepared-view storage; no snowfall blend policy is introduced.
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
from mesoforge.application.prepared_probability_sources import (
    _areas,
    _read_inputs,
    _retain,
    retain_acquisition,
)
from mesoforge.application.prepared_shadow import _axis_slice
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _code_identity,
    _hour,
    _iso,
    _write_bytes,
)
from mesoforge.application.snowfall_forecast import CLOSURE, QUANTITY, UNIT, SnowView
from mesoforge.application.spatial_coverage import native_bbox_bounds
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.snowfall import (
    SOURCES,
    acquire_snowfall_lead,
    decode_snowfall_lead,
    snowfall_url,
)

POLICY = {
    "status": "unavailable",
    "weights": {},
    "reason": "No approved snowfall-water-"
    "equivalent blend rule; native contributors are zero-weight evidence only",
}
RetainedInput = tuple[dict[str, Any], bytes, bytes]


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_snowfall.py",
        "application/snowfall_forecast.py",
        "guidance/sources/snowfall.py",
        "guidance/sources/probabilistic.py",
    ):
        result["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return result


def snowfall_requests(model: str, cycle: datetime, target: datetime) -> list[tuple[int, int]]:
    """Output interval lead bounds. Native three-hour amounts are never split hourly."""
    offset = int((target - cycle).total_seconds() / 3600)
    if cycle > target:
        raise ValueError("Snowfall cycle cannot follow the prepared reference time")
    support = SOURCES[model]["temporal_support"]
    period = 3 if support == "cumulative_native_3h" else 1
    return [(end - period, end) for end in range(offset + 1, offset + 37) if end % period == 0]


def _normalized_interval(
    end: xr.Dataset,
    end_event: dict[str, Any],
    start: xr.Dataset | None = None,
    start_event: dict[str, Any] | None = None,
) -> tuple[xr.Dataset, dict[str, Any]]:
    """Difference compatible native cumulative parents, then convert water-equivalent units.

    No snowpack differencing, temporal interpolation, packing clamp, or snow ratio.
    Invalid/negative increments stay missing instead of being converted to zero.
    """
    event = deepcopy(end_event)
    factor = event["unit_factor_to_kg_m2"]
    native_end = end.native_amount.values
    valid = np.isfinite(native_end) & (native_end >= 0)
    values = native_end
    fields = {"native_end_amount": end.native_amount.copy(deep=True)}
    method = "native_interval_amount_times_unit_factor"
    if start is not None:
        assert start_event is not None
        if (
            any(
                start_event[key] != event[key]
                for key in (
                    "source_cycle",
                    "interval_start",
                    "native_quantity",
                    "native_unit",
                    "unit_factor_to_kg_m2",
                    "interval_closure",
                    "spatial_support",
                    "version",
                )
            )
            or event["native_quantity"] != QUANTITY
            or datetime.fromisoformat(start_event["interval_end"])
            >= datetime.fromisoformat(event["interval_end"])
            or any(not np.array_equal(start[axis], end[axis]) for axis in ("x", "y"))
        ):
            raise ValueError(
                "Snowfall cumulative parents have incompatible intervals/identity/grid"
            )
        native_start = start.native_amount.values
        valid &= np.isfinite(native_start) & (native_start >= 0)
        values = native_end - native_start
        fields["native_start_amount"] = start.native_amount.copy(deep=True)
        event["native_interval_start"] = event["interval_start"]
        event["interval_start"] = start_event["interval_end"]
        method = "native_cumulative_end_minus_start_then_unit_factor"
    amount = values * factor
    amount = np.where(valid & np.isfinite(amount) & (amount >= 0), amount, np.nan)
    fields["amount"] = xr.DataArray(
        amount, dims=("y", "x"), coords=end.coords, attrs={"units": UNIT}
    )
    event["normalization"] = {
        "method": method,
        "unit_factor_to_kg_m2": factor,
        "invalid_cells": "nonfinite/negative native parents or increments are missing; no clipping",
    }
    return xr.Dataset(fields, attrs=end.attrs), event


def prepare_snowfall_run(
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
        raise ValueError("Snowfall preparation requires a new directory outside Git")
    original_bytes = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_bytes)
    selection = original["current_model_set"]["selection"]
    if not selection.get("surface_fields"):
        raise ValueError("Snowfall attaches to existing surface-grid preparation")
    target = _hour(datetime.fromisoformat(selection["target_reference_time"]))
    areas = _areas(original)
    previous = {
        row["model"]: row for row in original.get("snowfall_guidance", {}).get("sources", [])
    }
    if from_raw and not previous:
        raise ValueError("Offline snowfall replay requires retained source descriptors")
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    owned = (
        BoundedHttpTransport(body_budget=96 * 1024 * 1024)
        if transport is None and not from_raw
        else None
    )
    active_transport = transport or owned
    before = int(getattr(active_transport, "downloaded_bytes", 0))
    output_directory.mkdir(parents=True)
    descriptors, statuses = [], deepcopy(SOURCES)
    try:
        for model, source in SOURCES.items():
            if not source["supported"] or model not in selection["selected_cycles"]:
                statuses[model].setdefault(
                    "missing_reason", "No selected cycle for snowfall source"
                )
                continue
            cycle = _hour(datetime.fromisoformat(selection["selected_cycles"][model]))
            cumulative = source["temporal_support"] == "cumulative_native_3h"
            intervals = snowfall_requests(model, cycle, target)
            leads = sorted(
                {lead for bounds in intervals for lead in (bounds if cumulative else bounds[1:])}
            )
            directory = output_directory / model
            (directory / "raw").mkdir(parents=True)
            replay: dict[int, RetainedInput] = {}
            failures: dict[str, str] = {}
            if from_raw:
                old = previous[model]
                old_directory = Path(old["directory"])
                manifest = json.loads(
                    _checked_file(old_directory, "manifest.json", old["manifest_sha256"])
                )
                if (
                    manifest["model"] != model
                    or manifest["target_reference_time"] != _iso(target)
                    or manifest["policy"] != POLICY
                    or manifest["source_cycle"] != _iso(cycle)
                ):
                    raise ValueError("Retained snowfall source/target/policy/cycle mismatch")
                replay = {
                    row[0]["request"]["end_hour"]: row
                    for row in _read_inputs(old_directory, manifest)
                }
                failures = deepcopy(manifest["request_failures"])
                if set(replay) | {int(key) for key in failures} != set(leads):
                    raise ValueError("Retained snowfall request set differs from target")
            inputs, native, native_axes = [], {}, None
            for lead in leads:
                row = replay.get(lead) if from_raw else (retained_records or {}).get((model, lead))
                if row is None and not from_raw:
                    try:
                        snowfall_url(model, cycle, lead)
                    except ValueError as exc:
                        failures[str(lead)] = str(exc)
                        continue
                    assert active_transport is not None
                    try:
                        acquired = acquire_snowfall_lead(
                            model,
                            cycle,
                            lead,
                            transport=active_transport,
                            clock=clock,
                            sleeper=sleeper,
                        )
                        if acquired.model != model:
                            raise ValueError("Snowfall acquisition model identity differs")
                        row = retain_acquisition(
                            {
                                "source_id": model,
                                "cycle": _iso(cycle),
                                "start_hour": 0 if cumulative else lead - 1,
                                "end_hour": lead,
                            },
                            acquired,
                        )
                        row[0]["selected_index_row"] = acquired.selected_messages[0].row.line
                    except (FetchError, GribIndexError) as exc:
                        failures[str(lead)] = f"Native snowfall acquisition unavailable: {exc}"
                        continue
                if row is None:
                    continue
                request = row[0]["request"]
                if (
                    request["source_id"] != model
                    or request["cycle"] != _iso(cycle)
                    or request["end_hour"] != lead
                ):
                    raise ValueError(
                        "Retained snowfall input differs from requested source/cycle/lead"
                    )
                record = _retain(directory, lead, row)
                inputs.append(record)
                dataset, crs, metadata = decode_snowfall_lead(row[1], model, cycle, lead)
                axes = dataset.x.values, dataset.y.values
                if native_axes is None:
                    native_axes = *axes, crs
                elif crs != native_axes[2] or any(
                    not np.array_equal(a, b) for a, b in zip(axes, native_axes[:2], strict=True)
                ):
                    raise ValueError("Native snowfall grid changed within source cycle")
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
            region_fields: list[list[xr.Dataset]] = [[] for _ in areas]
            for index, (start, end) in enumerate(intervals):
                event = {
                    **deepcopy(source),
                    "event_id": f"{model}:{index}",
                    "source_cycle": _iso(cycle),
                    "source_lead_hours": end,
                    "interval_start": _iso(cycle + timedelta(hours=start)),
                    "interval_end": _iso(cycle + timedelta(hours=end)),
                    "interval_closure": CLOSURE,
                    "temporal_semantics": "accumulation",
                    "missing_reasons": [],
                }
                needed = (start, end) if cumulative else (end,)
                absent = [lead for lead in needed if lead not in native]
                if absent:
                    event["missing_reasons"] = [
                        failures.get(str(lead), f"Missing native parent lead {lead}")
                        for lead in absent
                    ]
                    if native:
                        for fields, template in zip(
                            region_fields, next(iter(native.values()))[0], strict=True
                        ):
                            blank = xr.full_like(template.amount, np.nan)
                            fields.append(xr.Dataset({"amount": blank}))
                else:
                    for region_index, fields in enumerate(region_fields):
                        end_view, end_metadata, _ = native[end]
                        ds, metadata = _normalized_interval(
                            end_view[region_index],
                            end_metadata,
                            native[start][0][region_index] if cumulative else None,
                            native[start][1] if cumulative else None,
                        )
                        event.update(metadata)
                        fields.append(ds)
                    event["provenance"] = {
                        "parents": [
                            {**deepcopy(native[lead][2]), "native_event": deepcopy(native[lead][1])}
                            for lead in needed
                        ]
                    }
                events.append(event)
            manifest = {
                "model": model,
                "source_cycle": _iso(cycle),
                "source_metadata": deepcopy(source),
                "target_reference_time": _iso(target),
                "policy": deepcopy(POLICY),
                "created_at": _iso(clock.now()),
                "code_identity": _identity(),
                "inputs": inputs,
                "events": events,
                "request_failures": failures,
                "regions": [],
            }
            if not native:
                statuses[model]["missing_reasons"] = list(dict.fromkeys(failures.values())) or [
                    "No retained native snowfall intervals for the selected cycle"
                ]
            for index, fields in enumerate(region_fields):
                if not fields:
                    continue
                view = xr.concat(fields, dim="event").assign_coords(event=list(range(len(events))))
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
                    "raw_bytes": sum(r["raw_bytes"] for r in inputs),
                    "index_bytes": sum(r["index_bytes"] for r in inputs),
                    "prepared_bytes": sum(r["bytes"] for r in manifest["regions"]),
                }
            )
    finally:
        if owned is not None:
            owned.close()
    result = {
        **original,
        "snowfall_guidance": {
            "sources": descriptors,
            "source_status": statuses,
            "policy": deepcopy(POLICY),
            "source_preparation_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "downloaded_bytes": int(getattr(active_transport, "downloaded_bytes", 0)) - before,
            "created_at": _iso(clock.now()),
            "note": "Native snowfall acquired separately from surface decision; "
            "no retroactive availability claim",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def load_snowfall_guidance(
    descriptor: dict[str, Any], *, target_reference_time: np.datetime64
) -> list[SnowView]:
    """Validate retained native records and load shared interval views once."""
    if descriptor["policy"] != POLICY:
        raise ValueError("Snowfall has no approved active blend policy")
    result, seen = [], set()
    for source in descriptor["sources"]:
        model = source["model"]
        if model in seen:
            raise ValueError("Duplicate snowfall source descriptor")
        seen.add(model)
        directory = Path(source["directory"])
        manifest = json.loads(_checked_file(directory, "manifest.json", source["manifest_sha256"]))
        if (
            manifest["model"] != model
            or manifest["policy"] != POLICY
            or np.datetime64(manifest["target_reference_time"].removesuffix("Z"), "ns")
            != target_reference_time
        ):
            raise ValueError("Snowfall source/target/policy mismatch")
        _read_inputs(directory, manifest)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                ds = opened.load()
            if ds.sizes["event"] != len(manifest["events"]) or ds.amount.attrs.get("units") != UNIT:
                raise ValueError("Snowfall interval dimensions/units disagree")
            result.append(
                SnowView(
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
    result = prepare_snowfall_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(json.dumps(result["snowfall_guidance"], indent=2))


if __name__ == "__main__":
    main()
