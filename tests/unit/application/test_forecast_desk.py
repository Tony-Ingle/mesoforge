"""Finite operational desk behavior: real scientific edits, deterministic fake inference."""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from mesoforge.application.forecast_desk import run_forecast_desk
from mesoforge.application.forecast_desk_context import build_context, inspect_evidence, task_queue
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.contracts.forecast_desk import DeskConfig, DeskProviderUnavailableError, DeskResponse
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import QPF, TEMPERATURE
from mesoforge.forecasting.field_edit import grid_values_digest, replay_edits
from tests.unit.application.test_corrections import DECISION, _forecast


def forecast():
    result = _forecast()
    grid = result["local_grid_baseline"]
    for cell in grid["cells"]:
        for hour in cell["hours"]:
            end = DECISION + timedelta(hours=hour["horizon_hours"])
            hour["surface"]["fields"][QPF] = {
                "value": 2.0,
                "unit": "kg/m^2",
                "status": "available",
                "missing_reasons": [],
                "interval_start": (end - timedelta(hours=1)).isoformat(),
                "interval_end": end.isoformat(),
                "interval_closure": "left_open_right_closed",
                "temporal_semantics": "accumulation",
                "policy": "phase2-qpf-fallback.v1",
            }
    result = extract_grid_point(
        grid, latitude=result["latitude"], longitude=result["longitude"], copy_grid=False
    )
    result["baseline_snapshot"] = {
        "baseline_snapshot_id": "fixture-baseline",
        "prepared_snapshot_id": "fixture-prepared",
        "forecast_analysis_cutoff": DECISION.isoformat(),
        "background_analysis_cutoff": DECISION.isoformat(),
        "built_at": DECISION.isoformat(),
        "completed_at": DECISION.isoformat(),
        "published_at": DECISION.isoformat(),
        "information_cutoff": {"status": "proven"},
    }
    result["learning_stage"] = {
        "variant_id": "sha256:" + "c" * 64,
        "transformation_type": "deterministic_corrected",
        "baseline_snapshot_id": "fixture-baseline",
        "analysis_cutoff": DECISION.isoformat(),
    }
    return result


class Provider:
    provider_name, model_name = "fixture", "deterministic"

    def __init__(self, actions):
        self.actions = iter(actions)
        self.requests = []

    def request(self, payload, **kwargs):
        self.requests.append(deepcopy(payload))
        action = next(self.actions)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action = action(payload)
        return DeskResponse(action, input_tokens=10, output_tokens=10)


ASSESS = {"type": "assessment", "summary": "Review pinned amount and contributor evidence."}
PRIORITY = {"type": "task_priority", "fields": [QPF, TEMPERATURE], "rationale": "Rain evidence."}
NO_EDIT = {"type": "no_edit", "rationale": "No material edit justified."}
COMPLETE = {"type": "complete", "rationale": "Review complete."}
REVIEW = {"type": "final_review", "accepted": True, "rationale": "Deterministic checks passed."}
# Desk policy v1 (instructions, tool version, edit permissions). Changing any of them
# requires a new policy version, not an edit of this constant.
PINNED_DESK_POLICY_V1_DIGEST = (
    "sha256:97dded4bfe0a772d0e53ba12d9da87e00b7e79bb4910276bb2adb960c4d970b7"
)


def edit(payload, *, field=QPF, operation="scale", amount=1.2, cell="3:3"):
    return {
        "type": "edit_proposal",
        "proposal": {
            "field": field,
            "operation": operation,
            "valid_times": [payload["context"]["valid_times"][0]],
            "cell_ids": [cell],
            "parameters": {"factor" if operation == "scale" else "delta": amount},
            "taper": None,
            "rationale": "Fixture edit only, no real skill assertion.",
            "evidence_refs": [payload["context"]["context_digest"]],
        },
    }


def test_noedit_returns_exact_parent_with_bounded_context_and_pinned_lineage():
    parent = forecast()
    before = canonical_json_digest(parent)
    final, report = run_forecast_desk(parent, provider=Provider([NO_EDIT]))
    assert final is parent and canonical_json_digest(final) == before
    assert report["completion_reason"] == "no_edit"
    assert report["validation"]["status"] == "valid"
    assert report["pinned_evidence"]["baseline_snapshot_id"] == "fixture-baseline"
    assert report["usage"]["provider_calls"] == 1
    assert len(canonical_json_bytes(report["context"])) <= DeskConfig().max_context_bytes
    assert report["context"]["verification"]["status"] == "unavailable"


