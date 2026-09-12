"""Native thunder events retain their probability, interval and provider identity."""

from datetime import UTC, datetime

import numpy as np
import pyproj
import pytest

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError
from mesoforge.guidance.sources import thunder as source
from tests.unit.guidance.test_acquisition_v2 import (
    _FakeClock,
    _FakeResponse,
    _FakeSleeper,
    _FakeTransport,
    _grib2_message,
)
from tests.unit.guidance.test_cloud import _field as _native_grid_fixture

CYCLE = datetime(2026, 9, 11, 12, tzinfo=UTC)
INDEX = (
    b"1:0:d=2026091112:THUNC:entire atmosphere:6-7 hour missing fcst:\n"
    b"2:100:d=2026091112:TSTM:surface:5-6 hour acc fcst:probability forecast\n"
    b"3:200:d=2026091112:TSTM:surface:3-6 hour acc fcst:probability forecast\n"
    b"4:300:d=2026091112:TSTM:surface:0-6 hour acc fcst:probability forecast\n"
    b"5:400:d=2026091112:LTNG:surface:6 hour fcst:\n"
    b"6:500:d=2026091112:CAPE:surface:6 hour fcst:\n"
)


@pytest.mark.parametrize("source_id,start", [("NBM_1H", 100), ("NBM_3H", 200), ("NBM_6H", 300)])
def test_selects_exact_event_interval_not_thunder_coverage_or_lightning_diagnostic(
    source_id, start
):
    row, end = source.selected_thunder_row(source_id, CYCLE, 6, INDEX, 600)
    assert (row.byte_offset, end) == (start, start + 100)
    with pytest.raises(ValueError, match="cycle mismatch"):
        source.selected_thunder_row(
            source_id, CYCLE, 6, INDEX.replace(b"2026091112", b"2026091106"), 600
        )


def test_missing_or_ambiguous_native_event_does_not_fall_back_to_another_period():
    absent = INDEX.replace(b"5-6 hour acc fcst", b"4-6 hour acc fcst")
    with pytest.raises(GribIndexError, match="unavailable"):
        source.selected_thunder_row("NBM_1H", CYCLE, 6, absent, 600)
    duplicate = INDEX.replace(b"3-6 hour acc fcst", b"5-6 hour acc fcst")
    with pytest.raises(ValueError, match="ambiguous"):
        source.selected_thunder_row("NBM_1H", CYCLE, 6, duplicate, 600)


@pytest.mark.parametrize("source_id", ["GLMP_1H", "HREF_CT_1H", "HRRR_RAP_LIGHTNING", "GFS"])
def test_unbound_or_nonprobabilistic_source_never_contacts_provider(source_id):
    with pytest.raises(ValueError, match="Unsupported native thunder"):
        source.acquire_thunder_lead(
            source_id,
            CYCLE,
            6,
            transport=_FakeTransport(),
            clock=_FakeClock(CYCLE),
            sleeper=_FakeSleeper(),
        )
    assert all(row["active_weight"] == 0 for row in source.INSPECTED_GUIDANCE.values())


def test_native_window_boundaries_and_nonzero_cycle_hour_are_not_guessed_from_lead_modulo():
    with pytest.raises(ValueError, match="complete native"):
        source.thunder_url("NBM_6H", CYCLE, 5)
    with pytest.raises(ValueError, match="complete native"):
        source.thunder_url("NBM_1H", CYCLE, True)
    with pytest.raises(ValueError, match="through 36"):
        source.thunder_url("NBM_1H", CYCLE, 37)
    # NBM's inventory, not a lead%3 assumption, establishes published intervals.
    source.thunder_url("NBM_3H", CYCLE.replace(hour=13), 5)


@pytest.mark.parametrize("changed", [False, True])
def test_single_message_acquisition_preserves_available_time_and_object_identity(changed):
    url = source.thunder_url("NBM_1H", CYCLE, 6)
    payload = _grib2_message(b"t")
    size = len(payload)
    headers = {"ETag": '"fixed"', "Last-Modified": "Fri, 11 Sep 2026 18:00:00 GMT"}
    index = b"1:0:d=2026091112:TSTM:surface:5-6 hour acc fcst:probability forecast"
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
            source.acquire_thunder_lead(
                "NBM_1H",
                CYCLE,
                6,
                transport=transport,
                clock=_FakeClock(CYCLE),
                sleeper=_FakeSleeper(),
            )
    else:
        result = source.acquire_thunder_lead(
            "NBM_1H",
            CYCLE,
            6,
            transport=transport,
            clock=_FakeClock(CYCLE),
            sleeper=_FakeSleeper(),
        )
        assert result.model == "NBM" and result.forecast_hour == 6
        assert result.index_payload == index and result.full_object_etag == '"fixed"'
        assert len(result.selected_messages) == 1
        assert result.selected_messages[0].canonical_variable_id == "thunder_probability"
        assert result.selected_messages[0].payload == payload
        assert result.grib_available_at == datetime(2026, 9, 11, 18, tzinfo=UTC)


