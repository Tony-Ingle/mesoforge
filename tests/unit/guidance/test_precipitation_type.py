"""Native p-type selection, source identity and decoding contracts."""

import json
from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.sources import precipitation_type as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


def test_gfs_selects_instantaneous_flags_not_averaged_flags():
    rows = []
    for i, (_, (name, _)) in enumerate(source.FLAGS.items()):
        rows.extend(
            [
                f"{2 * i + 1}:{100 * (2 * i)}:d=2026091112:{name}:surface:6-7 hour ave fcst:",
                f"{2 * i + 2}:{100 * (2 * i + 1)}:d=2026091112:{name}:surface:7 hour fcst:",
            ]
        )
    selected = source.selected_type_rows("GFS", CYCLE, 7, "\n".join(rows).encode(), 800)
    assert [row.byte_offset for _, row, _ in selected] == [100, 300, 500, 700]
    assert all("ave" not in row.line for _, row, _ in selected)


def test_nbm_preserves_conditional_category_ranges_including_wet_snow():
    rows = [
        f"{i + 1}:{100 * i}:d=2026091112:PTYPE:surface:7 hour fcst:prob >={a} <{b}:prob fcst 1/1"
        for i, (a, b) in enumerate(source.PROB_RANGES.values())
    ]
    selected = source.selected_type_rows("NBM", CYCLE, 7, "\n".join(rows).encode(), 400)
    snow = next(row for name, row, _ in selected if name == "snow")
    assert ">=5 <7" in snow.line
    assert source.SOURCES["NBM"]["condition"] == "given_precipitation"
    assert source.SOURCES["NBM"]["encoding"] == "conditional_probabilities"


def test_ifs_source_time_and_category_code_identity_are_explicit():
    row = {
        "_offset": 0,
        "_length": 100,
        "param": "ptype",
        "class": "od",
        "stream": "oper",
        "type": "fc",
        "levtype": "sfc",
        "step": "9",
        "date": "20260911",
        "time": "1200",
    }
    selected = source.selected_type_rows("IFS", CYCLE, 9, json.dumps(row).encode(), 100)
    assert selected[0][0] == "native_code"
    row["type"] = "pf"
    with pytest.raises(ValueError, match="source/time"):
        source.selected_type_rows("IFS", CYCLE, 9, json.dumps(row).encode(), 100)
    with pytest.raises(ValueError):
        source.type_url("IFS", CYCLE, 8)


@pytest.mark.parametrize("changed", [False, True])
def test_acquisition_keeps_four_fields_and_provider_identity(changed):
    url = source.type_url("GFS", CYCLE, 7)
    payload = _grib2_message(b"f")
    size = len(payload)
    identity = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    transport = _FakeTransport()
    transport.head_queue[url] = [_FakeResponse(200, {**identity, "Content-Length": str(4 * size)})]
    index = "\n".join(
        f"{i + 1}:{i * size}:d=2026091112:{name}:surface:7 hour fcst:"
        for i, (name, _) in enumerate(source.FLAGS.values())
    ).encode()
    transport.get_queue[url + ".idx"] = [_FakeResponse(200, identity, index)]
    transport.get_queue[url] = [
        _FakeResponse(
            206,
            {**identity, "Content-Range": f"bytes {i * size}-{(i + 1) * size - 1}/{4 * size}"},
            payload,
        )
        for i in range(4)
    ]
    if changed:
        transport.get_queue[url][-1].headers["ETag"] = '"changed"'

    def acquire():
        return source.acquire_type_lead(
            "GFS", CYCLE, 7, transport=transport, clock=_FakeClock(CYCLE), sleeper=_FakeSleeper()
        )

    if changed:
        with pytest.raises(FetchError, match="changed ETag"):
            acquire()
    else:
        actual = acquire()
        assert actual.index_payload == index and actual.full_object_etag == '"fixed"'
        assert [m.canonical_variable_id for m in actual.selected_messages] == list(source.FLAGS)
        assert all(m.payload == payload for m in actual.selected_messages)


@pytest.mark.parametrize(
    "change",
    [
        None,
        ("stepType", "avg"),
        ("dataTime", 600),
        ("parameterNumber", 33),
        ("units", "K"),
        ("generatingProcessIdentifier", 83),
    ],
)
def test_binary_decoder_validates_native_contract_before_shared_grid_normalization(
    monkeypatch, change
):
    payloads = {name: _grib2_message(name[0].encode()) for name in source.FLAGS}

    def decode(payload, **kwargs):
        name = next(k for k, v in payloads.items() if v == payload)
        attrs = {
            "centre": "kwbc",
            "discipline": 0,
            "parameterCategory": 1,
            "parameterNumber": source.FLAGS[name][1] + 159,
            "typeOfLevel": "surface",
            "stepType": "instant",
            "startStep": 7,
            "endStep": 7,
            "stepUnits": 1,
            "dataDate": 20260911,
            "dataTime": 1200,
            "productDefinitionTemplateNumber": 0,
            "generatingProcessIdentifier": 96,
            "gridType": "regular_ll",
            "Ni": 1440,
            "Nj": 721,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
            "iScansNegatively": 0,
            "jScansPositively": 0,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
            "units": "(Code table 4.222)",
        }
        if change:
            attrs[change[0]] = change[1]
        field = xr.DataArray(
            np.full((2, 2), float(name == "rain")),
            dims=("y", "x"),
            name=name,
            coords={
                "time": np.datetime64("2026-09-11T12:00:00"),
                "valid_time": np.datetime64("2026-09-11T19:00:00"),
            },
            attrs={"GRIB_" + k: v for k, v in attrs.items()},
        )
        return [field.to_dataset()]

    monkeypatch.setattr(source, "_decode_all", decode)
    # The shared geometry validator has retained tests and real-message checks;
    # this small fixture isolates the new field/time/model assertions.
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda field: (field, np.array([0, 1]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )
    if change:
        with pytest.raises(ValueError):
            source.decode_type_lead(payloads, "GFS", CYCLE, 7)
    else:
        data, _, event = source.decode_type_lead(payloads, "GFS", CYCLE, 7)
        assert np.all(data.rain == 1) and np.all(data.snow == 0)
        assert event["temporal_semantics"] == "instantaneous" and event["interval_start"] is None
        assert event["grib_fields"]["rain"]["parameterNumber"] == 192