def test_context_priorities_and_bounded_native_inspection_no_future_evidence():
    parent = forecast()
    context = build_context(parent)
    assert task_queue(context, 4)[0]["field"] == QPF
    assert not context["fields"]["wind_speed_10m"]["edit_contract"]["operations"]
    request = {
        "tool": "inspect_contributors",
        "field": TEMPERATURE,
        "valid_times": [context["valid_times"][0]],
        "region": "context",
        "max_rows": 2,
    }
    result = inspect_evidence(parent, context, request, max_bytes=4096)
    assert len(result["rows"]) == 2 and result["truncated"]
    assert "contributors" in result["rows"][0]
    with pytest.raises(ValueError):
        inspect_evidence(parent, context, {**request, "tool": "shell"}, max_bytes=4096)
    with pytest.raises(ValueError):
        inspect_evidence(
            parent, context, {**request, "valid_times": [DECISION.isoformat()]}, max_bytes=4096
        )
    # Unproven, later-than-cutoff or other-coordinate verification evidence is
    # excluded from the provider context; it never enters and never aborts the desk.
    location = {"latitude": parent["latitude"], "longitude": parent["longitude"]}
    proven = {
        "coordinate": location,
        "evaluation": {
            "evidence_availability": "verified_input_cutoff_and_fact_registration",
            "evidence_cutoff": DECISION.isoformat(),
        },
        "evidence_policy": {"minimum_samples": 5},
        "correction_readiness": {"status": "insufficient_evidence", "note": "secret-free"},
    }
    available = build_context(parent, evidence=proven)["verification"]
    assert available["status"] == "available"
    assert available["evidence_cutoff"] == DECISION.isoformat()
    future = (DECISION + timedelta(seconds=1)).isoformat()
    for rejected in (
        {**proven, "evaluation": {"evidence_availability": "unproven"}},
        {**proven, "evaluation": {**proven["evaluation"], "evidence_cutoff": future}},
        {**proven, "coordinate": {**location, "latitude": location["latitude"] + 0.1}},
        {**proven, "evaluation": {}},
    ):
        verification = build_context(parent, evidence=rejected)["verification"]
        assert verification["status"] == "unavailable"
        assert "correction_readiness" not in verification
        assert "evidence_cutoff" not in verification
    later = deepcopy(parent)
    later["baseline_snapshot"]["published_at"] = (DECISION + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="cutoff"):
        build_context(later)
    for cell in parent["local_grid_baseline"]["cells"]:
        for hour in cell["hours"]:
            hour["surface"]["fields"][QPF]["value"] = 0
    dry = build_context(parent)
    assert not dry["precipitation_relevant"]
    assert task_queue(dry, 1)[0]["field"] == TEMPERATURE


def test_edit_checkpoint_replay_rollback_and_context_cells_unchanged():
    parent = forecast()
    before = canonical_json_digest(parent)
    saved = []

    def sink(checkpoint):
        saved.append(checkpoint)
        return {"content_digest": str(canonical_json_digest(checkpoint))}

    provider = Provider(
        [ASSESS, PRIORITY, edit, lambda p: edit(p, operation="add", amount=-9), COMPLETE, REVIEW]
    )
    final, report = run_forecast_desk(parent, provider=provider, checkpoint_sink=sink)
    assert report["completion_reason"] == "complete"
    assert len(saved) == report["usage"]["accepted_edits"] == 1
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4
    assert canonical_json_digest(parent) == before
    for a, b in zip(
        parent["local_grid_baseline"]["cells"], final["local_grid_baseline"]["cells"], strict=True
    ):
        if a["context_only"]:
            assert a == b
    replay = replay_edits(parent["local_grid_baseline"], report["accepted_recipes"])
    assert grid_values_digest(replay) == grid_values_digest(final["local_grid_baseline"])
    assert any(row.get("edit_result", {}).get("status") == "rejected" for row in report["audit"])


def test_final_review_retains_exact_accepted_edit_summary_after_task_memory_clears():
    provider = Provider(
        [
            ASSESS,
            PRIORITY,
            edit,
            NO_EDIT,
            lambda p: edit(p, field=TEMPERATURE, operation="add", amount=0.5),
            NO_EDIT,
            REVIEW,
        ]
    )
    final, report = run_forecast_desk(
        forecast(), provider=provider, config=replace(DeskConfig(), max_tasks=2)
    )
    review = provider.requests[-1]
    assert report["completion_reason"] == "complete"
    assert report["usage"]["accepted_edits"] == 2
    assert review["phase"] == "final_review"
    assert review["last_result"] is None and review["last_evidence"] is None
    assert review["latest_valid_checkpoint_digest"] == grid_values_digest(
        final["local_grid_baseline"]
    )
    for summary, recipe in zip(review["accepted_edits"], report["accepted_recipes"], strict=True):
        assert summary == {
            "recipe_digest": str(canonical_json_digest(recipe)),
            **{
                key: recipe["proposal"][key]
                for key in (
                    "field",
                    "operation",
                    "parameters",
                    "valid_times",
                    "cell_ids",
                    "taper",
                )
            },
        }
        assert "changes" not in summary and "coherence_reports" not in summary
    assert len(canonical_json_bytes(review["accepted_edits"])) < 2048


def test_inspection_memory_retains_distinct_windows_deduplicates_and_exposes_budgets():
    def inspect(index):
        return lambda payload: {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_contributors",
                "field": QPF,
                "valid_times": [payload["context"]["valid_times"][index]],
                "region": "point",
                "max_rows": 1,
            },
        }

    provider = Provider(
        [
            ASSESS,
            PRIORITY,
            inspect(0),
            inspect(1),
            inspect(0),
            inspect(2),
            inspect(3),
            inspect(4),
            COMPLETE,
            REVIEW,
        ]
    )
    config = replace(DeskConfig(), max_tool_output_bytes=4096)
    parent = forecast()
    final, report = run_forecast_desk(parent, provider=provider, config=config)
    assert final is parent and report["completion_reason"] == "no_edit"
    both = provider.requests[4]
    assert len(both["earlier_evidence"]) == 1
    assert (
        both["last_evidence"]["request"]["valid_times"]
        != (both["earlier_evidence"][0]["request"]["valid_times"])
    )
    repeated = provider.requests[5]
    assert len(repeated["earlier_evidence"]) == 1
    assert len(repeated["inspection_index"]) == 2
    assert sorted(row["request_count"] for row in repeated["inspection_index"]) == [1, 2]
    review = provider.requests[-1]
    retained = [*review["earlier_evidence"], review["last_evidence"]]
    assert len(retained) == len({row["evidence_ref"] for row in retained}) == 4
    assert len(review["inspection_index"]) == 5
    assert all(len(canonical_json_bytes(row)) <= 4096 for row in retained)
    assert all("rows" not in row for row in review["inspection_index"])
    for index, request in enumerate(provider.requests):
        budgets = request["remaining_budgets"]
        assert budgets["provider_calls_including_this_request"] == config.max_provider_calls - index
        assert budgets["max_tool_output_bytes"] == 4096
        assert budgets["recent_inspection_result_limit"] == 4
        assert budgets["analysis_seconds"] <= config.target_seconds
        assert budgets["edit_proposals"] == config.max_edit_proposals
        assert budgets["accepted_edits"] == config.max_accepted_edits
    assert repeated["remaining_budgets"]["tool_calls"] == config.max_tool_calls - 3
    assert review["remaining_budgets"]["tool_calls"] == config.max_tool_calls - 6


