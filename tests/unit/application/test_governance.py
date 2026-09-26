"""Explicit governance: deterministic eligibility, CAS activation, rollback and retirement.

Every stage, issuance and binding here is produced by the real learning/issuance code
on the in-memory store. Only the verified observations are synthetic (see
tests/support/governance.py). Race, trigger and visibility behavior is proven against
PostgreSQL in tests/integration/storage/test_governance_repository.py.
"""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application import governance as governance_module
from mesoforge.application import learning as learning_module
from mesoforge.application import prospective_cycle
from mesoforge.application.governance import (
    FINAL_REVIEW_BEHAVIOR,
    GovernanceService,
    desk_version_payload,
)
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.common.identifiers import ArtifactId, GovernanceEventId
from mesoforge.contracts.forecast_variants import instant
from mesoforge.contracts.policy_governance import (
    BLEND_POLICY,
    GovernanceConflict,
    GovernanceUnavailableError,
    blend_scope,
    request_digest,
    request_key,
)
from tests.support.governance import shift_forecast, synthetic_control
from tests.unit.application.test_corrections import CREATED, CUTOFF, DECISION, LAT, LON, _analysis
from tests.unit.application.test_corrections import _policy as correction_policy
from tests.unit.application.test_issued_qpf_verification import case as case  # noqa: F401
from tests.unit.application.test_learning import SCOPE, WRITE_CLOCK, govern
from tests.unit.application.test_learning import learning as learning  # noqa: F401
from tests.unit.forecasting.test_candidate_policy import temperature_policy

DAYS = 10
END = DECISION + timedelta(days=DAYS)
ROOT = Path(__file__).resolve().parents[3]


def _state(case):
    return deepcopy(
        (
            case.factory.artifacts,
            case.factory.activities,
            case.factory.governance_events,
            case.objects.objects,
        )
    )


def prospective(learning, monkeypatch, *, raw_bias_k=1.0, payload=None, activate=False):
    """Register one candidate, then run ten real governed decisions on ten UTC dates."""
    service, case, factory = learning.service, learning.case, learning.case.factory
    qualified, insufficient = _analysis(), learning.service.evidence.return_value
    monkeypatch.setattr(
        service,
        "evidence",
        Mock(
            side_effect=lambda lat, lon, cut: qualified if instant(cut) == CUTOFF else insufficient
        ),
    )
    governed = govern(learning, payload or correction_policy(), activate=activate)
    decisions = []
    for day in range(DAYS):
        decision = DECISION + timedelta(days=day)
        forecast = shift_forecast(learning.forecast, timedelta(days=day))
        factory.governance_clock = lambda d=decision: d + timedelta(seconds=30)
        service.clock = lambda d=decision: d + timedelta(seconds=1)
        scope = GovernanceService(service).correction_snapshot([(LAT, LON)], decision).scope(SCOPE)
        corrected, report = service.local_stage(forecast, governance=scope)
        issuer = ForecastIssuanceService(
            case.objects,
            case.factory,
            code_identity={"test": True},
            clock=lambda d=decision: d + timedelta(minutes=1),
        )
        record = issuer.issue(corrected, batch_run_id=uuid4(), location_index=0)
        service.bind(record.model_dump(mode="json"), report)
        decisions.append((forecast, record))
    factory.governance_clock = lambda: END + timedelta(minutes=5)
    service.clock = lambda: END + timedelta(minutes=5)
    governance = GovernanceService(service)
    control = synthetic_control(decisions, raw_bias_k=raw_bias_k)
    monkeypatch.setattr(governance, "_temperature_control", lambda lat, lon, cutoff: control)
    return SimpleNamespace(
        governance=governance, governed=governed, decisions=decisions, control=control
    )


