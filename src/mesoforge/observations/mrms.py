"""Offline MRMS analysis-reference contracts; no forecast verification or scoring.

Template 4.0 does not encode an accumulation. The QPE-only timing rule below is
an owner-approved NOAA product contract, never a decoder inference.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import math
import zlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import eccodes
import jcs
from pyproj import Geod

from mesoforge.common.errors import MesoForgeError

SCHEMA_VERSION = "mesoforge.mrms-product-extraction.v1"
EXTRACTION_POLICY = "mrms.nearest-native-gridpoint.wgs84.v1"
_TABLE = "https://www.nssl.noaa.gov/projects/mrms/operational/tables.php"
_TIMING = "https://vlab.noaa.gov/web/wdtd/-/multi-sensor-qpe"
_MAX_MESSAGE_BYTES = 256 * 1024 * 1024
_GEOD = Geod(ellps="WGS84")


class MRMSContractError(MesoForgeError):
    """Unreadable, contradictory or unsupported source; never a numeric zero."""


@dataclass(frozen=True, slots=True)
class MRMSProductContract:
    contract_id: str
    product: str
    category: int
    parameter: int
    units: str
    physical_meaning: str
    accumulation_seconds: int | None = None
    provider: str = "NOAA/NCEP MRMS"
    discipline: int = 209
    center: int = 161
    missing_sentinel: int = -1
    no_coverage_sentinel: int = -3
    metadata_source: str = _TABLE


PRODUCT_CONTRACTS = MappingProxyType(
    {
        "MultiSensor_QPE_01H_Pass2": MRMSProductContract(
            "mrms.multisensor-qpe-01h-pass2.v1",
            "MultiSensor_QPE_01H_Pass2",
            6,
            37,
            "mm",
            "multi-sensor analysed liquid-equivalent precipitation accumulation",
            3600,
        ),
        "GaugeInflIndex_01H_Pass2": MRMSProductContract(
            "mrms.gauge-influence-01h-pass2.v1",
            "GaugeInflIndex_01H_Pass2",
            8,
            17,
            "1",
            "gauge-influence support index; not probability or total analysis confidence",
        ),
        "RadarAccumulationQualityIndex_01H": MRMSProductContract(
            "mrms.radar-accumulation-quality-01h.v1",
            "RadarAccumulationQualityIndex_01H",
            8,
            10,
            "1",
            "radar-accumulation quality support index; not total analysis confidence",
        ),
    }
)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _message(raw: bytes) -> bytes:
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise MRMSContractError("MRMS source exceeds the bounded message size")
    try:
        if raw.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
                message = source.read(_MAX_MESSAGE_BYTES + 1)
        else:
            message = raw
    except (OSError, EOFError, zlib.error) as exc:
        raise MRMSContractError("Malformed compressed MRMS source") from exc
    if (
        len(message) < 20
        or len(message) > _MAX_MESSAGE_BYTES
        or message[:4] != b"GRIB"
        or message[7] != 2
        or int.from_bytes(message[8:16]) != len(message)
        or message[-4:] != b"7777"
    ):
        raise MRMSContractError("Expected exactly one complete GRIB2 message")
    sections: list[int] = []
    offset = 16
    while offset < len(message) - 4:
        size = int.from_bytes(message[offset : offset + 4])
        if size < 5 or offset + size > len(message) - 4:
            raise MRMSContractError("Malformed GRIB section length")
        sections.append(message[offset + 4])
        offset += size
    if offset != len(message) - 4 or sections not in (
        [1, 3, 4, 5, 6, 7],
        [1, 2, 3, 4, 5, 6, 7],
    ):
        raise MRMSContractError("Expected a single readable GRIB field")
    return message


def _time(handle: int, prefix: str = "") -> datetime:
    keys = ["year", "month", "day", "hour", "minute", "second"]
    if prefix:
        keys = [f"{key}OfEndOfOverallTimeInterval" for key in keys]
    year, month, day, hour, minute, second = (int(eccodes.codes_get(handle, key)) for key in keys)
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _duration(handle: int, unit_key: str, value_key: str) -> int:
    units = {0: 60, 1: 3600, 2: 86400, 10: 10800, 11: 21600, 12: 43200, 13: 1}
    unit = int(eccodes.codes_get(handle, unit_key))
    if unit not in units:
        raise MRMSContractError("Unsupported encoded statistical time unit")
    return int(eccodes.codes_get(handle, value_key)) * units[unit]


def _temporal(handle: int, contract: MRMSProductContract) -> dict[str, Any]:
    indicated = _time(handle)
    if indicated.minute != 0 or indicated.second != 0:
        raise MRMSContractError("MRMS hourly product time is not the top of an hour")
    template = int(eccodes.codes_get(handle, "productDefinitionTemplateNumber"))
    encoded: dict[str, Any] | None = None
    if template == 0:
        if int(eccodes.codes_get(handle, "forecastTime")) != 0:
            raise MRMSContractError("MRMS reference time contradicts a nonzero forecast step")
    elif template == 8 and contract.accumulation_seconds is not None:
        end = _time(handle, "end")
        duration = _duration(handle, "indicatorOfUnitForTimeRange", "lengthOfTimeRange")
        start = indicated + timedelta(
            seconds=_duration(handle, "indicatorOfUnitOfTimeRange", "forecastTime")
        )
        if (
            int(eccodes.codes_get(handle, "numberOfTimeRange")) != 1
            or int(eccodes.codes_get(handle, "typeOfStatisticalProcessing")) != 1
            or duration != contract.accumulation_seconds
            or end != indicated
            or start != end - timedelta(seconds=duration)
        ):
            raise MRMSContractError("Encoded accumulation contradicts the MRMS product contract")
        encoded = {"interval_start": _iso(start), "interval_end": _iso(end)}
    else:
        raise MRMSContractError("Unsupported MRMS statistical product template")
    qpe = contract.accumulation_seconds is not None
    return {
        "product_time": _iso(indicated),
        "interval_start": _iso(indicated - timedelta(hours=1)) if qpe else None,
        "interval_end": _iso(indicated) if qpe else None,
        "duration_seconds": contract.accumulation_seconds,
        "closure": "(start,end]" if qpe else None,
        "semantics_source": _TIMING if qpe else _TABLE,
        "semantics_origin": "documented_product_contract" if qpe else "hourly_support_association",
        "encoded_statistical_bounds": encoded,
        "encoded_reference_time": _iso(indicated),
        "update_frequency_seconds": 3600 if qpe else None,
        "documented_latency_seconds_approximate": 3600 if qpe else None,
    }


def _grid(handle: int) -> dict[str, Any]:
    expected = {
        "gridType": "regular_ll",
        "gridDefinitionTemplateNumber": 0,
        "shapeOfTheEarth": 2,
        "scanningMode": 0,
        "bitmapPresent": 0,
    }
    if any(eccodes.codes_get(handle, key) != value for key, value in expected.items()):
        raise MRMSContractError("Unsupported MRMS native grid layout or bitmap")
    ni, nj = (int(eccodes.codes_get(handle, key)) for key in ("Ni", "Nj"))
    north, west, south, east, dx, dy = (
        float(eccodes.codes_get(handle, key))
        for key in (
            "latitudeOfFirstGridPointInDegrees",
            "longitudeOfFirstGridPointInDegrees",
            "latitudeOfLastGridPointInDegrees",
            "longitudeOfLastGridPointInDegrees",
            "iDirectionIncrementInDegrees",
            "jDirectionIncrementInDegrees",
        )
    )
    if (
        not 1 <= ni <= 7000
        or not 1 <= nj <= 3500
        or ni * nj != int(eccodes.codes_get(handle, "numberOfDataPoints"))
        or ni * nj != int(eccodes.codes_get(handle, "numberOfValues"))
        or not math.isclose(dx, 0.01, abs_tol=1e-9)
        or not math.isclose(dy, 0.01, abs_tol=1e-9)
        or not 20.004997 <= south <= north <= 54.995003
        or not 230.004997 <= west <= east <= 299.995003
        or not math.isclose(south, north - (nj - 1) * dy, abs_tol=3e-6)
        or not math.isclose(east, west + (ni - 1) * dx, abs_tol=3e-6)
        or not math.isclose((north - 20.005) / dy, round((north - 20.005) / dy), abs_tol=3e-4)
        or not math.isclose((west - 230.005) / dx, round((west - 230.005) / dx), abs_tol=3e-4)
    ):
        raise MRMSContractError("Unsupported MRMS CONUS 0.01-degree grid characteristics")
    result = {
        "layout": "mrms.conus.regular-ll.0p01.v1",
        "ni": ni,
        "nj": nj,
        "latitude_first": north,
        "longitude_first": west - 360.0,
        "latitude_last": south,
        "longitude_last": east - 360.0,
        "spacing_longitude_degrees": dx,
        "spacing_latitude_degrees": dy,
        "scanning_mode": 0,
        "earth_shape_code": 2,
        "is_native_aligned_subset": ni != 7000 or nj != 3500,
    }
    result["identity_sha256"] = hashlib.sha256(jcs.canonicalize(result)).hexdigest()
    return result


def _metadata(handle: int, raw: bytes, message: bytes, product: str) -> dict[str, Any]:
    try:
        contract = PRODUCT_CONTRACTS[product]
    except KeyError as exc:
        raise MRMSContractError(f"Unsupported MRMS product: {product}") from exc
    expected = {
        "centre": contract.center,
        "subCentre": 0,
        "discipline": contract.discipline,
        "parameterCategory": contract.category,
        "parameterNumber": contract.parameter,
        "tablesVersion": 255,
        "localTablesVersion": 1,
        "significanceOfReferenceTime": 3,
    }
    if any(eccodes.codes_get_long(handle, key) != value for key, value in expected.items()):
        raise MRMSContractError("GRIB identity does not match the approved MRMS product")
    decoded_units = str(eccodes.codes_get(handle, "units"))
    accepted_units = (
        {"mm"} if contract.units == "mm" else {"1", "dimensionless", "non-dim", "Numeric"}
    )
    if decoded_units not in accepted_units | {"unknown"}:
        raise MRMSContractError("Decoded units contradict the MRMS product contract")
    temporal = _temporal(handle, contract)
    grib: dict[str, Any] = dict(expected)
    for key in ("productDefinitionTemplateNumber", "packingType", "bitmapPresent"):
        grib[key] = eccodes.codes_get(handle, key)
    grib["decoded_units"] = decoded_units
    grib["units_authority"] = _TABLE
    grib["algorithm_version"] = None
    return {
        "schema_version": SCHEMA_VERSION,
        "product_contract": asdict(contract),
        "product_time": temporal["product_time"],
        "temporal": temporal,
        "grib": grib,
        "grid": _grid(handle),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw),
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "message_bytes": len(message),
    }


def _selected_cell(grid: dict[str, Any], latitude: float, longitude: float) -> dict[str, Any]:
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise MRMSContractError("Non-finite forecast coordinate")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise MRMSContractError("Invalid forecast coordinate")
    row = (grid["latitude_first"] - latitude) / grid["spacing_latitude_degrees"]
    column = (longitude - grid["longitude_first"]) / grid["spacing_longitude_degrees"]
    if not -0.5 <= row <= grid["nj"] - 0.5 or not -0.5 <= column <= grid["ni"] - 0.5:
        raise MRMSContractError("Forecast coordinate is outside retained native-grid support")
    candidates = []
    for j in {max(0, min(grid["nj"] - 1, index)) for index in (math.floor(row), math.ceil(row))}:
        for i in {
            max(0, min(grid["ni"] - 1, index)) for index in (math.floor(column), math.ceil(column))
        }:
            lat = grid["latitude_first"] - j * grid["spacing_latitude_degrees"]
            lon = grid["longitude_first"] + i * grid["spacing_longitude_degrees"]
            _, _, distance = _GEOD.inv(longitude, latitude, lon, lat)
            candidates.append((distance, j * grid["ni"] + i, j, i, lat, lon))
    minimum = min(item[0] for item in candidates)
    distance, index, j, i, lat, lon = min(
        (item for item in candidates if abs(item[0] - minimum) <= 1e-7), key=lambda item: item[1]
    )
    return {
        "policy_id": EXTRACTION_POLICY,
        "forecast_coordinate": {"latitude": latitude, "longitude": longitude},
        "grid_index": index,
        "row": j,
        "column": i,
        "native_coordinate": {"latitude": lat, "longitude": lon},
        "distance_m": distance,
        "distance_geodesic": "WGS84",
        "tie_break": "lowest native scanning index within 1e-7 metre numerical tie",
        "spatial_meaning": "analysed QPE/support at nearest native grid point; not a point gauge",
    }


def _read(raw: bytes, product: str, coordinate: tuple[float, float] | None) -> dict[str, Any]:
    message = _message(raw)
    handle = None
    try:
        handle = eccodes.codes_new_from_message(message)
        if handle is None:
            raise MRMSContractError("Unreadable GRIB message")
        result = _metadata(handle, raw, message, product)
        # Metadata-only reads must also establish that the packed data are readable.
        # Decode a single element; never retain/copy the entire source grid here.
        if coordinate is None:
            eccodes.codes_get_double_element(handle, "values", 0)
        if coordinate is not None:
            selected = _selected_cell(result["grid"], *coordinate)
            value = float(
                eccodes.codes_get_double_element(handle, "values", selected["grid_index"])
            )
            if not math.isfinite(value) or (value < 0 and value not in (-1, -3)):
                raise MRMSContractError(
                    "Malformed MRMS value outside documented sentinel semantics"
                )
            state = (
                "missing"
                if value == -1
                else "no_coverage"
                if value == -3
                else "zero"
                if value == 0
                else "positive"
            )
            result["extraction"] = selected
            result["value"] = {
                "state": state,
                "value": value if value >= 0 else None,
                "units": PRODUCT_CONTRACTS[product].units,
                "raw_value": value,
            }
        return result
    except MRMSContractError:
        raise
    except Exception as exc:
        raise MRMSContractError("Malformed or unreadable MRMS GRIB metadata/data") from exc
    finally:
        if handle is not None:
            eccodes.codes_release(handle)


def inspect_mrms(raw: bytes, *, product: str) -> dict[str, Any]:
    """Validate one raw/compressed message and return compact deterministic metadata."""
    return _read(raw, product, None)


def extract_mrms(raw: bytes, *, product: str, latitude: float, longitude: float) -> dict[str, Any]:
    """Extract one native value without interpolation, network or storage access."""
    return _read(raw, product, (latitude, longitude))


def validate_support_alignment(qpe: dict[str, Any], support: dict[str, Any]) -> None:
    """Require shared time, grid and target; do not equate physical/statistical meaning."""
    if (
        qpe["product_contract"]["product"] != "MultiSensor_QPE_01H_Pass2"
        or support["product_contract"]["product"]
        not in {"GaugeInflIndex_01H_Pass2", "RadarAccumulationQualityIndex_01H"}
        or qpe["product_time"] != support["product_time"]
        or qpe["grid"] != support["grid"]
        or qpe["extraction"] != support["extraction"]
    ):
        raise MRMSContractError("MRMS support evidence is not aligned with the QPE extraction")
