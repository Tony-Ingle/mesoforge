"""Independent arithmetic and missing-pair checks for saved temperature comparisons."""

from __future__ import annotations

import copy
import math

import pytest

from mesoforge.catalog.contributors import (
    IFS_MODEL_DEFINITION,
    RAP_MODEL_DEFINITION,
    ModelDefinition,
)
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    Recipe,
    RecipeContributor,
)
from mesoforge.verification.model_comparison import PREDICTION_KEYS, compare_hour, summarize


def _hour(*, horizon=1, hrrr=280.0, gfs=290.0, control=283.0):
    return {
        "horizon_hours": horizon,
        "valid_time": "2026-01-01T13:00:00Z",
        "temperature": {"value": control, "unit": "K"},
        "missing_reasons": [],
        "sources": [
            {
                "model": "HRRR",
                "weight": 0.7,
                "cycle": "2026-01-01T06:00:00Z",
                "source_lead_hours": 7,
                "temperature": {"value": hrrr, "unit": "K"},
                "missing_reasons": [],
            },
            {
                "model": "GFS",
                "weight": 0.3,
                "cycle": "2026-01-01T00:00:00Z",
                "source_lead_hours": 13,
                "temperature": {"value": gfs, "unit": "K"},
                "missing_reasons": [],
            },
        ],
    }


def test_independent_predictions_signed_errors_and_inputs_unchanged():
    hour = _hour()
    observation = {"value": 282.0, "unit": "K"}
    original = copy.deepcopy((hour, observation))
    result = compare_hour(hour, observation)

    assert {key: value["value"] for key, value in result["predictions"].items()} == {
        "HRRR": 280.0,
        "GFS": 290.0,
        "blend_70_30": 283.0,
        "blend_50_50": 285.0,
    }
    assert result["errors"] == {"HRRR": -2.0, "GFS": 8.0, "blend_70_30": 1.0, "blend_50_50": 3.0}
    assert result["paired_sample"] is True
    assert result["exclusion_reasons"] == []
    assert result["error_unit"] == "K"
    assert all(prediction["unit"] == "K" for prediction in result["predictions"].values())
    assert (hour, observation) == original


def test_adjusted_stage_routes_to_unified_evaluator_while_noop_is_exact():
    stage = {
        "transformation_type": "deterministic_corrected",
        "overlay": {"predictions": [], "correction": {"status": "no_policy", "changes": []}},
    }
    assert compare_hour(_hour(), None, forecast_stage=stage) == compare_hour(_hour(), None)
    stage["overlay"]["correction"]["status"] = "applied"
    with pytest.raises(ValueError, match="learning analyze"):
        compare_hour(_hour(control=282.0), None, forecast_stage=stage)


@pytest.mark.parametrize(
    ("horizon", "bucket"),
    [(1, "1-6"), (6, "1-6"), (7, "7-18"), (18, "7-18"), (19, "19-36"), (36, "19-36")],
)
def test_bucket_uses_saved_target_horizon_not_native_model_lead(horizon, bucket):
    assert compare_hour(_hour(horizon=horizon), None)["lead_bucket"] == bucket


@pytest.mark.parametrize("horizon", [0, 37, True, 1.0])
def test_invalid_horizon_is_rejected(horizon):
    with pytest.raises(ValueError, match="integer target horizon"):
        compare_hour(_hour(horizon=horizon), None)


def test_legacy_missing_contributor_is_not_recovered_from_saved_blend():
    hour = _hour()
    del hour["sources"][0]["temperature"]
    result = compare_hour(hour, {"value": 282.0, "unit": "K"})
    assert result["predictions"]["HRRR"]["value"] is None
    assert "not retained" in result["predictions"]["HRRR"]["missing_reasons"][0]
    assert result["predictions"]["blend_50_50"]["value"] is None
    assert result["predictions"]["blend_70_30"]["value"] == 283.0
    assert result["errors"] == {"HRRR": None, "GFS": 8.0, "blend_70_30": 1.0, "blend_50_50": None}
    assert result["paired_sample"] is False


