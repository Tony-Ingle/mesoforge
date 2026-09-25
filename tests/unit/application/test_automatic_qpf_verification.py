"""Retryable acquisition is separate from immutable exact-event measurement."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application import automatic_qpf_verification as automatic
from mesoforge.application import forward_verification
from mesoforge.application.artifacts import SourceRegistrationRequest
from mesoforge.application.forecast_from_baseline import forecast_from_baseline
from mesoforge.application.prepared_mrms import MRMSUnavailableError
from mesoforge.common.identifiers import Digest
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.verification.issued_qpf import FIELD
from tests.unit.application.test_issued_qpf_verification import case as case
from tests.unit.verification.test_issued_qpf import (
    CUTOFF,
    LAT,
    LON,
    VALID,
    observation,
    saved_forecast,
)


def run(case, resolver, **overrides):
    return automatic.accumulate_window(
        **{
            "service": case.service,
            "resolve_hour": resolver,
            "latitude": LAT,
            "longitude": LON,
            "start_valid_time": VALID,
            "end_valid_time": VALID + timedelta(hours=1),
            "max_issuances": 10,
            "max_opportunities": 10,
            **overrides,
        }
    )


def retained(case):
    return {
        "extraction_artifact_id": str(case.extraction.artifact_id),
        "acquisition": {"acquired_bytes": 0, "provider_calls": 0, "extraction_bytes": 42},
    }


@pytest.mark.parametrize(
    ("evaluation", "status"),
    [
        (VALID - timedelta(minutes=1), "future"),
        (VALID, "not_yet_expected"),
        (VALID + timedelta(minutes=59), "not_yet_expected"),
    ],
)
def test_incomplete_and_latency_defer_without_observation_or_fact(case, evaluation, status):
    resolver = Mock(side_effect=AssertionError("ineligible request"))
    state = deepcopy(case.factory.artifacts)
    result = run(case, resolver, now=evaluation)
    assert result["summary"] == {status: 2}
    assert case.factory.artifacts == state
    resolver.assert_not_called()


def test_missing_product_retries_then_matches_and_reuses_without_fetch(case):
    original = case.issuer.read(case.record.issued_forecast_id)
    resolver = Mock(side_effect=MRMSUnavailableError("not yet published"))
    first = run(case, resolver)
    assert first["summary"] == {"retryable": 2}
    assert resolver.call_count == 1  # Same hour across both stages.
    assert not any(
        m.artifact_type == "issued-qpf-verification" for m in case.factory.artifacts.values()
    )
    resolver.side_effect = None
    resolver.return_value = retained(case)
    second = run(case, resolver)
    assert second["summary"] == {"matched": 2}
    ids = [r["verification_id"] for r in second["results"]]
    resolver.reset_mock()
    repeat = run(case, resolver)
    assert repeat["summary"] == {"already_existing": 2}
    assert [r["verification_ids"] for r in repeat["results"]] == [[i] for i in ids]
    resolver.assert_not_called()
    assert case.issuer.read(case.record.issued_forecast_id) == original


def test_old_observation_missing_fact_does_not_suppress_retry(case):
    case.service.verify(case.record.issued_forecast_id, VALID)
    result = run(case, Mock(return_value=retained(case)), stages=("final_issued",))
    assert result["summary"] == {"matched": 1}
    assert (
        len(
            [
                m
                for m in case.factory.artifacts.values()
                if m.artifact_type == "issued-qpf-verification"
            ]
        )
        == 2
    )


def test_completed_versions_do_not_spend_backfill_read_budget(case):
    resolver = Mock(return_value=retained(case))
    first = run(case, resolver, max_issuances=1)
    assert first["summary"] == {"matched": 2}
    original = case.issuer.read
    case.issuer.issue(saved_forecast()["forecast"], batch_run_id=uuid4(), location_index=0)
    reader = Mock(side_effect=original)
    case.issuer.read = reader
    second = run(case, resolver, max_issuances=1)
    assert second["summary"] == {"already_existing": 2, "matched": 2}
    assert reader.call_count == 1
    reader.reset_mock()
    assert run(case, resolver, max_issuances=1)["summary"] == {"already_existing": 4}
    reader.assert_not_called()


def test_bounds_limit_work_and_never_invent_incompatible_hourly_evidence(case):
    case.issuer.issue(saved_forecast()["forecast"], batch_run_id=uuid4(), location_index=0)
    resolver = Mock(return_value=retained(case))
    result = run(
        case,
        resolver,
        max_issuances=1,
        max_opportunities=1,
        end_valid_time=VALID + timedelta(hours=2),
    )
    assert result["limits"]["issuances_omitted"] == 1
    assert result["limits"]["attempts"] == 1
    # The retained fixture is the older event; never relabel it as the newer hour.
    assert result["summary"] == {"excluded": 1, "limit_reached": 3}
    assert "incompatible_interval" in result["results"][0]["reasons"]
    assert resolver.call_count == 1
    # End-exclusive bound; the later saved hour is not inspected/acquired.
    result = run(case, resolver, end_valid_time=VALID + timedelta(seconds=1))
    assert all(r["valid_time"] == VALID.isoformat() for r in result["results"])
    with pytest.raises(ValueError, match="maximum"):
        run(case, resolver, max_opportunities=0)


def test_ineligible_saved_qpf_never_downloads(case):
    saved = case.issuer.read(case.record.issued_forecast_id)
    saved["forecast"]["hours"][0]["surface"]["fields"][FIELD]["value"] = None
    case.issuer.read = Mock(return_value=saved)
    resolver = Mock(side_effect=AssertionError("missing forecast must not acquire"))
    result = run(case, resolver)
    assert result["summary"] == {"excluded": 2}
    resolver.assert_not_called()


def test_recent_hours_are_not_starved_by_old_unavailable_history(case):
    resolver = Mock(side_effect=MRMSUnavailableError("temporarily absent"))
    result = run(
        case,
        resolver,
        end_valid_time=VALID + timedelta(hours=2),
        max_opportunities=2,
    )
    assert result["summary"] == {"retryable": 2, "limit_reached": 2}
    resolver.assert_called_once_with(
        latitude=LAT, longitude=LON, product_time=VALID + timedelta(hours=1)
    )
    assert not any(
        m.artifact_type == "issued-qpf-verification" for m in case.factory.artifacts.values()
    )


@pytest.mark.parametrize(("amount", "status"), [(-1, "native_missing"), (-3, "native_no_coverage")])
def test_native_sentinels_save_distinct_evidence_and_repeat_without_download(case, amount, status):
    extraction, _ = observation(amount)
    payload = CanonicalJsonSerializer().serialize(extraction)
    prior = case.extraction
    manifest = case.service.artifacts.register_source(
        SourceRegistrationRequest(
            source_authority="synthetic-contract-fixture",
            source_locator=f"fixture://native-{amount}",
            source_revision="v1",
            artifact_type="mrms-coordinate-extraction",
            artifact_schema_version="mesoforge.mrms-coordinate-extraction.v1",
            media_type="application/json",
            expected_content_digest=Digest.of_bytes(payload),
            created_at=prior.created_at,
            availability=prior.availability,
            configuration_snapshot_id=prior.configuration_snapshot_id,
            configuration_digest=prior.configuration_digest,
            code_revision="a" * 40,
            environment_digest=Digest.of_bytes(b"test"),
        ),
        payload,
    )
    resolver = Mock(
        return_value={**retained(case), "extraction_artifact_id": str(manifest.artifact_id)}
    )
    result = run(case, resolver)
    assert result["summary"] == {status: 2}
    for row in result["results"]:
        fact = case.service.read(row["verification_id"])["result"]
        assert fact["qpf_error_mm"] is None
        assert fact["observation"]["state"] == status.removeprefix("native_")
    resolver.reset_mock()
    assert run(case, resolver)["summary"] == {"already_existing": 2}
    resolver.assert_not_called()


def test_temperature_delegation_unchanged_and_field_failures_independent(monkeypatch):
    temperature = {"status": "completed", "sentinel": [1, 2]}
    old = Mock(return_value=temperature)
    monkeypatch.setattr(forward_verification, "verify_previous", old)
    monkeypatch.setattr(
        automatic, "verify_previous_qpf", Mock(side_effect=RuntimeError("provider"))
    )
    result = forward_verification.verify_previous_fields(LAT, LON, now=CUTOFF)
    assert result["temperature"] is temperature
    old.assert_called_once_with(LAT, LON, now=CUTOFF)
    assert result["qpf"]["status"] == "error"
    old.side_effect = RuntimeError("METAR")
    qpf = Mock(return_value={"status": "nothing_to_verify"})
    monkeypatch.setattr(automatic, "verify_previous_qpf", qpf)
    result = forward_verification.verify_previous_fields(LAT, LON, now=CUTOFF)
    assert result["temperature"]["status"] == "error"
    assert result["qpf"] == {"status": "nothing_to_verify"}


def test_pinned_baseline_issues_after_verification_failure_and_continues(
    case, monkeypatch, tmp_path
):
    from mesoforge.application import baseline_snapshot, forecast_from_snapshot
    from mesoforge.forecasting.coherence import CoherenceEngine
    from mesoforge.forecasting.field_blend import FieldBlendEngine

    forbidden = Mock(side_effect=AssertionError("must not recompute baseline"))
    monkeypatch.setattr(FieldBlendEngine, "blend_field", forbidden)
    monkeypatch.setattr(CoherenceEngine, "apply_baseline", forbidden)
    forecast = saved_forecast()["forecast"]
    stamp = (VALID - timedelta(hours=3)).isoformat()
    manifest = {
        "analysis_cutoff": stamp,
        "built_at": stamp,
        "completed_at": stamp,
        "information_cutoff": {"status": "proven"},
        "prepared_snapshot": {"snapshot_id": "prepared"},
        "baseline_snapshot_id": "baseline",
        "schema_version": "mesoforge.baseline-snapshot.v1",
        "field_policies": {},
    }

    def extract(*, latitude, longitude):
        return {**deepcopy(forecast), "latitude": latitude, "longitude": longitude}

    pinned = SimpleNamespace(
        manifest=manifest,
        directory=tmp_path,
        pointer={"published_at": stamp, "manifest_sha256": "a" * 64},
        reference_view=lambda _: SimpleNamespace(forecast=extract),
    )
    loader = Mock(return_value=pinned)
    monkeypatch.setattr(baseline_snapshot, "load_baseline", loader)
    monkeypatch.setattr(forecast_from_snapshot, "build_hourly_report", lambda *a, **k: {})
    service = Mock()
    service.find_versions.return_value = []
    service.issue.return_value.model_dump.return_value = {"issued_forecast_id": "saved"}
    attempts = Mock(
        side_effect=[RuntimeError("MRMS unavailable"), {"qpf": {"status": "completed"}}]
    )
    result = forecast_from_baseline(
        tmp_path,
        [{"lat": LAT, "lon": LON}, {"lat": 44.9, "lon": -93.2}],
        reference_time=VALID,
        request_time=CUTOFF,
        issue=True,
        issuer=service,
        run_lock=nullcontext,
        verification_runner=attempts,
    )
    assert result["summary"]["issued"] == 2
    assert result["results"][0]["previous_verification"]["status"] == "error"
    assert result["results"][1]["previous_verification"]["qpf"]["status"] == "completed"
    assert service.issue.call_count == 2
    assert loader.call_count == 1
    forbidden.assert_not_called()
