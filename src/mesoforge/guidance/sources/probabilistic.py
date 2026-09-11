"""Native, event-specific precipitation probabilities; no QPF-to-PoP conversion.

The registered products remain separate events. In particular, neighborhood
heavy-rain probabilities and 24-hour ensemble probabilities are not hourly PoP.
"""

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
    FetchedObject,
    FetchError,
    attempt_request,
    fetch_with_range,
    fetch_with_retry,
    header,
    parse_content_range,
    validate_grib_message_boundaries,
)
from mesoforge.guidance.index_parsing import (
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
    select_field_row,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.normalization import (
    build_lambert_conformal_crs,
    compute_latlon_grid,
    compute_projected_coordinates,
)
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.ifs import IFS_CAPABILITIES, IFS_RETRY_POLICY, _unique_keys

PRODUCTS: dict[str, dict[str, Any]] = {
    "NBM_6H": {
        "model": "NBM",
        "provider": "NOAA",
        "duration_hours": 6,
        "threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"},
        "spatial_support": {"kind": "grid_point"},
        "probability_method": "native_published_probability",
        "product": "NBM CONUS core native six-hour PoP",
        "status": "shadow",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/blend/",
        "expected": {
            "centre": "kwbc",
            "subCentre": 14,
            "generatingProcessIdentifier": 104,
            "typeOfGeneratingProcess": 2,
            "probabilityType": 1,
            "parameterNumber": 8,
            "gridType": "lambert",
            "Nx": 2345,
            "Ny": 1597,
            "DxInMetres": 2539.703,
            "DyInMetres": 2539.703,
            "LaDInDegrees": 25.0,
            "LoVInDegrees": 265.0,
            "Latin1InDegrees": 25.0,
            "Latin2InDegrees": 25.0,
            "radius": 6371200.0,
            "iScansNegatively": 0,
            "jScansPositively": 1,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 1,
        },
    },
    "GEFS_6H": {
        "model": "GEFS",
        "provider": "NOAA",
        "duration_hours": 6,
        "threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"},
        "spatial_support": {"kind": "grid_point"},
        "probability_method": "native_published_bias_corrected_ensemble_probability",
        "product": "GEFS bias-corrected six-hour PQPF",
        "status": "shadow",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/gens/",
        "expected": {
            "centre": "kwbc",
            "subCentre": 2,
            "generatingProcessIdentifier": 107,
            "typeOfGeneratingProcess": 11,
            "probabilityType": 1,
            "parameterNumber": 8,
            "gridType": "regular_ll",
            "Ni": 720,
            "Nj": 361,
            "iDirectionIncrementInDegrees": 0.5,
            "jDirectionIncrementInDegrees": 0.5,
            "iScansNegatively": 0,
            "jScansPositively": 0,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
        },
    },
    "REFS_1H": {
        "model": "REFS",
        "provider": "NOAA",
        "duration_hours": 1,
        "threshold": {"value": 12.7, "unit": "kg/m^2", "comparison": "gt"},
        "spatial_support": {
            "kind": "neighborhood",
            "radius_km": None,
            "radius_status": "not_encoded_in_retained_message",
        },
        "probability_method": "native_published_neighborhood_probability",
        "product": "REFS parallel CONUS one-hour heavy-rain probability",
        "status": "shadow",
        "operational_status": "pre_implementation_parallel",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/refs/",
        "expected": {
            "centre": "kwbc",
            "subCentre": 2,
            "generatingProcessIdentifier": 136,
            "typeOfGeneratingProcess": 194,
            "probabilityType": 1,
            "parameterNumber": 8,
            "gridType": "lambert",
            "Nx": 1799,
            "Ny": 1059,
            "DxInMetres": 3000.0,
            "DyInMetres": 3000.0,
            "LaDInDegrees": 38.5,
            "LoVInDegrees": 262.5,
            "Latin1InDegrees": 38.5,
            "Latin2InDegrees": 38.5,
            "radius": 6371229.0,
            "iScansNegatively": 0,
            "jScansPositively": 1,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
        },
    },
    "ECMWF_ENS_24H": {
        "model": "ECMWF_ENS",
        "provider": "ECMWF",
        "duration_hours": 24,
        "threshold": {"value": 1.0, "unit": "kg/m^2", "comparison": "ge"},
        "spatial_support": {
            "kind": "grid_box_mean",
            "published_grid_spacing_degrees": 0.25,
            "effective_event_footprint": "not_encoded_in_retained_message",
            "point_downscaling": "not_applied",
            "documentation": (
                "https://confluence.ecmwf.int/spaces/FUG/pages/673551197/"
                "Section+8.1.7+Point+rainfall"
            ),
        },
        "probability_method": "native_published_ensemble_probability",
        "product": "IFS ENS native tpg1 24-hour probability",
        "status": "shadow",
        "documentation": IFS_CAPABILITIES["product_documentation"],
        "licence": IFS_CAPABILITIES["licence"],
        "attribution": IFS_CAPABILITIES["attribution"],
        "expected": {
            "centre": "ecmf",
            "generatingProcessIdentifier": 161,
            "typeOfGeneratingProcess": 5,
            "probabilityType": 3,
            "parameterNumber": 52,
            "gridType": "regular_ll",
            "Ni": 1440,
            "Nj": 721,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
            "iScansNegatively": 0,
            "jScansPositively": 0,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
            "marsClass": "od",
            "marsStream": "enfo",
            "marsType": "ep",
            "modelName": "IFS",
            "modelVersion": "cy50r1",
            "paramId": 131060,
        },
    },
}

