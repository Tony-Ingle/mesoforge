"""Read-only temperature comparisons on exact saved forecast/observation pairs.

The issued 70/30 value remains the control. A 50/50 calculation is comparison
output only. Aggregate metrics use the same complete pairs for every prediction.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar
from mesoforge.verification.metrics import _compute_scalar_metrics

PREDICTION_KEYS = ("HRRR", "GFS", "blend_70_30", "blend_50_50")
_CONTROL_WEIGHTS = {"HRRR": 0.7, "GFS": 0.3}
_CONTROL_TOLERANCE_K = 1e-10
_BUCKETS = ("1-6", "7-18", "19-36")


def _lead_bucket(horizon: Any) -> str:
    if type(horizon) is not int or not 1 <= horizon <= 36:
        raise ValueError("comparison requires an integer target horizon from 1 through 36")
    return "1-6" if horizon <= 6 else "7-18" if horizon <= 18 else "19-36"


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


def compare_hour(hour: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any]:
    """Compare retained values against the already-selected verified temperature.

    The caller supplies the observation from an eligible immutable verification;
    this function neither selects observations nor changes eligibility. Missing
    legacy contributor values stay missing, even when the control was retained.
    """
    bucket = _lead_bucket(hour.get("horizon_hours"))
    sources = hour.get("sources")
    if (
        not isinstance(sources, list)
        or len(sources) != 2
        or any(not isinstance(source, dict) for source in sources)
        or {source.get("model") for source in sources} != set(_CONTROL_WEIGHTS)
    ):
        raise ValueError("comparison requires exactly the HRRR and GFS contributors")

    predictions: dict[str, dict[str, Any]] = {}
    for source in sources:
        model = source["model"]
        weight = source.get("weight")
        if type(weight) not in (int, float) or weight != _CONTROL_WEIGHTS[model]:
            raise ValueError(f"{model}: comparison requires the saved 70/30 control weights")
        predictions[model] = _prediction(
            source.get("temperature"), source.get("missing_reasons", []), model
        )
    control = _prediction(hour.get("temperature"), hour.get("missing_reasons", []), "blend_70_30")
    predictions["blend_70_30"] = control
    contributors_missing = [
        reason for model in _CONTROL_WEIGHTS for reason in predictions[model]["missing_reasons"]
    ]
    comparison_value = None
    if not contributors_missing:
        control_check = blend_scalar(
            tuple(
                Contribution(model, predictions[model]["value"], weight)
                for model, weight in _CONTROL_WEIGHTS.items()
            )
        ).blended_value
        if control["value"] is not None and not math.isclose(
            control["value"], control_check, rel_tol=0.0, abs_tol=_CONTROL_TOLERANCE_K
        ):
            raise ValueError("saved 70/30 control disagrees with its retained contributors")
        comparison_value = blend_scalar(
            tuple(
                Contribution(model, predictions[model]["value"], 0.5) for model in _CONTROL_WEIGHTS
            )
        ).blended_value
    predictions["blend_50_50"] = {
        "value": comparison_value,
        "unit": "K",
        "missing_reasons": list(contributors_missing),
    }
    observed = _prediction(observation, [], "observation")
    errors: dict[str, float | None] = {}
    for name in PREDICTION_KEYS:
        value = predictions[name]["value"]
        error = (
            value - observed["value"]
            if value is not None and observed["value"] is not None
            else None
        )
        if error is not None and not math.isfinite(error):
            raise ValueError(f"{name}: forecast-minus-observation error is nonfinite")
        errors[name] = error
    exclusions = []
    if observed["value"] is None:
        exclusions.append("observation_missing")
    exclusions.extend(
        f"{name}_missing" for name in PREDICTION_KEYS if predictions[name]["value"] is None
    )
    return {
        "lead_bucket": bucket,
        "predictions": predictions,
        "observation": observed,
        "errors": errors,
        "error_unit": "K",
        "paired_sample": not exclusions,
        "exclusion_reasons": exclusions,
    }


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [row for row in rows if row["paired_sample"]]
    metrics: dict[str, Any] = {}
    for name in PREDICTION_KEYS:
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
    exclusions = Counter(
        reason for row in rows if not row["paired_sample"] for reason in row["exclusion_reasons"]
    )
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
    return {
        "all": _summarize_rows(rows),
        **{
            bucket: _summarize_rows([row for row in rows if row["lead_bucket"] == bucket])
            for bucket in _BUCKETS
        },
        "interpretation": (
            "Descriptive statistics of the same complete paired samples for all four predictions. "
            "Issued versions remain separate samples; overlapping forecasts are not independent. "
            "Sample counts alone do not establish predictive skill. Exclusion reasons may overlap."
        ),
    }
