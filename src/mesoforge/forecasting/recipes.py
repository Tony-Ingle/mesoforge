"""Named scalar recipes evaluated without silently changing weights or membership."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from mesoforge.catalog.contributors import (
    DEFAULT_MODEL_DEFINITIONS,
    SURFACE_MODEL_FIELDS,
    ModelDefinition,
)
from mesoforge.forecasting.provisional_policy import PROVISIONAL_MULTIMODEL_POLICY
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
    control_recipe: Recipe | None
    comparison_recipes: tuple[Recipe, ...] = ()
    field_policy_family: Literal["mesoforge.provisional-multimodel-120h.v1"] | None = None

    def model_map(self) -> dict[str, ModelDefinition]:
        return {model.model_id: model for model in self.models}

    def recipes(self) -> tuple[Recipe, ...]:
        return (
            (self.control_recipe, *self.comparison_recipes)
            if self.control_recipe is not None
            else ()
        )

    @model_serializer(mode="wrap")
    def _historical_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        if self.field_policy_family is None:
            payload.pop("field_policy_family", None)
        return payload

    @model_validator(mode="after")
    def _references(self) -> ContributorConfiguration:
        definitions = self.model_map()
        if not definitions or len(definitions) != len(self.models):
            raise ValueError("models must contain unique model IDs and must not be empty")
        if self.field_policy_family is not None:
            if self.control_recipe is not None or self.comparison_recipes:
                raise ValueError("Provisional field policies cannot claim fixed scalar recipes")
            if set(definitions) != {"HRRR", "RAP", "GFS", "IFS", "NBM"}:
                raise ValueError(
                    "Provisional contributor configuration must declare all five sources"
                )
            return self
        if self.control_recipe is None:
            raise ValueError("Legacy contributor configuration requires its control recipe")
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
    if configuration.field_policy_family is not None:
        return configuration
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
    if configuration.field_policy_family is not None:
        return configuration
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


_PROVISIONAL_FIELDS = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "cloud_area_fraction",
    "liquid_equivalent_precipitation_amount",
)
_PROVISIONAL_CAPABILITIES: tuple[
    tuple[str, str, str, str, tuple[int, ...], tuple[int, ...], Literal["projected", "geographic"]],
    ...,
] = (
    ("HRRR", "NOAA/NCEP", "HRRR", "CONUS", tuple(range(24)), tuple(range(49)), "projected"),
    ("RAP", "NOAA/NCEP", "RAP/WRF-ARW", "CONUS", tuple(range(24)), tuple(range(52)), "projected"),
    (
        "GFS",
        "NOAA/NCEP",
        "GFS",
        "global",
        (0, 6, 12, 18),
        (*range(121), *range(123, 385, 3)),
        "geographic",
    ),
    (
        "IFS",
        "ECMWF",
        "IFS open-data oper/fc",
        "global",
        (0, 6, 12, 18),
        (*range(0, 145, 3), *range(150, 361, 6)),
        "geographic",
    ),
    (
        "NBM",
        "NOAA/NCEP",
        "National Blend of Models",
        "CONUS",
        tuple(range(24)),
        (*range(1, 49), *range(51, 193, 3), *range(198, 265, 6)),
        "projected",
    ),
)
PROVISIONAL_CONFIGURATION = ContributorConfiguration(
    models=tuple(
        ModelDefinition(
            model_id=model,
            provider=provider,
            family=family,
            domain=domain,
            cycle_hours=cycles,
            supported_leads=leads,
            grid_type=grid,
            supported_fields=(
                *_PROVISIONAL_FIELDS,
                *(
                    ("wind_gust_10m_interval_maximum",)
                    if model == "IFS"
                    else ("wind_gust_10m", "liquid_equivalent_precipitation_amount_1h")
                ),
            ),
            status="active",
        )
        for model, provider, family, domain, cycles, leads, grid in _PROVISIONAL_CAPABILITIES
    ),
    control_recipe=None,
    field_policy_family=PROVISIONAL_MULTIMODEL_POLICY,
)
