"""Native surface horizontal visibility evidence, without cause or blend inference."""

from __future__ import annotations

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
from mesoforge.guidance.sources.cloud import _CLOUD_READ_KEYS
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.precipitation_type import type_url
from mesoforge.guidance.sources.probabilistic import PRODUCTS, _same_object, normalized_native_grid
from mesoforge.guidance.sources.snowfall import _GRIDS

UNIT_SOURCE = "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-19.shtml"
UPP_SOURCE = "https://noaa-emc.github.io/UPP/upp-srw-v2.2.0/CALVIS__GSD_8f_source.html"
NBM_SOURCE = "https://vlab.noaa.gov/web/mdl/nbm-weather-elements-v4.1"
SOURCES: dict[str, dict[str, Any]] = {
    **{
        model: {
            "model": model,
            "provider": "NOAA",
            "product": product,
            "supported": True,
            "status": "evidence",
            "active_weight": 0.0,
            "native_parameter": "VIS",
            "native_unit": "m",
            "native_factor_to_m": 1.0,
            "native_vertical_binding": "surface",
            "native_step_hours": 1,
            "visibility_definition": "horizontal_visibility",
            "vertical_extent": "surface",
            "spatial_support": "native_model_grid",
            "documentation": UNIT_SOURCE,
            "definition_note": "Provider native surface horizontal visibility diagnostic; "
            "not slant visibility, ceiling, a fog diagnosis or precipitation intensity. "
            "Native parameterizations differ between models.",
            "censoring": {
                "status": "not_encoded_in_selected_native_product",
                "upper_bound_m": None,
                "lower_bound_m": None,
                "application_cap": None,
                "note": "Retain the published value without imposing a display/observation cap "
                "or treating the largest grid value as a declared censoring threshold.",
            },
        }
        for model, product in (
            ("HRRR", "wrfsfc CONUS"),
            ("GFS", "pgrb2.0p25"),
            ("RAP", "awp130pgrb"),
            ("NBM", "core CONUS deterministic visibility"),
        )
    },
    "IFS": {
        "model": "IFS",
        "provider": "ECMWF",
        "product": "official open-data deterministic IFS oper fc 0.25-degree",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "native_unit": "m",
        "native_factor_to_m": 1.0,
        "visibility_definition": "horizontal_visibility",
        "vertical_extent": "surface",
        "spatial_support": "native_model_grid",
        "documentation": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
        "missing_reason": "Native visibility is absent from the inspected official IFS "
        "open-data deterministic product/catalog; no substitute field or reconstruction",
    },
}
for _model in ("HRRR", "RAP"):
    SOURCES[_model]["diagnostic_reference"] = {
        "source": UPP_SOURCE,
        "scope": "Published CALVIS_GSD implementation reference, not a per-message version proof",
        "note": "Hydrometeor, aerosol and RH effects plus day/night transformation; "
        "the 90 km bound shown in an intermediate calculation is not used as a universal "
        "published-field cap. Preserve the actual source generating process and values.",
    }
SOURCES["NBM"]["diagnostic_reference"] = {
    "source": NBM_SOURCE,
    "note": "Provider postprocessed visibility; native deterministic VIS is separate from "
    "adjacent threshold-probability products and observational reporting limits.",
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def visibility_url(model: str, cycle: datetime, lead: int) -> str:
    """Reuse provider products; unsupported visibility fails before a network call."""
    if model not in SOURCES or not SOURCES[model]["supported"]:
        raise ValueError(f"Unsupported native visibility source: {model}")
    return type_url(model, cycle, lead)


def selected_visibility_row(
    model: str, cycle: datetime, lead: int, payload: bytes, length: int
) -> tuple[IndexRow, int]:
    """Select the instantaneous surface distance, not probabilities or restricted causes."""
    visibility_url(model, cycle, lead)
    rows = (
        rap.physical_index_rows(payload)[0]
        if model == "RAP"
        else parse_index_rows(payload.decode())
    )
    matches = [row for row in rows if row.descriptor == f":VIS:surface:{lead} hour fcst:"]
    if not matches:
        raise GribIndexError(f"{model} native instantaneous surface visibility unavailable")
    if len(matches) != 1:
        raise ValueError(f"{model} native surface visibility inventory is ambiguous")
    row = matches[0]
    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise ValueError("Visibility inventory source cycle mismatch")
    _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
    return row, end


def acquire_visibility_lead(
    model: str,
    cycle: datetime,
    lead: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = visibility_url(model, cycle, lead)
    endpoint, policy, deadline = "noaa_aws", ifs.IFS_RETRY_POLICY, clock.now()
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
        raise FetchError("Visibility provider object needs positive Content-Length")
    index_url = url + ".idx"
    index = fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="get",
        urls_by_endpoint=[(endpoint, index_url)],
        retry_policy=policy,
        cycle_deadline=deadline,
    )
    row, end = selected_visibility_row(model, cycle, lead, index.payload, length)
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
            SelectedMessage("visibility", row, row.byte_offset, end, fetched.payload),
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


def decode_visibility_lead(
    payload: bytes, model: str, cycle: datetime, lead: int
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Preserve native metre distances, including zero and large uncapped values."""
    visibility_url(model, cycle, lead)
    validate_grib_message_boundaries(payload, url=model, range_header="retained visibility")
    decoded = _decode_all(payload, read_keys=_CLOUD_READ_KEYS)
    candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
    if len(candidates) != 1:
        raise ValueError("Expected one native visibility field per retained message")
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
        "centre": "kwbc",
        "discipline": 0,
        "parameterCategory": 19,
        "parameterNumber": 0,
        "paramId": 3020,
        "typeOfLevel": "surface",
        "typeOfFirstFixedSurface": "sfc",
        "typeOfSecondFixedSurface": 255,
        "level": 0,
        "stepType": "instant",
        "startStep": lead,
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "productDefinitionTemplateNumber": 0,
        "generatingProcessIdentifier": {"HRRR": 83, "GFS": 96, "RAP": 105, "NBM": 104}[model],
        "iScansNegatively": 0,
        "jScansPositively": 0 if model == "GFS" else 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 1 if model == "NBM" else 0,
        "units": "m",
    }
    for key, value in expected.items():
        if attrs.get("GRIB_" + key) != value:
            raise ValueError(
                f"{model} visibility {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
            )
    valid = cycle + timedelta(hours=lead)
    for name, expected_time in (("time", cycle), ("valid_time", valid)):
        if (
            name not in field.coords
            or field[name].ndim
            or field[name].values[()] != np.datetime64(expected_time.replace(tzinfo=None), "ns")
        ):
            raise ValueError("Visibility decoded time mismatch")
    field, x, y, crs = normalized_native_grid(field)
    native = np.asarray(field.values, dtype=np.float64)
    if native.shape != (len(y), len(x)):
        raise ValueError("Native visibility grid shape mismatch")
    valid_cells = np.isfinite(native) & (native >= 0)
    variables = {
        "native_visibility": (("y", "x"), native, {"units": "m"}),
        "visibility": (("y", "x"), np.where(valid_cells, native, np.nan), {"units": "m"}),
    }
    metadata = {
        **SOURCES[model],
        "unit": "m",
        "source_cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(valid),
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "missing_reasons": [],
        "invalid_cell_count": int(np.count_nonzero(np.isfinite(native) & ~valid_cells)),
        "missing_cell_count": int(np.count_nonzero(~valid_cells)),
        "grib_keys": {
            k[5:]: v.item() if isinstance(v, np.generic) else v
            for k, v in attrs.items()
            if k.startswith("GRIB_")
        },
        "version": {
            "adapter_contract": "native_surface_visibility_v1",
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
