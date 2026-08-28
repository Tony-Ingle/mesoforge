"""Unit tests for mesoforge.observations.sources.aviationweather (plan
Section 2.4, Task 8): exact URL construction and query ordering."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from mesoforge.catalog.sources import AviationWeatherSettings, RetryPolicy
from mesoforge.contracts.observations import RawMetarRecord
from mesoforge.observations.sources.aviationweather import (
    AviationWeatherParseError,
    build_metar_url,
    build_stationinfo_url,
    parse_raw_metar_record,
    parse_raw_metar_response,
    request_headers,
)

_RETRY = RetryPolicy(
    connect_timeout_seconds=10.0,
    read_timeout_seconds=60.0,
    attempts_per_endpoint=4,
    backoff_seconds=(1.0, 2.0, 4.0, 8.0),
    retry_after_cap_seconds=60.0,
)

_SETTINGS = AviationWeatherSettings(
    base_url="https://aviationweather.gov",
    stationinfo_path="/api/data/stationinfo",
    metar_path="/api/data/metar",
    query_parameter_order=("ids", "format", "date", "hours"),
    user_agent="MesoForge/0.1 (+https://example.invalid)",
    max_requests_per_minute=60,
    min_request_interval_seconds=1.0,
    metar_window_hours=6.5,
    metar_completion_offset_minutes=15.0,
    retry_policy=_RETRY,
)


class TestBuildStationinfoUrl:
    def test_builds_exact_url(self) -> None:
        url = build_stationinfo_url(_SETTINGS, station_ids=("KCBG", "KJMR", "KROS"))
        assert url == (
            "https://aviationweather.gov/api/data/stationinfo?ids=KCBG%2CKJMR%2CKROS&format=json"
        )


class TestBuildMetarUrl:
    def test_builds_exact_url_with_canonical_parameter_order(self) -> None:
        url = build_metar_url(
            _SETTINGS,
            station_ids=("KCBG", "KJMR", "KROS"),
            query_date=datetime(2026, 8, 29, 0, 15, tzinfo=UTC),
        )
        assert url == (
            "https://aviationweather.gov/api/data/metar?ids=KCBG,KJMR,KROS&format=json"
            "&date=2026-08-29T00:15:00Z&hours=6.5"
        )

    def test_parameter_order_matches_settings_exactly(self) -> None:
        url = build_metar_url(
            _SETTINGS,
            station_ids=("KCBG",),
            query_date=datetime(2026, 8, 29, 0, 15, tzinfo=UTC),
        )
        query = url.split("?", 1)[1]
        keys_in_order = [pair.split("=")[0] for pair in query.split("&")]
        assert keys_in_order == ["ids", "format", "date", "hours"]


class TestRequestHeaders:
    def test_includes_descriptive_user_agent(self) -> None:
        headers = request_headers(_SETTINGS)
        assert headers["User-Agent"] == _SETTINGS.user_agent


def _live_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "icaoId": "KCBG",
        "receiptTime": "2026-08-28T20:56:23.517Z",
        "obsTime": 1787950380,
        "reportTime": "2026-08-28T21:00:00.000Z",
        "temp": 29.4,
        "dewp": 15.0,
        "wdir": 190.0,
        "wspd": 6.0,
        "visib": "10+",
        "altim": 1019.4,
        "slp": 1018.2,
        "qcField": 12.0,
        "presTend": -1.6,
        "metarType": "METAR",
        "rawOb": "METAR KCBG 282053Z 19006KT 10SM SCT250 29/15 A3010",
        "lat": 45.557,
        "lon": -93.264,
        "elev": 285.0,
        "name": "Cambridge Muni, MN, US",
        "cover": "SCT",
        "clouds": [{"cover": "SCT", "base": 25000}],
        "fltCat": "VFR",
    }
    record.update(overrides)
    return record


class TestParseRawMetarRecord:
    """Codex review t_09a43c6c finding 6: the production parser must map
    the provider's exact camelCase schema strictly into RawMetarRecord,
    including its documented extra fields the model does not retain."""

    def test_maps_live_shaped_record_exactly(self) -> None:
        record = parse_raw_metar_record(_live_record())
        assert isinstance(record, RawMetarRecord)
        assert record.icao_id == "KCBG"
        assert record.metar_type == "METAR"
        assert record.temp == pytest.approx(29.4)
        assert record.wdir == pytest.approx(190.0)
        assert record.wspd == pytest.approx(6.0)
        assert record.qc_field == pytest.approx(12.0)
        assert record.lat == pytest.approx(45.557)
        assert record.lon == pytest.approx(-93.264)
        assert record.elev == pytest.approx(285.0)
        assert record.obs_time.tzinfo is not None

    def test_obs_time_parsed_from_epoch_seconds(self) -> None:
        record = parse_raw_metar_record(_live_record(obsTime=1787950380))
        assert record.obs_time == datetime.fromtimestamp(1787950380, tz=UTC)

    def test_report_and_receipt_time_parsed_from_iso_string(self) -> None:
        record = parse_raw_metar_record(_live_record())
        assert record.report_time == datetime(2026, 8, 28, 21, 0, 0, tzinfo=UTC)
        assert record.receipt_time == datetime(2026, 8, 28, 20, 56, 23, 517000, tzinfo=UTC)

    def test_vrb_wind_direction_maps_through(self) -> None:
        record = parse_raw_metar_record(_live_record(wdir="VRB"))
        assert record.wdir == "VRB"

    def test_null_temp_wind_qc_are_optional(self) -> None:
        record = parse_raw_metar_record(_live_record(temp=None, wdir=None, wspd=None, qcField=None))
        assert record.temp is None
        assert record.wdir is None
        assert record.wspd is None
        assert record.qc_field is None

    def test_rejects_missing_required_key(self) -> None:
        record = _live_record()
        del record["icaoId"]
        with pytest.raises(AviationWeatherParseError, match="missing required provider key"):
            parse_raw_metar_record(record)

    def test_rejects_malformed_obs_time(self) -> None:
        with pytest.raises(AviationWeatherParseError, match="obsTime"):
            parse_raw_metar_record(_live_record(obsTime="not-a-time"))

    def test_rejects_wrong_type_for_lat(self) -> None:
        with pytest.raises(AviationWeatherParseError):
            parse_raw_metar_record(_live_record(lat="not-a-float"))

    def test_extra_provider_fields_are_ignored_not_rejected(self) -> None:
        # dewp/visib/altim/slp/presTend/name/cover/clouds/fltCat are
        # real provider fields RawMetarRecord does not model; they must
        # not cause a strict-validation failure (only unrecognized
        # *required* mapped fields are enforced).
        record = parse_raw_metar_record(_live_record(someBrandNewField="unexpected"))
        assert record.icao_id == "KCBG"


class TestParseRawMetarResponse:
    def test_parses_json_array_in_order(self) -> None:
        payload = json.dumps([_live_record(icaoId="KCBG"), _live_record(icaoId="KJMR")]).encode()
        records = parse_raw_metar_response(payload)
        assert [r.icao_id for r in records] == ["KCBG", "KJMR"]

    def test_empty_body_yields_empty_list(self) -> None:
        assert parse_raw_metar_response(b"") == []
        assert parse_raw_metar_response(b"   ") == []

    def test_canonicalized_empty_array_yields_empty_list(self) -> None:
        assert parse_raw_metar_response(b"[]") == []

    def test_rejects_non_array_json(self) -> None:
        with pytest.raises(AviationWeatherParseError, match="must be a JSON array"):
            parse_raw_metar_response(b'{"not": "an array"}')

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(AviationWeatherParseError, match="not valid JSON"):
            parse_raw_metar_response(b"not json")