def test_explicit_missing_guidance_remains_missing_without_renormalizing():
    hour = _hour(hrrr=None, control=None)
    hour["sources"][0]["missing_reasons"] = ["HRRR: prepared guidance file is missing"]
    hour["missing_reasons"] = ["HRRR: prepared guidance file is missing"]
    result = compare_hour(hour, {"value": 282.0, "unit": "K"})
    for name in ("HRRR", "blend_70_30", "blend_50_50"):
        assert result["predictions"][name]["value"] is None
        assert (
            "HRRR: prepared guidance file is missing"
            in result["predictions"][name]["missing_reasons"]
        )
    assert result["errors"]["GFS"] == 8.0


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True, "280"])
def test_nonfinite_or_nonnumeric_source_is_explicitly_unavailable(invalid):
    result = compare_hour(_hour(hrrr=invalid), {"value": 282.0, "unit": "K"})
    assert result["predictions"]["HRRR"]["value"] is None
    assert result["predictions"]["HRRR"]["missing_reasons"]
    assert result["predictions"]["blend_50_50"]["value"] is None
    assert result["paired_sample"] is False


@pytest.mark.parametrize("observation", [None, {"value": float("nan"), "unit": "K"}])
def test_missing_observation_keeps_predictions_but_produces_no_errors(observation):
    result = compare_hour(_hour(), observation)
    assert result["predictions"]["blend_50_50"]["value"] == 285.0
    assert result["errors"] == dict.fromkeys(PREDICTION_KEYS)
    assert result["exclusion_reasons"] == ["observation_missing"]


@pytest.mark.parametrize("target", ["HRRR", "control", "observation"])
def test_incompatible_units_are_rejected(target):
    hour = _hour()
    observation = {"value": 282.0, "unit": "K"}
    temperature = (
        hour["sources"][0]["temperature"]
        if target == "HRRR"
        else hour["temperature"]
        if target == "control"
        else observation
    )
    temperature["unit"] = "degC"
    with pytest.raises(ValueError, match="units K"):
        compare_hour(hour, observation)


def test_wrong_models_weights_and_inconsistent_control_are_rejected():
    hour = _hour()
    hour["sources"][1]["model"] = "HRRR"
    with pytest.raises(ValueError, match="exactly the saved control recipe"):
        compare_hour(hour, None)
    hour = _hour()
    hour["sources"][0]["weight"] = 0.5
    with pytest.raises(ValueError, match="saved control recipe"):
        compare_hour(hour, None)
    with pytest.raises(ValueError, match="disagrees"):
        compare_hour(_hour(control=283.001), None)


def test_small_roundoff_never_replaces_saved_control():
    retained = 283.0 + 5e-11
    assert (
        compare_hour(_hour(control=retained), None)["predictions"]["blend_70_30"]["value"]
        == retained
    )


def test_finite_value_with_missing_reason_is_rejected():
    hour = _hour()
    hour["sources"][0]["missing_reasons"] = ["no guidance"]
    with pytest.raises(ValueError, match="contradicts declared missingness"):
        compare_hour(hour, None)


def test_aggregates_use_identical_pairs_and_independent_mixed_sign_arithmetic():
    first = compare_hour(_hour(), {"value": 282.0, "unit": "K"})
    second = compare_hour(
        _hour(horizon=7, hrrr=284.0, gfs=278.0, control=282.2), {"value": 285.0, "unit": "K"}
    )
    missing = _hour(horizon=19)
    del missing["sources"][0]["temperature"]
    third = compare_hour(missing, {"value": 282.0, "unit": "K"})
    summary = summarize([first, second, third])
    all_rows = summary["all"]
    assert (all_rows["row_count"], all_rows["paired_sample_count"], all_rows["excluded_count"]) == (
        3,
        2,
        1,
    )
    assert all_rows["exclusion_counts"] == {"HRRR_missing": 1, "blend_50_50_missing": 1}
    expected = {
        "HRRR": (1.5, -1.5, math.sqrt(2.5)),
        "GFS": (7.5, 0.5, math.sqrt(56.5)),
        "blend_70_30": (1.9, -0.9, math.sqrt(4.42)),
        "blend_50_50": (3.5, -0.5, math.sqrt(12.5)),
    }
    for name, (mae, bias, rmse) in expected.items():
        metrics = all_rows["predictions"][name]
        assert metrics["sample_count"] == 2
        assert (metrics["mae"], metrics["mean_bias"], metrics["rmse"]) == pytest.approx(
            (mae, bias, rmse)
        )
    assert summary["1-6"]["paired_sample_count"] == 1
    assert summary["7-18"]["paired_sample_count"] == 1
    assert summary["19-36"]["paired_sample_count"] == 0
    assert summary["19-36"]["excluded_count"] == 1
    assert summary["19-36"]["predictions"]["GFS"]["mae"] is None
    assert "do not establish predictive skill" in summary["interpretation"]


