"""Shared artifact stages preserve raw forecasts, immutable identity and safe fallbacks."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application import artifacts as artifact_module
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.learning import LearningService
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import QPF, RH, TEMPERATURE
from mesoforge.forecasting.field_blend import FieldBlendEngine
from tests.unit.application.test_corrections import (
    CREATED,
    DECISION,
    LAT,
    LON,
    _analysis,
    _forecast,
    _policy,
)
from tests.unit.application.test_issued_qpf_verification import case as case  # noqa: F401
from tests.unit.forecasting.test_candidate_policy import temperature_policy


@pytest.fixture
def learning(case, monkeypatch):
    # ArtifactService and the dedicated in-memory transaction have controlled clocks;
    # policy availability tests must not depend on the test runner's actual date.
    registered = CREATED + timedelta(minutes=1)
    monkeypatch.setattr(artifact_module, "datetime", SimpleNamespace(now=lambda zone: registered))
    with case.factory() as uow:
        repository = type(uow.artifacts)
        original_add = repository.add_derived

    def add(self, **kwargs):
        row = original_add(self, **kwargs)
        available = max(
            [registered, kwargs["activity_completed_at"], *kwargs["parent_available_ats"]]
        )
        row = ArtifactManifest.model_validate(
            {
                **row.model_dump(),
                "registered_at": registered,
                "availability": row.availability.model_copy(update={"available_at": available}),
            }
        )
        self._store[row.artifact_id] = row
        return row

    monkeypatch.setattr(repository, "add_derived", add)
    forecast = _forecast()
    grid = forecast["local_grid_baseline"]
    for cell in grid["cells"]:
        for hour in cell["hours"]:
            end = datetime.fromisoformat(hour["valid_time"])
            hour["surface"]["fields"][QPF] = {
                "value": 1.25,
                "unit": "kg/m^2",
                "interval_start": (end - timedelta(hours=1)).isoformat(),
                "interval_end": end.isoformat(),
                "interval_closure": "left_open_right_closed",
                "temporal_semantics": "accumulation",
            }
    forecast = extract_grid_point(grid, latitude=LAT, longitude=LON, copy_grid=False)
    forecast["baseline_snapshot"] = {
        "baseline_snapshot_id": "fixture-baseline",
        "prepared_snapshot_id": "fixture-prepared",
        "forecast_analysis_cutoff": DECISION.isoformat(),
        "field_policies": {TEMPERATURE: "temperature_control_v1/1", QPF: "phase2-qpf-fallback.v1"},
        "issuance_mode": "primary",
    }
    case.service.issuer = ForecastIssuanceService(
        case.objects,
        case.factory,
        code_identity={"test": True},
        clock=lambda: DECISION + timedelta(minutes=1),
    )
    service = LearningService(case.service, clock=lambda: DECISION + timedelta(seconds=1))
    analysis = _analysis(qualified=False)
    analysis["evaluation"]["evidence_cutoff"] = DECISION.isoformat()
    monkeypatch.setattr(service, "evidence", Mock(return_value=analysis))
    return SimpleNamespace(service=service, forecast=forecast, case=case)


def test_no_policy_stage_and_binding_are_compact_idempotent_and_keep_historical_payload(learning):
    service, forecast, case = learning.service, learning.forecast, learning.case
    before = canonical_json_bytes(forecast)
    changed, report = service.local_stage(forecast)
    assert canonical_json_bytes(forecast) == before
    assert changed["hours"] == forecast["hours"]
    assert changed["local_grid_baseline"] is forecast["local_grid_baseline"]
    assert report["status"] == "no_policy"
    assert report["candidate_status"] == "insufficient_evidence"
    assert report["failures"] == []
    assert report["operational_stage"]["overlay"]["predictions"] == []
    assert report["operational_stage"]["parent_stage_id"] == report["control_stage"]["variant_id"]
    assert report["operational_stage"]["policy"]["id"] == "no-policy"
    assert (
        sum(report[key]["byte_size"] for key in ("control_reference", "operational_reference"))
        < 25000
    )
    for reference in (report["control_reference"], report["operational_reference"]):
        saved = service.read(ArtifactId(reference["artifact_id"]))
        assert "local_grid_baseline" not in saved["payload"]
    record = case.service.issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    historical = case.service.issuer.read(record.issued_forecast_id)
    replay = service.stage_issued(record.issued_forecast_id)
    assert replay["numerical_no_op"] and replay["historical_issuance_unchanged"]
    stored = deepcopy((case.factory.artifacts, case.factory.activities, case.objects.objects))
    repeated = service.stage_issued(record.issued_forecast_id)
    assert repeated["already_existing"]
    assert repeated["binding"]["artifact_id"] == replay["binding"]["artifact_id"]
    assert stored == (case.factory.artifacts, case.factory.activities, case.objects.objects)
    assert case.service.issuer.read(record.issued_forecast_id) == historical


def test_active_fixture_changes_only_qualified_temperature_bucket_and_records_coherence(learning):
    service, forecast = learning.service, learning.forecast
    saved = service.register_policy(_policy(role="active"))
    before = canonical_json_bytes(forecast)
    corrected, report = service.local_stage(forecast, policy_ids=[saved["artifact_id"]])
    assert report["status"] == "applied" and report["failures"] == []
    assert corrected["hours"][0]["temperature"]["value"] == 289
    assert corrected["hours"][6]["temperature"]["value"] == 290
    assert (
        corrected["hours"][0]["surface"]["fields"][RH]
        != forecast["hours"][0]["surface"]["fields"][RH]
    )
    assert (
        corrected["hours"][0]["surface"]["fields"][QPF]
        == forecast["hours"][0]["surface"]["fields"][QPF]
    )
    assert canonical_json_bytes(forecast) == before
    assert report["correction"]["coherence"]["relationships"] == [
        "blended_dew_point_consistency",
        "relative_humidity",
    ]
    persisted = service.read(ArtifactId(report["operational_reference"]["artifact_id"]))["payload"]
    assert persisted["overlay"]["correction"]["changes"]
    assert persisted["parent_stage_id"] == report["control_stage"]["variant_id"]


def test_policy_identity_is_immutable_and_registration_cutoff_is_enforced(learning):
    service = learning.service
    policy = _policy(role="shadow")
    first = service.register_policy(policy)
    again = service.register_policy(policy)
    assert again["already_existing"] and again["artifact_id"] == first["artifact_id"]
    other = _policy(role="shadow", bias=2)
    with pytest.raises(ValueError, match="different immutable policy"):
        service.register_policy(other)
    with pytest.raises(ValueError, match="not available"):
        service.policies([first["artifact_id"]], CREATED)
    failed_forecast = deepcopy(learning.forecast)
    failed_forecast["baseline_snapshot"]["forecast_analysis_cutoff"] = CREATED.isoformat()
    failed_forecast["target_reference_time"] = CREATED.isoformat()
    unmodified, report = service.local_stage(failed_forecast, policy_ids=[first["artifact_id"]])
    assert unmodified["hours"] == failed_forecast["hours"]
    assert any(row["phase"] == "policy_lookup" for row in report["failures"])


def test_failed_binding_cannot_become_empty_or_relabel_an_issued_correction(learning):
    service, forecast = learning.service, learning.forecast
    before = len(learning.case.factory.artifacts)
    with pytest.raises(ValueError, match="incomplete"):
        service.bind({"issued_forecast_id": str(uuid4())}, {"status": "fallback"})
    assert len(learning.case.factory.artifacts) == before
    policy = service.register_policy(_policy(role="active"))
    corrected, report = service.local_stage(
        forecast, policy_ids=[ArtifactId(policy["artifact_id"])]
    )
    record = learning.case.service.issuer.issue(corrected, batch_run_id=uuid4(), location_index=0)
    with pytest.raises(ValueError, match="potentially corrected"):
        service.stage_issued(record.issued_forecast_id)
    assert (
        service.read(ArtifactId(report["control_reference"]["artifact_id"]))["payload"]["overlay"][
            "predictions"
        ][0]["value"]
        == 290
    )


def test_failed_stage_storage_discards_entire_active_transformation(learning):
    service, forecast = learning.service, learning.forecast
    policy = service.register_policy(_policy(role="active"))
    learning.case.objects.fail_next_put = True
    corrected, report = service.local_stage(forecast, policy_ids=[policy["artifact_id"]])
    assert report["status"] == "active_storage_failed_fallback"
    assert corrected["hours"] == forecast["hours"]
    assert corrected["local_grid_baseline"] is forecast["local_grid_baseline"]
    assert report["correction"]["changes"] == []
    assert report["operational_stage"]["overlay"]["predictions"] == []
    assert report["operational_stage"]["policy"]["id"] == "no-policy"
    assert "operational_reference" not in report


def test_candidate_projection_uses_background_patches_and_inherits_raw_control(
    learning, monkeypatch
):
    service, forecast = learning.service, learning.forecast
    _, report = service.local_stage(forecast)
    policy = temperature_policy(lifecycle_role="shadow", activated_at=CREATED)
    hour = forecast["hours"][0]
    overlay = {
        "analysis_cutoff": DECISION.isoformat(),
        "schema_version": "mesoforge.candidate-baseline-overlay.v1",
        "baseline_snapshot_id": "fixture-baseline",
        "policy": policy.model_dump(mode="json"),
        "affected_fields": [TEMPERATURE],
        "domains": [
            {
                "latitude": LAT,
                "longitude": LON,
                "reference_time": DECISION.isoformat(),
                "point_target": {"x_index": 3, "y_index": 3},
                "cells": [
                    {
                        "x_index": 3,
                        "y_index": 3,
                        "hours": [
                            {
                                "horizon_hours": 1,
                                "valid_time": hour["valid_time"],
                                "fields": {TEMPERATURE: {"value": 288.0, "unit": "K"}},
                            }
                        ],
                    }
                ],
            }
        ],
    }
    saved = service.save("learning-overlay", overlay)
    monkeypatch.setattr(
        FieldBlendEngine, "blend_field", Mock(side_effect=AssertionError("location reblend"))
    )
    before = canonical_json_digest(forecast)
    service.candidate_stages(forecast, report, [service._reference(saved)])
    assert report["failures"] == []
    assert len(report["shadows"]) == 1
    stage = service.read(ArtifactId(report["shadows"][0]["artifact_id"]))["payload"]
    values = stage["overlay"]["predictions"]
    assert len(values) == 72
    assert next(row for row in values if row["field"] == TEMPERATURE)["value"] == 288
    assert [row["value"] for row in values if row["field"] == QPF] == [1.25] * 36
    assert len([row for row in values if row["field"] == TEMPERATURE and row["value"] == 290]) == 35
    assert stage["parent_stage_id"] == report["control_stage"]["variant_id"]
    assert stage["lifecycle_role"] == "shadow"
    assert canonical_json_digest(forecast) == before
    bad = {**overlay, "baseline_snapshot_id": "wrong-parent"}
    reference = service._reference(service.save("learning-overlay", bad))
    service.candidate_stages(forecast, report, [reference])
    assert len(report["shadows"]) == 1
    assert report["failures"][-1]["phase"] == "candidate_projection"
    future = {**overlay, "analysis_cutoff": (DECISION + timedelta(hours=1)).isoformat()}
    service.candidate_stages(
        forecast, report, [service._reference(service.save("learning-overlay", future))]
    )
    assert "follows location analysis" in report["failures"][-1]["reason"]


def test_background_candidate_requires_shadow_and_reuses_existing_overlay(learning, monkeypatch):
    service = learning.service
    proposed = temperature_policy()
    candidate = service.register_policy(proposed.model_dump(mode="json"))
    shadow_policy = proposed.model_copy(
        update={
            "version": "2",
            "parameters": proposed.parameters.model_copy(update={"version": "2"}),
            "lifecycle_role": "shadow",
            "activated_at": CREATED,
        }
    )
    shadow = service.register_policy(shadow_policy.model_dump(mode="json"))
    pinned = SimpleNamespace(manifest={"baseline_snapshot_id": "fixture-baseline"})
    built = Mock(
        return_value={
            "overlay": {
                "schema_version": "mesoforge.candidate-baseline-overlay.v1",
                "baseline_snapshot_id": "fixture-baseline",
                "policy": shadow_policy.model_dump(mode="json"),
                "domains": [],
            },
            "timings": {"total_seconds": 0.01},
        }
    )
    monkeypatch.setattr("mesoforge.application.candidate_baseline.build_candidate_overlay", built)
    result = service.background(
        pinned, [candidate["artifact_id"], shadow["artifact_id"]], analysis_cutoff=DECISION
    )
    assert len(result["failures"]) == 1 and "activation" in result["failures"][0]["reason"]
    assert len(result["overlays"]) == 1
    built.assert_called_once()
    state = deepcopy((learning.case.factory.artifacts, learning.case.objects.objects))
    repeated = service.background(pinned, [shadow["artifact_id"]], analysis_cutoff=DECISION)
    assert repeated["failures"] == [] and repeated["overlays"] == result["overlays"]
    built.assert_called_once()
    assert state == (learning.case.factory.artifacts, learning.case.objects.objects)


def desk_report(corrected):
    from mesoforge.forecasting.field_edit import grid_values_digest

    parent = corrected["learning_stage"]
    return {
        "policy": {"id": "forecast-desk-fixture", "version": "1"},
        "context_digest": str(canonical_json_digest({"fixture": "pinned"})),
        "provider": "deterministic-fixture",
        "model": "no-op",
        "completion_reason": "no_edit",
        "usage": {"provider_calls": 1, "validated_actions": 1},
        "accepted_recipes": [],
        "checkpoints": [{"values_digest": grid_values_digest(corrected["local_grid_baseline"])}],
        "pinned_evidence": {
            **{
                k: parent[k]
                for k in ("baseline_snapshot_id", "prepared_snapshot_id", "analysis_cutoff")
            },
            "corrected_stage_id": parent["variant_id"],
            "parent_grid_sha256": corrected["local_grid"]["sha256"],
        },
        "validation": {"status": "valid"},
    }


def test_ai_noedit_stage_retains_three_distinct_stages_and_exact_issuance(learning):
    service, original, case = learning.service, learning.forecast, learning.case
    corrected, report = service.local_stage(original)
    final = service.ai_stage(corrected, corrected, desk_report(corrected), report)
    assert final["hours"] == original["hours"]
    assert final["local_grid_baseline"] is original["local_grid_baseline"]
    ai = final["learning_stage"]
    assert ai["transformation_type"] == "ai_adjusted" and ai["lifecycle_role"] == "active"
    assert ai["parent_stage_id"] == final["deterministic_stage"]["variant_id"]
    assert final["deterministic_stage"]["parent_stage_id"] == final["baseline_stage"]["variant_id"]
    assert ai["evidence_cutoff"] == ai["analysis_cutoff"]
    assert ai["policy_created_at"] is None
    record = case.service.issuer.issue(final, batch_run_id=uuid4(), location_index=0)
    binding = service.bind(record.model_dump(mode="json"), report)
    retained = service.read(ArtifactId(binding["artifact_id"]))["payload"]
    assert len(retained["variants"]) == 3
    assert case.service.issuer.read(record.issued_forecast_id)["forecast"] == final
    saved = service.read(ArtifactId(report["operational_reference"]["artifact_id"]))
    assert saved["payload"] == ai and saved["byte_size"] < 60000
    assert service.stage_issued(record.issued_forecast_id)["already_existing"]


def test_ai_storage_failure_cannot_update_report_or_overwrite_corrected_parent(learning):
    service, forecast, case = learning.service, learning.forecast, learning.case
    corrected, report = service.local_stage(forecast)
    before = deepcopy(report)
    case.objects.fail_next_put = True
    with pytest.raises(RuntimeError, match="simulated upload failure"):
        service.ai_stage(corrected, corrected, desk_report(corrected), report)
    assert report == before
    assert corrected["hours"] == forecast["hours"]


@pytest.mark.parametrize("defect", ["pin", "checkpoint", "validation", "parent"])
def test_ai_stage_rejects_unproven_or_substituted_controller_evidence(learning, defect):
    service, forecast = learning.service, learning.forecast
    corrected, report = service.local_stage(forecast)
    desk = desk_report(corrected)
    final = corrected
    if defect == "pin":
        desk["pinned_evidence"]["prepared_snapshot_id"] = "later-provider-arrival"
    elif defect == "checkpoint":
        desk["checkpoints"][-1]["values_digest"] = str(canonical_json_digest({"wrong": True}))
    elif defect == "validation":
        desk["validation"]["status"] = "invalid"
    else:
        final = {**corrected, "latitude": 30.0}
    with pytest.raises(ValueError, match="pinned|checkpoint"):
        service.ai_stage(corrected, final, desk, report)
    assert report["operational_stage"]["transformation_type"] == "deterministic_corrected"
