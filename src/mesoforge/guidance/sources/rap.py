"""Bounded RAP grid-130 temperature discovery, acquisition, and decoding.

RAP remains an optional shadow contributor. Its cycle is aligned to the active
forecast's valid times and never changes that forecast's reference or weights.
Only inventories and the selected complete 2-m temperature messages are fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import xarray as xr

from mesoforge.catalog.contributors import RAP_MODEL_DEFINITION as RAP_MODEL_DEFINITION
from mesoforge.catalog.sources import Phase2FieldContract, RetryPolicy
from mesoforge.common.errors import MesoForgeError
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    _fetch_selected,
    _head_full_object_length,
    resolve_available_at,
)
from mesoforge.guidance.http_fetch import FetchedObject, FetchError, fetch_with_retry, header
from mesoforge.guidance.index_parsing import (
    GribIndexError,
    IndexRow,
    parse_index_rows,
    select_field_row,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources.hrrr_phase2_decoding import (
    HrrrPhase2DecodeError,
    decode_selected_message,
)

RAP_PRODUCT = "awp130pgrb"
RAP_SCIENTIFIC_VERSION = "RAPv5"
# A documentation observation, not inferred from or attributed to a GRIB file.
RAP_DOCUMENTED_PRODUCTION_PACKAGE = "rap.v5.1.24"
RAP_VERSION_SOURCE = "https://www.nco.ncep.noaa.gov/pmb/codes/nwprod/"
RAP_ENDPOINT = "noaa_aws"
RAP_BASE_URL = "https://noaa-rap-pds.s3.amazonaws.com"
RAP_EXTENDED_CYCLE_HOURS = (3, 9, 15, 21)
RAP_REQUEST_INTERVAL_SECONDS = 0.5
RAP_CAPABILITIES = {
    "model": "RAP",
    "product": RAP_PRODUCT,
    "grid": "NCEP grid 130 (CONUS Lambert conformal)",
    "cycle_hours": tuple(range(24)),
    "ordinary_maximum_lead_hours": 21,
    "extended_cycle_hours": RAP_EXTENDED_CYCLE_HOURS,
    "extended_maximum_lead_hours": 51,
    "scientific_version": RAP_SCIENTIFIC_VERSION,
    "documented_production_package": RAP_DOCUMENTED_PRODUCTION_PACKAGE,
    "package_documentation_checked_at": "2026-09-10",
    "package_version_source": RAP_VERSION_SOURCE,
    "product_documentation": "https://www.nco.ncep.noaa.gov/pmb/products/rap/",
    "cycle_documentation": "https://registry.opendata.aws/noaa-rap/",
    "provider_mirror": "NOAA public AWS Open Data",
    "provider_endpoint": RAP_ENDPOINT,
    "bucket": "noaa-rap-pds",
    "bucket_region": "us-east-1",
    "public_source_url": RAP_BASE_URL,
}
RAP_RETRY_POLICY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=3,
    backoff_seconds=(1.0, 2.0, 4.0),
    retry_after_cap_seconds=60.0,
)
RAP_READ_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfLevel",
    "level",
    "stepType",
    "startStep",
    "endStep",
    "gridType",
    "Nx",
    "Ny",
    "DxInMetres",
    "DyInMetres",
    "latitudeOfFirstGridPointInDegrees",
    "longitudeOfFirstGridPointInDegrees",
    "LoVInDegrees",
    "Latin1InDegrees",
    "Latin2InDegrees",
    "LaDInDegrees",
    "units",
    "step",
    "dataDate",
    "dataTime",
    "validityDate",
    "validityTime",
    "iScansNegatively",
    "jScansPositively",
    "jPointsAreConsecutive",
    "alternativeRowScanning",
    "shapeOfTheEarth",
    "radius",
)
RAP_TEMPERATURE_CONTRACT = Phase2FieldContract(
    canonical_variable_id="air_temperature_2m",
    discipline=0,
    parameter_category=0,
    parameter_number=0,
    type_of_level="heightAboveGround",
    level=2.0,
    expected_unit_id="K",
)


def _utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("RAP cycles and reference time must be explicit UTC timestamps")
    if value.minute or value.second or value.microsecond:
        raise ValueError("RAP cycles and reference time must identify exact hours")
    return value.astimezone(UTC)


def maximum_lead(cycle: datetime) -> int:
    return 51 if _utc_hour(cycle).hour in RAP_EXTENDED_CYCLE_HOURS else 21


def build_grib_url(*, cycle: datetime, forecast_hour: int) -> str:
    cycle = _utc_hour(cycle)
    if type(forecast_hour) is not int or not 0 <= forecast_hour <= maximum_lead(cycle):
        raise ValueError("RAP source lead is outside this cycle's supported range")
    return (
        f"{RAP_BASE_URL}/"
        f"rap.{cycle:%Y%m%d}/rap.t{cycle:%H}z.{RAP_PRODUCT}f{forecast_hour:02d}.grib2"
    )


class RapTemperatureUnavailableError(GribIndexError):
    """A valid inventory contains no requested instantaneous 2-m temperature."""


def _selected_temperature(
    payload: bytes, *, cycle: datetime, forecast_hour: int
) -> tuple[tuple[IndexRow, ...], IndexRow]:
    # RAP packs some wind components into one physical GRIB message. wgrib2
    # identifies their fields as e.g. 12.1/12.2 at the same byte offset. Collapse
    # those physical-message boundaries for the shared byte-range engine while
    # retaining all original inventory bytes and the selected original row.
    groups: list[list[tuple[int | None, IndexRow]]] = []
    for line in payload.decode("utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split(":")
        number = re.fullmatch(r"([1-9]\d*)(?:\.([1-9]\d*))?", fields[0])
        if len(fields) < 4 or number is None:
            raise GribIndexError(f"Malformed RAP inventory row: {line!r}")
        physical = int(number[1])
        submessage = int(number[2]) if number[2] is not None else None
        try:
            offset = int(fields[1])
        except ValueError as exc:
            raise GribIndexError(f"Malformed RAP inventory offset: {line!r}") from exc
        row = IndexRow(physical, offset, line)
        if physical == len(groups) + 1:
            if submessage not in (None, 1):
                raise GribIndexError("RAP compound message must start at submessage 1")
            groups.append([(submessage, row)])
        elif groups and physical == len(groups):
            prior_submessage, prior_row = groups[-1][-1]
            if (
                prior_submessage is None
                or submessage != prior_submessage + 1
                or offset != prior_row.byte_offset
            ):
                raise GribIndexError("RAP compound message fields must be sequential at one offset")
            groups[-1].append((submessage, row))
        else:
            raise GribIndexError("RAP physical message numbers must be sequential")

    # Validate increasing physical byte offsets through the existing parser.
    # Projecting only message-number syntax leaves field/cycle text unchanged.
    representatives = tuple(group[0][1] for group in groups)
    projected = "\n".join(
        f"{row.message_number}:" + row.line.split(":", 1)[1] for row in representatives
    )
    parse_index_rows(projected)
    step = "anl" if forecast_hour == 0 else f"{forecast_hour} hour fcst"
    logical_rows = tuple(row for group in groups for _, row in group)
    selector = rf":TMP:2 m above ground:{step}:$"
    if not any(re.search(selector, row.descriptor) for row in logical_rows):
        raise RapTemperatureUnavailableError(
            f"RAP temperature selector {selector!r} matched zero rows"
        )
    row = select_field_row(logical_rows, selector)
    selected_group = groups[row.message_number - 1]
    if len(selected_group) != 1 or selected_group[0][0] is not None:
        raise GribIndexError("Selected RAP temperature must be a standalone physical GRIB message")
    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise GribIndexError("RAP inventory temperature source cycle does not match the request")
    return representatives, row


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
    url = build_grib_url(cycle=cycle, forecast_hour=forecast_hour) + ".idx"
    sleeper.sleep(RAP_REQUEST_INTERVAL_SECONDS)
    return fetch_with_retry(
        transport,
        clock,
        sleeper,
        method="get",
        urls_by_endpoint=[(RAP_ENDPOINT, url)],
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
    )


@dataclass(frozen=True, slots=True)
class RapCycleProbe:
    cycle: datetime
    source_leads: tuple[int, ...]
    available_leads: tuple[int, ...]
    missing_hours: dict[int, str]
    index_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RapCycleSelection:
    cycle: datetime | None
    source_leads: tuple[int, ...]
    available_leads: tuple[int, ...]
    missing_hours: dict[int, str]
    probes: tuple[RapCycleProbe, ...]
    reason: str


def discover_rap_cycle(
    *,
    target_reference_time: datetime,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    lookback_hours: int = 24,
    cycle_override: datetime | None = None,
    retry_policy: RetryPolicy = RAP_RETRY_POLICY,
) -> RapCycleSelection:
    """Find complete extended guidance first, otherwise explicitly partial shadow.

    Inventories, including the exact temperature field/cycle/step, establish
    observed availability. Nominal schedules only bound which URLs to inspect.
    A selected inventory is checked again during acquisition and decoding; a
    subsequent provider failure must remain missing, never change active output.
    """
    target = _utc_hour(target_reference_time)
    now = clock.now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Discovery clock must be timezone aware")
    if type(lookback_hours) is not int or not 1 <= lookback_hours <= 48:
        raise ValueError("RAP discovery lookback_hours must be within 1..48")
    latest = min(target, now.astimezone(UTC).replace(minute=0, second=0, microsecond=0))
    if cycle_override is not None:
        cycle_override = _utc_hour(cycle_override)
        if cycle_override > latest:
            raise ValueError("RAP source cycle cannot be after the target reference or execution")
        candidates = [cycle_override]
    else:
        candidates = [latest - timedelta(hours=age) for age in range(lookback_hours + 1)]

    probes: dict[datetime, RapCycleProbe] = {}

    def probe(cycle: datetime) -> RapCycleProbe:
        if cycle in probes:
            return probes[cycle]
        age = int((target - cycle).total_seconds() / 3600)
        leads = tuple(age + hour for hour in range(1, 37))
        available: list[int] = []
        missing: dict[int, str] = {}
        urls: list[str] = []
        for hour, lead in enumerate(leads, start=1):
            if not 0 <= lead <= maximum_lead(cycle):
                missing[hour] = f"RAP source lead {lead} exceeds cycle coverage"
                continue
            urls.append(build_grib_url(cycle=cycle, forecast_hour=lead) + ".idx")
            try:
                fetched = _fetch_index(
                    cycle=cycle,
                    forecast_hour=lead,
                    transport=transport,
                    clock=clock,
                    sleeper=sleeper,
                    retry_policy=retry_policy,
                    cycle_deadline=now - timedelta(microseconds=1),
                )
                _selected_temperature(fetched.payload, cycle=cycle, forecast_hour=lead)
            except (FetchError, RapTemperatureUnavailableError) as exc:
                # The shared fetch helper currently exposes terminal status in
                # its message. Access/rate rejection applies to the provider,
                # not one cycle; scanning more cycles would multiply the load.
                if isinstance(exc, FetchError) and (
                    "non-retryable HTTP" in str(exc) or "last_status=429" in str(exc)
                ):
                    raise
                missing[hour] = f"RAP temperature inventory unavailable: {exc}"
            else:
                available.append(lead)
        result = RapCycleProbe(cycle, leads, tuple(available), missing, tuple(urls))
        probes[cycle] = result
        return result

    def selection(result: RapCycleProbe, reason: str) -> RapCycleSelection:
        return RapCycleSelection(
            result.cycle,
            result.source_leads,
            result.available_leads,
            result.missing_hours,
            tuple(probes.values()),
            reason,
        )

    if cycle_override is not None:
        result = probe(cycle_override)
        return selection(
            result, "Explicit RAP cycle override; unavailable shadow hours are explicit"
        )

    # An older complete extended cycle wins over fresher, shorter/partial data.
    for cycle in candidates:
        if cycle.hour not in RAP_EXTENDED_CYCLE_HOURS:
            continue
        if int((target - cycle).total_seconds() / 3600) + 36 > 51:
            continue
        result = probe(cycle)
        if not result.missing_hours:
            return selection(
                result, "Newest observed complete RAP extended cycle for all 36 valid times"
            )

    # Shadow-only fallback: freshest cycle with any usable temperature guidance.
    # Never combine cycles or renormalize the active recipe to fill these gaps.
    for cycle in candidates:
        result = probe(cycle)
        if result.available_leads:
            return selection(
                result, "No complete RAP cycle; newest partial shadow with explicit missing hours"
            )
    return RapCycleSelection(
        None,
        (),
        (),
        {hour: "No usable RAP temperature cycle in bounded discovery" for hour in range(1, 37)},
        tuple(probes.values()),
        "No usable RAP temperature guidance; active forecast is unchanged",
    )


def acquire_rap_lead(
    *,
    cycle: datetime,
    forecast_hour: int,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    cycle_deadline: datetime,
    retry_policy: RetryPolicy = RAP_RETRY_POLICY,
) -> Phase2LeadAcquisition:
    """Acquire and retain one complete native-grid temperature GRIB message."""
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
    rows, selected = _selected_temperature(index.payload, cycle=cycle, forecast_hour=forecast_hour)
    sleeper.sleep(RAP_REQUEST_INTERVAL_SECONDS)
    length, etag, modified = _head_full_object_length(
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        endpoint=RAP_ENDPOINT,
        grib_url=grib_url,
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
    )
    sleeper.sleep(RAP_REQUEST_INTERVAL_SECONDS)
    messages, attempts, completed, etag, modified = _fetch_selected(
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        endpoint=RAP_ENDPOINT,
        grib_url=grib_url,
        rows=rows,
        selected_rows=[("air_temperature_2m", selected)],
        retry_policy=retry_policy,
        cycle_deadline=cycle_deadline,
        full_object_content_length=length,
        full_object_etag=etag,
        full_object_last_modified=modified,
    )
    index_modified = header(index.headers, "Last-Modified")
    return Phase2LeadAcquisition(
        model="rap",
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
        forecast_hour=forecast_hour,
        endpoint=RAP_ENDPOINT,
        resolved_grib_url=grib_url,
        resolved_index_url=index.resolved_url,
        index_payload=index.payload,
        index_attempts=index.attempts,
        index_completed_at=index.completed_at,
        selected_messages=tuple(messages),
        grib_attempts=tuple(attempts),
        grib_completed_at=completed,
        full_object_etag=etag,
        full_object_last_modified=modified,
        full_object_content_length=length,
        index_available_at=resolve_available_at(index_modified, retrieved_at=index.completed_at),
        grib_available_at=resolve_available_at(modified, retrieved_at=completed),
        index_last_modified=index_modified,
    )


class RapDecodeError(MesoForgeError):
    """Selected RAP temperature does not meet its scientific source contract."""


def decode_temperature_message(
    payload: bytes, *, cycle: datetime, forecast_hour: int
) -> xr.DataArray:
    cycle = _utc_hour(cycle)
    build_grib_url(cycle=cycle, forecast_hour=forecast_hour)  # Validate cycle-specific lead range.
    try:
        field = decode_selected_message(
            payload,
            contract=RAP_TEMPERATURE_CONTRACT,
            read_keys=RAP_READ_KEYS,
            forecast_hour=forecast_hour,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
        )
    except HrrrPhase2DecodeError as exc:
        raise RapDecodeError(str(exc).replace("HRRR", "RAP")) from exc
    attrs = field.attrs
    # This adapter acquires full grid130 messages. Read geometry from each one;
    # reject accidentally routed native/full-domain or different-resolution files.
    expected = {
        "Nx": 451,
        "Ny": 337,
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "shapeOfTheEarth": 6,
        "radius": 6371229,
    }
    for key, value in expected.items():
        if attrs.get(f"GRIB_{key}") != value:
            raise RapDecodeError(
                f"RAP grid130 {key} mismatch: expected {value}, got {attrs.get(f'GRIB_{key}')!r}"
            )
    if field.shape != (337, 451):
        raise RapDecodeError("RAP decoded temperature shape differs from grid130 dimensions")
    return field
