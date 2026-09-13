"""Ice accretion mass-equivalent and freezing-rain liquid never become one quantity."""

from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.sources import ice as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)
from tests.unit.guidance.test_cloud import _field as _native_grid_fixture

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("source_id,offset", [("NBM_FICEAC_1H", 100), ("NBM_FICEAC_6H", 200)])
def test_native_flat_ice_is_not_radial_liquid_sleet_probability_or_percentile(source_id, offset):
    index = (
        b"1:0:d=2026091112:LICEAC:surface:5-6 hour acc fcst:\n"
        b"2:100:d=2026091112:FICEAC:surface:5-6 hour acc fcst:\n"
        b"3:200:d=2026091112:FICEAC:surface:0-6 hour acc fcst:\n"
        b"4:300:d=2026091112:FICEAC:surface:0-6 hour acc fcst:prob >0.254:prob fcst 255/255\n"
        b"5:400:d=2026091112:FICEAC:surface:0-6 hour acc@(fcst,dt=6 hour),missing=0:50% level\n"
        b"6:500:d=2026091112:FRZR:surface:0-6 hour acc fcst:\n"
        b"7:600:d=2026091112:SLACC:surface:5-6 hour acc fcst:\n"
    )
    row, end = source.selected_ice_row(source_id, CYCLE, 6, index, 700)
    assert (row.byte_offset, end) == (offset, offset + 100)
    with pytest.raises(ValueError, match="cycle mismatch"):
        source.selected_ice_row(
            source_id, CYCLE, 6, index.replace(b"2026091112", b"2026091106"), 700
        )


@pytest.mark.parametrize("model", ["HRRR", "RAP"])
def test_freezing_rain_is_native_cycle_cumulative_not_hourly_or_frozen_rain(model):
    index = (
        b"1:0:d=2026091112:FROZR:surface:0-6 hour acc fcst:\n"
        b"2:100:d=2026091112:FRZR:surface:5-6 hour acc fcst:\n"
        b"3:200:d=2026091112:FRZR:surface:0-6 hour acc fcst:\n"
        b"4:300:d=2026091112:CFRZR:surface:6 hour fcst:\n"
    )
    row, end = source.selected_ice_row(f"{model}_FRZR", CYCLE, 6, index, 400)
    assert (row.byte_offset, end) == (200, 300)
    absent = index.replace(b"3:200:d=2026091112:FRZR:", b"3:200:d=2026091112:APCP:")
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_ice_row(f"{model}_FRZR", CYCLE, 6, absent, 400)


def test_rap_reuses_physical_message_boundaries_without_requiring_temperature():
    index = (
        b"1.1:0:d=2026091112:UGRD:10 m above ground:6 hour fcst:\n"
        b"1.2:0:d=2026091112:VGRD:10 m above ground:6 hour fcst:\n"
        b"2:100:d=2026091112:FRZR:surface:0-6 hour acc fcst:\n"
        b"3:200:d=2026091112:CFRZR:surface:6 hour fcst:\n"
    )
    row, end = source.selected_ice_row("RAP_FRZR", CYCLE, 6, index, 300)
    assert (row.byte_offset, end) == (100, 200)