def test_eligible_candidate_activates_by_exact_cas_then_rolls_back_and_retires(
    learning, monkeypatch
):
    run = prospective(learning, monkeypatch)
    policy = ArtifactId(run.governed.saved["artifact_id"])
    governance = run.governance
    before = _state(learning.case)
    preview = governance.evaluate(policy, information_cutoff=END)
    assert _state(learning.case) == before and preview["writes"] == 0
    evaluation = preview["evaluation"]
    assert evaluation["decision"] == "eligible" and evaluation["reasons"] == []
    cohort = evaluation["cohort"]
    assert cohort["common_samples"] == cohort["total_samples"] == DAYS * 36
    assert cohort["composition"]["distinct_utc_decision_dates"] == DAYS
    assert cohort["composition"]["independence_claim"] is False
    assert cohort["composition"]["wet_non_wet"]["status"] == "not_available"
    assert evaluation["eligibility"]["evidence_qualification"]["1-6"]["status"] == (
        "evidence_policy_met"
    )
    assert "not a significance claim" in evaluation["eligibility"]["improvement"]["1-6"]["label"]
    assert evaluation["reproduction"]["status"] == "reproduced"
    assert evaluation["execution_attempts"]["stored"] == DAYS
    # Deterministic and clock-free: a later wall clock reproduces identical content.
    learning.service.clock = lambda: END + timedelta(days=3)
    assert governance.evaluate(policy, information_cutoff=END)["evaluation"] == evaluation
    assert json.dumps(evaluation).count('"rows"') == 0
    assert len(json.dumps(evaluation)) < 64000
    recorded = governance.evaluate(
        policy, information_cutoff=END, record=True, actor="owner", reason="prospective review"
    )
    evaluation_id = ArtifactId(recorded["recorded"]["evaluation_reference"]["artifact_id"])
    # Eligibility never activates: the scope still resolves no ACTIVE policy.
    assert governance.resolve("temperature_correction", SCOPE, END)["status"] == "resolved_none"
    with pytest.raises(GovernanceConflict) as stale:
        governance.activate(
            policy,
            evaluation_id,
            expected_head=GovernanceEventId.generate(),
            actor="owner",
            reason="activate",
        )
    assert stale.value.code == "active_changed"
    activated = governance.activate(
        policy, evaluation_id, expected_head=None, actor="owner", reason="activate"
    )
    head = GovernanceEventId(activated["event"]["event_id"])
    assert activated["event"]["event_type"] == "ACTIVATED" and not activated["already_existing"]
    repeat = governance.activate(
        policy, evaluation_id, expected_head=None, actor="owner", reason="activate"
    )
    assert repeat["already_existing"] and repeat["event"]["event_id"] == str(head)
    with pytest.raises(GovernanceConflict) as reused:
        governance.activate(policy, evaluation_id, expected_head=None, actor="owner", reason="x")
    assert reused.value.code == "request_key_reused"
    later = END + timedelta(minutes=10)
    learning.case.factory.governance_clock = lambda: later
    assert governance.resolve("temperature_correction", SCOPE, later)["status"] == (
        "resolved_active"
    )
    with pytest.raises(GovernanceConflict) as active_retire:
        governance.retire(policy, actor="owner", reason="retire")
    assert active_retire.value.code == "retire_active_policy"
    rolled = governance.rollback(expected_head=head, target=None, actor="owner", reason="undo")
    assert rolled["event"]["event_type"] == "ROLLED_BACK"
    with pytest.raises(GovernanceConflict) as unsafe:
        governance.emergency_rollback(
            expected_head=GovernanceEventId(rolled["event"]["event_id"]),
            actor="owner",
            reason="panic",
        )
    assert unsafe.value.code == "emergency_target_unsafe"
    # ABA: the old evaluation's head no longer matches, although "none" is active again.
    with pytest.raises(GovernanceConflict) as aba:
        governance.activate(
            policy,
            evaluation_id,
            expected_head=GovernanceEventId(rolled["event"]["event_id"]),
            actor="owner",
            reason="again",
        )
    assert aba.value.code == "active_changed"
    retired = governance.retire(policy, actor="owner", reason="retire")
    assert governance.retire(policy, actor="owner", reason="retire")["already_existing"]
    assert retired["event"]["event_type"] == "RETIRED"
    history = governance.history(policy_artifact_id=policy)
    assert [row["event_type"] for row in history["events"]] == [
        "REGISTERED",
        "ELIGIBILITY_EVALUATED",
        "ACTIVATED",
        "ROLLED_BACK",
        "RETIRED",
    ]
    assert [row["effective_until"] is None for row in history["intervals"]] == [False, True]


