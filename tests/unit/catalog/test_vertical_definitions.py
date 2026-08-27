"""Unit tests for mesoforge.catalog.vertical definitions (Task 4, plan
Section 4.3).

RED: written before VerticalDefinition exists.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mesoforge.catalog.units import VerticalDefinition


class TestVerticalDefinition:
    def test_surface_requires_no_value_or_unit(self) -> None:
        definition = VerticalDefinition(
            vertical_definition_id="surface",
            coordinate_type="surface",
        )
        assert definition.value is None
        assert definition.unit_id is None

    def test_height_above_ground_requires_value_and_unit(self) -> None:
        with pytest.raises(ValidationError, match="value"):
            VerticalDefinition(
                vertical_definition_id="height-agl-2m",
                coordinate_type="height_above_ground",
            )

    def test_height_above_ground_with_value_and_unit_is_valid(self) -> None:
        definition = VerticalDefinition(
            vertical_definition_id="height-agl-2m",
            coordinate_type="height_above_ground",
            value=2.0,
            unit_id="m",
        )
        assert definition.value == 2.0
        assert definition.unit_id == "m"

    def test_pressure_requires_value_and_unit(self) -> None:
        with pytest.raises(ValidationError, match="value"):
            VerticalDefinition(
                vertical_definition_id="pressure-850",
                coordinate_type="pressure",
                unit_id="Pa",
            )

    def test_value_without_unit_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VerticalDefinition(
                vertical_definition_id="height-agl-2m",
                coordinate_type="height_above_ground",
                value=2.0,
            )

    def test_optional_positive_direction(self) -> None:
        definition = VerticalDefinition(
            vertical_definition_id="pressure-850",
            coordinate_type="pressure",
            value=850.0,
            unit_id="Pa",
            positive_direction="down",
        )
        assert definition.positive_direction == "down"

    def test_rejects_invalid_positive_direction(self) -> None:
        with pytest.raises(ValidationError):
            VerticalDefinition(
                vertical_definition_id="pressure-850",
                coordinate_type="pressure",
                value=850.0,
                unit_id="Pa",
                positive_direction="sideways",
            )

    def test_schema_version_pinned(self) -> None:
        definition = VerticalDefinition(vertical_definition_id="surface", coordinate_type="surface")
        assert definition.schema_version == "vertical-definition.v1"

    def test_frozen(self) -> None:
        definition = VerticalDefinition(vertical_definition_id="surface", coordinate_type="surface")
        with pytest.raises(ValidationError):
            definition.coordinate_type = "pressure"  # type: ignore[misc]
