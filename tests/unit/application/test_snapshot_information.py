"""Input clocks are independent; fixed fixture times do not claim provider availability."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from mesoforge.application import forecast_from_snapshot as fast
from mesoforge.application import prepared_snapshot as snapshots

EARLY = "2026-09-18T00:40:00Z"
DECISION = "2026-09-18T00:45:00Z"
LATER = "2026-09-18T00:55:00Z"
PUBLISHED = "2026-09-18T01:00:00Z"
REQUEST = datetime(2026, 9, 18, 1, 5, tzinfo=UTC)


def _input(available=EARLY, retrieved=LATER):
    return {
        "model": "NBM",
        "cycle": "2026-09-17T18:00:00Z",
        "source_lead_hours": 7,
        "grib_available_at": available,
        "index_available_at": available,
        "grib_retrieved_at": retrieved,
        "index_retrieved_at": retrieved,
        "source_grib_url": "https://example.test/fixture.grib2",
        "raw_sha256": "a" * 64,
    }


def _manifest(directory, inputs, **extra):
    directory.mkdir(parents=True)
    payload = json.dumps({"inputs": inputs, "created_at": PUBLISHED, **extra}).encode()
    (directory / "manifest.json").write_bytes(payload)
    return {"directory": str(directory), "manifest_sha256": hashlib.sha256(payload).hexdigest()}


def evidence_fixture(tmp_path: Path):
    control = _manifest(tmp_path / "control", [_input()])
    pop = _manifest(
        tmp_path / "pop",
        [_input(LATER)],
        selection_evidence={"selection": {"decision_time": LATER}},
    )
    cloud = _manifest(tmp_path / "cloud", [_input(LATER)])
    return {
        "directory": control["directory"],
        "shadow_directories": {},
        "current_model_set": {"selection": {"decision_time": DECISION}},
        "pop_guidance": pop,
        "cloud_guidance": {"sources": [{**cloud, "model": "NBM"}]},
    }


def test_mixed_cutoffs_are_preserved_without_retroactive_attachment_claims(tmp_path):
    preparation = evidence_fixture(tmp_path)
    information = snapshots.source_information(preparation)
    assert information["status"] == "complete"
    control, pop, cloud = information["sources"]
    assert [s["selection_cutoff"] for s in (control, pop, cloud)] == [DECISION, LATER, None]
    assert cloud["inputs"][0]["grib_available_at"] == LATER
    assert control["inputs"][0]["grib_retrieved_at"] == LATER
    assert (
        snapshots.check_information_cutoff(
            information, analysis_cutoff=REQUEST, published_at=PUBLISHED, completed_at=PUBLISHED
        )
        == []
    )
    assert "Every input was available before the snapshot's decision" not in fast.PROVENANCE_RULE


@pytest.mark.parametrize(
    "clock", ["grib_available_at", "index_available_at", "grib_retrieved_at", "index_retrieved_at"]
)
def test_each_input_clock_must_precede_analysis_cutoff(tmp_path, clock):
    information = snapshots.source_information(evidence_fixture(tmp_path))
    information["sources"][2]["inputs"][0][clock] = "2026-09-18T01:06:00Z"
    problems = snapshots.check_information_cutoff(
        information, analysis_cutoff=REQUEST, published_at=PUBLISHED, completed_at=PUBLISHED
    )
    assert len(problems) == 1 and clock in problems[0] and "analysis cutoff" in problems[0]


def test_publication_is_distinct_from_reference_and_analysis_time(tmp_path):
    information = snapshots.source_information(evidence_fixture(tmp_path))
    problems = snapshots.check_information_cutoff(
        information,
        analysis_cutoff=REQUEST,
        published_at="2026-09-18T01:10:00Z",
        completed_at=PUBLISHED,
    )
    assert len(problems) == 1 and "snapshot publication" in problems[0]


def test_unknown_historical_evidence_is_reported_without_invented_timestamp(tmp_path):
    preparation = evidence_fixture(tmp_path)
    preparation["shadow_directories"] = {"RAP": str(tmp_path / "old-shadow")}
    information = snapshots.source_information(preparation)
    assert information["status"] == "unproven"
    assert "RAP: retained input manifest unavailable" in information["limitations"]
    assert information["sources"][1]["inputs"] == []


def test_attachment_digest_is_checked(tmp_path):
    preparation = evidence_fixture(tmp_path)
    path = Path(preparation["pop_guidance"]["directory"]) / "manifest.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(snapshots.SnapshotError, match="digest mismatch"):
        snapshots.source_information(preparation)


def test_issuance_refuses_unproven_or_changed_evidence_before_load_or_writes(tmp_path, monkeypatch):
    preparation = evidence_fixture(tmp_path)
    information = snapshots.source_information(preparation)
    manifest = {"completed_at": PUBLISHED, "source_information": deepcopy(information)}
    monkeypatch.setattr(
        fast, "resolve_latest_complete", lambda _: ({"published_at": PUBLISHED}, manifest, tmp_path)
    )
    monkeypatch.setattr(fast, "verify_prepared_run", lambda _: preparation)
    forbidden = Mock(side_effect=AssertionError("must not load/issue unproven input"))
    monkeypatch.setattr(fast, "load_preparation", forbidden)
    monkeypatch.setattr(fast, "create_issuer", forbidden)
    # New manifests pin the evidence itself; changed clocks cannot evade the cutoff gate.
    manifest["source_information"]["sources"][0]["selection_cutoff"] = EARLY
    result = fast.forecast_from_snapshot(tmp_path, [], request_time=REQUEST, issue=True)
    assert result["status"] == "no_current_snapshot"
    assert "differs" in result["reason"]
    # Historical snapshots lack the summary: rebuild facts, but do not issue without proof.
    manifest.pop("source_information")
    preparation["shadow_directories"] = {"RAP": str(tmp_path / "missing")}
    result = fast.forecast_from_snapshot(tmp_path, [], request_time=REQUEST, issue=True)
    assert result["status"] == "no_current_snapshot"
    assert result["information_cutoff"]["status"] == "unproven"
    forbidden.assert_not_called()


def test_naive_reference_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Reference time must include"):
        fast.forecast_from_snapshot(tmp_path, [], reference_time=datetime(2026, 9, 18, 1))


def test_loaded_region_clocks_cannot_differ_from_audited_source(tmp_path):
    preparation = evidence_fixture(tmp_path)
    region = _manifest(tmp_path / "region", [_input(retrieved="2026-09-18T02:00:00Z")])
    directory = tmp_path / "coverage"
    directory.mkdir()
    (directory / "coverage.json").write_text(
        json.dumps(
            {
                "source_directory": preparation["directory"],
                "regions": [region],
            }
        )
    )
    preparation["directory"] = str(directory)
    with pytest.raises(snapshots.SnapshotError, match="regional input evidence differs"):
        snapshots.source_information(preparation)


def test_invalid_provider_header_retains_retrieval_availability_basis(tmp_path):
    preparation = evidence_fixture(tmp_path)
    path = Path(preparation["directory"]) / "manifest.json"
    original = json.loads(path.read_bytes())
    original["inputs"][0]["grib_last_modified"] = "not a date"
    path.write_text(json.dumps(original))
    information = snapshots.source_information(preparation)
    assert (
        information["sources"][0]["inputs"][0]["availability_basis"]["grib"]
        == "acquisition known-by bound"
    )


def test_historical_relative_region_paths_remain_readable(tmp_path):
    preparation = evidence_fixture(tmp_path)
    source = Path(preparation["directory"])
    coverage = tmp_path / "coverage"
    region = _manifest(coverage / "region", [_input()])
    (coverage / "coverage.json").write_text(
        json.dumps(
            {
                "source_directory": str(source),
                "regions": [{**region, "directory": "region"}],
            }
        )
    )
    preparation["directory"] = str(coverage)
    information = snapshots.source_information(preparation)
    assert information["status"] == "complete"
    assert information["sources"][0]["region_manifests"][0]["manifest_file"] == str(
        coverage / "region" / "manifest.json"
    )


@pytest.mark.parametrize("model", ["RAP", "IFS"])
@pytest.mark.parametrize("usable_event", [False, True])
def test_undiscovered_shadow_type_events_do_not_require_fabricated_input_clocks(
    tmp_path, model, usable_event
):
    from datetime import timedelta

    preparation = evidence_fixture(tmp_path)
    # This is prepare_type_run's actual absent-cycle shape: no raw inputs, no
    # missing_hours/request_failures keys, and individually explicit missing events.
    events = [
        {
            "event_id": f"{model}:{hour}",
            "source_cycle": None,
            "source_lead_hours": None,
            "valid_time": (REQUEST + timedelta(hours=hour)).isoformat(),
            "temporal_semantics": "instantaneous",
            "interval_start": None,
            "interval_end": None,
            "missing_reasons": ["No existing selected cycle for this type source"],
        }
        for hour in range(1, 37)
    ]
    if usable_event:
        events[0]["missing_reasons"] = []
    descriptor = _manifest(tmp_path / model, [], model=model, events=events)
    preparation["ptype_guidance"] = {"sources": [{**descriptor, "model": model}]}
    information = snapshots.source_information(preparation)
    source = next(
        row for row in information["sources"] if row["source"] == f"ptype_guidance:{model}"
    )
    assert source["source"] == f"ptype_guidance:{model}"
    assert source["inputs"] == []
    assert source["selection_cutoff"] is None
    problems = snapshots.check_information_cutoff(
        information, analysis_cutoff=REQUEST, published_at=PUBLISHED, completed_at=PUBLISHED
    )
    if usable_event:
        assert information["status"] == "unproven"
        assert problems == [f"ptype_guidance:{model}: no retained input timing evidence"]
    else:
        assert information["status"] == "complete"
        assert problems == []
