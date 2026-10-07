"""Compact learning stages on the existing artifact store; never automatic promotion.

Policies/overlays/stages are artifacts, not a second forecast-history system.
Operational issuance remains the authority binding stages to an issued version.
Which persistent policy executes is resolved by application.governance from
append-only events at a pinned decision time; payload lifecycle roles never select.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from mesoforge.application.artifacts import TransformationInputRef, TransformationRequest
from mesoforge.application.issued_qpf_verification import IssuedQpfVerificationService
from mesoforge.application.site_verification_analysis import analyze_site_verification
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.forecast_variants import (
    instant,
    seal_variant,
    validate_variant,
)
from mesoforge.contracts.policy_governance import ResolvedPolicy, ScopeResolution
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.coherence import DEW_POINT, QPF, RH, TEMPERATURE
from mesoforge.storage.json import CanonicalJsonSerializer

_JSON = CanonicalJsonSerializer()
_KINDS = frozenset(
    {
        "forecast-variant",
        "learning-policy",
        "learning-overlay",
        "learning-binding",
        "governance-evaluation",
    }
)


def _utc() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return instant(value).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return str(Digest.of_bytes(canonical_json_bytes(value)))


class LearningService:
    """Shared artifact/lineage boundary for every deterministic or future shadow stage."""

    def __init__(
        self, storage: IssuedQpfVerificationService, *, clock: Callable[[], datetime] = _utc
    ) -> None:
        self.storage = storage
        self.clock = clock
        self.identity = {
            **storage.identity,
            "learning_sources": {
                name: hashlib.sha256((Path(__file__).parents[1] / name).read_bytes()).hexdigest()
                for name in (
                    "application/learning.py",
                    "contracts/forecast_variants.py",
                    "forecasting/field_blend.py",
                    "forecasting/coherence.py",
                    "application/corrections.py",
                    "application/candidate_baseline.py",
                    "forecasting/candidate_policy.py",
                    "verification/variant_evaluation.py",
                    "application/site_verification_analysis.py",
                    "verification/site_analysis.py",
                    "verification/analytical_attributes.py",
                    "verification/model_comparison.py",
                    "application/forecast_desk.py",
                    "application/forecast_desk_context.py",
                    "application/forecast_desk_provider.py",
                    "contracts/forecast_desk.py",
                    "forecasting/field_edit.py",
                    "application/governance.py",
                    "contracts/policy_governance.py",
                    "verification/governance_eligibility.py",
                )
            },
        }

    def read(self, identifier: ArtifactId) -> dict[str, Any]:
        manifest, raw = self.storage.artifacts.load_verified_payload(identifier)
        if manifest.artifact_type not in _KINDS:
            raise ValueError("Not a learning artifact")
        payload = json.loads(raw)
        if manifest.artifact_type == "forecast-variant":
            validate_variant(payload)
        return {
            "artifact_id": str(manifest.artifact_id),
            "content_digest": str(manifest.content_digest),
            "byte_size": manifest.byte_size,
            "registered_at": _iso(manifest.registered_at),
            "available_at": _iso(manifest.availability.available_at),
            "payload": payload,
        }

    def find(self, kind: str, attributes: dict[str, object]) -> list[dict[str, Any]]:
        with self.storage.factory() as uow:
            rows = uow.artifacts.find_learning_artifacts(kind, attributes=attributes)
        return [self.read(row.artifact_id) for row in rows]

    def save(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        inputs: tuple[ArtifactId, ...] = (),
        attributes: dict[str, object] | None = None,
        identity_key: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Existing advisory-lock/content-addressed transaction supplies immutability."""
        if kind not in _KINDS:
            raise ValueError("Unsupported learning artifact type")
        if kind == "forecast-variant":
            validate_variant(payload)
        configuration = self.storage.configuration()
        request = TransformationRequest(
            activity_type="retain-learning-stage",
            activity_version="v1",
            inputs=tuple(
                TransformationInputRef(role=f"parent-{index}", artifact_id=value)
                for index, value in enumerate(inputs)
            ),
            output_role="learning",
            output_artifact_type=kind,
            output_artifact_schema_version=payload["schema_version"],
            output_media_type="application/json",
            parameters={"identity": identity_key or {"content": _digest(payload)}, "kind": kind},
            configuration_snapshot_id=configuration.configuration_snapshot_id,
            configuration_digest=configuration.configuration_digest,
            code_revision=self.identity["git_commit"],
            environment_digest=Digest.of_bytes(canonical_json_bytes(self.identity)),
            attributes=attributes,
        )
        called = False

        def transform(*parents: bytes) -> dict[str, Any]:
            nonlocal called
            called = True
            return payload

        def validate(value: dict[str, Any]) -> None:
            if kind == "forecast-variant":
                validate_variant(value)
            elif not isinstance(value.get("schema_version"), str):
                raise ValueError("Learning artifact needs an explicit schema")

        result = self.storage.artifacts.execute_raw_transformation(
            request, transform, _JSON, input_loader=bytes, output_validator=validate
        )
        return {**self.read(result.output.artifact_id), "already_existing": not called}

    def register_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        from mesoforge.application.corrections import validate_temperature_policy
        from mesoforge.forecasting.candidate_policy import CandidateBlendPolicy

        if payload.get("schema_version") == "mesoforge.temperature-correction-policy.v1":
            validate_temperature_policy(payload)
            if payload["lifecycle_role"] == "insufficient_evidence":
                raise ValueError("An insufficient-evidence report is not a candidate policy")
        elif payload.get("schema_version") == "mesoforge.forecast-desk-version.v1":
            from mesoforge.application.governance import validate_desk_version

            validate_desk_version(payload)
        else:
            CandidateBlendPolicy.model_validate_json(canonical_json_bytes(payload))
        # Version is a scientific identity, not an overwrite slot. Also inspect older
        # registrations made under a different code/config execution identity.
        key = {"policy_id": payload["policy_id"], "version": payload["version"]}
        with self.storage.artifacts.acquire_identity(
            Digest.of_bytes(canonical_json_bytes({"learning_policy_identity": key}))
        ):
            existing = self.find("learning-policy", key)
            if existing:
                if any(row["payload"] != payload for row in existing):
                    raise ValueError(
                        "Policy ID/version already identifies a different immutable policy"
                    )
                return {**existing[0], "already_existing": True}
            saved = self.save(
                "learning-policy",
                payload,
                attributes={
                    "policy_id": payload["policy_id"],
                    "version": payload["version"],
                    "lifecycle_role": payload["lifecycle_role"],
                },
                identity_key=key,
            )
            if saved["payload"] != payload:
                raise ValueError(
                    "Concurrent policy registration conflicts with immutable ID/version"
                )
            return saved

    def evidence(self, latitude: float, longitude: float, cutoff: datetime) -> dict[str, Any]:
        return analyze_site_verification(
            latitude,
            longitude,
            now=self.clock(),
            as_of=cutoff,
            raw_baseline_only=True,
            unit_of_work_factory=self.storage.factory,
            load_payload=lambda manifest: self.storage.artifacts.load_verified_payload(
                manifest.artifact_id
            )[1],
        )

    def _stage(
        self,
        forecast: dict[str, Any],
        *,
        parent: dict[str, Any] | None,
        transformation: str,
        role: str,
        policy: dict[str, Any] | None,
        overlay: dict[str, Any],
        status: str,
        policy_reference: dict[str, Any] | None = None,
        activated_at: datetime | None = None,
        governance_resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        lineage = forecast["baseline_snapshot"]
        governed = (
            {"governance_resolution": governance_resolution}
            if governance_resolution is not None
            else {}
        )
        return seal_variant(
            {
                **governed,
                "parent_stage_id": parent["variant_id"] if parent else None,
                "transformation_type": transformation,
                "lifecycle_role": role,
                "fields": sorted({TEMPERATURE, QPF, *overlay.get("affected_fields", [])}),
                "affected_fields": overlay.get("affected_fields", []),
                "policy": {
                    "id": policy["policy_id"]
                    if policy
                    else "active-field-policies"
                    if parent is None
                    else "no-policy",
                    "version": policy["version"] if policy else "1",
                    "digest": _digest(policy) if policy else None,
                },
                "policy_reference": policy_reference,
                "baseline_snapshot_id": lineage["baseline_snapshot_id"],
                "prepared_snapshot_id": lineage["prepared_snapshot_id"],
                "parent_grid_sha256": forecast["local_grid"]["sha256"],
                "location": {"latitude": forecast["latitude"], "longitude": forecast["longitude"]},
                "reference_time": forecast["target_reference_time"],
                "analysis_cutoff": lineage["forecast_analysis_cutoff"],
                "evidence_cutoff": policy.get("evidence_cutoff") if policy else None,
                "evidence_required": policy is not None,
                "evidence_status": status,
                "policy_created_at": policy.get("created_at") if policy else None,
                "policy_activated_at": (
                    _iso(activated_at)
                    if policy and activated_at is not None
                    else policy.get("activated_at")
                    if policy
                    else None
                ),
                "created_at": _iso(self.clock()),
                "code_identity": self.identity,
                "field_policies": lineage["field_policies"],
                "overlay": overlay,
            }
        )

    def local_stage(
        self, forecast: dict[str, Any], *, governance: ScopeResolution | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """An explicit stage always exists; optional learning/storage failures are isolated.

        ``governance`` is the committed state of this coordinate's correction scope at
        the batch decision time. None means no policy may execute (no active policy and
        no shadows). A resolved ACTIVE policy executes under its operational grant;
        registered candidates execute only as shadows under their registration grant.
        Site evidence is read-only here: normal forecasts do not propose or persist
        new policies. Operators retain the separate proposal/registration interfaces.
        """
        from mesoforge.application.corrections import apply_temperature_correction
        from mesoforge.contracts.policy_governance import GovernanceBlockedError

        started = time.perf_counter()
        cutoff = instant(forecast["baseline_snapshot"]["forecast_analysis_cutoff"])
        report: dict[str, Any] = {
            "status": "no_policy",
            "shadows": [],
            "shadow_attempts": [],
            "failures": [],
            "candidate_status": "not_generated",
            "policy_generation": "operator_only",
        }
        if governance is not None:
            if governance.error is not None:
                raise GovernanceBlockedError("governance_unavailable", governance.error)
            if instant(governance.decision_time) != cutoff:
                raise GovernanceBlockedError(
                    "governance_decision_time_mismatch",
                    "Governance was resolved for a different decision time",
                )
            report["governance"] = governance.summary()
        try:
            analysis = self.evidence(forecast["latitude"], forecast["longitude"], cutoff)
            report["evidence"] = analysis["correction_readiness"]
            report["desk_evidence"] = {
                key: analysis[key]
                for key in ("coordinate", "evaluation", "evidence_policy", "correction_readiness")
            }
        except Exception as exc:
            report["failures"].append({"phase": "evidence_update", "reason": str(exc)})
        active = governance.active if governance is not None else None
        resolution = (
            governance.record("resolved_active" if active else "resolved_none", active)
            if governance is not None
            else None
        )
        corrected, outcome = apply_temperature_correction(
            forecast,
            active.payload if active is not None else None,
            analysis_cutoff=cutoff,
            mode="operational",
            grant=active.grant("operational") if active is not None else None,
        )
        if outcome["status"] not in {"applied", "no_op", "no_policy", "fallback"}:
            # A resolved ACTIVE policy that cannot execute is a recorded no-op, never
            # a silent substitution or a partially applied stage.
            corrected = forecast
            outcome = {
                **outcome,
                "status": "fallback",
                "reason": f"governed_policy_not_executable:{outcome['status']}",
                "changes": [],
                "applied_delta_k": 0.0,
            }
        report["correction"] = outcome
        report["status"] = outcome["status"]
        control = self._stage(
            forecast,
            parent=None,
            transformation="active_baseline",
            role="active",
            policy=None,
            overlay={"inherit_unchanged": False, "predictions": self._point_values(forecast)},
            status="baseline",
        )
        # An invalid ACTIVE transformation is a recorded no-op, never a partial stage.
        used_policy = (
            active.payload
            if active is not None and outcome["status"] in {"applied", "no_op"}
            else None
        )
        stage = self._stage(
            forecast,
            parent=control,
            transformation="deterministic_corrected",
            role="active",
            policy=used_policy,
            overlay=self._correction_overlay(corrected, outcome),
            status=outcome["status"],
            policy_reference=active.reference if active is not None else None,
            activated_at=active.event.recorded_at if active is not None else None,
            governance_resolution=resolution,
        )
        report["control_stage"] = control
        report["operational_stage"] = stage
        try:
            control_ref = self.save(
                "forecast-variant", control, attributes=self._attributes(control)
            )
            report["control_reference"] = self._reference(control_ref)
            stage_ref = self.save(
                "forecast-variant",
                stage,
                inputs=(ArtifactId(control_ref["artifact_id"]),),
                attributes=self._attributes(stage),
            )
            report["operational_reference"] = self._reference(stage_ref)
        except Exception as exc:
            report["failures"].append({"phase": "stage_storage", "reason": str(exc)})
            if corrected is not forecast:
                corrected = forecast
                report["status"] = "active_storage_failed_fallback"
                report["correction"] = {
                    "status": "fallback",
                    "applied_delta_k": 0.0,
                    "changes": [],
                    "reason": str(exc),
                }
                report["operational_stage"] = self._stage(
                    forecast,
                    parent=control,
                    transformation="deterministic_corrected",
                    role="active",
                    policy=None,
                    overlay={
                        "inherit_unchanged": True,
                        "predictions": [],
                        "correction": report["correction"],
                    },
                    status="active_storage_failed_fallback",
                    policy_reference=active.reference if active is not None else None,
                    governance_resolution=resolution,
                )
                report.pop("operational_reference", None)
        if governance is not None:
            report["shadow_attempts"].extend(dict(row) for row in governance.shadow_failures)
            for candidate in governance.shadows:
                attempt = self._shadow(forecast, candidate, control, governance, cutoff, report)
                report["shadow_attempts"].append(attempt)
        report["seconds"] = time.perf_counter() - started
        # Compact lineage only: the unchanged numerical grid is shared, not copied.
        return {
            **corrected,
            "learning_stage": report["operational_stage"],
            "learning_reference": report.get("operational_reference"),
        }, report

    def _shadow(
        self,
        forecast: dict[str, Any],
        candidate: ResolvedPolicy,
        control: dict[str, Any],
        governance: ScopeResolution,
        cutoff: datetime,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """One registered candidate on the raw parent; its outcome is always recorded."""
        from mesoforge.application.corrections import apply_temperature_correction

        attempt: dict[str, Any] = {
            "policy_artifact_id": str(candidate.policy_artifact_id),
            "registration_event_id": str(candidate.registration.event_id),
        }
        try:
            shadow, outcome = apply_temperature_correction(
                forecast,
                candidate.payload,
                analysis_cutoff=cutoff,
                mode="shadow",
                grant=candidate.grant("shadow"),
            )
            if outcome["status"] not in {"applied", "no_op"}:
                raise ValueError(f"Shadow correction failed: {outcome.get('reason', outcome)}")
            overlay = self._correction_overlay(shadow, outcome)
            # Shadows inherit the raw parent, never a potentially corrected issuance.
            overlay.update(inherit_unchanged=False, predictions=self._point_values(shadow))
            variant = self._stage(
                forecast,
                parent=control,
                transformation="deterministic_corrected",
                role="shadow",
                policy=candidate.payload,
                overlay=overlay,
                status=outcome["status"],
                policy_reference=candidate.reference,
                activated_at=candidate.event.recorded_at,
                governance_resolution=governance.record("candidate_shadow", candidate),
            )
        except Exception as exc:
            report["failures"].append({"phase": "correction_shadow", "reason": str(exc)})
            return {**attempt, "status": "candidate_failed", "reason": str(exc)}
        try:
            retained = self.save("forecast-variant", variant, attributes=self._attributes(variant))
        except Exception as exc:
            report["failures"].append({"phase": "correction_shadow_storage", "reason": str(exc)})
            return {**attempt, "status": "storage_failed", "reason": str(exc)}
        report["shadows"].append(self._reference(retained))
        return {**attempt, "status": "stored", "variant_id": variant["variant_id"]}

    @staticmethod
    def _reference(saved: dict[str, Any]) -> dict[str, Any]:
        return {
            k: saved[k]
            for k in ("artifact_id", "content_digest", "byte_size", "registered_at", "available_at")
            if k in saved
        }

    @staticmethod
    def _attributes(stage: dict[str, Any]) -> dict[str, object]:
        return {
            **stage["location"],
            "baseline_snapshot_id": stage["baseline_snapshot_id"],
            "reference_time": stage["reference_time"],
            "variant_id": stage["variant_id"],
            "transformation_type": stage["transformation_type"],
            "policy_id": stage["policy"]["id"],
        }

    @staticmethod
    def _point_values(forecast: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for hour in forecast["hours"]:
            rows.append(
                {
                    "field": TEMPERATURE,
                    "valid_time": hour["valid_time"],
                    "value": hour["temperature"]["value"],
                    "unit": "K",
                }
            )
            qpf = hour.get("surface", {}).get("fields", {}).get(QPF)
            if qpf is not None:
                if qpf["unit"] not in {"kg/m^2", "mm"}:
                    raise ValueError("Unsupported QPF amount unit for learning evaluation")
                rows.append(
                    {
                        "field": QPF,
                        "valid_time": hour["valid_time"],
                        "value": qpf["value"],
                        "unit": "mm",
                        "native_unit": qpf["unit"],
                        "interval_start": qpf.get("interval_start"),
                        "interval_end": qpf.get("interval_end"),
                        "interval_closure": qpf.get("interval_closure"),
                        "temporal_semantics": qpf.get("temporal_semantics"),
                        "policy": qpf.get("policy"),
                        "missing_reasons": qpf.get("missing_reasons", []),
                        "status": qpf.get("status"),
                    }
                )
        return rows

    @staticmethod
    def _correction_overlay(forecast: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        predictions = []
        if outcome.get("changes"):
            predictions = [
                {
                    "field": TEMPERATURE,
                    "valid_time": hour["valid_time"],
                    "value": hour["temperature"]["value"],
                    "unit": "K",
                }
                for hour in forecast["hours"]
            ]
        return {
            "inherit_unchanged": True,
            "inheritance_basis": "parent_stage",
            "predictions": predictions,
            "correction": outcome,
            "affected_fields": [TEMPERATURE, DEW_POINT, RH] if outcome.get("changes") else [],
        }

    def ai_stage(
        self,
        corrected: dict[str, Any],
        final: dict[str, Any],
        desk: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """Retain runtime AI through the shared stage store, before operational issuance.

        No promotion or new policy registration occurs. A failed retention raises to
        the caller, which must retain the complete corrected forecast instead.
        """
        parent = report["operational_stage"]
        if parent["transformation_type"] != "deterministic_corrected":
            raise ValueError("AI desk requires the deterministic corrected stage as parent")
        parent_reference = report["operational_reference"]
        lineage = corrected["baseline_snapshot"]
        from mesoforge.forecasting.field_edit import (
            grid_values_digest,
            validate_edit_scope,
            validate_grid,
        )

        pin = desk.get("pinned_evidence", {})
        if not desk.get("usage", {}).get("validated_actions"):
            raise ValueError("An AI stage requires at least one validated provider action")
        if (
            desk.get("validation", {}).get("status") != "valid"
            or any(
                pin.get(key) != lineage[key]
                for key in ("baseline_snapshot_id", "prepared_snapshot_id")
            )
            or instant(pin.get("analysis_cutoff")) != instant(parent["analysis_cutoff"])
            or pin.get("corrected_stage_id") != parent["variant_id"]
            or pin.get("parent_grid_sha256") != corrected["local_grid"]["sha256"]
            or any(
                final[key] != corrected[key]
                for key in ("latitude", "longitude", "target_reference_time", "baseline_snapshot")
            )
            or [h["valid_time"] for h in final["hours"]]
            != [h["valid_time"] for h in corrected["hours"]]
        ):
            raise ValueError("AI stage evidence differs from its pinned corrected parent")
        validate_grid(final["local_grid_baseline"])
        grid = final["local_grid_baseline"]
        if grid["geometry"] != corrected["local_grid_baseline"]["geometry"]:
            raise ValueError("AI final grid cannot change the pinned geometry")
        target = grid["geometry"]["point_target"]
        center = [
            cell
            for cell in grid["cells"]
            if (cell["x_index"], cell["y_index"]) == (target["x_index"], target["y_index"])
        ]
        if len(center) != 1 or final["hours"] != center[0]["hours"]:
            raise ValueError("AI issued point must equal its saved grid center")
        # Replayable lineage: checkpoint 0 is the corrected parent, each ordered recipe
        # consumes the previous validated values, and the last output is the issued grid.
        # The full-grid content digest was computed by the bounded final extraction of
        # this same object; only its unchanged/changed relationship is rechecked here.
        recipes = desk.get("accepted_recipes")
        checkpoints = desk.get("checkpoints")
        if not isinstance(recipes, list) or not isinstance(checkpoints, list):
            raise ValueError("AI stage requires ordered recipes and validated checkpoints")
        chain = [grid_values_digest(corrected["local_grid_baseline"])]
        for recipe in recipes:
            if recipe.get("input_values_sha256") != chain[-1]:
                raise ValueError("AI recipes do not consume the previous validated checkpoint")
            chain.append(recipe.get("output_values_sha256"))
        if [checkpoint.get("values_digest") for checkpoint in checkpoints] != chain or chain[
            -1
        ] != grid_values_digest(grid):
            raise ValueError("AI final grid differs from the latest validated checkpoint")
        # Only recipe-selected editable cell-hours and their coherence dependents differ.
        validate_edit_scope(corrected["local_grid_baseline"], grid, recipes)
        unchanged = grid is corrected["local_grid_baseline"]
        if unchanged != (not recipes) or (
            (final["local_grid"]["sha256"] == corrected["local_grid"]["sha256"]) != unchanged
        ):
            raise ValueError("AI grid content differs from its retained content identity")
        desk = {
            **desk,
            "point_values": [
                {
                    "valid_time": before["valid_time"],
                    "corrected_temperature": before["temperature"],
                    "final_temperature": after["temperature"],
                    "applied_delta_k": (
                        after["temperature"]["value"] - before["temperature"]["value"]
                        if before["temperature"]["value"] is not None
                        and after["temperature"]["value"] is not None
                        else None
                    ),
                }
                for before, after in zip(corrected["hours"], final["hours"], strict=True)
            ],
        }
        from mesoforge.application.forecast_desk import desk_policy_identity, desk_summary

        policy = desk_policy_identity(
            provider=desk["provider"],
            model=desk["model"],
            reasoning_effort=desk.get("inference_settings", {}).get("reasoning_effort"),
        )
        stage = seal_variant(
            {
                "parent_stage_id": parent["variant_id"],
                "transformation_type": "ai_adjusted",
                "lifecycle_role": "active",
                "fields": sorted({TEMPERATURE, QPF, *desk.get("affected_fields", [])}),
                "affected_fields": desk.get("affected_fields", []),
                "policy": policy,
                "baseline_snapshot_id": lineage["baseline_snapshot_id"],
                "prepared_snapshot_id": lineage["prepared_snapshot_id"],
                "parent_grid_sha256": corrected["local_grid"]["sha256"],
                "location": parent["location"],
                "reference_time": parent["reference_time"],
                "analysis_cutoff": parent["analysis_cutoff"],
                "evidence_cutoff": parent["analysis_cutoff"],
                "evidence_required": True,
                "evidence_basis": "pinned_forecast_evidence",
                "evidence_status": "pinned",
                "policy_created_at": None,
                "policy_activated_at": None,
                "created_at": _iso(self.clock()),
                "code_identity": self.identity,
                "field_policies": lineage["field_policies"],
                "context_digest": desk["context_digest"],
                "pinned_evidence": {
                    "baseline_snapshot_id": lineage["baseline_snapshot_id"],
                    "prepared_snapshot_id": lineage["prepared_snapshot_id"],
                    "analysis_cutoff": parent["analysis_cutoff"],
                    "corrected_stage_id": parent["variant_id"],
                },
                "validation": desk["validation"],
                "overlay": {
                    "inherit_unchanged": False,
                    "predictions": self._point_values(final),
                    "desk": desk,
                    "result_grid_sha256": final["local_grid"]["sha256"],
                },
            }
        )
        retained = self.save(
            "forecast-variant",
            stage,
            inputs=(ArtifactId(parent_reference["artifact_id"]),),
            attributes=self._attributes(stage),
        )
        # The shared variant seal normalizes tuples/datetimes to canonical JSON.
        # Issuance and immediate readback must expose that exact same representation.
        desk = stage["overlay"]["desk"]
        report["corrected_stage"] = parent
        report["corrected_reference"] = parent_reference
        report["operational_stage"] = stage
        report["operational_reference"] = self._reference(retained)
        report["ai"] = desk
        return {
            **final,
            "baseline_stage": report["control_stage"],
            "baseline_stage_reference": report["control_reference"],
            "deterministic_stage": parent,
            "deterministic_reference": parent_reference,
            "learning_stage": stage,
            "learning_reference": report["operational_reference"],
            "ai_desk": desk_summary(desk),
        }

    def bind(self, issued: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        if not all(key in report for key in ("control_reference", "operational_reference")):
            raise ValueError(
                "Cannot bind an incomplete learning stage; operational issuance is preserved"
            )
        control = self.read(ArtifactId(report["control_reference"]["artifact_id"]))["payload"]
        operational = self.read(ArtifactId(report["operational_reference"]["artifact_id"]))[
            "payload"
        ]
        corrected = (
            self.read(ArtifactId(report["corrected_reference"]["artifact_id"]))["payload"]
            if "corrected_reference" in report
            else operational
        )
        if (
            control["transformation_type"] != "active_baseline"
            or corrected["transformation_type"] != "deterministic_corrected"
            or corrected["parent_stage_id"] != control["variant_id"]
            or (
                operational is not corrected
                and (
                    operational["transformation_type"] != "ai_adjusted"
                    or operational["parent_stage_id"] != corrected["variant_id"]
                )
            )
            or operational["lifecycle_role"] != "active"
            or any(
                stage[key] != control[key]
                for stage in (corrected, operational)
                for key in (
                    "baseline_snapshot_id",
                    "prepared_snapshot_id",
                    "location",
                    "reference_time",
                    "analysis_cutoff",
                )
            )
        ):
            raise ValueError("Operational correction must retain its exact raw baseline parent")
        with self.storage.factory() as uow:
            record = uow.issued_forecasts.get(UUID(issued["issued_forecast_id"]))
        if (
            record.latitude != control["location"]["latitude"]
            or record.longitude != control["location"]["longitude"]
            or record.target_reference_time != instant(control["reference_time"])
        ):
            raise ValueError(
                "Learning binding differs from its immutable issued decision/coordinate"
            )
        refs = [
            report[key]
            for key in ("control_reference", "corrected_reference", "operational_reference")
            if key in report
        ]
        refs.extend(report.get("shadows", []))
        # v2 adds every governed shadow attempt, so a candidate is never judged only
        # where it succeeded. Readers accept v1 (no attempts recorded) and v2.
        payload = {
            "schema_version": "mesoforge.learning-issuance-binding.v2",
            "issued_forecast_id": issued["issued_forecast_id"],
            "variants": refs,
            "shadow_attempts": report.get("shadow_attempts", []),
        }
        return self._reference(
            self.save(
                "learning-binding",
                payload,
                inputs=tuple(ArtifactId(ref["artifact_id"]) for ref in refs),
                attributes={"issued_forecast_id": issued["issued_forecast_id"]},
            )
        )

    def _candidate_overlay_key(
        self, pinned: Any, candidate: ResolvedPolicy, analysis_cutoff: datetime
    ) -> tuple[Any, dict[str, str], dict[str, object]]:
        """Validated candidate payload and its idempotent overlay identity for one baseline."""
        from mesoforge.forecasting.candidate_policy import CandidateBlendPolicy

        identifier = candidate.policy_artifact_id
        if (
            candidate.event.event_type != "REGISTERED"
            or candidate.event.recorded_at is None
            or candidate.event.recorded_at > analysis_cutoff
        ):
            raise ValueError("Background execution requires a committed registration")
        saved = self.read(ArtifactId(identifier))
        if max(
            instant(saved["registered_at"]), instant(saved["available_at"])
        ) > analysis_cutoff or saved["content_digest"] != str(candidate.content_digest):
            raise ValueError("Policy artifact was not available before analysis cutoff")
        policy = CandidateBlendPolicy.model_validate_json(canonical_json_bytes(saved["payload"]))
        policy.validate_execution(analysis_cutoff)
        if policy.lifecycle_role != "candidate":
            raise ValueError("Only governed candidate payloads execute as shadows")
        governance = {
            "policy_artifact_id": str(identifier),
            "content_digest": str(candidate.content_digest),
            "registration_event_id": str(candidate.registration.event_id),
            "registered_at": _iso(candidate.event.recorded_at),
        }
        key: dict[str, object] = {
            "baseline_snapshot_id": pinned.manifest["baseline_snapshot_id"],
            "policy_digest": policy.digest,
            "registration_event_id": governance["registration_event_id"],
            "code_identity": _digest(self.identity),
        }
        return policy, governance, key

    def background(
        self,
        pinned: Any,
        candidates: list[ResolvedPolicy],
        *,
        analysis_cutoff: datetime,
    ) -> dict[str, Any]:
        """Governed blend candidates build overlays once; failures cannot publish active.

        ``candidates`` are registered, non-retired, non-active blend candidates resolved
        from committed governance at ``analysis_cutoff``. Payload roles never select.
        """
        from mesoforge.application.candidate_baseline import build_candidate_overlay

        report: dict[str, Any] = {"overlays": [], "failures": []}
        for candidate in candidates:
            identifier = candidate.policy_artifact_id
            try:
                policy, governance, key = self._candidate_overlay_key(
                    pinned, candidate, analysis_cutoff
                )
                existing = self.find("learning-overlay", key)
                if existing:
                    overlay = existing[0]
                else:
                    built = build_candidate_overlay(pinned, policy, analysis_cutoff=analysis_cutoff)
                    overlay = self.save(
                        "learning-overlay",
                        {**built["overlay"], "governance": governance},
                        inputs=(ArtifactId(identifier),),
                        attributes=key,
                        identity_key=key,
                    )
                    report.setdefault("measurements", []).append(built["timings"])
                report["overlays"].append(self._reference(overlay))
            except Exception as exc:
                report["failures"].append({"policy_artifact": str(identifier), "reason": str(exc)})
        return report

    def overlays_for(
        self,
        pinned: Any,
        candidates: list[ResolvedPolicy],
        *,
        analysis_cutoff: datetime,
    ) -> dict[str, Any]:
        """Lookup-only: overlays the background already retained for this baseline.

        Issuance never builds candidate blends. A candidate without a retained overlay
        is reported as missing and simply has no shadow stage for this issuance.
        """
        report: dict[str, Any] = {"overlays": [], "missing": [], "failures": []}
        for candidate in candidates:
            identifier = str(candidate.policy_artifact_id)
            try:
                _, _, key = self._candidate_overlay_key(pinned, candidate, analysis_cutoff)
                existing = self.find("learning-overlay", key)
            except Exception as exc:
                report["failures"].append({"policy_artifact": identifier, "reason": str(exc)})
                continue
            if existing:
                report["overlays"].append(self._reference(existing[0]))
            else:
                report["missing"].append(
                    {"policy_artifact": identifier, "reason": "overlay_missing"}
                )
        return report

    def candidate_stages(
        self, forecast: dict[str, Any], report: dict[str, Any], overlays: list[dict[str, Any]]
    ) -> None:
        """Attach background field patches; this method cannot invoke numerical blending."""
        from mesoforge.application.candidate_baseline import candidate_point_overlay
        from mesoforge.contracts.policy_governance import BLEND_POLICY, blend_scope

        for reference in overlays:
            attempt: dict[str, Any] = {
                "family": BLEND_POLICY,
                "overlay_artifact_id": reference.get("artifact_id"),
            }
            report.setdefault("shadow_attempts", []).append(attempt)
            try:
                retained = self.read(ArtifactId(reference["artifact_id"]))
            except Exception as exc:
                attempt.update(status="storage_failed", reason=str(exc))
                report["failures"].append({"phase": "candidate_projection", "reason": str(exc)})
                continue
            try:
                overlay = retained["payload"]
                governed = overlay.get("governance")
                if isinstance(governed, dict):
                    attempt.update(
                        policy_artifact_id=governed.get("policy_artifact_id"),
                        registration_event_id=governed.get("registration_event_id"),
                    )
                if (
                    overlay["baseline_snapshot_id"]
                    != forecast["baseline_snapshot"]["baseline_snapshot_id"]
                ):
                    raise ValueError("Candidate overlay has a different parent baseline")
                cutoff = instant(forecast["baseline_snapshot"]["forecast_analysis_cutoff"])
                if (
                    max(instant(retained["registered_at"]), instant(retained["available_at"]))
                    > cutoff
                ):
                    raise ValueError("Candidate baseline overlay was not available at analysis")
                if instant(overlay["analysis_cutoff"]) > cutoff:
                    raise ValueError("Candidate baseline analysis follows location analysis cutoff")
                governance = overlay.get("governance")
                if (
                    not isinstance(governance, dict)
                    or instant(governance["registered_at"]) > cutoff
                ):
                    raise ValueError("Candidate overlay lacks a committed governance registration")
                point = candidate_point_overlay(
                    overlay,
                    latitude=forecast["latitude"],
                    longitude=forecast["longitude"],
                    reference_time=instant(forecast["target_reference_time"]),
                )
                predictions = {
                    (p["field"], p["valid_time"]): p for p in self._point_values(forecast)
                }
                for hour in point:
                    for field in (TEMPERATURE, QPF):
                        if field not in hour["fields"]:
                            continue
                        value = hour["fields"][field]
                        row = {
                            "field": field,
                            "valid_time": hour["valid_time"],
                            "value": value["value"],
                            "unit": "K" if field == TEMPERATURE else "mm",
                        }
                        if field == QPF:
                            row.update(
                                interval_start=value["interval_start"],
                                interval_end=value["interval_end"],
                            )
                        predictions[(field, hour["valid_time"])] = row
                stage = self._stage(
                    forecast,
                    parent=report["control_stage"],
                    transformation="candidate_blend",
                    role="shadow",
                    policy=overlay["policy"],
                    overlay={
                        "inherit_unchanged": False,
                        "predictions": list(predictions.values()),
                        "background_overlay": reference,
                        "affected_fields": overlay["affected_fields"],
                    },
                    status="shadow",
                    activated_at=instant(governance["registered_at"]),
                    governance_resolution={
                        "schema_version": "mesoforge.governance-resolution.v1",
                        "family": BLEND_POLICY,
                        "scope_key": blend_scope(overlay["policy"]["field"]),
                        "status": "candidate_shadow",
                        "decision_time": overlay["analysis_cutoff"],
                        **{
                            key: governance[key]
                            for key in (
                                "registration_event_id",
                                "policy_artifact_id",
                                "registered_at",
                            )
                        },
                        "policy_content_digest": governance["content_digest"],
                    },
                )
            except Exception as exc:
                attempt.update(status="candidate_failed", reason=str(exc))
                report["failures"].append({"phase": "candidate_projection", "reason": str(exc)})
                continue
            try:
                saved = self.save(
                    "forecast-variant",
                    stage,
                    inputs=(ArtifactId(reference["artifact_id"]),),
                    attributes=self._attributes(stage),
                )
            except Exception as exc:
                attempt.update(status="storage_failed", reason=str(exc))
                report["failures"].append({"phase": "candidate_storage", "reason": str(exc)})
                continue
            attempt.update(status="stored", variant_id=stage["variant_id"])
            report["shadows"].append(self._reference(saved))

    def stage_issued(self, identifier: UUID) -> dict[str, Any]:
        """Explicit retained-data replay; never rewrites a historical issuance.

        Replay never consults governance: a policy that is active today, or was active
        at the historical decision, must not be bound retroactively to an issuance that
        went out without it. Only the unchanged no-op stage may be retained.
        """
        with self.storage.artifacts.acquire_identity(
            Digest.of_bytes(canonical_json_bytes({"learning_issued_replay": str(identifier)}))
        ):
            return self._stage_issued(identifier)

    def _stage_issued(self, identifier: UUID) -> dict[str, Any]:
        existing = self.find("learning-binding", {"issued_forecast_id": str(identifier)})
        if existing:
            return {"already_existing": True, "binding": existing[0]}
        saved = self.storage.issuer.read(identifier)
        original = saved["forecast"]
        if original.get("learning_stage") is not None:
            raise ValueError(
                "Issued stage already has learning lineage but no binding; "
                "do not reinterpret a potentially corrected forecast as its raw baseline"
            )
        corrected, report = self.local_stage(original, governance=None)
        if corrected["hours"] != original["hours"]:
            raise ValueError("Historical replay would bind a changed forecast; nothing was bound")
        report["historical_issuance_unchanged"] = True
        report["numerical_no_op"] = True
        report["binding"] = self.bind(saved, report)
        return report

    def analyze(
        self,
        field: str,
        latitude: float,
        longitude: float,
        *,
        start: datetime,
        end: datetime,
        variant_ids: list[ArtifactId] | None = None,
    ) -> dict[str, Any]:
        """Read existing facts and compact bound variants; no new verification facts."""
        from mesoforge.verification.variant_evaluation import evaluate_variants

        started = time.perf_counter()
        if instant(end) <= instant(start):
            raise ValueError("Analysis end must follow start")
        if field == TEMPERATURE:
            control = analyze_site_verification(
                latitude,
                longitude,
                unit_of_work_factory=self.storage.factory,
                load_payload=lambda m: self.storage.artifacts.load_verified_payload(m.artifact_id)[
                    1
                ],
            )
            control = {
                **control,
                "samples": [
                    row
                    for row in control["samples"]
                    if instant(start) <= instant(row["valid_time"]) < instant(end)
                ],
            }
        elif field == QPF:
            control = self.storage.analyze_window(
                latitude=latitude,
                longitude=longitude,
                start_valid_time=start,
                end_valid_time=end,
                stages=("final_issued",),
            )
        else:
            raise ValueError("Learning metrics currently support temperature and exact-hour QPF")
        with self.storage.factory() as uow:
            issued = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        bindings: dict[str, list[str]] = {}
        for issuance in issued:
            for row in self.find(
                "learning-binding", {"issued_forecast_id": str(issuance.issued_forecast_id)}
            ):
                for ref in row["payload"]["variants"]:
                    bindings.setdefault(ref["artifact_id"], []).append(
                        str(issuance.issued_forecast_id)
                    )
        chosen = (
            [str(value) for value in variant_ids] if variant_ids is not None else list(bindings)
        )
        variants = []
        for identifier in chosen:
            value = self.read(ArtifactId(identifier))["payload"]
            validate_variant(value)
            variants.append({**value, "control_issued_forecast_ids": bindings.get(identifier, [])})
        ancestors = []
        if variant_ids is not None:
            for identifier in sorted(set(bindings) - set(chosen)):
                value = self.read(ArtifactId(identifier))["payload"]
                validate_variant(value)
                ancestors.append({**value, "control_issued_forecast_ids": bindings[identifier]})
        result = evaluate_variants(field, control, variants, ancestor_stages=ancestors)
        return {
            **result,
            "analysis_seconds": time.perf_counter() - started,
            "control_canonicalization": control["canonicalization"],
            "canonicalization_scope": (
                "all_retained_temperature_history; samples_filtered_to_requested_window"
                if field == TEMPERATURE
                else "requested_qpf_window"
            ),
            "writes": 0,
        }


def configured_learning() -> LearningService:
    from mesoforge.application.issued_qpf_verification import configured_service

    return LearningService(configured_service())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register-policy")
    register.add_argument("--file", type=Path, required=True)
    read = commands.add_parser("read")
    read.add_argument("--artifact-id", type=ArtifactId, required=True)
    stage = commands.add_parser(
        "stage-issued", help="Explicit retained-data stage replay; no forecast rewrite"
    )
    stage.add_argument("--issued-forecast-id", type=UUID, required=True)
    background = commands.add_parser(
        "build-candidate", help="Explicit background build of registered blend candidates"
    )
    background.add_argument("--baseline-root", type=Path, required=True)
    background.add_argument("--policy-id", type=ArtifactId, action="append", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--field", choices=(TEMPERATURE, QPF), required=True)
    analyze.add_argument("--lat", type=float, required=True)
    analyze.add_argument("--lon", type=float, required=True)
    analyze.add_argument("--start", type=datetime.fromisoformat, required=True)
    analyze.add_argument("--end", type=datetime.fromisoformat, required=True)
    analyze.add_argument("--variant-id", type=ArtifactId, action="append")
    args = parser.parse_args(argv)
    try:
        service = configured_learning()
        if args.command == "read":
            result = service.read(args.artifact_id)
        elif args.command == "register-policy":
            result = service.register_policy(json.loads(args.file.read_text(encoding="utf-8")))
        elif args.command == "stage-issued":
            result = service.stage_issued(args.issued_forecast_id)
        elif args.command == "build-candidate":
            from mesoforge.application.baseline_snapshot import load_baseline
            from mesoforge.application.governance import GovernanceService

            cutoff = _utc()
            governed = {
                str(row.policy_artifact_id): row
                for row in GovernanceService(service).blend_candidates(cutoff)
            }
            unknown = sorted(set(map(str, args.policy_id)) - set(governed))
            if unknown:
                raise ValueError(f"Not registered blend candidates at the cutoff: {unknown}")
            result = service.background(
                load_baseline(args.baseline_root),
                [governed[str(identifier)] for identifier in args.policy_id],
                analysis_cutoff=cutoff,
            )
        else:
            result = service.analyze(
                args.field,
                args.lat,
                args.lon,
                start=args.start,
                end=args.end,
                variant_ids=args.variant_id,
            )
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
