"""Read-only temperature comparisons on exact saved forecast/observation pairs.

The issued 70/30 value remains the control. A 50/50 calculation is comparison
output only. Aggregate metrics use the same complete pairs for every prediction.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    evaluate_recipe,
)
from mesoforge.verification.metrics import _compute_scalar_metrics

PREDICTION_KEYS = tuple(model.model_id for model in DEFAULT_CONFIGURATION.models) + tuple(
    recipe.result_key for recipe in DEFAULT_CONFIGURATION.recipes()
)
_CONTROL_TOLERANCE_K = 1e-10
LEAD_BUCKETS = ("1-6", "7-18", "19-36")
_BUCKETS = LEAD_BUCKETS


def lead_bucket(horizon: Any) -> str:
    """Shared horizon grouping for comparisons and verified-history counts."""
    if type(horizon) is not int or not 1 <= horizon <= 36:
        raise ValueError("comparison requires an integer target horizon from 1 through 36")
    return "1-6" if horizon <= 6 else "7-18" if horizon <= 18 else "19-36"


_lead_bucket = lead_bucket


def _prediction(temperature: Any, reasons: Any, label: str) -> dict[str, Any]:
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        raise ValueError(f"{label}: missing_reasons must be a list of strings")
    missing = list(reasons)
    if temperature is None:
        missing.append(f"{label}: temperature was not retained")
        value = None
    else:
        if not isinstance(temperature, dict) or temperature.get("unit") != "K":
            raise ValueError(f"{label}: comparison requires temperature units K")
        value = temperature.get("value")
        if value is None:
            if not missing:
                missing.append(f"{label}: temperature is missing")
        elif type(value) not in (float, int) or not math.isfinite(value):
            missing.append(f"{label}: temperature is not a finite number")
            value = None
        elif missing:
            raise ValueError(f"{label}: finite temperature contradicts declared missingness")
        else:
            value = float(value)
    return {"value": value, "unit": "K", "missing_reasons": list(dict.fromkeys(missing))}


def is_raw_temperature_control(stage: Any) -> bool:
    """Legacy/no-op stages retain the raw recipe; transformed temperatures do not."""
    if stage is None:
        return True
    if not isinstance(stage, Mapping):
        return False
    if stage.get("transformation_type") == "active_baseline":
        return True
    overlay = stage.get("overlay", {})
    correction = overlay.get("correction", {}) if isinstance(overlay, Mapping) else {}
    return (
        stage.get("transformation_type") == "deterministic_corrected"
        and isinstance(correction, Mapping)
        and correction.get("status")
        in {"no_policy", "insufficient_evidence", "no_op", "not_active", "retired", "fallback"}
        and not correction.get("changes")
        and not overlay.get("predictions")
    )


def temperature_control_stage(forecast: Mapping[str, Any]) -> Any:
    """The stage whose temperature an issued forecast carries.

    An AI stage that accepted no temperature edit carries its deterministic parent's
    temperature unchanged, so that parent decides raw-control eligibility. A missing
    parent keeps the AI stage itself, which is never treated as raw control.
    """
    stage = forecast.get("learning_stage")
    if (
        isinstance(stage, Mapping)
        and stage.get("transformation_type") == "ai_adjusted"
        and isinstance(stage.get("affected_fields"), list)
        and "air_temperature_2m" not in stage["affected_fields"]
    ):
        parent = forecast.get("deterministic_stage")
        if isinstance(parent, Mapping) and parent.get("variant_id") == stage.get("parent_stage_id"):
            return parent
    return stage


def require_raw_temperature_control(stage: Any) -> None:
    """Keep the historical contributor comparison explicit about the stage it scores."""
    if not is_raw_temperature_control(stage):
        raise ValueError(
            "Adjusted temperature stage is not the raw contributor blend; "
            "use mesoforge.application.learning analyze for stage-aware comparison."
        )


def compare_hour(
    hour: dict[str, Any],
    observation: dict[str, Any] | None,
    *,
    configuration: ContributorConfiguration = DEFAULT_CONFIGURATION,
    ineligible_models: Mapping[str, Sequence[str]] | None = None,
    forecast_stage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare retained values against the already-selected verified temperature.

    The caller supplies the observation from an eligible immutable verification;
    this function neither selects observations nor changes eligibility. Missing
    legacy contributor values stay missing, even when the control was retained.
    """
    require_raw_temperature_control(forecast_stage)
    bucket = _lead_bucket(hour.get("horizon_hours"))
    active = hour.get("sources")
    shadows = hour.get("shadow_sources", [])
    control_recipe = configuration.control_recipe
    if control_recipe.field != "air_temperature_2m":
        raise ValueError("temperature comparison requires air_temperature_2m")
    weights = {item.model: item.weight for item in control_recipe.contributors}
    if (
        not isinstance(active, list)
        or len(active) != len(weights)
        or any(not isinstance(source, dict) for source in active)
        or {source.get("model") for source in active} != set(weights)
    ):
        raise ValueError("comparison requires exactly the saved control recipe contributors")
    if not isinstance(shadows, list) or any(not isinstance(source, dict) for source in shadows):
        raise ValueError("shadow_sources must contain contributor records")
    sources = active + shadows
    by_model = {source["model"]: source for source in sources}
    if len(by_model) != len(sources) or not set(by_model).issubset(configuration.model_map()):
        raise ValueError("comparison has duplicate or unregistered contributors")

    predictions: dict[str, dict[str, Any]] = {}
    eligibility = {key: list(value) for key, value in (ineligible_models or {}).items()}
    for source in active:
        model = source["model"]
        weight = source.get("weight")
        if type(weight) not in (int, float) or weight != weights[model]:
            raise ValueError(f"{model}: comparison requires the saved control recipe weights")
    for source in shadows:
        if type(source.get("weight")) not in (int, float) or source["weight"] != 0:
            raise ValueError("shadow contributors must have zero active weight")
    definitions: dict[str, Any] = {}
    for model in configuration.models:
        if model.status == "retired":
            continue
        source = by_model.get(model.model_id, {})
        predictions[model.model_id] = _prediction(
            source.get("temperature"), source.get("missing_reasons", []), model.model_id
        )
        definitions[model.model_id] = {
            "kind": "model",
            "model_id": model.model_id,
            "field": control_recipe.field,
        }
    values = {name: prediction["value"] for name, prediction in predictions.items()}
    for recipe in configuration.recipes():
        evaluated = evaluate_recipe(recipe, values)
        key = recipe.result_key
        reasons = [
            reason
            for model in evaluated.missing_models
            for reason in predictions[model]["missing_reasons"]
        ]
        prediction: dict[str, Any] = {
            "value": evaluated.value,
            "unit": "K",
            "missing_reasons": reasons,
        }
        if recipe == control_recipe:
            prediction = _prediction(hour.get("temperature"), hour.get("missing_reasons", []), key)
            if (
                prediction["value"] is not None
                and evaluated.value is not None
                and not math.isclose(
                    prediction["value"], evaluated.value, rel_tol=0.0, abs_tol=_CONTROL_TOLERANCE_K
                )
            ):
                raise ValueError("saved control disagrees with its retained contributors")
        predictions[key] = prediction
        definitions[key] = {"kind": "recipe", **recipe.model_dump(mode="json")}
        eligibility[key] = [
            f"{item.model}: {reason}"
            for item in recipe.contributors
            for reason in eligibility.get(item.model, [])
        ]
    observed = _prediction(observation, [], "observation")
    errors: dict[str, float | None] = {}
    for name in predictions:
        value = predictions[name]["value"]
        error = (
            value - observed["value"]
            if value is not None and observed["value"] is not None and not eligibility.get(name)
            else None
        )
        if error is not None and not math.isfinite(error):
            raise ValueError(f"{name}: forecast-minus-observation error is nonfinite")
        errors[name] = error
    exclusions = []
    if observed["value"] is None:
        exclusions.append("observation_missing")
    exclusions.extend(
        f"{name}_missing" for name in predictions if predictions[name]["value"] is None
    )
    exclusions.extend(f"{name}_ineligible" for name in predictions if eligibility.get(name))
    return {
        "lead_bucket": bucket,
        "predictions": predictions,
        "prediction_definitions": definitions,
        "ineligible_reasons": {name: eligibility.get(name, []) for name in predictions},
        "observation": observed,
        "errors": errors,
        "error_unit": "K",
        "paired_sample": not exclusions,
        "exclusion_reasons": exclusions,
    }


