"""Compact analytical attributes derived from one immutable verification fact.

The full fact payload stays the authoritative evidence record. This block copies only
the identity, values and point context that ordinary statistics need, so analysis
does not have to open a multi-megabyte payload per fact. It is a pure projection of
the payload: the same function serves new facts at write time and legacy facts at
read time, which keeps both paths identical. It is not a model, weight or correction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.forecast_variants import VARIANT_SCHEMA, instant, validate_variant
from mesoforge.contracts.serialization import canonical_json_bytes

ANALYTICAL_SCHEMA_VERSION = "mesoforge.verification-analytical-attributes.v1"
ATTRIBUTE_KEY = "analysis"
RAW_BASELINE_PROJECTION = "mesoforge.raw-baseline-temperature-projection.v1"

# Saved point fields of the verified issued hour that are worth keeping for later
# descriptive regime analysis. Values and units only; never provenance or policy prose.
CONTEXT_FIELDS: tuple[str, ...] = (
    "cloud_area_fraction",
    "wind_speed_10m",
    "wind_from_direction_10m",
    "wind_gust_10m",
    "dew_point_temperature_2m",
    "relative_humidity_2m",
    "liquid_equivalent_precipitation_amount_1h",
    "probability_of_precipitation_1h",
    "precipitation_type",
    "probability_of_thunder_1h",
)

_OBSERVATION_KEYS: tuple[str, ...] = (
    "station_id",
    "catalog_station_id",
    "network",
    "provider",
    "latitude",
    "longitude",
    "elevation_m",
    "distance_km",
    "observation_time",
    "time_difference_seconds",
)
_OBSERVATION_PROVENANCE_KEYS: tuple[str, ...] = (
    "revision_digest",
    "logical_observation_digest",
    "raw_record_digest",
    "raw_artifact_id",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _kelvin(value: Any) -> Any:
    block = _mapping(value)
    return block.get("value") if block.get("unit") == "K" else None


def _forecast_stage(context: Mapping[str, Any]) -> dict[str, Any] | None:
    stage = context.get("learning_stage")
    if stage is None:
        return None
    if not isinstance(stage, Mapping):
        return {"invalid": True}
    overlay = _mapping(stage.get("overlay"))
    correction = _mapping(overlay.get("correction"))
    return {
        "variant_id": stage.get("variant_id"),
        "transformation_type": stage.get("transformation_type"),
        "overlay": {
            "predictions": bool(overlay.get("predictions")),
            "correction": {
                "status": correction.get("status"),
                "changes": bool(correction.get("changes")),
            },
        },
    }


def _raw_baseline_temperature(
    context: Mapping[str, Any], forecast: Mapping[str, Any], issued_at: Any
) -> dict[str, Any] | None:
    """Recover the exact saved raw point, never reconstruct it from native models.

    The compact context is a projection of the sealed stage retained in the
    immutable issuance; its authoritative artifact reference remains explicit.
    Invalid/missing legacy lineage is excluded, never inferred from an AI result.
    """
    stage = context.get("baseline_stage")
    if stage is None:
        return None
    try:
        if not isinstance(stage, dict):
            raise ValueError("raw_stage_malformed")
        reference = _mapping(context.get("baseline_stage_reference"))
        ArtifactId(reference["artifact_id"])
        Digest(reference["content_digest"])
        Digest(stage["variant_id"])
        if stage.get("schema_version") == VARIANT_SCHEMA:
            validate_variant(stage)
            predictions = stage["overlay"]["predictions"]
        else:
            if (
                stage.get("representation") != "summary_reference_not_sealed_variant"
                or stage.get("source_schema_version") != VARIANT_SCHEMA
                or stage.get("authoritative_artifact") != reference
            ):
                raise ValueError("raw_stage_summary_identity_unproven")
            predictions = stage["baseline_temperature_predictions"]
        if (
            stage.get("transformation_type") != "active_baseline"
            or stage.get("parent_stage_id") is not None
            or stage.get("lifecycle_role") != "active"
            or "air_temperature_2m" not in stage["fields"]
            or stage.get("evidence_status") != "baseline"
            or stage.get("location")
            != {"latitude": forecast.get("latitude"), "longitude": forecast.get("longitude")}
        ):
            raise ValueError("raw_stage_identity_disagrees")
        lineage = _mapping(context.get("baseline_snapshot"))
        for key in ("baseline_snapshot_id", "prepared_snapshot_id"):
            if not stage.get(key) or stage[key] != lineage.get(key):
                raise ValueError("raw_stage_baseline_lineage_disagrees")
        target, valid = instant(context["target_reference_time"]), instant(forecast["valid_time"])
        lead = forecast["horizon_hours"]
        if (
            type(lead) is not int
            or not 1 <= lead <= 36
            or valid - target != timedelta(hours=lead)
            or instant(stage["reference_time"]) != target
        ):
            raise ValueError("raw_stage_valid_hour_disagrees")
        cutoff, created, issued = (
            instant(stage["analysis_cutoff"]),
            instant(stage["created_at"]),
            instant(issued_at),
        )
        if (
            not target <= cutoff <= created <= issued
            or cutoff != instant(lineage["forecast_analysis_cutoff"])
            or instant(reference["registered_at"]) > issued
        ):
            raise ValueError("raw_stage_time_identity_disagrees")
        if not isinstance(predictions, list):
            raise ValueError("raw_stage_predictions_missing")
        selected = [
            row
            for row in predictions
            if isinstance(row, dict)
            and row.get("field") == "air_temperature_2m"
            and instant(row["valid_time"]) == valid
        ]
        if len(selected) != 1:
            raise ValueError("raw_stage_hour_missing_or_ambiguous")
        row = selected[0]
        value = row.get("value")
        if (
            row.get("unit") != "K"
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or row.get("interval_start") is not None
            or row.get("interval_end") is not None
        ):
            raise ValueError("raw_stage_temperature_unavailable_or_incompatible")
        return {
            "status": "available",
            "projection_policy": RAW_BASELINE_PROJECTION,
            "variant_id": stage["variant_id"],
            "transformation_type": "active_baseline",
            "authoritative_artifact": dict(reference),
            "baseline_snapshot_id": stage["baseline_snapshot_id"],
            "prepared_snapshot_id": stage["prepared_snapshot_id"],
            "valid_time": forecast["valid_time"],
            "value": value,
            "unit": "K",
            "analysis_cutoff": stage["analysis_cutoff"],
        }
    except (ValueError, TypeError, KeyError):
        return {"status": "unavailable", "reason": "raw_baseline_temperature_identity_unproven"}


def build_analytical_attributes(fact: Mapping[str, Any]) -> dict[str, Any]:
    """Project one verification fact payload onto the compact analytical block."""
    match = _mapping(fact.get("match"))
    forecast = _mapping(match.get("forecast"))
    selected = _mapping(match.get("selected"))
    provenance = _mapping(selected.get("provenance"))
    context = _mapping(match.get("forecast_context"))
    fields = _mapping(_mapping(forecast.get("surface")).get("fields"))
    selection_policy = match.get("selection_policy")
    error = _mapping(fact.get("temperature_error"))
    models: dict[str, Any] = {}
    for group in ("sources", "shadow_sources"):
        for source in forecast.get(group) or []:
            if isinstance(source, Mapping) and isinstance(source.get("model"), str):
                models[source["model"]] = _kelvin(source.get("temperature"))
    saved_fields = {name: _mapping(fields.get(name)) for name in CONTEXT_FIELDS if name in fields}
    return {
        "schema_version": ANALYTICAL_SCHEMA_VERSION,
        "field": "air_temperature_2m",
        "unit": "K",
        "error_definition": "forecast_minus_observation",
        "fact_schema_version": fact.get("schema_version"),
        "status": fact.get("status"),
        "verification_policy_id": _mapping(fact.get("verification_policy")).get("policy_id"),
        "matching_policy_digest": (
            str(Digest.of_bytes(canonical_json_bytes(dict(selection_policy))))
            if isinstance(selection_policy, Mapping)
            else None
        ),
        "verification_cutoff": fact.get("verification_cutoff"),
        "code_commit": _mapping(fact.get("code_identity")).get("git_commit"),
        "issued_forecast_id": match.get("issued_forecast_id"),
        "issued_forecast_digest": fact.get("issued_forecast_digest"),
        "issued_at": match.get("issued_at"),
        "latitude": forecast.get("latitude"),
        "longitude": forecast.get("longitude"),
        "target_reference_time": context.get("target_reference_time"),
        "valid_time": forecast.get("valid_time"),
        "horizon_hours": forecast.get("horizon_hours"),
        "forecast_temperature_k": _kelvin(forecast.get("temperature")),
        "forecast_stage": _forecast_stage(context),
        "raw_baseline_temperature": _raw_baseline_temperature(
            context, forecast, match.get("issued_at")
        ),
        "temperature_error_k": error.get("value") if error.get("unit") == "K" else None,
        "observation": {
            **{key: selected.get(key) for key in _OBSERVATION_KEYS},
            "temperature_k": _kelvin(selected.get("temperature")),
            **{key: provenance.get(key) for key in _OBSERVATION_PROVENANCE_KEYS},
            "observations_artifact_id": _mapping(
                _mapping(match.get("input_provenance")).get("observations")
            ).get("artifact_id"),
        },
        "display_timezone": _mapping(context.get("hourly_report")).get("display_timezone"),
        "context": {
            "fields": {name: field.get("value") for name, field in saved_fields.items()},
            "units": {name: field.get("unit") for name, field in saved_fields.items()},
            "model_temperatures_k": models,
        },
    }


def analytical_block(attributes: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """Return a usable compact block from artifact attributes, or why there is none."""
    block = _mapping(attributes).get(ATTRIBUTE_KEY)
    if block is None:
        return None, "no_analytical_attributes"
    if not isinstance(block, Mapping):
        return None, "malformed_analytical_attributes"
    if block.get("schema_version") != ANALYTICAL_SCHEMA_VERSION:
        return None, "unsupported_analytical_schema"
    if not isinstance(block.get("observation"), Mapping) or not isinstance(
        block.get("context"), Mapping
    ):
        return None, "malformed_analytical_attributes"
    return dict(block), "compact_attributes"