def _field(duration=1, **changes):
    field = _native_grid_fixture("NBM")
    field.attrs.update(
        {
            "GRIB_" + key: value
            for key, value in {
                "paramId": 3060,
                "parameterCategory": 19,
                "parameterNumber": 2,
                "typeOfGeneratingProcess": 5,
                "productDefinitionTemplateNumber": 8,
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
    field.values[:] = [[0, 12.5, 100], [-1, 101, np.nan]]
    return field


def _decode_fixture(monkeypatch, field):
    monkeypatch.setattr(source, "_decode_all", lambda *a, **kw: [field.to_dataset()])
    monkeypatch.setattr(
        source,
        "normalized_native_grid",
        lambda f: (f, np.array([0, 1, 2]), np.array([0, 1]), pyproj.CRS.from_epsg(4326)),
    )


@pytest.mark.parametrize("duration,start", [(1, "17:00:00Z"), (3, "15:00:00Z"), (6, "12:00:00Z")])
def test_probability_units_exact_native_intervals_and_unknown_event_scope_are_preserved(
    monkeypatch, duration, start
):
    native = _field(duration)
    _decode_fixture(monkeypatch, native)
    dataset, _, event = source.decode_thunder_lead(
        _grib2_message(b"t"), f"NBM_{duration}H", CYCLE, 6
    )
    np.testing.assert_array_equal(dataset.thunder_probability.values[0], [0, 0.125, 1])
    assert np.isnan(dataset.thunder_probability.values[1]).all()
    np.testing.assert_array_equal(dataset.native_probability, native)
    assert event["unit"] == "1" and event["native_unit"] == "percent"
    assert event["native_factor_to_fraction"] == 0.01
    assert event["invalid_cell_count"] == 2 and event["missing_cell_count"] == 3
    assert event["interval_start"] == "2026-09-11T" + start
    assert event["interval_end"] == event["valid_time"] == "2026-09-11T18:00:00Z"
    assert event["temporal_semantics"] == "interval_probability"
    assert event["interval_closure"] == "left_open_right_closed"
    assert event["source_cycle"] == "2026-09-11T12:00:00Z" and event["source_lead_hours"] == 6
    assert event["duration_hours"] == duration
    assert event["event_definition"]["physical_threshold"] is None
    assert event["spatial_support"]["radius_km"] is None
    assert event["grib_threshold"]["probability_type"] is None
    assert event["grib_threshold"]["scaled_value_of_upper_limit"] is None
    assert event["member_population"]["expected"] is None
    assert event["active_eligible"] == (duration == 1)
    assert event["grib_keys"]["parameterNumber"] == 2
    assert event["grib_keys"]["generatingProcessIdentifier"] == 104
    # Mutating one emitted metadata object cannot alter later native events.
    event["event_definition"]["parameter"] = "changed"
    assert source.SOURCES[f"NBM_{duration}H"]["event_definition"]["parameter"] == "TSTM"


@pytest.mark.parametrize(
    "changes",
    [
        {"units": "1"},
        {"parameterNumber": 203},
        {"parameterCategory": 7},
        {"typeOfLevel": "atmosphere"},
        {"typeOfSecondFixedSurface": 8},
        {"stepType": "instant"},
        {"startStep": 3},
        {"lengthOfTimeRange": 3},
        {"dataTime": 600},
        {"productDefinitionTemplateNumber": 9},
        {"typeOfGeneratingProcess": 2},
        {"typeOfStatisticalProcessing": 0},
        {"numberOfTimeRanges": 2},
        {"numberOfMissingInStatisticalProcess": 1},
    ],
)
def test_rejects_incompatible_probabilities_diagnostics_units_and_intervals(monkeypatch, changes):
    _decode_fixture(monkeypatch, _field(**changes))
    with pytest.raises(ValueError, match="mismatch"):
        source.decode_thunder_lead(_grib2_message(b"t"), "NBM_1H", CYCLE, 6)


def test_valid_time_mismatch_is_not_relabelled(monkeypatch):
    _decode_fixture(
        monkeypatch,
        _field().assign_coords(valid_time=np.datetime64("2026-09-11T19:00:00")),
    )
    with pytest.raises(ValueError, match="decoded time mismatch"):
        source.decode_thunder_lead(_grib2_message(b"t"), "NBM_1H", CYCLE, 6)
