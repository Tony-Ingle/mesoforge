"""Native p-type evidence: independent flags, category codes, or conditional probabilities."""

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
    PRODUCTS,
    _same_object,
    normalized_native_grid,
)

FLAGS = {
    "rain": ("CRAIN", 33),
    "snow": ("CSNOW", 36),
    "freezing_rain": ("CFRZR", 34),
    "ice_pellets": ("CICEP", 35),
}
TYPE_CODES = {
    "0": [],
    "1": ["rain"],
    "3": ["freezing_rain"],
    "5": ["snow"],
    "6": ["wet_snow"],
    "7": ["rain", "snow"],
    "8": ["ice_pellets"],
    "12": ["freezing_drizzle"],
}
PROB_RANGES = {"rain": (1, 2), "snow": (5, 7), "freezing_rain": (3, 4), "ice_pellets": (8, 9)}
SOURCES: dict[str, dict[str, Any]] = {
    **{
        model: {
            "model": model,
            "encoding": "binary_flags",
            "native_unit": "code_table_4.222",
            "provider": "NOAA",
            "types": list(FLAGS),
            "documentation": f"https://www.nco.ncep.noaa.gov/pmb/products/{model.lower()}/",
        }
        for model in ("HRRR", "GFS", "RAP")
    },
    "IFS": {
        "model": "IFS",
        "provider": "ECMWF",
        "encoding": "category_code",
        "native_unit": "code_table_4.201",
        "code_mapping": TYPE_CODES,
        "licence": ifs.IFS_CAPABILITIES["licence"],
        "attribution": ifs.IFS_CAPABILITIES["attribution"],
        "documentation": "https://codes.ecmwf.int/grib/param-db/260015",
        "note": "Native instantaneous diagnosis, including tiny rates; "
        "no chart rain-rate mask applied",
    },
    "NBM": {
        "model": "NBM",
        "provider": "NOAA",
        "encoding": "conditional_probabilities",
        "native_unit": "percent",
        "category_ranges": PROB_RANGES,
        "snow_includes": ["snow", "wet_snow"],
        "condition": "given_precipitation",
        "documentation": "https://vlab.noaa.gov/web/mdl/nbm-weather-elements-v4.1",
    },
}
_EXTRA_KEYS = (
    "productDefinitionTemplateNumber",
    "forecastTime",
    "typeOfGeneratingProcess",
    "paramId",
    "probabilityType",
    "scaleFactorOfLowerLimit",
    "scaledValueOfLowerLimit",
    "scaleFactorOfUpperLimit",
    "scaledValueOfUpperLimit",
    "modelName",
    "modelVersion",
    "marsClass",
    "marsStream",
    "marsType",
    "subCentre",
    "units",
    "stepType",
)


def type_url(model: str, cycle: datetime, lead: int) -> str:
    if (
        model not in SOURCES
        or cycle.tzinfo is None
        or cycle.utcoffset() != timedelta(0)
        or cycle.minute
        or cycle.second
        or cycle.microsecond
        or type(lead) is not int
        or lead < 0
    ):
        raise ValueError("P-type needs a supported model, exact UTC cycle and integer lead")
    if model == "IFS":
        return ifs.build_grib_url(cycle=cycle, forecast_hour=lead)
    if model == "RAP":
        return rap.build_grib_url(cycle=cycle, forecast_hour=lead)
    day, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    if model == "HRRR":
        if lead > (48 if cycle.hour % 6 == 0 else 18):
            raise ValueError("HRRR p-type source lead outside this cycle's coverage")
        return f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{day}/conus/hrrr.t{hh}z.wrfsfcf{lead:02d}.grib2"
    if model == "GFS":
        if cycle.hour % 6 or lead > 120:
            raise ValueError("Bounded GFS p-type requires a six-hour cycle and hourly lead <=120")
        return f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{day}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{lead:03d}"
    if lead > 36:
        raise ValueError("Bounded NBM p-type experiment supports leads through 36")
    return f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{day}/{hh}/core/blend.t{hh}z.core.f{lead:03d}.co.grib2"


