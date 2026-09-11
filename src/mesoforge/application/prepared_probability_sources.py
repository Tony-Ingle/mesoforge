"""Attach bounded native probability shadows to an existing prepared forecast.

Source requests are an acquisition artifact, not geographic user configuration.
This uses the existing raw/NetCDF/preparation path; it neither changes the active
NBM hourly product nor derives probabilities from deterministic rainfall amounts.
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
from mesoforge.application.prepared_shadow import _axis_slice
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _code_identity,
    _hour,
    _iso,
    _write_bytes,
)
from mesoforge.application.probability_contributors import ProbabilityView
from mesoforge.application.spatial_coverage import native_bbox_bounds
from mesoforge.catalog.domains import BoundingBox
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.sources.probabilistic import PRODUCTS, acquire_product, decode_product


def _identity() -> dict[str, Any]:
    result = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for name in (
        "application/prepared_probability_sources.py",
        "application/probability_contributors.py",
        "guidance/sources/probabilistic.py",
    ):
        result["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    return result


def _areas(preparation: dict[str, Any]) -> tuple[BoundingBox, ...]:
    directory = Path(preparation["directory"])
    if (directory / "coverage.json").exists():
        coverage = json.loads((directory / "coverage.json").read_bytes())
        areas = [row["area"] for row in coverage["regions"]]
    else:
        areas = [json.loads((directory / "manifest.json").read_bytes())["prepared_area"]]
    return tuple(BoundingBox.model_validate(area) for area in areas)


def _request(request: dict[str, Any], target: datetime) -> dict[str, Any]:
    source = request["source_id"]
    if source not in PRODUCTS:
        raise ValueError(f"Unsupported native probability product: {source}")
    cycle = _hour(datetime.fromisoformat(request["cycle"]))
    start, end = request["start_hour"], request["end_hour"]
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
        or cycle > target
        or cycle + timedelta(hours=start) < target
        or cycle + timedelta(hours=end) > target + timedelta(hours=36)
    ):
        raise ValueError("Native probability interval must fit the retained 36-hour target window")
    return {"source_id": source, "cycle": _iso(cycle), "start_hour": start, "end_hour": end}


def retain_acquisition(
    request: dict[str, Any], acquired: Phase2LeadAcquisition
) -> tuple[dict[str, Any], bytes, bytes]:
    """Use the existing acquisition record without losing native object identities."""
    cycle = _hour(datetime.fromisoformat(request["cycle"]))
    if (
        len(acquired.selected_messages) != 1
        or acquired.cycle_date != cycle.date()
        or acquired.cycle_hour != cycle.hour
        or acquired.forecast_hour != request["end_hour"]
    ):
        raise ValueError("Native probability acquisition differs from its bounded request")
    message = acquired.selected_messages[0]
    evidence = {
        "request": request,
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
    return evidence, message.payload, acquired.index_payload


def _retain(
    directory: Path, index: int, row: tuple[dict[str, Any], bytes, bytes]
) -> dict[str, Any]:
    evidence, payload, inventory = row
    raw_file, index_file = f"raw/{index}.grib2", f"raw/{index}.index"
    return {
        **deepcopy(evidence),
        "raw_file": raw_file,
        "raw_sha256": _write_bytes(directory / raw_file, payload),
        "raw_bytes": len(payload),
        "index_file": index_file,
        "index_sha256": _write_bytes(directory / index_file, inventory),
        "index_bytes": len(inventory),
    }


def _read_inputs(
    directory: Path, manifest: dict[str, Any]
) -> list[tuple[dict[str, Any], bytes, bytes]]:
    result = []
    for row in manifest["inputs"]:
        payload = _checked_file(directory, row["raw_file"], row["raw_sha256"], row["raw_bytes"])
        inventory = _checked_file(
            directory, row["index_file"], row["index_sha256"], row["index_bytes"]
        )
        if row["byte_end"] - row["byte_start"] != len(payload):
            raise ValueError("Native probability retained byte range disagrees")
        result.append((row, payload, inventory))
    return result


def prepare_retained_sources(
    preparation: dict[str, Any],
    output_directory: Path,
    records: list[tuple[dict[str, Any], bytes, bytes]],
    *,
    clock: Clock | None = None,
    downloaded_bytes: int = 0,
) -> list[dict[str, Any]]:
    """Decode each native source once, then prepare shared coordinate-derived views.

    Also accepts previously acquired records for an offline import. The record's
    acquisition timestamps remain original; this preparation records a separate time.
    """
    target = _hour(
        datetime.fromisoformat(
            preparation["current_model_set"]["selection"]["target_reference_time"]
        )
    )
    areas = _areas(preparation)
    grouped: dict[str, list[tuple[dict[str, Any], bytes, bytes]]] = {}
    unique = set()
    for evidence, payload, inventory in records:
        request = _request(evidence["request"], target)
        key = tuple(request.values())
        if key in unique:
            raise ValueError("Duplicate native probability event request")
        unique.add(key)
        grouped.setdefault(request["source_id"], []).append((evidence, payload, inventory))
    descriptors = []
    for source_id, rows in grouped.items():
        rows.sort(key=lambda row: (row[0]["request"]["cycle"], row[0]["request"]["end_hour"]))
        directory = output_directory / source_id
        if directory.exists():
            raise ValueError("Native probability output already exists; preserve retained evidence")
        (directory / "raw").mkdir(parents=True)
        inputs = [_retain(directory, index, row) for index, row in enumerate(rows)]
        events, decoded = [], []
        for index, (record, (_, payload, _)) in enumerate(zip(inputs, rows, strict=True)):
            request = record["request"]
            dataset, crs, metadata = decode_product(
                payload,
                source_id=source_id,
                cycle=datetime.fromisoformat(request["cycle"]),
                start_hour=request["start_hour"],
                end_hour=request["end_hour"],
            )
            event = {
                **metadata,
                "event_id": f"{source_id}:{index}",
                "source_cycle": request["cycle"],
                "source_lead_hours": request["end_hour"],
                "provenance": deepcopy(record),
            }
            events.append(event)
            decoded.append((dataset, crs))
        first, crs = decoded[0]
        for other, other_crs in decoded[1:]:
            if other_crs != crs or not all(
                np.array_equal(first[axis], other[axis]) for axis in ("x", "y")
            ):
                raise ValueError("Native probability events cannot silently change source grids")
        manifest: dict[str, Any] = {
            "source_id": source_id,
            "source_metadata": deepcopy(PRODUCTS[source_id]),
            "role": "shadow",
            "active_weight": 0.0,
            "target_reference_time": _iso(target),
            "created_at": _iso((clock or SystemClock()).now()),
            "code_identity": _identity(),
            "inputs": inputs,
            "events": events,
            "regions": [],
        }
        for area_index, area in enumerate(areas):
            xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
            ys, xs = (
                _axis_slice(first.y.values, ymin, ymax),
                _axis_slice(first.x.values, xmin, xmax),
            )
            view = xr.concat(
                [dataset.isel(y=ys, x=xs) for dataset, _ in decoded], dim="event"
            ).assign_coords(event=list(range(len(events))))
            view.attrs = {"crs_wkt2": crs.to_wkt(), "source_id": source_id}
            view["probability"].attrs = {"units": "1", "temporal_semantics": "probability"}
            view["native_probability"].attrs = {"units": "percent"}
            filename = f"regions/{area_index}.nc"
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                view.to_netcdf(output, engine="h5netcdf", format="NETCDF4")
            manifest["regions"].append(
                {
                    "area": area.model_dump(),
                    "file": filename,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
            )
        digest = _write_bytes(
            directory / "manifest.json", json.dumps(manifest, indent=2, allow_nan=False).encode()
        )
        descriptors.append(
            {
                "source_id": source_id,
                "status": "prepared",
                "directory": str(directory.resolve()),
                "manifest_sha256": digest,
                "role": "shadow",
                "active_weight": 0.0,
                "retained_raw_bytes": sum(row["raw_bytes"] for row in inputs),
                "retained_index_bytes": sum(row["index_bytes"] for row in inputs),
                "prepared_bytes": sum(row["bytes"] for row in manifest["regions"]),
            }
        )
    return descriptors


def load_probability_sources(
    descriptors: list[dict[str, Any]], *, target_reference_time: np.datetime64
) -> list[ProbabilityView]:
    """Validate/load each prepared view once before any grid or HTTP calculation."""
    views = []
    sources = set()
    for descriptor in descriptors:
        source = descriptor["source_id"]
        if source in sources or descriptor["role"] != "shadow" or descriptor["active_weight"] != 0:
            raise ValueError("Probability attachments require unique zero-weight shadow sources")
        sources.add(source)
        directory = Path(descriptor["directory"])
        manifest = json.loads(
            _checked_file(directory, "manifest.json", descriptor["manifest_sha256"])
        )
        target = np.datetime64(manifest["target_reference_time"].removesuffix("Z"), "ns")
        if target != target_reference_time or manifest["source_id"] != source:
            raise ValueError("Probability shadow target/source differs from the prepared forecast")
        _read_inputs(directory, manifest)
        for region in manifest["regions"]:
            _checked_file(directory, region["file"], region["sha256"], region["bytes"])
            with xr.open_dataset(directory / region["file"], engine="h5netcdf") as opened:
                dataset = opened.load()
            native = dataset.native_probability.values
            valid = np.isfinite(native) & (native >= 0) & (native <= 100)
            expected = np.full(native.shape, np.nan)
            expected[valid] = native[valid] / 100.0
            if (
                dataset.probability.dims != ("event", "y", "x")
                or dataset.sizes["event"] != len(manifest["events"])
                or dataset.probability.attrs.get("units") != "1"
                or dataset.native_probability.attrs.get("units") != "percent"
                or not np.array_equal(
                    dataset.probability.values,
                    expected,
                    equal_nan=True,
                )
            ):
                raise ValueError("Native probability data differ from retained percentages/events")
            crs = pyproj.CRS.from_wkt(dataset.attrs["crs_wkt2"])
            views.append(
                ProbabilityView(
                    dataset=dataset,
                    crs=crs,
                    manifest={
                        **manifest,
                        "manifest_sha256": descriptor["manifest_sha256"],
                        "prepared_file": region,
                    },
                )
            )
    return views


def prepare_probability_sources(
    prepared_run: Path,
    output_directory: Path,
    *,
    requests: list[dict[str, Any]] | None = None,
    from_raw: bool = False,
    retained_records: list[tuple[dict[str, Any], bytes, bytes]] | None = None,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    """Write a new prepared-run wrapper; active inputs and older issuances stay untouched."""
    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("Retained probability inputs must remain outside Git")
    if output_directory.exists():
        raise ValueError("Use a new probability preparation directory")
    if sum((requests is not None, from_raw, retained_records is not None)) != 1:
        raise ValueError("Choose bounded native requests, retained inputs, or raw replay")
    original = json.loads((prepared_run / "preparation.json").read_bytes())
    if not original.get("pop_guidance") or not original["current_model_set"]["selection"].get(
        "surface_fields"
    ):
        raise ValueError("Native probability shadows require the existing surface/NBM PoP run")
    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    records = []
    downloaded = 0
    if from_raw:
        for descriptor in original.get("probability_sources", []):
            directory = Path(descriptor["directory"])
            manifest = json.loads(
                _checked_file(directory, "manifest.json", descriptor["manifest_sha256"])
            )
            records.extend(_read_inputs(directory, manifest))
    elif retained_records is not None:
        records = retained_records
    else:
        target = datetime.fromisoformat(
            original["current_model_set"]["selection"]["target_reference_time"]
        )
        validated = [_request(request, target) for request in requests or []]
        owned = BoundedHttpTransport() if transport is None else None
        active_transport = transport or owned
        assert active_transport is not None
        before = int(getattr(active_transport, "downloaded_bytes", 0))
        try:
            for request in validated:
                acquired = acquire_product(
                    source_id=request["source_id"],
                    cycle=datetime.fromisoformat(request["cycle"]),
                    start_hour=request["start_hour"],
                    end_hour=request["end_hour"],
                    transport=active_transport,
                    clock=clock,
                    sleeper=sleeper,
                )
                records.append(retain_acquisition(request, acquired))
            downloaded = int(getattr(active_transport, "downloaded_bytes", 0)) - before
        finally:
            if owned is not None:
                owned.close()
    if not records:
        raise ValueError("No retained native probability source events to prepare")
    descriptors = prepare_retained_sources(
        original, output_directory / "probabilities", records, clock=clock
    )
    result = {
        **original,
        "probability_sources": descriptors,
        "probability_source_run": {
            "created_at": _iso(clock.now()),
            "source_preparation_sha256": hashlib.sha256(
                (prepared_run / "preparation.json").read_bytes()
            ).hexdigest(),
            "downloaded_bytes": downloaded,
            "native_events": len(records),
            "note": "Bounded shadow experiment; native periods are not delivered hourly PoP",
        },
    }
    _write_bytes(
        output_directory / "preparation.json",
        json.dumps(result, indent=2, allow_nan=False).encode(),
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument(
        "--requests", type=Path, help="Bounded native source acquisition requests JSON"
    )
    choice.add_argument("--from-raw", action="store_true")
    args = parser.parse_args()
    result = prepare_probability_sources(
        args.prepared_run,
        args.output_dir,
        requests=json.loads(args.requests.read_text()) if args.requests else None,
        from_raw=args.from_raw,
    )
    print(
        json.dumps(
            {
                "probability_sources": result["probability_sources"],
                "run": result["probability_source_run"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
