"""Background candidate fields as sparse overlays over an immutable active baseline.

Retained native contributor values are the exact prepared inputs already extracted
at each node. They are replayed through FieldBlendEngine, never acquired again.
Unchanged forecast fields and all source evidence remain owned by the parent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from mesoforge.application.baseline_snapshot import PinnedBaseline, read_artifact
from mesoforge.application.prepared_snapshot import SnapshotError
from mesoforge.catalog.configuration import Phase2BlendConfiguration
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.candidate_policy import CandidateBlendPolicy
from mesoforge.forecasting.coherence import (
    BASELINE_COHERENCE,
    DEW_POINT,
    GUST,
    QPF,
    RH,
    TEMPERATURE,
    WIND,
)
from mesoforge.forecasting.field_blend import BlendState, FieldBlendEngine
from mesoforge.forecasting.provisional_policy import ProvisionalFieldPolicy
from mesoforge.forecasting.recipes import ContributorConfiguration, Recipe

_DEPENDENT_FIELDS = {
    TEMPERATURE: (TEMPERATURE, DEW_POINT, RH),
    DEW_POINT: (DEW_POINT, RH),
    WIND: (
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_speed_10m",
        "wind_from_direction_10m",
        GUST,
    ),
    GUST: (GUST,),
    QPF: (QPF,),
}


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Retained baseline time must include a timezone")
    return result.astimezone(UTC)


def governed_overrides(manifest: dict[str, Any]) -> dict[str, CandidateBlendPolicy]:
    """ACTIVE governed blend policies pinned by the parent baseline manifest."""
    policies = (manifest.get("blend_governance") or {}).get("policies", {})
    return {
        field: CandidateBlendPolicy.model_validate_json(canonical_json_bytes(row["policy"]))
        for field, row in policies.items()
    }


def _engine(
    grid: dict[str, Any],
    policy: CandidateBlendPolicy,
    governed: dict[str, CandidateBlendPolicy] | None = None,
) -> FieldBlendEngine:
    """The parent's exact engine (including its governed overrides) plus one candidate."""
    parents = {field: row.parameters for field, row in (governed or {}).items()}
    context = grid["forecast_context"]
    contributors = ContributorConfiguration.model_validate_json(
        json.dumps(context["contributor_configuration"])
    )
    if contributors.field_policy_family is not None:
        raise ValueError(
            "Legacy fixed-weight candidate data cannot override a provisional role policy"
        )
    configuration = Phase2BlendConfiguration.model_validate_json(
        json.dumps(
            context["current_model_set"]["selection"]["source_configuration"]["blend_configuration"]
        )
    )
    active = FieldBlendEngine(
        contributors=contributors, phase2=configuration, policy_overrides=parents
    )
    control = active.policy_for(policy.field)
    if isinstance(control, ProvisionalFieldPolicy):
        raise ValueError(
            "Legacy fixed-weight candidate data cannot override a provisional role policy"
        )
    identity = (
        f"{control.name}/{control.version}"
        if isinstance(control, Recipe)
        else control.table_id
        if not isinstance(control, str)
        else control
    )
    if policy.parent_policy != identity:
        raise ValueError("Candidate parent policy differs from the retained active field policy")
    return FieldBlendEngine(
        contributors=contributors,
        phase2=configuration,
        policy_overrides={**parents, policy.field: policy.parameters},
    )


def _state(hour: dict[str, Any]) -> BlendState:
    native = hour["surface"]["contributors"]
    values = {
        model: {
            name: row["value"]
            for name, row in contributor["fields"].items()
            if name in (TEMPERATURE, DEW_POINT, "eastward_wind_10m", "northward_wind_10m", GUST)
        }
        for model, contributor in native.items()
        if any(
            name in contributor["fields"]
            for name in (TEMPERATURE, DEW_POINT, "eastward_wind_10m", "northward_wind_10m", GUST)
        )
    }
    qpf = {
        model: contributor["fields"][QPF]
        for model, contributor in native.items()
        if QPF in contributor["fields"]
    }
    return BlendState(horizon=hour["horizon_hours"], contributors=values, precipitation=qpf)