@pytest.mark.parametrize("budget", ["token", "cost"])
def test_provider_preflight_counts_retained_inspection_history(budget):
    class BoundedProvider(Provider):
        def estimate_input_tokens(self, payload, max_output_tokens):
            if budget == "token" and payload["earlier_evidence"]:
                return DeskConfig().max_total_tokens
            return 100

        def estimate_max_cost(self, payload, max_output_tokens):
            return 2.0 if payload["earlier_evidence"] else 0.01

    def inspect(payload):
        index = 0 if payload["last_evidence"] is None else 1
        return {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_baseline",
                "field": QPF,
                "valid_times": [payload["context"]["valid_times"][index]],
                "region": "point",
                "max_rows": 1,
            },
        }

    provider = BoundedProvider([ASSESS, PRIORITY, inspect, inspect, COMPLETE])
    parent = forecast()
    final, report = run_forecast_desk(
        parent,
        provider=provider,
        config=replace(DeskConfig(), max_cost_usd=1 if budget == "cost" else None),
    )
    assert final is parent
    assert report["completion_reason"] == (
        "token_budget" if budget == "token" else "cost_budget_or_price_unavailable"
    )
    assert len(provider.requests) == 4
    assert report["usage"]["tool_calls"] == 2


def test_inspection_memory_keeps_old_and_current_checkpoint_identities_after_edit():
    def inspect(payload):
        return {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_baseline",
                "field": QPF,
                "valid_times": [payload["context"]["valid_times"][0]],
                "region": "point",
                "max_rows": 1,
            },
        }

    parent = forecast()
    provider = Provider([ASSESS, PRIORITY, inspect, edit, inspect, COMPLETE, REVIEW])
    final, report = run_forecast_desk(parent, provider=provider)
    review = provider.requests[-1]
    old, new = review["earlier_evidence"][0], review["last_evidence"]
    assert report["completion_reason"] == "complete"
    assert old["request"] == new["request"]
    assert old["current_state_digest"] == grid_values_digest(parent["local_grid_baseline"])
    assert new["current_state_digest"] == grid_values_digest(final["local_grid_baseline"])
    assert old["current_state_digest"] != new["current_state_digest"]
    assert old["evidence_ref"] != new["evidence_ref"]
    assert old["rows"][0]["baseline"]["value"] == 2.0
    assert new["rows"][0]["baseline"]["value"] == 2.4
    assert len(review["inspection_index"]) == 2


@pytest.mark.parametrize(
    "failure,reason",
    [
        (TimeoutError("secret-must-not-be-saved"), "provider_timeout"),
        (RuntimeError("secret-must-not-be-saved"), "desk_failure"),
        (DeskProviderUnavailableError("secret-must-not-be-saved"), "provider_unavailable"),
        ({"type": "shell", "command": "secret-must-not-be-saved"}, "desk_failure"),
    ],
)
def test_failures_after_accepted_edit_preserve_latest_checkpoint(failure, reason):
    final, report = run_forecast_desk(
        forecast(), provider=Provider([ASSESS, PRIORITY, edit, failure])
    )
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4
    assert report["completion_reason"] == reason
    assert report["validation"]["status"] == "valid"
    assert b"secret-must-not-be-saved" not in canonical_json_bytes(report)


def test_duplicate_inverse_and_previously_rejected_edits_do_not_oscillate():
    def plus(p):
        return edit(p, operation="add", amount=1)

    final, report = run_forecast_desk(
        forecast(),
        provider=Provider(
            [
                ASSESS,
                PRIORITY,
                plus,
                plus,
                lambda p: edit(p, operation="add", amount=-1),
                lambda p: edit(p, cell="0:0"),
                lambda p: edit(p, cell="0:0"),
                COMPLETE,
                REVIEW,
            ]
        ),
    )
    assert report["usage"]["accepted_edits"] == 1
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 3
    assert sum(a.get("edit_result", {}).get("status") == "rejected" for a in report["audit"]) == 4


def test_temperature_edit_reruns_current_dependencies_and_point_report():
    parent = forecast()
    # QPF remains first, then temperature. NO_EDIT advances exactly one task.
    final, report = run_forecast_desk(
        parent,
        provider=Provider(
            [
                ASSESS,
                PRIORITY,
                NO_EDIT,
                lambda p: edit(p, field=TEMPERATURE, operation="add", amount=1),
                COMPLETE,
                REVIEW,
            ]
        ),
    )
    assert final["hours"][0]["temperature"]["value"] == 291
    assert report["point_values"][0]["applied_delta_k"] == 1
    assert report["accepted_recipes"][0]["coherence_reports"]