def test_active_policy_blocks_replacement_and_changed_evidence_blocks_activation(
    learning, monkeypatch
):
    run = prospective(learning, monkeypatch, activate=True)
    policy = ArtifactId(run.governed.saved["artifact_id"])
    evaluation = run.governance.evaluate(policy, information_cutoff=END)["evaluation"]
    # The candidate itself is ACTIVE for every decision: nothing is prospective shadow.
    assert evaluation["decision"] == "not_eligible"
    assert "active_replacement_rule_not_defined" in evaluation["reasons"]
    assert evaluation["cohort"]["exclusions_by_reason"] == {"candidate_was_operational": DAYS * 36}


def test_insufficient_or_unimproved_evidence_is_not_eligible(learning, monkeypatch):
    run = prospective(learning, monkeypatch, raw_bias_k=0.0)
    policy = ArtifactId(run.governed.saved["artifact_id"])
    evaluation = run.governance.evaluate(policy, information_cutoff=END)["evaluation"]
    assert evaluation["decision"] == "not_eligible"
    assert "insufficient_evidence:1-6" in evaluation["reasons"]
    assert "mae_and_rmse_not_both_improved:1-6" in evaluation["reasons"]
    with pytest.raises(GovernanceConflict) as refused:
        recorded = run.governance.evaluate(
            policy, information_cutoff=END, record=True, actor="owner", reason="record"
        )
        run.governance.activate(
            policy,
            ArtifactId(recorded["recorded"]["evaluation_reference"]["artifact_id"]),
            expected_head=None,
            actor="owner",
            reason="try",
        )
    assert refused.value.code == "candidate_not_eligible"


def test_changed_evidence_and_unrelated_sparse_series_are_handled_explicitly(learning, monkeypatch):
    run = prospective(learning, monkeypatch)
    policy = ArtifactId(run.governed.saved["artifact_id"])
    governance = run.governance
    recorded = governance.evaluate(
        policy, information_cutoff=END, record=True, actor="owner", reason="record"
    )
    evaluation_id = ArtifactId(recorded["recorded"]["evaluation_reference"]["artifact_id"])
    baseline = recorded["evaluation"]["cohort"]["common_samples"]
    # A sparse unrelated series (one extra stage) is an ancestor only; no shrinkage.
    original = governance._bound_stages

    def with_sparse(*args, **kwargs):
        stages, attempts, window = original(*args, **kwargs)
        extra = {**stages[0], "variant_id": "sha256:" + "f" * 64, "lifecycle_role": "shadow"}
        return [*stages, extra], attempts, window

    monkeypatch.setattr(governance, "_bound_stages", with_sparse)
    again = governance.evaluate(policy, information_cutoff=END)["evaluation"]
    assert again["cohort"]["common_samples"] == baseline
    monkeypatch.setattr(governance, "_bound_stages", original)
    changed = synthetic_control(run.decisions[1:], raw_bias_k=1.0)
    monkeypatch.setattr(governance, "_temperature_control", lambda lat, lon, cutoff: changed)
    with pytest.raises(GovernanceConflict) as drift:
        governance.activate(
            policy, evaluation_id, expected_head=None, actor="owner", reason="activate"
        )
    assert drift.value.code == "evidence_identity_changed"
    assert json.loads(str(drift.value).split(": ", 1)[1])["removed"]


def test_reproduction_detects_changed_evidence_and_unregistered_candidates(learning, monkeypatch):
    run = prospective(learning, monkeypatch)
    policy = ArtifactId(run.governed.saved["artifact_id"])
    different = _analysis(bias=2.0)
    monkeypatch.setattr(learning.service, "evidence", Mock(return_value=different))
    evaluation = run.governance.evaluate(policy, information_cutoff=END)["evaluation"]
    assert evaluation["reproduction"]["status"] == "changed"
    assert "evidence_not_reproducible" in evaluation["reasons"]
    unregistered = run.governance.evaluate(
        policy, information_cutoff=CREATED + timedelta(seconds=90)
    )
    assert unregistered["evaluation"]["reasons"] == [
        "candidate_not_registered_at_information_cutoff"
    ]


