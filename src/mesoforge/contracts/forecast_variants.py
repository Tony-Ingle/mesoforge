"""Compact immutable forecast stages shared by corrections, candidates and evaluation.

An output creation time is not an information cutoff. Policies and training evidence
must already exist at analysis time; deterministic computation may finish afterward.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import canonical_json_bytes

VARIANT_SCHEMA = "mesoforge.forecast-variant.v1"
TRANSFORMATIONS = frozenset(
    {"active_baseline", "deterministic_corrected", "candidate_blend", "ai_adjusted"}
)
ROLES = frozenset({"active", "candidate", "shadow", "retired"})
NO_EVIDENCE_STATES = frozenset(
    {"no_policy", "insufficient_evidence", "fallback", "active_storage_failed_fallback"}
)


def instant(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Variant times must include a timezone")
    return parsed.astimezone(UTC)


def _overlay(value: dict[str, Any]) -> bool:
    """Validate compact point prediction identity; return whether this is a real no-op."""
    overlay = value.get("overlay")
    if not isinstance(overlay, dict) or type(overlay.get("inherit_unchanged")) is not bool:
        raise ValueError("Variant requires explicit overlay inheritance semantics")
    predictions = overlay.get("predictions")
    if not isinstance(predictions, list):
        raise ValueError("Variant predictions must be a list")
    seen: set[tuple[str, datetime]] = set()
    for row in predictions:
        if not isinstance(row, dict):
            raise ValueError("Variant predictions must be records")
        field = row.get("field")
        if not isinstance(field, str) or field not in value["fields"]:
            raise ValueError("Prediction field is absent from variant field identity")
        valid_value = row.get("valid_time")
        if not isinstance(valid_value, (str, datetime)):
            raise ValueError("Prediction valid time is unavailable")
        valid = instant(valid_value)
        key = (field, valid)
        if key in seen:
            raise ValueError("Duplicate field/valid-time prediction in one variant")
        seen.add(key)
        amount, unit = row.get("value"), row.get("unit")
        if amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount)):
            raise ValueError("Variant predictions require finite values or explicit null")
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError("Variant predictions must retain units")
        start, end = row.get("interval_start"), row.get("interval_end")
        if (start is None) != (end is None):
            raise ValueError("Accumulation bounds must both be retained or both unavailable")
        if start is not None:
            if (
                not isinstance(start, (str, datetime))
                or not isinstance(end, (str, datetime))
                or not instant(start) < instant(end) == valid
            ):
                raise ValueError("Variant interval must end at its valid time and follow its start")
    correction = overlay.get("correction", {})
    if not isinstance(correction, dict):
        raise ValueError("Correction reporting must be structured")
    return (
        overlay["inherit_unchanged"] is True
        and not predictions
        and not correction.get("changes")
        and correction.get("applied_delta_k", 0) == 0
    )


def validate_variant(value: dict[str, Any]) -> None:
    """Reject changed identities, incomplete lineage and future-trained policies."""
    if value.get("schema_version") != VARIANT_SCHEMA:
        raise ValueError("Unsupported forecast variant schema")
    body = {k: v for k, v in value.items() if k != "variant_id"}
    if value.get("variant_id") != str(Digest.of_bytes(canonical_json_bytes(body))):
        raise ValueError("Forecast variant content differs from its immutable identity")
    required = (
        "transformation_type",
        "lifecycle_role",
        "parent_stage_id",
        "baseline_snapshot_id",
        "prepared_snapshot_id",
        "fields",
        "policy",
        "location",
        "analysis_cutoff",
        "reference_time",
        "created_at",
        "evidence_required",
        "evidence_status",
    )
    if any(key not in value for key in required):
        raise ValueError("Variant identity/lineage is incomplete")
    if value["transformation_type"] not in TRANSFORMATIONS or value["lifecycle_role"] not in ROLES:
        raise ValueError("Unsupported variant transformation or lifecycle role")
    if value["transformation_type"] != "active_baseline":
        Digest(value["parent_stage_id"])
    elif value.get("parent_stage_id") is not None:
        raise ValueError("A control baseline stage cannot have a transformed parent")
    if any(
        not isinstance(value[key], str) or not value[key].strip()
        for key in ("baseline_snapshot_id", "prepared_snapshot_id")
    ):
        raise ValueError("Variant must retain baseline and contributor-state identities")
    fields, policy = value["fields"], value["policy"]
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field.strip() for field in fields)
        or len(set(fields)) != len(fields)
        or not isinstance(policy, dict)
        or any(
            not isinstance(policy.get(key), str) or not policy[key].strip()
            for key in ("id", "version")
        )
    ):
        raise ValueError("Variant fields and versioned policy are required")
    if policy.get("digest") is not None:
        Digest(policy["digest"])
    if not isinstance(value["location"], dict):
        raise ValueError("Variant location must be a geographic coordinate record")
    for axis, bound in (("latitude", 90), ("longitude", 180)):
        number = value["location"].get(axis)
        if (
            isinstance(number, bool)
            or not isinstance(number, (float, int))
            or not math.isfinite(number)
            or abs(number) > bound
        ):
            raise ValueError("Variant location must be a finite geographic coordinate")
    cutoff = instant(value["analysis_cutoff"])
    reference = instant(value["reference_time"])
    if reference.minute or reference.second or reference.microsecond or reference > cutoff:
        raise ValueError("Variant reference must be a covered decision hour at/before analysis")
    if instant(value["created_at"]) < cutoff:
        raise ValueError("Variant output cannot precede its analysis")
    no_op = _overlay(value)
    if type(value["evidence_required"]) is not bool:
        raise ValueError("Evidence requirement must be explicit")
    baseline = value["transformation_type"] == "active_baseline"
    if baseline and (
        value["evidence_required"]
        or value["evidence_status"] != "baseline"
        or value["overlay"]["inherit_unchanged"]
        or not value["overlay"]["predictions"]
    ):
        raise ValueError(
            "Raw baseline requires explicit saved predictions and baseline evidence identity"
        )
    if (
        not baseline
        and not value["evidence_required"]
        and (
            value["transformation_type"] != "deterministic_corrected"
            or value["evidence_status"] not in NO_EVIDENCE_STATES
            or not no_op
        )
    ):
        raise ValueError(
            "Evidence exemption is only valid for an explicit unchanged correction stage"
        )
    evidence = value.get("evidence_cutoff")
    runtime_ai = (
        value["transformation_type"] == "ai_adjusted"
        and value.get("evidence_basis") == "pinned_forecast_evidence"
    )
    if runtime_ai:
        pinned = value.get("pinned_evidence")
        overlay = value.get("overlay")
        desk = overlay.get("desk") if isinstance(overlay, dict) else None
        policy_record = value.get("policy")
        if (
            not isinstance(pinned, dict)
            or not isinstance(value.get("validation"), dict)
            or not isinstance(overlay, dict)
            or not isinstance(desk, dict)
            or not isinstance(policy_record, dict)
            or overlay.get("inherit_unchanged") is not False
            or not isinstance(overlay.get("predictions"), list)
            or not overlay["predictions"]
            or not isinstance(desk.get("accepted_recipes"), list)
            or any(
                not isinstance(policy_record.get(key), str) or not policy_record[key].strip()
                for key in ("provider", "model", "tool_policy_version")
            )
        ):
            raise ValueError(
                "AI stage requires provider/model identity, explicit predictions and recipes"
            )
        result_grid = overlay.get("result_grid_sha256")
        if not isinstance(result_grid, str):
            raise ValueError("AI stage requires the issued result grid content digest")
        Digest(result_grid)
        if (
            not value["evidence_required"]
            or evidence is None
            or instant(evidence) != cutoff
            or any(
                pinned.get(key) != value[key]
                for key in ("baseline_snapshot_id", "prepared_snapshot_id", "analysis_cutoff")
            )
            or pinned.get("corrected_stage_id") != value["parent_stage_id"]
            or value.get("validation", {}).get("status") != "valid"
        ):
            raise ValueError("AI stage must retain validated pinned evidence and corrected parent")
        context_digest = value.get("context_digest")
        if not isinstance(context_digest, str):
            raise ValueError("AI stage requires immutable context identity")
        Digest(context_digest)
        # Runtime meteorological evidence is not learned-policy training data.
        # A versioned desk/code contract is retained without fabricating its release time.
        if (
            value.get("policy_created_at") is not None
            or value.get("policy_activated_at") is not None
        ):
            raise ValueError("Runtime desk policy must not fabricate training/activation times")
    if evidence is not None and instant(evidence) > cutoff:
        raise ValueError("Variant training evidence follows analysis cutoff")
    if value["evidence_required"] and evidence is None:
        raise ValueError("Learning evidence cutoff is unproven")
    created, activated = value.get("policy_created_at"), value.get("policy_activated_at")
    if value["evidence_required"] and not runtime_ai and created is None:
        raise ValueError("Learning policy creation time is unproven")
    if (
        value["evidence_required"]
        and not runtime_ai
        and value["lifecycle_role"] in {"active", "shadow"}
        and activated is None
    ):
        raise ValueError("Executed learning policy activation time is unproven")
    if created is not None:
        if instant(created) > cutoff or (
            evidence is not None and instant(evidence) > instant(created)
        ):
            raise ValueError("Policy creation/evidence follows its permitted cutoff")
    if activated is not None and (
        created is None or not instant(created) <= instant(activated) <= cutoff
    ):
        raise ValueError("Policy activation must follow creation and precede analysis")
    if (
        value["transformation_type"] == "candidate_blend"
        or value["transformation_type"] == "ai_adjusted"
        and not runtime_ai
    ) and value["lifecycle_role"] == "active":
        raise ValueError(
            "Cannot activate a candidate or an AI variant without pinned runtime proof"
        )
    if not isinstance(value.get("code_identity"), dict) or not value["code_identity"]:
        raise ValueError("Variant requires code/config identity and explicit overlay semantics")


def seal_variant(body: dict[str, Any]) -> dict[str, Any]:
    """Copy JSON data and bind its complete lineage/recipe to a content identity."""
    value: dict[str, Any] = json.loads(
        canonical_json_bytes({**body, "schema_version": VARIANT_SCHEMA})
    )
    value.pop("variant_id", None)
    value["variant_id"] = str(Digest.of_bytes(canonical_json_bytes(value)))
    validate_variant(value)
    return value
