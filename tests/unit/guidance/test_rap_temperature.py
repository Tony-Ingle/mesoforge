"""RAP-only provider and native-grid contract checks; no network or services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from mesoforge.catalog.contributors import DEFAULT_MODEL_DEFINITIONS, RAP_MODEL_DEFINITION
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import GribIndexError, compute_message_byte_range
from mesoforge.guidance.sources.rap import (
    RAP_CAPABILITIES,
    RapDecodeError,
    _selected_temperature,
    acquire_rap_lead,
    build_grib_url,
    decode_temperature_message,
    discover_rap_cycle,
    maximum_lead,
)
from tests.fixtures.hrrr_grib import NX, NY, make_temperature_message
from tests.support.phase1_fixture_transports import FakeHttpResponse, FixedClock, RecordingSleeper
from tests.support.phase2_provider_transports import (
    InventoryEntry,
    LeadObject,
    ScriptedProviderTransport,
)

TARGET = datetime(2026, 9, 10, 18, tzinfo=UTC)


def _cycle(hour: int) -> datetime:
    return TARGET.replace(hour=hour)


def _object(cycle: datetime, lead: int, payload: bytes = b"inventory-only") -> LeadObject:
    return LeadObject(
        entries=(InventoryEntry(f"TMP:2 m above ground:{lead} hour fcst", payload),),
        cycle_date=cycle.date(),
        cycle_hour=cycle.hour,
    )


def _published(cycle: datetime, leads: range) -> dict[str, LeadObject]:
    return {build_grib_url(cycle=cycle, forecast_hour=lead): _object(cycle, lead) for lead in leads}


def _transport(objects: dict[str, LeadObject]) -> ScriptedProviderTransport:
    return ScriptedProviderTransport(objects=objects, url_for_lead=lambda _: None)


def _discover(transport: ScriptedProviderTransport, **kwargs):
    clock = FixedClock(TARGET + timedelta(minutes=10))
    return discover_rap_cycle(
        target_reference_time=TARGET,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        **kwargs,
    )


def test_shadow_registration_does_not_change_active_defaults():
    assert [model.model_id for model in DEFAULT_MODEL_DEFINITIONS] == ["HRRR", "GFS"]
    assert RAP_MODEL_DEFINITION.status == "shadow"
    assert RAP_MODEL_DEFINITION.supported_fields == ("air_temperature_2m",)
    assert maximum_lead(_cycle(15)) == 51
    assert maximum_lead(_cycle(18)) == 21
    assert build_grib_url(cycle=_cycle(15), forecast_hour=39) == (
        "https://noaa-rap-pds.s3.amazonaws.com/rap.20260910/rap.t15z.awp130pgrbf39.grib2"
    )
    assert RAP_CAPABILITIES["provider_endpoint"] == "noaa_aws"
    assert RAP_CAPABILITIES["bucket_region"] == "us-east-1"


def test_discovery_prefers_complete_older_extended_cycle_and_aligns_valid_times():
    objects = _published(_cycle(15), range(4, 39))  # Newest lacks final required lead39.
    objects.update(_published(_cycle(9), range(10, 46)))
    # A fresher short cycle cannot supply36h and must not displace a complete one.
    objects.update(_published(_cycle(18), range(1, 22)))
    transport = _transport(objects)
    result = _discover(transport)
    assert result.cycle == _cycle(9)
    assert result.source_leads == tuple(range(10, 46))
    assert result.available_leads == result.source_leads
    assert result.missing_hours == {}
    assert result.probes[0].missing_hours.keys() == {36}
    assert result.cycle + timedelta(hours=result.source_leads[0]) == TARGET + timedelta(hours=1)
    assert result.cycle + timedelta(hours=result.source_leads[-1]) == TARGET + timedelta(hours=36)
    assert all(url.endswith(".idx") and headers is None for url, headers in transport.get_calls)
    assert transport.head_calls == []
    assert all(probe.cycle <= TARGET for probe in result.probes)


def test_discovery_reports_partial_short_cycle_only_when_no_complete_cycle_exists():
    transport = _transport(_published(_cycle(18), range(1, 22)))
    result = _discover(transport)
    assert result.cycle == TARGET
    assert result.available_leads == tuple(range(1, 22))
    assert result.source_leads == tuple(range(1, 37))
    assert set(result.missing_hours) == set(range(22, 37))
    assert "partial" in result.reason
    assert all("exceeds cycle coverage" in reason for reason in result.missing_hours.values())


def test_discovery_empty_provider_is_explicit_and_bounded():
    transport = _transport({})
    result = _discover(transport, lookback_hours=6)
    assert result.cycle is None
    assert set(result.missing_hours) == set(range(1, 37))
    assert len(result.probes) == 7
    assert all(TARGET - timedelta(hours=6) <= probe.cycle <= TARGET for probe in result.probes)
    assert len(transport.get_calls) <= 7 * 36


@pytest.mark.parametrize("status,attempts", [(403, 1), (429, 3)])
def test_discovery_stops_on_provider_access_or_rate_rejection(status, attempts):
    transport = ScriptedProviderTransport(
        objects={},
        url_for_lead=lambda _: None,
        fault_script={"noaa-rap-pds": [FakeHttpResponse(status_code=status)] * attempts},
    )
    with pytest.raises(FetchError):
        _discover(transport)
    assert len(transport.get_calls) == attempts
    assert len({url for url, _ in transport.get_calls}) == 1


def test_discovery_rejects_wrong_inventory_field_cycle_and_never_probes_future_cycle():
    objects = _published(_cycle(15), range(4, 40))
    objects[build_grib_url(cycle=_cycle(15), forecast_hour=4)] = _object(_cycle(9), 4)
    url = build_grib_url(cycle=_cycle(15), forecast_hour=5)
    objects[url] = LeadObject(
        entries=(InventoryEntry("TMP:surface:5 hour fcst", b"unused"),),
        cycle_date=TARGET.date(),
        cycle_hour=15,
    )
    transport = _transport(objects)
    with pytest.raises(GribIndexError, match="source cycle"):
        _discover(transport)
    assert len(transport.get_calls) == 1  # Invalid source identity stops the scan immediately.
    objects[build_grib_url(cycle=_cycle(15), forecast_hour=4)] = _object(_cycle(15), 4)
    result = _discover(transport, cycle_override=_cycle(15))
    assert set(result.missing_hours) == {2}
    assert "matched zero" in result.missing_hours[2]
    with pytest.raises(ValueError, match="execution"):
        _discover(transport, cycle_override=_cycle(21))
    clock = FixedClock(_cycle(14))
    result = discover_rap_cycle(
        target_reference_time=TARGET,
        transport=_transport({}),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        lookback_hours=1,
    )
    assert all(probe.cycle <= _cycle(14) for probe in result.probes)


@pytest.fixture(scope="module")
def rap_payload() -> bytes:
    """Reuse the existing temperature GRIB generator, then set RAP native geometry."""
    import eccodes

    initial = make_temperature_message(
        forecast_hour=4,
        values_k=np.full((NY, NX), 281.25),
        cycle_date="20260910",
        cycle_hour=15,
    )
    message = eccodes.codes_new_from_message(initial)
    try:
        for key, value in {
            "Nx": 451,
            "Ny": 337,
            "DxInMetres": 13545.087,
            "DyInMetres": 13545.087,
            "latitudeOfFirstGridPointInDegrees": 16.281,
            "longitudeOfFirstGridPointInDegrees": 233.862,
            "LoVInDegrees": 265.0,
            "Latin1InDegrees": 25.0,
            "Latin2InDegrees": 25.0,
            "LaDInDegrees": 25.0,
            "shapeOfTheEarth": 6,
            "jScansPositively": 1,
        }.items():
            eccodes.codes_set(message, key, value)
        eccodes.codes_set_array(message, "values", np.full(451 * 337, 281.25))
        return bytes(eccodes.codes_get_message(message))
    finally:
        eccodes.codes_release(message)


def test_acquisition_retains_exact_selected_message_and_provenance(rap_payload):
    cycle, lead = _cycle(15), 4
    url = build_grib_url(cycle=cycle, forecast_hour=lead)
    published = _object(cycle, lead, rap_payload)
    # The surface message must not be fetched even though it precedes temperature.
    published.entries = (
        InventoryEntry("TMP:surface:4 hour fcst", b"not-requested"),
        *published.entries,
    )
    transport = _transport({url: published})
    clock = FixedClock(TARGET)
    acquired = acquire_rap_lead(
        cycle=cycle,
        forecast_hour=lead,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        cycle_deadline=TARGET,
    )
    assert acquired.model == "rap"
    assert acquired.endpoint == "noaa_aws"
    assert acquired.forecast_hour == 4
    assert acquired.selected_messages[0].payload == rap_payload
    assert acquired.selected_messages[0].byte_start == len(b"not-requested")
    assert acquired.index_payload == published.index_text().encode()
    assert acquired.resolved_grib_url == url
    assert acquired.resolved_index_url == url + ".idx"
    assert all(attempt.endpoint == "noaa_aws" for attempt in acquired.index_attempts)
    assert all(attempt.endpoint == "noaa_aws" for attempt in acquired.grib_attempts)
    assert acquired.full_object_etag is not None
    assert acquired.full_object_last_modified is not None
    assert acquired.index_completed_at == TARGET + timedelta(seconds=0.5)
    assert acquired.grib_completed_at == TARGET + timedelta(seconds=1.5)
    assert len(transport.range_headers) == 1
    assert transport.head_calls == [url]


def test_acquisition_rejects_ambiguous_temperature_inventory_before_download():
    cycle, lead = _cycle(15), 4
    published = _object(cycle, lead)
    published.entries = published.entries * 2
    transport = _transport({build_grib_url(cycle=cycle, forecast_hour=lead): published})
    clock = FixedClock(TARGET)
    with pytest.raises(GribIndexError, match="ambiguous"):
        acquire_rap_lead(
            cycle=cycle,
            forecast_hour=lead,
            transport=transport,
            clock=clock,
            sleeper=RecordingSleeper(clock),
            cycle_deadline=TARGET,
        )
    assert transport.head_calls == []
    assert transport.range_headers == []


def test_compound_wind_inventory_preserves_standalone_temperature_boundaries(rap_payload):
    cycle, lead = _cycle(15), 4
    index = (
        "1.1:0:d=2026091015:UGRD:100 mb:4 hour fcst:\n"
        "1.2:0:d=2026091015:VGRD:100 mb:4 hour fcst:\n"
        "2:20:d=2026091015:TMP:2 m above ground:4 hour fcst:\n"
        f"3.1:{20 + len(rap_payload)}:d=2026091015:UGRD:10 m above ground:4 hour fcst:\n"
        f"3.2:{20 + len(rap_payload)}:d=2026091015:VGRD:10 m above ground:4 hour fcst:\n"
    )
    rows, selected = _selected_temperature(index.encode(), cycle=cycle, forecast_hour=lead)
    assert len(rows) == 3
    assert selected.line == index.splitlines()[2]
    assert compute_message_byte_range(rows, selected=selected, full_object_length=None) == (
        20,
        20 + len(rap_payload),
    )
    published = _object(cycle, lead, b"W" * 20 + rap_payload + b"wind")
    published.index_text = lambda: index
    url = build_grib_url(cycle=cycle, forecast_hour=lead)
    transport = _transport({url: published})
    clock = FixedClock(TARGET)
    acquired = acquire_rap_lead(
        cycle=cycle,
        forecast_hour=lead,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        cycle_deadline=TARGET,
    )
    assert acquired.index_payload == index.encode()
    assert acquired.selected_messages[0].payload == rap_payload
    assert acquired.selected_messages[0].row.line == index.splitlines()[2]
    assert transport.range_headers == [f"bytes=20-{19 + len(rap_payload)}"]


@pytest.mark.parametrize(
    "lines,reason",
    [
        (["1.1:0:TMP:2 m above ground", "1.2:0:DPT:2 m above ground"], "standalone"),
        (["1.1:0:TMP:2 m above ground"], "standalone"),
        (["1.1:0:UGRD:100 mb", "1.2:10:VGRD:100 mb"], "one offset"),
        (["1.1:0:UGRD:100 mb", "1.3:0:VGRD:100 mb"], "sequential"),
        (["1:0:UGRD:100 mb", "2.2:10:VGRD:100 mb"], "submessage 1"),
        (["1:0:UGRD:100 mb", "3:10:TMP:2 m above ground"], "sequential"),
        (["1:20:UGRD:100 mb", "2:10:TMP:2 m above ground"], "non-monotonic"),
    ],
)
def test_compound_index_rejects_shared_temperature_or_invalid_boundaries(lines, reason):
    text = "\n".join(
        ":".join(line.split(":")[:2])
        + ":d=2026091015:"
        + ":".join(line.split(":")[2:])
        + ":4 hour fcst:"
        for line in lines
    )
    with pytest.raises(GribIndexError, match=reason):
        _selected_temperature(text.encode(), cycle=_cycle(15), forecast_hour=4)


def test_decodes_actual_rap_geometry_temperature_and_valid_time(rap_payload):
    decoded = decode_temperature_message(rap_payload, cycle=_cycle(15), forecast_hour=4)
    assert decoded.shape == (337, 451)
    np.testing.assert_array_equal(decoded.values, np.full((337, 451), 281.25))
    assert decoded.attrs["GRIB_units"] == "K"
    assert decoded.attrs["GRIB_DxInMetres"] == pytest.approx(13545.087)
    assert decoded.attrs["GRIB_Latin1InDegrees"] == 25.0
    assert decoded.attrs["GRIB_validityDate"] == 20260910
    assert decoded.attrs["GRIB_validityTime"] == 1900
    assert decoded.attrs["GRIB_dataTime"] == 1500


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("dataTime", 900, "cycle reference time"),
        ("step", 5, "lead mismatch"),
        ("level", 10, "level mismatch"),
        ("parameterNumber", 6, "no decoded RAP"),
        ("stepType", "accum", "instantaneous"),
        ("jScansPositively", 0, "jScansPositively"),
        ("shapeOfTheEarth", 0, "shapeOfTheEarth"),
    ],
)
def test_decoder_rejects_wrong_source_semantics(rap_payload, key, value, reason):
    import eccodes

    message = eccodes.codes_new_from_message(rap_payload)
    try:
        eccodes.codes_set(message, key, value)
        payload = bytes(eccodes.codes_get_message(message))
    finally:
        eccodes.codes_release(message)
    with pytest.raises(RapDecodeError, match=reason):
        decode_temperature_message(payload, cycle=_cycle(15), forecast_hour=4)