def test_blend_and_ai_families_are_never_eligible_and_ai_cannot_activate(learning, monkeypatch):
    service, factory = learning.service, learning.case.factory
    governance = GovernanceService(service)
    monkeypatch.setattr(governance, "_temperature_control", lambda *args: {"samples": []})
    monkeypatch.setattr(
        governance, "_qpf_control", lambda *args: {"canonicalization": {"samples": []}}
    )
    factory.governance_clock = lambda: WRITE_CLOCK
    blend = service.register_policy(temperature_policy().model_dump(mode="json"))
    governance.register(ArtifactId(blend["artifact_id"]), actor="owner", reason="shadow")
    desk = governance.register_desk_version(actor="owner", reason="record desk version")
    assert governance.register_desk_version(actor="owner", reason="record desk version")[
        "already_existing"
    ]
    factory.governance_clock = lambda: DECISION + timedelta(minutes=5)
    result = governance.evaluate(ArtifactId(blend["artifact_id"]), information_cutoff=DECISION)
    assert result["evaluation"]["decision"] == "not_eligible"
    assert result["evaluation"]["reasons"] == [
        "evaluation_locations_required",
        "promotion_rule_not_defined",
        "temperature_recipe_activation_unsupported",
    ]
    desk_id = ArtifactId(desk["event"]["policy_artifact_id"])
    ai = governance.evaluate(desk_id, information_cutoff=DECISION, locations=[(LAT, LON)])
    assert ai["evaluation"]["reasons"] == ["no_approved_ai_promotion_rule"]
    with pytest.raises(GovernanceConflict) as refused:
        governance.activate(
            desk_id, desk_id, expected_head=None, actor="owner", reason="never allowed"
        )
    assert refused.value.code == "ai_desk_policy_not_activatable"
    payload = service.read(desk_id)["payload"]
    assert payload == desk_version_payload()
    assert payload["final_review_behavior"] == FINAL_REVIEW_BEHAVIOR
    assert governance.status(BLEND_POLICY)["scopes"][0]["scope_key"] == blend_scope(
        "air_temperature_2m"
    )


def test_governance_outage_and_future_decision_times_are_unavailable(learning):
    governance = GovernanceService(learning.service)
    learning.case.factory.governance_clock = lambda: DECISION
    with pytest.raises(GovernanceUnavailableError) as future:
        governance.correction_snapshot([(LAT, LON)], DECISION + timedelta(seconds=1))
    assert future.value.code == "decision_time_in_future"
    learning.case.factory.governance_unavailable = True
    with pytest.raises(GovernanceUnavailableError) as busy:
        governance.correction_snapshot([(LAT, LON)], DECISION)
    assert busy.value.code == "governance_busy"


def test_uncommitted_governance_writes_are_never_visible(learning):
    factory = learning.case.factory
    saved = learning.service.register_policy(correction_policy())
    factory.governance_clock = lambda: WRITE_CLOCK
    key = request_key(test="uncommitted")
    event = GovernanceService(learning.service)._event(
        family="temperature_correction",
        scope_key=SCOPE,
        seq=1,
        event_type="REGISTERED",
        key=key,
        digest=request_digest(key, actor="t", reason="r"),
        actor="t",
        reason="r",
        policy=ArtifactId(saved["artifact_id"]),
        content_digest=saved["content_digest"],
    )
    with pytest.raises(RuntimeError), factory() as uow:
        uow.governance.append(event)
        raise RuntimeError("writer crashed before commit")
    assert factory.governance_events == []


