"""Prepare sparse native five-day fields once, retaining exact evidence and intervals.

Provider discovery and conditional range acquisition remain the existing shared
boundaries. Native fields are decoded/cropped one lead at a time; this module does
not regrid, time-interpolate, blend or divide a precipitation accumulation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.application.native_discovery import (
    NATIVE_PREPARATION_POLICY,
    load_native_selection,
    native_window_usable,
)
from mesoforge.application.prepared_shadow import _axis_slice
from mesoforge.application.prepared_temperature import (
    BoundedHttpTransport,
    _code_identity,
    _iso,
    _write_prepared_file,
)
from mesoforge.application.spatial_coverage import native_bbox_bounds, plan_regions
from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.catalog.domains import BoundingBox
from mesoforge.common.horizon import ForecastHorizon
from mesoforge.guidance.http_fetch import fetch_with_range
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.normalization import rotate_wind_to_earth_relative
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.guidance.selected_objects import SelectedObjectTransport, selected_messages
from mesoforge.guidance.sources.cloud import decode_cloud_lead
from mesoforge.guidance.sources.ifs import IFS_RETRY_POLICY
from mesoforge.guidance.sources.native_fields import (
    CLOUD,
    POP,
    QPF,
    TEMPERATURE,
    decode_interval,
    decode_state,
)
from mesoforge.guidance.sources.nbm import convert_speed_direction_to_components
from mesoforge.guidance.sources.probabilistic import normalized_native_grid
from mesoforge.guidance.sources.rap import RAP_REQUEST_INTERVAL_SECONDS

_ROOT = Path(__file__).resolve().parents[3]
_UNITS = {
    TEMPERATURE: "K",
    "dew_point_temperature_2m": "K",
    "eastward_wind_10m": "m/s",
    "northward_wind_10m": "m/s",
    "wind_gust_10m": "m/s",
    CLOUD: "1",
    QPF: "kg/m^2",
    POP: "1",
}


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _crop(
    field: xr.DataArray, area: BoundingBox
) -> tuple[np.ndarray, np.ndarray, np.ndarray, pyproj.CRS]:
    normalized, x, y, crs = normalized_native_grid(field)
    xmin, xmax, ymin, ymax = native_bbox_bounds(area, crs)
    xs, ys = _axis_slice(x, xmin, xmax), _axis_slice(y, ymin, ymax)
    # The crop must own its bytes: a float64 slice would otherwise retain the
    # full provider grid through NumPy's base reference after this lead ends.
    return np.array(normalized.values[ys, xs], dtype=np.float64, copy=True), x[xs], y[ys], crs


def normalize_native_frame(
    payloads: dict[str, bytes],
    *,
    model: str,
    cycle: datetime,
    lead: int,
    target: datetime,
    configuration: Phase2Configuration,
    area: BoundingBox,
) -> tuple[xr.Dataset, dict[str, Any]]:
    """Decode one endpoint and preserve native-grid, field and interval semantics."""
    temperature = decode_state(
        payloads[TEMPERATURE], model, TEMPERATURE, cycle, lead, configuration
    )
    data, x, y, crs = _crop(temperature, area)
    fields = {name: np.full(data.shape, np.nan) for name in _UNITS}
    fields[TEMPERATURE] = data
    missing: dict[str, list[str]] = {}
    wind: dict[str, tuple[np.ndarray, xr.DataArray]] = {}
    qpf_info: dict[str, Any] = {
        "status": "unavailable",
        "source_cycle": _iso(cycle),
        "source_lead_hours": lead,
        "start": _iso(cycle + timedelta(hours=max(0, lead - 1))),
        "end": _iso(cycle + timedelta(hours=lead)),
        "closure": "left_open_right_closed",
        "parents": [],
    }

    def compatible(field: xr.DataArray) -> np.ndarray:
        values, fx, fy, fc = _crop(field, area)
        if fc != crs or not np.array_equal(fx, x) or not np.array_equal(fy, y):
            raise ValueError(f"{model}: native field cells/projection differ from temperature")
        return values

    for variable, payload in payloads.items():
        if variable == TEMPERATURE:
            continue
        if variable == QPF + "_equivalent_parent":
            continue
        if variable == CLOUD:
            cloud, cloud_crs, _ = decode_cloud_lead(payload, model, cycle, lead)
            xi, yi = (
                np.flatnonzero(np.isin(cloud.x.values, x)),
                np.flatnonzero(np.isin(cloud.y.values, y)),
            )
            if (
                cloud_crs != crs
                or not np.array_equal(cloud.x.values[xi], x)
                or not np.array_equal(cloud.y.values[yi], y)
            ):
                raise ValueError("Native cloud cells/projection differ from temperature")
            fields[CLOUD] = np.asarray(cloud.cloud_cover.values[np.ix_(yi, xi)]) / 100.0
        elif variable == QPF:
            field, start, end, factor = decode_interval(payload, model, cycle, lead)
            values = compatible(field) * factor
            duplicate = payloads.get(QPF + "_equivalent_parent")
            if duplicate is not None:
                second, second_start, second_end, second_factor = decode_interval(
                    duplicate, model, cycle, lead
                )
                if (start, end) != (second_start, second_end) or not np.array_equal(
                    values, compatible(second) * second_factor, equal_nan=True
                ):
                    raise ValueError("GFS duplicate native accumulation candidates disagree")
            fields[QPF] = np.where(np.isfinite(values) & (values >= 0), values, np.nan)
            qpf_info.update(
                status="available",
                start=_iso(cycle + timedelta(hours=start)),
                end=_iso(cycle + timedelta(hours=end)),
                method="native_accumulation",
                native_start_step=start,
                native_end_step=end,
                parents=[
                    {
                        "model": model,
                        "source_cycle": _iso(cycle),
                        "source_lead_hours": lead,
                        "interval_start": _iso(cycle + timedelta(hours=start)),
                        "interval_end": _iso(cycle + timedelta(hours=end)),
                        "native_unit_id": field.attrs["GRIB_units"],
                    }
                ],
            )
        elif variable == POP:
            field = decode_state(payload, model, variable, cycle, lead, configuration)
            values = compatible(field)
            fields[POP] = np.where(
                np.isfinite(values) & (values >= 0) & (values <= 100), values / 100.0, np.nan
            )
        else:
            field = decode_state(payload, model, variable, cycle, lead, configuration)
            values = compatible(field)
            if variable in {
                "eastward_wind_10m",
                "northward_wind_10m",
                "wind_speed_10m",
                "wind_from_direction_10m",
            }:
                wind[variable] = values, field
            elif variable in fields:
                fields[variable] = values
    u, v = "eastward_wind_10m", "northward_wind_10m"
    if model == "NBM" and {"wind_speed_10m", "wind_from_direction_10m"}.issubset(wind):
        speed, direction = wind["wind_speed_10m"][0], wind["wind_from_direction_10m"][0]
        for cell in np.ndindex(speed.shape):
            if np.isfinite(speed[cell]) and (speed[cell] == 0 or np.isfinite(direction[cell])):
                try:
                    fields[u][cell], fields[v][cell] = convert_speed_direction_to_components(
                        speed_m_s=float(speed[cell]), direction_degrees=float(direction[cell])
                    )
                except ValueError:
                    pass
        wind_policy = "nbm_native_speed_direction_to_earth_relative_uv"
    elif {u, v}.issubset(wind):
        flags = [wind[name][1].attrs.get("GRIB_uvRelativeToGrid") for name in (u, v)]
        if any(flag not in (0, 1) for flag in flags):
            raise ValueError("Native wind pair lacks vector reference metadata")
        rotated = rotate_wind_to_earth_relative(
            u_grid=wind[u][0],
            v_grid=wind[v][0],
            x=x,
            y=y,
            crs=crs,
            u_relative_to_grid=bool(flags[0]),
            v_relative_to_grid=bool(flags[1]),
        )
        fields[u], fields[v], wind_policy = rotated.eastward, rotated.northward, rotated.policy
    else:
        wind_policy = "unavailable_native_wind_pair"
    for variable, values in fields.items():
        if not np.any(np.isfinite(values)):
            missing[variable] = ["Native field unavailable at this source lead"]
    source = np.datetime64(cycle.replace(tzinfo=None), "ns")
    duration = np.array([lead], dtype="timedelta64[h]").astype("timedelta64[ns]")
    bounds = np.array(
        [
            [
                datetime.fromisoformat(qpf_info["start"]).replace(tzinfo=None),
                datetime.fromisoformat(qpf_info["end"]).replace(tzinfo=None),
            ]
        ],
        dtype="datetime64[ns]",
    )
    if qpf_info["status"] != "available":
        # An absent field does not prove an event window. In particular, sparse
        # state endpoints must not manufacture hourly QPF partition boundaries.
        bounds[:] = np.datetime64("NaT", "ns")
    dataset = xr.Dataset(
        {
            variable: (
                ("source_lead_time", "y", "x"),
                values[np.newaxis, ...],
                {
                    "unit_id": _UNITS[variable],
                    "units": _UNITS[variable],
                    "temporal_semantics": "accumulation" if variable == QPF else "instantaneous",
                },
            )
            for variable, values in fields.items()
        },
        coords={
            "x": x,
            "y": y,
            "forecast_reference_time": source,
            "source_lead_time": duration,
            "source_valid_time": ("source_lead_time", source + duration),
        },
        attrs={
            "model": model,
            "data_kind": "real_prepared_guidance",
            "target_reference_time": _iso(target),
            "crs_wkt2": crs.to_wkt(),
            "prepared_area_json": area.model_dump_json(),
            "native_preparation_policy": NATIVE_PREPARATION_POLICY,
            "surface_fields": 1,
            "wind_reference": "earth_relative",
            "field_missing_reasons_json": json.dumps(
                {key: {str(lead): value} for key, value in missing.items()}, sort_keys=True
            ),
            "wind_rotation_policy_json": json.dumps({str(lead): wind_policy}, sort_keys=True),
        },
    )
    bounds_name = QPF + "_interval_bounds"
    dataset[bounds_name] = (("source_lead_time", "bounds"), bounds)
    dataset[QPF].attrs.update(
        interval_bounds=bounds_name, interval_closure="left_open_right_closed"
    )
    dataset[POP].attrs.update(
        temporal_semantics="probability",
        interval_closure="left_open_right_closed",
        interval_bounds=POP + "_interval_bounds",
        probability_threshold_kg_m2=0.254,
        probability_comparison="gt",
        probability_type=1,
        event_definition=(
            "liquid-equivalent precipitation greater than 0.254 mm during the preceding hour"
        ),
    )
    pop_bounds = np.array([[source + duration[0] - np.timedelta64(1, "h"), source + duration[0]]])
    if POP not in payloads:
        pop_bounds[:] = np.datetime64("NaT", "ns")
    dataset[POP + "_interval_bounds"] = (("source_lead_time", "bounds"), pop_bounds)
    return dataset, qpf_info


def difference_native_accumulations(dataset: xr.Dataset, metadata: dict[str, Any]) -> xr.Dataset:
    """Difference nested native events without splitting or smoothing their amounts.

    Only same-cycle/grid parents with the same start can be differenced. A
    missing/negative difference remains missing. Reset and independently defined
    rolling events retain their actual bounds. Each output names both parents.
    """
    result = dataset.copy(deep=True)
    previous: tuple[np.ndarray, dict[str, Any]] | None = None
    bounds = result[QPF + "_interval_bounds"].values
    for index, duration in enumerate(result.source_lead_time.values):
        lead = int(duration / np.timedelta64(1, "h"))
        info = metadata[str(lead)]
        if info["status"] != "available":
            previous = None
            continue
        native = np.array(result[QPF].values[index], copy=True)
        original = json.loads(json.dumps(info))
        if previous is not None:
            prior, prior_info = previous
            if info["start"] == prior_info["start"] and prior_info["end"] < info["end"]:
                value = native - prior
                result[QPF].values[index] = np.where(
                    np.isfinite(value) & (value >= 0), value, np.nan
                )
                info.update(
                    start=prior_info["end"],
                    method="native_same_start_accumulation_difference",
                    parents=[*info["parents"], *prior_info["parents"]],
                )
                bounds[index, 0] = np.datetime64(
                    datetime.fromisoformat(info["start"]).replace(tzinfo=None), "ns"
                )
        previous = native, original
    result.attrs["qpf_metadata_json"] = json.dumps(metadata, sort_keys=True)
    result.attrs["qpf_fields"] = 1
    return result


def prepare_native_selected(
    locations: list[Any],
    selection_path: Path,
    output_directory: Path,
    *,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    """Acquire selected source messages once and materialize each configured native crop."""
    from mesoforge.application.batch_forecast import _coordinates
    from mesoforge.application.spatial_preparation import source_identity

    clock, sleeper = clock or SystemClock(), sleeper or SystemSleeper()
    selection, configuration, probes = load_native_selection(selection_path, clock=clock)
    directory = output_directory.resolve()
    if directory.is_relative_to(_ROOT):
        raise ValueError("Keep retained native guidance outside Git")
    directory.mkdir(parents=True, exist_ok=False)
    control = directory / "control"
    source = control / "source"
    source.mkdir(parents=True)
    areas = plan_regions([_coordinates(location) for location in locations])
    if not areas:
        raise ValueError("Native preparation requires configured spatial coverage")
    destinations = [source, *(control / f"region-{index}" for index in range(1, len(areas)))]
    for destination in destinations[1:]:
        destination.mkdir()
    budget = sum(
        probe["index"]["content_bytes"]
        + sum(
            {
                (message["byte_start"], message["byte_end_exclusive"]): message["content_bytes"]
                for message in selected_messages(probe)
            }.values()
        )
        for probe in probes
    )
    owned = BoundedHttpTransport(body_budget=budget) if transport is None else None
    transport = transport or owned
    assert transport is not None
    pinned = SelectedObjectTransport(
        transport,
        probes,
        decision_time=datetime.fromisoformat(selection["decision_time"]),
        clock=clock,
    )
    inputs: list[dict[str, Any]] = []
    prepared_files: list[dict[str, Any]] = [{} for _ in areas]
    frames: list[dict[str, list[xr.Dataset]]] = [{} for _ in areas]
    qpf: list[dict[str, dict[str, Any]]] = [{} for _ in areas]
    try:
        for probe in sorted(probes, key=lambda item: (item["model"], item["source_lead_hours"])):
            model, lead = probe["model"], probe["source_lead_hours"]
            cycle = datetime.fromisoformat(probe["cycle"])
            # Preserve the existing RAP/IFS half-second request pacing; the
            # native path must not bypass it by fetching pinned ranges directly.
            request_interval = RAP_REQUEST_INTERVAL_SECONDS if model in {"RAP", "IFS"} else 0
            if request_interval:
                sleeper.sleep(request_interval)
            index_response = pinned.get(probe["index"]["url"])
            if request_interval:
                sleeper.sleep(request_interval)
            pinned.head(probe["grib"]["url"])
            index_file = f"raw/{model}-f{lead:03d}.idx"
            index_digest = _write(source / index_file, index_response.content)
            messages: list[dict[str, Any]] = []
            payloads: dict[str, bytes] = {}
            acquired_ranges: set[tuple[int, int]] = set()
            for message in selected_messages(probe):
                variable = message["canonical_variable_id"]
                start, end = message["byte_start"], message["byte_end_exclusive"]
                if request_interval and (start, end) not in acquired_ranges:
                    sleeper.sleep(request_interval)
                fetched = fetch_with_range(
                    pinned,
                    clock,
                    sleeper,
                    endpoint=probe["selected_endpoint"],
                    url=probe["grib"]["url"],
                    range_header=f"bytes={start}-{end - 1}",
                    byte_start=start,
                    byte_end=end,
                    retry_policy=IFS_RETRY_POLICY,
                    cycle_deadline=clock.now() - timedelta(microseconds=1),
                    expected_length=end - start,
                    full_object_length=probe["grib"]["content_length"],
                )
                acquired_ranges.add((start, end))
                filename = f"raw/{model}-f{lead:03d}-{variable}.grib2"
                digest = _write(source / filename, fetched.payload)
                payloads[variable] = fetched.payload
                messages.append(
                    {
                        "canonical_variable_id": variable,
                        "raw_file": filename,
                        "raw_sha256": digest,
                        "raw_bytes": len(fetched.payload),
                        "byte_start": start,
                        "byte_end": end,
                    }
                )
            temperature = next(
                message for message in messages if message["canonical_variable_id"] == TEMPERATURE
            )
            inputs.append(
                {
                    **temperature,
                    "model": model,
                    "cycle": _iso(cycle),
                    "source_lead_hours": lead,
                    "valid_time": probe["valid_time"],
                    "index_file": index_file,
                    "index_sha256": index_digest,
                    "index_bytes": len(index_response.content),
                    "source_grib_url": probe["grib"]["url"],
                    "source_index_url": probe["index"]["url"],
                    "endpoint": probe["selected_endpoint"],
                    "grib_retrieved_at": _iso(clock.now()),
                    "index_retrieved_at": _iso(clock.now()),
                    "grib_available_at": probe["grib"]["available_at"],
                    "index_available_at": probe["index"]["available_at"],
                    "grib_last_modified": probe["grib"]["last_modified"],
                    "index_last_modified": probe["index"]["last_modified"],
                    "etag": probe["grib"]["etag"],
                    "extra_messages": [
                        message for message in messages if message is not temperature
                    ],
                }
            )
            for region, area in enumerate(areas):
                frame, info = normalize_native_frame(
                    payloads,
                    model=model,
                    cycle=cycle,
                    lead=lead,
                    target=datetime.fromisoformat(selection["target_reference_time"]),
                    configuration=configuration,
                    area=area,
                )
                frames[region].setdefault(model, []).append(frame)
                qpf[region].setdefault(model, {})[str(lead)] = info
            pinned.release_completed_object(probe["grib"]["url"])
            payloads.clear()
            del fetched
            # Full provider grids and message payloads are released after this lead;
            # only small native crops remain in memory.
        pinned.assert_complete()
        if not native_window_usable(
            datetime.fromisoformat(selection["target_reference_time"]),
            len(selection["horizon_hours"]),
            clock.now(),
        ):
            raise ValueError(
                "Native preparation no longer has a complete prospective reference view"
            )
        evidence = {
            "selection_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
            "selection": selection,
            "object_validation": pinned.validations,
            "preparation_code_sha256": {
                "application/native_preparation.py": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest()
            },
        }
        coverage_rows = []
        for region, destination in enumerate(destinations):
            for model, parts in frames[region].items():
                dataset = xr.concat(
                    parts,
                    dim="source_lead_time",
                    data_vars="all",
                    coords="minimal",
                    compat="equals",
                )
                dataset.attrs["field_missing_reasons_json"] = json.dumps(
                    {
                        field: {
                            lead: reasons
                            for part in parts
                            for lead, reasons in json.loads(
                                part.attrs["field_missing_reasons_json"]
                            )
                            .get(field, {})
                            .items()
                        }
                        for field in _UNITS
                    },
                    sort_keys=True,
                )
                dataset.attrs["wind_rotation_policy_json"] = json.dumps(
                    {
                        lead: policy
                        for part in parts
                        for lead, policy in json.loads(
                            part.attrs["wind_rotation_policy_json"]
                        ).items()
                    },
                    sort_keys=True,
                )
                dataset = difference_native_accumulations(dataset, qpf[region][model])
                prepared_files[region][model] = _write_prepared_file(destination, model, dataset)
            manifest = {
                "data_kind": "real_prepared_guidance",
                "forecast_horizon": ForecastHorizon(120).payload(),
                "native_preparation_policy": NATIVE_PREPARATION_POLICY,
                "target_reference_time": selection["target_reference_time"],
                "target_horizon_hours": selection["horizon_hours"],
                "created_at": _iso(clock.now()),
                "inputs": inputs,
                "prepared_files": prepared_files[region],
                "source_directory": str(source),
                "prepared_area": areas[region].model_dump(),
                "surface_fields": True,
                "qpf_fields": True,
                "surface_blend_configuration": configuration.blend_configuration.model_dump(
                    mode="json"
                ),
                "current_model_set": evidence,
                "configuration_sha256": hashlib.sha256(
                    configuration.model_dump_json().encode()
                ).hexdigest(),
                "preparation_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "code_identity": _code_identity(),
                "availability_note": (
                    "Exact selected provider objects proved available by the retained discovery "
                    "cutoff; retrieval time remains separate."
                ),
                "native_retention_limitations": {
                    "NBM": (
                        "Retain state-file endpoints and compatible complete six-hour QPF events "
                        "after the hourly state horizon. Later one-hour-QPF-only files can exist "
                        "but are not acquired by this bounded preparation policy; no six-hour "
                        "amount is split into pseudo-hourly amounts."
                    )
                },
            }
            payload = json.dumps(manifest, indent=2, allow_nan=False).encode()
            digest = _write(destination / "manifest.json", payload)
            coverage_rows.append(
                {
                    "area": areas[region].model_dump(),
                    "directory": str(destination),
                    "status": "prepared_native_120h",
                    "manifest_sha256": digest,
                    "prepared_bytes": sum(
                        (destination / f"{model}.nc").stat().st_size
                        for model in prepared_files[region]
                    ),
                }
            )
        coverage = {
            "source_directory": str(source),
            "source_identity": source_identity(manifest),
            "regions": coverage_rows,
            "failures": [],
            "downloaded_bytes": 0,
        }
        _write(control / "coverage.json", json.dumps(coverage, indent=2).encode())
        report = {
            "directory": str(control),
            "forecast_horizon": ForecastHorizon(120).payload(),
            "shadow_directories": {},
            "shadows": {},
            "current_model_set": evidence,
            "coverage": coverage,
            "downloaded_bytes": pinned.downloaded_bytes
            or sum(path.stat().st_size for path in (source / "raw").iterdir()),
            "retained_raw_bytes": sum(
                path.stat().st_size for path in (source / "raw").glob("*.grib2")
            ),
            "source_shortfalls": selection["source_shortfalls"],
        }
        _write(directory / "preparation.json", json.dumps(report, indent=2).encode())
        return report
    except Exception as exc:
        _write(
            directory / "failure.json",
            json.dumps(
                {"error": str(exc), "object_validation": pinned.validations}, indent=2
            ).encode(),
        )
        raise
    finally:
        if owned is not None:
            owned.close()