def test_zero_samples_is_null_and_single_sample_is_descriptive_without_ranking():
    empty = summarize([])
    assert empty["all"]["paired_sample_count"] == 0
    for name in PREDICTION_KEYS:
        assert empty["all"]["predictions"][name] == {
            "sample_count": 0,
            "mae": None,
            "mean_bias": None,
            "rmse": None,
            "unit": "K",
        }
    one = summarize([compare_hour(_hour(), {"value": 282.0, "unit": "K"})])
    assert one["all"]["predictions"]["HRRR"]["sample_count"] == 1
    assert one["all"]["predictions"]["HRRR"]["mae"] == 2.0
    assert "rank" not in one["all"]["predictions"]["HRRR"]


def test_summary_rejects_nonfinite_errors_in_a_claimed_complete_pair():
    row = compare_hour(_hour(), {"value": 282.0, "unit": "K"})
    row["errors"]["GFS"] = float("nan")
    with pytest.raises(ValueError, match="finite errors"):
        summarize([row])


def _shadow_case():
    shadow = ModelDefinition(
        model_id="SYNTH_SHADOW",
        provider="fixture",
        family="synthetic",
        lineage=("fixture-v1",),
        domain="fixture grid",
        supported_fields=("air_temperature_2m",),
        cycle_hours=(0, 6, 12, 18),
        supported_leads=tuple(range(49)),
        status="shadow",
    )
    recipe = Recipe(
        name="three_model_comparison",
        version="1",
        contributors=tuple(
            RecipeContributor(model=model, weight=weight)
            for model, weight in (("HRRR", 0.5), ("GFS", 0.25), ("SYNTH_SHADOW", 0.25))
        ),
    )
    config = ContributorConfiguration(
        models=(*DEFAULT_CONFIGURATION.models, shadow),
        control_recipe=DEFAULT_CONFIGURATION.control_recipe,
        comparison_recipes=(*DEFAULT_CONFIGURATION.comparison_recipes, recipe),
    )
    hour = _hour()
    hour["shadow_sources"] = [
        {
            "model": "SYNTH_SHADOW",
            "weight": 0.0,
            "temperature": {"value": 300.0, "unit": "K"},
            "missing_reasons": [],
        }
    ]
    return config, hour


def test_named_three_contributor_recipe_and_metrics_preserve_existing_results():
    config, hour = _shadow_case()
    observed = {"value": 282.0, "unit": "K"}
    before = compare_hour(_hour(), observed)
    result = compare_hour(hour, observed, configuration=config)
    for key in before["predictions"]:
        assert result["predictions"][key] == before["predictions"][key]
        assert result["errors"][key] == before["errors"][key]
    assert result["predictions"]["three_model_comparison"]["value"] == 287.5
    assert result["errors"]["three_model_comparison"] == 5.5
    assert result["errors"]["SYNTH_SHADOW"] == 18.0
    metrics = summarize([result])["1-6"]["predictions"]["three_model_comparison"]
    assert metrics == {"sample_count": 1, "mae": 5.5, "mean_bias": 5.5, "rmse": 5.5, "unit": "K"}


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_ineligible_shadow_is_explicit_and_does_not_change_control(missing):
    config, hour = _shadow_case()
    if missing:
        hour["shadow_sources"][0]["temperature"]["value"] = None
    result = compare_hour(
        hour,
        {"value": 282.0, "unit": "K"},
        configuration=config,
        ineligible_models={}
        if missing
        else {"SYNTH_SHADOW": ["source_cycle_after_forecast_issuance"]},
    )
    assert result["predictions"]["blend_70_30"]["value"] == 283.0
    assert result["errors"]["blend_70_30"] == 1.0
    assert result["predictions"]["blend_50_50"]["value"] == 285.0
    assert result["errors"]["SYNTH_SHADOW"] is None
    assert result["errors"]["three_model_comparison"] is None
    assert result["exclusion_reasons"]
    assert summarize([result])["all"]["paired_sample_count"] == 0


