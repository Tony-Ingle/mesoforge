"""Native interval thunder probabilities, without lightning-diagnostic conversion.

NBM's encoded probability parameter does not define a universal flash population
or event footprint. Preserve that uncertainty instead of borrowing a radius or
threshold from one component. Native one-, three- and six-hour events stay distinct.
"""

from __future__ import annotations

from copy import deepcopy
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
from mesoforge.guidance.sources.cloud import _CLOUD_READ_KEYS
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.ifs import IFS_RETRY_POLICY
from mesoforge.guidance.sources.precipitation_type import type_url
from mesoforge.guidance.sources.probabilistic import (
    _READ_KEYS,
    PRODUCTS,
    _same_object,
    normalized_native_grid,
)

NBM_DOCUMENTATION = "https://vlab.noaa.gov/web/mdl/nbm-weather-elements-v4.1"
NBM_INPUTS = "https://blend.mdl.nws.noaa.gov/nbm/html/1_hr_Probability_of_Thunderstorm_00.html"
NATIVE_PARAMETER = (
    "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-19.shtml"
)
EVENT_DEFINITION = {
    "id": "nbm_native_probability_of_thunder",
    "parameter": "TSTM",
    "physical_threshold": None,
    "threshold_status": "provider_defined_not_encoded",
}
SPATIAL_SUPPORT = {
    "kind": "provider_native_probability_grid",
    "geometry_status": "not_encoded",
    "radius_km": None,
    "note": "Native NBM thunder probability combines provider guidance; an exact event "
    "footprint is not encoded and cross-source spatial equivalence is unproven.",
}
SOURCES: dict[str, dict[str, Any]] = {
    f"NBM_{duration}H": {
        "source_id": f"NBM_{duration}H",
        "model": "NBM",
        "provider": "NOAA",
        "product": f"NBM CONUS core native {duration}-hour probability of thunder",
        "supported": True,
        "status": "active" if duration == 1 else "shadow",
        "active_weight": 1.0 if duration == 1 else 0.0,
        "active_eligible": duration == 1,
        "duration_hours": duration,
        "native_parameter": "TSTM",
        "native_unit": "percent",
        "native_factor_to_fraction": 0.01,
        "probability_method": "native_published_probability",
        "event_definition": deepcopy(EVENT_DEFINITION),
        "spatial_support": deepcopy(SPATIAL_SUPPORT),
        "documentation": NBM_DOCUMENTATION,
        "input_product_documentation": NBM_INPUTS,
        "parameter_documentation": NATIVE_PARAMETER,
        "definition_note": "Native provider probability of thunder; no application-defined "
        "flash-count, lightning-type or neighborhood threshold. The published event "
        "is preserved, not inferred from deterministic CAPE, LTNG, PoP or precipitation.",
    }
    for duration in (1, 3, 6)
}

