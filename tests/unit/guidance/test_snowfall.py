"""Native snowfall amounts cannot be confused with snowpack, depth or rates."""

import json
from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.sources import snowfall as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("model", ["HRRR", "RAP"])
def test_selects_hourly_snowfall_not_instant_snowpack_or_cycle_total(model):
    rows = (
        b"1:0:d=2026091112:WEASD:surface:7 hour fcst:\n"
        b"2:100:d=2026091112:WEASD:surface:0-7 hour acc fcst:\n"
        b"3:200:d=2026091112:TMP:2 m above ground:7 hour fcst:\n"
        b"4:300:d=2026091112:WEASD:surface:6-7 hour acc fcst:\n"
        b"5:400:d=2026091112:ASNOW:surface:6-7 hour acc fcst:\n"
    )
    row, end = source.selected_snowfall_row(model, CYCLE, 7, rows, 500)
    assert row.byte_offset == 300 and end == 400
    for invalid in (
        rows.replace(b":6-7 hour acc fcst:", b":7 hour fcst:"),
        rows.replace(b"d=2026091112", b"d=2026091106"),
    ):
        with pytest.raises((ValueError, GribIndexError)):
            source.selected_snowfall_row(model, CYCLE, 7, invalid, 500)


def test_rap_reuses_physical_byte_boundaries_with_wind_submessages():
    rows = (
        b"1:0:d=2026091112:TMP:2 m above ground:7 hour fcst:\n"
        b"2.1:100:d=2026091112:UGRD:10 m above ground:7 hour fcst:\n"
        b"2.2:100:d=2026091112:VGRD:10 m above ground:7 hour fcst:\n"
        b"3:200:d=2026091112:WEASD:surface:6-7 hour acc fcst:\n"
        b"4:300:d=2026091112:FROZR:surface:6-7 hour acc fcst:\n"
    )
    row, end = source.selected_snowfall_row("RAP", CYCLE, 7, rows, 400)
    assert (row.byte_offset, end) == (200, 300)


def _ifs_row(**changes):
    return {
        "_offset": 0,
        "_length": 100,
        "param": "sf",
        "domain": "g",
        "class": "od",
        "stream": "oper",
        "type": "fc",
        "levtype": "sfc",
        "step": "9",
        "date": "20260911",
        "time": "1200",
        "expver": "0001",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [None, {"type": "pf"}, {"step": "6"}, {"number": "0"}, {"model": "aifs"}],
)
def test_ifs_selects_only_exact_deterministic_snowfall_identity(changes):
    row = _ifs_row(**(changes or {}))
    if changes:
        with pytest.raises(ValueError):
            source.selected_snowfall_row("IFS", CYCLE, 9, json.dumps(row).encode(), 100)
    else:
        selected, end = source.selected_snowfall_row("IFS", CYCLE, 9, json.dumps(row).encode(), 100)
        assert selected.byte_offset == 0 and end == 100
        duplicate = "\n".join(json.dumps(r) for r in (row, _ifs_row(_offset=100)))
        with pytest.raises(ValueError, match="ambiguous"):
            source.selected_snowfall_row("IFS", CYCLE, 9, duplicate.encode(), 200)


@pytest.mark.parametrize("parameter", ["sd", "tp"])
def test_ifs_absent_snowfall_is_explicitly_unavailable_not_an_identity_failure(parameter):
    inventory = json.dumps(_ifs_row(param=parameter)).encode()
    with pytest.raises(GribIndexError, match="snowfall field unavailable in selected product"):
        source.selected_snowfall_row("IFS", CYCLE, 9, inventory, 100)


def test_unsupported_sources_and_non_native_leads_fail_before_network():
    for model, lead in (("GFS", 3), ("NBM", 3), ("HRRR", 0), ("IFS", 8), ("HRRR", 49)):
        with pytest.raises(ValueError):
            source.snowfall_url(model, CYCLE, lead)
    assert source.SOURCES["GFS"]["supported"] is False
    assert "snowpack" in source.SOURCES["GFS"]["missing_reason"]
    assert "depth" in source.SOURCES["NBM"]["missing_reason"]


