"""IFS native-slot discovery and exact deterministic temperature acquisition, offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.sources.ifs import (
    IFS_CAPABILITIES,
    NO_NATIVE_GUIDANCE,
    IfsDecodeError,
    IfsIndexError,
    IfsTemperatureUnavailableError,
    acquire_ifs_lead,
    build_grib_url,
    build_index_url,
    decode_temperature_message,
    discover_ifs_cycle,
    selected_temperature,
)
from tests.support.phase1_fixture_transports import FakeHttpResponse, FixedClock, RecordingSleeper
from tests.support.phase2_provider_transports import (
    InventoryEntry,
    LeadObject,
    ScriptedProviderTransport,
)

TARGET = datetime(2026, 9, 10, 22, tzinfo=UTC)
CYCLE = TARGET.replace(hour=18)


def _row(cycle=CYCLE, lead=6, **changes):
    row = {
        "domain": "g",
        "date": cycle.strftime("%Y%m%d"),
        "time": cycle.strftime("%H00"),
        "expver": "0001",
        "class": "od",
        "type": "fc",
        "stream": "oper",
        "step": str(lead),
        "levtype": "sfc",
        "param": "2t",
        "_offset": 0,
        "_length": 25,
    }
    row.update(changes)
    return row


class IfsObject(LeadObject):
    def index_text(self):
        cycle = datetime.combine(self.cycle_date, CYCLE.timetz()).replace(hour=self.cycle_hour)
        rows, offset = [], 0
        for entry in self.entries:
            param, lead = entry.descriptor.split(":")
            rows.append(
                json.dumps(
                    _row(cycle, int(lead), param=param, _offset=offset, _length=len(entry.payload))
                )
            )
            offset += len(entry.payload)
        return "\n".join(rows) + "\n"


class IfsTransport(ScriptedProviderTransport):
    """Reuse existing range/HEAD fixture behavior with ECMWF's index suffix."""

    def _object_for(self, url):
        base = url.removesuffix(".index") + ".grib2" if url.endswith(".index") else url
        return self._objects.get(base)


def _published(cycle, leads):
    return {
        build_grib_url(cycle=cycle, forecast_hour=lead): IfsObject(
            entries=(InventoryEntry(f"2t:{lead}", b"inventory-only-placeholder"),),
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
        )
        for lead in leads
    }


def _transport(objects, **kwargs):
    return IfsTransport(
        objects=objects, url_for_lead=lambda _: None, index_suffix=".index", **kwargs
    )


def _discover(transport, **kwargs):
    clock = FixedClock(TARGET + timedelta(minutes=5))
    return discover_ifs_cycle(
        target_reference_time=TARGET,
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        **kwargs,
    )


def test_public_control_product_url_and_native_slots():
    assert build_grib_url(cycle=CYCLE, forecast_hour=6) == (
        "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/20260910/18z/ifs/0p25/oper/"
        "20260910180000-6h-oper-fc.grib2"
    )
    assert build_index_url(cycle=CYCLE, forecast_hour=6).endswith("-6h-oper-fc.index")
    assert IFS_CAPABILITIES["licence"] == "CC-BY-4.0"
    for lead in (1, 2, 91, True):
        with pytest.raises(ValueError, match="three-hour"):
            build_grib_url(cycle=CYCLE, forecast_hour=lead)


def test_inventory_preserves_original_json_and_uses_declared_message_length():
    payload = (
        json.dumps(_row(param="10u", _length=100))
        + "\n"
        + json.dumps(_row(_offset=100, _length=652541))
        + "\n"
    ).encode()
    row, end = selected_temperature(payload, cycle=CYCLE, forecast_hour=6)
    assert row.byte_offset == 100
    assert end == 652641
    assert row.line == payload.decode().splitlines()[1]


@pytest.mark.parametrize(
    "changes",
    [
        {"date": "20260909"},
        {"time": "1200"},
        {"step": "9"},
        {"class": "ai"},
        {"type": "pf"},
        {"stream": "enfo"},
        {"levtype": "pl"},
        {"number": "0"},
        {"model": "aifs"},
        {"_length": -1},
        {"_offset": True},
    ],
)
def test_inventory_rejects_wrong_identity_or_byte_framing(changes):
    with pytest.raises(IfsIndexError):
        selected_temperature(json.dumps(_row(**changes)).encode(), cycle=CYCLE, forecast_hour=6)


def test_inventory_rejects_missing_ambiguous_overlapping_and_duplicate_keys():
    with pytest.raises(IfsTemperatureUnavailableError):
        selected_temperature(json.dumps(_row(param="10u")).encode(), cycle=CYCLE, forecast_hour=6)
    for rows in [(_row(), _row(_offset=25)), (_row(), _row(param="10u", _offset=24))]:
        with pytest.raises(IfsIndexError):
            selected_temperature(
                "\n".join(map(json.dumps, rows)).encode(), cycle=CYCLE, forecast_hour=6
            )
    with pytest.raises(IfsIndexError, match="Duplicate"):
        selected_temperature(b'{"param":"2t","param":"10u"}', cycle=CYCLE, forecast_hour=6)