# These inspected possibilities are not acquisition adapters. Incompatible or
# unbound products must not masquerade as equivalent NBM probability contributors.
INSPECTED_GUIDANCE: dict[str, dict[str, Any]] = {
    "GLMP_1H": {
        "model": "GLMP",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "product": "LAMP native one-hour total-lightning probability",
        "documentation": "https://vlab.noaa.gov/web/mdl/lamp-grib-convective-and-lightning",
        "product_inventory": "https://www.nco.ncep.noaa.gov/pmb/products/lmp/",
        "missing_reason": "A native probability product exists, but independent acquisition "
        "and event/spatial equivalence to the NBM native event are not bound in this adapter; "
        "the LAMP component inside NBM is not a separately acquired contributor.",
    },
    "HREF_CT_1H": {
        "model": "HREF",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "product": "Calibrated one-hour probability of at least one CG flash within 20 km",
        "documentation": "https://hwt.nssl.noaa.gov/sfe/2023/docs/HWT_SFE2023_operations_plan_v2.pdf",
        "missing_reason": "Published calibrated guidance has its own event definition; "
        "a compatible available provider binding has not been established. "
        "Raw ensemble lightning diagnostics are not this probability product.",
    },
    "HRRR_RAP_LIGHTNING": {
        "model": "HRRR/RAP",
        "supported": False,
        "status": "incompatible",
        "active_weight": 0.0,
        "product": "Retained native LTNG/LTNGSD diagnostics",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/hrrr/",
        "missing_reason": "Lightning/CAPE diagnostics are not probabilities; no conversion applied",
    },
    "GFS": {
        "model": "GFS",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "product": "Retained deterministic GFS pgrb2.0p25",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/gfs/",
        "inventory_evidence": {
            "source_cycle": "2026-09-11T12:00:00Z",
            "source_lead_hours": 7,
            "sha256": "0454b4909d3f55558c0f5574ff1c46b3be7d9540a162ab9032b3fe34086d499b",
            "finding": "CAPE and other convective diagnostics present; no native TSTM probability",
        },
        "missing_reason": "No native thunder probability in the inspected retained GFS product; "
        "deterministic CAPE/precipitation are not converted into probabilities",
    },
    "IFS": {
        "model": "IFS",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "product": "Official open-data deterministic IFS oper fc",
        "documentation": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
        "inventory_evidence": {
            "source_cycle": "2026-09-11T06:00:00Z",
            "source_lead_hours": 15,
            "sha256": "10d2b3d2346ca44c15317fad6e8ef196cb181bd0c65e7d1dc1d5ec91f656187e",
            "finding": "mucape present; no native lightning/thunder probability in this inventory",
        },
        "missing_reason": "No native thunder probability in the inspected deterministic IFS "
        "open-data product; convective diagnostics are not converted into probabilities",
    },
    "GEFS": {
        "model": "GEFS",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "product": "Existing GEFS probability adapter and NCO product catalog",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/gens/",
        "missing_reason": "The retained GEFS adapter supplies PQPF, not thunder probability. "
        "No matching native thunder event was established from the inspected catalog; "
        "CAPE/member precipitation fractions are not substituted",
    },
    "REFS_LIGHTNING": {
        "model": "REFS",
        "supported": False,
        "status": "incompatible",
        "active_weight": 0.0,
        "product": "Retained REFS parallel CONUS native LTNG diagnostic-threshold probability",
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/refs/",
        "inventory_evidence": {
            "source_cycle": "2026-09-11T12:00:00Z",
            "source_lead_hours": 7,
            "sha256": "ad3b16114625233b9cc5922311268375a4569de2b3b4ee845777541ab97e7830",
            "descriptor": "LTNG:entire atmosphere:7 hour fcst:prob >0.08:prob fcst 0/14",
        },
        "native_threshold": {"value": 0.08, "comparison": "gt", "unit": None},
        "missing_reason": "A native probability of exceeding the LTNG diagnostic threshold "
        "exists, but the inspected instantaneous descriptor does not establish the NBM "
        "hourly thunder event, threshold units, population or spatial support. "
        "No equivalence or hourly probability is inferred",
    },
    "ECMWF_ENS_LIGHTNING": {
        "model": "ECMWF_ENS",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "product": "ECMWF lightning-flash-density probability charts; inspected open-data ep feed",
        "documentation": "https://www.ecmwf.int/en/forecasts/datasets/probabilities-lightning-flash-density",
        "open_data_catalog": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
        "inventory_evidence": {
            "source_cycle": "2026-09-11T12:00:00Z",
            "sha256": "c53e5f6057153734a3e2182eb3c18f792a4451dfeddc7262a800770f8f1bc077",
            "finding": "Inspected public ensemble probability index has no lightning parameter",
        },
        "missing_reason": "ECMWF publishes ENS lightning-probability charts, but a compatible "
        "native lightning event is absent from the inspected open-data ep feed/catalog. "
        "No chart extraction, flash-density conversion or new data-access path is implemented",
    },
}
_THUNDER_READ_KEYS = tuple(
    sorted(
        {
            *_CLOUD_READ_KEYS,
            *_READ_KEYS,
            "numberOfTimeRanges",
            "typeOfTimeIncrement",
            "forecastTime",
            "indicatorOfUnitOfTimeRange",
        }
    )
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def thunder_url(source_id: str, cycle: datetime, lead: int) -> str:
    """Reuse the existing NBM core object without expanding its bounded lead range."""
    if source_id not in SOURCES:
        raise ValueError(f"Unsupported native thunder probability source: {source_id}")
    if type(lead) is not int or lead < SOURCES[source_id]["duration_hours"]:
        raise ValueError("Thunder probability requires a complete native forecast interval")
    return type_url("NBM", cycle, lead)


def selected_thunder_row(
    source_id: str, cycle: datetime, lead: int, payload: bytes, length: int
) -> tuple[IndexRow, int]:
    """Select the exact native thunder event interval, excluding coverage categories."""
    thunder_url(source_id, cycle, lead)
    duration = SOURCES[source_id]["duration_hours"]
    rows = parse_index_rows(payload.decode())
    descriptor = f":TSTM:surface:{lead - duration}-{lead} hour acc fcst:probability forecast"
    matches = [row for row in rows if row.descriptor == descriptor]
    if not matches:
        raise GribIndexError(f"{source_id} native thunder probability interval unavailable")
    if len(matches) != 1:
        raise ValueError(f"{source_id} native thunder probability inventory is ambiguous")
    row = matches[0]
    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise ValueError("Thunder probability inventory source cycle mismatch")
    _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
    return row, end


def acquire_thunder_lead(
    source_id: str,
    cycle: datetime,
    lead: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = thunder_url(source_id, cycle, lead)
    endpoint, policy, deadline = "noaa_aws", IFS_RETRY_POLICY, clock.now()
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
        raise FetchError("Thunder provider object needs positive Content-Length")
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
    row, end = selected_thunder_row(source_id, cycle, lead, index.payload, length)
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
        model="NBM",
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
            SelectedMessage("thunder_probability", row, row.byte_offset, end, fetched.payload),
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


def decode_thunder_lead(
    payload: bytes, source_id: str, cycle: datetime, lead: int
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Convert published percentages to fractions without modifying event semantics."""
    thunder_url(source_id, cycle, lead)
    duration = SOURCES[source_id]["duration_hours"]
    validate_grib_message_boundaries(payload, url=source_id, range_header="retained thunder")
    decoded = _decode_all(payload, read_keys=_THUNDER_READ_KEYS)
    candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
    if len(candidates) != 1:
        raise ValueError("Expected one native thunder probability per retained message")
    field = candidates[0]
    attrs = field.attrs
    expected = {
        **{
            key: value
            for key, value in PRODUCTS["NBM_6H"]["expected"].items()
            if key not in ("probabilityType", "parameterNumber", "typeOfGeneratingProcess")
        },
        "discipline": 0,
        "parameterCategory": 19,
        "parameterNumber": 2,
        "paramId": 3060,
        "typeOfLevel": "surface",
        "typeOfFirstFixedSurface": "sfc",
        "typeOfSecondFixedSurface": 255,
        "level": 0,
        "stepType": "accum",
        "startStep": lead - duration,
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "productDefinitionTemplateNumber": 8,
        "typeOfGeneratingProcess": 5,
        "typeOfStatisticalProcessing": 1,
        "lengthOfTimeRange": duration,
        "indicatorOfUnitForTimeRange": 1,
        "numberOfTimeRanges": 1,
        "numberOfMissingInStatisticalProcess": 0,
        "units": "%",
    }
    for key, value in expected.items():
        if attrs.get("GRIB_" + key) != value:
            raise ValueError(
                f"{source_id} thunder {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
            )
    valid = cycle + timedelta(hours=lead)
    for name, expected_time in (("time", cycle), ("valid_time", valid)):
        if (
            name not in field.coords
            or field[name].ndim
            or field[name].values[()] != np.datetime64(expected_time.replace(tzinfo=None), "ns")
        ):
            raise ValueError("Thunder probability decoded time mismatch")
    field, x, y, crs = normalized_native_grid(field)
    native = np.asarray(field.values, dtype=np.float64)
    if native.shape != (len(y), len(x)):
        raise ValueError("Native thunder probability grid shape mismatch")
    valid_cells = np.isfinite(native) & (native >= 0) & (native <= 100)
    variables = {
        "native_probability": (("y", "x"), native, {"units": "percent"}),
        "thunder_probability": (
            ("y", "x"),
            np.where(valid_cells, native * 0.01, np.nan),
            {"units": "1"},
        ),
    }
    grib_keys = {
        key[5:]: value.item() if isinstance(value, np.generic) else value
        for key, value in attrs.items()
        if key.startswith("GRIB_")
    }
    metadata = {
        **deepcopy(SOURCES[source_id]),
        "unit": "1",
        "source_cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(valid),
        "temporal_semantics": "interval_probability",
        "interval_start": _iso(valid - timedelta(hours=duration)),
        "interval_end": _iso(valid),
        "interval_closure": "left_open_right_closed",
        "missing_reasons": [],
        "invalid_cell_count": int(np.count_nonzero(np.isfinite(native) & ~valid_cells)),
        "missing_cell_count": int(np.count_nonzero(~valid_cells)),
        "grib_keys": grib_keys,
        "grib_threshold": {
            "probability_type": grib_keys.get("probabilityType"),
            "scale_factor_of_lower_limit": grib_keys.get("scaleFactorOfLowerLimit"),
            "scaled_value_of_lower_limit": grib_keys.get("scaledValueOfLowerLimit"),
            "scale_factor_of_upper_limit": grib_keys.get("scaleFactorOfUpperLimit"),
            "scaled_value_of_upper_limit": grib_keys.get("scaledValueOfUpperLimit"),
            "meaning": "Encoding audit only; not interpreted as a physical lightning threshold",
        },
        "member_population": {
            "expected": None,
            "available": None,
            "missing": None,
            "status": "not_encoded_in_native_probability",
        },
        "version": {
            "adapter_contract": "native_interval_thunder_probability_v1",
            "generating_process_identifier": expected["generatingProcessIdentifier"],
            "model_version": attrs.get("GRIB_modelVersion"),
        },
    }
    return (
        xr.Dataset(
            variables,
            coords={"x": x, "y": y},
            attrs={"crs_wkt2": crs.to_wkt(), "model": "NBM", "source_id": source_id},
        ),
        crs,
        metadata,
    )
