"""Native-time point extraction for the provisional policy, without provider I/O.

This adapts prepared evidence to FieldBlendEngine; it contains no blend weights.
State interpolation and accumulation composition use their separate scientific
contracts. Neither operation turns missing data into a value.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import bilinear_interpolate, project_station_point
from mesoforge.alignment.state_interpolation import NativeStateSample, align_state_samples
from mesoforge.catalog.native_horizons import native_field_contract
from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.field_blend import BlendState
from mesoforge.guidance.precipitation import QpfIntervalAmount, compose_qpf_interval

QPF = "liquid_equivalent_precipitation_amount_1h"
STATE_UNITS = {
    "air_temperature_2m": "K",
    "dew_point_temperature_2m": "K",
    "eastward_wind_10m": "m/s",
    "northward_wind_10m": "m/s",
    "wind_gust_10m": "m/s",
    "cloud_area_fraction": "1",
}
NativeEntry = tuple[xr.Dataset, pyproj.CRS, dict[str, Any] | None]


def utc(value: np.datetime64) -> datetime:
    return datetime.fromisoformat(str(np.datetime_as_string(value, unit="s"))).replace(tzinfo=UTC)


def iso(value: np.datetime64) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def _point(
    entry: NativeEntry, field: str, index: int, latitude: float, longitude: float
) -> tuple[float, dict[str, Any]]:
    dataset, crs, _ = entry
    px, py = project_station_point(crs, latitude=latitude, longitude=longitude)
    if crs.is_geographic and dataset.x.values.min() >= 0 and px < 0:
        px += 360
    extracted = bilinear_interpolate(
        field=dataset[field].values[index],
        x=dataset.x.values,
        y=dataset.y.values,
        station_x=px,
        station_y=py,
    )
    cell, weights = extracted.cell, extracted.weights
    corners = dataset[field].values[index][np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])]
    if field == QPF and np.any(corners < 0):
        raise ValueError("Negative native precipitation corner")
    # Nested interpolation retains a constant bounded endpoint exactly (e.g. 100% cloud).
    tx = float(
        (px - dataset.x.values[cell.x0]) / (dataset.x.values[cell.x1] - dataset.x.values[cell.x0])
    )
    ty = float(
        (py - dataset.y.values[cell.y0]) / (dataset.y.values[cell.y1] - dataset.y.values[cell.y0])
    )
    bottom = float(corners[0, 0]) + tx * float(corners[0, 1] - corners[0, 0])
    top = float(corners[1, 0]) + tx * float(corners[1, 1] - corners[1, 0])
    value = bottom + ty * (top - bottom)
    return value, {
        "method": "native_grid_bilinear",
        "calculation": "nested_linear_interpolation.v1",
        "source_y": [cell.y0, cell.y1],
        "source_x": [cell.x0, cell.x1],
        "weights": [weights.w00, weights.w01, weights.w10, weights.w11],
    }


def _evidence(entry: NativeEntry, model: str, field: str, lead: int) -> dict[str, str]:
    dataset, _, manifest = entry
    if manifest is None:
        if dataset.attrs.get("data_kind") != "synthetic_demonstration":
            raise ValueError("Real native values require retained source evidence")
        return {"fixture": f"{model}/{field}/{lead}"}
    prepared = manifest["prepared_files"][model]["sha256"]
    rows = [
        row
        for row in manifest["inputs"]
        if row["model"] == model and row["source_lead_hours"] == lead
    ]
    if len(rows) != 1:
        raise ValueError("No unique native source evidence")
    row = rows[0]
    cycle = iso(dataset.forecast_reference_time.values[()])
    if row.get("cycle") != cycle or row.get("valid_time") != iso(
        dataset.forecast_reference_time.values[()] + np.timedelta64(lead, "h")
    ):
        raise ValueError("Native source evidence cycle/time disagrees")
    if model == "NBM" and field in ("eastward_wind_10m", "northward_wind_10m"):
        refs = {"prepared_sha256": prepared, "input": f"{model}/{lead}/{field}"}
        for name in ("wind_speed_10m", "wind_from_direction_10m"):
            messages = [
                item
                for item in row.get("extra_messages", [])
                if item["canonical_variable_id"] == name
            ]
            if len(messages) != 1:
                raise ValueError("No unique native NBM speed/direction evidence")
            refs[f"{name}_raw_sha256"] = messages[0]["raw_sha256"]
        policies = json.loads(dataset.attrs.get("wind_rotation_policy_json", "{}"))
        if policies.get(str(lead)) != "nbm_native_speed_direction_to_earth_relative_uv":
            raise ValueError("Native NBM wind conversion identity is missing")
        refs["derivation"] = policies[str(lead)]
        return refs
    messages = (
        [row]
        if field == "air_temperature_2m"
        else [
            extra
            for extra in row.get("extra_messages", [])
            if extra["canonical_variable_id"] == field
        ]
    )
    if len(messages) != 1:
        raise ValueError(f"No unique retained {field} source message")
    message = messages[0]
    return {
        "prepared_sha256": prepared,
        "raw_sha256": message["raw_sha256"],
        "input": f"{model}/{lead}/{field}",
    }


def extract_native_state(
    model: str,
    field: str,
    entry: NativeEntry | None,
    *,
    valid_time: np.datetime64,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    """One native state or adjacent documented endpoints, with exact source identity."""
    row: dict[str, Any] = {
        "value": None,
        "unit": STATE_UNITS[field],
        "valid_time": iso(valid_time),
        "temporal_semantics": "instantaneous",
        "missing_reasons": [],
    }
    try:
        if entry is None or field not in entry[0]:
            raise ValueError("Field not retained by this source")
        dataset = entry[0]
        cycle = dataset.forecast_reference_time.values.astype("datetime64[ns]")[()]
        row.update(
            source_cycle=iso(cycle),
            source_lead_hours=int((valid_time - cycle) / np.timedelta64(1, "h")),
        )
        contract = native_field_contract(model, field)
        plan = contract.plan(utc(cycle), utc(valid_time))
        if plan.status not in ("native", "bracketed"):
            raise ValueError(plan.status)
        if dataset[field].attrs.get("unit_id") != STATE_UNITS[field]:
            raise ValueError("Canonical field units disagree")
        attrs = dataset[field].attrs
        if attrs.get("temporal_semantics", "instantaneous") != "instantaneous":
            raise ValueError("Native state is not instantaneous")
        if field == "cloud_area_fraction" and (
            attrs.get("cloud_definition", "total_cloud_cover") != "total_cloud_cover"
            or attrs.get("vertical_extent", "entire_atmosphere") != "entire_atmosphere"
        ):
            raise ValueError("Cloud evidence is not compatible total-column cover")
        missing = json.loads(dataset.attrs.get("field_missing_reasons_json", "{}"))
        samples = []
        spatial = []
        for lead in plan.source_leads:
            if missing.get(field, {}).get(str(lead)):
                raise ValueError(str(missing[field][str(lead)]))
            native_time = cycle + np.timedelta64(lead, "h")
            indices = np.flatnonzero(dataset.source_valid_time.values == native_time)
            if len(indices) != 1:
                raise ValueError("Expected native endpoint is missing or ambiguous")
            value, point = _point(entry, field, int(indices[0]), latitude, longitude)
            refs = _evidence(entry, model, field, lead)
            spatial.append(point)
            if field == "wind_gust_10m":
                if plan.status != "native" or not math.isfinite(value) or not 0 <= value <= 150:
                    raise ValueError("Gust requires a finite bounded exact native state")
                row.update(
                    value=value,
                    provenance=refs,
                    spatial_extraction=point,
                    native_valid_times=[iso(native_time)],
                )
                return row
            samples.append(
                NativeStateSample(
                    source=model,
                    cycle=utc(cycle),
                    field=field,
                    unit=STATE_UNITS[field],
                    definition="total_cloud_cover" if field == "cloud_area_fraction" else field,
                    vertical_extent="entire_atmosphere"
                    if field == "cloud_area_fraction"
                    else ("2m" if field.endswith("2m") else "10m"),
                    spatial_support=f"native_bilinear:{latitude!r},{longitude!r}",
                    valid_time=utc(native_time),
                    value=value,
                    evidence_refs=refs,
                )
            )
        aligned = align_state_samples(
            samples,
            target_valid_time=utc(valid_time),
            native_step_hours=plan.native_step_hours or 1,
            **({"native_lattice_origin": samples[0].valid_time} if model == "NBM" else {}),
        )
        row.update(
            value=aligned.value,
            temporal_alignment=aligned.payload(),
            spatial_extraction=spatial,
            native_valid_times=[sample.payload()["valid_time"] for sample in aligned.endpoints],
        )
    except (KeyError, ValueError, IndexError, MesoForgeError) as exc:
        row["missing_reasons"].append(f"{model}: {field}: {exc}")
    return row


def extract_native_qpf(
    model: str,
    entry: NativeEntry | None,
    *,
    start: np.datetime64,
    end: np.datetime64,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    """Extract/sum a complete exact native event; never divide an accumulation."""
    row: dict[str, Any] = {
        "value": None,
        "unit": "kg/m^2",
        "temporal_semantics": "accumulation",
        "interval_start": iso(start),
        "interval_end": iso(end),
        "interval_closure": "left_open_right_closed",
        "missing_reasons": [],
    }
    try:
        if entry is None or QPF not in entry[0]:
            raise ValueError("No retained liquid precipitation amount")
        dataset, _, manifest = entry
        variable = dataset[QPF]
        if (
            variable.attrs.get("unit_id") != "kg/m^2"
            or variable.attrs.get("temporal_semantics") != "accumulation"
            or variable.attrs.get("interval_closure") != "left_open_right_closed"
        ):
            raise ValueError("QPF units/accumulation contract mismatch")
        cycle = dataset.forecast_reference_time.values.astype("datetime64[ns]")[()]
        row.update(
            source_cycle=iso(cycle), source_lead_hours=int((end - cycle) / np.timedelta64(1, "h"))
        )
        bounds = dataset[f"{QPF}_interval_bounds"].values
        missing = json.loads(dataset.attrs.get("field_missing_reasons_json", "{}"))
        native = []
        extractions = []
        for index, (left, right) in enumerate(bounds):
            if np.isnat(left) or np.isnat(right) or left < start or right > end or left >= right:
                continue
            lead = int((right - cycle) / np.timedelta64(1, "h"))
            if dataset.source_valid_time.values[index] != right:
                raise ValueError("Native QPF valid time differs from accumulation end")
            if missing.get(QPF, {}).get(str(lead)):
                raise ValueError(str(missing[QPF][str(lead)]))
            value, point = _point(entry, QPF, index, latitude, longitude)
            if manifest is None:
                if dataset.attrs.get("data_kind") != "synthetic_demonstration":
                    raise ValueError("Real QPF requires retained evidence")
                refs = {"fixture": f"{model}/qpf/{lead}"}
            else:
                metadata = json.loads(dataset.attrs.get("qpf_metadata_json", "{}")).get(
                    str(lead), {}
                )
                parents = metadata.get("parents", [])
                parent_leads = [parent["source_lead_hours"] for parent in parents]
                if (
                    not parents
                    or len(set(parent_leads)) != len(parent_leads)
                    or any(
                        parent["model"] != model or parent["source_cycle"] != iso(cycle)
                        for parent in parents
                    )
                ):
                    raise ValueError("Missing, duplicate or mixed-cycle native QPF parents")
                refs = {"prepared_sha256": manifest["prepared_files"][model]["sha256"]}
                for parent_lead in parent_leads:
                    proof = _evidence(entry, model, QPF, parent_lead)
                    refs[f"raw_{parent_lead}"] = proof["raw_sha256"]
            native.append(
                QpfIntervalAmount(
                    model,
                    utc(cycle),
                    utc(left),
                    utc(right),
                    "kg/m^2",
                    f"native_bilinear:{latitude!r},{longitude!r}",
                    value,
                    refs,
                )
            )
            extractions.append(point)
        composed = compose_qpf_interval(native, target_start=utc(start), target_end=utc(end))
        row.update(
            value=composed.value, normalization=composed.payload(), spatial_extraction=extractions
        )
    except (KeyError, ValueError, IndexError, MesoForgeError) as exc:
        row["missing_reasons"].append(f"{model}: exact QPF interval unavailable: {exc}")
    return row


def qpf_partition(
    datasets: dict[str, NativeEntry], *, reference: np.datetime64, duration_hours: int
) -> list[tuple[np.datetime64, np.datetime64]]:
    """Finest native contiguous events, favoring continuous deterministic streams.

    Sparse NBM one-hour events cannot fragment a valid three-hour deterministic
    accumulation. A gap stays an unavailable hourly event, never a split total.
    The partition is based on encoded bounds, not forecast rain amount.
    """
    endpoint = reference + np.timedelta64(duration_hours, "h")
    intervals: dict[str, list[tuple[np.datetime64, np.datetime64]]] = {}
    for model, (dataset, _, _) in datasets.items():
        bounds_name = f"{QPF}_interval_bounds"
        if bounds_name not in dataset:
            continue
        intervals[model] = [
            (left, right)
            for left, right in dataset[bounds_name].values
            if not np.isnat(left)
            and not np.isnat(right)
            and reference <= left < right <= endpoint
            and right - left <= np.timedelta64(6, "h")
        ]
    cursor, result = reference, []
    while cursor < endpoint:
        candidates = [
            right
            for model, rows in intervals.items()
            if model != "NBM"
            for left, right in rows
            if left == cursor
        ]
        if not candidates:
            candidates = [right for left, right in intervals.get("NBM", []) if left == cursor]
        end = min(candidates) if candidates else cursor + np.timedelta64(1, "h")
        if end - cursor > np.timedelta64(1, "h"):
            # Once hourly deterministic coverage ends, a native six-hour NBM
            # event can share an exact target with two global three-hour events.
            # This changes event resolution, never distributes its amount.
            coarse = [
                right
                for left, right in intervals.get("NBM", [])
                if left == cursor and right - cursor == np.timedelta64(6, "h")
            ]
            if coarse:
                end = coarse[0]
        result.append((cursor, end))
        cursor = end
    return result


def extract_native_inputs(
    datasets: dict[str, NativeEntry],
    *,
    reference: np.datetime64,
    horizon: int,
    latitude: float,
    longitude: float,
) -> tuple[BlendState, dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Adapt all retained sources to the single blend dispatch boundary."""
    valid = reference + np.timedelta64(horizon, "h")
    contributors: dict[str, dict[str, Any]] = {}
    values: dict[str, dict[str, float | None]] = {}
    contexts: dict[str, dict[str, Any]] = {}
    precipitation: dict[str, dict[str, Any]] = {}
    sources = []
    for model in ("HRRR", "RAP", "GFS", "IFS", "NBM"):
        entry = datasets.get(model)
        fields = {
            name: extract_native_state(
                model, name, entry, valid_time=valid, latitude=latitude, longitude=longitude
            )
            for name in STATE_UNITS
        }
        values[model] = {name: item["value"] for name, item in fields.items()}
        cycle = iso(entry[0].forecast_reference_time.values[()]) if entry else None
        lead = (
            int((valid - entry[0].forecast_reference_time.values[()]) / np.timedelta64(1, "h"))
            if entry
            else None
        )
        contexts[model] = {
            "cycle": cycle,
            "reference_time": iso(reference),
            "source_lead_hours": lead,
        }
        if entry is None:
            contexts[model]["missing_reasons"] = ["Prepared source unavailable"]
        fields[QPF] = precipitation[model] = extract_native_qpf(
            model,
            entry,
            start=valid - np.timedelta64(1, "h"),
            end=valid,
            latitude=latitude,
            longitude=longitude,
        )
        contributors[model] = {
            "model": model,
            "cycle": cycle,
            "source_lead_hours": lead,
            "role": "eligible_contributor",
            "fields": fields,
            "native_supported_fields": list(fields),
        }
        sources.append(
            {
                "model": model,
                "cycle": cycle,
                "source_lead_hours": lead,
                "weight": 0.0,
                "temperature": {"value": values[model]["air_temperature_2m"], "unit": "K"},
                "missing_reasons": fields["air_temperature_2m"]["missing_reasons"],
                "provenance": fields["air_temperature_2m"].get("temporal_alignment"),
            }
        )
    return (
        BlendState(
            horizon=horizon,
            contributors=values,
            precipitation=precipitation,
            source_context=contexts,
            precipitation_interval=(iso(valid - np.timedelta64(1, "h")), iso(valid)),
        ),
        contributors,
        sources,
    )
