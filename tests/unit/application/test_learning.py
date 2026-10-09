"""Shared artifact stages preserve raw forecasts, immutable identity and safe fallbacks."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application import artifacts as artifact_module
from mesoforge.application.governance import GovernanceService
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.learning import LearningService
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.forecast_variants import validate_variant
from mesoforge.contracts.policy_governance import (
    TEMPERATURE_CORRECTION,
    GovernanceBlockedError,
    GovernanceConflict,
    correction_scope,
)
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import QPF, RH, TEMPERATURE
from mesoforge.forecasting.field_blend import FieldBlendEngine
from tests.support.governance import append_event, fixture_evaluation
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

SCOPE = correction_scope(LAT, LON)
WRITE_CLOCK = CREATED + timedelta(minutes=2)
READ_CLOCK = DECISION + timedelta(minutes=1)


def govern(learning, payload, *, activate=True):
    """Register (and optionally activate at repository level) one correction candidate."""
    factory = learning.case.factory
    factory.governance_clock = lambda: WRITE_CLOCK
    saved = learning.service.register_policy(payload)
    governance = GovernanceService(learning.service)
    registered = governance.register(ArtifactId(saved["artifact_id"]), actor="t", reason="r")
    activated = None
    if activate:
        evaluation = fixture_evaluation(learning.service)
        activated = append_event(
            factory,
            "ACTIVATED",
            family=TEMPERATURE_CORRECTION,
            scope_key=SCOPE,
            policy=saved["artifact_id"],
            content_digest=saved["content_digest"],
            evaluation=evaluation["artifact_id"],
        )
    factory.governance_clock = lambda: READ_CLOCK
    return SimpleNamespace(
        saved=saved, registered=registered["event"], activated=activated, service=governance
    )


def resolved(learning):
    governance = GovernanceService(learning.service)
    return governance.correction_snapshot([(LAT, LON)], DECISION).scope(SCOPE)


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
    assert report["candidate_status"] == "not_generated"
    assert report["policy_generation"] == "operator_only"
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


@pytest.mark.parametrize("qualified", [False, True])
def test_operational_evidence_never_generates_persistent_policy(learning, monkeypatch, qualified):
    service, forecast, case = learning.service, learning.forecast, learning.case
    analysis = _analysis(qualified=qualified)
    analysis["evaluation"]["evidence_cutoff"] = DECISION.isoformat()
    evidence = Mock(return_value=analysis)
    monkeypatch.setattr(service, "evidence", evidence)
    monkeypatch.setattr(
        "mesoforge.application.corrections.propose_temperature_policy",
        Mock(side_effect=AssertionError("Operational forecast must not propose policy")),
    )
    monkeypatch.setattr(
        service,
        "register_policy",
        Mock(side_effect=AssertionError("Operational forecast must not register policy")),
    )
    before = canonical_json_bytes(forecast)
    corrected, report = service.local_stage(forecast)
    evidence.assert_called_once_with(LAT, LON, DECISION)
    assert canonical_json_bytes(forecast) == before
    assert corrected["hours"] == forecast["hours"]
    assert report["status"] == "no_policy"
    assert report["candidate_status"] == "not_generated"
    assert report["policy_generation"] == "operator_only"
    assert report["evidence"] == analysis["correction_readiness"]
    assert report["desk_evidence"]["evaluation"] == analysis["evaluation"]
    assert report["failures"] == [] and "candidate" not in report
    assert service.find("learning-policy", {}) == []
    assert len(case.factory.artifacts) >= 2  # Control/corrected evidence still persists.


def test_active_fixture_changes_only_qualified_temperature_bucket_and_records_coherence(learning):
    service, forecast = learning.service, learning.forecast
    governed = govern(learning, _policy())
    scope = resolved(learning)
    assert scope.active is not None and scope.shadows == ()
    before = canonical_json_bytes(forecast)
    corrected, report = service.local_stage(forecast, governance=scope)
    assert report["status"] == "applied" and report["failures"] == []
    assert report["policy_generation"] == "operator_only"
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
    # One immutable identity: the stage policy digest is the artifact content digest,
    # activation time is the governance event time, and the resolution is sealed.
    assert persisted["policy"]["digest"] == governed.saved["content_digest"]
    assert persisted["policy_activated_at"] == governed.activated.recorded_at.isoformat().replace(
        "+00:00", "Z"
    )
    resolution = persisted["governance_resolution"]
    assert resolution["status"] == "resolved_active"
    assert resolution["head_event_id"] == str(governed.activated.event_id)
    assert resolution["scope_seq"] == governed.activated.scope_seq
    assert persisted["policy_reference"]["artifact_id"] == governed.saved["artifact_id"]


def test_policy_identity_is_immutable_and_only_registered_candidates_govern(learning):
    service = learning.service
    policy = _policy()
    first = service.register_policy(policy)
    again = service.register_policy(policy)
    assert again["already_existing"] and again["artifact_id"] == first["artifact_id"]
    with pytest.raises(ValueError, match="different immutable policy"):
        service.register_policy(_policy(bias=2))
    governance = GovernanceService(service)
    learning.case.factory.governance_clock = lambda: WRITE_CLOCK
    from mesoforge.application.corrections import _digest

    for role in ("active", "shadow", "retired"):
        payload = {**_policy(role=role), "version": role}
        payload["digest"] = _digest(payload)
        legacy = service.register_policy(payload)
        with pytest.raises(GovernanceConflict) as refused:
            governance.register(ArtifactId(legacy["artifact_id"]), actor="t", reason="r")
        assert refused.value.code == "payload_not_governable_candidate"
    # The database clock must not precede the payload's declared creation time.
    learning.case.factory.governance_clock = lambda: CREATED - timedelta(seconds=1)
    learning.case.factory._governance_last = None
    with pytest.raises(GovernanceConflict) as refused:
        governance.register(ArtifactId(first["artifact_id"]), actor="t", reason="r")
    assert refused.value.code == "policy_created_after_registration_clock"
    # An automatic proposal is PROPOSED only: without registration nothing executes.
    learning.case.factory.governance_clock = lambda: READ_CLOCK
    scope = resolved(learning)
    assert scope.active is None and scope.shadows == ()
    unchanged, report = service.local_stage(learning.forecast, governance=scope)
    assert unchanged["hours"] == learning.forecast["hours"] and report["shadows"] == []
    assert report["operational_stage"]["governance_resolution"]["status"] == "resolved_none"


def test_decision_before_registration_or_activation_sees_neither(learning):
    governed = govern(learning, _policy())
    governance = GovernanceService(learning.service)
    early = CREATED + timedelta(seconds=30)
    scope = governance.correction_snapshot([(LAT, LON)], early).scope(SCOPE)
    assert scope.active is None and scope.shadows == () and scope.scope_seq == 0
    between = governed.registered["recorded_at"]
    scope = governance.correction_snapshot([(LAT, LON)], datetime.fromisoformat(between)).scope(
        SCOPE
    )
    assert scope.active is None and [row.policy_artifact_id for row in scope.shadows] == [
        governed.saved["artifact_id"]
    ]


def test_failed_binding_cannot_become_empty_or_relabel_an_issued_correction(learning):
    service, forecast = learning.service, learning.forecast
    before = len(learning.case.factory.artifacts)
    with pytest.raises(ValueError, match="incomplete"):
        service.bind({"issued_forecast_id": str(uuid4())}, {"status": "fallback"})
    assert len(learning.case.factory.artifacts) == before
    govern(learning, _policy())
    corrected, report = service.local_stage(forecast, governance=resolved(learning))
    record = learning.case.service.issuer.issue(corrected, batch_run_id=uuid4(), location_index=0)
    with pytest.raises(ValueError, match="potentially corrected"):
        service.stage_issued(record.issued_forecast_id)
    assert (
        service.read(ArtifactId(report["control_reference"]["artifact_id"]))["payload"]["overlay"][
            "predictions"
        ][0]["value"]
        == 290
    )


def test_replay_never_binds_a_changed_forecast_even_while_a_policy_is_active(learning, monkeypatch):
    service, case = learning.service, learning.case
    record = case.service.issuer.issue(learning.forecast, batch_run_id=uuid4(), location_index=0)
    govern(learning, _policy())
    replay = service.stage_issued(record.issued_forecast_id)
    stage = service.read(ArtifactId(replay["operational_reference"]["artifact_id"]))["payload"]
    assert replay["numerical_no_op"] and stage["policy"]["id"] == "no-policy"
    assert "governance_resolution" not in stage
    other = case.service.issuer.issue(learning.forecast, batch_run_id=uuid4(), location_index=1)
    original = service.local_stage

    def changed(forecast, governance=None):
        corrected, report = original(forecast, governance=governance)
        hours = deepcopy(corrected["hours"])
        hours[0]["temperature"]["value"] += 1
        return {**corrected, "hours": hours}, report

    monkeypatch.setattr(service, "local_stage", changed)
    bindings = len(
        service.find("learning-binding", {"issued_forecast_id": str(other.issued_forecast_id)})
    )
    with pytest.raises(ValueError, match="nothing was bound"):
        service.stage_issued(other.issued_forecast_id)
    assert bindings == 0
    assert not service.find(
        "learning-binding", {"issued_forecast_id": str(other.issued_forecast_id)}
    )


def test_failed_stage_storage_discards_entire_active_transformation(learning):
    service, forecast = learning.service, learning.forecast
    govern(learning, _policy())
    learning.case.objects.fail_next_put = True
    corrected, report = service.local_stage(forecast, governance=resolved(learning))
    assert report["status"] == "active_storage_failed_fallback"
    assert corrected["hours"] == forecast["hours"]
    assert corrected["local_grid_baseline"] is forecast["local_grid_baseline"]
    assert report["correction"]["changes"] == []
    assert report["operational_stage"]["overlay"]["predictions"] == []
    assert report["operational_stage"]["policy"]["id"] == "no-policy"
    assert "operational_reference" not in report
    # The recorded no-op still states which governed policy was resolved and not applied.
    assert report["operational_stage"]["governance_resolution"]["status"] == "resolved_active"
    assert report["operational_stage"]["policy_reference"] is not None


def test_active_fallback_outcome_is_pinned_with_its_resolution(learning):
    service, forecast = learning.service, learning.forecast
    govern(learning, _policy(bias=-60.0))  # +60 K leaves the scientific contract
    corrected, report = service.local_stage(forecast, governance=resolved(learning))
    assert report["status"] == "fallback" and corrected["hours"] == forecast["hours"]
    stage = report["operational_stage"]
    validate_variant(stage)
    assert stage["governance_resolution"]["status"] == "resolved_active"
    assert stage["policy_reference"] is not None


def test_registered_candidates_shadow_active_policy_never_shadows_itself(learning):
    service, forecast, factory = learning.service, learning.forecast, learning.case.factory
    active = govern(learning, _policy())
    factory.governance_clock = lambda: WRITE_CLOCK + timedelta(minutes=1)
    shadow_payload = {**_policy(bias=2.0), "version": "2"}
    from mesoforge.application.corrections import _digest

    shadow_payload["digest"] = _digest(shadow_payload)
    shadow = service.register_policy(shadow_payload)
    registration = GovernanceService(service).register(
        ArtifactId(shadow["artifact_id"]), actor="t", reason="r"
    )
    retired_payload = {**_policy(bias=3.0), "version": "3"}
    retired_payload["digest"] = _digest(retired_payload)
    retired = service.register_policy(retired_payload)
    GovernanceService(service).register(ArtifactId(retired["artifact_id"]), actor="t", reason="r")
    GovernanceService(service).retire(ArtifactId(retired["artifact_id"]), actor="t", reason="r")
    factory.governance_clock = lambda: READ_CLOCK
    scope = resolved(learning)
    assert [row.policy_artifact_id for row in scope.shadows] == [shadow["artifact_id"]]
    corrected, report = service.local_stage(forecast, governance=scope)
    assert report["status"] == "applied" and len(report["shadows"]) == 1
    assert [row["status"] for row in report["shadow_attempts"]] == ["stored"]
    stage = service.read(ArtifactId(report["shadows"][0]["artifact_id"]))["payload"]
    validate_variant(stage)
    assert stage["lifecycle_role"] == "shadow"
    assert stage["policy_activated_at"] == registration["event"]["recorded_at"].replace(
        "+00:00", "Z"
    )
    assert (
        stage["governance_resolution"]["registration_event_id"] == registration["event"]["event_id"]
    )
    assert stage["overlay"]["inherit_unchanged"] is False
    # Shadows start from the raw parent, not the operational correction.
    assert stage["overlay"]["predictions"][0]["value"] == 288.0
    assert active.saved["artifact_id"] not in [
        row["policy_artifact_id"] for row in report["shadow_attempts"]
    ]
    record = learning.case.service.issuer.issue(corrected, batch_run_id=uuid4(), location_index=0)
    binding = service.bind(record.model_dump(mode="json"), report)
    payload = service.read(ArtifactId(binding["artifact_id"]))["payload"]
    assert payload["schema_version"] == "mesoforge.learning-issuance-binding.v2"
    assert payload["shadow_attempts"][0]["status"] == "stored"


def test_candidate_caused_shadow_failure_is_recorded_not_hidden(learning):
    service, forecast = learning.service, learning.forecast
    govern(learning, _policy(bias=-60.0), activate=False)
    corrected, report = service.local_stage(forecast, governance=resolved(learning))
    assert corrected["hours"] == forecast["hours"] and report["shadows"] == []
    assert [row["status"] for row in report["shadow_attempts"]] == ["candidate_failed"]


def test_unreadable_active_payload_blocks_only_that_scope(learning, monkeypatch):
    service = learning.service
    govern(learning, _policy())
    original = service.read

    def broken(identifier):
        saved = original(identifier)
        if saved["payload"].get("schema_version") == "mesoforge.temperature-correction-policy.v1":
            raise OSError("object store unavailable")
        return saved

    monkeypatch.setattr(service, "read", broken)
    scope = resolved(learning)
    assert scope.error is not None and scope.active is None
    with pytest.raises(GovernanceBlockedError) as blocked:
        service.local_stage(learning.forecast, governance=scope)
    assert blocked.value.code == "governance_unavailable"


def test_candidate_projection_uses_background_patches_and_inherits_raw_control(
    learning, monkeypatch
):
    service, forecast = learning.service, learning.forecast
    _, report = service.local_stage(forecast)
    policy = temperature_policy()
    hour = forecast["hours"][0]
    registered_at = (CREATED + timedelta(minutes=2)).isoformat()
    overlay = {
        "analysis_cutoff": DECISION.isoformat(),
        "schema_version": "mesoforge.candidate-baseline-overlay.v1",
        "baseline_snapshot_id": "fixture-baseline",
        "policy": policy.model_dump(mode="json"),
        "affected_fields": [TEMPERATURE],
        "governance": {
            "policy_artifact_id": "art_00000000-0000-4000-8000-000000000001",
            "content_digest": policy.digest,
            "registration_event_id": "gev_00000000-0000-4000-8000-000000000002",
            "registered_at": registered_at,
        },
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
    assert stage["governance_resolution"]["registration_event_id"].startswith("gev_")
    assert stage["policy_activated_at"] == registered_at.replace("+00:00", "Z")
    assert canonical_json_digest(forecast) == before
    blend_attempts = [row for row in report["shadow_attempts"] if row["family"] == "blend_policy"]
    assert [row["status"] for row in blend_attempts] == ["stored"]
    assert (
        blend_attempts[0]["registration_event_id"] == overlay["governance"]["registration_event_id"]
    )
    bad = {**overlay, "baseline_snapshot_id": "wrong-parent"}
    reference = service._reference(service.save("learning-overlay", bad))
    service.candidate_stages(forecast, report, [reference])
    assert len(report["shadows"]) == 1
    assert report["failures"][-1]["phase"] == "candidate_projection"
    assert report["shadow_attempts"][-1]["status"] == "candidate_failed"
    future = {**overlay, "analysis_cutoff": (DECISION + timedelta(hours=1)).isoformat()}
    service.candidate_stages(
        forecast, report, [service._reference(service.save("learning-overlay", future))]
    )
    assert "follows location analysis" in report["failures"][-1]["reason"]
    ungoverned = {k: v for k, v in overlay.items() if k != "governance"}
    service.candidate_stages(
        forecast, report, [service._reference(service.save("learning-overlay", ungoverned))]
    )
    assert "governance registration" in report["failures"][-1]["reason"]


def test_background_builds_only_registered_candidates_and_reuses_overlays(learning, monkeypatch):
    service, factory = learning.service, learning.case.factory
    proposed = temperature_policy()
    unregistered = temperature_policy(
        version="3", parameters=proposed.parameters.model_copy(update={"version": "3"})
    )
    candidate = service.register_policy(proposed.model_dump(mode="json"))
    service.register_policy(unregistered.model_dump(mode="json"))
    factory.governance_clock = lambda: WRITE_CLOCK
    GovernanceService(service).register(ArtifactId(candidate["artifact_id"]), actor="t", reason="r")
    factory.governance_clock = lambda: READ_CLOCK
    candidates = GovernanceService(service).blend_candidates(DECISION)
    assert [str(row.policy_artifact_id) for row in candidates] == [candidate["artifact_id"]]
    pinned = SimpleNamespace(manifest={"baseline_snapshot_id": "fixture-baseline"})
    built = Mock(
        return_value={
            "overlay": {
                "schema_version": "mesoforge.candidate-baseline-overlay.v1",
                "baseline_snapshot_id": "fixture-baseline",
                "policy": proposed.model_dump(mode="json"),
                "domains": [],
            },
            "timings": {"total_seconds": 0.01},
        }
    )
    monkeypatch.setattr("mesoforge.application.candidate_baseline.build_candidate_overlay", built)
    result = service.background(pinned, candidates, analysis_cutoff=DECISION)
    assert result["failures"] == [] and len(result["overlays"]) == 1
    overlay = service.read(ArtifactId(result["overlays"][0]["artifact_id"]))["payload"]
    assert overlay["governance"]["registration_event_id"] == str(
        candidates[0].registration.event_id
    )
    built.assert_called_once()
    state = deepcopy((learning.case.factory.artifacts, learning.case.objects.objects))
    repeated = service.background(pinned, candidates, analysis_cutoff=DECISION)
    assert repeated["failures"] == [] and repeated["overlays"] == result["overlays"]
    built.assert_called_once()
    assert state == (learning.case.factory.artifacts, learning.case.objects.objects)
    early = service.background(pinned, candidates, analysis_cutoff=CREATED)
    assert early["overlays"] == [] and "registration" in early["failures"][0]["reason"]


def test_overlays_for_finds_background_overlays_without_building(learning, monkeypatch):
    """Hosted issuance looks up exactly what the guidance worker's background retained."""
    service, factory = learning.service, learning.case.factory
    proposed = temperature_policy()
    other = temperature_policy(
        version="3", parameters=proposed.parameters.model_copy(update={"version": "3"})
    )
    first = service.register_policy(proposed.model_dump(mode="json"))
    second = service.register_policy(other.model_dump(mode="json"))
    factory.governance_clock = lambda: WRITE_CLOCK
    governance = GovernanceService(service)
    for saved in (first, second):
        governance.register(ArtifactId(saved["artifact_id"]), actor="t", reason="r")
    factory.governance_clock = lambda: READ_CLOCK
    candidates = GovernanceService(service).blend_candidates(DECISION)
    by_id = {str(row.policy_artifact_id): row for row in candidates}
    pinned = SimpleNamespace(manifest={"baseline_snapshot_id": "fixture-baseline"})
    built = Mock(
        return_value={
            "overlay": {
                "schema_version": "mesoforge.candidate-baseline-overlay.v1",
                "baseline_snapshot_id": "fixture-baseline",
                "policy": proposed.model_dump(mode="json"),
                "domains": [],
            },
            "timings": {"total_seconds": 0.01},
        }
    )
    monkeypatch.setattr("mesoforge.application.candidate_baseline.build_candidate_overlay", built)
    retained = service.background(pinned, [by_id[first["artifact_id"]]], analysis_cutoff=DECISION)
    assert built.call_count == 1
    state = deepcopy((learning.case.factory.artifacts, learning.case.objects.objects))
    later = DECISION + timedelta(hours=1)
    found = service.overlays_for(pinned, candidates, analysis_cutoff=later)
    assert found["overlays"] == retained["overlays"]
    assert found["missing"] == [
        {"policy_artifact": second["artifact_id"], "reason": "overlay_missing"}
    ]
    assert found["failures"] == []
    assert built.call_count == 1  # lookup never builds
    assert state == (learning.case.factory.artifacts, learning.case.objects.objects)
    other_baseline = SimpleNamespace(manifest={"baseline_snapshot_id": "another-baseline"})
    assert service.overlays_for(other_baseline, candidates, analysis_cutoff=later)["overlays"] == []


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


@pytest.mark.parametrize("defect", ["pin", "checkpoint", "validation", "parent", "qpf_point"])
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
    elif defect == "qpf_point":
        final = {**corrected, "qpf_intervals": [{"value": 1, "unit": "kg/m^2"}]}
    else:
        final = {**corrected, "latitude": 30.0}
    with pytest.raises(ValueError, match="pinned|checkpoint|grid center"):
        service.ai_stage(corrected, final, desk, report)
    assert report["operational_stage"]["transformation_type"] == "deterministic_corrected"
