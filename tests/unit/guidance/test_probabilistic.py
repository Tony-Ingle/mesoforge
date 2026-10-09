"""Native probability events remain distinct; acquisition is bounded and reproducible."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import xarray as xr

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.sources import probabilistic as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)
PAYLOAD = _grib2_message(b"p")


def _decoded(monkeypatch, source_id="GEFS_6H", values=None):
    """Small decoder result exercises event assertions independently of GRIB packing."""
    product = copy.deepcopy(source.PRODUCTS[source_id])
    expected = product["expected"]
    expected.update(
        gridType="regular_ll",
        Ni=2,
        Nj=2,
        iDirectionIncrementInDegrees=0.5,
        jDirectionIncrementInDegrees=0.5,
    )
    for key in ("Nx", "Ny"):
        expected.pop(key, None)
    monkeypatch.setitem(source.PRODUCTS, source_id, product)
    start, end = (12, 36) if source_id == "ECMWF_ENS_24H" else (6, 12)
    bound = "Lower" if source_id == "ECMWF_ENS_24H" else "Upper"
    attrs = {
        **{f"GRIB_{key}": value for key, value in expected.items()},
        "GRIB_discipline": 0,
        "GRIB_parameterCategory": 1,
        "GRIB_typeOfLevel": "surface",
        "GRIB_productDefinitionTemplateNumber": 9,
        "GRIB_typeOfStatisticalProcessing": 1,
        "GRIB_lengthOfTimeRange": end - start,
        "GRIB_indicatorOfUnitForTimeRange": 1,
        "GRIB_startStep": start,
        "GRIB_endStep": end,
        "GRIB_stepUnits": 1,
        "GRIB_dataDate": 20260911,
        "GRIB_dataTime": 1200,
        "GRIB_numberOfMissingInStatisticalProcess": 0,
        f"GRIB_scaleFactorOf{bound}Limit": 3,
        f"GRIB_scaledValueOf{bound}Limit": int(product["threshold"]["value"] * 1000),
        "GRIB_units": "%",
        "GRIB_tablesVersion": 2,
        "GRIB_latitudeOfFirstGridPointInDegrees": 45.0,
        "GRIB_longitudeOfFirstGridPointInDegrees": 266.5,
    }
    field = xr.DataArray(
        values if values is not None else [[0.0, 100.0], [20.0, np.nan]],
        dims=("latitude", "longitude"),
        coords={
            "latitude": [45.0, 44.5],
            "longitude": [266.5, 267.0],
            "time": np.datetime64(CYCLE.replace(tzinfo=None)),
            "valid_time": np.datetime64((CYCLE + timedelta(hours=end)).replace(tzinfo=None)),
        },
        attrs=attrs,
        name="tp",
    )
    monkeypatch.setattr(source, "_decode_all", lambda *args, **kwargs: [field.to_dataset()])
    return field, start, end


def test_native_probability_percent_zero_missing_and_geographic_order(monkeypatch):
    field, start, end = _decoded(monkeypatch)
    normalized, crs, event = source.decode_product(PAYLOAD, "GEFS_6H", CYCLE, start, end)
    np.testing.assert_equal(normalized.probability.values, [[0.2, np.nan], [0.0, 1.0]])
    np.testing.assert_equal(normalized.native_probability.values, field.values[::-1])
    np.testing.assert_equal(normalized.x.values, [-93.5, -93.0])
    np.testing.assert_equal(normalized.y.values, [44.5, 45.0])
    assert crs.to_epsg() == 4326
    assert event["threshold"] == {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}
    assert event["interval_start"] == "2026-09-11T18:00:00Z"
    assert event["interval_end"] == "2026-09-12T00:00:00Z"
    assert event["source_lead_hours"] == 12
    assert event["invalid_cell_count"] == 0
    assert event["missing_cell_count"] == 1
    assert event["probability_method"] == "native_published_bias_corrected_ensemble_probability"
    assert event["ensemble_population"]["member_ids"] is None
    json.dumps(event)


@pytest.mark.parametrize("bad", [-1.0, 100.01, np.inf])
def test_invalid_probability_is_missing_without_clipping_native_input(monkeypatch, bad):
    _, start, end = _decoded(monkeypatch, values=[[bad, 100.0], [0.0, 25.0]])
    normalized, _, event = source.decode_product(PAYLOAD, "GEFS_6H", CYCLE, start, end)
    assert np.isnan(normalized.probability.values[1, 0])
    assert normalized.native_probability.values[1, 0] == bad
    assert normalized.probability.values[0, 0] == 0
    assert event["missing_cell_count"] == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("probabilityType", 3),
        ("scaledValueOfUpperLimit", 1000),
        ("startStep", 0),
        ("lengthOfTimeRange", 12),
        ("typeOfStatisticalProcessing", 0),
        ("numberOfMissingInStatisticalProcess", 1),
        ("units", "mm"),
        ("typeOfGeneratingProcess", 194),
        ("dataTime", 0),
        ("Ni", 3),
        ("productDefinitionTemplateNumber", 8),
    ],
)
def test_wrong_event_identity_is_rejected(monkeypatch, key, value):
    field, start, end = _decoded(monkeypatch)
    field.attrs[f"GRIB_{key}"] = value
    with pytest.raises(ValueError):
        source.decode_product(PAYLOAD, "GEFS_6H", CYCLE, start, end)


def test_ecmwf_native_inclusive_threshold_and_24h_window_are_preserved(monkeypatch):
    _, start, end = _decoded(monkeypatch, "ECMWF_ENS_24H")
    _, _, event = source.decode_product(PAYLOAD, "ECMWF_ENS_24H", CYCLE, start, end)
    assert event["threshold"] == {"value": 1.0, "unit": "kg/m^2", "comparison": "ge"}
    assert event["interval_start"] == "2026-09-12T00:00:00Z"
    assert event["interval_end"] == "2026-09-13T00:00:00Z"
    assert event["licence"] == "CC-BY-4.0"
    assert event["version"]["model_version"] == "cy50r1"
    assert event["spatial_support"]["kind"] == "grid_box_mean"
    assert event["spatial_support"]["point_downscaling"] == "not_applied"
    assert event["spatial_support"]["effective_event_footprint"] == (
        "not_encoded_in_retained_message"
    )


@pytest.mark.parametrize("coordinate", ["latitude", "longitude"])
def test_displaced_regular_grid_coordinates_are_rejected(monkeypatch, coordinate):
    field, start, end = _decoded(monkeypatch)
    field[coordinate] = field[coordinate] + 1
    with pytest.raises(ValueError, match="geographic coordinates"):
        source.decode_product(PAYLOAD, "GEFS_6H", CYCLE, start, end)


def test_lambert_decoded_orientation_matches_projection(monkeypatch):
    product = copy.deepcopy(source.PRODUCTS["NBM_6H"])
    product["expected"].update(Nx=2, Ny=2)
    monkeypatch.setitem(source.PRODUCTS, "NBM_6H", product)
    expected = product["expected"].copy()
    field, start, end = _decoded(monkeypatch, "NBM_6H")
    product["expected"] = expected
    monkeypatch.setitem(source.PRODUCTS, "NBM_6H", product)
    attrs = {**field.attrs, **{f"GRIB_{key}": value for key, value in expected.items()}}
    attrs.update(
        GRIB_latitudeOfFirstGridPointInDegrees=19.229,
        GRIB_longitudeOfFirstGridPointInDegrees=233.7234,
    )
    crs = source.build_lambert_conformal_crs(
        lov_degrees=265,
        lad_degrees=25,
        latin1_degrees=25,
        latin2_degrees=25,
        earth_radius_m=6371200,
    )
    x, y = source.compute_projected_coordinates(
        crs,
        first_lat_degrees=19.229,
        first_lon_degrees=233.7234,
        dx_m=2539.703,
        dy_m=2539.703,
        nx=2,
        ny=2,
    )
    lat, lon = source.compute_latlon_grid(crs, x=x, y=y)
    native = xr.DataArray(
        field.values,
        dims=("y", "x"),
        attrs=attrs,
        name="tp",
        coords={
            "latitude": (("y", "x"), lat),
            "longitude": (("y", "x"), lon),
            "time": field.time,
            "valid_time": field.valid_time,
        },
    )
    monkeypatch.setattr(source, "_decode_all", lambda *args, **kwargs: [native.to_dataset()])
    normalized, _, _ = source.decode_product(PAYLOAD, "NBM_6H", CYCLE, start, end)
    assert normalized.probability.values[0, 1] == 1
    native.latitude.values[0, 1] += 0.1
    with pytest.raises(ValueError, match="disagree with projection"):
        source.decode_product(PAYLOAD, "NBM_6H", CYCLE, start, end)


@pytest.mark.parametrize(
    "source_id,start,end",
    [
        ("NBM_6H", 0, 1),
        ("GEFS_6H", 1, 7),
        ("REFS_1H", 0, 6),
        ("ECMWF_ENS_24H", 0, 6),
    ],
)
def test_native_period_is_not_silently_converted(source_id, start, end):
    with pytest.raises(ValueError):
        source.product_url(source_id, CYCLE, start, end)


def test_refs_probability_is_a_distinct_neighborhood_event():
    product = source.PRODUCTS["REFS_1H"]
    assert product["threshold"]["value"] == 12.7
    assert product["spatial_support"]["kind"] == "neighborhood"
    assert product["spatial_support"]["radius_km"] is None
    assert product["status"] == "shadow"


@pytest.mark.parametrize("cycle,start,end", [(CYCLE, 6, 30), (CYCLE.replace(hour=6), 12, 36)])
def test_ecmwf_probability_only_advertises_native_cycles_and_period_steps(cycle, start, end):
    with pytest.raises(ValueError, match="00/12Z"):
        source.product_url("ECMWF_ENS_24H", cycle, start, end)


def _responses(source_id, index_payload, payload=PAYLOAD):
    start, end = (12, 36) if source_id == "ECMWF_ENS_24H" else (6, 12)
    if source_id == "NBM_6H":
        start, end = 0, 6
    transport = _FakeTransport()
    url = source.product_url(source_id, CYCLE, start, end)
    identities = {"ETag": '"native-fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:31:30 GMT"}
    transport.head_queue[url] = [
        _FakeResponse(200, {**identities, "Content-Length": str(len(payload))})
    ]
    ranged = _FakeResponse(
        206, {**identities, "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"}, payload
    )
    if source_id == "GEFS_6H":
        transport.get_queue[url] = [
            _FakeResponse(
                206, {**identities, "Content-Range": f"bytes 0-15/{len(payload)}"}, payload[:16]
            ),
            ranged,
        ]
    else:
        suffix = ".index" if source_id == "ECMWF_ENS_24H" else ".idx"
        index_url = url.removesuffix(".grib2") + suffix if suffix == ".index" else url + suffix
        transport.get_queue[index_url] = [_FakeResponse(200, identities, index_payload)]
        transport.get_queue[url] = [ranged]
    return transport, start, end


@pytest.mark.parametrize("source_id", ["NBM_6H", "GEFS_6H", "ECMWF_ENS_24H"])
def test_acquisition_retains_exact_native_message_and_index_or_header(source_id):
    if source_id == "ECMWF_ENS_24H":
        index = json.dumps(
            {
                "_offset": 0,
                "_length": len(PAYLOAD),
                "param": "tpg1",
                "step": "12-36",
                "class": "od",
                "stream": "enfo",
                "type": "ep",
                "levtype": "sfc",
                "date": "20260911",
                "time": "1200",
            }
        ).encode()
    else:
        index = b"1:0:d=2026091112:APCP:surface:0-6 hour acc fcst:prob >0.254:prob fcst 255/255\n"
    transport, start, end = _responses(source_id, index)
    acquisition = source.acquire_product(
        source_id,
        CYCLE,
        start,
        end,
        transport=transport,
        clock=_FakeClock(CYCLE + timedelta(hours=12)),
        sleeper=_FakeSleeper(),
    )
    assert acquisition.selected_messages[0].payload == PAYLOAD
    assert acquisition.selected_messages[0].byte_start == 0
    assert acquisition.selected_messages[0].byte_end == len(PAYLOAD)
    assert acquisition.index_payload == (PAYLOAD[:16] if source_id == "GEFS_6H" else index)
    assert acquisition.full_object_etag == '"native-fixed"'
    assert acquisition.grib_available_at == datetime(2026, 9, 11, 18, 31, 30, tzinfo=UTC)
    assert len(transport.calls) == 3


def test_changed_provider_identity_fails_before_accepting_probability():
    transport, start, end = _responses("GEFS_6H", b"")
    next(iter(transport.get_queue.values()))[-1].headers["ETag"] = '"changed"'
    with pytest.raises(FetchError, match="changed ETag"):
        source.acquire_product(
            "GEFS_6H",
            CYCLE,
            start,
            end,
            transport=transport,
            clock=_FakeClock(CYCLE + timedelta(hours=12)),
            sleeper=_FakeSleeper(),
        )


def test_gefs_first_message_header_range_must_be_exact():
    transport, start, end = _responses("GEFS_6H", b"")
    next(iter(transport.get_queue.values()))[0].headers["Content-Range"] = "bytes 1-16/100"
    with pytest.raises(FetchError, match="Section 0"):
        source.acquire_product(
            "GEFS_6H",
            CYCLE,
            start,
            end,
            transport=transport,
            clock=_FakeClock(CYCLE + timedelta(hours=12)),
            sleeper=_FakeSleeper(),
        )


def test_probability_cutoff_rejects_later_object_before_grid_download():
    transport, start, end = _responses("GEFS_6H", b"")
    with pytest.raises(ValueError, match="pinned information cutoff"):
        source.acquire_product(
            "GEFS_6H",
            CYCLE,
            start,
            end,
            transport=transport,
            clock=_FakeClock(CYCLE + timedelta(hours=12)),
            sleeper=_FakeSleeper(),
            information_cutoff=CYCLE + timedelta(hours=6),
        )
    assert len(transport.calls) == 1  # HEAD only, no index or model-body request.


def test_nbm_six_hour_event_alignment_is_valid_utc_not_lead_modulo():
    cycle = CYCLE.replace(hour=13)
    assert "f125" in source.product_url("NBM_6H", cycle, 119, 125)
    with pytest.raises(ValueError, match="UTC valid"):
        source.product_url("NBM_6H", cycle, 120, 126)