def test_duplicate_amount_is_not_arbitrarily_selected():
    index = (
        b"1:0:d=2026091112:FICEAC:surface:5-6 hour acc fcst:\n"
        b"2:100:d=2026091112:FICEAC:surface:5-6 hour acc fcst:\n"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        source.selected_ice_row("NBM_FICEAC_1H", CYCLE, 6, index, 200)


@pytest.mark.parametrize("model,hour", [("HRRR", 12), ("RAP", 15)])
@pytest.mark.parametrize("lead", [24, 48])
def test_whole_day_native_freezing_rain_index_is_exactly_equivalent_to_hour_bounds(
    model, hour, lead
):
    cycle = CYCLE.replace(hour=hour)
    source_id = f"{model}_FRZR"
    index = (
        f"1:0:d={cycle:%Y%m%d%H}:FRZR:surface:0-{lead // 24} day acc fcst:\n"
        f"2:100:d={cycle:%Y%m%d%H}:CFRZR:surface:{lead} hour fcst:\n"
    ).encode()
    row, end = source.selected_ice_row(source_id, cycle, lead, index, 200)
    assert (row.byte_offset, end) == (0, 100)
    hours = index.replace(f"0-{lead // 24} day".encode(), f"0-{lead} hour".encode())
    assert source.selected_ice_row(source_id, cycle, lead, hours, 200)[0].byte_offset == 0
    for changed in (
        index.replace(f"0-{lead // 24} day".encode(), f"0-{lead // 24 + 1} day".encode()),
        index.replace(b"day acc fcst:", b"day ave fcst:"),
        index.replace(b"day acc fcst:", b"day acc fcst:prob >0:"),
    ):
        with pytest.raises(GribIndexError, match="unavailable"):
            source.selected_ice_row(source_id, cycle, lead, changed, 200)
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_ice_row(source_id, cycle, lead - 1, index, 200)


@pytest.mark.parametrize("model", ["GFS", "IFS"])
def test_unsupported_sources_never_download_or_substitute_type_and_total_precipitation(model):
    with pytest.raises(ValueError, match="Unsupported native ice"):
        source.acquire_ice_lead(
            model,
            CYCLE,
            6,
            transport=_FakeTransport(),
            clock=_FakeClock(CYCLE),
            sleeper=_FakeSleeper(),
        )
    assert source.SOURCES[model]["supported"] is False
    assert source.SOURCES[model]["active_weight"] == 0


def test_source_cycle_lead_validation_preserves_parent_leads_and_native_periods():
    assert "f06" in source.ice_url("HRRR_FRZR", CYCLE, 6)
    source.ice_url("RAP_FRZR", CYCLE.replace(hour=15), 3)
    with pytest.raises(ValueError, match="positive complete"):
        source.ice_url("NBM_FICEAC_6H", CYCLE, 5)
    with pytest.raises(ValueError, match="positive complete"):
        source.ice_url("HRRR_FRZR", CYCLE, False)
    with pytest.raises(ValueError, match="exact UTC"):
        source.ice_url("NBM_FICEAC_1H", CYCLE.replace(minute=1), 6)


@pytest.mark.parametrize("changed", [False, True])
def test_acquisition_retains_exact_native_message_and_source_identity(changed):
    url = source.ice_url("NBM_FICEAC_1H", CYCLE, 6)
    payload = _grib2_message(b"i")
    size = len(payload)
    headers = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    index = b"1:0:d=2026091112:FICEAC:surface:5-6 hour acc fcst:"
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
            source.acquire_ice_lead(
                "NBM_FICEAC_1H",
                CYCLE,
                6,
                transport=transport,
                clock=_FakeClock(CYCLE),
                sleeper=_FakeSleeper(),
            )
    else:
        result = source.acquire_ice_lead(
            "NBM_FICEAC_1H",
            CYCLE,
            6,
            transport=transport,
            clock=_FakeClock(CYCLE),
            sleeper=_FakeSleeper(),
        )
        assert result.model == "NBM" and result.forecast_hour == 6
        assert result.index_payload == index and result.full_object_etag == '"fixed"'
        assert len(result.selected_messages) == 1
        assert result.selected_messages[0].canonical_variable_id == "ice_amount"
        assert result.selected_messages[0].payload == payload
        assert result.grib_available_at == datetime(2026, 9, 11, 18, tzinfo=UTC)


def _field(model="NBM", duration=1, **changes):
    field = _native_grid_fixture(model)
    field.attrs.update(
        {
            "GRIB_" + key: value
            for key, value in {
                "parameterCategory": 1,
                "parameterNumber": 228 if model == "NBM" else 225,
                "typeOfLevel": "surface",
                "typeOfFirstFixedSurface": "sfc",
                "typeOfSecondFixedSurface": 255,
                "typeOfGeneratingProcess": 2,
                "productDefinitionTemplateNumber": 8,
                "units": "kg m**-2",
                "stepType": "accum",
                "startStep": 6 - duration,
                "endStep": 6,
                "typeOfStatisticalProcessing": 1,
                "lengthOfTimeRange": duration,
                "indicatorOfUnitForTimeRange": 1,
                "numberOfTimeRanges": 1,
                "numberOfMissingInStatisticalProcess": 0,
                **changes,
            }.items()
        }
    )
    field = field.assign_coords(valid_time=np.datetime64("2026-09-11T18:00:00"))
    field.values[:] = [[0, 0.127, 2.54], [-1, np.nan, np.inf]]
    return field


def _decode_fixture(monkeypatch, field):
    monkeypatch.setattr(source, "_decode_all", lambda *a, **kw: [field.to_dataset()])
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda f: (f, np.array([0, 1, 2]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )


@pytest.mark.parametrize(
    "source_id,model,duration,quantity",
    [
        ("NBM_FICEAC_1H", "NBM", 1, "flat_ice_accretion_mass_equivalent"),
        ("NBM_FICEAC_6H", "NBM", 6, "flat_ice_accretion_mass_equivalent"),
        ("HRRR_FRZR", "HRRR", 6, "freezing_rain_liquid_equivalent"),
        ("RAP_FRZR", "RAP", 6, "freezing_rain_liquid_equivalent"),
    ],
)
def test_native_mass_units_zero_missing_and_quantity_provenance(
    monkeypatch, source_id, model, duration, quantity
):
    field = _field(model, duration)
    _decode_fixture(monkeypatch, field)
    ds, _, event = source.decode_ice_lead(_grib2_message(b"i"), source_id, CYCLE, 6)
    np.testing.assert_array_equal(ds.amount.values[0], [0, 0.127, 2.54])
    assert np.isnan(ds.amount.values[1]).all()
    np.testing.assert_array_equal(ds.native_amount, field)
    assert event["quantity_kind"] == quantity
    assert event["unit"] == "kg/m^2" and event["native_unit"] == "kg m**-2"
    assert event["native_factor_to_canonical"] == 1
    assert event["duration_hours"] == duration
    assert event["interval_end"] == event["valid_time"] == "2026-09-11T18:00:00Z"
    assert event["interval_start"] == (
        "2026-09-11T17:00:00Z" if duration == 1 else "2026-09-11T12:00:00Z"
    )
    assert event["interval_closure"] == "left_open_right_closed"
    assert event["invalid_cell_count"] == 1 and event["missing_cell_count"] == 3
    assert event["source_cycle"] == "2026-09-11T12:00:00Z" and event["source_lead_hours"] == 6
    assert event["active_weight"] == 0
    assert event["accretion_geometry"] == ("elevated_flat_surface" if model == "NBM" else None)
    assert event["grib_keys"]["parameterNumber"] == (228 if model == "NBM" else 225)


@pytest.mark.parametrize(
    "changes",
    [
        {"units": "m"},
        {"units": "mm"},
        {"units": "kg m**-2 s**-1"},
        {"units": "%"},
        {"parameterNumber": 229},
        {"parameterNumber": 225},
        {"parameterNumber": 227},
        {"productDefinitionTemplateNumber": 9},
        {"productDefinitionTemplateNumber": 10},
        {"stepType": "instant"},
        {"startStep": 0},
        {"lengthOfTimeRange": 6},
        {"typeOfStatisticalProcessing": 0},
        {"numberOfMissingInStatisticalProcess": 1},
    ],
)
def test_incompatible_depth_rate_probability_geometry_or_interval_is_rejected(monkeypatch, changes):
    _decode_fixture(monkeypatch, _field(**changes))
    with pytest.raises(ValueError, match="mismatch|requires kg/m"):
        source.decode_ice_lead(_grib2_message(b"i"), "NBM_FICEAC_1H", CYCLE, 6)


def test_valid_time_mismatch_never_relabels_native_ice(monkeypatch):
    _decode_fixture(
        monkeypatch, _field().assign_coords(valid_time=np.datetime64("2026-09-11T19:00:00"))
    )
    with pytest.raises(ValueError, match="cycle/valid time mismatch"):
        source.decode_ice_lead(_grib2_message(b"i"), "NBM_FICEAC_1H", CYCLE, 6)


@pytest.mark.parametrize("duration", [1, 6])
def test_only_exact_nbm_ficeac_unknown_local_unit_uses_official_noaa_binding(monkeypatch, duration):
    field = _field(
        duration=duration, units="unknown", paramId=0, shortName="unknown", name="unknown"
    )
    _decode_fixture(monkeypatch, field)
    ds, _, event = source.decode_ice_lead(_grib2_message(b"i"), f"NBM_FICEAC_{duration}H", CYCLE, 6)
    np.testing.assert_array_equal(ds.amount.values[0], [0, 0.127, 2.54])
    assert ds.native_amount.attrs["units"] == event["native_unit"] == "kg m**-2"
    assert event["decoded_native_unit"] == event["grib_keys"]["units"] == "unknown"
    assert event["grib_keys"]["paramId"] == 0
    assert event["grib_keys"]["shortName"] == event["grib_keys"]["name"] == "unknown"
    assert event["native_unit_resolution"] == "official_noaa_ficeac_parameter_binding"
    assert "grib2_table4-2-0-1" in event["native_unit_documentation"]


@pytest.mark.parametrize(
    "model,changes",
    [
        ("HRRR", {"units": "unknown", "paramId": 0}),
        ("NBM", {"units": "unknown", "paramId": 999}),
        ("NBM", {"units": "unknown", "paramId": 0, "parameterNumber": 229}),
        ("NBM", {"units": "unknown", "paramId": 0, "generatingProcessIdentifier": 83}),
    ],
)
def test_unknown_units_are_not_accepted_for_other_products_or_incompatible_bindings(
    monkeypatch, model, changes
):
    _decode_fixture(
        monkeypatch, _field(model=model, duration=6 if model == "HRRR" else 1, **changes)
    )
    with pytest.raises(ValueError, match="mismatch|requires kg/m"):
        source.decode_ice_lead(
            _grib2_message(b"i"), "HRRR_FRZR" if model == "HRRR" else "NBM_FICEAC_1H", CYCLE, 6
        )
