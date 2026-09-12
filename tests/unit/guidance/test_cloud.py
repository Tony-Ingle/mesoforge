"""Native total cover selection and units cannot silently become another product."""

import json
from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.sources import cloud as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "NBM"])
def test_total_instant_selection_excludes_layers_average_and_standard_deviation(model):
    level = "surface" if model == "NBM" else "entire atmosphere"
    rows = (
        "1:0:d=2026091112:TCDC:high cloud layer:7 hour fcst:\n"
        f"2:100:d=2026091112:TCDC:{level}:6-7 hour ave fcst:\n"
        f"3:200:d=2026091112:TCDC:{level}:7 hour fcst:ens std dev\n"
        f"4:300:d=2026091112:TCDC:{level}:7 hour fcst:\n"
        "5:400:d=2026091112:TCDC:boundary layer cloud layer:7 hour fcst:\n"
    ).encode()
    row, end = source.selected_cloud_row(model, CYCLE, 7, rows, 500)
    assert (row.byte_offset, end) == (300, 400)
    absent = rows.replace(
        f"4:300:d=2026091112:TCDC:{level}:7 hour fcst:\n".encode(),
        b"4:300:d=2026091112:TMP:2 m above ground:7 hour fcst:\n",
    )
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_cloud_row(model, CYCLE, 7, absent, 500)
    with pytest.raises(ValueError, match="cycle mismatch"):
        source.selected_cloud_row(model, CYCLE, 7, rows.replace(b"2026091112", b"2026091106"), 500)


def test_rap_physical_byte_boundaries_allow_unrelated_shared_wind_messages():
    rows = (
        b"1.1:0:d=2026091112:UGRD:10 m above ground:7 hour fcst:\n"
        b"1.2:0:d=2026091112:VGRD:10 m above ground:7 hour fcst:\n"
        b"2:100:d=2026091112:TCDC:entire atmosphere:7 hour fcst:\n"
        b"3:200:d=2026091112:TCDC:boundary layer cloud layer:7 hour fcst:\n"
    )
    row, end = source.selected_cloud_row("RAP", CYCLE, 7, rows, 300)
    assert (row.byte_offset, end) == (100, 200)


def _ifs_row(**changes):
    return {
        "domain": "g",
        "class": "od",
        "stream": "oper",
        "type": "fc",
        "levtype": "sfc",
        "param": "tcc",
        "date": "20260911",
        "time": "1200",
        "expver": "0001",
        "step": "9",
        "_offset": 0,
        "_length": 100,
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [None, {"param": "lcc"}, {"type": "pf"}, {"step": "6"}, {"number": "1"}, {"model": "aifs"}],
)
def test_ifs_exact_deterministic_total_and_native_time(change):
    inventory = json.dumps(_ifs_row(**(change or {}))).encode()
    if change:
        with pytest.raises((ValueError, GribIndexError)):
            source.selected_cloud_row("IFS", CYCLE, 9, inventory, 100)
    else:
        row, end = source.selected_cloud_row("IFS", CYCLE, 9, inventory, 100)
        assert row.byte_offset == 0 and end == 100
    with pytest.raises(ValueError):
        source.cloud_url("IFS", CYCLE, 8)