@pytest.mark.parametrize("changed", [False, True])
def test_acquisition_retains_one_exact_native_message_and_object_identity(changed):
    url = source.snowfall_url("HRRR", CYCLE, 7)
    payload = _grib2_message(b"s")
    size = len(payload)
    identity = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    transport = _FakeTransport()
    transport.head_queue[url] = [_FakeResponse(200, {**identity, "Content-Length": str(size)})]
    index = b"1:0:d=2026091112:WEASD:surface:6-7 hour acc fcst:"
    transport.get_queue[url + ".idx"] = [_FakeResponse(200, identity, index)]
    transport.get_queue[url] = [
        _FakeResponse(
            206,
            {
                **identity,
                "Content-Range": f"bytes 0-{size - 1}/{size}",
                "ETag": '"changed"' if changed else '"fixed"',
            },
            payload,
        )
    ]

    def acquire():
        return source.acquire_snowfall_lead(
            "HRRR", CYCLE, 7, transport=transport, clock=_FakeClock(CYCLE), sleeper=_FakeSleeper()
        )

    if changed:
        with pytest.raises(FetchError, match="changed ETag"):
            acquire()
    else:
        result = acquire()
        assert len(result.selected_messages) == 1
        assert result.selected_messages[0].canonical_variable_id == "snowfall_water_equivalent"
        assert result.selected_messages[0].payload == payload
        assert result.index_payload == index and result.full_object_etag == '"fixed"'


def _field(model="HRRR", *, param=144, start=None, lead=None, unit=None, changes=None):
    if lead is None:
        lead = 9 if model == "IFS" else 7
    if start is None:
        start = 0 if model == "IFS" else lead - 1
    attrs = {
        "centre": "ecmf" if model == "IFS" else "kwbc",
        "discipline": 0,
        "parameterCategory": 1,
        "parameterNumber": 13,
        "typeOfLevel": "surface",
        "stepType": "accum",
        "startStep": start,
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": 20260911,
        "dataTime": 1200,
        "productDefinitionTemplateNumber": 8,
        "typeOfStatisticalProcessing": 1,
        "lengthOfTimeRange": lead - start,
        "indicatorOfUnitForTimeRange": 1,
        "numberOfMissingInStatisticalProcess": 0,
        "generatingProcessIdentifier": {"HRRR": 83, "RAP": 105, "IFS": 161}[model],
        "gridType": "lambert",
        "Nx": 1799,
        "Ny": 1059,
        "DxInMetres": 3000.0,
        "DyInMetres": 3000.0,
        "LoVInDegrees": 262.5,
        "LaDInDegrees": 38.5,
        "Latin1InDegrees": 38.5,
        "Latin2InDegrees": 38.5,
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "units": "kg m**-2",
        "modelVersion": "retained-test-v1",
        "edition": 2,
        "packingType": "grid_simple",
        "bitsPerValue": 16,
        "binaryScaleFactor": -10,
        "decimalScaleFactor": 0,
    }
    if model == "RAP":
        attrs.update(
            Nx=451,
            Ny=337,
            DxInMetres=13545.0,
            DyInMetres=13545.0,
            LoVInDegrees=265.0,
            LaDInDegrees=25.0,
            Latin1InDegrees=25.0,
            Latin2InDegrees=25.0,
        )
    if model == "IFS":
        attrs.update(
            gridType="regular_ll",
            Ni=1440,
            Nj=721,
            iDirectionIncrementInDegrees=0.25,
            jDirectionIncrementInDegrees=0.25,
            jScansPositively=0,
            marsClass="od",
            marsStream="oper",
            marsType="fc",
            modelName="IFS",
            paramId=param,
            parameterNumber=198 if param == 144 else 53,
            units="m of water equivalent" if param == 144 else "kg m**-2",
        )
    if unit:
        attrs["units"] = unit
    attrs.update(changes or {})
    return xr.DataArray(
        [[0.0, 0.002], [-0.001, np.nan]],
        dims=("y", "x"),
        name="native_snow",
        coords={
            "time": np.datetime64("2026-09-11T12:00:00"),
            "valid_time": np.datetime64("2026-09-11T12:00:00") + np.timedelta64(lead, "h"),
        },
        attrs={"GRIB_" + k: v for k, v in attrs.items()},
    )