class HeavyProvider(Provider):
    """Reports billed usage large enough to exhaust the default total-token budget."""

    def request(self, payload, **kwargs):
        response = super().request(payload, **kwargs)
        return replace(response, input_tokens=DeskConfig().max_total_tokens // 2)


@pytest.mark.parametrize(
    "budget,expected,provider_type",
    [
        ({"max_provider_calls": 3}, "provider_budget", Provider),
        ({}, "token_budget", HeavyProvider),
        ({"max_cost_usd": 0.01}, "cost_budget_or_price_unavailable", Provider),
    ],
)
def test_finite_provider_token_and_cost_budgets(budget, expected, provider_type):
    final, report = run_forecast_desk(
        forecast(),
        provider=provider_type([ASSESS, PRIORITY, edit]),
        config=replace(DeskConfig(), **budget),
    )
    assert report["completion_reason"] == expected
    assert report["usage"]["provider_calls"] <= budget.get("max_provider_calls", 20)
    if expected == "token_budget":
        assert report["usage"]["input_tokens"] > DeskConfig().max_total_tokens - 100


def test_default_budgets_fit_two_worst_case_requests_and_reject_inconsistent_ones():
    config = DeskConfig()
    worst = (
        config.max_context_bytes
        + 4 * config.max_tool_output_bytes
        + 16384
        + config.max_output_tokens
    )
    assert 2 * worst <= config.max_total_tokens
    with pytest.raises(ValueError, match="two worst-case"):
        DeskConfig(max_total_tokens=100000, max_tool_output_bytes=16384)


def test_injected_deadline_reserves_finalization_without_sleep():
    seconds = [0.0]

    def slow(p):
        seconds[0] = 850
        return edit(p)

    final, report = run_forecast_desk(
        forecast(), provider=Provider([ASSESS, PRIORITY, slow]), monotonic=lambda: seconds[0]
    )
    assert report["completion_reason"] == "analysis_time_budget"
    assert report["accepted_recipes"] == []
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2


def test_storage_failure_does_not_accept_edit_or_persist_exception_secret():
    parent = forecast()

    def sink(checkpoint):
        raise ValueError("secret-storage-string")

    final, report = run_forecast_desk(
        parent, provider=Provider([ASSESS, PRIORITY, edit, COMPLETE, REVIEW]), checkpoint_sink=sink
    )
    assert final is parent and not report["accepted_recipes"]
    assert b"secret-storage-string" not in canonical_json_bytes(report)


def test_invalid_parent_never_claims_successful_validation():
    parent = forecast()
    parent["local_grid_baseline"]["cells"][0]["hours"][0]["surface"]["fields"][QPF]["value"] = -1
    final, report = run_forecast_desk(parent, provider=Provider([NO_EDIT]))
    assert report["validation"]["status"] == "invalid"
    assert report["usage"]["provider_calls"] == 0


def test_tool_and_edit_budgets_keep_only_valid_checkpoint():
    def inspect(p):
        return {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_contributors",
                "field": QPF,
                "valid_times": [p["context"]["valid_times"][0]],
                "region": "point",
                "max_rows": 1,
            },
        }

    parent = forecast()
    provider = Provider([ASSESS, PRIORITY, inspect, inspect, COMPLETE, REVIEW])
    final, report = run_forecast_desk(
        parent, provider=provider, config=replace(DeskConfig(), max_tool_calls=1)
    )
    # An exhausted inspection budget rejects the request; the analysis continues.
    assert final is parent and report["completion_reason"] == "no_edit"
    assert report["budget_limits_reached"] == ["tool_budget"]
    assert report["usage"]["tool_calls"] == 1
    assert provider.requests[4]["last_result"]["status"] == "rejected_tool_request"
    assert any("evidence" in row for row in report["audit"])
    provider = Provider(
        [
            ASSESS,
            PRIORITY,
            edit,
            lambda p: edit(p, operation="add", amount=1),
            REVIEW,
        ]
    )
    final, report = run_forecast_desk(
        parent, provider=provider, config=replace(DeskConfig(), max_accepted_edits=1)
    )
    # The global accepted-edit budget moves the desk to its final review.
    assert report["completion_reason"] == "complete"
    assert report["budget_limits_reached"] == ["accepted_edit_budget"]
    assert provider.requests[-1]["phase"] == "final_review"
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4


def test_multiple_checkpoints_and_negative_final_review_remain_explicit():
    final, report = run_forecast_desk(
        forecast(),
        provider=Provider(
            [
                ASSESS,
                PRIORITY,
                edit,
                lambda p: edit(p, operation="add", amount=1),
                COMPLETE,
                {"type": "final_review", "accepted": False, "rationale": "Residual uncertainty."},
            ]
        ),
    )
    # The desk's own negative review is binding: the corrected parent is issued and
    # both validated checkpoints remain only as discarded audit.
    assert report["completion_reason"] == "review_not_accepted"
    assert report["accepted_recipes"] == [] and len(report["discarded_recipes"]) == 2
    assert len(report["checkpoints"]) == 1 and len(report["discarded_checkpoints"]) == 2
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.0
    assert report["validation"]["basis"] == "validated_corrected_parent"


def test_blocking_provider_cannot_hold_forecast_job_past_call_deadline():
    released = threading.Event()

    def blocked(payload):
        released.wait(5)
        return NO_EDIT

    try:
        parent = forecast()
        final, report = run_forecast_desk(
            parent,
            provider=Provider([blocked]),
            config=replace(DeskConfig(), request_timeout_seconds=0.01),
        )
        assert final is parent and report["completion_reason"] == "provider_timeout"
        assert report["usage"]["provider_calls"] == 1
    finally:
        released.set()


