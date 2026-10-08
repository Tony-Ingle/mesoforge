"""Native extended-window inventory/GRIB contracts; no temporal redistribution.

This supplements the existing per-provider scientific decoders. It selects only
plain deterministic records from the pinned object, and retains the encoded QPF
event even when it is longer than one hour.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import numpy as np
import xarray as xr

from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.catalog.sources import GfsSourceSettings, HrrrPhase2SourceSettings, NbmSourceSettings
from mesoforge.guidance.http_fetch import validate_grib_message_boundaries
from mesoforge.guidance.index_parsing import (
    IndexRow,
    compute_message_byte_range,
    parse_index_rows,
)
from mesoforge.guidance.sources import gfs_decoding, hrrr_phase2_decoding, ifs, nbm_decoding, rap
from mesoforge.guidance.sources.cloud import _CLOUD_READ_KEYS
from mesoforge.guidance.sources.gfs_decoding import _decode_all, _with_dataset_coords

TEMPERATURE = "air_temperature_2m"
QPF = "liquid_equivalent_precipitation_amount_1h"
CLOUD = "cloud_area_fraction"
POP = "probability_of_precipitation_1h"
_FRAGMENTS = {
    "dew_point_temperature_2m": "DPT:2 m above ground",
    "eastward_wind_10m": "UGRD:10 m above ground",
    "northward_wind_10m": "VGRD:10 m above ground",
    "wind_speed_10m": "WIND:10 m above ground",
    "wind_from_direction_10m": "WDIR:10 m above ground",
    "wind_gust_10m": "GUST:surface",
}
_IFS_PARAMS = {
    "dew_point_temperature_2m": "2d",
    "eastward_wind_10m": "10u",
    "northward_wind_10m": "10v",
    CLOUD: "tcc",
    QPF: "tp",
}


def _ifs_rows(
    payload: bytes, cycle: datetime, lead: int, length: int
) -> dict[str, tuple[IndexRow, int]]:
    result: dict[str, tuple[IndexRow, int]] = {}
    previous = 0
    for number, line in enumerate(payload.decode().splitlines(), 1):
        record = json.loads(line, object_pairs_hook=ifs._unique_keys)
        start, size = record.get("_offset"), record.get("_length")
        if (
            type(start) is not int
            or type(size) is not int
            or start < previous
            or size < 20
            or start + size > length
        ):
            raise ValueError("Invalid IFS native inventory byte ranges")
        previous = start + size
        parameter = record.get("param")
        if parameter not in _IFS_PARAMS.values():
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
            any(record.get(key) != value for key, value in expected.items())
            or "number" in record
            or record.get("model", "ifs") != "ifs"
        ):
            raise ValueError("IFS native inventory product/cycle/lead mismatch")
        if parameter in result:
            raise ValueError("Ambiguous IFS native field")
        result[parameter] = IndexRow(number, start, line), previous
    return result


def selected_native_fields(
    model: str, payload: bytes, cycle: datetime, lead: int, length: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Select native states plus one deterministic accumulation per valid endpoint.

    NBM prefers the complete six-hour event beyond its hourly file cadence. Its
    sparse one-hour event is never treated as covering the intervening hours.
    """
    selected: dict[str, tuple[IndexRow, int]] = {}
    missing: dict[str, str] = {}
    if model == "IFS":
        rows_ifs = _ifs_rows(payload, cycle, lead, length)
        selected = {
            variable: rows_ifs[param]
            for variable, param in _IFS_PARAMS.items()
            if param in rows_ifs
        }
        if lead == 0:
            selected.pop(QPF, None)
            missing[QPF] = "Cycle-time state bracket is not a precipitation event"
        missing["wind_gust_10m"] = ifs.GUST_TEMPORAL_MISMATCH
    else:
        if model == "RAP":
            rows, logical_rows = rap.physical_index_rows(payload)
        else:
            rows = logical_rows = parse_index_rows(payload.decode())
        fields = dict(_FRAGMENTS)
        if model == "NBM":
            fields.pop("eastward_wind_10m")
            fields.pop("northward_wind_10m")
            fields["wind_gust_10m"] = "GUST:10 m above ground"
        else:
            fields.pop("wind_speed_10m")
            fields.pop("wind_from_direction_10m")
        fields[CLOUD] = "TCDC:surface" if model == "NBM" else "TCDC:entire atmosphere"
        for variable, fragment in fields.items():
            matches = [
                row for row in logical_rows if row.descriptor == f":{fragment}:{lead} hour fcst:"
            ]
            if len(matches) > 1:
                raise ValueError(f"Ambiguous {model} {variable} inventory")
            if matches:
                row = matches[0]
                _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
                selected[variable] = row, end
            else:
                missing[variable] = f"No native instantaneous {variable} at source lead {lead}"
        candidates = []
        for row in logical_rows:
            match = re.fullmatch(r":APCP:surface:(\d+)-(\d+) hour acc fcst:", row.descriptor)
            if match and int(match[2]) == lead:
                candidates.append((int(match[1]), row))
        if candidates:
            if model == "NBM" and lead > 48 and (cycle.hour + lead) % 6:
                candidates = []
        if candidates:
            preferred = lead - (6 if model == "NBM" and lead > 48 else 1)
            if model in {"GFS", "RAP", "HRRR"}:
                preferred = max(start for start, _ in candidates)
            compatible = [row for start, row in candidates if start == preferred]
            # GFS can carry physically equivalent duplicated early bucket records;
            # retain all in one selected physical bundle and validate equality later.
            if len(compatible) > 1:
                if model != "GFS" or lead > 6 or len(compatible) != 2:
                    raise ValueError(f"Ambiguous {model} native QPF inventory")
                duplicate = compatible.pop()
                _, end = compute_message_byte_range(
                    rows, selected=duplicate, full_object_length=length
                )
                selected[QPF + "_equivalent_parent"] = duplicate, end
            if compatible:
                row = compatible[0]
                _, end = compute_message_byte_range(rows, selected=row, full_object_length=length)
                selected[QPF] = row, end
        if QPF not in selected:
            missing[QPF] = "No compatible native accumulation in this source file"
        if model == "NBM" and lead <= 48:
            from mesoforge.guidance.sources.nbm import build_field_selector

            matches = [
                row
                for row in rows
                if re.search(build_field_selector(POP, forecast_hour=lead), row.descriptor)
            ]
            if len(matches) > 1:
                raise ValueError("Ambiguous NBM hourly PoP inventory")
            if matches:
                _, end = compute_message_byte_range(
                    rows, selected=matches[0], full_object_length=length
                )
                selected[POP] = matches[0], end
    result = []
    for variable, (row, end) in selected.items():
        if model != "IFS" and row.line.split(":")[2] != f"d={cycle:%Y%m%d%H}":
            raise ValueError("Native inventory cycle mismatch")
        if row.byte_offset < 0 or end > length or end - row.byte_offset < 20:
            raise ValueError("Native selected message outside provider object")
        result.append(
            {
                "canonical_variable_id": variable,
                "index_row": row.line,
                "byte_start": row.byte_offset,
                "byte_end_exclusive": end,
                "content_bytes": end - row.byte_offset,
            }
        )
    return result, missing