def _decode(monkeypatch, field, model, lead):
    monkeypatch.setattr(source, "_decode_all", lambda *_a, **_kw: [field.to_dataset()])
    # Isolate the new field contract; geometry is already tested in retained source tests.
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda f: (f, np.array([0, 1]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )
    return source.decode_snowfall_lead(_grib2_message(b"s"), model, CYCLE, lead)


@pytest.mark.parametrize(
    "model,param,factor", [("HRRR", 0, 1), ("RAP", 0, 1), ("IFS", 144, 1000), ("IFS", 228144, 1)]
)
def test_native_units_zero_missing_negative_and_provenance(monkeypatch, model, param, factor):
    field = _field(model, param=param)
    lead = 9 if model == "IFS" else 7
    data, _, event = _decode(monkeypatch, field, model, lead)
    assert data.native_amount.values[0, 0] == data.amount.values[0, 0] == 0
    assert data.amount.values[0, 1] == pytest.approx(0.002 * factor)
    assert data.native_amount.values[1, 0] == -0.001
    assert np.isnan(data.amount.values[1]).all()
    assert event["unit_factor_to_kg_m2"] == factor
    assert event["interval_start"] == (
        "2026-09-11T12:00:00Z" if model == "IFS" else "2026-09-11T18:00:00Z"
    )
    assert event["interval_end"] == (
        "2026-09-11T21:00:00Z" if model == "IFS" else "2026-09-11T19:00:00Z"
    )
    assert event["invalid_cell_count"] == 1 and event["missing_cell_count"] == 2
    assert event["grib_keys"]["packingType"] == "grid_simple"
    assert event["version"]["model_version"] == "retained-test-v1"
    again, _, metadata = _decode(monkeypatch, field, model, lead)
    xr.testing.assert_identical(data, again)
    assert metadata == event


@pytest.mark.parametrize(
    "changes",
    [
        {"stepType": "instant"},
        {"stepType": "avg"},
        {"parameterNumber": 11},
        {"parameterNumber": 29},
        {"units": "m"},
        {"units": "kg m**-2 s**-1"},
        {"startStep": 0},
        {"lengthOfTimeRange": 7},
        {"endStep": 8},
        {"dataTime": 600},
        {"numberOfMissingInStatisticalProcess": 1},
        {"generatingProcessIdentifier": 96},
        {"productDefinitionTemplateNumber": 0},
        {"typeOfStatisticalProcessing": 0},
        {"DxInMetres": 13000.0},
    ],
)
def test_rejects_wrong_snowfall_semantics(monkeypatch, changes):
    with pytest.raises(ValueError):
        _decode(monkeypatch, _field(changes=changes), "HRRR", 7)


def test_ifs_preserves_native_nonhourly_window_and_actual_zero_parent(monkeypatch):
    _, _, event = _decode(monkeypatch, _field("IFS", start=6), "IFS", 9)
    assert event["interval_start"] == "2026-09-11T18:00:00Z"
    assert event["interval_end"] == "2026-09-11T21:00:00Z"
    _, _, parent = _decode(monkeypatch, _field("IFS", start=0, lead=0), "IFS", 0)
    assert parent["interval_start"] == parent["interval_end"] == "2026-09-11T12:00:00Z"
    with pytest.raises(ValueError, match="zero duration"):
        _decode(monkeypatch, _field("IFS", start=9), "IFS", 9)
    with pytest.raises(ValueError, match="water-equivalent units"):
        _decode(monkeypatch, _field("IFS", unit="m"), "IFS", 9)