_READ_KEYS = tuple(
    sorted(
        {
            "discipline",
            "parameterCategory",
            "typeOfLevel",
            "level",
            "units",
            "stepType",
            "startStep",
            "endStep",
            "stepUnits",
            "dataDate",
            "dataTime",
            "productDefinitionTemplateNumber",
            "typeOfStatisticalProcessing",
            "lengthOfTimeRange",
            "indicatorOfUnitForTimeRange",
            "scaleFactorOfUpperLimit",
            "scaledValueOfUpperLimit",
            "scaleFactorOfLowerLimit",
            "scaledValueOfLowerLimit",
            "numberOfMissingInStatisticalProcess",
            "totalNumberOfForecastProbabilities",
            "forecastProbabilityNumber",
            "tablesVersion",
            "localTablesVersion",
            "shapeOfTheEarth",
            "latitudeOfFirstGridPointInDegrees",
            "longitudeOfFirstGridPointInDegrees",
            "latitudeOfLastGridPointInDegrees",
            "longitudeOfLastGridPointInDegrees",
            *(key for product in PRODUCTS.values() for key in product["expected"]),
        }
    )
)


def _request(source_id: str, cycle: datetime, start_hour: int, end_hour: int) -> dict[str, Any]:
    product = PRODUCTS[source_id]
    if (
        cycle.tzinfo is None
        or cycle.utcoffset() != timedelta(0)
        or cycle.minute
        or cycle.second
        or cycle.microsecond
    ):
        raise ValueError("Probability source cycle must be an exact UTC hour")
    if (
        type(start_hour) is not int
        or type(end_hour) is not int
        or start_hour < 0
        or end_hour - start_hour != product["duration_hours"]
    ):
        raise ValueError("Requested interval does not match the native probability product")
    if source_id != "NBM_6H" and cycle.hour not in (0, 6, 12, 18):
        raise ValueError("Probability cycle must be 00/06/12/18Z")
    if source_id == "GEFS_6H" and (end_hour % 6 or end_hour > 384):
        raise ValueError("GEFS PQPF is published at six-hour leads through 384")
    if source_id == "REFS_1H" and end_hour > 60:
        raise ValueError("REFS probability lead exceeds 60 hours")
    if source_id == "ECMWF_ENS_24H" and (
        cycle.hour not in (0, 12) or end_hour % 12 or end_hour > 240
    ):
        raise ValueError("ECMWF 24-hour probability uses 00/12Z cycles and twelve-hour steps")
    return product


def product_url(source_id: str, cycle: datetime, start_hour: int, end_hour: int) -> str:
    _request(source_id, cycle, start_hour, end_hour)
    day, hh = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    if source_id == "NBM_6H":
        return (
            f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{day}/{hh}/core/"
            f"blend.t{hh}z.core.f{end_hour:03d}.co.grib2"
        )
    if source_id == "GEFS_6H":
        return (
            f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/naefs/prod/gefs.{day}/{hh}/"
            f"prcp_bc_gb2/gepqpf.t{hh}z.pgrb2a.0p50.bc_06hf{end_hour:03d}"
        )
    if source_id == "REFS_1H":
        return (
            f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/refs/para/refs.{day}/{hh}/ensprod/"
            f"refs.t{hh}z.prob.f{end_hour:02d}.conus.grib2"
        )
    return (
        f"https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/{day}/{hh}z/ifs/0p25/enfo/"
        f"{day}{hh}0000-240h-enfo-ep.grib2"
    )