def test_discovery_prefers_complete_older_cycle_and_aligns_native_valid_times():
    objects = _published(CYCLE, range(6, 39, 3))  # 39 missing in newest native window.
    older = CYCLE - timedelta(hours=6)
    objects.update(_published(older, range(12, 46, 3)))
    transport = _transport(objects)
    result = _discover(transport)
    assert result.cycle == older
    assert result.source_leads == tuple(range(11, 47))
    assert result.native_leads == result.available_leads == tuple(range(12, 46, 3))
    assert set(result.missing_hours) == set(range(1, 37)) - set(range(2, 36, 3))
    assert set(result.missing_hours.values()) == {NO_NATIVE_GUIDANCE}
    assert result.probes[0].missing_hours[35].startswith("IFS native temperature unavailable")
    for lead in result.available_leads:
        hour = lead - 10
        assert older + timedelta(hours=lead) == TARGET + timedelta(hours=hour)
    assert len(transport.get_calls) == 24
    assert all(url.endswith(".index") and headers is None for url, headers in transport.get_calls)
    assert transport.head_calls == []


def test_discovery_override_retains_partial_and_all_requested_hour_reasons():
    result = _discover(_transport(_published(CYCLE, [6, 9])), cycle_override=CYCLE)
    assert result.available_leads == (6, 9)
    assert result.source_leads == tuple(range(5, 41))
    assert len(result.missing_hours) == 34
    assert result.missing_hours[1] == NO_NATIVE_GUIDANCE
    assert "unavailable" in result.missing_hours[8]
    assert len(result.probes) == 1
    with pytest.raises(ValueError, match="execution"):
        _discover(_transport({}), cycle_override=CYCLE + timedelta(hours=6))


def test_discovery_empty_is_bounded_and_never_assumes_nominal_availability():
    transport = _transport({})
    result = _discover(transport, lookback_hours=6)
    assert result.cycle is None
    assert set(result.missing_hours) == set(range(1, 37))
    assert len(result.probes) == 1
    assert len(transport.get_calls) == 12
    assert all(probe.cycle <= TARGET for probe in result.probes)


def test_bounded_discovery_probes_only_requested_native_slots_and_retains_gaps():
    hours = (1, 2, 5, 8, 11)
    transport = _transport(_published(CYCLE, (6, 9, 12, 15)))
    result = _discover(transport, target_horizons=hours)
    assert result.cycle == CYCLE
    assert result.source_leads == (5, 6, 9, 12, 15)
    assert result.native_leads == result.available_leads == (6, 9, 12, 15)
    assert result.missing_hours == {1: NO_NATIVE_GUIDANCE}
    assert len(transport.get_calls) == 4
    assert [url for url, _ in transport.get_calls] == [
        build_index_url(cycle=CYCLE, forecast_hour=lead) for lead in result.native_leads
    ]
    assert transport.head_calls == []


def test_bounded_override_preserves_target_hour_numbers_for_missing_native_leads():
    transport = _transport(_published(CYCLE, (6, 12)))
    result = _discover(transport, target_horizons=(2, 5, 8), cycle_override=CYCLE)
    assert result.source_leads == (6, 9, 12)
    assert result.available_leads == (6, 12)
    assert set(result.missing_hours) == {5}
    assert len(transport.get_calls) == 3


@pytest.mark.parametrize("override", [None, CYCLE])
def test_only_non_native_requested_hours_do_not_claim_an_available_cycle(override):
    transport = _transport({})
    result = _discover(transport, target_horizons=(1, 3, 4), cycle_override=override)
    assert result.cycle is None
    assert result.probes == result.native_leads == result.available_leads == ()
    assert result.missing_hours == {hour: NO_NATIVE_GUIDANCE for hour in (1, 3, 4)}
    assert "no cycle was probed or selected" in result.reason
    assert transport.get_calls == transport.head_calls == []


def test_discovery_stops_on_invalid_identity_instead_of_scanning_more_cycles():
    objects = _published(CYCLE - timedelta(hours=6), [6])
    wrong = next(iter(objects.values()))
    transport = _transport({build_grib_url(cycle=CYCLE, forecast_hour=6): wrong})
    with pytest.raises(IfsIndexError, match="time mismatch"):
        _discover(transport)
    assert len(transport.get_calls) == 1


@pytest.mark.parametrize("status,attempts", [(403, 1), (429, 3)])
def test_discovery_halts_provider_access_or_rate_rejection(status, attempts):
    transport = _transport(
        {}, fault_script={"ecmwf-forecasts": [FakeHttpResponse(status)] * attempts}
    )
    with pytest.raises(FetchError):
        _discover(transport)
    assert len(transport.get_calls) == attempts
    assert len({url for url, _ in transport.get_calls}) == 1


