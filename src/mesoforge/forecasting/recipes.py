"""Named scalar recipes evaluated without silently changing weights or membership."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mesoforge.catalog.contributors import (
    DEFAULT_MODEL_DEFINITIONS,
    SURFACE_MODEL_FIELDS,
    ModelDefinition,
)
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar


class RecipeContributor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: str = Field(pattern=r"^[A-Z][A-Z0-9_-]*$")
    weight: float = Field(ge=0, le=1, allow_inf_nan=False)


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    field: str = "air_temperature_2m"
    contributors: tuple[RecipeContributor, ...]
    missing_policy: Literal["require_all"] = "require_all"
    output_key: str | None = None

    @field_validator("name", "version", "field", "output_key")
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError(
                "recipe identity/field must be nonblank without surrounding whitespace"
            )
        return value

    @model_validator(mode="after")
    def _weights(self) -> Recipe:
        if not self.contributors:
            raise ValueError("a recipe must contain at least one contributor")
        models = [contributor.model for contributor in self.contributors]
        if len(models) != len(set(models)):
            raise ValueError("a recipe cannot contain duplicate model IDs")
        if abs(math.fsum(contributor.weight for contributor in self.contributors) - 1.0) > 1e-12:
            raise ValueError("recipe weights must sum to one within 1e-12")
        return self

    @property
    def result_key(self) -> str:
        return self.output_key if self.output_key is not None else self.name


class ContributorConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    models: tuple[ModelDefinition, ...]
    control_recipe: Recipe
    comparison_recipes: tuple[Recipe, ...] = ()

    def model_map(self) -> dict[str, ModelDefinition]:
        return {model.model_id: model for model in self.models}

    def recipes(self) -> tuple[Recipe, ...]:
        return (self.control_recipe, *self.comparison_recipes)

    @model_validator(mode="after")
    def _references(self) -> ContributorConfiguration:
        definitions = self.model_map()
        if not definitions or len(definitions) != len(self.models):
            raise ValueError("models must contain unique model IDs and must not be empty")
        identities = [(recipe.name, recipe.version) for recipe in self.recipes()]
        if len(identities) != len(set(identities)):
            raise ValueError("recipe name/version identities must be unique")
        output_keys = [*definitions, *(recipe.result_key for recipe in self.recipes())]
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("recipe output keys and model IDs must be unique")
        for recipe in self.recipes():
            if recipe.field != self.control_recipe.field:
                raise ValueError("comparison recipes must use the same field as the control")
            for contributor in recipe.contributors:
                definition = definitions.get(contributor.model)
                if definition is None:
                    raise ValueError(f"recipe references unregistered model {contributor.model}")
                if recipe.field not in definition.supported_fields:
                    raise ValueError(
                        f"{contributor.model} does not support recipe field {recipe.field}"
                    )
                if definition.status == "retired":
                    raise ValueError(f"retired model {contributor.model} cannot be in a new recipe")
                if recipe == self.control_recipe and definition.status != "active":
                    raise ValueError(f"control recipe requires active model {contributor.model}")
        return self


@dataclass(frozen=True, slots=True)
class RecipeEvaluation:
    value: float | None
    missing_models: tuple[str, ...]


def with_surface_fields(configuration: ContributorConfiguration) -> ContributorConfiguration:
    """Enable registered native fields without changing recipes, weights or lifecycle status."""
    return configuration.model_copy(
        update={
            "models": tuple(
                model.model_copy(update={"supported_fields": SURFACE_MODEL_FIELDS[model.model_id]})
                if model.model_id in SURFACE_MODEL_FIELDS
                else model
                for model in configuration.models
            )
        }
    )


def with_qpf_fields(configuration: ContributorConfiguration) -> ContributorConfiguration:
    """Register retained HRRR/GFS interval QPF without changing any recipe weights."""
    field = "liquid_equivalent_precipitation_amount_1h"
    return configuration.model_copy(
        update={
            "models": tuple(
                model.model_copy(update={"supported_fields": (*model.supported_fields, field)})
                if model.model_id in {"HRRR", "GFS"} and field not in model.supported_fields
                else model
                for model in configuration.models
            )
        }
    )


def evaluate_recipe(recipe: Recipe, values: Mapping[str, float | None]) -> RecipeEvaluation:
    """Blend normalized finite scalars; missing contributors never change the row.

    Values must already have consistent field/unit/time semantics. Extra values,
    including shadow guidance not named by this recipe, have no influence.
    """
    missing: list[str] = []
    contributions: list[Contribution] = []
    for contributor in recipe.contributors:
        value = values.get(contributor.model)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("recipe values must be normalized numeric scalars, not booleans")
        if value is None or not math.isfinite(value):
            missing.append(contributor.model)
            continue
        contributions.append(Contribution(contributor.model, value, contributor.weight))
    if missing:
        return RecipeEvaluation(value=None, missing_models=tuple(missing))
    return RecipeEvaluation(
        value=blend_scalar(tuple(contributions)).blended_value, missing_models=()
    )


DEFAULT_CONFIGURATION = ContributorConfiguration(
    models=DEFAULT_MODEL_DEFINITIONS,
    control_recipe=Recipe(
        name="temperature_control_v1",
        version="1",
        output_key="blend_70_30",
        contributors=(
            RecipeContributor(model="HRRR", weight=0.7),
            RecipeContributor(model="GFS", weight=0.3),
        ),
    ),
    comparison_recipes=(
        Recipe(
            name="temperature_equal_v1",
            version="1",
            output_key="blend_50_50",
            contributors=(
                RecipeContributor(model="HRRR", weight=0.5),
                RecipeContributor(model="GFS", weight=0.5),
            ),
        ),
    ),
)
