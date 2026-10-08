"""Discovery identities constrain acquisition without contacting providers."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from mesoforge.guidance.http_fetch import FetchError, fetch_with_range, fetch_with_retry
from mesoforge.guidance.selected_objects import SelectedObjectError, SelectedObjectTransport
from mesoforge.guidance.sources.ifs import IFS_RETRY_POLICY
from tests.support.phase1_fixture_transports import FakeHttpResponse, FixedClock
from tests.unit.guidance.test_current_availability import (
    DECISION,
    LATE,
    MetadataTransport,
    inventory,
    probe,
)
from tests.unit.guidance.test_http_fetch import (
    FixedClock as RetryClock,
)
from tests.unit.guidance.test_http_fetch import (
    RecordingSleeper,
    _grib_message,
)


class AcquisitionTransport(MetadataTransport):
    def __init__(self, model="HRRR"):
        super().__init__(model)
        self.downloaded_bytes = 0
        self.ranged = FakeHttpResponse(
            206,
            {**self.grib.headers, "Content-Length": "80", "Content-Range": "bytes 20-99/200"},
            b"temperature message".ljust(80, b" "),
        )

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append(("GET", url, headers))
        response = self.index if url.endswith((".idx", ".index")) else self.ranged
        self.downloaded_bytes += len(response.content)
        return response

    def head(self, url, *, headers=None, timeout=None):
        self.calls.append(("HEAD", url, headers))
        return self.grib


def selected(model="HRRR", *, evidence=None):
    evidence = evidence or probe(MetadataTransport(model), model).evidence
    underlying = AcquisitionTransport(model)
    wrapper = SelectedObjectTransport(
        underlying,
        [evidence],
        decision_time=DECISION,
        clock=FixedClock(DECISION + timedelta(minutes=2)),
    )
    return wrapper, underlying, evidence


def acquire(wrapper, evidence):
    wrapper.get(evidence["index"]["url"])
    wrapper.head(evidence["grib"]["url"])
    return wrapper.get(evidence["grib"]["url"], headers={"Range": "bytes=20-99"})


def test_qpf_parent_acquires_only_both_proved_gfs_candidates_and_reuses_objects():
    source = MetadataTransport("GFS")
    source.index.content = (
        b"1:0:d=2026091018:TMP:2 m above ground:6 hour fcst:\n"
        b"2:40:d=2026091018:APCP:surface:0-6 hour acc fcst:\n"
        b"3:100:d=2026091018:APCP:surface:0-6 hour acc fcst:\n"
    )
    evidence = probe(source, "GFS", qpf_fields=True).evidence
    evidence["qpf_only"] = True
    underlying = AcquisitionTransport("GFS")
    underlying.index = source.index
    wrapper = SelectedObjectTransport(
        underlying,
        [evidence],
        decision_time=DECISION,
        clock=FixedClock(DECISION + timedelta(minutes=2)),
    )
    wrapper.get(evidence["index"]["url"])
    wrapper.head(evidence["grib"]["url"])
    for start, end in ((40, 100), (100, 200)):
        size = end - start
        underlying.ranged = FakeHttpResponse(
            206,
            {
                **underlying.grib.headers,
                "Content-Length": str(size),
                "Content-Range": f"bytes {start}-{end - 1}/200",
            },
            b"q" * size,
        )
        arguments = {"Range": f"bytes={start}-{end - 1}"}
        original = wrapper.get(evidence["grib"]["url"], headers=arguments)
        assert wrapper.get(evidence["grib"]["url"], headers=arguments).content == original.content
    wrapper.assert_complete()
    assert len(underlying.calls) == 4  # one index, one HEAD, two distinct APCP bodies
    assert all(
        row["canonical_variable_id"] == "liquid_equivalent_precipitation_amount_1h"
        for row in wrapper.validations
        if "canonical_variable_id" in row
    )
    assert all(row.get("byte_start") != 0 for row in wrapper.validations)
    proofs = wrapper.validations
    assert wrapper.cached_range_bytes == 160
    wrapper.release_completed_object(evidence["grib"]["url"])
    assert wrapper.cached_range_bytes == 0
    assert wrapper.validations == proofs
    wrapper.assert_complete()
    # A completed/released object is not re-fetched from a possibly changed
    # provider. Its durable raw evidence is the only replay path.
    before = len(underlying.calls)
    with pytest.raises(SelectedObjectError, match="released"):
        wrapper.get(evidence["grib"]["url"], headers={"Range": "bytes=40-99"})
    assert len(underlying.calls) == before


@pytest.mark.parametrize("model", ["HRRR", "GFS", "RAP", "IFS"])
def test_selected_native_message_matches_discovery_and_retains_acquisition_evidence(model):
    wrapper, underlying, evidence = selected(model)
    response = acquire(wrapper, evidence)
    wrapper.assert_complete()
    assert response.content == underlying.ranged.content
    assert underlying.calls[-1][2] == {"Range": "bytes=20-99", "If-Match": '"grib-version"'}
    assert wrapper.downloaded_bytes == len(inventory(model)) + 80
    records = wrapper.validations
    assert [record["method"] for record in records] == ["GET", "HEAD", "GET"]
    assert all(record["status"] == "matched" for record in records)
    assert all(record["decision_time"] == "2026-09-10T22:00:00Z" for record in records)
    assert records[-1]["sha256"] == hashlib.sha256(response.content).hexdigest()
    assert records[-1]["source_lead_hours"] == 6
    assert records[-1]["full_object_length"] == 200
    assert records[-1]["completed_at"] == "2026-09-10T22:02:00Z"
    assert records[0]["sha256"] == evidence["index"]["sha256"]
    json.dumps(records)


def test_metadata_is_reused_but_each_binary_request_is_conditionally_validated():
    wrapper, underlying, evidence = selected()
    acquire(wrapper, evidence)
    acquire(wrapper, evidence)
    assert [call[0] for call in underlying.calls] == ["GET", "HEAD", "GET", "GET"]
    assert wrapper.downloaded_bytes == len(inventory()) + 160
    assert len(wrapper.validations) == 4
    copied = wrapper.validations
    copied[0]["status"] = "tampered"
    assert wrapper.validations[0]["status"] == "matched"


@pytest.mark.parametrize(
    ("object_name", "field", "replacement", "reason"),
    [
        ("index", "content", b"changed inventory", "Inventory bytes"),
        ("index", "ETag", '"changed"', "ETag"),
        ("index", "Last-Modified", LATE, "Last-Modified"),
        ("grib", "ETag", '"changed"', "ETag"),
        ("grib", "ETag", None, "ETag"),
        ("grib", "Content-Length", "201", "full-object length"),
        ("grib", "Last-Modified", None, "Last-Modified"),
        ("grib", "Last-Modified", LATE, "Last-Modified"),
        ("ranged", "ETag", '"changed-after-head"', "ETag"),
        ("ranged", "Last-Modified", LATE, "Last-Modified"),
        ("ranged", "Content-Range", "bytes 20-99/201", "range/length"),
        ("ranged", "Content-Range", "bytes 21-100/200", "range/length"),
        ("ranged", "Content-Range", None, "range/length"),
        ("ranged", "Content-Length", "79", "range/length"),
        ("ranged", "content", b"truncated", "range/length"),
        ("ranged", "Content-Encoding", "gzip", "Encoded response"),
    ],
)
def test_changed_or_unprovable_identity_fails_and_cannot_be_bypassed_by_retry(
    object_name, field, replacement, reason
):
    wrapper, underlying, evidence = selected()
    response = getattr(underlying, object_name)
    if field == "content":
        response.content = replacement
    elif replacement is None:
        response.headers.pop(field, None)
    else:
        response.headers[field] = replacement
    with pytest.raises(SelectedObjectError, match=reason):
        acquire(wrapper, evidence)
    count = len(underlying.calls)
    with pytest.raises(SelectedObjectError, match=reason):
        wrapper.get(evidence["index"]["url"])
    with pytest.raises(SelectedObjectError, match=reason):
        wrapper.assert_complete()
    assert len(underlying.calls) == count
    assert wrapper.validations[-1]["status"] == "failed"


@pytest.mark.parametrize("status", [200, 404, 412])
def test_range_response_must_be_partial_success_for_the_conditional_request(status):
    wrapper, underlying, evidence = selected()
    underlying.ranged.status_code = status
    with pytest.raises(SelectedObjectError, match=f"HTTP {status}"):
        acquire(wrapper, evidence)
    assert wrapper.validations[-1]["request_headers"]["If-Match"] == '"grib-version"'


class ScriptedRangeTransport(AcquisitionTransport):
    def __init__(self, statuses, *, changed_etag=False):
        super().__init__("IFS")
        self.remaining = list(statuses)
        self.changed_etag = changed_etag
        self.ranged.content = _grib_message(b"\0" * 60)

    def get(self, url, *, headers=None, timeout=None):
        if headers and "Range" in headers:
            self.ranged.status_code = self.remaining.pop(0)
            self.ranged.headers["Retry-After"] = "2"
            if self.changed_etag:
                self.ranged.headers["ETag"] = '"changed-after-head"'
        return super().get(url, headers=headers, timeout=timeout)


def ranged_retry_fixture(statuses, *, changed_etag=False):
    evidence = probe(MetadataTransport("IFS"), "IFS").evidence
    underlying = ScriptedRangeTransport(statuses, changed_etag=changed_etag)
    clock = RetryClock(DECISION + timedelta(minutes=2))
    wrapper = SelectedObjectTransport(underlying, [evidence], decision_time=DECISION, clock=clock)
    wrapper.get(evidence["index"]["url"])
    wrapper.head(evidence["grib"]["url"])
    sleeper = RecordingSleeper(clock)

    def fetch():
        return fetch_with_range(
            wrapper,
            clock,
            sleeper,
            endpoint="ifs",
            url=evidence["grib"]["url"],
            range_header="bytes=20-99",
            byte_start=20,
            byte_end=100,
            retry_policy=IFS_RETRY_POLICY,
            cycle_deadline=clock.now(),
            expected_length=80,
            full_object_length=200,
        )

    return wrapper, underlying, sleeper, fetch


def test_transient_range_503_recovers_with_same_pinned_identity_and_existing_retry_budget():
    # Live commissioning saw one S3 503 poison all later attempts. An HTTP error
    # does not prove an identity change; only a subsequent fully checked 206 can
    # acquire the selected message. Metadata remains pinned and is not fetched again.
    wrapper, underlying, sleeper, fetch = ranged_retry_fixture([503, 206])
    result = fetch()
    wrapper.assert_complete()
    assert result.payload == _grib_message(b"\0" * 60)
    assert [attempt.status_code for attempt in result.attempts] == [503, 206]
    assert sleeper.sleeps == [2]  # Existing Retry-After policy, no new retry loop.
    assert len(underlying.calls) == 4  # Index + HEAD + two conditional range attempts.
    assert all(
        call[2] == {"Range": "bytes=20-99", "If-Match": '"grib-version"'}
        for call in underlying.calls[2:]
    )
    assert [row["status"] for row in wrapper.validations] == [
        "matched",
        "matched",
        "retryable",
        "matched",
    ]
    assert wrapper.validations[2]["status_code"] == 503
    assert wrapper.failed_reason is None and wrapper.failures == []


def test_exhausted_transient_ranges_fail_incomplete_after_exact_existing_attempt_limit():
    wrapper, underlying, sleeper, fetch = ranged_retry_fixture([503, 503, 503, 206])
    with pytest.raises(FetchError, match="exhausted retries"):
        fetch()
    assert len(underlying.calls) == 2 + IFS_RETRY_POLICY.attempts_per_endpoint
    assert underlying.remaining == [206]  # No extra attempt is invented.
    assert sleeper.sleeps == [2, 2]
    assert [row["status"] for row in wrapper.validations[2:]] == ["retryable"] * 3
    with pytest.raises(SelectedObjectError, match="not acquired"):
        wrapper.assert_complete()


@pytest.mark.parametrize("failure", ["precondition", "etag"])
def test_identity_failure_still_latches_without_contacting_provider_again(failure):
    wrapper, underlying, _, fetch = ranged_retry_fixture(
        [412 if failure == "precondition" else 206, 206], changed_etag=failure == "etag"
    )
    with pytest.raises(FetchError, match="exhausted retries"):
        fetch()
    assert len(underlying.calls) == 3  # Existing outer retry cannot bypass identity latch.
    assert underlying.remaining == [206]
    assert wrapper.validations[-1]["status"] == "failed"
    with pytest.raises(SelectedObjectError, match="HTTP 412|ETag"):
        wrapper.assert_complete()


@pytest.mark.parametrize("range_header", [None, "bytes=0-199", "bytes=20-100", "bytes=20-"])
def test_only_the_exact_selected_temperature_range_can_be_downloaded(range_header):
    wrapper, underlying, evidence = selected()
    headers = {} if range_header is None else {"Range": range_header}
    with pytest.raises(SelectedObjectError, match="exactly the temperature range"):
        wrapper.get(evidence["grib"]["url"], headers=headers)
    assert not underlying.calls


def test_unknown_mirror_cannot_be_contacted_and_latches_failure():
    wrapper, underlying, _ = selected()
    with pytest.raises(SelectedObjectError, match="not selected"):
        wrapper.get("https://unselected.test/other-model.idx")
    assert not underlying.calls
    with pytest.raises(SelectedObjectError, match="not selected"):
        wrapper.assert_complete()


def test_each_range_requires_revalidated_index_and_head():
    wrapper, underlying, evidence = selected()
    wrapper.get(evidence["index"]["url"])
    with pytest.raises(SelectedObjectError, match="Revalidate"):
        wrapper.get(evidence["grib"]["url"], headers={"Range": "bytes=20-99"})
    assert len(underlying.calls) == 1


def test_multi_field_ranges_share_identity_and_compound_bytes_without_extra_downloads():
    evidence = probe(MetadataTransport()).evidence
    wind = {
        "canonical_variable_id": "eastward_wind_10m",
        "byte_start": 100,
        "byte_end_exclusive": 200,
        "content_bytes": 100,
        "index_row": "compound u",
    }
    evidence["extra_messages"] = [
        wind,
        {**wind, "canonical_variable_id": "northward_wind_10m", "index_row": "compound v"},
    ]
    wrapper, underlying, evidence = selected(evidence=evidence)
    acquire(wrapper, evidence)
    with pytest.raises(SelectedObjectError, match="not acquired"):
        wrapper.assert_complete()
    underlying.ranged = FakeHttpResponse(
        206,
        {**underlying.grib.headers, "Content-Length": "100", "Content-Range": "bytes 100-199/200"},
        b"shared u and v".ljust(100, b" "),
    )
    url = evidence["grib"]["url"]
    first = wrapper.get(url, headers={"Range": "bytes=100-199"})
    second = wrapper.get(url, headers={"Range": "bytes=100-199"})
    assert first.content == second.content == underlying.ranged.content
    wrapper.assert_complete()
    assert len(underlying.calls) == 4  # One inventory, one HEAD, temperature, compound winds.
    assert len(wrapper.validations) == 4  # Cache hits do not invent provider requests.
    assert wrapper.validations[-1]["if_match"] == evidence["grib"]["etag"]


def test_surface_ranges_reject_partial_overlap_before_network():
    evidence = probe(MetadataTransport()).evidence
    evidence["extra_messages"] = [
        {
            "canonical_variable_id": "eastward_wind_10m",
            "byte_start": 80,
            "byte_end_exclusive": 160,
            "content_bytes": 80,
        }
    ]
    with pytest.raises(ValueError, match="partially overlap"):
        selected(evidence=evidence)


def test_conflicting_conditional_header_is_not_silently_overwritten():
    wrapper, underlying, evidence = selected()
    wrapper.get(evidence["index"]["url"])
    wrapper.head(evidence["grib"]["url"])
    with pytest.raises(SelectedObjectError, match="conflicts"):
        wrapper.get(
            evidence["grib"]["url"], headers={"Range": "bytes=20-99", "if-match": '"other"'}
        )
    assert len(underlying.calls) == 2


def test_completeness_requires_every_selected_message_not_just_metadata():
    wrapper, _, evidence = selected()
    wrapper.get(evidence["index"]["url"])
    wrapper.head(evidence["grib"]["url"])
    with pytest.raises(SelectedObjectError, match="not acquired"):
        wrapper.assert_complete()
    with pytest.raises(SelectedObjectError, match="incompletely acquired"):
        wrapper.release_completed_object(evidence["grib"]["url"])
    acquire(wrapper, evidence)
    wrapper.release_completed_object(evidence["grib"]["url"])
    wrapper.assert_complete()


@pytest.mark.parametrize("mutation", ["unavailable", "late", "cutoff", "weak_etag", "range"])
def test_unsafe_discovery_evidence_is_rejected_before_network(mutation):
    evidence = deepcopy(probe(MetadataTransport()).evidence)
    if mutation == "unavailable":
        evidence["status"] = "unavailable"
    elif mutation == "late":
        evidence["grib"]["last_modified"] = LATE
        evidence["grib"]["available_at"] = "2026-09-10T22:00:01Z"
    elif mutation == "cutoff":
        evidence["decision_time"] = "2026-09-10T23:00:00Z"
    elif mutation == "weak_etag":
        evidence["grib"]["etag"] = 'W/"grib-version"'
    elif mutation == "range":
        evidence["selected_message"]["byte_end_exclusive"] = 201
    with pytest.raises(ValueError):
        selected(evidence=evidence)


def test_unlatched_shadow_view_keeps_acquiring_after_a_provider_failure():
    """A zero-weight shadow's transient 503 stays recorded; later objects are still tried."""
    evidence = probe(MetadataTransport("IFS"), "IFS").evidence
    underlying = AcquisitionTransport("IFS")
    wrapper = SelectedObjectTransport(
        underlying,
        [evidence],
        decision_time=DECISION,
        clock=FixedClock(DECISION + timedelta(minutes=2)),
        latch_failures=False,
    )
    healthy = underlying.index
    underlying.index = FakeHttpResponse(503, {}, b"")
    with pytest.raises(SelectedObjectError, match="HTTP 503"):
        wrapper.get(evidence["index"]["url"])
    assert wrapper.failed_reason is not None
    assert [row["reason"] for row in wrapper.failures] == [
        "Selected object returned HTTP 503; expected 200"
    ]
    underlying.index = healthy
    # Without latching the same object may be retried and every object is still validated.
    response = acquire(wrapper, evidence)
    assert response.content == underlying.ranged.content
    assert [row["status"] for row in wrapper.validations] == [
        "failed",
        "matched",
        "matched",
        "matched",
    ]
    with pytest.raises(SelectedObjectError, match="HTTP 503"):
        wrapper.assert_complete()  # Completeness still reports the recorded failure.
    latched, latched_underlying, latched_evidence = selected("IFS")
    latched_underlying.index = FakeHttpResponse(503, {}, b"")
    with pytest.raises(SelectedObjectError, match="HTTP 503"):
        latched.get(latched_evidence["index"]["url"])
    latched_underlying.index = healthy
    with pytest.raises(SelectedObjectError, match="HTTP 503"):
        latched.get(latched_evidence["index"]["url"])  # The default view stays latched.
    assert len(latched_underlying.calls) == 1


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("changed_identity", [False, True])
def test_opted_metadata_retries_recover_but_never_override_object_identity(
    monkeypatch, method, changed_identity
):
    evidence = probe(MetadataTransport("IFS"), "IFS").evidence
    underlying = AcquisitionTransport("IFS")
    clock = RetryClock(DECISION + timedelta(minutes=2))
    sleeper = RecordingSleeper(clock)
    wrapper = SelectedObjectTransport(
        underlying, [evidence], decision_time=DECISION, clock=clock, retry_metadata=True
    )
    original = getattr(underlying, method)
    calls = []

    def temporary_outage(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return FakeHttpResponse(503, {"Retry-After": "1"})
        response = original(url, **kwargs)
        if changed_identity:
            return FakeHttpResponse(
                response.status_code, {**response.headers, "ETag": '"changed"'}, response.content
            )
        return response

    monkeypatch.setattr(underlying, method, temporary_outage)
    url = evidence["index" if method == "get" else "grib"]["url"]

    def fetch():
        return fetch_with_retry(
            wrapper,
            clock,
            sleeper,
            method=method,
            urls_by_endpoint=[("selected", url)],
            retry_policy=IFS_RETRY_POLICY,
            cycle_deadline=DECISION,
        )

    if changed_identity:
        with pytest.raises(FetchError):
            fetch()
        with pytest.raises(SelectedObjectError, match="ETag differs"):
            wrapper.assert_complete()
        assert [row["status"] for row in wrapper.validations] == ["retryable", "failed"]
    else:
        fetch()
        acquire(wrapper, evidence)
        wrapper.assert_complete()
        assert wrapper.failed_reason is None
        assert sleeper.sleeps == [1.0]
    assert calls.count(url) == 2
    if changed_identity:
        assert len(calls) == 2  # The identity failure latches before another provider request.
