"""Native new-snow amounts, provider SLR, and bounded RAP thermal evidence.

Snowpack depth/density and snowfall water equivalent are separate quantities.
This adapter does not calculate a snowfall ratio or choose a delivered amount.
"""

from __future__ import annotations

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
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.sources import ifs, rap
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.probabilistic import PRODUCTS, _same_object, normalized_native_grid
from mesoforge.guidance.sources.snowfall import _GRIDS, _KEYS

LEVELS_HPA = tuple(range(500, 1001, 25))
UNIT_SOURCE = "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-1.shtml"
GSL_SOURCE = "https://repository.library.noaa.gov/view/noaa/72271/noaa_72271_DS1.pdf"
SOURCES: dict[str, dict[str, Any]] = {
    **{
        model: {
            "model": model,
            "provider": "NOAA",
            "product": product,
            "supported": True,
            "native_quantity": "new_snowfall_amount",
            "native_parameter": "ASNOW",
            "temporal_support": "cumulative_hourly",
            "hydrometeor_scope": "snow",
            "amount_method": "provider_native_variable_density_snow_accumulation",
            "method_source": GSL_SOURCE,
            "method_note": "GSL-76 describes current HRRR/RAP variable-density snowfall "
            "from snow fallout, excluding graupel; retain source model/version "
            "and native processing",
            **(
                {"profile_method": "native_isobaric_temperature_500_to_1000_hpa_every_25_hpa"}
                if model == "RAP"
                else {}
            ),
        }
        for model, product in (("HRRR", "wrfsfc CONUS"), ("RAP", "awp130pgrb"))
    },
    "NBM": {
        "model": "NBM",
        "provider": "NOAA",
        "product": "core CONUS",
        "supported": True,
        "native_quantity": "new_snowfall_amount",
        "native_parameter": "ASNOW",
        "temporal_support": "hourly_accumulation",
        "hydrometeor_scope": "snow_and_sleet",
        "amount_method": "provider_native_NBM_deterministic_snow_amount",
        "method_source": "https://vlab.noaa.gov/documents/6609493/7858320/"
        "Blend_Winter_v5.0-Configuration_and_Technical_Details.pdf",
        "method_note": "NBM snowfall includes its provider sleet treatment; "
        "native SNOWLR is separate instantaneous evidence, not an interval-average ratio",
    },
    "GFS": {
        "model": "GFS",
        "provider": "NOAA",
        "product": "pgrb2.0p25",
        "supported": False,
        "native_quantity": "new_snowfall_amount",
        "missing_reason": "Inspected GFS guidance has snowpack SNOD/WEASD, "
        "not native new-snow depth accumulation or fresh-snow SLR",
    },
    "IFS": {
        "model": "IFS",
        "provider": "ECMWF",
        "product": "official IFS open data",
        "supported": False,
        "native_quantity": "new_snowfall_amount",
        "missing_reason": "IFS sf is snowfall water equivalent; sd/rsn are snowpack state/density, "
        "not native newly accumulated snow depth or fresh-snow SLR",
    },
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def amount_url(model: str, cycle: datetime, lead: int) -> str:
    if model not in SOURCES or not SOURCES[model]["supported"]:
        raise ValueError(f"Unsupported native snowfall-amount source: {model}")
    if (
        cycle.tzinfo is None
        or cycle.utcoffset() != timedelta(0)
        or cycle.minute
        or cycle.second
        or cycle.microsecond
        or type(lead) is not int
        or lead < 0
    ):
        raise ValueError("Snowfall amount needs an exact UTC cycle and nonnegative integer lead")
    if model == "RAP":
        return rap.build_grib_url(cycle=cycle, forecast_hour=lead)
    if model == "HRRR":
        if lead > (48 if cycle.hour % 6 == 0 else 18):
            raise ValueError("HRRR snowfall amount lead exceeds source coverage")
        return (
            f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{cycle:%Y%m%d}/conus/"
            f"hrrr.t{cycle:%H}z.wrfsfcf{lead:02d}.grib2"
        )
    if not 1 <= lead <= 36:
        raise ValueError("Bounded NBM hourly snowfall amount requires leads 1..36")
    return (
        f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{cycle:%Y%m%d}/{cycle:%H}/core/"
        f"blend.t{cycle:%H}z.core.f{lead:03d}.co.grib2"
    )


def _descriptors(model: str, lead: int, include_profile: bool) -> dict[str, str]:
    start = lead - 1 if model == "NBM" else 0
    result = {"amount": f":ASNOW:surface:{start}-{lead} hour acc fcst:"}
    if model == "NBM":
        result["native_slr"] = f":SNOWLR:surface:{lead} hour fcst:"
    if include_profile:
        if model != "RAP":
            raise ValueError("Bounded native temperature profiles are supported only for RAP")
        result.update(
            temperature_2m=f":TMP:2 m above ground:{lead} hour fcst:",
            surface_pressure=f":PRES:surface:{lead} hour fcst:",
        )
        result.update(
            {
                f"temperature_{level}hpa": f":TMP:{level} mb:{lead} hour fcst:"
                for level in LEVELS_HPA
            }
        )
    return result


def selected_amount_rows(
    model: str,
    cycle: datetime,
    lead: int,
    payload: bytes,
    length: int,
    *,
    include_profile: bool = False,
) -> list[tuple[str, IndexRow, int]]:
    """Exact native products only; optional absence is distinct from duplicate identity."""
    amount_url(model, cycle, lead)
    descriptors = _descriptors(model, lead, include_profile)
    if model == "RAP":
        rows, _ = rap.physical_index_rows(payload)
    else:
        rows = parse_index_rows(payload.decode())
    result = []
    for name, descriptor in descriptors.items():
        aliases = {descriptor}
        start = lead - 1 if model == "NBM" else 0
        # wgrib2 represents some exact 24-hour multiples in days. Match both
        # accumulation bounds, not merely the interval duration or ending lead.
        if name == "amount" and lead > 0 and start % 24 == 0 and lead % 24 == 0:
            aliases.add(f":ASNOW:surface:{start // 24}-{lead // 24} day acc fcst:")
        matches = [row for row in rows if row.descriptor in aliases]
        if not matches:
            continue
        if len(matches) != 1:
            raise ValueError(f"Ambiguous {model} native {name} inventory")
        row = matches[0]
        if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
            raise ValueError(f"{model} native {name} inventory cycle mismatch")
        _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
        result.append((name, row, end))
    if not result:
        raise GribIndexError(f"{model} requested native snowfall amount/SLR/profile unavailable")
    return result


def acquire_amount_lead(
    model: str,
    cycle: datetime,
    lead: int,
    *,
    include_profile: bool = False,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = amount_url(model, cycle, lead)
    _descriptors(model, lead, include_profile)
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
        raise FetchError("Snowfall amount provider object needs positive Content-Length")
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
    selected = selected_amount_rows(
        model, cycle, lead, index.payload, length, include_profile=include_profile
    )
    messages, attempts = [], list(head.attempts)
    for name, row, end in selected:
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
        messages.append(SelectedMessage(name, row, row.byte_offset, end, fetched.payload))
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


def _field_contract(name: str, model: str, lead: int) -> tuple[dict[str, Any], str]:
    expected: dict[str, Any] = {
        "discipline": 0,
        "typeOfLevel": "surface",
        "level": 0,
        "stepType": "instant",
        "startStep": lead,
        "endStep": lead,
        "productDefinitionTemplateNumber": 0,
    }
    if name == "amount":
        start = lead - 1 if model == "NBM" else 0
        expected.update(
            parameterCategory=1,
            stepType="accum",
            startStep=start,
            productDefinitionTemplateNumber=8,
            typeOfStatisticalProcessing=1,
            lengthOfTimeRange=lead - start,
            indicatorOfUnitForTimeRange=1,
            numberOfMissingInStatisticalProcess=0,
        )
        return expected, "m"
    if name == "native_slr" and model == "NBM":
        expected.update(parameterCategory=1, parameterNumber=233)
        return expected, "kg kg-1"
    if model != "RAP":
        raise ValueError(f"Unexpected {model} native snowfall input {name}")
    if name == "surface_pressure":
        expected.update(parameterCategory=3, parameterNumber=0)
        return expected, "Pa"
    expected.update(parameterCategory=0, parameterNumber=0)
    if name == "temperature_2m":
        expected.update(typeOfLevel="heightAboveGround", level=2)
        return expected, "K"
    match = re.fullmatch(r"temperature_(\d+)hpa", name)
    if match and int(match[1]) in LEVELS_HPA:
        expected.update(typeOfLevel="isobaricInhPa", level=int(match[1]))
        return expected, "K"
    raise ValueError(f"Unexpected native snowfall input {name}")


def decode_amount_lead(
    payloads: dict[str, bytes],
    model: str,
    cycle: datetime,
    lead: int,
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Keep provider amounts, optional native ratio and a complete native RAP profile."""
    amount_url(model, cycle, lead)
    if not payloads:
        raise ValueError("Native amount decoding requires at least one retained field")
    grid = (
        _GRIDS[model]
        if model != "NBM"
        else {
            k: v
            for k, v in PRODUCTS["NBM_6H"]["expected"].items()
            if k not in ("probabilityType", "parameterNumber")
        }
    )
    shared = {
        **grid,
        "centre": "kwbc",
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "generatingProcessIdentifier": {"HRRR": 83, "RAP": 105, "NBM": 104}[model],
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 1 if model == "NBM" else 0,
    }
    native, keys, raw_units = {}, {}, {}
    axes = None
    for name, payload in payloads.items():
        contract, unit = _field_contract(name, model, lead)
        validate_grib_message_boundaries(
            payload, url=model, range_header="retained snowfall amount"
        )
        decoded = _decode_all(payload, read_keys=_KEYS)
        candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
        if len(candidates) != 1:
            raise ValueError("Expected one native field per snowfall-amount message")
        field = candidates[0]
        attrs = field.attrs
        if name == "amount" and attrs.get("GRIB_parameterNumber") not in (29, 57):
            raise ValueError("Expected native ASNOW amount, not snowpack/SWE/rate")
        for key, value in {**shared, **contract}.items():
            if attrs.get("GRIB_" + key) != value:
                raise ValueError(
                    f"{model} {name} {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
                )
        raw_unit = str(attrs.get("GRIB_units"))
        accepted = (
            ("m", "unknown")
            if name == "amount"
            else (("kg kg-1", "kg kg**-1", "kg/kg", "unknown") if name == "native_slr" else (unit,))
        )
        if raw_unit not in accepted:
            raise ValueError(f"{model} {name} native units mismatch: {raw_unit!r}")
        for coord, expected_time in (
            ("time", cycle),
            ("valid_time", cycle + timedelta(hours=lead)),
        ):
            if (
                coord not in field.coords
                or field[coord].ndim
                or field[coord].values[()]
                != np.datetime64(expected_time.replace(tzinfo=None), "ns")
            ):
                raise ValueError("Native snowfall amount/profile decoded time mismatch")
        field, x, y, crs = normalized_native_grid(field)
        values = np.asarray(field.values, dtype=np.float64)
        if values.shape != (len(y), len(x)):
            raise ValueError("Native snowfall amount/profile shape mismatch")
        if axes is not None and (
            not np.array_equal(x, axes[0]) or not np.array_equal(y, axes[1]) or crs != axes[2]
        ):
            raise ValueError("Native snowfall amount/profile grid mismatch")
        axes = x, y, crs
        native[name] = values
        keys[name] = {
            k[5:]: v.item() if isinstance(v, np.generic) else v
            for k, v in attrs.items()
            if k.startswith("GRIB_")
        }
        raw_units[name] = raw_unit
    assert axes is not None
    x, y, crs = axes
    amount = native.get("amount", np.full((len(y), len(x)), np.nan))
    variables: dict[str, Any] = {
        "native_amount": (
            ("y", "x"),
            amount,
            {"units": "m", "raw_grib_unit": raw_units.get("amount", "unavailable")},
        ),
        "amount": (
            ("y", "x"),
            np.where(np.isfinite(amount) & (amount >= 0), amount, np.nan),
            {"units": "m"},
        ),
    }
    valid = cycle + timedelta(hours=lead)
    identity = {"source_cycle": _iso(cycle), "source_lead_hours": lead, "valid_time": _iso(valid)}
    metadata = {
        **SOURCES[model],
        **identity,
        "unit": "m",
        "native_unit": "m",
        "raw_grib_unit": raw_units.get("amount", "unavailable"),
        "declared_native_unit": "m",
        "unit_definition_source": UNIT_SOURCE,
        "unit_factor_to_m": 1.0,
        "temporal_semantics": "accumulation",
        "interval_closure": "left_open_right_closed",
        "interval_start": _iso(cycle + timedelta(hours=lead - 1 if model == "NBM" else 0)),
        "interval_end": _iso(valid),
        "spatial_support": "native_model_grid",
        "amount_available": "amount" in native,
        "missing_reasons": []
        if "amount" in native
        else ["Native ASNOW amount unavailable in retained input"],
        "grib_keys": keys.get("amount", {}),
        "grib_fields": keys,
        "invalid_cell_count": int(np.count_nonzero(np.isfinite(amount) & (amount < 0))),
        "missing_cell_count": int(np.count_nonzero(~np.isfinite(amount) | (amount < 0))),
        "version": {
            "adapter_contract": "native_snowfall_amount_v1",
            "generating_process_identifier": shared["generatingProcessIdentifier"],
            "model_version": next(iter(keys.values())).get("modelVersion"),
        },
    }
    if "native_slr" in native:
        variables["native_slr"] = (
            ("y", "x"),
            native["native_slr"],
            {"units": "1", "raw_grib_unit": raw_units["native_slr"]},
        )
    if model == "NBM":
        metadata["native_slr"] = {
            **identity,
            "unit": "1",
            "native_unit": raw_units.get("native_slr", "unavailable"),
            "declared_native_unit": "kg kg-1",
            "unit_definition_source": UNIT_SOURCE,
            "temporal_semantics": "instantaneous",
            "interval_start": None,
            "interval_end": None,
            "status": "available" if "native_slr" in native else "unavailable",
            "missing_reasons": [] if "native_slr" in native else ["Native SNOWLR unavailable"],
            "grib_keys": keys.get("native_slr", {}),
            "method": "provider_native_SNOWLR; not applied to another source or interval",
        }
    if model == "RAP":
        profile_names = [f"temperature_{level}hpa" for level in LEVELS_HPA]
        required = (*profile_names, "temperature_2m", "surface_pressure")
        missing = [name for name in required if name not in native]
        for name, unit in (("temperature_2m", "K"), ("surface_pressure", "Pa")):
            if name in native:
                variables[name] = (("y", "x"), native[name], {"units": unit})
        if not missing:
            variables["temperature_profile"] = (
                ("level", "y", "x"),
                np.stack([native[n] for n in profile_names]),
                {"units": "K"},
            )
        metadata["profile"] = {
            **identity,
            "complete": not missing,
            "status": "available" if not missing else "unavailable",
            "missing_reasons": [
                f"Native RAP profile field unavailable: {name}" for name in missing
            ],
            "level_hpa": list(LEVELS_HPA),
            "temperature_unit": "K",
            "surface_pressure_unit": "Pa",
            "temporal_semantics": "instantaneous",
            "interval_start": None,
            "interval_end": None,
            "grib_keys": {k: v for k, v in keys.items() if k in required},
            "method": "native_isobaric_profile; below-ground levels not masked by provider adapter",
        }
    coords: dict[str, Any] = {"x": x, "y": y}
    if "temperature_profile" in variables:
        coords["level"] = ("level", np.asarray(LEVELS_HPA), {"units": "hPa"})
    return (
        xr.Dataset(variables, coords=coords, attrs={"crs_wkt2": crs.to_wkt(), "model": model}),
        crs,
        metadata,
    )