def _same_object(expected: dict[str, str], actual: dict[str, str]) -> None:
    for name in ("ETag", "Last-Modified"):
        if header(expected, name) is not None and header(actual, name) != header(expected, name):
            raise FetchError(f"Probability provider object changed {name} during acquisition")


def acquire_product(
    source_id: str,
    cycle: datetime,
    start_hour: int,
    end_hour: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    """Fetch only one native event, retaining its actual index or GRIB header evidence."""
    product = _request(source_id, cycle, start_hour, end_hour)
    url = product_url(source_id, cycle, start_hour, end_hour)
    endpoint = "ecmwf_aws" if product["provider"] == "ECMWF" else "noaa"
    deadline = clock.now()
    head = fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="head",
        urls_by_endpoint=[(endpoint, url)],
        retry_policy=IFS_RETRY_POLICY,
        cycle_deadline=deadline,
    )
    length = int(header(head.headers, "Content-Length") or 0)
    if length <= 0:
        raise FetchError("Probability provider requires a positive full-object Content-Length")
    if source_id == "GEFS_6H":
        # This provider publishes no index. Read the first message's actual Section 0;
        # decoding below must independently confirm it is the registered threshold.
        response, attempt = attempt_request(
            transport,
            method="get",
            url=url,
            endpoint=endpoint,
            timeout=(10.0, 60.0),
            request_headers={"Range": "bytes=0-15"},
        )
        if (
            response is None
            or response.status_code != 206
            or len(response.content) != 16
            or parse_content_range(header(dict(response.headers), "Content-Range"))
            != (0, 15, length)
            or response.content[:4] != b"GRIB"
            or response.content[7] != 2
        ):
            raise FetchError("GEFS probability Section 0 range is missing or invalid")
        _same_object(head.headers, dict(response.headers))
        index = FetchedObject(
            endpoint,
            url,
            url,
            bytes(response.content),
            dict(response.headers),
            (attempt,),
            clock.now(),
        )
        end = int.from_bytes(index.payload[8:16], "big")
        row = IndexRow(1, 0, "GRIB Section 0: first native message; event validated from GRIB keys")
    else:
        index_url = (
            url.removesuffix(".grib2") + ".index" if source_id == "ECMWF_ENS_24H" else url + ".idx"
        )
        index = fetch_with_retry(
            transport,
            clock,
            sleeper,
            method="get",
            urls_by_endpoint=[(endpoint, index_url)],
            retry_policy=IFS_RETRY_POLICY,
            cycle_deadline=deadline,
        )
        if source_id == "ECMWF_ENS_24H":
            rows = []
            previous_end = 0
            for number, line in enumerate(index.payload.decode().splitlines(), 1):
                record = json.loads(line, object_pairs_hook=_unique_keys)
                offset, size = record.get("_offset"), record.get("_length")
                if (
                    type(offset) is not int
                    or type(size) is not int
                    or offset < previous_end
                    or size < 20
                ):
                    raise ValueError("Invalid ECMWF probability index byte ranges")
                previous_end = offset + size
                if (
                    record.get("param") == "tpg1"
                    and record.get("step") == f"{start_hour}-{end_hour}"
                ):
                    expected = {
                        "class": "od",
                        "stream": "enfo",
                        "type": "ep",
                        "levtype": "sfc",
                        "date": cycle.strftime("%Y%m%d"),
                        "time": cycle.strftime("%H00"),
                    }
                    if any(record.get(key) != value for key, value in expected.items()):
                        raise ValueError("ECMWF probability inventory identity mismatch")
                    rows.append((IndexRow(number, offset, line), offset + size))
            if len(rows) != 1:
                raise ValueError("ECMWF probability native event missing or ambiguous")
            row, end = rows[0]
        else:
            parsed = parse_index_rows(index.payload.decode())
            threshold = product["threshold"]["value"]
            selector = re.escape(
                f":APCP:surface:{start_hour}-{end_hour} hour acc fcst:prob >{threshold}:prob fcst "
            )
            row = select_field_row(parsed, selector)
            if f"d={cycle:%Y%m%d%H}:" not in row.line:
                raise ValueError("Probability inventory cycle mismatch")
            if source_id == "REFS_1H" and not row.line.endswith(":Neighborhood Probability"):
                raise ValueError("REFS probability inventory spatial support mismatch")
            _, end = compute_message_byte_range(parsed, selected=row, full_object_length=length)
    if end > length or end - row.byte_offset < 20:
        raise FetchError("Probability selected message exceeds provider object")
    fetched = fetch_with_range(
        transport,
        clock,
        sleeper,
        endpoint=endpoint,
        url=url,
        range_header=f"bytes={row.byte_offset}-{end - 1}",
        byte_start=row.byte_offset,
        byte_end=end,
        retry_policy=IFS_RETRY_POLICY,
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
        model=source_id,
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
        forecast_hour=end_hour,
        endpoint=endpoint,
        resolved_grib_url=url,
        resolved_index_url=index.resolved_url,
        index_payload=index.payload,
        index_attempts=index.attempts,
        index_completed_at=index.completed_at,
        selected_messages=(
            SelectedMessage(
                "precipitation_probability", row, row.byte_offset, end, fetched.payload
            ),
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


def decode_product(
    payload: bytes,
    source_id: str,
    cycle: datetime,
    start_hour: int,
    end_hour: int,
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Validate one native probability event and normalize percent once, without clipping."""
    product = _request(source_id, cycle, start_hour, end_hour)
    validate_grib_message_boundaries(payload, url=source_id, range_header="retained message")
    decoded = _decode_all(payload, read_keys=_READ_KEYS)
    fields = [_with_dataset_coords(value, ds) for ds in decoded for value in ds.data_vars.values()]
    if len(fields) != 1:
        raise ValueError("Expected exactly one native probability field")
    field = fields[0]
    attrs = field.attrs
    expected = {
        **product["expected"],
        "discipline": 0,
        "parameterCategory": 1,
        "typeOfLevel": "surface",
        "productDefinitionTemplateNumber": 9,
        "typeOfStatisticalProcessing": 1,
        "lengthOfTimeRange": end_hour - start_hour,
        "indicatorOfUnitForTimeRange": 1,
        "startStep": start_hour,
        "endStep": end_hour,
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "numberOfMissingInStatisticalProcess": 0,
    }
    for key, value in expected.items():
        if attrs.get(f"GRIB_{key}") != value:
            raise ValueError(
                f"{source_id} GRIB {key} mismatch: expected {value!r}, "
                f"got {attrs.get(f'GRIB_{key}')!r}"
            )
    bound = "Lower" if product["threshold"]["comparison"] == "ge" else "Upper"
    threshold = float(attrs[f"GRIB_scaledValueOf{bound}Limit"]) / 10 ** int(
        attrs[f"GRIB_scaleFactorOf{bound}Limit"]
    )
    if threshold != product["threshold"]["value"]:
        raise ValueError("Native probability threshold mismatch")
    accepted_units = ("%",) if source_id == "ECMWF_ENS_24H" else ("%", "kg m**-2", "kg m-2")
    if str(attrs.get("GRIB_units", "")).lower() not in accepted_units:
        raise ValueError("Native probability units are unsupported")
    start, end = cycle + timedelta(hours=start_hour), cycle + timedelta(hours=end_hour)
    for name, expected_time in (
        ("time", np.datetime64(cycle.replace(tzinfo=None), "ns")),
        ("valid_time", np.datetime64(end.replace(tzinfo=None), "ns")),
    ):
        if (
            name not in field.coords
            or field[name].ndim != 0
            or field[name].values[()] != expected_time
        ):
            raise ValueError("Native probability decoded cycle/valid time mismatch")
    if product["expected"]["gridType"] == "regular_ll":
        if field.shape != (attrs["GRIB_Nj"], attrs["GRIB_Ni"]):
            raise ValueError("Probability native grid dimensions disagree with decoded array")
        expected_lat = (
            attrs["GRIB_latitudeOfFirstGridPointInDegrees"]
            - np.arange(attrs["GRIB_Nj"]) * attrs["GRIB_jDirectionIncrementInDegrees"]
        )
        expected_lon = (
            attrs["GRIB_longitudeOfFirstGridPointInDegrees"]
            + np.arange(attrs["GRIB_Ni"]) * attrs["GRIB_iDirectionIncrementInDegrees"]
        )
        if not np.allclose(
            field.latitude.values, expected_lat, atol=1e-6, rtol=0
        ) or not np.allclose(
            ((field.longitude.values - expected_lon + 180) % 360) - 180,
            0,
            atol=1e-6,
            rtol=0,
        ):
            raise ValueError("Probability decoded geographic coordinates disagree with GRIB")
        field = field.assign_coords(longitude=((field.longitude + 180) % 360) - 180).sortby(
            ["latitude", "longitude"]
        )
        field = field.transpose("latitude", "longitude")
        x, y = np.asarray(field.longitude), np.asarray(field.latitude)
        if not np.allclose(np.diff(x), attrs["GRIB_iDirectionIncrementInDegrees"], atol=1e-8):
            raise ValueError("Probability longitude coordinate spacing disagrees with GRIB")
        if not np.allclose(np.diff(y), attrs["GRIB_jDirectionIncrementInDegrees"], atol=1e-8):
            raise ValueError("Probability latitude coordinate spacing disagrees with GRIB")
        crs = pyproj.CRS.from_epsg(4326)
    else:
        crs = build_lambert_conformal_crs(
            lov_degrees=attrs["GRIB_LoVInDegrees"],
            lad_degrees=attrs["GRIB_LaDInDegrees"],
            latin1_degrees=attrs["GRIB_Latin1InDegrees"],
            latin2_degrees=attrs["GRIB_Latin2InDegrees"],
            earth_radius_m=attrs["GRIB_radius"],
        )
        x, y = compute_projected_coordinates(
            crs,
            first_lat_degrees=attrs["GRIB_latitudeOfFirstGridPointInDegrees"],
            first_lon_degrees=attrs["GRIB_longitudeOfFirstGridPointInDegrees"],
            dx_m=attrs["GRIB_DxInMetres"],
            dy_m=attrs["GRIB_DyInMetres"],
            nx=attrs["GRIB_Nx"],
            ny=attrs["GRIB_Ny"],
        )
        field = field.transpose("y", "x")
        # Check the corners, edges and center against the projection. This validates
        # decoded array orientation without allocating another full native mesh.
        xi, yi = np.unique([0, len(x) // 2, len(x) - 1]), np.unique([0, len(y) // 2, len(y) - 1])
        expected_lat, expected_lon = compute_latlon_grid(crs, x=x[xi], y=y[yi])
        if (
            field.latitude.dims != ("y", "x")
            or field.longitude.dims != ("y", "x")
            or not np.allclose(
                field.latitude.values[np.ix_(yi, xi)], expected_lat, atol=1e-5, rtol=0
            )
            or not np.allclose(
                ((field.longitude.values[np.ix_(yi, xi)] - expected_lon + 180) % 360) - 180,
                0,
                atol=1e-5,
                rtol=0,
            )
        ):
            raise ValueError("Probability decoded geographic coordinates disagree with projection")
    native = np.asarray(field.values, dtype=np.float64)
    if native.shape != (len(y), len(x)) or not np.all(np.diff(x) > 0) or not np.all(np.diff(y) > 0):
        raise ValueError("Probability grid geometry/array shape mismatch")
    valid = np.isfinite(native) & (native >= 0) & (native <= 100)
    fraction = np.where(valid, native / 100, np.nan)
    event = {key: value for key, value in product.items() if key != "expected"}
    event.update(
        source_id=source_id,
        source_cycle=cycle.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        source_lead_hours=end_hour,
        interval_start=start.isoformat().replace("+00:00", "Z"),
        interval_end=end.isoformat().replace("+00:00", "Z"),
        valid_time=end.isoformat().replace("+00:00", "Z"),
        interval_closure="left_open_right_closed",
        native_unit="percent",
        unit="1",
        normalization="percent_divided_by_100_once",
        version={
            "adapter_contract": "native_probability_products_v1",
            "model_version": attrs.get("GRIB_modelVersion"),
            "generating_process_identifier": attrs["GRIB_generatingProcessIdentifier"],
            "grib_tables_version": attrs.get("GRIB_tablesVersion"),
            "grib_local_tables_version": attrs.get("GRIB_localTablesVersion"),
        },
        invalid_cell_count=int(np.count_nonzero(np.isfinite(native) & ~valid)),
        missing_cell_count=int(np.count_nonzero(~valid)),
        grib_keys={
            key.removeprefix("GRIB_"): value.item() if isinstance(value, np.generic) else value
            for key, value in attrs.items()
            if key.startswith("GRIB_")
        },
        ensemble_population={
            "method": "provider_native_probability_not_locally_recomputed",
            "member_count": None,
            "member_ids": None,
        },
    )
    result = xr.Dataset(
        {
            "probability": (("y", "x"), fraction, {"units": "1", "unit_id": "1"}),
            "native_probability": (("y", "x"), native, {"units": "%", "unit_id": "percent"}),
        },
        coords={"x": x, "y": y},
        attrs={"crs_wkt2": crs.to_wkt(), "source_id": source_id},
    )
    return result, crs, event