def test_failed_provider_preserves_known_usage_and_distinguishes_unknown_usage():
    from mesoforge.contracts.forecast_desk import DeskProviderError, DeskUsage

    parent = forecast()
    for known in (True, False):
        usage = DeskUsage(110, 60, 0.005, "fixture-v1") if known else None
        final, report = run_forecast_desk(
            parent,
            provider=Provider([DeskProviderError("Incomplete structured response", usage=usage)]),
        )
        assert final is parent and report["completion_reason"] == "provider_failure"
        assert report["provider_failure_code"] == "provider_failure"
        assert report["usage"]["provider_calls"] == 1
        assert report["usage"]["unreported_provider_calls"] == (0 if known else 1)
        assert report["usage"]["input_tokens"] == (110 if known else 0)
        assert report["usage"]["output_tokens"] == (60 if known else 0)
        assert report["usage"]["cost_usd"] == (0.005 if known else None)
        assert report.get("response_models") == (["fixture-v1"] if known else None)


def test_provider_pacing_is_clock_controlled_and_cannot_extend_analysis_budget():
    clock = [0.0]

    def wait(seconds):
        clock[0] += seconds

    provider = Provider([ASSESS, PRIORITY, COMPLETE, REVIEW])
    parent = forecast()
    final, report = run_forecast_desk(
        parent,
        provider=provider,
        config=replace(DeskConfig(), min_provider_interval_seconds=2),
        monotonic=lambda: clock[0],
        wait=wait,
    )
    assert final is parent and report["completion_reason"] == "no_edit"
    assert report["timings"]["provider_pacing_seconds"] == 6
    assert report["usage"]["provider_calls"] == 4
    clock[0] = 0
    _, report = run_forecast_desk(
        parent,
        provider=Provider([ASSESS, PRIORITY, COMPLETE]),
        config=replace(
            DeskConfig(),
            min_provider_interval_seconds=2,
            target_seconds=3,
            hard_seconds=5,
            finalization_reserve_seconds=2,
            request_timeout_seconds=1,
        ),
        monotonic=lambda: clock[0],
        wait=wait,
    )
    assert report["completion_reason"] == "analysis_time_budget"
    assert report["usage"]["provider_calls"] == 2
    assert clock[0] == 2


def test_missing_corrected_parent_fails_before_provider_evidence_exposure():
    parent = forecast()
    del parent["learning_stage"]
    provider = Provider([NO_EDIT])
    final, report = run_forecast_desk(parent, provider=provider)
    assert final is parent
    assert report["completion_reason"] == "desk_failure"
    assert provider.requests == []


def test_stalled_checkpoint_storage_cannot_accept_a_late_edit():
    released = threading.Event()

    def store(checkpoint):
        released.wait(5)
        return {"artifact_id": "late-immutable-checkpoint"}

    parent = forecast()
    try:
        final, report = run_forecast_desk(
            parent,
            provider=Provider([ASSESS, PRIORITY, edit]),
            config=replace(
                DeskConfig(),
                target_seconds=1.0,
                hard_seconds=2.0,
                finalization_reserve_seconds=1.0,
                request_timeout_seconds=0.5,
            ),
            monotonic=lambda: 0.0,
            checkpoint_sink=store,
        )
        assert final is parent
        assert report["completion_reason"] == "checkpoint_storage_timeout"
        assert report["accepted_recipes"] == []
        assert len(report["checkpoints"]) == 1
    finally:
        released.set()


def test_stalled_final_extraction_returns_complete_parent(monkeypatch):
    released = threading.Event()

    def extract(*args, **kwargs):
        released.wait(5)
        return {}

    monkeypatch.setattr("mesoforge.application.forecast_desk.extract_grid_point", extract)
    parent = forecast()
    try:
        final, report = run_forecast_desk(
            parent,
            provider=Provider([ASSESS, PRIORITY, edit, COMPLETE, REVIEW]),
            config=replace(
                DeskConfig(),
                target_seconds=1.0,
                hard_seconds=2.0,
                finalization_reserve_seconds=1.0,
                request_timeout_seconds=0.5,
            ),
            monotonic=lambda: 0.0,
        )
        assert final is parent
        assert report["completion_reason"] == "final_validation_fallback"
        assert report["accepted_recipes"] == []
        assert len(report["discarded_recipes"]) == 1
        assert report["validation"]["status"] == "valid"
    finally:
        released.set()


