"""Retain and normalize interval-aware HRRR/GFS QPF in existing prepared guidance.

This adapts the retained Phase 2 decoders and bucket differencing. It does not
blend, interpolate, infer probability, or reinterpret the instantaneous fields.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.catalog.sources import GfsSourceSettings, HrrrPhase2SourceSettings
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition
from mesoforge.guidance.precipitation import (
    GfsPrecipitationError,
    compute_bucket_start,
    compute_one_hour_qpf,
    is_bucket_reset_hour,
    select_bucket_record,
    validate_dual_parent_equivalence,
)
from mesoforge.guidance.sources.gfs_decoding import GfsDecodeError, decode_apcp_candidates
from mesoforge.guidance.sources.hrrr_phase2_decoding import (
    HrrrPhase2DecodeError,
    decode_selected_message,
)

QPF_VARIABLE = "liquid_equivalent_precipitation_amount_1h"
QpfPayloads = dict[int, bytes | tuple[bytes, ...]]


def required_qpf_leads(model: str, leads: tuple[int, ...]) -> tuple[int, ...]:
    """Include the first preceding bucket parent only when differencing needs it."""
    if not leads or model not in ("HRRR", "GFS"):
        raise ValueError("QPF requires HRRR/GFS and at least one source lead")
    parents = {lead - 1 for lead in leads if model == "GFS" and not is_bucket_reset_hour(lead)}
    return tuple(sorted(set(leads) | parents))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write(path: Path, payload: bytes) -> str:
    with path.open("xb") as stream:
        stream.write(payload)
    return hashlib.sha256(payload).hexdigest()


def retain_qpf_input(directory: Path, acquired: Phase2LeadAcquisition) -> dict[str, Any]:
    """Keep every selected bucket candidate and the original provider evidence."""
    model, lead = acquired.model.upper(), acquired.forecast_hour
    if (
        model not in ("HRRR", "GFS")
        or not acquired.selected_messages
        or any(row.canonical_variable_id != QPF_VARIABLE for row in acquired.selected_messages)
    ):
        raise ValueError("QPF retention requires HRRR/GFS QPF-only acquisitions")
    cycle = datetime.combine(acquired.cycle_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=acquired.cycle_hour
    )
    index_file = f"raw/{model}-f{lead:03d}-qpf.idx"
    row: dict[str, Any] = {
        "model": model,
        "canonical_variable_id": QPF_VARIABLE,
        "cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(cycle + timedelta(hours=lead)),
        "index_file": index_file,
        "index_sha256": _write(directory / index_file, acquired.index_payload),
        "index_bytes": len(acquired.index_payload),
        "source_grib_url": acquired.resolved_grib_url,
        "source_index_url": acquired.resolved_index_url,
        "endpoint": acquired.endpoint,
        "grib_retrieved_at": _iso(acquired.grib_completed_at),
        "index_retrieved_at": _iso(acquired.index_completed_at),
        "grib_available_at": _iso(acquired.grib_available_at),
        "index_available_at": _iso(acquired.index_available_at),
        "grib_last_modified": acquired.full_object_last_modified,
        "index_last_modified": acquired.index_last_modified,
        "etag": acquired.full_object_etag,
        "full_object_content_length": acquired.full_object_content_length,
        "messages": [],
    }
    for index, message in enumerate(acquired.selected_messages):
        filename = f"raw/{model}-f{lead:03d}-qpf-{index}.grib2"
        row["messages"].append(
            {
                "raw_file": filename,
                "raw_sha256": _write(directory / filename, message.payload),
                "raw_bytes": len(message.payload),
                "byte_start": message.byte_start,
                "byte_end": message.byte_end,
            }
        )
    return row


def read_qpf_inputs(
    directory: Path, rows: list[dict[str, Any]]
) -> tuple[dict[str, QpfPayloads], dict[str, bytes]]:
    """Read checked raw evidence for offline rebuilding, without touching providers."""
    result: dict[str, QpfPayloads] = {"HRRR": {}, "GFS": {}}
    retained: dict[str, bytes] = {}
    for row in rows:
        model, lead = row["model"], row["source_lead_hours"]
        if (
            model not in result
            or lead in result[model]
            or row["canonical_variable_id"] != QPF_VARIABLE
        ):
            raise ValueError("Invalid or duplicate retained QPF lead")
        cycle = datetime.fromisoformat(row["cycle"])
        if row["valid_time"] != _iso(cycle + timedelta(hours=lead)):
            raise ValueError("Retained QPF cycle/lead/valid time disagrees")

        def checked(entry: dict[str, Any], prefix: str, expected: str) -> bytes:
            filename: str = entry[f"{prefix}_file"]
            path = (directory / filename).resolve()
            if filename != expected or not path.is_relative_to(directory.resolve()):
                raise ValueError("Invalid retained QPF path")
            payload = path.read_bytes()
            if (
                len(payload) != entry[f"{prefix}_bytes"]
                or hashlib.sha256(payload).hexdigest() != entry[f"{prefix}_sha256"]
            ):
                raise ValueError(f"Retained QPF checksum or byte count differs: {filename}")
            if prefix == "raw" and entry["byte_end"] - entry["byte_start"] != len(payload):
                raise ValueError("Retained QPF byte range disagrees")
            retained[filename] = payload
            return payload

        checked(row, "index", f"raw/{model}-f{lead:03d}-qpf.idx")
        candidates = tuple(
            checked(message, "raw", f"raw/{model}-f{lead:03d}-qpf-{index}.grib2")
            for index, message in enumerate(row["messages"])
        )
        if not candidates or (model == "HRRR" and len(candidates) != 1):
            raise ValueError("Retained QPF has no candidates or ambiguous HRRR candidates")
        result[model][lead] = candidates[0] if len(candidates) == 1 else candidates
    return result, retained


def qpf_raw_bytes(rows: list[dict[str, Any]]) -> int:
    return sum(message["raw_bytes"] for row in rows for message in row["messages"])


def add_qpf_fields(
    dataset: xr.Dataset,
    *,
    model: str,
    settings: HrrrPhase2SourceSettings | GfsSourceSettings,
    cycle: datetime,
    payloads_by_lead: QpfPayloads,
    grid_reader: Callable[[xr.DataArray], tuple[pyproj.CRS, np.ndarray, np.ndarray, np.ndarray]],
) -> xr.Dataset:
    """Validate intervals and difference GFS native cells before any interpolation."""
    contract = next(
        item for item in settings.field_contracts if item.canonical_variable_id == QPF_VARIABLE
    )
    values = np.full(dataset.air_temperature_2m.shape, np.nan, dtype=np.float64)
    floors = np.zeros(values.shape, dtype=np.uint8)
    reasons = json.loads(dataset.attrs.get("field_missing_reasons_json", "{}"))
    missing: dict[str, list[str]] = {}
    metadata: dict[str, Any] = {}
    selection: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def validated_grid(field: xr.DataArray) -> None:
        nonlocal selection
        crs, x, y, order = grid_reader(field)
        xi, yi = (
            np.flatnonzero(np.isin(x, dataset.x.values)),
            np.flatnonzero(np.isin(y, dataset.y.values)),
        )
        if (
            crs != pyproj.CRS.from_wkt(dataset.attrs["crs_wkt2"])
            or not np.array_equal(x[xi], dataset.x.values)
            or not np.array_equal(y[yi], dataset.y.values)
        ):
            raise ValueError(f"{model}: QPF native cells/projection differ from temperature")
        selected = yi, xi, order
        if selection is not None and any(
            not np.array_equal(a, b) for a, b in zip(selection, selected, strict=True)
        ):
            raise ValueError(f"{model}: QPF grid changes between interval parents")
        selection = selected

    def subset(array: np.ndarray) -> np.ndarray:
        assert selection is not None
        yi, xi, order = selection
        return np.asarray(array[:, order][np.ix_(yi, xi)], dtype=np.float64)

    # Cache only cropped buckets; keep at most the current/previous parent grids.
    buckets: dict[int, tuple[np.ndarray, dict[str, Any]]] = {}

    def gfs_bucket(lead: int) -> tuple[np.ndarray, dict[str, Any]]:
        if lead in buckets:
            return buckets[lead]
        payload = payloads_by_lead.get(lead)
        if payload is None:
            raise ValueError(f"GFS required native bucket parent at source lead {lead} is missing")
        assert isinstance(settings, GfsSourceSettings)
        records = decode_apcp_candidates(
            payload,
            contract=contract,
            settings=settings,
            forecast_hour=lead,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
            validate_grid=validated_grid,
        )
        bucket = select_bucket_record(records, forecast_hour=lead)
        equivalent = all(
            validate_dual_parent_equivalence(bucket, item) for item in records if item is not bucket
        )
        if not equivalent:
            raise ValueError("GFS duplicate APCP candidates are not equivalent")
        data = subset(np.asarray(bucket.values_kg_m2).reshape(bucket.grid_shape))
        invalid = np.isfinite(data) & (data < 0)
        data[invalid] = np.nan
        info = {
            "source_lead_hours": lead,
            "interval_start": _iso(cycle + timedelta(hours=bucket.start_step)),
            "interval_end": _iso(cycle + timedelta(hours=bucket.end_step)),
            "model": model,
            "source_cycle": _iso(cycle),
            "native_unit_id": bucket.unit_id,
            "candidate_count": len(records),
            "invalid_negative_parent_cell_count": int(np.count_nonzero(invalid)),
            "duplicate_candidates_equivalent": equivalent if len(records) > 1 else None,
        }
        buckets[lead] = data, info
        return data, info

    for index, duration in enumerate(dataset.source_lead_time.values):
        lead = int(duration / np.timedelta64(1, "h"))
        info: dict[str, Any] = {
            "status": "unavailable",
            "start": _iso(cycle + timedelta(hours=lead - 1)),
            "end": _iso(cycle + timedelta(hours=lead)),
            "closure": "left_open_right_closed",
            "source_cycle": _iso(cycle),
            "source_lead_hours": lead,
            "parents": [],
        }
        metadata[str(lead)] = info
        try:
            if model == "HRRR":
                payload = payloads_by_lead.get(lead)
                if not isinstance(payload, bytes):
                    raise ValueError(
                        f"HRRR has no unique retained one-hour QPF message at source lead {lead}"
                    )
                assert isinstance(settings, HrrrPhase2SourceSettings)
                field = decode_selected_message(
                    payload,
                    contract=contract,
                    settings=settings,
                    forecast_hour=lead,
                    cycle_date=cycle.date(),
                    cycle_hour=cycle.hour,
                )
                validated_grid(field)
                one_hour = subset(field.values)
                info.update(
                    method="native_one_hour_accumulation",
                    parents=[
                        {
                            "source_lead_hours": lead,
                            "interval_start": info["start"],
                            "interval_end": info["end"],
                            "model": model,
                            "source_cycle": _iso(cycle),
                            "native_unit_id": str(field.attrs["GRIB_units"]),
                            "candidate_count": 1,
                            "duplicate_candidates_equivalent": None,
                        }
                    ],
                )
            else:
                current, current_info = gfs_bucket(lead)
                reset = is_bucket_reset_hour(lead)
                previous, previous_info = (None, None) if reset else gfs_bucket(lead - 1)
                info.update(
                    method="gfs_native_bucket_passthrough"
                    if reset
                    else "gfs_native_same_bucket_difference",
                    bucket_start_hour=compute_bucket_start(lead),
                    reset=reset,
                    parents=[current_info] + ([] if previous_info is None else [previous_info]),
                )
                one_hour = np.full(current.shape, np.nan)
                invalid_differences = 0
                for cell in np.ndindex(current.shape):
                    if not np.isfinite(current[cell]) or (
                        previous is not None and not np.isfinite(previous[cell])
                    ):
                        continue
                    try:
                        computed = compute_one_hour_qpf(
                            forecast_hour=lead,
                            bucket_value_current_kg_m2=float(current[cell]),
                            bucket_value_previous_kg_m2=None
                            if previous is None
                            else float(previous[cell]),
                        )
                    except GfsPrecipitationError:
                        invalid_differences += 1
                        continue
                    one_hour[cell] = computed.one_hour_qpf_kg_m2
                    floors[(index, *cell)] = computed.finite_precision_floor_applied
                info["invalid_nonmonotonic_difference_cell_count"] = invalid_differences
                buckets = {key: value for key, value in buckets.items() if key >= lead}
            invalid_hourly = np.isfinite(one_hour) & (one_hour < 0)
            one_hour[invalid_hourly | ~np.isfinite(one_hour)] = np.nan
            values[index] = one_hour
            info.update(
                status="available",
                finite_precision_floor_count=int(np.count_nonzero(floors[index])),
                missing_cell_count=int(np.count_nonzero(~np.isfinite(one_hour))),
                invalid_negative_hourly_cell_count=int(np.count_nonzero(invalid_hourly)),
            )
        except (GfsDecodeError, HrrrPhase2DecodeError, GfsPrecipitationError, ValueError) as exc:
            missing[str(lead)] = [str(exc)]
            info["reason"] = str(exc)
            floors[index] = 0
    reasons[QPF_VARIABLE] = missing
    bounds_name = f"{QPF_VARIABLE}_interval_bounds"
    ends = dataset.source_valid_time.values.astype("datetime64[ns]")
    dataset[QPF_VARIABLE] = (
        ("source_lead_time", "y", "x"),
        values,
        {
            "unit_id": "kg/m^2",
            "units": "kg/m^2",
            "temporal_semantics": "accumulation",
            "interval_bounds": bounds_name,
            "interval_closure": "left_open_right_closed",
        },
    )
    dataset[bounds_name] = (
        ("source_lead_time", "bounds"),
        np.stack((ends - np.timedelta64(1, "h"), ends), axis=1),
    )
    dataset["qpf_finite_precision_floor_applied"] = (
        ("source_lead_time", "y", "x"),
        floors,
        {
            "description": (
                "Existing Phase 2 GFS difference floor only for [-1e-6,0) kg/m^2; "
                "no trace-amount cleanup"
            ),
        },
    )
    dataset.attrs.update(
        qpf_fields=1,
        field_missing_reasons_json=json.dumps(reasons, sort_keys=True),
        qpf_metadata_json=json.dumps(metadata, sort_keys=True),
    )
    return dataset


def prepare_qpf_run(
    prepared_run: Path,
    output_directory: Path,
    *,
    from_raw: bool = False,
) -> dict[str, Any]:
    """Add or replay QPF through the existing selected-preparation directory interface."""
    # Local import avoids a cycle: core temperature preparation invokes add_qpf_fields.
    from mesoforge.application.prepared_temperature import (
        BoundedHttpTransport,
        _raw_byte_count,
        enrich_prepared_qpf,
        rebuild_temperature_guidance,
    )
    from mesoforge.catalog.configuration import Phase2Configuration, _lists_to_tuples
    from mesoforge.guidance.runtime import SystemClock, SystemSleeper

    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("Retain QPF model data outside the repository")
    if output_directory.exists():
        raise ValueError("QPF preparation requires a new output directory")
    original_payload = (prepared_run / "preparation.json").read_bytes()
    original = json.loads(original_payload)
    source = Path(original["directory"])
    if (source / "coverage.json").is_file():
        source = Path(json.loads((source / "coverage.json").read_bytes())["source_directory"])
    configuration = Phase2Configuration.model_validate(
        _lists_to_tuples(original["current_model_set"]["selection"]["source_configuration"])
    )
    source_manifest = json.loads((source / "manifest.json").read_bytes())
    if from_raw and not source_manifest.get("qpf_fields"):
        raise ValueError("Offline QPF rebuilding requires already retained precipitation inputs")
    output_directory.mkdir(parents=True)
    destination = output_directory / "source"
    if from_raw:
        manifest = rebuild_temperature_guidance(
            source, destination, configuration=configuration, clock=SystemClock()
        )
    else:
        transport = BoundedHttpTransport()
        try:
            manifest = enrich_prepared_qpf(
                source,
                destination,
                configuration=configuration,
                transport=transport,
                clock=SystemClock(),
                sleeper=SystemSleeper(),
            )
        finally:
            transport.close()
    report = {
        "directory": str(destination),
        "shadow_directories": original["shadow_directories"],
        "current_model_set": original["current_model_set"],
        "qpf_fields": True,
        "qpf_acquisition": manifest.get("qpf_acquisition"),
        "downloaded_bytes": manifest["downloaded_bytes"],
        "retained_raw_bytes": _raw_byte_count(manifest["inputs"])
        + qpf_raw_bytes(manifest["qpf_inputs"]),
        "qpf_raw_bytes": qpf_raw_bytes(manifest["qpf_inputs"]),
        "source_preparation": {
            "file": str((prepared_run / "preparation.json").resolve()),
            "sha256": hashlib.sha256(original_payload).hexdigest(),
        },
        "coverage": {
            "mode": "retained_native_spatial_footprint",
            "prepared_area": manifest["prepared_area"],
        },
    }
    _write(output_directory / "preparation.json", json.dumps(report, indent=2).encode())
    return report


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--from-raw", action="store_true", help="Rebuild retained QPF without provider access"
    )
    args = parser.parse_args(argv)
    report = prepare_qpf_run(args.prepared_run, args.output_dir, from_raw=args.from_raw)
    print(
        json.dumps(
            {
                **{key: value for key, value in report.items() if key != "current_model_set"},
                "selected_cycles": report["current_model_set"]["selection"].get(
                    "selected_cycles", {}
                ),
                "preparation_file": str((args.output_dir / "preparation.json").resolve()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