def test_cli_is_json_only_read_only_by_default_and_requires_exact_state_change_ids(
    learning, monkeypatch, capsys
):
    governance = GovernanceService(learning.service)
    monkeypatch.setattr(governance_module, "configured_governance", lambda: governance)
    learning.case.factory.governance_clock = lambda: DECISION
    before = _state(learning.case)
    assert governance_module.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["writes"] == 0
    at = DECISION.isoformat()
    assert (
        governance_module.main(
            ["resolve", "--family", "temperature_correction", "--lat", str(LAT), "--lon", str(LON)]
            + ["--at", at]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "resolved_none"
    assert _state(learning.case) == before
    with pytest.raises(SystemExit):
        governance_module.main(["activate", "--policy-artifact-id", "art_x"])
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_arguments"
    code = governance_module.main(
        [
            "rollback",
            "--expected-head",
            str(GovernanceEventId.generate()),
            "--target",
            "none",
            "--actor",
            "owner",
            "--reason",
            "missing head",
        ]
    )
    error = json.loads(capsys.readouterr().err)["error"]
    assert code == 2 and error["code"] == "unknown_expected_head"


def test_superseded_selector_is_removed_and_desk_has_no_governance_path():
    assert not hasattr(learning_module, "load_learning_config")
    with pytest.raises(SystemExit):
        learning_module.main(
            ["stage-issued", "--issued-forecast-id", str(uuid4()), "--learning-policies", "x"]
        )
    with pytest.raises(SystemExit):
        prospective_cycle.main(["--learning-policies", "x"])
    for name in (
        "forecast_desk.py",
        "forecast_desk_context.py",
        "forecast_desk_provider.py",
    ):
        tree = ast.parse((ROOT / "src/mesoforge/application" / name).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "mesoforge.application.governance" not in imported
        assert "mesoforge.contracts.policy_governance" not in imported


def test_historical_desk_versions_stay_evaluable_but_only_current_code_registers():
    from mesoforge.application.governance import (
        policy_scope,
        validate_desk_version,
        validate_desk_version_record,
    )

    older = {**desk_version_payload(), "version": "0", "identity_digest": "sha256:" + "0" * 64}
    validate_desk_version_record(older)
    assert policy_scope(older)[0] == "ai_desk_policy"
    with pytest.raises(ValueError, match="current code-versioned"):
        validate_desk_version(older)
    with pytest.raises(ValueError):
        validate_desk_version_record({**older, "final_review_behavior": ""})


def test_ai_window_is_explicit_bounded_and_only_for_the_ai_family(learning, monkeypatch):
    service, factory = learning.service, learning.case.factory
    governance = GovernanceService(service)
    monkeypatch.setattr(governance, "_temperature_control", lambda *args: {"samples": []})

    def truncated(*args):
        raise ValueError("Too many QPF facts; narrow the window or raise --limit")

    monkeypatch.setattr(governance, "_qpf_control", truncated)
    factory.governance_clock = lambda: WRITE_CLOCK
    desk = governance.register_desk_version(actor="owner", reason="record")
    policy = ArtifactId(desk["event"]["policy_artifact_id"])
    factory.governance_clock = lambda: DECISION + timedelta(minutes=5)
    result = governance.evaluate(
        policy,
        information_cutoff=DECISION,
        locations=[(LAT, LON)],
        window_start=CREATED,
    )["evaluation"]
    assert result["window"]["start"].startswith(CREATED.date().isoformat())
    assert result["reasons"] == ["evidence_window_truncated", "no_approved_ai_promotion_rule"]
    field = result["cohort"]["by_location"][0]["fields"][
        "liquid_equivalent_precipitation_amount_1h"
    ]
    assert field["reason"] == "evidence_window_truncated"
    assert "issuances_before_window" in result["cohort"]["by_location"][0]["composition"]
    saved = service.register_policy(correction_policy())
    governance.register(ArtifactId(saved["artifact_id"]), actor="owner", reason="shadow")
    with pytest.raises(GovernanceConflict) as refused:
        governance.evaluate(
            ArtifactId(saved["artifact_id"]), information_cutoff=DECISION, window_start=CREATED
        )
    assert refused.value.code == "window_start_ai_family_only"


def test_cli_argument_errors_use_the_json_error_envelope(capsys):
    with pytest.raises(SystemExit) as exited:
        governance_module.main(
            ["rollback", "--expected-head", "gev_not-a-uuid", "--target", "none"]
        )
    assert exited.value.code == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "invalid_arguments"