def test_winter_inspection_projects_saved_native_derived_and_ratio_evidence_without_delivery():
    from mesoforge.application.forecast_desk_context import contributors_at, fields_at
    from mesoforge.application.ice import extract_ice_contributors
    from mesoforge.application.snowfall_amount_forecast import extract_snowfall_amount_contributors
    from mesoforge.application.snowfall_forecast import extract_snowfall_contributors
    from mesoforge.forecasting.ice import FLAT_ICE, FRZR

    parent = forecast()
    hour = parent["hours"][0]
    kwargs = {
        "latitude": parent["latitude"],
        "longitude": parent["longitude"],
        "valid_time": hour["valid_time"],
    }
    swe = extract_snowfall_contributors([], **kwargs)
    amount = extract_snowfall_amount_contributors([], swe_views=[], **kwargs)
    ice = extract_ice_contributors([], **kwargs)
    interval = hour["surface"]["fields"][QPF]
    bounds = {
        name: interval[name] for name in ("interval_start", "interval_end", "interval_closure")
    }
    swe["contributors"][2].update(value=1.0, status="available", **bounds)
    native = next(row for row in amount["native_contributors"] if row["model"] == "NBM")
    native.update(value=0.01, status="available", **bounds)
    derived = amount["derived_contributors"][0]
    derived.update(value=0.012, status="available", diagnostic_ratio=12.0, **bounds)
    amount["native_slr"][0].update(value=10.0, status="available")
    for row in ice["contributors"]:
        if row.get("quantity_kind") == ice["fields"][FRZR]["quantity_kind"]:
            row.update(value=0.2, status="available", **bounds)
    # An enormous native provenance block must never leak into provider context.
    derived["provenance"] = {"raw": "secret-native-path" * 10000}
    hour["surface"].update(snowfall_guidance=swe, snowfall_amount_guidance=amount, ice_guidance=ice)
    center = next(
        cell for cell in parent["local_grid_baseline"]["cells"] if cell["is_forecast_point"]
    )
    center["hours"][0] = hour
    context = build_context(parent)
    families = {
        "snowfall_water_equivalent_amount",
        "snowfall_amount",
        "shadow_kuchera_snowfall_amount",
        "snow_to_liquid_ratio",
        FLAT_ICE,
        FRZR,
    }
    for name in families:
        assert context["fields"][name]["evidence_only"] is True
        assert context["fields"][name]["edit_contract"]["operations"] == ()
        assert fields_at(hour)[name]["value"] is None
        request = {
            "tool": "inspect_contributors",
            "field": name,
            "valid_times": [hour["valid_time"]],
            "region": "point",
            "max_rows": 1,
        }
        viewed = inspect_evidence(parent, context, request, max_bytes=32768)
        assert viewed["rows"][0]["baseline"]["evidence_only"]
        assert "secret-native-path" not in canonical_json_bytes(viewed).decode()
    native_rows = contributors_at(hour, "snowfall_amount")
    assert any(row["value"] == 0.01 and row["method"] == "native" for row in native_rows.values())
    derived_rows = contributors_at(hour, "shadow_kuchera_snowfall_amount")
    assert next(iter(derived_rows.values()))["value"] == 0.012
    assert next(iter(derived_rows.values()))["method"] == "kuchera"
    assert next(iter(derived_rows.values()))["interval_start"] == interval["interval_start"]
    assert contributors_at(hour, FLAT_ICE).keys() != contributors_at(hour, FRZR).keys()
    assert (
        context["fields"]["snowfall_amount"]["native_evidence"][
            "cell_hours_with_available_evidence"
        ]
        == 1
    )
    assert len(canonical_json_bytes(context)) <= 65536


def test_context_categorical_availability_and_missing_evidence_are_distinct():
    parent = forecast()
    center = next(
        cell for cell in parent["local_grid_baseline"]["cells"] if cell["is_forecast_point"]
    )
    for index, (value, status) in enumerate(
        (
            ("rain", "known"),
            ("mixed", "ambiguous"),
            ("unknown", "unknown"),
            ("unavailable", "unavailable"),
        )
    ):
        center["hours"][index]["surface"]["fields"]["precipitation_type"] = {
            "value": value,
            "status": status,
            "unit": "category",
        }
    context = build_context(parent)
    ptype = context["fields"]["precipitation_type"]
    assert ptype["available_cell_hours"] == 3
    assert ptype["numerical_cell_hours"] == 0
    assert ptype["range"] is None
    assert ptype["states"]["ambiguous"] == ptype["states"]["unknown"] == 1
    assert "precipitation_type" not in context["unavailable_fields"]
    assert context["fields"]["snowfall_amount"]["available_cell_hours"] == 0
    assert context["fields"]["snowfall_amount"]["native_evidence"]["source_identities"] == []


def test_semantics_truncation_is_explicit_and_same_model_products_do_not_collapse():
    from mesoforge.application.forecast_desk_context import contributors_at, field_view

    view = field_view(
        {"event_definition": "definition " * 100, "method_metadata": {"inputs": list(range(30))}}
    )
    assert view["event_definition"]["truncated"] is True
    assert view["method_metadata"]["inputs"]["truncated"] is True
    hour = forecast()["hours"][0]
    field = {
        "value": 0.1,
        "unit": "1",
        "interval_start": "2026-01-01T00:00:00Z",
        "interval_end": "2026-01-01T01:00:00Z",
    }
    hour["surface"]["fields"]["probability_of_thunder_1h"] = field
    hour["surface"]["thunder_guidance"] = {
        "field": field,
        "contributors": [
            {**field, "source_id": "NBM_1H", "model": "NBM"},
            {
                **field,
                "source_id": "NBM_3H",
                "model": "NBM",
                "interval_start": "2025-12-31T22:00:00Z",
            },
        ],
    }
    rows = contributors_at(hour, "probability_of_thunder_1h")
    assert set(rows) == {"NBM_1H", "NBM_3H"}
    assert rows["NBM_1H"]["interval_start"] != rows["NBM_3H"]["interval_start"]


def test_probability_and_type_native_evidence_keep_definitions_separate():
    from mesoforge.application.forecast_desk_context import contributors_at

    hour = forecast()["hours"][0]
    hour["surface"]["fields"]["probability_of_precipitation_1h"] = {"value": 0.1, "unit": "1"}
    hour["surface"]["probability_guidance"] = {
        "contributors": [
            {
                "source_id": "GEFS_6H",
                "value": 0.2,
                "unit": "1",
                "threshold": {"value": 0.254, "unit": "mm", "operator": ">="},
                "interval_start": "2026-01-01T00:00:00Z",
                "interval_end": "2026-01-01T06:00:00Z",
            }
        ]
    }
    probability = contributors_at(hour, "probability_of_precipitation_1h")["GEFS_6H"]
    assert probability["threshold"]["value"] == 0.254
    assert probability["interval_end"] == "2026-01-01T06:00:00Z"
    hour["surface"]["fields"]["precipitation_type"] = {"value": "unknown"}
    hour["surface"]["precipitation_type_guidance"] = {
        "field": hour["surface"]["fields"]["precipitation_type"],
        "contributors": [
            {
                "model": "NBM",
                "encoding": "conditional_probabilities",
                "native_values": {"rain": 60, "snow": 40},
                "conditional_type_fractions": {"rain": 0.6, "snow": 0.4},
            }
        ],
    }
    native = contributors_at(hour, "precipitation_type")["NBM"]
    assert native["encoding"] == "conditional_probabilities"
    assert native["native_values"]["snow"] == 40


