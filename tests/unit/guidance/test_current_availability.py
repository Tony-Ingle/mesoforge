"""Metadata-only provider completeness evidence; no model body or live network."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.guidance.sources.current_availability import (
    ProviderEvidenceError,
    probe_temperature,
)
from tests.support.phase1_fixture_transports import FakeHttpResponse, FixedClock, RecordingSleeper
from tests.unit.application.test_prepared_temperature import phase2_configuration

CYCLE = datetime(2026, 9, 10, 18, tzinfo=UTC)
DECISION = CYCLE + timedelta(hours=4)
PUBLISHED = "Thu, 10 Sep 2026 21:00:00 GMT"
LATE = "Thu, 10 Sep 2026 22:00:01 GMT"


def inventory(model="HRRR", **changes):
    if model == "IFS":
        row = {
            "domain": "g",
            "date": "20260910",
            "time": "1800",
            "expver": "0001",
            "class": "od",
            "type": "fc",
            "stream": "oper",
            "step": "6",
            "levtype": "sfc",
            "param": "2t",
            "_offset": 20,
            "_length": 80,
        }
        row.update(changes)
        return json.dumps(row).encode()
    return (
        b"1:0:d=2026091018:UGRD:10 m above ground:6 hour fcst:\n"
        b"2:20:d=2026091018:TMP:2 m above ground:6 hour fcst:\n"
        b"3:100:d=2026091018:DPT:2 m above ground:6 hour fcst:\n"
    )


class MetadataTransport:
    def __init__(self, model="HRRR", *, index=None, grib=None):
        self.index = index or FakeHttpResponse(
            200, {"Last-Modified": PUBLISHED, "ETag": '"inventory-version"'}, inventory(model)
        )
        self.grib = grib or FakeHttpResponse(
            200, {"Last-Modified": PUBLISHED, "ETag": '"grib-version"', "Content-Length": "200"}
        )
        self.calls = []

    def get(self, url, *, headers=None, timeout=None):
        assert url.endswith((".idx", ".index")), "GRIB body download is forbidden"
        assert headers is None
        self.calls.append(("GET", url))
        return self.index

    def head(self, url, *, headers=None, timeout=None):
        assert not url.endswith((".idx", ".index"))
        assert headers is None
        self.calls.append(("HEAD", url))
        return self.grib


def probe(transport, model="HRRR", **kwargs):
    clock = FixedClock(DECISION + timedelta(minutes=1))
    return probe_temperature(
        model=model,
        cycle=CYCLE,
        lead=6,
        configuration=phase2_configuration(),
        transport=transport,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        decision_time=DECISION,
        **kwargs,
    )


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
def test_surface_inventory_uses_same_identity_and_preserves_optional_missingness(model):
    transport = MetadataTransport(model)
    if model == "IFS":
        first = json.loads(inventory(model))
        payload = "\n".join(
            json.dumps({**first, "param": param, "_offset": index * 40, "_length": 40})
            for index, param in enumerate(("2t", "2d", "10u", "10v"))
        ).encode()
    elif model == "RAP":
        payload = (
            b"1:0:d=2026091018:TMP:2 m above ground:6 hour fcst:\n"
            b"2:40:d=2026091018:DPT:2 m above ground:6 hour fcst:\n"
            b"3.1:80:d=2026091018:UGRD:10 m above ground:6 hour fcst:\n"
            b"3.2:80:d=2026091018:VGRD:10 m above ground:6 hour fcst:\n"
            b"4:120:d=2026091018:GUST:surface:6 hour fcst:\n"
        )
    else:
        payload = (
            b"1:0:d=2026091018:TMP:2 m above ground:6 hour fcst:\n"
            b"2:40:d=2026091018:DPT:2 m above ground:6 hour fcst:\n"
            b"3:80:d=2026091018:UGRD:10 m above ground:6 hour fcst:\n"
            b"4:120:d=2026091018:VGRD:10 m above ground:6 hour fcst:\n"
            b"5:160:d=2026091018:GUST:surface:0-6 hour max fcst:\n"
        )
    transport.index.content = payload
    result = probe(transport, model, surface_fields=True)
    assert result.available
    assert [method for method, _ in transport.calls] == ["GET", "HEAD"]
    evidence = result.evidence
    assert evidence["selected_message"]["canonical_variable_id"] == "air_temperature_2m"
    fields = {entry["canonical_variable_id"]: entry for entry in evidence["extra_messages"]}
    assert set(fields) == {
        "dew_point_temperature_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
    } | ({"wind_gust_10m"} if model == "RAP" else set())
    if model == "RAP":
        assert fields["eastward_wind_10m"]["byte_start"] == 80
        assert fields["northward_wind_10m"]["byte_start"] == 80
        assert fields["northward_wind_10m"]["byte_end_exclusive"] == 120
        assert fields["northward_wind_10m"]["index_row"].startswith("3.2:")
        assert evidence["missing_fields"] == {}
    else:
        assert set(evidence["missing_fields"]) == {"wind_gust_10m"}
        assert "instantaneous" in evidence["missing_fields"]["wind_gust_10m"]
    assert evidence["index"]["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
def test_native_temperature_inventory_and_full_object_head_prove_availability(model):
    transport = MetadataTransport(model)
    result = probe(transport, model)
    assert result.available and result.reason is None
    assert [method for method, _ in transport.calls] == ["GET", "HEAD"]
    evidence = result.evidence
    assert evidence["cycle"] == "2026-09-10T18:00:00Z"
    assert evidence["source_lead_hours"] == 6
    assert evidence["valid_time"] == "2026-09-11T00:00:00Z"
    assert evidence["decision_time"] == "2026-09-10T22:00:00Z"
    assert evidence["index"]["available_at"] == "2026-09-10T21:00:00Z"
    assert evidence["grib"]["available_at"] == "2026-09-10T21:00:00Z"
    assert evidence["grib"]["etag"] == '"grib-version"'
    assert evidence["grib"]["content_length"] == 200
    assert evidence["selected_message"]["byte_start"] == 20
    assert evidence["selected_message"]["byte_end_exclusive"] == 100
    assert evidence["selected_message"]["content_bytes"] == 80
    assert evidence["index"]["sha256"] == hashlib.sha256(inventory(model)).hexdigest()
    assert result.index_payload == inventory(model)
    assert result.index_payloads == {evidence["index"]["url"]: inventory(model)}
    assert evidence["endpoints"][0]["requests"][1]["method"] == "HEAD"
    json.dumps(evidence)


@pytest.mark.parametrize("object_kind", ["index", "grib"])
def test_missing_object_is_explicit_unavailable_with_recorded_status(object_kind):
    transport = MetadataTransport(**{object_kind: FakeHttpResponse(404)})
    result = probe(transport)
    assert not result.available
    assert "404" in result.reason
    assert all(
        endpoint[object_kind]["status_code"] == 404 for endpoint in result.evidence["endpoints"]
    )
    if object_kind == "index":
        assert all(method == "GET" for method, _ in transport.calls)
        assert not result.index_payloads


@pytest.mark.parametrize("object_kind", ["index", "grib"])
def test_objects_published_after_decision_time_are_not_admitted(object_kind):
    transport = MetadataTransport()
    getattr(transport, object_kind).headers["Last-Modified"] = LATE
    result = probe(transport)
    assert not result.available
    assert "after decision time" in result.reason
    if object_kind == "index":
        assert all(method == "GET" for method, _ in transport.calls)


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
def test_absent_temperature_field_is_explicit_unavailable(model):
    transport = MetadataTransport(model)
    transport.index.content = (
        inventory(model, param="10u")
        if model == "IFS"
        else inventory(model).replace(b":TMP:", b":PRES:")
    )
    result = probe(transport, model)
    assert not result.available
    assert all(method == "GET" for method, _ in transport.calls)


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
@pytest.mark.parametrize("mismatch", ["cycle", "lead"])
def test_wrong_inventory_identity_fails_closed(model, mismatch):
    transport = MetadataTransport(model)
    if model == "IFS":
        transport.index.content = inventory(
            model, **({"time": "1200"} if mismatch == "cycle" else {"step": "9"})
        )
    else:
        transport.index.content = (
            inventory(model).replace(b"d=2026091018", b"d=2026091012")
            if mismatch == "cycle"
            else inventory(model).replace(b"6 hour fcst", b"9 hour fcst")
        )
    with pytest.raises(ProviderEvidenceError) as caught:
        probe(transport, model)
    assert caught.value.index_payload == transport.index.content
    assert caught.value.evidence["status"] == "error"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("object_kind", ["index", "grib"])
@pytest.mark.parametrize("last_modified", [None, "not-a-date"])
def test_unprovable_publication_does_not_silently_use_retrieval_time(object_kind, last_modified):
    transport = MetadataTransport()
    if last_modified is None:
        getattr(transport, object_kind).headers.pop("Last-Modified")
    else:
        getattr(transport, object_kind).headers["Last-Modified"] = last_modified
    with pytest.raises(ProviderEvidenceError, match="cannot prove publication"):
        probe(transport)


@pytest.mark.parametrize("length", [None, "garbage", "-1", "0", "99"])
def test_invalid_or_truncated_object_length_fails_closed(length):
    transport = MetadataTransport()
    if length is None:
        transport.grib.headers.pop("Content-Length")
    else:
        transport.grib.headers["Content-Length"] = length
    with pytest.raises(ProviderEvidenceError):
        probe(transport)


@pytest.mark.parametrize("etag", [None, 'W/"weak-version"', "unquoted-version"])
def test_unknown_or_weak_grib_identity_fails_closed(etag):
    transport = MetadataTransport()
    if etag is None:
        transport.grib.headers.pop("ETag")
    else:
        transport.grib.headers["ETag"] = etag
    with pytest.raises(ProviderEvidenceError, match="strong ETag"):
        probe(transport)


@pytest.mark.parametrize("status", [403, 429, 503])
def test_access_rate_and_transport_errors_are_not_treated_as_missing_runs(status):
    transport = MetadataTransport(index=FakeHttpResponse(status))
    with pytest.raises(ProviderEvidenceError) as caught:
        probe(transport)
    assert caught.value.evidence["status"] == "error"
    assert len(caught.value.evidence["endpoints"]) == 1
    requests = caught.value.evidence["endpoints"][0]["requests"]
    assert requests
    assert all(request["status_code"] == status for request in requests)


def test_all_failover_inventories_are_retained_even_when_first_was_late():
    class LaterMirror(MetadataTransport):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            return FakeHttpResponse(
                200,
                {"Last-Modified": LATE if len(self.calls) == 1 else PUBLISHED},
                response.content,
            )

    transport = LaterMirror()
    result = probe(transport)
    assert result.available
    assert len(result.index_payloads) == 2
    assert result.evidence["endpoints"][0]["status"] == "unavailable"
    assert result.evidence["endpoints"][1]["status"] == "available"


def test_ambiguous_temperature_is_not_a_complete_run():
    transport = MetadataTransport()
    transport.index.content = inventory().replace(b":DPT:", b":TMP:")
    with pytest.raises(ProviderEvidenceError, match="ambiguous"):
        probe(transport)


class _MissingArchivesDeniedFinalMirror(MetadataTransport):
    def __init__(self, final_status=403):
        super().__init__("GFS")
        self.final_status = final_status

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if "nomads.ncep.noaa.gov" not in url:
            return FakeHttpResponse(404)
        if isinstance(self.final_status, Exception):
            raise self.final_status
        return FakeHttpResponse(self.final_status)


def test_explicit_missing_archives_and_denied_final_mirror_reject_only_this_candidate():
    result = probe(_MissingArchivesDeniedFinalMirror(), "GFS")
    assert not result.available
    assert result.evidence["status"] == "unavailable"
    assert "availability is unknown" in result.reason
    attempts = result.evidence["endpoints"]
    assert [attempt["status"] for attempt in attempts] == [
        "unavailable",
        "unavailable",
        "access_denied",
    ]
    assert [attempt["requests"][-1]["status_code"] for attempt in attempts] == [404, 404, 403]
    assert attempts[-1]["availability"] == "unknown"
    assert "HTTP 403" in attempts[-1]["reason"]
    assert "selected_endpoint" not in result.evidence


@pytest.mark.parametrize("failure", [429, RuntimeError("network unavailable")])
def test_missing_archives_do_not_hide_rate_or_transport_failure(failure):
    with pytest.raises(ProviderEvidenceError) as caught:
        probe(_MissingArchivesDeniedFinalMirror(failure), "GFS")
    assert caught.value.evidence["status"] == "error"


def test_all_denied_mirrors_still_fail_without_independent_missing_evidence():
    transport = MetadataTransport("GFS", index=FakeHttpResponse(403))
    with pytest.raises(ProviderEvidenceError) as caught:
        probe(transport, "GFS")
    assert caught.value.evidence["status"] == "error"
    assert len(caught.value.evidence["endpoints"]) == 1
