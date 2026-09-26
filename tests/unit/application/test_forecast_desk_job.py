"""Configured-job desk attempt: only a retained, validated AI stage can be issued.

Deterministic providers only; no network, credentials or paid inference.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from mesoforge.application.forecast_from_baseline import attempt_forecast_desk
from mesoforge.application.hourly_report import build_hourly_report
from mesoforge.application.issuance import issued_forecast_context
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.forecast_desk import DeskProviderUnavailableError
from mesoforge.forecasting.coherence import QPF, TEMPERATURE
from mesoforge.forecasting.field_edit import grid_values_digest, replay_edits
from tests.unit.application.test_corrections import DECISION
from tests.unit.application.test_forecast_desk import (
    ASSESS,
    COMPLETE,
    NO_EDIT,
    PRIORITY,
    REVIEW,
    Provider,
    edit,
)
from tests.unit.application.test_issued_qpf_verification import case as case  # noqa: F401
from tests.unit.application.test_learning import learning as learning  # noqa: F401


def _corrected(learning):
    learning.forecast["baseline_snapshot"].update(
        background_analysis_cutoff=DECISION.isoformat(),
        built_at=DECISION.isoformat(),
        completed_at=DECISION.isoformat(),
        published_at=DECISION.isoformat(),
        information_cutoff={"status": "proven"},
    )
    return learning.service.local_stage(learning.forecast)


def _provider(monkeypatch, provider):
    monkeypatch.setattr(
        "mesoforge.application.forecast_desk_provider.provider_from_environment",
        lambda: provider,
    )


def _presentable(forecast):
    return {**forecast, "data_kind": "synthetic_fixture", "notice": "fixture only"}


def _temperature(payload):
    return edit(payload, field=TEMPERATURE, operation="add", amount=0.5)


def test_accepted_edit_is_retained_as_replayable_ai_stage_and_issued(learning, monkeypatch):
    corrected, report = _corrected(learning)
    _provider(
        monkeypatch,
        Provider([ASSESS, PRIORITY, edit, COMPLETE, REVIEW]),
    )
    final = attempt_forecast_desk(corrected, report, learning.service)
    stage = final["learning_stage"]
    assert stage["transformation_type"] == "ai_adjusted"
    assert stage["parent_stage_id"] == corrected["learning_stage"]["variant_id"]
    assert stage["policy"]["model"] == "deterministic"
    assert final["deterministic_stage"]["variant_id"] == stage["parent_stage_id"]
    assert final["baseline_stage"]["variant_id"] == final["deterministic_stage"]["parent_stage_id"]
    first = final["hours"][0]["surface"]["fields"][QPF]
    assert first["value"] == pytest.approx(1.25 * 1.2)
    recipes = stage["overlay"]["desk"]["accepted_recipes"]
    replayed = replay_edits(corrected["local_grid_baseline"], recipes)
    assert grid_values_digest(replayed) == grid_values_digest(final["local_grid_baseline"])
    # The issued forecast carries a compact desk summary; the full report is in the stage.
    summary = final["ai_desk"]
    assert summary["representation"] == "issued_summary_full_report_in_ai_stage"
    assert "changes" not in summary["accepted_recipes"][0]
    assert "context" not in summary and "audit" not in summary
    checkpoint = report["ai"]["checkpoints"][1]["artifact_reference"]
    saved = learning.service.read(ArtifactId(checkpoint["artifact_id"]))["payload"]
    assert saved["recipe"] == recipes[0] and "recipes" not in saved
    assert "local_grid_baseline" not in saved
    record = learning.case.service.issuer.issue(final, batch_run_id=uuid4(), location_index=0)
    binding = learning.service.bind(record.model_dump(mode="json"), report)
    variants = learning.service.read(ArtifactId(binding["artifact_id"]))["payload"]["variants"]
    assert len(variants) == 3
    context = issued_forecast_context(final)
    assert context["ai_desk"]["authoritative_artifact"] == final["learning_reference"]
    assert context["ai_desk"]["accepted_edit_count"] == 1


def test_stage_persistence_failure_issues_complete_corrected_forecast(learning, monkeypatch):
    corrected, report = _corrected(learning)
    _provider(monkeypatch, Provider([ASSESS, PRIORITY, edit, COMPLETE, REVIEW]))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated stage storage outage")

    monkeypatch.setattr(learning.service, "ai_stage", fail)
    final = attempt_forecast_desk(corrected, report, learning.service)
    assert final["hours"] == corrected["hours"]
    assert final["local_grid_baseline"] is corrected["local_grid_baseline"]
    assert final["learning_stage"] is corrected["learning_stage"]
    desk = final["ai_desk"]
    assert desk["completion_reason"] == "stage_persistence_failed_fallback"
    assert desk["attempt_completion_reason"] == "complete"
    assert desk["accepted_recipes"] == [] and len(desk["discarded_recipes"]) == 1
    assert desk["issued_checkpoint"] == "deterministic_corrected"
    assert desk["validation"]["basis"] == "validated_corrected_parent"
    assert len(report["ai"]["discarded_checkpoints"]) == 1
    assert report["ai_storage_failure"] == "RuntimeError"
    assert final["baseline_stage"]["variant_id"] == report["control_stage"]["variant_id"]
    assert final["deterministic_reference"] == report["operational_reference"]
    context = issued_forecast_context(final)
    assert context["ai_desk"]["authoritative_artifact"] is None
    assert context["ai_desk"]["issued_checkpoint"] == "deterministic_corrected"
    hourly = build_hourly_report(_presentable(final), display_timezone="UTC")
    assert hourly["hours"][0]["ai_adjustment"]["stage_id"] is None
    assert hourly["hours"][0]["ai_adjustment"]["issued_checkpoint"] == "deterministic_corrected"


@pytest.mark.parametrize(
    "failure,reason",
    [
        (DeskProviderUnavailableError("fixture unavailable"), "provider_unavailable"),
        (TimeoutError("fixture timeout"), "provider_timeout"),
    ],
)
def test_no_provider_action_issues_corrected_stage_without_claiming_ai(
    learning, monkeypatch, failure, reason
):
    corrected, report = _corrected(learning)
    provider = Provider([failure])
    _provider(monkeypatch, provider)
    final = attempt_forecast_desk(corrected, report, learning.service)
    assert final["hours"] == corrected["hours"]
    assert final["learning_stage"]["transformation_type"] == "deterministic_corrected"
    assert final["ai_desk"]["completion_reason"] == reason
    assert final["ai_desk"]["usage"]["validated_actions"] == 0
    assert report["operational_stage"]["transformation_type"] == "deterministic_corrected"


def test_missing_lineage_spends_no_provider_call(learning, monkeypatch):
    corrected, report = _corrected(learning)
    del report["operational_reference"]
    provider = Provider([ASSESS])
    _provider(monkeypatch, provider)
    final = attempt_forecast_desk(corrected, report, learning.service)
    assert provider.requests == []
    assert final["hours"] == corrected["hours"]
    assert final["ai_desk"]["completion_reason"] == "lineage_unavailable"
    assert attempt_forecast_desk(corrected, report, None)["ai_desk"]["completion_reason"] == (
        "lineage_unavailable"
    )


def test_unexpected_controller_exception_is_isolated_to_corrected_fallback(learning, monkeypatch):
    corrected, report = _corrected(learning)

    def broken(*args, **kwargs):
        raise RuntimeError("controller defect")

    monkeypatch.setattr("mesoforge.application.forecast_desk.run_forecast_desk", broken)
    final = attempt_forecast_desk(corrected, report, learning.service)
    assert final["hours"] == corrected["hours"]
    assert final["ai_desk"]["completion_reason"] == "desk_failure"
    assert final["ai_desk"]["failure_type"] == "RuntimeError"


def test_presentation_uses_ai_final_temperature_and_keeps_raw_and_corrected(learning, monkeypatch):
    corrected, report = _corrected(learning)
    _provider(
        monkeypatch,
        # QPF stays the first task on a wet grid; no_edit advances to temperature.
        Provider(
            [ASSESS, {**PRIORITY, "fields": [TEMPERATURE]}, NO_EDIT, _temperature, COMPLETE, REVIEW]
        ),
    )
    final = attempt_forecast_desk(corrected, report, learning.service)
    assert final["learning_stage"]["transformation_type"] == "ai_adjusted"
    before = corrected["hours"][0]["temperature"]["value"]
    assert final["hours"][0]["temperature"]["value"] == pytest.approx(before + 0.5)
    hourly = build_hourly_report(_presentable(final), display_timezone="UTC")
    first = hourly["hours"][0]
    assert first["ai_adjustment"]["action"] == "adjust_temperature"
    assert first["ai_adjustment"]["applied_delta"]["value"] == pytest.approx(0.5)
    assert first["final_temperature"]["value"] == pytest.approx(before + 0.5)
    assert first["ai_adjustment"]["stage_id"] == final["learning_stage"]["variant_id"]
    assert hourly["hours"][1]["ai_adjustment"]["action"] == "no_temperature_edit"
    assert hourly["ai_desk"]["completion_reason"] == "complete"
    assert first["bias_correction"]["stage_id"] == final["deterministic_stage"]["variant_id"]


def test_qpf_only_edit_is_labeled_in_the_hourly_report(learning, monkeypatch):
    corrected, report = _corrected(learning)
    _provider(monkeypatch, Provider([ASSESS, PRIORITY, edit, COMPLETE, REVIEW]))
    final = attempt_forecast_desk(corrected, report, learning.service)
    hourly = build_hourly_report(_presentable(final), display_timezone="UTC")
    first, second = hourly["hours"][0]["ai_adjustment"], hourly["hours"][1]["ai_adjustment"]
    assert first["action"] == "adjust_other_fields" and first["edited_fields"] == [QPF]
    assert second["action"] == "no_temperature_edit" and second["edited_fields"] == []


@pytest.mark.parametrize("defect", ["missing_recipe", "reordered_input", "no_action"])
def test_ai_stage_requires_the_complete_replayable_recipe_chain(learning, defect):
    from copy import deepcopy

    from mesoforge.application.forecast_desk import run_forecast_desk

    corrected, report = _corrected(learning)
    final, desk = run_forecast_desk(
        corrected,
        provider=Provider(
            [
                ASSESS,
                PRIORITY,
                edit,
                lambda p: edit(p, operation="add", amount=0.5, cell="2:2"),
                COMPLETE,
                REVIEW,
            ]
        ),
    )
    assert desk["usage"]["accepted_edits"] == 2
    tampered = deepcopy(desk)
    if defect == "missing_recipe":
        tampered["accepted_recipes"] = tampered["accepted_recipes"][1:]
    elif defect == "reordered_input":
        tampered["accepted_recipes"][1]["input_values_sha256"] = tampered["checkpoints"][0][
            "values_digest"
        ]
    else:
        tampered["usage"]["validated_actions"] = 0
    with pytest.raises(ValueError, match="checkpoint|provider action"):
        learning.service.ai_stage(corrected, final, tampered, report)
    assert report["operational_stage"]["transformation_type"] == "deterministic_corrected"