def test_oversized_complete_evidence_row_is_reported_without_aborting_desk():
    parent = forecast()
    point = next(c for c in parent["local_grid_baseline"]["cells"] if c["is_forecast_point"])
    point["hours"][0]["surface"]["contributors"] = {
        "HRRR": {
            "fields": {
                QPF: {
                    **point["hours"][0]["surface"]["fields"][QPF],
                    "method_metadata": {"native_definition": ["meaning " * 40] * 16},
                }
            }
        }
    }

    def inspect(payload):
        return {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_contributors",
                "field": QPF,
                "valid_times": [payload["context"]["valid_times"][0]],
                "region": "point",
                "max_rows": 1,
            },
        }

    provider = Provider([ASSESS, PRIORITY, inspect, COMPLETE, REVIEW])
    final, report = run_forecast_desk(
        parent, provider=provider, config=replace(DeskConfig(), max_tool_output_bytes=2048)
    )
    assert final is parent and report["completion_reason"] == "no_edit"
    assert report["usage"]["tool_calls"] == 1
    result = provider.requests[3]["last_evidence"]
    assert result["status"] == "output_budget_exceeded"
    assert result["truncated"] is True and result["rows"] == []
    assert result["total_matching_rows"] == 1
    assert "complete evidence row" in result["reason"]
    assert len(canonical_json_bytes(result)) <= 2048
    assert result["evidence_ref"] == str(
        canonical_json_digest({k: v for k, v in result.items() if k != "evidence_ref"})
    )


def test_tool_byte_cap_includes_evidence_reference_and_never_returns_partial_rows():
    parent = forecast()
    context = build_context(parent)
    request = {
        "tool": "inspect_contributors",
        "field": QPF,
        "valid_times": [context["valid_times"][0]],
        "region": "context",
        "max_rows": 3,
    }
    full = inspect_evidence(parent, context, request, max_bytes=32768)
    full_size = len(canonical_json_bytes(full))
    exact = inspect_evidence(parent, context, request, max_bytes=full_size)
    assert exact["rows"] == full["rows"]
    assert len(canonical_json_bytes(exact)) <= full_size
    smaller_cap = len(canonical_json_bytes(exact)) - 1
    limited = inspect_evidence(parent, context, request, max_bytes=smaller_cap)
    assert len(canonical_json_bytes(limited)) <= smaller_cap
    assert limited["truncated"] is True
    assert limited["rows"] == full["rows"][:-1]
    with pytest.raises(ValueError, match="metadata exceeds"):
        inspect_evidence(parent, context, request, max_bytes=1)


def _inspect(field, *, time_of=lambda p: p["context"]["valid_times"][0], rows=1):
    def action(payload):
        return {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_baseline",
                "field": field,
                "valid_times": [time_of(payload)],
                "region": "point",
                "max_rows": rows,
            },
        }

    return action


def test_invalid_tool_request_and_phase_slip_are_rejected_without_ending_analysis():
    provider = Provider(
        [
            ASSESS,
            edit,  # An edit during prioritization is a counted protocol rejection.
            {**PRIORITY, "fields": [QPF, "not_a_saved_field"]},
            _inspect("wind_10m_typo"),
            _inspect(QPF),
            NO_EDIT,
            COMPLETE,
            REVIEW,
        ]
    )
    parent = forecast()
    final, report = run_forecast_desk(parent, provider=provider)
    assert final is parent and report["completion_reason"] == "no_edit"
    assert report["usage"]["rejected_actions"] == 1
    assert report["usage"]["tool_calls"] == 2
    assert report["usage"]["validated_actions"] == 8
    rejected = provider.requests[2]["last_result"]
    assert rejected["status"] == "rejected_action" and "prioritize" in rejected["reason"]
    tool = provider.requests[4]["last_result"]
    assert tool["status"] == "rejected_tool_request"
    assert "Unsupported inspection tool or field" in tool["reason"]
    assert provider.requests[5]["last_evidence"]["status"] == "available"
    assert any("priority_note" in row for row in report["audit"])
    later = provider.requests[3]
    assert later["assessment"] == ASSESS["summary"]
    assert later["task_queue"][0] == QPF and "not_a_saved_field" not in later["task_queue"]
    assert later["allowed_actions"] == ["evidence_request", "edit_proposal", "no_edit", "complete"]
    assert later["policy"] == {"id": "mesoforge.forecast-desk", "version": "1"}


def test_equivalent_utc_spellings_resolve_to_the_exact_saved_valid_time():
    def zulu(payload):
        action = edit(payload)
        saved = payload["context"]["valid_times"][0]
        action["proposal"]["valid_times"] = [saved.replace("+00:00", "Z")]
        return action

    final, report = run_forecast_desk(
        forecast(), provider=Provider([ASSESS, PRIORITY, zulu, COMPLETE, REVIEW])
    )
    assert report["usage"]["accepted_edits"] == 1
    saved = report["context"]["valid_times"][0]
    assert report["accepted_recipes"][0]["proposal"]["valid_times"] == [saved]
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4


def test_checkpoints_store_only_the_new_recipe_linked_to_the_previous_checkpoint():
    saved = []

    def sink(checkpoint):
        saved.append(checkpoint)
        return {"artifact_id": f"checkpoint-{len(saved)}"}

    provider = Provider(
        [
            ASSESS,
            PRIORITY,
            edit,
            lambda p: edit(p, operation="add", amount=0.5, cell="2:2"),
            COMPLETE,
            REVIEW,
        ]
    )
    final, report = run_forecast_desk(forecast(), provider=provider, checkpoint_sink=sink)
    assert len(saved) == 2 and "recipes" not in saved[1]
    assert saved[1]["recipe"] == report["accepted_recipes"][1]
    assert saved[1]["previous_checkpoint"] == {"artifact_id": "checkpoint-1"}
    assert saved[1]["previous_values_digest"] == saved[0]["values_digest"]
    assert saved[0]["previous_checkpoint"] is None
    assert replay_edits(forecast()["local_grid_baseline"], [c["recipe"] for c in saved])
    assert report["checkpoints"][2]["recipe_digest"] == str(
        canonical_json_digest(report["accepted_recipes"][1])
    )


