"""Finite operational forecast desk over a pinned corrected local forecast.

Providers can return validated actions only. They never receive the forecast
object, storage handles, executable tools, credentials or unbounded data.
"""

from __future__ import annotations

import json
import math
import queue
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from typing import Any

from mesoforge.application.forecast_desk_context import (
    DeskContextBudgetError,
    build_context,
    inspect_evidence,
    task_queue,
)
from mesoforge.application.forecast_desk_provider import REASONING_EFFORTS
from mesoforge.application.local_surface_grid import extract_grid_point
from mesoforge.contracts.forecast_desk import (
    DESK_POLICY,
    RECENT_INSPECTION_RESULTS,
    DeskConfig,
    DeskConfigurationError,
    DeskProviderError,
    DeskProviderUnavailableError,
    DeskResponse,
    ForecastDeskProvider,
    validate_action,
)
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import DEW_POINT, QPF, RH, TEMPERATURE
from mesoforge.forecasting.field_edit import (
    TOOL_VERSION,
    FieldEditError,
    apply_edit,
    grid_values_digest,
    validate_grid,
)

RECENT_INSPECTION_LIMIT = RECENT_INSPECTION_RESULTS
ALLOWED_ACTIONS = {
    "assess": ("assessment", "no_edit", "complete"),
    "prioritize": ("task_priority", "no_edit", "complete"),
    "task": ("evidence_request", "edit_proposal", "no_edit", "complete"),
    "final_review": ("final_review", "no_edit", "complete"),
}


_SUMMARY_KEYS = (
    "schema_version",
    "provider",
    "model",
    "inference_settings",
    "context_digest",
    "pinned_evidence",
    "completion_reason",
    "attempt_completion_reason",
    "issued_checkpoint",
    "validation",
    "assessment",
    "final_review",
    "usage",
    "timings",
    "affected_fields",
    "point_values",
    "provider_failure_code",
    "configuration_error",
    "failure_type",
    "started_at",
    "ended_at",
    "tool_policy_version",
)


def desk_summary(desk: dict[str, Any]) -> dict[str, Any]:
    """Compact issued-forecast view; the complete run report lives in the AI stage.

    Recipes are summarized by digest and proposal (never per-cell change lists), and
    context, audit and evidence rows are referenced by digest only.
    """

    def compact(recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "recipe_digest": str(canonical_json_digest(recipe)),
                "output_values_sha256": recipe.get("output_values_sha256"),
                **{
                    key: recipe.get("proposal", {}).get(key)
                    for key in ("field", "operation", "parameters", "valid_times", "cell_ids")
                },
            }
            for recipe in recipes
        ]

    policy = desk.get("policy") or {}
    return {
        "representation": "issued_summary_full_report_in_ai_stage",
        "policy": {key: policy.get(key) for key in ("id", "version")},
        **{key: desk[key] for key in _SUMMARY_KEYS if key in desk},
        "checkpoint_digests": [row.get("values_digest") for row in desk.get("checkpoints", [])],
        "accepted_recipes": compact(desk.get("accepted_recipes", [])),
        "discarded_recipes": compact(desk.get("discarded_recipes", [])),
    }


def desk_policy_identity(
    *, provider: str, model: str, reasoning_effort: str | None
) -> dict[str, Any]:
    """Immutable evaluator series identity for one runtime desk configuration.

    The digest binds the versioned instructions, the deterministic tool version and the
    field-registry edit permissions. Provider, model and reasoning effort are part of
    the series id, so different runtimes are separate series and never pooled evidence.
    Execution budgets are retained in each run report but do not split series.
    """
    from mesoforge.forecasting.field_blend import FIELD_REGISTRY, field_edit_contract

    permissions = {
        name: asdict(field_edit_contract(name))
        for name in sorted(FIELD_REGISTRY)
        if field_edit_contract(name).operations
    }
    effort = reasoning_effort or "provider-default"
    return {
        "id": f"{DESK_POLICY['id']}:{_identity(provider)}:{_identity(model)}:{effort}",
        "version": DESK_POLICY["version"],
        "digest": str(
            canonical_json_digest(
                {
                    "policy": DESK_POLICY,
                    "tool_policy_version": TOOL_VERSION,
                    "edit_permissions": permissions,
                }
            )
        ),
        "desk_policy_id": DESK_POLICY["id"],
        "tool_policy_version": TOOL_VERSION,
        "provider": _identity(provider),
        "model": _identity(model),
        "reasoning_effort": reasoning_effort,
    }


