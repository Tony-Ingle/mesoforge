"""Contract tests for mesoforge.catalog.variables.VariableDefinition
(Task 4, plan Section 4.5).

RED: written before VariableDefinition exists.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.catalog.variables import VariableDefinition


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        variable_id="air-temperature-2m",
        standard_name="air_temperature",
        canonical_unit_id="K",
        dtype="float32",
        temporal_semantics="instantaneous",
        spatial_support="cell_mean",
        vertical_definition_id="height-agl-2m",
        allowed_dimension_variants=(("lead_time", "y", "x"),),
        missing_value_policy="nan_with_quality_mask",
        interval_required=False,
    )
    kwargs.update(overrides)
    return kwargs


class TestVariableDefinition:
    def test_valid_variable_constructs(self) -> None:
        variable = VariableDefinition(**_base_kwargs())
        assert variable.variable_id == "air-temperature-2m"

    def test_rejects_unknown_dimension_variant(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(**_base_kwargs(allowed_dimension_variants=(("bogus",),)))

    @pytest.mark.parametrize(
        "variant",
        [
            ("lead_time", "location"),
            ("lead_time", "y", "x"),
            ("member", "lead_time", "location"),
            ("member", "lead_time", "y", "x"),
            ("lead_time", "level", "location"),
            ("lead_time", "level", "y", "x"),
        ],
    )
    def test_accepts_each_allowlisted_variant(self, variant: tuple[str, ...]) -> None:
        variable = VariableDefinition(**_base_kwargs(allowed_dimension_variants=(variant,)))
        assert variant in variable.allowed_dimension_variants

    def test_member_must_be_first_when_present(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(
                **_base_kwargs(allowed_dimension_variants=(("lead_time", "member", "location"),))
            )

    def test_level_must_precede_spatial_dims(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(
                **_base_kwargs(allowed_dimension_variants=(("lead_time", "y", "x", "level"),))
            )

    def test_interval_required_true_for_accumulation(self) -> None:
        variable = VariableDefinition(
            **_base_kwargs(temporal_semantics="accumulation", interval_required=True)
        )
        assert variable.interval_required is True

    def test_interval_required_must_be_true_for_accumulation(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(
                **_base_kwargs(temporal_semantics="accumulation", interval_required=False)
            )

    def test_interval_required_must_be_false_for_instantaneous(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(
                **_base_kwargs(temporal_semantics="instantaneous", interval_required=True)
            )

    def test_probability_requires_probability_event(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(
                **_base_kwargs(temporal_semantics="probability", interval_required=True)
            )

    def test_probability_with_event_is_valid(self) -> None:
        variable = VariableDefinition(
            **_base_kwargs(
                temporal_semantics="probability",
                interval_required=True,
                probability_event={
                    "operator": "greater_than_or_equal",
                    "threshold": 0.5,
                    "threshold_unit_id": "m",
                    "population": "cell_mean",
                    "processing_status": "raw",
                },
            )
        )
        assert variable.probability_event is not None

    def test_probability_event_forbidden_outside_probability(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(
                **_base_kwargs(
                    probability_event={
                        "operator": "greater_than_or_equal",
                        "threshold": 0.5,
                        "threshold_unit_id": "m",
                        "population": "cell_mean",
                        "processing_status": "raw",
                    }
                )
            )

    def test_valid_min_max_must_be_finite(self) -> None:
        with pytest.raises(ValidationError):
            VariableDefinition(**_base_kwargs(valid_min=float("nan")))

    def test_schema_version_pinned(self) -> None:
        variable = VariableDefinition(**_base_kwargs())
        assert variable.schema_version == "variable-definition.v1"

    def test_frozen(self) -> None:
        variable = VariableDefinition(**_base_kwargs())
        with pytest.raises(ValidationError):
            variable.dtype = "float64"  # type: ignore[misc]