def selected_type_rows(
    model: str, cycle: datetime, lead: int, payload: bytes, length: int
) -> list[tuple[str, IndexRow, int]]:
    """Select native instantaneous evidence only; reject average/ambiguous products."""
    if model == "IFS":
        selected = []
        previous_end = 0
        for number, line in enumerate(payload.decode().splitlines(), 1):
            row = json.loads(line, object_pairs_hook=ifs._unique_keys)
            offset, size = row["_offset"], row["_length"]
            if (
                type(offset) is not int
                or type(size) is not int
                or offset < previous_end
                or size < 20
                or offset + size > length
            ):
                raise ValueError("Invalid IFS p-type inventory byte ranges")
            previous_end = offset + size
            if row.get("param") == "ptype":
                expected = {
                    "class": "od",
                    "stream": "oper",
                    "type": "fc",
                    "levtype": "sfc",
                    "step": str(lead),
                    "date": cycle.strftime("%Y%m%d"),
                    "time": cycle.strftime("%H00"),
                }
                if any(row.get(k) != v for k, v in expected.items()):
                    raise ValueError("IFS p-type inventory source/time mismatch")
                selected.append(("native_code", IndexRow(number, offset, line), offset + size))
        if len(selected) != 1:
            raise ValueError("IFS p-type is missing or ambiguous")
        return selected
    if model == "RAP":
        # Reuse the existing physical-message parser (RAP wind rows can share offsets).
        rows, _ = rap.selected_surface_field(
            payload, canonical_variable_id="air_temperature_2m", cycle=cycle, forecast_hour=lead
        )
    else:
        rows = parse_index_rows(payload.decode())
    result = []
    for field, (name, _) in FLAGS.items():
        if model == "NBM":
            lower, upper = PROB_RANGES[field]
            selector = re.escape(
                f":PTYPE:surface:{lead} hour fcst:prob >={lower} <{upper}:prob fcst "
            )
        else:
            selector = re.escape(f":{name}:surface:{lead} hour fcst:") + "$"
        row = select_field_row(rows, selector)
        if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
            raise ValueError("P-type inventory cycle mismatch")
        _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
        result.append((field, row, end))
    return result


def acquire_type_lead(
    model: str,
    cycle: datetime,
    lead: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = type_url(model, cycle, lead)
    endpoint = "ecmwf_aws" if model == "IFS" else "noaa_aws"
    policy = ifs.IFS_RETRY_POLICY
    deadline = clock.now()
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
        raise FetchError("P-type provider object needs positive Content-Length")
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
    selected = selected_type_rows(model, cycle, lead, index.payload, length)
    messages = []
    attempts = list(head.attempts)
    for field, row, end in selected:
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
        messages.append(SelectedMessage(field, row, row.byte_offset, end, fetched.payload))
        attempts.extend(fetched.attempts)
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
        selected_messages=tuple(messages),
        grib_attempts=tuple(attempts),
        grib_completed_at=fetched.completed_at,
        full_object_etag=header(head.headers, "ETag"),
        full_object_last_modified=modified,
        full_object_content_length=length,
        index_available_at=resolve_available_at(index_modified, retrieved_at=index.completed_at),
        grib_available_at=resolve_available_at(modified, retrieved_at=fetched.completed_at),
        index_last_modified=index_modified,
    )