class DeskBudgetExceededError(RuntimeError):
    """A finite runtime budget has been reached; retain the last valid checkpoint."""


class DeskOperationTimeoutError(TimeoutError):
    """A bounded local operation did not finish before its deadline."""


def _bounded_operation(operation: Callable[[], Any], timeout: float, name: str) -> Any:
    """Late results cannot replace a checkpoint; immutable orphan artifacts are harmless."""
    if timeout <= 0:
        raise DeskOperationTimeoutError(name)
    mailbox: queue.Queue[Any] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            mailbox.put((True, operation()))
        except Exception as exc:
            mailbox.put((False, exc))

    threading.Thread(target=invoke, daemon=True, name=f"mesoforge-desk-{name}").start()
    try:
        succeeded, result = mailbox.get(timeout=timeout)
    except queue.Empty:
        raise DeskOperationTimeoutError(name) from None
    if not succeeded:
        raise result
    return result


def _now() -> datetime:
    return datetime.now(UTC)


def _identity(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", value) else "invalid_identity"


def _record_usage(report: dict[str, Any], response: Any) -> None:
    usage = report["usage"]
    usage["input_tokens"] += response.input_tokens
    usage["output_tokens"] += response.output_tokens
    if response.cost_usd is not None:
        usage["cost_usd"] = (usage["cost_usd"] or 0) + response.cost_usd
    if response.response_model is not None:
        report.setdefault("response_models", []).append(_identity(response.response_model))


def _pinned_times(values: list[str], pinned: list[str]) -> list[str]:
    """Map equivalent ISO spellings ('Z' or '+00:00') onto the exact saved valid times.

    Unknown instants are kept verbatim so deterministic validation rejects them.
    """
    by_instant: dict[datetime, str] = {}
    for text in pinned:
        by_instant[datetime.fromisoformat(text.replace("Z", "+00:00"))] = text
    result = []
    for text in values:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            result.append(text)
            continue
        result.append(by_instant.get(moment, text))
    return result


def _inverse(proposal: dict[str, Any], recipes: list[dict[str, Any]]) -> bool:
    """Net return to an earlier state on the same selection (A, B, A^-1 included).

    Selections are canonical (sorted) before comparison, so permuted lists match.
    """
    same = [
        recipe["proposal"]
        for recipe in recipes
        if all(
            recipe["proposal"][key] == proposal[key]
            for key in ("field", "operation", "valid_times", "cell_ids", "taper")
        )
    ]
    if not same:
        return False
    if proposal["operation"] == "add":
        deltas = [row["parameters"]["delta"] for row in same]
        return abs(math.fsum([*deltas, proposal["parameters"]["delta"]])) < 1e-9
    if proposal["operation"] == "scale":
        product = float(proposal["parameters"]["factor"])
        for row in same:
            product *= float(row["parameters"]["factor"])
        return abs(product - 1.0) < 1e-9
    return False


def _request(
    provider: ForecastDeskProvider,
    payload: dict[str, Any],
    timeout: float,
    tokens: int,
) -> DeskResponse:
    """Stop waiting at deadline even if an adapter misbehaves.

    One daemon worker gets a bounded JSON copy only. On timeout the desk ends;
    no further worker is started and the worker cannot touch forecast state.
    The concrete HTTP adapter also has transport deadlines and no retries.
    """
    mailbox: queue.Queue[DeskResponse | Exception] = queue.Queue(maxsize=1)
    copied = json.loads(canonical_json_bytes(payload))

    def invoke() -> None:
        try:
            mailbox.put(provider.request(copied, timeout_seconds=timeout, max_output_tokens=tokens))
        except Exception as exc:
            mailbox.put(exc)

    threading.Thread(target=invoke, daemon=True, name="mesoforge-bounded-desk-request").start()
    try:
        response = mailbox.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("Forecast desk provider deadline reached") from None
    if isinstance(response, Exception):
        raise response
    if not isinstance(response, DeskResponse):
        raise ValueError("Provider response does not satisfy the structured protocol")
    return response


def run_forecast_desk(
    forecast: dict[str, Any],
    *,
    provider: ForecastDeskProvider | None = None,
    config: DeskConfig | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = _now,
    wait: Callable[[float], None] = time.sleep,
    evidence: dict[str, Any] | None = None,
    checkpoint_sink: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Always attempt the desk; return the complete latest deterministic valid state.

    No switch changes this into an off/shadow mode. An unavailable adapter or
    malformed config has an explicit fallback outcome. Injected providers/clocks
    make lower-level development replay deterministic and free of paid calls.
    """
    started = monotonic()
    parent_grid: dict[str, Any] = forecast.get("local_grid_baseline", {})
    current = parent_grid
    report: dict[str, Any] = {
        "schema_version": "mesoforge.forecast-desk-run.v1",
        "policy": dict(DESK_POLICY),
        "provider": "unavailable",
        "model": "unconfigured",
        "context_digest": None,
        "pinned_evidence": {},
        "accepted_recipes": [],
        "checkpoints": [],
        "audit": [],
        "task_queue": [],
        "completion_reason": "not_started",
        "validation": {"status": "pending"},
        "usage": {
            "provider_calls": 0,
            "tool_calls": 0,
            "edit_proposals": 0,
            "accepted_edits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": None,
            "unreported_provider_calls": 0,
            "validated_actions": 0,
            "rejected_actions": 0,
        },
        "timings": {
            "context_seconds": 0.0,
            "provider_seconds": 0.0,
            "provider_pacing_seconds": 0.0,
            "inspection_seconds": 0.0,
            "edit_seconds": 0.0,
            "validation_seconds": 0.0,
            "checkpoint_storage_seconds": 0.0,
        },
        "started_at": clock().astimezone(UTC).isoformat(),
        "tool_policy_version": TOOL_VERSION,
    }
    final = forecast
    usage, timings = report["usage"], report["timings"]
    valid_digests: set[str] = set()
    proposals: set[str] = set()
    field_edits: Counter[str] = Counter()
    evidence_refs: set[str] = set()
    context: dict[str, Any] = {}
    phase, index = "assess", 0
    parent_valid = False
    last_evidence: dict[str, Any] | None = None
    recent_evidence: list[dict[str, Any]] = []
    inspection_index: dict[str, dict[str, Any]] = {}
    last_result: dict[str, Any] | None = None
    last_request_started: float | None = None
    try:
        from mesoforge.application.forecast_desk_provider import (
            config_from_environment,
            provider_from_environment,
        )

        config = config or config_from_environment()
        provider = provider or provider_from_environment()
        report.update(
            provider=_identity(provider.provider_name),
            model=_identity(provider.model_name),
            budgets=asdict(config),
        )
        effort = getattr(provider, "reasoning_effort", None)
        # Every supported explicit effort, including none/minimal, is recorded; only an
        # unset effort is the provider default. The marker distinguishes this recording
        # from earlier stages, where none/minimal were pooled into provider-default.
        report["inference_settings"] = {
            "reasoning_effort": effort if effort in REASONING_EFFORTS else None,
            "effort_recording": "explicit.v1",
        }
        # Initial validation is required, even when provider configuration is absent.
        before = monotonic()
        validate_grid(current)
        parent_valid = True
        report["validation"] = {"status": "valid", "basis": "validated_corrected_parent"}
        timings["validation_seconds"] += monotonic() - before
        before = monotonic()
        context = build_context(forecast, max_bytes=config.max_context_bytes, evidence=evidence)
        report.update(
            context_digest=context["context_digest"], pinned_evidence=context["pinned_evidence"]
        )
        report["context"] = context
        evidence_refs.add(context["context_digest"])
        report["task_queue"] = task_queue(context, config.max_tasks)
        timings["context_seconds"] = monotonic() - before
        valid_digests.add(grid_values_digest(current))
        report["checkpoints"].append(
            {
                "index": 0,
                "values_digest": grid_values_digest(current),
                "validation": "valid",
                "parent_stage": context["pinned_evidence"]["corrected_stage_id"],
            }
        )
        work_end = started + config.hard_seconds - config.finalization_reserve_seconds
        # A bounded for loop, not a model-controlled continuation loop.
        for _ in range(config.max_provider_calls):
            delay = (
                max(0.0, last_request_started + config.min_provider_interval_seconds - monotonic())
                if last_request_started is not None
                else 0.0
            )
            start_at = monotonic() + delay
            remaining_calls = config.max_provider_calls - usage["provider_calls"]
            if phase in ("prioritize", "task") and "forced_final_review" not in report:
                # Leave the analysis window through the single final review (bounded by
                # the hard work deadline), never by silently skipping it.
                if start_at >= started + config.target_seconds:
                    report["forced_final_review"] = "analysis_target_reached"
                    phase = "final_review"
                elif remaining_calls == 1:
                    report["forced_final_review"] = "last_provider_call"
                    phase = "final_review"
            window_end = (
                work_end
                if phase == "final_review"
                else min(work_end, started + config.target_seconds)
            )
            if start_at >= window_end:
                raise DeskBudgetExceededError("analysis_time_budget")
            if delay:
                before = monotonic()
                wait(delay)
                timings["provider_pacing_seconds"] += monotonic() - before
            if monotonic() >= window_end:
                raise DeskBudgetExceededError("analysis_time_budget")
            if phase == "task" and index >= len(report["task_queue"]):
                phase = "final_review"
            task = report["task_queue"][index] if phase == "task" else None
            payload = {
                # Instructions travel once, as the adapter's system instructions.
                "policy": {"id": DESK_POLICY["id"], "version": DESK_POLICY["version"]},
                "phase": phase,
                "allowed_actions": list(ALLOWED_ACTIONS[phase]),
                "context": context,
                # The desk's own earlier assessment and the bounded queue, so later
                # phases continue the same analysis instead of re-deriving it.
                "assessment": report.get("assessment"),
                "task_queue": [row["field"] for row in report["task_queue"]],
                "task": task,
                "last_evidence": last_evidence,
                # Latest result occurs only once. Earlier detailed results have a
                # fixed four-result memory bound; the finite index identifies older
                # inspected windows without retaining their rows in the request.
                "earlier_evidence": recent_evidence[:-1],
                "inspection_index": list(inspection_index.values()),
                "last_result": last_result,
                "remaining_budgets": {
                    "provider_calls_including_this_request": (
                        config.max_provider_calls - usage["provider_calls"]
                    ),
                    "tool_calls": config.max_tool_calls - usage["tool_calls"],
                    "edit_proposals": config.max_edit_proposals - usage["edit_proposals"],
                    "accepted_edits": config.max_accepted_edits - usage["accepted_edits"],
                    "edits_per_field": {
                        field: config.max_edits_per_field - field_edits[field]
                        for field, summary in context["fields"].items()
                        if summary["edit_contract"]["operations"]
                    },
                    "analysis_seconds": max(
                        0.0, min(work_end, started + config.target_seconds) - monotonic()
                    ),
                    "finalization_reserve_seconds": config.finalization_reserve_seconds,
                    "total_tokens": max(
                        0, config.max_total_tokens - usage["input_tokens"] - usage["output_tokens"]
                    ),
                    "max_response_tokens": config.max_output_tokens,
                    "cost_usd": (
                        max(0.0, config.max_cost_usd - (usage["cost_usd"] or 0))
                        if config.max_cost_usd is not None
                        else None
                    ),
                    "max_tool_output_bytes": config.max_tool_output_bytes,
                    "recent_inspection_result_limit": RECENT_INSPECTION_LIMIT,
                },
                "accepted_edits": [
                    {
                        "recipe_digest": str(canonical_json_digest(r)),
                        **{
                            key: r["proposal"][key]
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
                    for r in report["accepted_recipes"]
                ],
                "latest_valid_checkpoint_digest": report["checkpoints"][-1]["values_digest"],
            }
            size = len(canonical_json_bytes(payload))
            # One UTF-8 byte per token is a conservative preflight cap, not billed usage.
            estimate_tokens = getattr(provider, "estimate_input_tokens", None)
            input_bound = (
                estimate_tokens(payload, config.max_output_tokens) if estimate_tokens else size
            )
            reserved = input_bound + config.max_output_tokens
            if usage["input_tokens"] + usage["output_tokens"] + reserved > config.max_total_tokens:
                raise DeskBudgetExceededError("token_budget")
            if config.max_cost_usd is not None:
                estimate = getattr(provider, "estimate_max_cost", lambda *_: None)(
                    payload, config.max_output_tokens
                )
                if estimate is None or estimate + (usage["cost_usd"] or 0) > config.max_cost_usd:
                    raise DeskBudgetExceededError("cost_budget_or_price_unavailable")
            timeout = min(config.request_timeout_seconds, window_end - monotonic())
            if timeout <= 0:
                raise DeskBudgetExceededError("analysis_time_budget")
            usage["provider_calls"] += 1
            before = monotonic()
            last_request_started = before
            try:
                response = _request(provider, payload, timeout, config.max_output_tokens)
            except DeskProviderError as exc:
                report["provider_failure_code"] = _identity(exc.reason_code)
                if exc.usage is not None:
                    _record_usage(report, exc.usage)
                else:
                    usage["unreported_provider_calls"] += 1
                raise
            except Exception:
                usage["unreported_provider_calls"] += 1
                raise
            finally:
                timings["provider_seconds"] += monotonic() - before
            _record_usage(report, response)
            if monotonic() >= work_end:
                raise DeskBudgetExceededError("analysis_time_budget")
            if usage["input_tokens"] + usage["output_tokens"] > config.max_total_tokens:
                raise DeskBudgetExceededError("token_budget")
            if config.max_cost_usd is not None and (usage["cost_usd"] or 0) > config.max_cost_usd:
                raise DeskBudgetExceededError("cost_budget")
            try:
                action = validate_action(response.action)
            except ValueError as exc:
                raw = response.action if isinstance(response.action, dict) else {}
                if raw.get("type") not in ("edit_proposal", "evidence_request"):
                    raise
                # Bounds/format errors in a known action (a naive time, an overlong
                # rationale) are rejected and counted; they do not end the analysis.
                usage["rejected_actions"] += 1
                usage["edit_proposals" if raw["type"] == "edit_proposal" else "tool_calls"] += 1
                last_result = {
                    "status": "rejected_action",
                    "reason": f"Malformed {raw['type']}: {str(exc)[:200]}",
                }
                report["audit"].append({"phase": phase, "protocol_rejection": last_result})
                continue
            usage["validated_actions"] += 1
            for holder in (action.get("request"), action.get("proposal")):
                if isinstance(holder, dict):
                    # Canonical selections: equivalent UTC spellings map to the saved
                    # strings and list order cannot disguise repeats or inverses.
                    holder["valid_times"] = sorted(
                        _pinned_times(holder["valid_times"], context["valid_times"])
                    )
                    if holder.get("cell_ids") is not None:
                        holder["cell_ids"] = sorted(holder["cell_ids"])
            report["audit"].append({"phase": phase, "task": task, "action": action})
            kind = action["type"]
            if kind not in ALLOWED_ACTIONS[phase]:
                # A schema-valid action in the wrong phase is a counted, rejected
                # request; the provider-call budget bounds repeated slips.
                usage["rejected_actions"] += 1
                last_result = {
                    "status": "rejected_action",
                    "reason": f"{kind} is not permitted in phase {phase}",
                    "allowed_actions": list(ALLOWED_ACTIONS[phase]),
                }
                report["audit"].append({"protocol_rejection": last_result})
                continue
            if phase == "assess":
                if kind in {"no_edit", "complete"}:
                    report["completion_reason"] = "no_edit"
                    break
                report["assessment"] = action["summary"]
                last_result = None
                phase = "prioritize"
            elif phase == "prioritize":
                if kind == "complete":
                    report["completion_reason"] = "no_edit"
                    break
                if kind == "task_priority":
                    names = [name for name in action["fields"] if name in context["fields"]]
                    unknown = sorted(set(action["fields"]) - set(context["fields"]))
                    if unknown:
                        report["audit"].append(
                            {"priority_note": {"ignored_unknown_fields": unknown[:32]}}
                        )
                    existing = [row["field"] for row in report["task_queue"]]
                    names = list(dict.fromkeys([*names, *existing]))
                    if context["precipitation_relevant"] and QPF in names:
                        names = [QPF, *(n for n in names if n != QPF)]
                    report["task_queue"] = [
                        {"field": name, "priority": i + 1}
                        for i, name in enumerate(names[: config.max_tasks])
                    ]
                last_result = None
                phase = "task"
            elif phase == "final_review":
                report["final_review"] = action
                if action.get("accepted") is False and report["accepted_recipes"]:
                    # The desk's own review rejected its edits: issue the validated
                    # corrected parent and keep the recipes only as discarded audit.
                    report["discarded_recipes"] = report["accepted_recipes"]
                    report["accepted_recipes"] = []
                    report["discarded_checkpoints"] = report["checkpoints"][1:]
                    report["checkpoints"] = report["checkpoints"][:1]
                    current = parent_grid
                report["completion_reason"] = (
                    "review_not_accepted"
                    if action.get("accepted") is False
                    else "complete"
                    if report["accepted_recipes"]
                    else "no_edit"
                )
                break
            elif kind == "evidence_request":
                if usage["tool_calls"] >= config.max_tool_calls:
                    usage["rejected_actions"] += 1
                    last_result = {
                        "status": "rejected_tool_request",
                        "reason": "Inspection budget exhausted; propose, no_edit or complete",
                    }
                    report.setdefault("budget_limits_reached", []).append("tool_budget")
                    report["audit"].append({"tool_result": last_result})
                    continue
                usage["tool_calls"] += 1
                before = monotonic()
                try:
                    inspected = inspect_evidence(
                        {**forecast, "local_grid_baseline": current},
                        context,
                        action["request"],
                        max_bytes=config.max_tool_output_bytes,
                        state_digest=report["checkpoints"][-1]["values_digest"],
                    )
                except ValueError as exc:
                    # A malformed request (unknown field, time outside the pinned
                    # horizon, row bound) is a counted, rejected tool call with a fixed
                    # local message. It cannot end the analysis or expose other data.
                    timings["inspection_seconds"] += monotonic() - before
                    last_result = {
                        "status": "rejected_tool_request",
                        "request": action["request"],
                        "reason": str(exc)[:256],
                        "hint": "Use field names and valid_time strings exactly as in context.",
                    }
                    report["audit"].append({"tool_result": last_result})
                    continue
                last_evidence = inspected
                timings["inspection_seconds"] += monotonic() - before
                evidence_refs.add(last_evidence["evidence_ref"])
                reference = last_evidence["evidence_ref"]
                recent_evidence = [
                    result for result in recent_evidence if result["evidence_ref"] != reference
                ]
                recent_evidence.append(last_evidence)
                recent_evidence = recent_evidence[-RECENT_INSPECTION_LIMIT:]
                previous = inspection_index.get(reference)
                inspection_index[reference] = {
                    "request": action["request"],
                    "evidence_ref": reference,
                    "current_state_digest": last_evidence["current_state_digest"],
                    "status": "truncated" if last_evidence["truncated"] else "complete",
                    "returned_rows": len(last_evidence["rows"]),
                    "total_matching_rows": last_evidence["total_matching_rows"],
                    "request_count": 1 if previous is None else previous["request_count"] + 1,
                }
                report["audit"].append({"evidence": last_evidence})
            elif kind == "edit_proposal":
                spent = (
                    "proposal_budget"
                    if usage["edit_proposals"] >= config.max_edit_proposals
                    else "accepted_edit_budget"
                    if usage["accepted_edits"] >= config.max_accepted_edits
                    else None
                )
                if spent is not None:
                    # Global edit budgets are spent: finish through the final review.
                    report.setdefault("budget_limits_reached", []).append(spent)
                    last_result = {
                        "status": "rejected",
                        "reason": f"{spent} exhausted; the final review follows",
                    }
                    report["audit"].append({"edit_result": last_result})
                    phase = "final_review"
                    continue
                usage["edit_proposals"] += 1
                proposal = action["proposal"]
                proposal_digest = str(
                    canonical_json_digest(
                        {
                            k: v
                            for k, v in proposal.items()
                            if k not in {"rationale", "evidence_refs"}
                        }
                    )
                )
                try:
                    # Contextual checks first: a proposal rejected only for its task or
                    # evidence references can be resubmitted in corrected form.
                    if task is None or proposal["field"] != task["field"]:
                        raise FieldEditError("Edit must belong to the current bounded task")
                    if (
                        not proposal["evidence_refs"]
                        or not set(proposal["evidence_refs"]) <= evidence_refs
                    ):
                        raise FieldEditError(
                            "Edit must reference evidence actually provided to this desk"
                        )
                    if field_edits[proposal["field"]] >= config.max_edits_per_field:
                        raise FieldEditError(
                            "Per-field edit budget exhausted; use no_edit to advance the task"
                        )
                    if proposal_digest in proposals:
                        raise FieldEditError("Repeated accepted or rejected edit")
                    proposals.add(proposal_digest)
                    if _inverse(proposal, report["accepted_recipes"]):
                        raise FieldEditError("Inverse oscillation to an earlier state")
                    before = monotonic()
                    proposed, recipe = _bounded_operation(
                        partial(apply_edit, current, proposal),
                        work_end - monotonic(),
                        "field_edit",
                    )
                    timings["edit_seconds"] += monotonic() - before
                    digest = grid_values_digest(proposed)
                    if digest in valid_digests:
                        raise FieldEditError(
                            "No-op or inverse oscillation returns a prior valid state"
                        )
                    before = monotonic()
                    validate_grid(proposed)
                    timings["validation_seconds"] += monotonic() - before
                    if monotonic() >= work_end:
                        raise DeskBudgetExceededError("analysis_time_budget")
                    checkpoint = {
                        "schema_version": "mesoforge.forecast-desk-checkpoint.v1",
                        "index": len(report["accepted_recipes"]) + 1,
                        "context_digest": context["context_digest"],
                        "pinned_evidence": context["pinned_evidence"],
                        "values_digest": digest,
                        "previous_values_digest": report["checkpoints"][-1]["values_digest"],
                        # Only the new recipe; earlier ones live in their own checkpoints,
                        # linked by digest and reference, so storage grows linearly.
                        "previous_checkpoint": report["checkpoints"][-1].get("artifact_reference"),
                        "recipe": recipe,
                        "validation": "valid",
                    }
                    before = monotonic()
                    saved = (
                        _bounded_operation(
                            partial(checkpoint_sink, checkpoint),
                            work_end - monotonic(),
                            "checkpoint_storage",
                        )
                        if checkpoint_sink is not None
                        else None
                    )
                    timings["checkpoint_storage_seconds"] += monotonic() - before
                    # Atomic acceptance occurs only after complete validation/storage.
                    current = proposed
                    valid_digests.add(digest)
                    report["accepted_recipes"].append(recipe)
                    report["checkpoints"].append(
                        {
                            **{k: v for k, v in checkpoint.items() if k != "recipe"},
                            "recipe_digest": str(canonical_json_digest(recipe)),
                        }
                    )
                    report["checkpoints"][-1]["artifact_reference"] = saved
                    usage["accepted_edits"] += 1
                    field_edits[proposal["field"]] += 1
                    last_result = {
                        "status": "accepted",
                        "checkpoint": checkpoint["index"],
                        "values_digest": digest,
                        "proposal": proposal,
                        "current_state_note": (
                            "Initial summary is the pinned parent; "
                            "inspection returns latest validated fields"
                        ),
                    }
                except (DeskBudgetExceededError, DeskOperationTimeoutError):
                    raise
                except Exception as exc:
                    # Provider/network exception strings can contain secrets. Only local
                    # deterministic ValueError messages are exposed; other failures are typed.
                    reason = (
                        str(exc)[:256] if isinstance(exc, FieldEditError) else type(exc).__name__
                    )
                    last_result = {"status": "rejected", "reason": reason}
                report["audit"].append({"edit_result": last_result})
            elif kind == "no_edit":
                index += 1
                last_result = None
            else:  # "complete" is the only remaining permitted task action.
                phase = "final_review"
        else:
            raise DeskBudgetExceededError("provider_budget")
    except DeskContextBudgetError:
        report["completion_reason"] = "context_budget_exceeded"
    except DeskConfigurationError as exc:
        # Names the operator setting only; the offending value is never retained.
        report["completion_reason"] = "configuration_invalid"
        report["configuration_error"] = exc.setting
    except DeskProviderUnavailableError:
        report["completion_reason"] = "provider_unavailable"
    except DeskProviderError:
        # HTTP/quota/rate-limit/malformed-output failures retain provider_failure_code.
        report["completion_reason"] = "provider_failure"
    except DeskOperationTimeoutError as exc:
        report["completion_reason"] = f"{exc}_timeout"
    except TimeoutError:
        report["completion_reason"] = "provider_timeout"
    except DeskBudgetExceededError as exc:
        report["completion_reason"] = str(exc)
    except Exception as exc:
        report["completion_reason"] = "desk_failure"
        report["failure_type"] = type(exc).__name__
    # Finalization never invokes a provider. Preserve the latest valid state.
    before = monotonic()
    try:
        if current is not None:
            validate_grid(current)
            report["validation"] = {
                "status": "valid",
                "basis": "latest_valid_checkpoint"
                if report["accepted_recipes"]
                else "validated_corrected_parent",
            }
        if current is not parent_grid:
            extracted = _bounded_operation(
                partial(
                    extract_grid_point,
                    current,
                    latitude=forecast["latitude"],
                    longitude=forecast["longitude"],
                    copy_grid=False,
                ),
                started + (config.hard_seconds if config is not None else 900) - monotonic(),
                "final_extraction",
            )
            final = {**forecast, **extracted}
    except Exception as exc:
        final = forecast
        report.update(
            attempt_completion_reason=report.get("completion_reason"),
            attempt_failure_type=report.get("failure_type"),
            completion_reason="final_validation_fallback",
            failure_type=type(exc).__name__,
        )
        report["validation"] = {
            "status": "valid" if parent_valid else "invalid",
            "basis": "unchanged_corrected_parent" if parent_valid else "parent_validation_failed",
        }
        report["discarded_recipes"] = report["accepted_recipes"]
        report["accepted_recipes"] = []
        report["discarded_checkpoints"] = report["checkpoints"][1:]
        report["checkpoints"] = report["checkpoints"][:1]
    timings["finalization_seconds"] = monotonic() - before
    timings["total_seconds"] = monotonic() - started
    report["ended_at"] = clock().astimezone(UTC).isoformat()
    affected = {recipe["proposal"]["field"] for recipe in report["accepted_recipes"]}
    if TEMPERATURE in affected:
        affected.update({DEW_POINT, RH})
    report["affected_fields"] = sorted(affected)
    report["point_values"] = [
        {
            "valid_time": original["valid_time"],
            "corrected_temperature": original["temperature"],
            "final_temperature": changed["temperature"],
            "applied_delta_k": (
                changed["temperature"]["value"] - original["temperature"]["value"]
                if changed["temperature"]["value"] is not None
                and original["temperature"]["value"] is not None
                else 0
            ),
        }
        for original, changed in zip(forecast["hours"], final["hours"], strict=True)
    ]
    report["storage_bytes"] = len(canonical_json_bytes(report))
    return final, report
