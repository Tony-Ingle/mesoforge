"""ECMWF IFS public 0.25-degree temperature, at published native valid times.

This optional shadow adapter uses the operational deterministic/control product,
not AIFS or perturbed ensemble members. It never interpolates forecast times.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import xarray as xr

from mesoforge.catalog.sources import Phase2FieldContract, RetryPolicy
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    SelectedMessage,
    _head_full_object_length,
    resolve_available_at,
)
from mesoforge.guidance.http_fetch import (
    FetchedObject,
    FetchError,
    fetch_with_range,
    fetch_with_retry,
    header,
    validate_grib_message_boundaries,
)
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources.gfs_decoding import (
    GfsDecodeError,
    _assert_instantaneous,
    _decode_all,
    _matches_contract,
    _with_dataset_coords,
)

IFS_ENDPOINT = "ecmwf_aws"
IFS_BASE_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
IFS_PRODUCT = "ifs/0p25/oper/fc"
IFS_SCIENTIFIC_VERSION = "IFS cycle 50r1"
IFS_VERSION_SOURCE = (
    "https://confluence.ecmwf.int/spaces/FCST/pages/567162191/Implementation+of+IFS+Cycle+50r1"
)
IFS_CAPABILITIES = {
    "model": "IFS",
    "product": IFS_PRODUCT,
    "provider_endpoint": IFS_ENDPOINT,
    "public_source_url": IFS_BASE_URL,
    "bucket": "ecmwf-forecasts",
    "bucket_region": "eu-central-1",
    "grid": "Published regular latitude/longitude 0.25 degree",
    "cycle_hours": (0, 6, 12, 18),
    "native_step_hours": 3,
    "local_adapter_maximum_lead_hours": 90,
    "scientific_version": IFS_SCIENTIFIC_VERSION,
    "expected_generating_process_identifier": 161,
    "validated_grib_identity": {
        "centre": "ecmf",
        "marsClass": "od",
        "marsStream": "oper",
        "marsType": "fc",
        "modelName": "IFS",
        "modelVersion": "cy50r1",
        "generatingProcessIdentifier": 161,
        "paramId": 167,
        "typeOfLevel": "heightAboveGround",
        "level": 2,
        "units": "K",
        "stepType": "instant",
    },
    "version_source": IFS_VERSION_SOURCE,
    "product_documentation": (
        "https://confluence.ecmwf.int/spaces/DAC/pages/272310539/"
        "ECMWF+open+data+real-time+forecasts+from+IFS+and+AIFS"
    ),
    "licence": "CC-BY-4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://apps.ecmwf.int/datasets/licences/general/",
    "attribution": (
        "This service is based on data and products of the European Centre for "
        "Medium-Range Weather Forecasts (ECMWF)."
    ),
    "modification": "Temperature extracted from published IFS guidance; no time interpolation.",
}
IFS_RETRY_POLICY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=3,
    backoff_seconds=(1.0, 2.0, 4.0),
    retry_after_cap_seconds=60.0,
)
IFS_READ_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfLevel",
    "level",
    "units",
    "stepType",
    "step",
    "startStep",
    "endStep",
    "dataDate",
    "dataTime",
    "validityDate",
    "validityTime",
    "gridType",
    "Ni",
    "Nj",
    "iDirectionIncrementInDegrees",
    "jDirectionIncrementInDegrees",
    "latitudeOfFirstGridPointInDegrees",
    "longitudeOfFirstGridPointInDegrees",
    "latitudeOfLastGridPointInDegrees",
    "longitudeOfLastGridPointInDegrees",
    "iScansNegatively",
    "jScansPositively",
    "jPointsAreConsecutive",
    "alternativeRowScanning",
    "shapeOfTheEarth",
    "radius",
    "centre",
    "subCentre",
    "marsClass",
    "marsStream",
    "marsType",
    "modelName",
    "modelVersion",
    "generatingProcessIdentifier",
    "typeOfGeneratingProcess",
    "typeOfProcessedData",
    "productionStatusOfProcessedData",
    "paramId",
    "tablesVersion",
)
IFS_TEMPERATURE_CONTRACT = Phase2FieldContract(
    canonical_variable_id="air_temperature_2m",
    discipline=0,
    parameter_category=0,
    parameter_number=0,
    type_of_level="heightAboveGround",
    level=2.0,
    expected_unit_id="K",
)
NO_NATIVE_GUIDANCE = "IFS has no native guidance at this valid time; published every 3 hours"


def _utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("IFS source cycles and target must be explicit UTC timestamps")
    if value.minute or value.second or value.microsecond:
        raise ValueError("IFS source cycles and target must identify exact hours")
    return value.astimezone(UTC)


def build_grib_url(*, cycle: datetime, forecast_hour: int) -> str:
    cycle = _utc_hour(cycle)
    if cycle.hour not in (0, 6, 12, 18):
        raise ValueError("IFS source cycles must be 00/06/12/18 UTC")
    if type(forecast_hour) is not int or forecast_hour not in range(0, 91, 3):
        raise ValueError("This IFS adapter supports native three-hour leads from 0 through 90")
    return (
        f"{IFS_BASE_URL}/{cycle:%Y%m%d}/{cycle:%H}z/ifs/0p25/oper/"
        f"{cycle:%Y%m%d%H}0000-{forecast_hour}h-oper-fc.grib2"
    )


def build_index_url(*, cycle: datetime, forecast_hour: int) -> str:
    return (
        build_grib_url(cycle=cycle, forecast_hour=forecast_hour).removesuffix(".grib2") + ".index"
    )


class IfsIndexError(MesoForgeError):
    """Invalid or ambiguous ECMWF JSON-lines inventory."""


class IfsTemperatureUnavailableError(IfsIndexError):
    """The inventory contains no native instantaneous two-metre temperature."""


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IfsIndexError(f"Duplicate IFS inventory key {key!r}")
        result[key] = value
    return result


def selected_temperature(
    payload: bytes, *, cycle: datetime, forecast_hour: int
) -> tuple[IndexRow, int]:
    """Return the original JSON row and its exclusive physical byte-range end."""
    build_index_url(cycle=cycle, forecast_hour=forecast_hour)
    candidates: list[tuple[IndexRow, int, dict[str, Any]]] = []
    previous_end = 0
    try:
        lines = [line for line in payload.decode("utf-8").splitlines() if line.strip()]
        if not lines:
            raise IfsIndexError("Empty IFS inventory")
        for number, line in enumerate(lines, start=1):
            record = json.loads(line, object_pairs_hook=_unique_keys)
            if not isinstance(record, dict):
                raise IfsIndexError("Every IFS inventory line must be a JSON object")
            offset, length = record.get("_offset"), record.get("_length")
            if type(offset) is not int or type(length) is not int or offset < 0 or length < 20:
                raise IfsIndexError(
                    "IFS inventory requires nonnegative offset and GRIB byte length"
                )
            if offset < previous_end:
                raise IfsIndexError("IFS inventory byte ranges overlap or are unordered")
            previous_end = offset + length
            if record.get("param") == "2t":
                candidates.append((IndexRow(number, offset, line), previous_end, record))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IfsIndexError(f"Malformed IFS JSON-lines inventory: {exc}") from exc
    if not candidates:
        raise IfsTemperatureUnavailableError("IFS inventory has no 2t temperature field")
    if len(candidates) != 1:
        raise IfsIndexError("Ambiguous IFS inventory: multiple 2t fields")
    row, end, record = candidates[0]
    expected = {
        "domain": "g",
        "date": cycle.strftime("%Y%m%d"),
        "time": cycle.strftime("%H00"),
        "expver": "0001",
        "class": "od",
        "type": "fc",
        "stream": "oper",
        "step": str(forecast_hour),
        "levtype": "sfc",
        "param": "2t",
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise IfsIndexError(f"IFS temperature inventory {key} mismatch: expected {value!r}")
    if "number" in record or record.get("model", "ifs") != "ifs":
        raise IfsIndexError("IFS shadow requires the deterministic/control IFS product")
    return row, end


def _fetch_index(
    *,
    cycle: datetime,
    forecast_hour: int,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    retry_policy: RetryPolicy,
    cycle_deadline: datetime,
) -> FetchedObject:
    sleeper.sleep(0.5)
    return fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="get",
        urls_by_endpoint=[
            (IFS_ENDPOINT, build_index_url(cycle=cycle, forecast_hour=forecast_hour))
        ],
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
    )


@dataclass(frozen=True, slots=True)
class IfsCycleProbe:
    cycle: datetime
    source_leads: tuple[int, ...]
    native_leads: tuple[int, ...]
    available_leads: tuple[int, ...]
    missing_hours: dict[int, str]
    index_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IfsCycleSelection:
    cycle: datetime | None
    source_leads: tuple[int, ...]
    native_leads: tuple[int, ...]
    available_leads: tuple[int, ...]
    missing_hours: dict[int, str]
    probes: tuple[IfsCycleProbe, ...]
    reason: str


def discover_ifs_cycle(
    *,
    target_reference_time: datetime,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    lookback_hours: int = 24,
    cycle_override: datetime | None = None,
    retry_policy: RetryPolicy = IFS_RETRY_POLICY,
) -> IfsCycleSelection:
    """Prefer the freshest observed complete set of native slots within target1..36."""
    target = _utc_hour(target_reference_time)
    now = clock.now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("IFS discovery clock must be timezone aware")
    if type(lookback_hours) is not int or not 1 <= lookback_hours <= 48:
        raise ValueError("IFS lookback_hours must be within 1..48")
    latest = min(target, now.astimezone(UTC).replace(minute=0, second=0, microsecond=0))
    if cycle_override is not None:
        cycle_override = _utc_hour(cycle_override)
        build_grib_url(cycle=cycle_override, forecast_hour=0)
        if cycle_override > latest or target - cycle_override > timedelta(hours=54):
            raise ValueError("IFS override must be at/before target and execution, with leads <=90")
        cycles = [cycle_override]
    else:
        cycles = [
            cycle
            for age in range(lookback_hours + 1)
            if (cycle := latest - timedelta(hours=age)).hour in (0, 6, 12, 18)
            and target - cycle <= timedelta(hours=54)
        ]
    probes: list[IfsCycleProbe] = []

    def selection(probe: IfsCycleProbe, reason: str) -> IfsCycleSelection:
        return IfsCycleSelection(
            probe.cycle,
            probe.source_leads,
            probe.native_leads,
            probe.available_leads,
            probe.missing_hours,
            tuple(probes),
            reason,
        )

    for cycle in cycles:
        age = int((target - cycle).total_seconds() / 3600)
        leads = tuple(age + hour for hour in range(1, 37))
        native = tuple(lead for lead in leads if lead % 3 == 0)
        missing = {hour: NO_NATIVE_GUIDANCE for hour, lead in enumerate(leads, 1) if lead % 3}
        available: list[int] = []
        urls: list[str] = []
        for lead in native:
            urls.append(build_index_url(cycle=cycle, forecast_hour=lead))
            try:
                index = _fetch_index(
                    cycle=cycle,
                    forecast_hour=lead,
                    transport=transport,
                    clock=clock,
                    sleeper=sleeper,
                    retry_policy=retry_policy,
                    cycle_deadline=now - timedelta(microseconds=1),
                )
                selected_temperature(index.payload, cycle=cycle, forecast_hour=lead)
            except (FetchError, IfsTemperatureUnavailableError) as exc:
                if isinstance(exc, FetchError) and (
                    "non-retryable HTTP" in str(exc) or "last_status=429" in str(exc)
                ):
                    raise
                missing[lead - age] = f"IFS native temperature unavailable: {exc}"
            else:
                available.append(lead)
        probe = IfsCycleProbe(cycle, leads, native, tuple(available), missing, tuple(urls))
        probes.append(probe)
        if cycle_override is not None:
            return selection(
                probe, "Explicit IFS cycle; native gaps and unavailable slots remain missing"
            )
        if probe.available_leads == probe.native_leads:
            return selection(
                probe, "Newest complete IFS native three-hour guidance for the target window"
            )
    for probe in probes:
        if probe.available_leads:
            return selection(probe, "No complete IFS native cycle; newest partial shadow guidance")
    return IfsCycleSelection(
        None,
        (),
        (),
        (),
        {hour: "No usable IFS cycle in bounded discovery" for hour in range(1, 37)},
        tuple(probes),
        "IFS shadow unavailable; active forecast unchanged",
    )


def acquire_ifs_lead(
    *,
    cycle: datetime,
    forecast_hour: int,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    cycle_deadline: datetime,
    retry_policy: RetryPolicy = IFS_RETRY_POLICY,
) -> Phase2LeadAcquisition:
    """Retain exact JSON inventory and the complete 2t message selected by offset/length."""
    cycle = _utc_hour(cycle)
    grib_url = build_grib_url(cycle=cycle, forecast_hour=forecast_hour)
    index = _fetch_index(
        cycle=cycle,
        forecast_hour=forecast_hour,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
    )
    row, end = selected_temperature(index.payload, cycle=cycle, forecast_hour=forecast_hour)
    sleeper.sleep(0.5)
    length, etag, modified = _head_full_object_length(
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        endpoint=IFS_ENDPOINT,
        grib_url=grib_url,
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
    )
    if end > length:
        raise IfsIndexError("IFS selected byte range exceeds the provider's full object length")
    sleeper.sleep(0.5)
    fetched = fetch_with_range(
        transport,
        clock,
        sleeper,
        endpoint=IFS_ENDPOINT,
        url=grib_url,
        range_header=f"bytes={row.byte_offset}-{end - 1}",
        byte_start=row.byte_offset,
        byte_end=end,
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
        expected_length=end - row.byte_offset,
        full_object_length=length,
    )
    index_modified = header(index.headers, "Last-Modified")
    etag = etag or header(fetched.headers, "ETag")
    modified = modified or header(fetched.headers, "Last-Modified")
    return Phase2LeadAcquisition(
        model="ifs",
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
        forecast_hour=forecast_hour,
        endpoint=IFS_ENDPOINT,
        resolved_grib_url=grib_url,
        resolved_index_url=index.resolved_url,
        index_payload=index.payload,
        index_attempts=index.attempts,
        index_completed_at=index.completed_at,
        selected_messages=(
            SelectedMessage("air_temperature_2m", row, row.byte_offset, end, fetched.payload),
        ),
        grib_attempts=fetched.attempts,
        grib_completed_at=fetched.completed_at,
        full_object_etag=etag,
        full_object_last_modified=modified,
        full_object_content_length=length,
        index_available_at=resolve_available_at(index_modified, retrieved_at=index.completed_at),
        grib_available_at=resolve_available_at(modified, retrieved_at=fetched.completed_at),
        index_last_modified=index_modified,
    )


class IfsDecodeError(MesoForgeError):
    """Selected bytes are not the supported instantaneous IFS temperature product."""


def decode_temperature_message(
    payload: bytes, *, cycle: datetime, forecast_hour: int
) -> xr.DataArray:
    """Reuse existing geographic temperature/time checks, adding IFS model identity."""
    build_grib_url(cycle=cycle, forecast_hour=forecast_hour)
    validate_grib_message_boundaries(
        payload, url="retained IFS temperature", range_header="retained"
    )
    datasets = _decode_all(payload, read_keys=IFS_READ_KEYS)
    fields = [
        _with_dataset_coords(field, dataset)
        for dataset in datasets
        for field in dataset.data_vars.values()
    ]
    if len(fields) != 1 or not _matches_contract(fields[0], IFS_TEMPERATURE_CONTRACT):
        raise IfsDecodeError("Expected exactly one IFS instantaneous 2-m temperature field")
    field = fields[0]
    try:
        _assert_instantaneous(
            field,
            IFS_TEMPERATURE_CONTRACT,
            forecast_hour=forecast_hour,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
        )
    except GfsDecodeError as exc:
        raise IfsDecodeError(str(exc).replace("GFS", "IFS")) from exc
    expected = {
        "centre": "ecmf",
        "subCentre": 0,
        "marsClass": "od",
        "marsStream": "oper",
        "marsType": "fc",
        "modelName": "IFS",
        "modelVersion": "cy50r1",
        "generatingProcessIdentifier": 161,
        "typeOfGeneratingProcess": 2,
        "typeOfProcessedData": "fc",
        "productionStatusOfProcessedData": 0,
        "paramId": 167,
        "Ni": 1440,
        "Nj": 721,
        "iDirectionIncrementInDegrees": 0.25,
        "jDirectionIncrementInDegrees": 0.25,
        "iScansNegatively": 0,
        "jScansPositively": 0,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "shapeOfTheEarth": 6,
        "radius": 6371229,
        "latitudeOfFirstGridPointInDegrees": 90.0,
        "latitudeOfLastGridPointInDegrees": -90.0,
    }
    for key, value in expected.items():
        if field.attrs.get(f"GRIB_{key}") != value:
            raise IfsDecodeError(
                f"IFS {key} mismatch: expected {value!r}, got {field.attrs.get(f'GRIB_{key}')!r}"
            )
    if field.dims != ("latitude", "longitude") or field.shape != (721, 1440):
        raise IfsDecodeError("IFS temperature must retain the published global 0.25-degree axes")
    longitude = np.asarray(field.longitude.values, dtype=np.float64)
    latitude = np.asarray(field.latitude.values, dtype=np.float64)
    first_lon = float(field.attrs["GRIB_longitudeOfFirstGridPointInDegrees"])
    expected_lon = (first_lon + np.arange(1440) * 0.25) % 360
    if (
        not np.array_equal(latitude, 90.0 - np.arange(721) * 0.25)
        or not np.allclose(longitude % 360, expected_lon, atol=1e-8, rtol=0)
        or float(field.attrs["GRIB_longitudeOfLastGridPointInDegrees"]) % 360 != expected_lon[-1]
    ):
        raise IfsDecodeError("IFS decoded coordinates disagree with native GRIB geometry")
    return field