def decode_type_lead(
    payloads: dict[str, bytes], model: str, cycle: datetime, lead: int
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    type_url(model, cycle, lead)
    fields = ("native_code",) if model == "IFS" else tuple(FLAGS)
    if set(payloads) != set(fields):
        raise ValueError("P-type requires its complete native field set")
    expected_grid = {
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
        "GFS": {
            "gridType": "regular_ll",
            "Ni": 1440,
            "Nj": 721,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
        },
        "IFS": {
            "gridType": "regular_ll",
            "Ni": 1440,
            "Nj": 721,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
        },
        "NBM": {
            k: v
            for k, v in PRODUCTS["NBM_6H"]["expected"].items()
            if k not in ("probabilityType", "parameterNumber")
        },
    }[model]
    arrays, metadata = {}, {}
    first_axes = None
    for name in fields:
        payload = payloads[name]
        validate_grib_message_boundaries(payload, url=model, range_header="retained p-type")
        decoded = _decode_all(payload, read_keys=tuple(sorted(set((*_READ_KEYS, *_EXTRA_KEYS)))))
        candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
        if len(candidates) != 1:
            raise ValueError("Expected one native p-type field per retained message")
        field = candidates[0]
        attrs = field.attrs
        parameter = (
            19
            if model in ("NBM", "IFS")
            else FLAGS[name][1] + (159 if model in ("GFS", "RAP") else 0)
        )
        expected = {
            **expected_grid,
            "centre": "ecmf" if model == "IFS" else "kwbc",
            "discipline": 0,
            "parameterCategory": 1,
            "parameterNumber": parameter,
            "typeOfLevel": "surface",
            "stepType": "instant",
            "startStep": lead,
            "endStep": lead,
            "stepUnits": 1,
            "dataDate": int(cycle.strftime("%Y%m%d")),
            "dataTime": cycle.hour * 100,
            "productDefinitionTemplateNumber": 5 if model == "NBM" else 0,
            "generatingProcessIdentifier": {
                "HRRR": 83,
                "GFS": 96,
                "RAP": 105,
                "IFS": 161,
                "NBM": 104,
            }[model],
            "iScansNegatively": 0,
            "jScansPositively": 0 if model in ("GFS", "IFS") else 1,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 1 if model == "NBM" else 0,
        }
        if model == "IFS":
            expected.update(
                marsClass="od",
                marsStream="oper",
                marsType="fc",
                modelName="IFS",
                modelVersion="cy50r1",
                paramId=260015,
            )
        for key, value in expected.items():
            if attrs.get("GRIB_" + key) != value:
                raise ValueError(
                    f"{model} p-type {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
                )
        unit = str(attrs.get("GRIB_units"))
        if model == "NBM":
            if attrs.get("GRIB_probabilityType") != 2:
                raise ValueError("NBM p-type requires native bounded-category probability")
            for side, expected_bound in zip(("Lower", "Upper"), PROB_RANGES[name], strict=True):
                bound = (
                    attrs[f"GRIB_scaledValueOf{side}Limit"]
                    / 10 ** attrs[f"GRIB_scaleFactorOf{side}Limit"]
                )
                if bound != expected_bound:
                    raise ValueError("NBM p-type category range mismatch")
            if unit not in ("%", "(Code table 4.201)"):
                raise ValueError("Unexpected native conditional-probability units")
        elif "Code table" not in unit:
            raise ValueError("Expected a native p-type category code table")
        valid = cycle + timedelta(hours=lead)
        for key, expected_time in (("time", cycle), ("valid_time", valid)):
            if field[key].ndim or field[key].values[()] != np.datetime64(
                expected_time.replace(tzinfo=None), "ns"
            ):
                raise ValueError("P-type decoded time mismatch")
        field, x, y, crs = normalized_native_grid(field)
        if first_axes is not None and (
            crs != first_axes[2]
            or not np.array_equal(x, first_axes[0])
            or not np.array_equal(y, first_axes[1])
        ):
            raise ValueError("P-type evidence fields have different native grids")
        first_axes = x, y, crs
        arrays[name] = (
            ("y", "x"),
            np.asarray(field.values, dtype=np.float64),
            {"units": SOURCES[model]["native_unit"]},
        )
        metadata[name] = {
            k[5:]: v.item() if isinstance(v, np.generic) else v
            for k, v in attrs.items()
            if k.startswith("GRIB_")
        }
    event = {
        **SOURCES[model],
        "source_cycle": cycle.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "source_lead_hours": lead,
        "valid_time": valid.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "temporal_semantics": "instantaneous",
        "interval_start": None,
        "interval_end": None,
        "spatial_support": "native_categorical_cell",
        "grib_fields": metadata,
        "classification_method": "provider_native; no surface-temperature inference",
    }
    return xr.Dataset(arrays, coords={"x": x, "y": y}), crs, event
