"""Immutable stage identity and truthful learning cutoffs, without policy promotion."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from mesoforge.common.identifiers import Digest
from mesoforge.contracts.forecast_variants import seal_variant, validate_variant

TEMPERATURE = "air_temperature_2m"
QPF = "liquid_equivalent_precipitation_amount_1h"


def body(**changes: Any) -> dict[str, Any]:
    value = {
        "parent_stage_id": str(Digest.of_bytes(b"parent-stage")),
        "transformation_type": "deterministic_corrected",
        "lifecycle_role": "shadow",
        "baseline_snapshot_id": "baseline-one",
        "prepared_snapshot_id": "prepared-one",
        "fields": [TEMPERATURE, QPF],
        "policy": {
            "id": "test-only-policy",
            "version": "1",
            "digest": str(Digest.of_bytes(b"policy")),
        },
        "location": {"latitude": 44.98861, "longitude": -93.25553},
        "reference_time": "2026-09-25T17:00:00Z",
        "analysis_cutoff": "2026-09-25T17:10:00Z",
        "evidence_cutoff": "2026-09-24T00:00:00Z",
        "evidence_required": True,
        "evidence_status": "applied",
        "policy_created_at": "2026-09-24T01:00:00Z",
        "policy_activated_at": "2026-09-24T02:00:00Z",
        "created_at": "2026-09-25T17:12:00Z",
        "code_identity": {"source_sha256": str(Digest.of_bytes(b"test-code"))},
        "overlay": {
            "inherit_unchanged": True,
            "predictions": [
                {
                    "field": TEMPERATURE,
                    "valid_time": "2026-09-25T18:00:00Z",
                    "value": 290.0,
                    "unit": "K",
                }
            ],
        },
    }
    value.update(changes)
    return value


def test_identity_binds_parent_policy_fields_and_cutoff_without_mutating_input() -> None:
    original = body()
    untouched = deepcopy(original)
    sealed = seal_variant(original)
    assert original == untouched
    validate_variant(sealed)
    assert seal_variant(original) == sealed
    for key, value in (
        ("parent_stage_id", str(Digest.of_bytes(b"another-parent"))),
        ("fields", [QPF]),
    ):
        changed = deepcopy(sealed)
        changed[key] = value
        with pytest.raises(ValueError, match="immutable identity"):
            validate_variant(changed)
    changed = deepcopy(sealed)
    changed["policy"]["version"] = "2"
    with pytest.raises(ValueError, match="immutable identity"):
        validate_variant(changed)
    changed = deepcopy(sealed)
    changed["analysis_cutoff"] = "2026-09-25T17:11:00Z"
    with pytest.raises(ValueError, match="immutable identity"):
        validate_variant(changed)


def test_output_creation_can_follow_cutoff_but_policy_and_evidence_cannot() -> None:
    assert seal_variant(body())["created_at"] == "2026-09-25T17:12:00Z"
    for key in ("evidence_cutoff", "policy_created_at", "policy_activated_at"):
        with pytest.raises(ValueError):
            seal_variant(body(**{key: "2026-09-25T18:00:00Z"}))
    with pytest.raises(ValueError, match="creation/evidence"):
        seal_variant(body(evidence_cutoff="2026-09-24T01:30:00Z"))
    with pytest.raises(ValueError, match="activation"):
        seal_variant(body(policy_activated_at="2026-09-24T00:30:00Z"))


@pytest.mark.parametrize("missing", ["evidence_cutoff", "policy_created_at", "policy_activated_at"])
def test_executable_learning_policy_requires_proven_timing(missing: str) -> None:
    with pytest.raises(ValueError, match="unproven"):
        seal_variant(body(**{missing: None}))


def test_no_policy_and_failure_fallback_are_explicit_unchanged_stages() -> None:
    for status in (
        "no_policy",
        "insufficient_evidence",
        "fallback",
        "active_storage_failed_fallback",
    ):
        value = body(
            lifecycle_role="active",
            evidence_required=False,
            evidence_status=status,
            evidence_cutoff=None,
            policy_created_at=None,
            policy_activated_at=None,
            overlay={
                "inherit_unchanged": True,
                "predictions": [],
                "correction": {
                    "status": status,
                    "changes": [],
                    "applied_delta_k": 0.0,
                },
            },
        )
        validate_variant(seal_variant(value))
        value["overlay"]["predictions"] = body()["overlay"]["predictions"]
        with pytest.raises(ValueError, match="unchanged correction"):
            seal_variant(value)
    with pytest.raises(ValueError, match="unchanged correction"):
        seal_variant(body(transformation_type="candidate_blend", evidence_required=False))


def test_baseline_is_saved_numerical_root_not_an_unexplained_learned_policy() -> None:
    raw = body(
        parent_stage_id=None,
        transformation_type="active_baseline",
        lifecycle_role="active",
        evidence_required=False,
        evidence_status="baseline",
        evidence_cutoff=None,
        policy_created_at=None,
        policy_activated_at=None,
    )
    raw["overlay"]["inherit_unchanged"] = False
    validate_variant(seal_variant(raw))
    raw["overlay"]["inherit_unchanged"] = True
    with pytest.raises(ValueError, match="explicit saved predictions"):
        seal_variant(raw)


@pytest.mark.parametrize(
    "change",
    [
        {"fields": TEMPERATURE},
        {"fields": [TEMPERATURE, TEMPERATURE]},
        {"fields": [42]},
        {"baseline_snapshot_id": True},
        {"prepared_snapshot_id": ["prepared"]},
        {"policy": {"id": "policy", "version": 1}},
        {"code_identity": "unstructured"},
        {"evidence_required": "false"},
    ],
)
def test_structural_identity_cannot_be_substituted_with_truthy_values(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        seal_variant(body(**change))


def test_prediction_field_event_units_and_missingness_remain_explicit() -> None:
    value = body()
    value["overlay"]["predictions"].append(
        {
            "field": QPF,
            "valid_time": "2026-09-25T18:00:00Z",
            "value": None,
            "unit": "mm",
            "interval_start": "2026-09-25T17:00:00Z",
            "interval_end": "2026-09-25T18:00:00Z",
        }
    )
    validate_variant(seal_variant(value))
    for key, bad in (
        ("field", "another-field"),
        ("unit", ""),
        ("value", True),
        ("interval_start", "2026-09-25T19:00:00Z"),
    ):
        changed = deepcopy(value)
        changed["overlay"]["predictions"][1][key] = bad
        with pytest.raises(ValueError):
            seal_variant(changed)
    value["overlay"]["predictions"].append(deepcopy(value["overlay"]["predictions"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        seal_variant(value)


def test_future_ai_identity_can_be_retained_in_shadow_without_execution_or_promotion() -> None:
    synthetic = body(transformation_type="ai_adjusted")
    validate_variant(seal_variant(synthetic))
    synthetic["lifecycle_role"] = "active"
    with pytest.raises(ValueError, match="does not activate"):
        seal_variant(synthetic)
