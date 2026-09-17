"""Compact analytical attributes derived from one immutable verification fact.

The full fact payload stays the authoritative evidence record. This block copies only
the identity, values and point context that ordinary statistics need, so analysis
does not have to open a multi-megabyte payload per fact. It is a pure projection of
the payload: the same function serves new facts at write time and legacy facts at
read time, which keeps both paths identical. It is not a model, weight or correction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import canonical_json_bytes

ANALYTICAL_SCHEMA_VERSION = "mesoforge.verification-analytical-attributes.v1"
ATTRIBUTE_KEY = "analysis"

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
