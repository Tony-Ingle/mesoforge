"""Native instantaneous total-cloud evidence, kept distinct from layer clouds.

This adapter normalizes units only. It does not select a delivered cloud source,
reconstruct total cover from layers, or turn period averages into snapshots.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    SelectedMessage,
    resolve_available_at,
)
from mesoforge.guidance.http_fetch import (
    FetchError,
    fetch_with_range,
    fetch_with_retry,
    header,
    validate_grib_message_boundaries,
)
from mesoforge.guidance.index_parsing import (
    GribIndexError,
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources import ifs, rap
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.precipitation_type import type_url
from mesoforge.guidance.sources.probabilistic import PRODUCTS, _same_object, normalized_native_grid
from mesoforge.guidance.sources.snowfall import _GRIDS, _KEYS

UNIT_SOURCE = "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-6.shtml"
_CLOUD_READ_KEYS = tuple(
    sorted(
        set(
            (
                *_KEYS,
                "typeOfFirstFixedSurface",
                "typeOfSecondFixedSurface",
                "scaleFactorOfFirstFixedSurface",
                "scaledValueOfFirstFixedSurface",
                "scaleFactorOfSecondFixedSurface",
                "scaledValueOfSecondFixedSurface",
            )
        )
    )
)
SOURCES: dict[str, dict[str, Any]] = {
    **{
        model: {
            "model": model,
            "provider": "NOAA",
            "product": product,
            "supported": True,
            "status": "evidence",
            "active_weight": 0.0,
            "native_parameter": "TCDC",
            "native_unit": "%",
            "native_factor_to_percent": 1.0,
            "native_vertical_binding": ("surface" if model == "NBM" else "atmosphere"),
            "native_step_hours": 1,
            "documentation": UNIT_SOURCE,
            "definition_note": "Provider total sky/cloud cover; provider cloud overlap and "
            "parameterization retained, not reconstructed by summing layers",
        }
        for model, product in (
            ("HRRR", "wrfsfc CONUS"),
            ("GFS", "pgrb2.0p25"),
            ("RAP", "awp130pgrb"),
            ("NBM", "core CONUS deterministic total sky cover"),
        )
    },
    "IFS": {
        "model": "IFS",
        "provider": "ECMWF",
        "product": "official open-data deterministic IFS oper fc 0.25-degree",
        "supported": True,
        "status": "evidence",
        "active_weight": 0.0,
        "native_parameter": "tcc",
        "native_unit": "(0 - 1)",
        "native_factor_to_percent": 100.0,
        "native_vertical_binding": "entireAtmosphere",
        "native_step_hours": 3,
        "documentation": "https://codes.ecmwf.int/grib/param-db/164",
        "licence": ifs.IFS_CAPABILITIES["licence"],
        "attribution": ifs.IFS_CAPABILITIES["attribution"],
        "definition_note": "Provider total cover using native cloud overlap assumptions; "
        "native three-hourly snapshots without time interpolation",
    },
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def cloud_url(model: str, cycle: datetime, lead: int) -> str:
    """Reuse already supported provider products and bounded cycle/lead validation."""
    if model not in SOURCES:
        raise ValueError(f"Unsupported total cloud source: {model}")
    from mesoforge.catalog.native_horizons import native_field_contract

    if lead not in native_field_contract(model, "cloud_area_fraction").native_leads(cycle):
        raise ValueError("Cloud source lead is outside the native product schedule")
    if model in {"GFS", "NBM"}:
        day, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
        if model == "GFS":
            return f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{day}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{lead:03d}"
        return f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{day}/{hh}/core/blend.t{hh}z.core.f{lead:03d}.co.grib2"
    return type_url(model, cycle, lead)


def selected_cloud_row(
    model: str, cycle: datetime, lead: int, payload: bytes, length: int
) -> tuple[IndexRow, int]:
    """Exact total snapshot only: no layer, average, or standard deviation fallback."""
    cloud_url(model, cycle, lead)
    if model == "IFS":
        selected = []
        previous_end = 0
        for number, line in enumerate(payload.decode().splitlines(), 1):
            row = json.loads(line, object_pairs_hook=ifs._unique_keys)
            if not isinstance(row, dict):
                raise ValueError("IFS cloud inventory requires JSON objects")
            offset, size = row.get("_offset"), row.get("_length")
            if (
                type(offset) is not int
                or type(size) is not int
                or offset < previous_end
                or size < 20
                or offset + size > length
            ):
                raise ValueError("Invalid IFS cloud inventory byte ranges")
            previous_end = offset + size
            if row.get("param") != "tcc":
                continue
            expected = {
                "domain": "g",
                "class": "od",
                "stream": "oper",
                "type": "fc",
                "levtype": "sfc",
                "step": str(lead),
                "date": cycle.strftime("%Y%m%d"),
                "time": cycle.strftime("%H00"),
                "expver": "0001",
            }
            if (
                any(row.get(k) != v for k, v in expected.items())
                or "number" in row
                or row.get("model", "ifs") != "ifs"
            ):
                raise ValueError("IFS total cloud inventory source/time mismatch")
            selected.append((IndexRow(number, offset, line), offset + size))
        if not selected:
            raise GribIndexError("IFS native total cloud field unavailable in selected product")
        if len(selected) != 1:
            raise ValueError("IFS native total cloud field is ambiguous")
        return selected[0]
    rows = (
        rap.physical_index_rows(payload)[0]
        if model == "RAP"
        else parse_index_rows(payload.decode())
    )
    level = "surface" if model == "NBM" else "entire atmosphere"
    descriptor = f":TCDC:{level}:{lead} hour fcst:"
    matches = [row for row in rows if row.descriptor == descriptor]
    if not matches:
        raise GribIndexError(f"{model} native instantaneous total cloud field unavailable")
    if len(matches) != 1:
        raise ValueError(f"{model} native total cloud inventory is ambiguous")
    selected_row = matches[0]
    if selected_row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise ValueError("Total cloud inventory source cycle mismatch")
    _, end = compute_message_byte_range(rows, selected=selected_row, full_object_length=length)
    return selected_row, end


def acquire_cloud_lead(
    model: str,
    cycle: datetime,
    lead: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = cloud_url(model, cycle, lead)
    endpoint = "ecmwf_aws" if model == "IFS" else "noaa_aws"
    policy, deadline = ifs.IFS_RETRY_POLICY, clock.now()
    head = fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="head",
        urls_by_endpoint=[(endpoint, url)],
        retry_policy=policy,
        cycle_deadline=deadline,
    )
    length = int(header(head.headers, "Content-Length") or 0)
    if length <= 0:
        raise FetchError("Cloud provider object needs positive Content-Length")
    index_url = url.removesuffix(".grib2") + ".index" if model == "IFS" else url + ".idx"
    index = fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="get",
        urls_by_endpoint=[(endpoint, index_url)],
        retry_policy=policy,
        cycle_deadline=deadline,
    )
    row, end = selected_cloud_row(model, cycle, lead, index.payload, length)
    fetched = fetch_with_range(
        transport,
        clock,
        sleeper,
        endpoint=endpoint,
        url=url,
        range_header=f"bytes={row.byte_offset}-{end - 1}",
        byte_start=row.byte_offset,
        byte_end=end,
        retry_policy=policy,
        cycle_deadline=deadline,
        expected_length=end - row.byte_offset,
        full_object_length=length,
    )
    _same_object(head.headers, fetched.headers)
    modified = header(head.headers, "Last-Modified")
    index_modified = header(index.headers, "Last-Modified")
    return Phase2LeadAcquisition(
        model=model,
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
        forecast_hour=lead,
        endpoint=endpoint,
        resolved_grib_url=url,
        resolved_index_url=index_url,
        index_payload=index.payload,
        index_attempts=index.attempts,
        index_completed_at=index.completed_at,
        selected_messages=(
            SelectedMessage("cloud_cover", row, row.byte_offset, end, fetched.payload),
        ),
        grib_attempts=(*head.attempts, *fetched.attempts),
        grib_completed_at=fetched.completed_at,
        full_object_etag=header(head.headers, "ETag"),
        full_object_last_modified=modified,
        full_object_content_length=length,
        index_available_at=resolve_available_at(index_modified, retrieved_at=index.completed_at),
        grib_available_at=resolve_available_at(modified, retrieved_at=fetched.completed_at),
        index_last_modified=index_modified,
    )


def decode_cloud_lead(
    payload: bytes, model: str, cycle: datetime, lead: int
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Retain native total cover and convert valid values to percent without clamping."""
    cloud_url(model, cycle, lead)
    validate_grib_message_boundaries(payload, url=model, range_header="retained total cloud")
    decoded = _decode_all(payload, read_keys=_CLOUD_READ_KEYS)
    candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
    if len(candidates) != 1:
        raise ValueError("Expected one native total cloud field per retained message")
    field = candidates[0]
    attrs = field.attrs
    grid = (
        {
            k: v
            for k, v in PRODUCTS["NBM_6H"]["expected"].items()
            if k not in ("probabilityType", "parameterNumber")
        }
        if model == "NBM"
        else _GRIDS["IFS" if model == "GFS" else model]
    )
    expected = {
        **grid,
        "centre": "ecmf" if model == "IFS" else "kwbc",
        "discipline": 0,
        "parameterCategory": 6,
        "parameterNumber": 192 if model == "IFS" else 1,
        "typeOfLevel": SOURCES[model]["native_vertical_binding"],
        "typeOfFirstFixedSurface": "sfc" if model in ("NBM", "IFS") else "10",
        "typeOfSecondFixedSurface": 8 if model == "IFS" else 255,
        "paramId": 164 if model == "IFS" else 228164,
        "level": 0,
        "stepType": "instant",
        "startStep": lead,
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "productDefinitionTemplateNumber": 0,
        "generatingProcessIdentifier": {"HRRR": 83, "GFS": 96, "RAP": 105, "IFS": 161, "NBM": 104}[
            model
        ],
        "iScansNegatively": 0,
        "jScansPositively": 0 if model in ("GFS", "IFS") else 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 1 if model == "NBM" else 0,
        "units": SOURCES[model]["native_unit"],
    }
    if model == "IFS":
        expected.update(
            marsClass="od",
            marsStream="oper",
            marsType="fc",
            modelName="IFS",
            modelVersion="cy50r1",
            paramId=164,
        )
    for key, value in expected.items():
        if attrs.get("GRIB_" + key) != value:
            raise ValueError(
                f"{model} cloud {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
            )
    valid = cycle + timedelta(hours=lead)
    for name, expected_time in (("time", cycle), ("valid_time", valid)):
        if (
            name not in field.coords
            or field[name].ndim
            or field[name].values[()] != np.datetime64(expected_time.replace(tzinfo=None), "ns")
        ):
            raise ValueError("Cloud decoded time mismatch")
    field, x, y, crs = normalized_native_grid(field)
    native = np.asarray(field.values, dtype=np.float64)
    if native.shape != (len(y), len(x)):
        raise ValueError("Native cloud grid shape mismatch")
    factor = SOURCES[model]["native_factor_to_percent"]
    percent = native * factor
    valid_cells = np.isfinite(percent) & (percent >= 0) & (percent <= 100)
    variables = {
        "native_cloud_cover": (("y", "x"), native, {"units": SOURCES[model]["native_unit"]}),
        "cloud_cover": (("y", "x"), np.where(valid_cells, percent, np.nan), {"units": "percent"}),
    }
    metadata = {
        **SOURCES[model],
        "unit": "percent",
        "cloud_definition": "total_cloud_cover",
        "vertical_extent": "entire_atmosphere",
        "source_cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(valid),
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "spatial_support": "native_model_grid",
        "missing_reasons": [],
        "invalid_cell_count": int(np.count_nonzero(np.isfinite(percent) & ~valid_cells)),
        "missing_cell_count": int(np.count_nonzero(~valid_cells)),
        "grib_keys": {
            k[5:]: v.item() if isinstance(v, np.generic) else v
            for k, v in attrs.items()
            if k.startswith("GRIB_")
        },
        "version": {
            "adapter_contract": "native_total_cloud_v1",
            "generating_process_identifier": expected["generatingProcessIdentifier"],
            "model_version": attrs.get("GRIB_modelVersion"),
        },
    }
    return (
        xr.Dataset(
            variables, coords={"x": x, "y": y}, attrs={"crs_wkt2": crs.to_wkt(), "model": model}
        ),
        crs,
        metadata,
    )
