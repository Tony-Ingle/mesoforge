"""Native snowfall water equivalent; snowpack and snowfall depth are distinct fields."""

from __future__ import annotations

import json
import re
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
    select_field_row,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources import ifs, rap
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.probabilistic import (
    _READ_KEYS,
    _same_object,
    normalized_native_grid,
)

SWE = "snowfall_water_equivalent"
SOURCES: dict[str, dict[str, Any]] = {
    **{
        model: {
            "model": model,
            "provider": "NOAA",
            "product": product,
            "supported": True,
            "temporal_support": "hourly_accumulation",
            "native_quantity": SWE,
            "native_parameter": "WEASD",
            "native_semantics": "Provider accumulated snowfall water equivalent; "
            "native hydrometeor definition retained, not a p-type-conditioned QPF split",
            "documentation": f"https://www.nco.ncep.noaa.gov/pmb/products/{model.lower()}/",
        }
        for model, product in (("HRRR", "wrfsfc CONUS"), ("RAP", "awp130pgrb"))
    },
    "IFS": {
        "model": "IFS",
        "provider": "ECMWF",
        "product": "official open-data deterministic IFS oper fc 0.25-degree",
        "supported": True,
        "temporal_support": "cumulative_native_3h",
        "native_quantity": SWE,
        "native_parameter": "sf",
        "native_semantics": "Native accumulated large-scale plus convective snowfall",
        "documentation": "https://codes.ecmwf.int/grib/param-db/144",
        "licence": ifs.IFS_CAPABILITIES["licence"],
        "attribution": ifs.IFS_CAPABILITIES["attribution"],
    },
    "GFS": {
        "model": "GFS",
        "provider": "NOAA",
        "product": "pgrb2.0p25",
        "supported": False,
        "native_quantity": SWE,
        "missing_reason": "Inspected GFS product provides instantaneous snowpack WEASD/SNOD, "
        "not native interval snowfall water equivalent; no snowpack differencing",
    },
    "NBM": {
        "model": "NBM",
        "provider": "NOAA",
        "product": "core CONUS",
        "supported": False,
        "native_quantity": SWE,
        "missing_reason": "Inspected NBM core provides ASNOW snowfall depth and SNOD snowpack "
        "depth, not native snowfall water equivalent; no snow-ratio conversion",
    },
}
_GRIDS = {
    "HRRR": {
        "gridType": "lambert",
        "Nx": 1799,
        "Ny": 1059,
        "DxInMetres": 3000.0,
        "DyInMetres": 3000.0,
        "LoVInDegrees": 262.5,
        "LaDInDegrees": 38.5,
        "Latin1InDegrees": 38.5,
        "Latin2InDegrees": 38.5,
    },
    "RAP": {
        "gridType": "lambert",
        "Nx": 451,
        "Ny": 337,
        "DxInMetres": 13545.0,
        "DyInMetres": 13545.0,
        "LoVInDegrees": 265.0,
        "LaDInDegrees": 25.0,
        "Latin1InDegrees": 25.0,
        "Latin2InDegrees": 25.0,
    },
    "IFS": {
        "gridType": "regular_ll",
        "Ni": 1440,
        "Nj": 721,
        "iDirectionIncrementInDegrees": 0.25,
        "jDirectionIncrementInDegrees": 0.25,
    },
}
_KEYS = tuple(
    sorted(
        set(
            (
                *_READ_KEYS,
                *ifs.IFS_READ_KEYS,
                "edition",
                "packingType",
                "bitsPerValue",
                "binaryScaleFactor",
                "decimalScaleFactor",
                "referenceValue",
                "numberOfTimeRange",
                "forecastTime",
                "numberOfValues",
                "bitmapPresent",
                "missingValue",
                "units",
                "stepType",
            )
        )
    )
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def snowfall_url(model: str, cycle: datetime, lead: int) -> str:
    if model not in SOURCES or not SOURCES[model]["supported"]:
        raise ValueError(f"Unsupported native snowfall source: {model}")
    if (
        cycle.tzinfo is None
        or cycle.utcoffset() != timedelta(0)
        or cycle.minute
        or cycle.second
        or cycle.microsecond
        or type(lead) is not int
        or lead < 0
    ):
        raise ValueError("Snowfall needs an exact UTC cycle and nonnegative integer lead")
    if model == "IFS":
        return ifs.build_grib_url(cycle=cycle, forecast_hour=lead)
    if lead < 1:
        raise ValueError("Native hourly snowfall accumulation requires a positive lead")
    if model == "RAP":
        return rap.build_grib_url(cycle=cycle, forecast_hour=lead)
    if lead > (48 if cycle.hour % 6 == 0 else 18):
        raise ValueError("HRRR snowfall lead outside this cycle's coverage")
    return (
        f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{cycle:%Y%m%d}/conus/"
        f"hrrr.t{cycle:%H}z.wrfsfcf{lead:02d}.grib2"
    )


def selected_snowfall_row(
    model: str, cycle: datetime, lead: int, payload: bytes, length: int
) -> tuple[IndexRow, int]:
    """Select one native amount; never select an instantaneous snowpack message."""
    snowfall_url(model, cycle, lead)
    if model == "IFS":
        selected = []
        previous_end = 0
        for number, line in enumerate(payload.decode().splitlines(), 1):
            row = json.loads(line, object_pairs_hook=ifs._unique_keys)
            if not isinstance(row, dict):
                raise ValueError("IFS snowfall inventory requires JSON objects")
            offset, size = row.get("_offset"), row.get("_length")
            if (
                type(offset) is not int
                or type(size) is not int
                or offset < previous_end
                or size < 20
                or offset + size > length
            ):
                raise ValueError("Invalid IFS snowfall inventory byte ranges")
            previous_end = offset + size
            if row.get("param") != "sf":
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
                raise ValueError("IFS snowfall inventory source/time mismatch")
            selected.append((IndexRow(number, offset, line), offset + size))
        if not selected:
            raise GribIndexError("IFS native snowfall field unavailable in selected product")
        if len(selected) != 1:
            raise ValueError("IFS native snowfall is ambiguous")
        return selected[0]
    if model == "RAP":
        # Reuse physical boundaries: RAP wind submessages share one byte offset.
        rows, _ = rap.selected_surface_field(
            payload, canonical_variable_id="air_temperature_2m", cycle=cycle, forecast_hour=lead
        )
    else:
        rows = parse_index_rows(payload.decode())
    selector = re.escape(f":WEASD:surface:{lead - 1}-{lead} hour acc fcst:") + "$"
    row = select_field_row(rows, selector)
    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise ValueError("Snowfall inventory source cycle mismatch")
    _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
    return row, end


def acquire_snowfall_lead(
    model: str,
    cycle: datetime,
    lead: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = snowfall_url(model, cycle, lead)
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
        raise FetchError("Snowfall provider object needs positive Content-Length")
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
    row, end = selected_snowfall_row(model, cycle, lead, index.payload, length)
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
    modified, index_modified = (
        header(head.headers, "Last-Modified"),
        header(index.headers, "Last-Modified"),
    )
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
        selected_messages=(SelectedMessage(SWE, row, row.byte_offset, end, fetched.payload),),
        grib_attempts=(*head.attempts, *fetched.attempts),
        grib_completed_at=fetched.completed_at,
        full_object_etag=header(head.headers, "ETag"),
        full_object_last_modified=modified,
        full_object_content_length=length,
        index_available_at=resolve_available_at(index_modified, retrieved_at=index.completed_at),
        grib_available_at=resolve_available_at(modified, retrieved_at=fetched.completed_at),
        index_last_modified=index_modified,
    )


def decode_snowfall_lead(
    payload: bytes, model: str, cycle: datetime, lead: int
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Retain native amounts and exact bounds; normalize water units exactly once."""
    snowfall_url(model, cycle, lead)
    validate_grib_message_boundaries(payload, url=model, range_header="retained snowfall")
    decoded = _decode_all(payload, read_keys=_KEYS)
    candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
    if len(candidates) != 1:
        raise ValueError("Expected one native snowfall field per retained message")
    field = candidates[0]
    attrs = field.attrs
    start = attrs.get("GRIB_startStep")
    if (
        not isinstance(start, (int, float))
        or isinstance(start, bool)
        or not np.isfinite(start)
        or int(start) != start
    ):
        raise ValueError("Snowfall requires integer-hour native accumulation bounds")
    start = int(start)
    if start < 0 or start > lead or (model != "IFS" and start != lead - 1):
        raise ValueError("Snowfall accumulation window mismatch")
    if model == "IFS" and start == lead and lead != 0:
        raise ValueError("Only an actual IFS zero-lead parent may have zero duration")
    expected = {
        **_GRIDS[model],
        "centre": "ecmf" if model == "IFS" else "kwbc",
        "discipline": 0,
        "parameterCategory": 1,
        "typeOfLevel": "surface",
        "stepType": "accum",
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "productDefinitionTemplateNumber": 8,
        "typeOfStatisticalProcessing": 1,
        "lengthOfTimeRange": lead - start,
        "indicatorOfUnitForTimeRange": 1,
        "numberOfMissingInStatisticalProcess": 0,
        "generatingProcessIdentifier": {"HRRR": 83, "RAP": 105, "IFS": 161}[model],
        "iScansNegatively": 0,
        "jScansPositively": 0 if model == "IFS" else 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
    }
    unit = str(attrs.get("GRIB_units"))
    factor = 1.0
    if model == "IFS":
        expected.update(marsClass="od", marsStream="oper", marsType="fc", modelName="IFS")
        if attrs.get("GRIB_paramId") == 144 and unit == "m of water equivalent":
            expected.update(parameterNumber=198, paramId=144)
            factor = 1000.0
        elif attrs.get("GRIB_paramId") == 228144 and unit in ("kg m**-2", "kg m-2"):
            expected.update(parameterNumber=53, paramId=228144)
        else:
            raise ValueError("IFS snowfall requires sf accumulated water-equivalent units")
    else:
        expected["parameterNumber"] = 13
        if unit not in ("kg m**-2", "kg m-2"):
            raise ValueError("Native WEASD must use kg/m^2; depth/rate units are unsupported")
    for key, value in expected.items():
        if attrs.get("GRIB_" + key) != value:
            raise ValueError(
                f"{model} snowfall {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
            )
    valid = cycle + timedelta(hours=lead)
    for key, expected_time in (("time", cycle), ("valid_time", valid)):
        if (
            key not in field.coords
            or field[key].ndim
            or field[key].values[()] != np.datetime64(expected_time.replace(tzinfo=None), "ns")
        ):
            raise ValueError("Snowfall decoded cycle/valid time mismatch")
    field, x, y, crs = normalized_native_grid(field)
    native = np.asarray(field.values, dtype=np.float64)
    if native.shape != (len(y), len(x)) or not np.all(np.diff(x) > 0) or not np.all(np.diff(y) > 0):
        raise ValueError("Snowfall native grid shape/axes mismatch")
    valid_cells = np.isfinite(native) & (native >= 0)
    amount = np.where(valid_cells, native * factor, np.nan)
    event = {
        **SOURCES[model],
        "source_cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(valid),
        "temporal_semantics": "accumulation",
        "interval_start": _iso(cycle + timedelta(hours=start)),
        "interval_end": _iso(valid),
        "interval_closure": "left_open_right_closed",
        "native_unit": unit,
        "unit": "kg/m^2",
        "unit_factor_to_kg_m2": factor,
        "spatial_support": "native_model_grid",
        "normalization": "native_amount_times_unit_factor_once; negative/nonfinite_unavailable",
        "missing_cell_count": int(np.count_nonzero(~valid_cells)),
        "invalid_cell_count": int(np.count_nonzero(np.isfinite(native) & ~valid_cells)),
        "version": {
            "adapter_contract": "native_snowfall_water_equivalent_v1",
            "model_version": attrs.get("GRIB_modelVersion"),
            "generating_process_identifier": attrs["GRIB_generatingProcessIdentifier"],
            "grib_tables_version": attrs.get("GRIB_tablesVersion"),
            "grib_local_tables_version": attrs.get("GRIB_localTablesVersion"),
        },
        "grib_keys": {
            k[5:]: v.item() if isinstance(v, np.generic) else v
            for k, v in attrs.items()
            if k.startswith("GRIB_")
        },
    }
    result = xr.Dataset(
        {
            "native_amount": (("y", "x"), native, {"units": unit}),
            "amount": (("y", "x"), amount, {"units": "kg/m^2"}),
        },
        coords={"x": x, "y": y},
        attrs={"crs_wkt2": crs.to_wkt(), "model": model},
    )
    return result, crs, event