@pytest.fixture(scope="module")
def ifs_payload():
    """Real ecCodes-generated GRIB geometry and IFS operational metadata; synthetic values."""
    import eccodes

    message = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, value in {
            "centre": "ecmf",
            "setLocalDefinition": 1,
            "class": "od",
            "stream": "oper",
            "type": "fc",
            "generatingProcessIdentifier": 161,
            "typeOfGeneratingProcess": 2,
            "typeOfProcessedData": 1,
            "productionStatusOfProcessedData": 0,
            "paramId": 167,
            "dataDate": 20260910,
            "dataTime": 1800,
            "stepType": "instant",
            "step": 6,
            "Ni": 1440,
            "Nj": 721,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
            "latitudeOfFirstGridPointInDegrees": 90.0,
            "latitudeOfLastGridPointInDegrees": -90.0,
            "longitudeOfFirstGridPointInDegrees": 180.0,
            "longitudeOfLastGridPointInDegrees": 179.75,
            "shapeOfTheEarth": 6,
            "iScansNegatively": 0,
            "jScansPositively": 0,
            "jPointsAreConsecutive": 0,
            "alternativeRowScanning": 0,
        }.items():
            eccodes.codes_set(message, key, value)
        values = np.full(721 * 1440, 281.25)
        values[1] = 282.25
        eccodes.codes_set_values(message, values)
        return bytes(eccodes.codes_get_message(message))
    finally:
        eccodes.codes_release(message)


def test_acquisition_retains_only_selected_complete_message_and_original_inventory(ifs_payload):
    url = build_grib_url(cycle=CYCLE, forecast_hour=6)
    published = IfsObject(
        entries=(
            InventoryEntry("10u:6", b"not-downloaded" * 3),
            InventoryEntry("2t:6", ifs_payload),
        ),
        cycle_date=CYCLE.date(),
        cycle_hour=CYCLE.hour,
    )
    transport = _transport({url: published})
    clock = FixedClock(TARGET)
    sleeper = RecordingSleeper(clock)
    result = acquire_ifs_lead(
        cycle=CYCLE,
        forecast_hour=6,
        transport=transport,
        clock=clock,
        sleeper=sleeper,
        cycle_deadline=TARGET,
    )
    assert result.model == "ifs"
    assert result.endpoint == "ecmwf_aws"
    assert result.resolved_grib_url == url
    assert result.index_payload == published.index_text().encode()
    assert result.selected_messages[0].payload == ifs_payload
    assert result.selected_messages[0].byte_start == 42
    assert result.full_object_content_length == 42 + len(ifs_payload)
    assert transport.range_headers == [f"bytes=42-{41 + len(ifs_payload)}"]
    assert result.index_completed_at < result.grib_completed_at
    assert result.full_object_etag
    assert sleeper.sleeps == [0.5, 0.5, 0.5]


def test_decoder_preserves_temperature_identity_time_native_wrapped_grid(ifs_payload):
    field = decode_temperature_message(ifs_payload, cycle=CYCLE, forecast_hour=6)
    assert field.dims == ("latitude", "longitude")
    assert field.shape == (721, 1440)
    assert field.values[0, 0] == 281.25
    assert field.values[0, 1] == 282.25
    assert field.attrs["GRIB_units"] == "K"
    assert field.attrs["GRIB_modelVersion"] == "cy50r1"
    assert field.attrs["GRIB_centre"] == "ecmf"
    assert field.attrs["GRIB_radius"] == 6371229
    assert field.time.values == np.datetime64("2026-09-10T18:00:00")
    assert field.valid_time.values == np.datetime64("2026-09-11T00:00:00")
    assert field.longitude.values[0] == -180  # cfgrib normalizes native180 modulo360.
    assert field.longitude.values[-1] == 179.75


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("generatingProcessIdentifier", 158, "modelVersion"),
        ("class", "ai", "marsClass"),
        ("type", "pf", "marsType"),
        ("step", 9, "lead mismatch"),
        ("dataTime", 1200, "reference time mismatch"),
        ("level", 10, "level mismatch"),
        ("iDirectionIncrementInDegrees", 0.5, "iDirectionIncrementInDegrees"),
    ],
)
def test_decoder_rejects_wrong_model_level_time_or_geometry(ifs_payload, key, value, reason):
    import eccodes

    message = eccodes.codes_new_from_message(ifs_payload)
    try:
        eccodes.codes_set(message, key, value)
        changed = bytes(eccodes.codes_get_message(message))
    finally:
        eccodes.codes_release(message)
    with pytest.raises(IfsDecodeError, match=reason):
        decode_temperature_message(changed, cycle=CYCLE, forecast_hour=6)
