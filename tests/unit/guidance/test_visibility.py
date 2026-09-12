"""Native surface visibility preserves distance semantics, missingness and source identity."""

from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.sources import visibility as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)
from tests.unit.guidance.test_cloud import _field as _native_grid_fixture

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "NBM"])
def test_selects_surface_distance_not_probability_average_or_cause_specific_visibility(model):
    rows = (
        b"1:0:d=2026091112:VIS:surface:6-7 hour ave fcst:\n"
        b"2:100:d=2026091112:VIS:surface:7 hour fcst:prob <1609.34:prob fcst 2/6:\n"
        b"3:200:d=2026091112:VIS:surface:7 hour fcst:ens std dev\n"
        b"4:300:d=2026091112:VIS:surface:7 hour fcst:\n"
        b"5:400:d=2026091112:VISLFOG:surface:7 hour fcst:\n"
        b"6:500:d=2026091112:VIS:1000 m above ground:7 hour fcst:\n"
    )
    selected, end = source.selected_visibility_row(model, CYCLE, 7, rows, 600)
    assert (selected.byte_offset, end) == (300, 400)
    absent = rows.replace(
        b"4:300:d=2026091112:VIS:surface:7 hour fcst:",
        b"4:300:d=2026091112:VISPCP:surface:7 hour fcst:",
    )
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_visibility_row(model, CYCLE, 7, absent, 600)
    with pytest.raises(ValueError, match="cycle mismatch"):
        source.selected_visibility_row(
            model, CYCLE, 7, rows.replace(b"2026091112", b"2026091106"), 600
        )


def test_duplicate_native_visibility_is_not_selected_arbitrarily():
    rows = (
        b"1:0:d=2026091112:VIS:surface:7 hour fcst:\n2:100:d=2026091112:VIS:surface:7 hour fcst:\n"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        source.selected_visibility_row("NBM", CYCLE, 7, rows, 200)


def test_rap_uses_retained_physical_message_parser():
    rows = (
        b"1.1:0:d=2026091112:UGRD:10 m above ground:7 hour fcst:\n"
        b"1.2:0:d=2026091112:VGRD:10 m above ground:7 hour fcst:\n"
        b"2:100:d=2026091112:VIS:surface:7 hour fcst:\n"
        b"3:200:d=2026091112:TMP:2 m above ground:7 hour fcst:\n"
    )
    selected, end = source.selected_visibility_row("RAP", CYCLE, 7, rows, 300)
    assert (selected.byte_offset, end) == (100, 200)


def test_unsupported_ifs_does_not_contact_provider_or_substitute_weather_field():
    assert source.SOURCES["IFS"]["supported"] is False
    assert "absent" in source.SOURCES["IFS"]["missing_reason"]
    with pytest.raises(ValueError, match="Unsupported native visibility source"):
        source.acquire_visibility_lead(
            "IFS",
            CYCLE,
            9,
            transport=_FakeTransport(),
            clock=_FakeClock(CYCLE),
            sleeper=_FakeSleeper(),
        )


@pytest.mark.parametrize("changed", [False, True])
def test_acquisition_preserves_single_message_and_provider_identity(changed):
    url = source.visibility_url("HRRR", CYCLE, 7)
    payload = _grib2_message(b"v")
    size = len(payload)
    headers = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    index = b"1:0:d=2026091112:VIS:surface:7 hour fcst:"
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
            source.acquire_visibility_lead(
                "HRRR",
                CYCLE,
                7,
                transport=transport,
                clock=_FakeClock(CYCLE),
                sleeper=_FakeSleeper(),
            )
    else:
        result = source.acquire_visibility_lead(
            "HRRR", CYCLE, 7, transport=transport, clock=_FakeClock(CYCLE), sleeper=_FakeSleeper()
        )
        assert result.index_payload == index and result.full_object_etag == '"fixed"'
        assert len(result.selected_messages) == 1
        assert result.selected_messages[0].canonical_variable_id == "visibility"
        assert result.selected_messages[0].payload == payload
        assert result.grib_available_at == datetime(2026, 9, 11, 18, tzinfo=UTC)


def _field(model="GFS", **changes):
    # Reuse the existing geometry fixture; visibility semantics are supplied independently.
    field = _native_grid_fixture(model)
    field.attrs.update(
        {
            "GRIB_" + k: v
            for k, v in {
                "parameterCategory": 19,
                "parameterNumber": 0,
                "paramId": 3020,
                "typeOfLevel": "surface",
                "typeOfFirstFixedSurface": "sfc",
                "typeOfSecondFixedSurface": 255,
                "units": "m",
                **changes,
            }.items()
        }
    )
    field.values[:] = [[0, 1609.344, 150000], [-1, np.nan, np.inf]]
    return field


def _decode_fixture(monkeypatch, field):
    monkeypatch.setattr(source, "_decode_all", lambda *a, **kw: [field.to_dataset()])
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda f: (f, np.array([0, 1, 2]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "NBM"])
def test_native_distances_zero_large_values_missingness_and_provenance(monkeypatch, model):
    field = _field(model)
    _decode_fixture(monkeypatch, field)
    dataset, _, event = source.decode_visibility_lead(_grib2_message(b"v"), model, CYCLE, 7)
    np.testing.assert_array_equal(dataset.visibility.values[0], [0, 1609.344, 150000])
    assert np.isnan(dataset.visibility.values[1]).all()
    np.testing.assert_array_equal(dataset.native_visibility, field)
    assert event["unit"] == "m" and event["native_unit"] == "m"
    assert event["native_factor_to_m"] == 1
    assert event["invalid_cell_count"] == 1 and event["missing_cell_count"] == 3
    assert event["visibility_definition"] == "horizontal_visibility"
    assert event["vertical_extent"] == "surface" and event["spatial_support"] == "native_model_grid"
    assert event["temporal_semantics"] == "instantaneous"
    assert event["interval_start"] is None and event["interval_end"] is None
    assert event["source_cycle"] == "2026-09-11T12:00:00Z"
    assert event["source_lead_hours"] == 7 and event["valid_time"] == "2026-09-11T19:00:00Z"
    assert event["grib_keys"]["parameterCategory"] == 19
    assert event["active_weight"] == 0
    assert event["censoring"]["upper_bound_m"] is None
    assert event["censoring"]["application_cap"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"units": "%"},
        {"units": "km"},
        {"parameterNumber": 33},
        {"parameterCategory": 6},
        {"typeOfLevel": "heightAboveGround"},
        {"level": 10},
        {"typeOfSecondFixedSurface": 8},
        {"stepType": "avg"},
        {"startStep": 6},
        {"dataTime": 600},
        {"productDefinitionTemplateNumber": 5},
        {"generatingProcessIdentifier": 83},
    ],
)
def test_rejects_incompatible_units_causes_levels_times_and_probability_templates(
    monkeypatch, changes
):
    _decode_fixture(monkeypatch, _field(**changes))
    with pytest.raises(ValueError, match="mismatch"):
        source.decode_visibility_lead(_grib2_message(b"v"), "GFS", CYCLE, 7)


def test_decoded_time_must_equal_requested_valid_time(monkeypatch):
    _decode_fixture(
        monkeypatch, _field().assign_coords(valid_time=np.datetime64("2026-09-11T18:00:00"))
    )
    with pytest.raises(ValueError, match="decoded time mismatch"):
        source.decode_visibility_lead(_grib2_message(b"v"), "GFS", CYCLE, 7)
