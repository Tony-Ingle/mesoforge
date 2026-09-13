"""Native flat-ice accretion and freezing-rain liquid remain distinct amounts.

This adapter does not implement FRAM, assign an ice/liquid ratio, infer icing
from p-type or temperature, or convert mass-equivalent encoding into thickness.
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
from mesoforge.guidance.sources import rap
from mesoforge.guidance.sources.cloud import _CLOUD_READ_KEYS
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords
from mesoforge.guidance.sources.ifs import IFS_RETRY_POLICY
from mesoforge.guidance.sources.precipitation_type import type_url
from mesoforge.guidance.sources.probabilistic import PRODUCTS, _same_object, normalized_native_grid
from mesoforge.guidance.sources.snowfall import _GRIDS

ICE = "flat_ice_accretion_mass_equivalent"
FREEZING_RAIN = "freezing_rain_liquid_equivalent"
NATIVE_PARAMETER_DOCUMENTATION = (
    "https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-1.shtml"
)
SOURCES: dict[str, dict[str, Any]] = {
    **{
        f"NBM_FICEAC_{duration}H": {
            "source_id": f"NBM_FICEAC_{duration}H",
            "model": "NBM",
            "provider": "NOAA",
            "product": f"NBM CONUS core native {duration}-hour flat ice accumulation (FRAM)",
            "supported": True,
            "status": "evidence",
            "active_weight": 0.0,
            "quantity_kind": ICE,
            "native_parameter": "FICEAC",
            "native_unit": "kg m**-2",
            "native_factor_to_canonical": 1.0,
            "temporal_support": "native_interval_accumulation",
            "duration_hours": duration,
            "accretion_geometry": "elevated_flat_surface",
            "method": "provider_FRAM",
            "definition_note": "Native provider FRAM flat ice accumulation in its published "
            "kg/m^2 mass-equivalent encoding; distinct from freezing-rain liquid, radial/line "
            "ice, ground ice and road icing. No density or thickness conversion applied.",
            "documentation": "https://vlab.noaa.gov/web/mdl/nbm-weather-elements-v4.1",
            "parameter_documentation": NATIVE_PARAMETER_DOCUMENTATION,
            "method_documentation": "https://doi.org/10.1175/WAF-D-15-0118.1",
        }
        for duration in (1, 6)
    },
    **{
        f"{model}_FRZR": {
            "source_id": f"{model}_FRZR",
            "model": model,
            "provider": "NOAA",
            "product": product,
            "supported": True,
            "status": "evidence",
            "active_weight": 0.0,
            "quantity_kind": FREEZING_RAIN,
            "native_parameter": "FRZR",
            "native_unit": "kg m**-2",
            "native_factor_to_canonical": 1.0,
            "temporal_support": "native_cycle_cumulative_accumulation",
            "duration_hours": None,
            "accretion_geometry": None,
            "method": "native_model_freezing_rain_liquid",
            "definition_note": "Native cumulative freezing-rain liquid amount since the "
            "source cycle; this is not accreted ice. No 1:1 liquid-to-ice mapping, "
            "ice/liquid ratio or p-type-conditioned QPF split is applied.",
            "documentation": f"https://www.nco.ncep.noaa.gov/pmb/products/{model.lower()}/",
            "parameter_documentation": NATIVE_PARAMETER_DOCUMENTATION,
        }
        for model, product in (("HRRR", "wrfsfc CONUS native FRZR"), ("RAP", "awp130pgrb FRZR"))
    },
    "GFS": {
        "source_id": "GFS",
        "model": "GFS",
        "provider": "NOAA",
        "product": "pgrb2.0p25",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "quantity_kind": ICE,
        "documentation": "https://www.nco.ncep.noaa.gov/pmb/products/gfs/",
        "missing_reason": "Inspected GFS product has categorical CFRZR, not native "
        "FICEAC/FRZR accumulation. Sea-ice fields are not freezing-rain ice accretion; "
        "no type/QPF or temperature conversion",
    },
    "IFS": {
        "source_id": "IFS",
        "model": "IFS",
        "provider": "ECMWF",
        "product": "official open-data deterministic IFS oper fc",
        "supported": False,
        "status": "unavailable",
        "active_weight": 0.0,
        "quantity_kind": ICE,
        "documentation": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
        "missing_reason": "No native ice-accretion/freezing-rain accumulation in the "
        "inspected IFS open-data product. ptype, total precipitation and sea-ice thickness "
        "remain separate; no substitute amount is derived",
    },
}
_ICE_READ_KEYS = tuple(sorted({*_CLOUD_READ_KEYS, "numberOfTimeRanges", "shortName", "name"}))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def ice_url(source_id: str, cycle: datetime, lead: int) -> str:
    """Use existing provider core/surface objects for exact native accumulated fields."""
    if source_id not in SOURCES or not SOURCES[source_id]["supported"]:
        raise ValueError(f"Unsupported native ice/freezing-rain source: {source_id}")
    source = SOURCES[source_id]
    minimum = source["duration_hours"] or 1
    if type(lead) is not int or lead < minimum:
        raise ValueError("Native ice/freezing-rain interval must have positive complete duration")
    return type_url(source["model"], cycle, lead)


def selected_ice_row(
    source_id: str, cycle: datetime, lead: int, payload: bytes, length: int
) -> tuple[IndexRow, int]:
    """Exclude probability/percentile, categorical, radial and incompatible-period products."""
    ice_url(source_id, cycle, lead)
    source = SOURCES[source_id]
    rows = (
        rap.physical_index_rows(payload)[0]
        if source["model"] == "RAP"
        else parse_index_rows(payload.decode())
    )
    start = lead - source["duration_hours"] if source["duration_hours"] else 0
    descriptors = {f":{source['native_parameter']}:surface:{start}-{lead} hour acc fcst:"}
    if source["duration_hours"] is None and lead % 24 == 0:
        # wgrib2 prints whole-day cumulative FRZR periods in days even when
        # the decoded GRIB bounds are hours. Admit only this exact equivalent.
        descriptors.add(f":FRZR:surface:0-{lead // 24} day acc fcst:")
    matches = [row for row in rows if row.descriptor in descriptors]
    if not matches:
        raise GribIndexError(f"{source_id} native accumulated amount unavailable")
    if len(matches) != 1:
        raise ValueError(f"{source_id} native accumulated amount inventory is ambiguous")
    row = matches[0]
    if row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
        raise ValueError("Ice/freezing-rain inventory source cycle mismatch")
    _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
    return row, end


def acquire_ice_lead(
    source_id: str,
    cycle: datetime,
    lead: int,
    *,
    transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2LeadAcquisition:
    url = ice_url(source_id, cycle, lead)
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
        raise FetchError("Ice provider object needs positive Content-Length")
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
    row, end = selected_ice_row(source_id, cycle, lead, index.payload, length)
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
        model=SOURCES[source_id]["model"],
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
            SelectedMessage("ice_amount", row, row.byte_offset, end, fetched.payload),
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


def decode_ice_lead(
    payload: bytes, source_id: str, cycle: datetime, lead: int
) -> tuple[xr.Dataset, pyproj.CRS, dict[str, Any]]:
    """Keep native accumulation geometry/quantity and water units without ice inference."""
    ice_url(source_id, cycle, lead)
    source = SOURCES[source_id]
    model = source["model"]
    start = lead - source["duration_hours"] if source["duration_hours"] else 0
    validate_grib_message_boundaries(payload, url=source_id, range_header="retained ice")
    decoded = _decode_all(payload, read_keys=_ICE_READ_KEYS)
    candidates = [_with_dataset_coords(a, d) for d in decoded for a in d.data_vars.values()]
    if len(candidates) != 1:
        raise ValueError("Expected one native ice/freezing-rain amount per retained message")
    field = candidates[0]
    attrs = field.attrs
    grid = (
        {
            key: value
            for key, value in PRODUCTS["NBM_6H"]["expected"].items()
            if key not in ("probabilityType", "parameterNumber")
        }
        if model == "NBM"
        else _GRIDS[model]
    )
    expected = {
        **grid,
        "centre": "kwbc",
        "discipline": 0,
        "parameterCategory": 1,
        "parameterNumber": 228 if model == "NBM" else 225,
        "typeOfLevel": "surface",
        "typeOfFirstFixedSurface": "sfc",
        "typeOfSecondFixedSurface": 255,
        "level": 0,
        "stepType": "accum",
        "startStep": start,
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "productDefinitionTemplateNumber": 8,
        "typeOfGeneratingProcess": 2,
        "typeOfStatisticalProcessing": 1,
        "lengthOfTimeRange": lead - start,
        "indicatorOfUnitForTimeRange": 1,
        "numberOfTimeRanges": 1,
        "numberOfMissingInStatisticalProcess": 0,
        "generatingProcessIdentifier": {"NBM": 104, "HRRR": 83, "RAP": 105}[model],
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 1 if model == "NBM" else 0,
    }
    for key, value in expected.items():
        if attrs.get("GRIB_" + key) != value:
            raise ValueError(
                f"{source_id} ice {key} mismatch: {attrs.get('GRIB_' + key)!r} != {value!r}"
            )
    raw_unit = attrs.get("GRIB_units")
    unit = raw_unit
    unit_resolution = "decoded_native_unit"
    if raw_unit == "unknown" and model == "NBM" and attrs.get("GRIB_paramId") == 0:
        # ecCodes lacks this NCEP local FICEAC entry. All native source,
        # parameter, surface, product-template, interval and grid bindings above
        # must match before consulting the published NOAA unit for 0/1/228.
        unit = "kg m**-2"
        unit_resolution = "official_noaa_ficeac_parameter_binding"
    if unit not in ("kg m**-2", "kg m-2"):
        raise ValueError(
            "Ice/freezing-rain native amount requires kg/m^2, not thickness/rate/probability"
        )
    valid = cycle + timedelta(hours=lead)
    for name, expected_time in (("time", cycle), ("valid_time", valid)):
        if (
            name not in field.coords
            or field[name].ndim
            or field[name].values[()] != np.datetime64(expected_time.replace(tzinfo=None), "ns")
        ):
            raise ValueError("Ice/freezing-rain decoded cycle/valid time mismatch")
    field, x, y, crs = normalized_native_grid(field)
    native = np.asarray(field.values, dtype=np.float64)
    if native.shape != (len(y), len(x)) or not np.all(np.diff(x) > 0) or not np.all(np.diff(y) > 0):
        raise ValueError("Native ice/freezing-rain grid shape/axes mismatch")
    valid_cells = np.isfinite(native) & (native >= 0)
    dataset = xr.Dataset(
        {
            "native_amount": (("y", "x"), native, {"units": unit}),
            "amount": (("y", "x"), np.where(valid_cells, native, np.nan), {"units": "kg/m^2"}),
        },
        coords={"x": x, "y": y},
        attrs={"crs_wkt2": crs.to_wkt(), "model": model, "source_id": source_id},
    )
    event = {
        **deepcopy(source),
        "source_cycle": _iso(cycle),
        "source_lead_hours": lead,
        "valid_time": _iso(valid),
        "temporal_semantics": "accumulation",
        "interval_start": _iso(cycle + timedelta(hours=start)),
        "interval_end": _iso(valid),
        "interval_closure": "left_open_right_closed",
        "duration_hours": lead - start,
        "native_unit": unit,
        "decoded_native_unit": raw_unit,
        "native_unit_resolution": unit_resolution,
        "native_unit_documentation": NATIVE_PARAMETER_DOCUMENTATION,
        "unit": "kg/m^2",
        "native_factor_to_canonical": 1.0,
        "spatial_support": "native_model_grid",
        "normalization": "native_mass_per_area_preserved; negative/nonfinite_unavailable",
        "missing_reasons": [],
        "invalid_cell_count": int(np.count_nonzero(np.isfinite(native) & ~valid_cells)),
        "missing_cell_count": int(np.count_nonzero(~valid_cells)),
        "version": {
            "adapter_contract": "native_ice_and_freezing_rain_amount_v1",
            "generating_process_identifier": expected["generatingProcessIdentifier"],
            "model_version": attrs.get("GRIB_modelVersion"),
            "grib_tables_version": attrs.get("GRIB_tablesVersion"),
            "grib_local_tables_version": attrs.get("GRIB_localTablesVersion"),
        },
        "grib_keys": {
            key[5:]: value.item() if isinstance(value, np.generic) else value
            for key, value in attrs.items()
            if key.startswith("GRIB_")
        },
    }
    return dataset, crs, event