def test_desk_policy_identity_is_versioned_and_separates_runtimes():
    from mesoforge.application.forecast_desk import desk_policy_identity

    first = desk_policy_identity(provider="openai", model="gpt-6-sol", reasoning_effort="low")
    other = desk_policy_identity(provider="openai", model="another", reasoning_effort="low")
    assert first["version"] == other["version"] == "1"
    assert first["digest"] == other["digest"]
    assert first["id"] == "mesoforge.forecast-desk:openai:gpt-6-sol:low" != other["id"]
    # Editing instructions, tool semantics or edit permissions requires a version bump:
    # this pin fails first, so one (id, version) never carries two policy digests.
    assert first["digest"] == PINNED_DESK_POLICY_V1_DIGEST


@pytest.mark.parametrize(
    "config,limit",
    [
        ({"max_edit_proposals": 2, "max_accepted_edits": 2}, ["proposal_budget"]),
        ({"max_edits_per_field": 1}, None),
    ],
)
def test_proposal_and_per_field_edit_budgets_keep_latest_valid_checkpoint(config, limit):
    provider = Provider(
        [
            ASSESS,
            PRIORITY,
            edit,
            lambda p: edit(p, operation="add", amount=0.5, cell="2:2"),
            lambda p: edit(p, operation="add", amount=0.25, cell="4:4"),
            COMPLETE,
            REVIEW,
        ]
    )
    final, report = run_forecast_desk(
        forecast(), provider=provider, config=replace(DeskConfig(), **config)
    )
    # Neither budget ends the analysis: global exhaustion moves to the final review,
    # and a per-field limit rejects that field's further proposals.
    assert report["completion_reason"] == "complete"
    assert report.get("budget_limits_reached") == limit
    assert report["usage"]["accepted_edits"] == min(config.get("max_edits_per_field", 2), 2)
    if limit is None:
        reasons = [
            row["edit_result"].get("reason", "") for row in report["audit"] if "edit_result" in row
        ]
        assert sum("Per-field edit budget" in reason for reason in reasons) == 2
    assert report["validation"]["status"] == "valid"
    assert (
        grid_values_digest(final["local_grid_baseline"])
        == report["checkpoints"][-1]["values_digest"]
    )


def test_permuted_repeat_and_net_inverse_selections_are_rejected():
    def spread(p, amount, cells):
        action = edit(p, operation="add", amount=amount)
        action["proposal"]["cell_ids"] = cells
        return action

    final, report = run_forecast_desk(
        forecast(),
        provider=Provider(
            [
                ASSESS,
                PRIORITY,
                lambda p: spread(p, 0.7, ["3:3", "2:2"]),
                lambda p: edit(p, operation="scale", amount=1.1),
                lambda p: spread(p, 0.7, ["2:2", "3:3"]),  # Permuted repeat.
                lambda p: spread(p, -0.7, ["2:2", "3:3"]),  # A, B, A^-1.
                COMPLETE,
                REVIEW,
            ]
        ),
    )
    reasons = [
        row["edit_result"].get("reason", "") for row in report["audit"] if "edit_result" in row
    ]
    assert report["usage"]["accepted_edits"] == 2
    assert "Repeated" in reasons[2] and "Inverse" in reasons[3]
    assert report["accepted_recipes"][0]["proposal"]["cell_ids"] == ["2:2", "3:3"]


def test_resubmission_after_evidence_rejection_and_malformed_times_continue():
    def unproven(p):
        action = edit(p)
        action["proposal"]["evidence_refs"] = ["sha256:" + "0" * 64]
        return action

    def naive(p):
        action = edit(p, operation="add", amount=0.5)
        action["proposal"]["valid_times"] = ["2026-10-20T03:00:00"]
        return action

    final, report = run_forecast_desk(
        forecast(),
        provider=Provider([ASSESS, PRIORITY, unproven, naive, edit, COMPLETE, REVIEW]),
    )
    assert report["completion_reason"] == "complete"
    assert report["usage"]["accepted_edits"] == 1
    assert report["usage"]["rejected_actions"] == 1
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4


def test_analysis_target_moves_to_final_review_within_the_hard_window():
    clock = [0.0]

    def slow_edit(p):
        clock[0] = 601.0
        return edit(p)

    provider = Provider([ASSESS, PRIORITY, slow_edit, REVIEW])
    final, report = run_forecast_desk(forecast(), provider=provider, monotonic=lambda: clock[0])
    assert report["forced_final_review"] == "analysis_target_reached"
    assert provider.requests[-1]["phase"] == "final_review"
    assert report["completion_reason"] == "complete"
    assert final["hours"][0]["surface"]["fields"][QPF]["value"] == 2.4
    provider = Provider([ASSESS, PRIORITY, NO_EDIT, NO_EDIT, REVIEW])
    final, report = run_forecast_desk(
        forecast(), provider=provider, config=replace(DeskConfig(), max_provider_calls=4)
    )
    # The last permitted provider call is reserved for the final review.
    assert report["forced_final_review"] == "last_provider_call"
    assert provider.requests[-1]["phase"] == "final_review"
    assert report["completion_reason"] == "no_edit"
