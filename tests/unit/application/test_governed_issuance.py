"""Configured issuance fails safe on unproven governance; ACTIVE blends reach the builder."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application import baseline_snapshot as baselines
from mesoforge.application import build_baseline as background
from mesoforge.application import forecast_from_baseline as location_job
from mesoforge.application.candidate_baseline import build_candidate_overlay
from mesoforge.application.forecast_from_baseline import forecast_from_baseline
from mesoforge.application.governance import GovernanceService
from mesoforge.application.issuance import ForecastIssuanceService, refuse_ungoverned_issuance
from mesoforge.application.learning import LearningService
from mesoforge.application.prepared_snapshot import SnapshotError
from mesoforge.catalog.configuration import FallbackWeightTable
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.policy_governance import (
    BLEND_POLICY,
    TEMPERATURE_CORRECTION,
    GovernanceBlockedError,
    blend_scope,
    correction_scope,
)
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.coherence import WIND
from tests.support.governance import append_event, fixture_evaluation
from tests.unit.application.test_baseline_snapshot import LOCATIONS, TARGET
from tests.unit.application.test_baseline_snapshot import (
    baseline_case as baseline_case,  # noqa: F401
)
from tests.unit.application.test_batch_forecast import FIRST
from tests.unit.application.test_corrections import _policy as correction_policy
from tests.unit.application.test_issued_qpf_verification import case as case  # noqa: F401
from tests.unit.application.test_learning import learning as learning  # noqa: F401
from tests.unit.forecasting.test_candidate_policy import temperature_policy
from tests.unit.forecasting.test_surface import configuration as configuration  # noqa: F401


def _issuer(case) -> ForecastIssuanceService:
    return ForecastIssuanceService(case.objects, case.factory, code_identity={"test": True})


@pytest.fixture
def store(case):
    """Real clocks: the baseline fixture and the forecast job run at the actual time."""
    case.service.issuer = _issuer(case)
    return SimpleNamespace(service=LearningService(case.service), case=case)


def test_unreadable_governance_issues_nothing_and_never_reaches_the_desk(
    baseline_case, monkeypatch
):
    monkeypatch.delenv("MESOFORGE_DATABASE_DSN", raising=False)
    desk = Mock(side_effect=AssertionError("desk attempted without governance"))
    monkeypatch.setattr(location_job, "attempt_forecast_desk", desk)
    issuer = Mock()
    result = forecast_from_baseline(
        baseline_case["baseline"],
        LOCATIONS,
        reference_time=TARGET,
        issue=True,
        issuer=issuer,
        verify_prior=False,
    )
    assert result["status"] == "governance_unavailable"
    assert result["summary"]["issued"] == 0
    assert [row["status"] for row in result["results"]] == ["not_run", "not_run"]
    assert not issuer.mock_calls
    desk.assert_not_called()
    preview = forecast_from_baseline(baseline_case["baseline"], LOCATIONS, reference_time=TARGET)
    assert preview["status"] == "ok"
    assert preview["governance"]["status"] == "governance_unavailable"


def test_pre_governance_baseline_is_refused_for_issuance(baseline_case, store):
    issuer = Mock()
    result = forecast_from_baseline(
        baseline_case["baseline"],
        LOCATIONS,
        reference_time=TARGET,
        issue=True,
        issuer=issuer,
        verify_prior=False,
        learning_service=store.service,
    )
    assert result["status"] == "baseline_governance_unproven"
    assert not issuer.mock_calls


def test_governed_build_is_equivalent_without_active_blends_and_issues(
    baseline_case, store, tmp_path
):
    governance = GovernanceService(store.service)
    root = tmp_path / "baseline"
    built = background.build_baseline(
        baseline_case["guidance"],
        root,
        [FIRST],
        reference_times=[TARGET],
        governance=governance,
    )
    manifest = built["manifest"]
    assert manifest["blend_governance"]["status"] == "resolved"
    assert manifest["blend_governance"]["heads"] == {}
    assert manifest["field_policies"] == baseline_case["result"]["manifest"]["field_policies"]
    original = baseline_case["result"]["manifest"]["domains"][0]
    assert manifest["domains"][0]["grid_sha256"] == original["grid_sha256"]
    assert "application/governance.py" in manifest["producer_source_sha256"]
    result = forecast_from_baseline(
        root,
        [FIRST],
        reference_time=TARGET,
        issue=True,
        issuer=_issuer(store.case),
        run_lock=nullcontext,
        verify_prior=False,
        learning_service=store.service,
        governance=governance,
    )
    assert result["status"] == "ok" and result["summary"]["issued"] == 1, result
    stage = result["results"][0]["forecast"]["deterministic_stage"]
    assert stage["governance_resolution"]["status"] == "resolved_none"
    assert "governance_resolution_seconds" in result["timings"]
    # A development build without governance can never replace the governed pointer.
    with pytest.raises(SnapshotError, match="blend governance"):
        background.build_baseline(
            baseline_case["guidance"], root, [FIRST], reference_times=[TARGET]
        )


def _governed_qpf(store, configuration):
    data = configuration.scalar_vector_table.model_dump(mode="json")
    data["table_id"] = "fixture-governed-wind.v1"
    for row in data["rows"]:
        if row["available_models"] == ["HRRR", "GFS"]:
            row["weights"] = [0.5, 0.0, 0.5]
    table = FallbackWeightTable.model_validate_json(json.dumps(data))
    candidate = temperature_policy(
        policy_id=table.table_id,
        field=WIND,
        parent_policy=configuration.scalar_vector_table.table_id,
        parameters=table,
        missing_behavior="approved_subset_row",
    )
    service, factory = store.service, store.case.factory
    saved = service.register_policy(candidate.model_dump(mode="json"))
    GovernanceService(service).register(ArtifactId(saved["artifact_id"]), actor="t", reason="r")
    activated = append_event(
        factory,
        "ACTIVATED",
        family=BLEND_POLICY,
        scope_key=blend_scope(WIND),
        policy=saved["artifact_id"],
        content_digest=saved["content_digest"],
        evaluation=fixture_evaluation(service)["artifact_id"],
    )
    return saved, activated, table


def test_active_governed_blend_executes_through_the_builder_and_rollback_revokes_it(
    baseline_case, store, configuration, tmp_path
):
    saved, activated, table = _governed_qpf(store, configuration)
    governance = GovernanceService(store.service)
    root = tmp_path / "baseline"
    built = background.build_baseline(
        baseline_case["guidance"],
        root,
        [FIRST],
        reference_times=[TARGET],
        governance=governance,
    )
    manifest = built["manifest"]
    pinned = manifest["blend_governance"]
    assert pinned["policies"][WIND]["policy_artifact_id"] == saved["artifact_id"]
    assert pinned["policies"][WIND]["policy"]["parameters"]["table_id"] == table.table_id
    assert manifest["field_policies"][WIND]["status"] == "governed_active"
    assert manifest["field_policies"][WIND]["head_event_id"] == str(activated.event_id)
    view = baselines.load_baseline(root).reference_view(TARGET)
    forecast = view.forecast(latitude=FIRST["lat"], longitude=FIRST["lon"])
    fields = [hour["surface"]["fields"] for hour in forecast["hours"]]
    assert {row["eastward_wind_10m"]["policy"] for row in fields} == {table.table_id}
    # Dew point shares the default table and is not governed: it stays unchanged.
    assert table.table_id not in {row["dew_point_temperature_2m"]["policy"] for row in fields}
    before = canonical_json_bytes(forecast["hours"])
    # Candidate overlays compose the parent's governed overrides: the parent identity
    # is the governed table, never the Phase 2 default it replaced.
    pinned_baseline = baselines.load_baseline(root)
    candidate_table = table.model_copy(update={"table_id": "fixture-candidate-wind.v2"})
    default_parent = temperature_policy(
        policy_id=candidate_table.table_id,
        version="2",
        field=WIND,
        parent_policy=configuration.scalar_vector_table.table_id,
        parameters=candidate_table,
        missing_behavior="approved_subset_row",
    )
    with pytest.raises(ValueError, match="parent policy"):
        build_candidate_overlay(pinned_baseline, default_parent, analysis_cutoff=datetime.now(UTC))
    governed_parent = default_parent.model_copy(update={"parent_policy": table.table_id})
    overlay = build_candidate_overlay(
        pinned_baseline, governed_parent, analysis_cutoff=datetime.now(UTC)
    )
    assert overlay["overlay"]["policy"]["parent_policy"] == table.table_id
    append_event(
        store.case.factory,
        "ROLLED_BACK",
        family=BLEND_POLICY,
        scope_key=blend_scope(WIND),
    )
    result = forecast_from_baseline(
        root,
        [FIRST],
        reference_time=TARGET,
        issue=True,
        issuer=Mock(),
        verify_prior=False,
        learning_service=store.service,
        governance=governance,
    )
    assert result["status"] == "baseline_governance_revoked"
    # History is pinned: the older baseline's saved forecast is unchanged.
    again = baselines.load_baseline(root).reference_view(TARGET)
    assert (
        canonical_json_bytes(again.forecast(latitude=FIRST["lat"], longitude=FIRST["lon"])["hours"])
        == before
    )


def test_rollback_recorded_in_flight_blocks_that_location(
    baseline_case, store, tmp_path, monkeypatch
):
    governance = GovernanceService(store.service)
    root = tmp_path / "baseline"
    background.build_baseline(
        baseline_case["guidance"], root, [FIRST], reference_times=[TARGET], governance=governance
    )
    service, factory = store.service, store.case.factory
    policy = correction_policy()
    policy = {**policy, "coordinate": {"latitude": FIRST["lat"], "longitude": FIRST["lon"]}}
    from mesoforge.application.corrections import _digest

    policy["digest"] = _digest(policy)
    # The fixture's created_at is in the future of the real clock; shift the payload.
    policy = {
        **policy,
        "evidence_cutoff": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        "created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }
    policy["digest"] = _digest(policy)
    saved = service.register_policy(policy)
    scope = correction_scope(FIRST["lat"], FIRST["lon"])
    GovernanceService(service).register(ArtifactId(saved["artifact_id"]), actor="t", reason="r")
    append_event(
        factory,
        "ACTIVATED",
        family=TEMPERATURE_CORRECTION,
        scope_key=scope,
        policy=saved["artifact_id"],
        content_digest=saved["content_digest"],
        evaluation=fixture_evaluation(service)["artifact_id"],
    )

    def desk(corrected, report, learning_service):
        append_event(factory, "ROLLED_BACK", family=TEMPERATURE_CORRECTION, scope_key=scope)
        return corrected

    monkeypatch.setattr(location_job, "attempt_forecast_desk", desk)
    issuer = _issuer(store.case)
    issued_before = len(store.case.factory.issued_forecasts)
    result = forecast_from_baseline(
        root,
        [FIRST],
        reference_time=TARGET,
        issue=True,
        issuer=issuer,
        run_lock=nullcontext,
        verify_prior=False,
        learning_service=service,
        governance=governance,
    )
    row = result["results"][0]
    assert row["status"] == "error", row
    assert row["error"]["code"] == "policy_rolled_back_before_issuance"
    assert result["summary"]["issued"] == 0
    assert len(store.case.factory.issued_forecasts) == issued_before


def test_development_issuance_is_refused_while_a_governed_policy_is_active(learning):
    factory = learning.case.factory
    saved = learning.service.register_policy(correction_policy())
    scope = correction_scope(
        saved["payload"]["coordinate"]["latitude"], saved["payload"]["coordinate"]["longitude"]
    )
    with factory() as uow:
        refuse_ungoverned_issuance(uow, 44.98861, -93.25553)  # nothing active: unchanged
    factory.governance_clock = lambda: datetime(2026, 10, 20, 2, tzinfo=UTC)
    GovernanceService(learning.service).register(
        ArtifactId(saved["artifact_id"]), actor="t", reason="r"
    )
    append_event(
        factory,
        "ACTIVATED",
        family=TEMPERATURE_CORRECTION,
        scope_key=scope,
        policy=saved["artifact_id"],
        content_digest=saved["content_digest"],
        evaluation=fixture_evaluation(learning.service)["artifact_id"],
    )
    with factory() as uow, pytest.raises(GovernanceBlockedError) as blocked:
        refuse_ungoverned_issuance(uow, 44.98861, -93.25553)
    assert blocked.value.code == "governed_policy_active_use_baseline_path"
    with factory() as uow:
        refuse_ungoverned_issuance(uow, 45.0, -93.0)  # another coordinate is unaffected
    development = {k: v for k, v in learning.forecast.items() if k != "baseline_snapshot"}
    issued_before = len(factory.issued_forecasts)
    issuer = _issuer(learning.case)
    with pytest.raises(GovernanceBlockedError) as refused:
        issuer.issue(development, batch_run_id=uuid4(), location_index=0)
    assert refused.value.code == "governed_policy_active_use_baseline_path"
    assert len(factory.issued_forecasts) == issued_before
    factory.governance_unavailable = True
    with factory() as uow, pytest.raises(GovernanceBlockedError) as unavailable:
        refuse_ungoverned_issuance(uow, 45.0, -93.0)
    assert unavailable.value.code == "governance_unavailable"


def test_build_cli_requires_governance_unless_explicit_development_flag(
    monkeypatch, tmp_path, capsys
):
    def unavailable():
        raise RuntimeError("MESOFORGE_DATABASE_DSN is not set")

    monkeypatch.setattr("mesoforge.application.governance.configured_governance", unavailable)
    built = Mock(side_effect=AssertionError("built without governance"))
    monkeypatch.setattr(background, "build_baseline", built)
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": [FIRST]}))
    arguments = ["--guidance-root", str(tmp_path / "g"), "--baseline-root", str(tmp_path / "b")]
    assert background.main([*arguments, "--config", str(config)]) == 2
    assert "previous_baseline" in capsys.readouterr().err
    built.assert_not_called()
    monkeypatch.setattr(background, "build_baseline", Mock(return_value={"status": "published"}))
    assert background.main([*arguments, "--config", str(config), "--without-governance"]) == 0
    assert background.build_baseline.call_args.kwargs["governance"] is None


def test_rollback_after_the_stage_guard_is_refused_inside_the_issuance_transaction(
    baseline_case, store, tmp_path, monkeypatch
):
    """A rollback committed after stage preparation (report build, lock wait, upload)
    still blocks that location: the issuance transaction re-checks under shared locks."""
    from mesoforge.application import forecast_from_snapshot as delivery
    from mesoforge.application.corrections import _digest

    governance = GovernanceService(store.service)
    root = tmp_path / "baseline"
    background.build_baseline(
        baseline_case["guidance"], root, [FIRST], reference_times=[TARGET], governance=governance
    )
    service, factory = store.service, store.case.factory
    policy = {
        **correction_policy(),
        "coordinate": {"latitude": FIRST["lat"], "longitude": FIRST["lon"]},
        "evidence_cutoff": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        "created_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
    }
    policy["digest"] = _digest(policy)
    saved = service.register_policy(policy)
    scope = correction_scope(FIRST["lat"], FIRST["lon"])
    GovernanceService(service).register(ArtifactId(saved["artifact_id"]), actor="t", reason="r")
    append_event(
        factory,
        "ACTIVATED",
        family=TEMPERATURE_CORRECTION,
        scope_key=scope,
        policy=saved["artifact_id"],
        content_digest=saved["content_digest"],
        evaluation=fixture_evaluation(service)["artifact_id"],
    )
    original_report = delivery.build_hourly_report

    def report_then_rollback(forecast, **kwargs):
        append_event(factory, "ROLLED_BACK", family=TEMPERATURE_CORRECTION, scope_key=scope)
        return original_report(forecast, **kwargs)

    monkeypatch.setattr(delivery, "build_hourly_report", report_then_rollback)
    issued_before = len(factory.issued_forecasts)
    result = forecast_from_baseline(
        root,
        [FIRST],
        reference_time=TARGET,
        issue=True,
        issuer=_issuer(store.case),
        run_lock=nullcontext,
        verify_prior=False,
        learning_service=service,
        governance=governance,
    )
    row = result["results"][0]
    assert row["status"] == "error" and row["error"]["code"] == "policy_rolled_back_before_issuance"
    assert len(factory.issued_forecasts) == issued_before


def test_blend_rollback_during_a_batch_blocks_every_later_issuance(
    baseline_case, store, configuration, tmp_path, monkeypatch
):
    from mesoforge.application import forecast_from_snapshot as delivery

    _governed_qpf(store, configuration)
    governance = GovernanceService(store.service)
    root = tmp_path / "baseline"
    background.build_baseline(
        baseline_case["guidance"], root, LOCATIONS, reference_times=[TARGET], governance=governance
    )
    original_report = delivery.build_hourly_report
    calls = []

    def report_then_revoke(forecast, **kwargs):
        calls.append(forecast["latitude"])
        if len(calls) == 1:
            append_event(
                store.case.factory, "ROLLED_BACK", family=BLEND_POLICY, scope_key=blend_scope(WIND)
            )
        return original_report(forecast, **kwargs)

    monkeypatch.setattr(delivery, "build_hourly_report", report_then_revoke)
    result = forecast_from_baseline(
        root,
        LOCATIONS,
        reference_time=TARGET,
        issue=True,
        issuer=_issuer(store.case),
        run_lock=nullcontext,
        verify_prior=False,
        learning_service=store.service,
        governance=governance,
    )
    assert result["summary"]["issued"] == 0
    assert [row["error"]["code"] for row in result["results"]] == [
        "policy_rolled_back_before_issuance"
    ] * len(LOCATIONS)