def build_candidate_overlay(
    pinned_baseline: PinnedBaseline,
    policy: CandidateBlendPolicy,
    *,
    analysis_cutoff: datetime,
) -> dict[str, Any]:
    """Materialize explicit shadow data without publishing/changing active state.

    The returned payload is compact and deterministic; runtime measurements are
    outside ``overlay`` so repeated calculation produces the same stage digest.
    The existing artifact service owns persistence at the orchestration boundary.
    """
    started = perf_counter()
    # Pydantic's frozen model does not deeply freeze arbitrary provenance mappings.
    # Pin and revalidate the complete policy before lengthy background execution.
    policy = CandidateBlendPolicy.model_validate_json(policy.model_dump_json())
    policy.validate_execution(analysis_cutoff)
    manifest = pinned_baseline.manifest
    governed = governed_overrides(manifest)
    for value in (manifest["analysis_cutoff"], pinned_baseline.pointer["published_at"]):
        if _instant(value) > analysis_cutoff:
            raise ValueError("Candidate cannot consume a baseline from after its analysis cutoff")
    domains: list[dict[str, Any]] = []
    coherence_reports: dict[str, dict[str, Any]] = {}
    execution_seconds = 0.0
    coherence_seconds = 0.0
    for domain in manifest["domains"]:
        encoded = read_artifact(pinned_baseline.directory, domain["artifact"])
        grid = pinned_baseline.codec.decode(encoded)
        if (
            str(canonical_json_digest(grid)) != domain["grid_sha256"]
            or grid["geometry"] != domain["geometry"]
        ):
            raise SnapshotError("Candidate parent grid differs from its immutable identity")
        engine = _engine(grid, policy, governed)
        cells = []
        for cell in grid["cells"]:
            if cell["status"] != "calculated":
                continue  # Inherit the parent's exact spatial missingness.
            hours = []
            for hour in cell["hours"]:
                if not policy.lead_start <= hour["horizon_hours"] <= policy.lead_end:
                    continue
                state = _state(hour)
                clock = perf_counter()
                fields = engine.surface_fields(state)
                execution_seconds += perf_counter() - clock
                coherence_seconds += sum(state.coherence_seconds.values())
                selected = {name: fields[name] for name in _DEPENDENT_FIELDS[policy.field]}
                if policy.field == TEMPERATURE:
                    # Historical surface projection label is not the candidate recipe.
                    selected[TEMPERATURE] = state.results[TEMPERATURE]
                report = {
                    "version": BASELINE_COHERENCE.report(state)["version"],
                    "relationships": {
                        name: {
                            key: event[key]
                            for key in ("status", "actions", "changed_fields", "derived_fields")
                        }
                        for name, event in state.coherence_events.items()
                    },
                }
                report_id = str(canonical_json_digest(report))
                coherence_reports.setdefault(report_id, report)
                hours.append(
                    {
                        "horizon_hours": hour["horizon_hours"],
                        "valid_time": hour["valid_time"],
                        "fields": selected,
                        "coherence_reference": report_id,
                    }
                )
            cells.append({"x_index": cell["x_index"], "y_index": cell["y_index"], "hours": hours})
        domains.append(
            {
                "latitude": domain["latitude"],
                "longitude": domain["longitude"],
                "reference_time": domain["reference_time"],
                "parent_grid_sha256": domain["grid_sha256"],
                "parent_artifact": domain["artifact"],
                "point_target": grid["geometry"]["point_target"],
                "cells": cells,
            }
        )
    overlay = {
        "schema_version": "mesoforge.candidate-baseline-overlay.v1",
        "kind": "candidate_blend",
        "lifecycle_role": "shadow",
        "baseline_snapshot_id": manifest["baseline_snapshot_id"],
        "baseline_manifest_sha256": pinned_baseline.pointer["manifest_sha256"],
        "prepared_snapshot_id": manifest["prepared_snapshot"]["snapshot_id"],
        "analysis_cutoff": analysis_cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "policy": policy.model_dump(mode="json"),
        "policy_digest": policy.digest,
        "affected_fields": list(_DEPENDENT_FIELDS[policy.field]),
        "inherited_fields": "All fields outside this sparse overlay remain the active parent's",
        "source_evidence": "Native contributors and provenance referenced through parent baseline",
        "coherence_reports": coherence_reports,
        "domains": domains,
    }
    return {
        "overlay": overlay,
        "sha256": str(canonical_json_digest(overlay)),
        "bytes": len(canonical_json_bytes(overlay)),
        "timings": {
            "field_and_coherence_seconds": execution_seconds,
            "coherence_seconds": coherence_seconds,
            "total_seconds": perf_counter() - started,
        },
        "network_calls": 0,
    }


def candidate_point_overlay(
    overlay: dict[str, Any], *, latitude: float, longitude: float, reference_time: datetime
) -> list[dict[str, Any]]:
    """Resolve exact saved center patches; all other values inherit the active parent."""
    domain = next(
        (
            row
            for row in overlay["domains"]
            if row["latitude"] == latitude
            and row["longitude"] == longitude
            and _instant(row["reference_time"]) == reference_time
        ),
        None,
    )
    if domain is None:
        raise ValueError("Candidate overlay does not cover this configured domain/reference")
    target = domain["point_target"]
    point = next(
        (
            row
            for row in domain["cells"]
            if row["x_index"] == target["x_index"] and row["y_index"] == target["y_index"]
        ),
        None,
    )
    if point is None:
        raise ValueError("Candidate overlay center retains unavailable parent coverage")
    return list(point["hours"])
