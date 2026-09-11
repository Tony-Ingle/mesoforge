"""Discovery identities constrain acquisition without contacting providers."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from mesoforge.guidance.selected_objects import SelectedObjectError, SelectedObjectTransport
from tests.support.phase1_fixture_transports import FakeHttpResponse, FixedClock
from tests.unit.guidance.test_current_availability import (
    DECISION,
    LATE,
    MetadataTransport,
    inventory,
    probe,
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


@pytest.mark.parametrize("status", [200, 404, 412, 503])
def test_range_response_must_be_partial_success_for_the_conditional_request(status):
    wrapper, underlying, evidence = selected()
    underlying.ranged.status_code = status
    with pytest.raises(SelectedObjectError, match=f"HTTP {status}"):
        acquire(wrapper, evidence)
    assert wrapper.validations[-1]["request_headers"]["If-Match"] == '"grib-version"'


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