def decode_state(
    payload: bytes,
    model: str,
    variable: str,
    cycle: datetime,
    lead: int,
    configuration: Phase2Configuration,
) -> xr.DataArray:
    """Use established source identity, unit, vertical, cycle and native-grid checks."""
    if model in {"IFS", "RAP"}:
        decoder = ifs.decode_surface_message if model == "IFS" else rap.decode_surface_message
        return decoder(payload, canonical_variable_id=variable, cycle=cycle, forecast_hour=lead)
    settings: HrrrPhase2SourceSettings | GfsSourceSettings | NbmSourceSettings
    settings = (
        configuration.hrrr
        if model == "HRRR"
        else configuration.gfs
        if model == "GFS"
        else configuration.nbm
    )
    contract = next(
        field for field in settings.field_contracts if field.canonical_variable_id == variable
    )
    if model == "HRRR":
        return hrrr_phase2_decoding.decode_selected_message(
            payload,
            contract=contract,
            read_keys=settings.read_keys,
            forecast_hour=lead,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
        )
    if model == "NBM":
        return nbm_decoding.decode_selected_message(
            payload,
            contract=contract,
            settings=configuration.nbm,
            forecast_hour=lead,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
        )
    return gfs_decoding.decode_instantaneous_message(
        payload,
        contract=contract,
        settings=configuration.gfs,
        forecast_hour=lead,
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
    )