def test_ambiguous_total_is_not_arbitrarily_selected():
    rows = (
        b"1:0:d=2026091112:TCDC:entire atmosphere:7 hour fcst:\n"
        b"2:100:d=2026091112:TCDC:entire atmosphere:7 hour fcst:"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        source.selected_cloud_row("GFS", CYCLE, 7, rows, 200)
    rows = "\n".join(json.dumps(r) for r in (_ifs_row(), _ifs_row(_offset=100))).encode()
    with pytest.raises(ValueError, match="ambiguous"):
        source.selected_cloud_row("IFS", CYCLE, 9, rows, 200)


@pytest.mark.parametrize("changed", [False, True])
def test_acquisition_retains_exact_message_index_and_provider_identity(changed):
    url = source.cloud_url("HRRR", CYCLE, 7)
    payload = _grib2_message(b"c")
    size = len(payload)
    headers = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    index = b"1:0:d=2026091112:TCDC:entire atmosphere:7 hour fcst:"
    transport = _FakeTransport()
    transport.head_queue[url] = [_FakeResponse(200, {**headers, "Content-Length": str(size)})]
    transport.get_queue[url + ".idx"] = [_FakeResponse(200, headers, index)]
    transport.get_queue[url] = [
        _FakeResponse(
            206,
            {
                **headers,
                "Content-Range": f"bytes 0-{size - 1}/{size}",
                "ETag": '"changed"' if changed else '"fixed"',
            },
            payload,
        )
    ]
    if changed:
        with pytest.raises(FetchError, match="changed ETag"):
            source.acquire_cloud_lead(
                "HRRR",
                CYCLE,
                7,
                transport=transport,
                clock=_FakeClock(CYCLE),
                sleeper=_FakeSleeper(),
            )
    else:
        result = source.acquire_cloud_lead(
            "HRRR", CYCLE, 7, transport=transport, clock=_FakeClock(CYCLE), sleeper=_FakeSleeper()
        )
        assert result.index_payload == index and result.full_object_etag == '"fixed"'
        assert len(result.selected_messages) == 1
        assert result.selected_messages[0].canonical_variable_id == "cloud_cover"
        assert result.selected_messages[0].payload == payload


def _field(model, **changes):
    lead = 9 if model == "IFS" else 7
    grid = {
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
        "NBM": {
            "gridType": "lambert",
            "Nx": 2345,
            "Ny": 1597,
            "DxInMetres": 2539.703,
            "DyInMetres": 2539.703,
            "LoVInDegrees": 265.0,
            "LaDInDegrees": 25.0,
            "Latin1InDegrees": 25.0,
            "Latin2InDegrees": 25.0,
            "radius": 6371200.0,
            "subCentre": 14,
            "typeOfGeneratingProcess": 2,
        },
    }.get(
        model,
        {
            "gridType": "regular_ll",
            "Ni": 1440,
            "Nj": 721,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
        },
    )
    attrs = {
        **grid,
        "centre": "ecmf" if model == "IFS" else "kwbc",
        "discipline": 0,
        "parameterCategory": 6,
        "parameterNumber": 192 if model == "IFS" else 1,
        "typeOfLevel": "entireAtmosphere"
        if model == "IFS"
        else ("surface" if model == "NBM" else "atmosphere"),
        "typeOfFirstFixedSurface": "sfc" if model in ("NBM", "IFS") else "10",
        "typeOfSecondFixedSurface": 8 if model == "IFS" else 255,
        "paramId": 164 if model == "IFS" else 228164,
        "level": 0,
        "stepType": "instant",
        "startStep": lead,
        "endStep": lead,
        "stepUnits": 1,
        "dataDate": 20260911,
        "dataTime": 1200,
        "productDefinitionTemplateNumber": 0,
        "generatingProcessIdentifier": {"HRRR": 83, "GFS": 96, "RAP": 105, "IFS": 161, "NBM": 104}[
            model
        ],
        "iScansNegatively": 0,
        "jScansPositively": 0 if model in ("GFS", "IFS") else 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 1 if model == "NBM" else 0,
        "units": "(0 - 1)" if model == "IFS" else "%",
    }
    if model == "IFS":
        attrs.update(
            marsClass="od",
            marsStream="oper",
            marsType="fc",
            modelName="IFS",
            modelVersion="cy50r1",
            paramId=164,
        )
    attrs.update(changes)
    values = np.array([[0, 37.5, 100], [-1, 101, np.nan]], dtype=float)
    if model == "IFS":
        values /= 100
    return xr.DataArray(
        values,
        dims=("y", "x"),
        name="native",
        coords={
            "time": np.datetime64("2026-09-11T12:00:00"),
            "valid_time": np.datetime64("2026-09-11T12:00:00") + np.timedelta64(lead, "h"),
        },
        attrs={"GRIB_" + k: v for k, v in attrs.items()},
    )


def _mock_decode(monkeypatch, field):
    monkeypatch.setattr(source, "_decode_all", lambda *a, **k: [field.to_dataset()])
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda f: (f, np.array([0, 1, 2]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "NBM", "IFS"])
def test_native_percent_fraction_normalization_zero_invalid_and_provenance(monkeypatch, model):
    native = _field(model)
    _mock_decode(monkeypatch, native)
    dataset, _, event = source.decode_cloud_lead(
        _grib2_message(b"c"), model, CYCLE, 9 if model == "IFS" else 7
    )
    np.testing.assert_array_equal(dataset.cloud_cover.values[0], [0, 37.5, 100])
    assert np.isnan(dataset.cloud_cover.values[1]).all()
    np.testing.assert_array_equal(dataset.native_cloud_cover, native)
    assert event["invalid_cell_count"] == 2 and event["missing_cell_count"] == 3
    assert event["unit"] == "percent" and event["cloud_definition"] == "total_cloud_cover"
    assert event["temporal_semantics"] == "instantaneous"
    assert event["interval_start"] is None and event["interval_end"] is None
    assert event["source_cycle"] == "2026-09-11T12:00:00Z"
    assert event["grib_keys"]["typeOfLevel"] == native.attrs["GRIB_typeOfLevel"]
    assert event["active_weight"] == 0
    assert event["native_factor_to_percent"] == (100 if model == "IFS" else 1)


@pytest.mark.parametrize(
    "change",
    [
        {"stepType": "avg"},
        {"startStep": 6},
        {"dataTime": 600},
        {"typeOfLevel": "highCloudLayer"},
        {"typeOfFirstFixedSurface": "200"},
        {"typeOfSecondFixedSurface": 8},
        {"parameterNumber": 5},
        {"units": "1"},
        {"productDefinitionTemplateNumber": 2},
        {"generatingProcessIdentifier": 83},
    ],
)
def test_decoder_rejects_incompatible_semantics_and_source(monkeypatch, change):
    _mock_decode(monkeypatch, _field("GFS", **change))
    with pytest.raises(ValueError, match="mismatch"):
        source.decode_cloud_lead(_grib2_message(b"c"), "GFS", CYCLE, 7)


def test_decoded_valid_time_must_match_requested_native_time(monkeypatch):
    field = _field("IFS").assign_coords(valid_time=np.datetime64("2026-09-11T20:00:00"))
    _mock_decode(monkeypatch, field)
    with pytest.raises(ValueError, match="decoded time mismatch"):
        source.decode_cloud_lead(_grib2_message(b"c"), "IFS", CYCLE, 9)