def test_summary_refuses_to_pool_changed_recipe_definitions_under_same_key():
    first = compare_hour(_hour(), {"value": 282.0, "unit": "K"})
    second = copy.deepcopy(first)
    second["prediction_definitions"]["blend_50_50"]["version"] = "different"
    with pytest.raises(ValueError, match="different recipe definitions"):
        summarize([first, second])


def test_history_without_shadow_is_excluded_from_joint_shadow_comparison_explicitly():
    config, hour = _shadow_case()
    before = compare_hour(_hour(), {"value": 282.0, "unit": "K"})
    after = compare_hour(hour, {"value": 282.0, "unit": "K"}, configuration=config)
    metrics = summarize([before, after])["all"]
    assert metrics["row_count"] == 2 and metrics["paired_sample_count"] == 1
    assert metrics["excluded_count"] == 1
    assert metrics["exclusion_counts"] == {
        "SYNTH_SHADOW_missing": 1,
        "three_model_comparison_missing": 1,
    }


def test_native_ifs_hour_and_missing_intermediate_hour_use_identical_pairs_for_every_model():
    configuration = ContributorConfiguration(
        models=(*DEFAULT_CONFIGURATION.models, RAP_MODEL_DEFINITION, IFS_MODEL_DEFINITION),
        control_recipe=DEFAULT_CONFIGURATION.control_recipe,
        comparison_recipes=DEFAULT_CONFIGURATION.comparison_recipes,
    )
    native = _hour(horizon=3)
    native["valid_time"] = "2026-01-01T15:00:00Z"
    native["sources"][0]["source_lead_hours"] = 9
    native["sources"][1]["source_lead_hours"] = 15
    native["shadow_sources"] = [
        {
            "model": model,
            "cycle": "2026-01-01T12:00:00Z",
            "source_lead_hours": 3,
            "valid_time": native["valid_time"],
            "weight": 0.0,
            "temperature": {"value": value, "unit": "K"},
            "missing_reasons": [],
        }
        for model, value in (("RAP", 286.0), ("IFS", 281.0))
    ]
    intermediate = copy.deepcopy(native)
    intermediate.update(horizon_hours=2, valid_time="2026-01-01T14:00:00Z")
    for source in intermediate["sources"]:
        source["source_lead_hours"] -= 1
    for source in intermediate["shadow_sources"]:
        source.update(valid_time=intermediate["valid_time"], source_lead_hours=2)
    intermediate["shadow_sources"][1].update(
        source_lead_hours=None,
        temperature={"value": None, "unit": "K"},
        missing_reasons=["IFS: no native three-hourly guidance for this valid time"],
    )
    original = copy.deepcopy((native, intermediate))
    result = compare_hour(native, {"value": 282.0, "unit": "K"}, configuration=configuration)
    missing = compare_hour(intermediate, {"value": 287.0, "unit": "K"}, configuration=configuration)
    expected = {
        "HRRR": -2.0,
        "GFS": 8.0,
        "RAP": 4.0,
        "IFS": -1.0,
        "blend_70_30": 1.0,
        "blend_50_50": 3.0,
    }
    assert result["errors"] == expected
    assert result["paired_sample"] is True
    assert missing["predictions"]["blend_70_30"]["value"] == 283.0
    assert missing["errors"]["blend_70_30"] == -4.0
    assert missing["predictions"]["IFS"]["value"] is None
    assert missing["predictions"]["IFS"]["missing_reasons"] == [
        "IFS: no native three-hourly guidance for this valid time"
    ]
    assert missing["errors"]["IFS"] is None
    assert missing["exclusion_reasons"] == ["IFS_missing"]
    summary = summarize([result, missing])["all"]
    assert summary["row_count"] == 2
    assert summary["paired_sample_count"] == summary["excluded_count"] == 1
    assert summary["exclusion_counts"] == {"IFS_missing": 1}
    for name, error in expected.items():
        assert summary["predictions"][name] == {
            "sample_count": 1,
            "mae": abs(error),
            "mean_bias": error,
            "rmse": abs(error),
            "unit": "K",
        }
    assert (native, intermediate) == original
