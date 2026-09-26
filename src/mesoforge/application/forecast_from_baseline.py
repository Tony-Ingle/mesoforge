"""Configured-location forecasts from one pinned, already blended baseline snapshot.

This path reads the saved MesoForge numerical forecast; it does not load native
guidance, blend fields, discover model providers or prepare model data. Before
issuance, independent prior-temperature/QPF verification may acquire observations.
An uncovered reference
or coordinate requires a new background baseline build, not on-request blending.

Governed persistent policy is read once per batch at the request time from committed
governance events. Issuance never proceeds under an unproven governed state: an
unreadable governance store, a baseline without resolved blend governance, or a
rollback recorded while a location was in flight issues nothing for that scope.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.forecast_from_snapshot import _deliver_locations, _public, _write_outputs
from mesoforge.application.governance import GovernanceService
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.learning import LearningService, configured_learning
from mesoforge.application.prepared_snapshot import SnapshotError, derive_reference_time
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.application.weather_transitions import validate_display_timezone
from mesoforge.common.identifiers import ArtifactId
from mesoforge.contracts.policy_governance import (
    GovernanceBlockedError,
    GovernanceSnapshot,
    GovernanceUnavailableError,
    correction_scope,
)

_ROOT = Path(__file__).resolve().parents[3]


def forecast_from_baseline(
    root: Path,
    locations: list[Any],
    *,
    baseline_pointer: dict[str, Any] | None = None,
    reference_time: datetime | None = None,
    request_time: datetime | None = None,
    display_timezone: str = "UTC",
    issue: bool = False,
    issuer: ForecastIssuanceService | None = None,
    reissue: bool = False,
    run_lock: Callable[[], AbstractContextManager[None]] | None = None,
    verify_prior: bool = True,
    verification_runner: Callable[[float, float], dict[str, Any]] | None = None,
    qpf_lookback_hours: int = 72,
    qpf_max_opportunities: int = 36,
    learning_service: LearningService | None = None,
    learning_overlays: list[dict[str, Any]] | None = None,
    governance: GovernanceService | None = None,
) -> dict[str, Any]:
    """Pin one complete baseline once, then isolate each location's delivery."""
    from mesoforge.application.baseline_snapshot import load_baseline

    validate_display_timezone(display_timezone)
    requested_at = request_time or datetime.now(UTC)
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("Request time must include a timezone")
    requested_at = requested_at.astimezone(UTC)
    derived = derive_reference_time(requested_at)
    if reference_time is not None and (
        reference_time.tzinfo is None or reference_time.utcoffset() is None
    ):
        raise ValueError("Reference time must include a timezone")
    reference = derived if reference_time is None else reference_time.astimezone(UTC)
    if reference.minute or reference.second or reference.microsecond:
        raise ValueError("Reference time must be an exact UTC hour")
    if issue and reference > derived:
        raise ValueError("Issuance cannot use a reference hour after the request hour")
    started = time.perf_counter()
    result: dict[str, Any] = {
        "request_time": requested_at.isoformat().replace("+00:00", "Z"),
        "reference_time": reference.isoformat().replace("+00:00", "Z"),
        "reference_time_source": "request_hour" if reference_time is None else "explicit",
        "network_calls": 0,
        "network_calls_scope": "model_guidance_only; observation attempts reported separately",
        "numerical_blend_execution": "background_baseline_only",
    }
    timings: dict[str, float] = {}
    try:
        pinned = (
            load_baseline(root)
            if baseline_pointer is None
            else load_baseline(root, pointer=baseline_pointer)
        )
        pointer, manifest = pinned.pointer, pinned.manifest
        for label, value in (
            ("baseline analysis cutoff", manifest["analysis_cutoff"]),
            ("baseline build", manifest["built_at"]),
            ("baseline completion", manifest["completed_at"]),
            ("baseline publication", pointer["published_at"]),
        ):
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise SnapshotError(f"{label} lacks a timezone")
            if instant > requested_at:
                raise SnapshotError(f"{label} follows forecast analysis cutoff")
        if manifest["information_cutoff"]["status"] != "proven":
            raise SnapshotError("Baseline input availability is not proven")
        view = pinned.reference_view(reference)
    except (SnapshotError, OSError, KeyError, ValueError) as exc:
        result.update(status="no_current_baseline", reason=str(exc), results=[])
        result["timings"] = {"total_seconds": time.perf_counter() - started}
        return result
    timings["baseline_load_and_verify_seconds"] = time.perf_counter() - started
    prepared = manifest["prepared_snapshot"]
    baseline_lineage = {
        "baseline_snapshot_id": manifest["baseline_snapshot_id"],
        "schema_version": manifest["schema_version"],
        "directory": str(pinned.directory),
        "manifest_sha256": pointer["manifest_sha256"],
        "prepared_snapshot_id": prepared["snapshot_id"],
        "background_analysis_cutoff": manifest["analysis_cutoff"],
        "built_at": manifest["built_at"],
        "completed_at": manifest["completed_at"],
        "published_at": pointer["published_at"],
        "forecast_analysis_cutoff": result["request_time"],
        "reference_time": result["reference_time"],
        "reference_time_source": result["reference_time_source"],
        "field_policies": manifest["field_policies"],
        **(
            {
                "blend_governance": {
                    key: manifest["blend_governance"].get(key)
                    for key in ("status", "decision_time", "heads")
                }
            }
            if isinstance(manifest.get("blend_governance"), dict)
            else {}
        ),
        "information_cutoff": manifest["information_cutoff"],
        "issuance_mode": ("explicit_reissue" if reissue else "primary") if issue else None,
    }
    prepared_lineage = {
        **prepared,
        "request_time": result["request_time"],
        "forecast_analysis_cutoff": result["request_time"],
        "reference_time": result["reference_time"],
        "reference_time_source": result["reference_time_source"],
        "information_evidence": {
            "baseline_snapshot_id": manifest["baseline_snapshot_id"],
            **manifest["information_cutoff"],
        },
        "issuance_mode": baseline_lineage["issuance_mode"],
    }
    result["baseline"] = baseline_lineage
    learning_error: str | None = None
    if learning_service is None:
        try:
            learning_service = configured_learning()
        except Exception as exc:
            learning_error = str(exc)
    snapshot: GovernanceSnapshot | None = None
    clock = time.perf_counter()
    try:
        if learning_service is None:
            raise GovernanceUnavailableError("learning_storage_unavailable", learning_error)
        governance = governance or GovernanceService(learning_service)
        coordinates = []
        for location in locations:
            try:
                coordinate = _coordinates(location)
                validate_coordinate(*coordinate)
            except ValueError:
                continue
            coordinates.append(coordinate)
        snapshot = governance.correction_snapshot(coordinates, requested_at)
        blend = manifest.get("blend_governance")
        if issue and (not isinstance(blend, dict) or blend.get("status") != "resolved"):
            return _not_issued(
                result,
                locations,
                started,
                "baseline_governance_unproven",
                "The pinned baseline has no resolved blend governance; rebuild it",
            )
        if issue and isinstance(blend, dict) and governance.blend_revoked(blend):
            return _not_issued(
                result,
                locations,
                started,
                "baseline_governance_revoked",
                "A blend policy pinned by this baseline was rolled back; rebuild it",
            )
        result["governance"] = {
            "status": "resolved",
            "decision_time": result["request_time"],
            "snapshot_read_at": snapshot.snapshot_read_at.isoformat().replace("+00:00", "Z"),
            "scopes": len(snapshot.scopes),
            "active_scopes": sum(scope.active is not None for scope in snapshot.scopes.values()),
            "blend": {key: blend.get(key) for key in ("status", "decision_time", "heads")}
            if isinstance(blend, dict)
            else {"status": "pre_governance"},
        }
    except GovernanceUnavailableError as exc:
        if issue:
            return _not_issued(result, locations, started, "governance_unavailable", str(exc))
        snapshot = None
        result["governance"] = {"status": "governance_unavailable", "code": exc.code}
    timings["governance_resolution_seconds"] = time.perf_counter() - clock
    verification: dict[int, dict[str, Any]] = {}
    if issue and verify_prior:
        from mesoforge.application.forward_verification import verify_previous_fields

        for index, location in enumerate(locations):
            try:
                lat, lon = _coordinates(location)
                validate_coordinate(lat, lon)
                verification[index] = (
                    verification_runner(lat, lon)
                    if verification_runner is not None
                    else verify_previous_fields(
                        lat,
                        lon,
                        now=requested_at,
                        qpf_lookback_hours=qpf_lookback_hours,
                        qpf_max_opportunities=qpf_max_opportunities,
                    )
                )
            except Exception as exc:
                verification[index] = {"status": "error", "retryable": True, "reason": str(exc)}
        # Observation work is outside the issuance lock and cannot gate delivery.
        # One immutable baseline stays pinned throughout all of these attempts.

    def local_stage(forecast: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        scope = (
            snapshot.scope(correction_scope(forecast["latitude"], forecast["longitude"]))
            if snapshot is not None
            else None
        )
        if learning_service is None:
            # Preview only: issuance returned above when governance was unavailable.
            report: dict[str, Any] = {
                "status": "fallback",
                "reason": learning_error,
                "applied_delta_k": 0.0,
                "stage": "deterministic_correction",
            }
            corrected = {**forecast, "learning_failure": report}
        else:
            try:
                corrected, report = learning_service.local_stage(forecast, governance=scope)
            except GovernanceBlockedError:
                raise
            except Exception as exc:
                if scope is not None and scope.active is not None:
                    # Issuing raw here would be an unrecorded rollback of ACTIVE policy.
                    raise GovernanceBlockedError("governed_correction_failed", str(exc)) from exc
                report = {"status": "fallback", "reason": str(exc), "applied_delta_k": 0.0}
                corrected = {**forecast, "learning_failure": report}
            else:
                try:
                    learning_service.candidate_stages(forecast, report, learning_overlays or [])
                except Exception as exc:
                    report["failures"].append({"phase": "candidate_projection", "reason": str(exc)})
        final = attempt_forecast_desk(corrected, report, learning_service)
        if issue and scope is not None and governance is not None:
            try:
                governance.rollback_guard(scope)
            except GovernanceUnavailableError as exc:
                raise GovernanceBlockedError("governance_unavailable", str(exc)) from exc
        return final, report

    result.update(
        _deliver_locations(
            view,
            locations,
            reference_time=reference,
            display_timezone=display_timezone,
            issue=issue,
            issuer=issuer,
            reissue=reissue,
            run_lock=run_lock,
            lineage={"baseline_snapshot": baseline_lineage, "prepared_snapshot": prepared_lineage},
            build_timing_key="baseline_extraction_seconds",
            stage_processor=local_stage,
            stage_binding=learning_service.bind if learning_service is not None else None,
        )
    )
    for row in result["results"]:
        if row["index"] in verification:
            row["previous_verification"] = verification[row["index"]]
    timings["total_seconds"] = time.perf_counter() - started
    result["timings"] = timings
    return result


def _not_issued(
    result: dict[str, Any], locations: list[Any], started: float, status: str, reason: str
) -> dict[str, Any]:
    """Fail safe: nothing is issued, no stage is built and no desk/provider call occurs."""
    result.update(
        status=status,
        reason=reason,
        results=[
            {"index": index, "location": location, "status": "not_run", "reason": reason}
            for index, location in enumerate(locations)
        ],
        summary={"ok": 0, "issued": 0, "skipped": 0, "failed": len(locations)},
    )
    result["timings"] = {"total_seconds": time.perf_counter() - started}
    return result


def attempt_forecast_desk(
    corrected: dict[str, Any],
    report: dict[str, Any],
    learning_service: LearningService | None,
) -> dict[str, Any]:
    """Always attempt the bounded desk after correction; return the forecast to issue.

    The result is either a retained AI stage (latest validated checkpoint) or the
    complete corrected forecast with an explicit non-AI desk outcome. A partial,
    unvalidated or unretained AI state is never returned.
    """
    from mesoforge.application.forecast_desk import run_forecast_desk

    # Every configured job attempts the desk. Provider failures are handled by
    # its finite controller; missing durable stage lineage cannot authorize edits.
    checkpoint_parent = report.get("operational_stage")
    checkpoint_reference = report.get("operational_reference")
    lineage_ready = (
        learning_service is not None
        and isinstance(checkpoint_parent, dict)
        and checkpoint_parent.get("transformation_type") == "deterministic_corrected"
        and checkpoint_reference is not None
        and report.get("control_reference") is not None
    )

    def retain_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
        if (
            not lineage_ready
            or learning_service is None
            or checkpoint_parent is None
            or checkpoint_reference is None
        ):
            raise ValueError("Durable corrected-stage lineage is unavailable")
        parent = checkpoint_parent
        saved = learning_service.save(
            "learning-overlay",
            {
                **checkpoint,
                "schema_version": "mesoforge.forecast-desk-checkpoint.v1",
                "parent_stage_id": parent["variant_id"],
                "baseline_snapshot_id": parent["baseline_snapshot_id"],
                "prepared_snapshot_id": parent["prepared_snapshot_id"],
                "analysis_cutoff": parent["analysis_cutoff"],
            },
            inputs=(ArtifactId(checkpoint_reference["artifact_id"]),),
            attributes={"parent_stage_id": parent["variant_id"]},
        )
        return learning_service._reference(saved)

    desk: dict[str, Any]
    if not lineage_ready:
        # Attempted, but no accepted edit could become durable lineage: spend no
        # provider calls on a run whose result must be discarded.
        desk = {
            "schema_version": "mesoforge.forecast-desk-run.v1",
            "completion_reason": "lineage_unavailable",
            "usage": {"provider_calls": 0, "accepted_edits": 0},
            "accepted_recipes": [],
            "checkpoints": [],
        }
        return _corrected_fallback(corrected, report, desk)
    try:
        final, desk = run_forecast_desk(
            corrected,
            checkpoint_sink=retain_checkpoint,
            evidence=report.get("desk_evidence"),
        )
    except Exception as exc:  # The controller should not raise; isolate if it does.
        desk = {
            "schema_version": "mesoforge.forecast-desk-run.v1",
            "completion_reason": "desk_failure",
            "failure_type": type(exc).__name__,
            "accepted_recipes": [],
            "checkpoints": [],
        }
        return _corrected_fallback(corrected, report, desk)
    report["ai"] = desk
    if (
        not desk.get("checkpoints")
        or desk.get("validation", {}).get("status") != "valid"
        or not desk.get("usage", {}).get("validated_actions")
    ):
        # No pinned validated parent, or no provider action at all (unconfigured,
        # unavailable, quota/timeout on the first request). The corrected forecast
        # is issued as the operational stage; no AI stage claims a model decision.
        return _corrected_fallback(corrected, report, desk)
    assert learning_service is not None  # lineage_ready proves the durable store.
    try:
        final = learning_service.ai_stage(corrected, final, desk, report)
    except Exception as exc:
        report["ai_storage_failure"] = type(exc).__name__
        desk = {
            **desk,
            "attempt_completion_reason": desk.get("completion_reason"),
            "completion_reason": "stage_persistence_failed_fallback",
        }
        return _corrected_fallback(corrected, report, desk)
    return final


def _corrected_fallback(
    corrected: dict[str, Any], report: dict[str, Any], desk: dict[str, Any]
) -> dict[str, Any]:
    """Issue the complete corrected forecast with an explicit, non-AI desk outcome.

    No AI stage exists for this issuance. Accepted-but-unretained recipes and their
    checkpoints are kept only as discarded audit; raw/corrected lineage stays intact.
    """
    accepted = desk.get("accepted_recipes", [])
    checkpoints = desk.get("checkpoints", [])
    outcome = {
        **desk,
        "issued_checkpoint": "deterministic_corrected",
        "ai_stage": None,
        "accepted_recipes": [],
        "affected_fields": [],
        "checkpoints": checkpoints[:1],
        **(
            {"discarded_recipes": [*desk.get("discarded_recipes", []), *accepted]}
            if accepted or desk.get("discarded_recipes")
            else {}
        ),
        **(
            {"discarded_checkpoints": [*desk.get("discarded_checkpoints", []), *checkpoints[1:]]}
            if checkpoints[1:] or desk.get("discarded_checkpoints")
            else {}
        ),
        "point_values": [
            {
                "valid_time": hour["valid_time"],
                "corrected_temperature": hour["temperature"],
                "final_temperature": hour["temperature"],
                "applied_delta_k": 0.0,
            }
            for hour in corrected["hours"]
        ],
    }
    if desk.get("validation", {}).get("status") == "valid":
        outcome["validation"] = {**desk["validation"], "basis": "validated_corrected_parent"}
    report["ai"] = outcome
    lineage: dict[str, Any] = {}
    if report.get("control_reference") is not None:
        lineage.update(
            baseline_stage=report["control_stage"],
            baseline_stage_reference=report["control_reference"],
        )
    if report.get("operational_reference") is not None:
        lineage.update(
            deterministic_stage=report["operational_stage"],
            deterministic_reference=report["operational_reference"],
        )
    from mesoforge.application.forecast_desk import desk_summary

    return {**corrected, **lineage, "ai_desk": desk_summary(outcome)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Baseline root with latest_baseline"
    )
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--name", help="Optional display label for --lat/--lon")
    parser.add_argument("--config", type=Path, help="Locations JSON instead of --lat/--lon")
    parser.add_argument("--display-timezone", default="UTC")
    parser.add_argument(
        "--reference-time",
        type=datetime.fromisoformat,
        help="Explicit covered UTC reference hour (default: the current UTC hour)",
    )
    parser.add_argument("--issue", action="store_true", help="Also save immutable issuances")
    parser.add_argument("--reissue", action="store_true", help="Issue even if a version exists")
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="Explicit issuance-only replay; no prior observation work",
    )
    parser.add_argument(
        "--qpf-lookback-hours",
        type=int,
        default=72,
        help="Bound automatic prior QPF valid-hour lookup (default 72)",
    )
    parser.add_argument(
        "--qpf-max-opportunities",
        type=int,
        default=36,
        help="Per-location unresolved QPF stage/hour work cap",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Retain result.json and reports outside Git"
    )
    args = parser.parse_args(argv)
    if (args.config is None) == (args.lat is None or args.lon is None):
        parser.error("Provide either --config or both --lat and --lon")
    locations = (
        load_locations(args.config)
        if args.config is not None
        else [{"lat": args.lat, "lon": args.lon, **({"name": args.name} if args.name else {})}]
    )
    if args.output_dir is not None and args.output_dir.resolve().is_relative_to(_ROOT):
        parser.error("--output-dir must lie outside the repository")
    try:
        result = forecast_from_baseline(
            args.root,
            locations,
            reference_time=args.reference_time,
            display_timezone=args.display_timezone,
            issue=args.issue,
            reissue=args.reissue,
            verify_prior=not args.skip_verification,
            qpf_lookback_hours=args.qpf_lookback_hours,
            qpf_max_opportunities=args.qpf_max_opportunities,
        )
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "forecast_failed", "message": str(exc)}}), file=sys.stderr
        )
        return 2
    if args.output_dir is not None:
        _write_outputs(result, args.output_dir, title="Forecast from MesoForge baseline")
    print(json.dumps(_public(result), indent=2, default=str))
    if result["status"] == "no_current_baseline":
        return 3
    if result["status"] != "ok":
        return 2
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