def decode_interval(
    payload: bytes, model: str, cycle: datetime, lead: int
) -> tuple[xr.DataArray, int, int, float]:
    """Validate encoded deterministic liquid accumulation; return native bounds/factor."""
    validate_grib_message_boundaries(payload, url=model, range_header="retained native QPF")
    keys = tuple(
        sorted(
            set(
                (
                    *_CLOUD_READ_KEYS,
                    *ifs.IFS_READ_KEYS,
                    "numberOfMissingInStatisticalProcess",
                    "indicatorOfUnitForTimeRange",
                    "lengthOfTimeRange",
                    "typeOfStatisticalProcessing",
                )
            )
        )
    )
    decoded = _decode_all(payload, read_keys=keys)
    fields = [_with_dataset_coords(a, ds) for ds in decoded for a in ds.data_vars.values()]
    if len(fields) != 1:
        raise ValueError("Expected one deterministic native QPF message")
    field = fields[0]
    attrs = field.attrs
    expected = {
        "centre": "ecmf" if model == "IFS" else "kwbc",
        "stepType": "accum",
        "stepUnits": 1,
        "endStep": lead,
        "typeOfLevel": "surface",
        "level": 0,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": cycle.hour * 100,
        "typeOfStatisticalProcessing": 1,
        "discipline": 0,
        "parameterCategory": 1,
        "generatingProcessIdentifier": {"HRRR": 83, "RAP": 105, "GFS": 96, "IFS": 161, "NBM": 104}[
            model
        ],
    }
    if model == "IFS":
        expected.update(
            paramId=228,
            marsClass="od",
            marsStream="oper",
            marsType="fc",
            modelName="IFS",
            modelVersion="cy50r1",
        )
    else:
        expected.update(parameterNumber=8, productDefinitionTemplateNumber=8)
    if any(attrs.get("GRIB_" + key) != value for key, value in expected.items()):
        raise ValueError(f"{model} native QPF GRIB identity/interval mismatch")
    start = attrs.get("GRIB_startStep")
    if (
        type(start) is not int
        or not 0 <= start < lead
        or attrs.get("GRIB_lengthOfTimeRange") != lead - start
        or attrs.get("GRIB_indicatorOfUnitForTimeRange") != 1
    ):
        raise ValueError("Native QPF has no consistent encoded accumulation interval")
    units = str(attrs.get("GRIB_units", ""))
    factor = 1000.0 if model == "IFS" and units == "m" else 1.0
    if units not in ({"m"} if model == "IFS" else {"kg m**-2", "kg m-2", "kg m^-2", "kg/m^2"}):
        raise ValueError("Native QPF units do not establish liquid-equivalent amount")
    if (
        attrs.get("GRIB_probabilityType") is not None
        or attrs.get("GRIB_numberOfMissingInStatisticalProcess", 0) != 0
    ):
        raise ValueError("Native QPF is probabilistic or has incomplete statistical support")
    from datetime import timedelta

    for name, value in (("time", cycle), ("valid_time", cycle + timedelta(hours=lead))):
        if (
            name not in field.coords
            or field[name].ndim
            or field[name].values[()] != np.datetime64(value.replace(tzinfo=None), "ns")
        ):
            raise ValueError("Native QPF decoded time mismatch")
    return field, start, lead, factor