def _summarize_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    paired = [
        row for row in rows if row["paired_sample"] and all(key in row["errors"] for key in keys)
    ]
    metrics: dict[str, Any] = {}
    for name in keys:
        errors = [row["errors"][name] for row in paired]
        if any(type(error) not in (int, float) or not math.isfinite(error) for error in errors):
            raise ValueError("a complete paired comparison requires finite errors for every model")
        bias, mae, rmse = _compute_scalar_metrics(errors)
        metrics[name] = {
            "sample_count": len(paired),
            "mae": mae,
            "mean_bias": bias,
            "rmse": rmse,
            "unit": "K",
        }
    exclusions: Counter[str] = Counter()
    for row in rows:
        exclusions.update(row["exclusion_reasons"])
        exclusions.update(f"{key}_missing" for key in keys if key not in row["errors"])
    return {
        "row_count": len(rows),
        "paired_sample_count": len(paired),
        "excluded_count": len(rows) - len(paired),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "predictions": metrics,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe identical complete pairs overall and by saved target horizon.

    One row represents one distinct issued version/hour. The storage reader must
    reject repeated verification rows for that same issued hour before calling.
    Exclusion reasons can overlap; excluded_count counts rows only once.
    """
    if any(row["lead_bucket"] not in _BUCKETS for row in rows):
        raise ValueError("comparison contains an unsupported lead bucket")
    definitions: dict[str, Any] = {}
    for row in rows:
        for key, definition in row.get("prediction_definitions", {}).items():
            if key in definitions and definitions[key] != definition:
                raise ValueError(f"Cannot pool different recipe definitions under {key!r}")
            definitions[key] = definition
    keys = (
        tuple(dict.fromkeys(key for row in rows for key in row["predictions"])) or PREDICTION_KEYS
    )
    return {
        "all": _summarize_rows(rows, keys),
        **{
            bucket: _summarize_rows([row for row in rows if row["lead_bucket"] == bucket], keys)
            for bucket in _BUCKETS
        },
        "interpretation": (
            "Descriptive statistics of the same complete paired samples for all requested "
            "predictions. "
            "Issued versions remain separate samples; overlapping forecasts are not independent. "
            "Sample counts alone do not establish predictive skill. Exclusion reasons may overlap."
        ),
    }
