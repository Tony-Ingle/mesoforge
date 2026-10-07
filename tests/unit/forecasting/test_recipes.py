"""Configured contributor membership, immutable metadata, and scalar recipe arithmetic."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mesoforge.catalog.contributors import ModelDefinition
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    Recipe,
    RecipeContributor,
    evaluate_recipe,
)


def _third(*, status="shadow", **updates):
    return ModelDefinition(
        **{
            "model_id": "THIRD",
            "provider": "synthetic",
            "family": "synthetic-family",
            "lineage": ("synthetic-parent",),
            "domain": "CONUS",
            "supported_fields": ("air_temperature_2m",),
            "cycle_hours": (0, 6, 12, 18),
            "supported_leads": tuple(range(49)),
            "status": status,
            **updates,
        }
    )


def _recipe(*, models=("HRRR", "GFS", "THIRD"), weights=(0.2, 0.3, 0.5), **updates):
    return Recipe(
        **{
            "name": "synthetic_three",
            "version": "1",
            "contributors": tuple(
                RecipeContributor(model=model, weight=weight)
                for model, weight in zip(models, weights, strict=True)
            ),
            **updates,
        }
    )


def _config(*, third=None, recipe=None):
    return ContributorConfiguration(
        models=(*DEFAULT_CONFIGURATION.models, third or _third()),
        control_recipe=DEFAULT_CONFIGURATION.control_recipe,
        comparison_recipes=(*DEFAULT_CONFIGURATION.comparison_recipes, recipe or _recipe()),
    )


def test_defaults_keep_approved_control_and_comparison_arithmetic():
    values = {"HRRR": 280.0, "GFS": 290.0, "THIRD": 999.0}
    assert evaluate_recipe(DEFAULT_CONFIGURATION.control_recipe, values).value == 283.0
    equal = DEFAULT_CONFIGURATION.comparison_recipes[0]
    assert evaluate_recipe(equal, values).value == 285.0
    assert DEFAULT_CONFIGURATION.control_recipe.result_key == "blend_70_30"
    assert equal.result_key == "blend_50_50"
    assert [row.model for row in DEFAULT_CONFIGURATION.control_recipe.contributors] == [
        "HRRR",
        "GFS",
    ]


def test_arbitrary_third_shadow_changes_only_named_comparison_and_roundtrips_metadata():
    config = _config()
    snapshot = config.model_dump(mode="json")
    parsed = ContributorConfiguration.model_validate_json(json.dumps(snapshot))
    assert parsed == config
    assert parsed.model_map()["THIRD"].status == "shadow"
    assert parsed.model_map()["THIRD"].lineage == ("synthetic-parent",)
    assert parsed.recipes()[-1].result_key == "synthetic_three"
    values = {"HRRR": 280.0, "GFS": 290.0, "THIRD": 300.0}
    assert evaluate_recipe(parsed.recipes()[-1], values).value == 293.0
    assert evaluate_recipe(parsed.control_recipe, values).value == 283.0
    assert config.model_map()["THIRD"].status == "shadow"
    with pytest.raises(ValidationError, match="frozen"):
        config.model_map()["THIRD"].status = "active"


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_shadow_never_renormalizes_or_breaks_control(missing):
    config = _config()
    values = {"HRRR": 280.0, "GFS": 290.0, "THIRD": missing}
    comparison = evaluate_recipe(config.recipes()[-1], values)
    assert comparison.value is None
    assert comparison.missing_models == ("THIRD",)
    assert evaluate_recipe(config.control_recipe, values).value == 283.0
    assert [row.weight for row in config.recipes()[-1].contributors] == [0.2, 0.3, 0.5]


def test_absent_models_report_all_required_missing_members_in_recipe_order():
    result = evaluate_recipe(_recipe(), {"GFS": 290.0})
    assert result.value is None
    assert result.missing_models == ("HRRR", "THIRD")


def test_expired_short_range_guidance_does_not_promote_gfs_to_temperature_control():
    # Native HRRR expiration is missing guidance, not an approved long-range policy.
    recipe = DEFAULT_CONFIGURATION.control_recipe
    before = recipe.model_dump()
    result = evaluate_recipe(recipe, {"HRRR": None, "GFS": 290.0})
    assert result.value is None and result.missing_models == ("HRRR",)
    assert recipe.model_dump() == before


@pytest.mark.parametrize("value", [True, "280"])
def test_recipe_rejects_nonnumeric_values(value):
    with pytest.raises(ValueError, match="numeric scalars"):
        evaluate_recipe(DEFAULT_CONFIGURATION.control_recipe, {"HRRR": value, "GFS": 290.0})


@pytest.mark.parametrize(
    "weights",
    [(0.2, 0.3, 0.4), (-0.1, 0.6, 0.5), (float("nan"), 0.5, 0.5), (float("inf"), 0.0, 0.0)],
)
def test_invalid_weights_are_rejected(weights):
    with pytest.raises(ValidationError):
        _recipe(weights=weights)


def test_duplicate_contributors_empty_recipes_and_renormalization_are_rejected():
    with pytest.raises(ValidationError, match="duplicate model IDs"):
        _recipe(models=("HRRR", "HRRR", "THIRD"))
    with pytest.raises(ValidationError, match="at least one"):
        _recipe(models=(), weights=())
    with pytest.raises(ValidationError, match="require_all"):
        _recipe(missing_policy="renormalize")


@pytest.mark.parametrize("status", ["shadow", "evaluated", "deprecated", "retired"])
def test_inactive_model_cannot_enter_control(status):
    with pytest.raises(ValidationError, match="active model|retired model"):
        ContributorConfiguration(
            models=(*DEFAULT_CONFIGURATION.models, _third(status=status)),
            control_recipe=_recipe(),
        )


@pytest.mark.parametrize("status", ["shadow", "evaluated", "active", "deprecated"])
def test_comparison_accepts_explicit_nonretired_status_without_promoting(status):
    config = _config(third=_third(status=status))
    assert config.model_map()["THIRD"].status == status
    assert (
        evaluate_recipe(config.recipes()[-1], {"HRRR": 280.0, "GFS": 290.0, "THIRD": 300.0}).value
        == 293.0
    )
    assert config.model_map()["THIRD"].status == status


def test_retired_registration_is_retained_but_cannot_be_used_by_a_new_recipe():
    retired = _third(status="retired")
    config = ContributorConfiguration(
        models=(*DEFAULT_CONFIGURATION.models, retired),
        control_recipe=DEFAULT_CONFIGURATION.control_recipe,
    )
    assert config.model_map()["THIRD"].status == "retired"
    with pytest.raises(ValidationError, match="retired model"):
        _config(third=retired)


def test_recipe_references_fields_and_output_identities_are_validated():
    with pytest.raises(ValidationError, match="unregistered model"):
        ContributorConfiguration(
            models=DEFAULT_CONFIGURATION.models,
            control_recipe=DEFAULT_CONFIGURATION.control_recipe,
            comparison_recipes=(_recipe(),),
        )
    with pytest.raises(ValidationError, match="does not support recipe field"):
        _config(third=_third(supported_fields=("another_field",)))
    with pytest.raises(ValidationError, match="same field"):
        _config(recipe=_recipe(field="another_field"))
    with pytest.raises(ValidationError, match="output keys"):
        _config(recipe=_recipe(output_key="HRRR"))
    with pytest.raises(ValidationError, match="output keys"):
        _config(recipe=_recipe(output_key="blend_50_50"))
    with pytest.raises(ValidationError, match="name/version identities"):
        ContributorConfiguration(
            models=DEFAULT_CONFIGURATION.models,
            control_recipe=DEFAULT_CONFIGURATION.control_recipe,
            comparison_recipes=(DEFAULT_CONFIGURATION.control_recipe,),
        )
    with pytest.raises(ValidationError, match="unique model IDs"):
        ContributorConfiguration(
            models=(*DEFAULT_CONFIGURATION.models, DEFAULT_CONFIGURATION.models[0]),
            control_recipe=DEFAULT_CONFIGURATION.control_recipe,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"model_id": "lowercase"},
        {"provider": " "},
        {"cycle_hours": (0, 24)},
        {"cycle_hours": (6, 0)},
        {"cycle_hours": (True,)},
        {"supported_leads": (-1, 0)},
        {"supported_leads": (0, 0)},
        {"supported_fields": ()},
        {"supported_fields": ("air_temperature_2m", "air_temperature_2m")},
        {"lineage": ("",)},
    ],
)
def test_invalid_model_metadata_is_rejected(updates):
    with pytest.raises(ValidationError):
        _third(**updates)


def test_adapter_capability_defaults_do_not_expand_provider_or_field_scope():
    for definition in DEFAULT_CONFIGURATION.models:
        assert definition.cycle_hours == (0, 6, 12, 18)
        assert definition.supported_leads == tuple(range(49))
        assert definition.supported_fields == ("air_temperature_2m",)
    assert DEFAULT_CONFIGURATION.model_map()["HRRR"].grid_type == "projected"
    assert DEFAULT_CONFIGURATION.model_map()["GFS"].grid_type == "geographic"
