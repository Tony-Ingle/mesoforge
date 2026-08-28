"""Variable definition contract (plan Section 4.5).

Dimension-variant allowlist, interval requirements by temporal
semantics, and the probability-event sub-contract are all enforced here.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

DType = Literal["float32", "float64", "int16", "int32", "uint8", "bool", "str"]
MissingValuePolicy = Literal[
    "nan_with_quality_mask", "explicit_fill_with_quality_mask", "not_applicable"
]
ProbabilityOperator = Literal[
    "greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal", "equal_to"
]
ProbabilityProcessingStatus = Literal["raw", "calibrated", "blended"]

_ALLOWED_DIMENSION_VARIANTS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("lead_time", "location"),
        ("lead_time", "y", "x"),
        ("member", "lead_time", "location"),
        ("member", "lead_time", "y", "x"),
        ("lead_time", "level", "location"),
        ("lead_time", "level", "y", "x"),
    }
)

_INTERVAL_REQUIRED_SEMANTICS: frozenset[str] = frozenset(
    {"accumulation", "average", "minimum", "maximum", "probability"}
)


class ProbabilityEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operator: ProbabilityOperator
    threshold: float
    threshold_unit_id: str
    population: str
    processing_status: ProbabilityProcessingStatus


class VariableDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["variable-definition.v1"] = "variable-definition.v1"
    variable_id: str
    standard_name: str
    canonical_unit_id: str
    dtype: DType
    temporal_semantics: str
    spatial_support: str
    vertical_definition_id: str
    allowed_dimension_variants: tuple[tuple[str, ...], ...]
    missing_value_policy: MissingValuePolicy
    valid_min: float | None = None
    valid_max: float | None = None
    interval_required: bool
    probability_event: ProbabilityEvent | None = None

    @model_validator(mode="after")
    def _check_dimension_variants(self) -> VariableDefinition:
        for variant in self.allowed_dimension_variants:
            if variant not in _ALLOWED_DIMENSION_VARIANTS:
                raise ValueError(
                    f"dimension variant {variant!r} is not in the allowed set: "
                    f"{sorted(_ALLOWED_DIMENSION_VARIANTS)}"
                )
        return self

    @model_validator(mode="after")
    def _check_interval_required(self) -> VariableDefinition:
        semantics_requires_interval = self.temporal_semantics in _INTERVAL_REQUIRED_SEMANTICS
        if semantics_requires_interval and not self.interval_required:
            raise ValueError(
                f"temporal_semantics={self.temporal_semantics!r} requires interval_required=True"
            )
        if not semantics_requires_interval and self.interval_required:
            raise ValueError(
                f"temporal_semantics={self.temporal_semantics!r} must not set "
                "interval_required=True"
            )
        return self

    @model_validator(mode="after")
    def _check_probability_event(self) -> VariableDefinition:
        if self.temporal_semantics == "probability" and self.probability_event is None:
            raise ValueError("temporal_semantics='probability' requires probability_event")
        if self.temporal_semantics != "probability" and self.probability_event is not None:
            raise ValueError(
                "probability_event is only permitted when temporal_semantics='probability'"
            )
        return self

    @model_validator(mode="after")
    def _check_valid_min_max_finite(self) -> VariableDefinition:
        for name, value in (("valid_min", self.valid_min), ("valid_max", self.valid_max)):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
        if self.valid_min is not None and self.valid_max is not None:
            if self.valid_min > self.valid_max:
                raise ValueError("valid_min must be <= valid_max")
        return self
