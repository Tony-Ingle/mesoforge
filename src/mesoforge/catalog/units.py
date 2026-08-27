"""One application-owned Pint unit registry and VerticalDefinition
(plan Section 4.3).

Only controlled unit IDs are accepted -- callers may never pass an
arbitrary Pint-parseable string through contract validation. Persist the
controlled ID everywhere, never Pint's display formatting.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Literal

import pint
from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.errors import InvalidIdentifier

# Controlled unit IDs -> the exact Pint unit string used to construct them.
_CONTROLLED_UNITS: dict[str, str] = {
    "K": "kelvin",
    "degC": "degC",
    "m/s": "meter / second",
    "kg/m^2": "kilogram / meter ** 2",
    "percent": "percent",
    "dimensionless": "dimensionless",
    "m": "meter",
    "Pa": "pascal",
}


@lru_cache(maxsize=1)
def _registry() -> Any:
    return pint.UnitRegistry(autoconvert_offset_to_baseunit=True)


def parse_registered_unit(unit_id: str) -> pint.Unit:
    """Parse a controlled unit ID into a Pint unit. Raises InvalidIdentifier
    for anything not in the controlled unit ID set, even if Pint itself
    could parse it as a free-form string."""
    if unit_id not in _CONTROLLED_UNITS:
        raise InvalidIdentifier(
            f"{unit_id!r} is not a controlled MesoForge unit ID; known IDs are "
            f"{sorted(_CONTROLLED_UNITS)}"
        )
    unit: pint.Unit = _registry().Unit(_CONTROLLED_UNITS[unit_id])
    return unit


def assert_compatible(from_unit_id: str, to_unit_id: str) -> None:
    from_unit = parse_registered_unit(from_unit_id)
    to_unit = parse_registered_unit(to_unit_id)
    quantity = _registry().Quantity(1.0, from_unit)
    try:
        quantity.to(to_unit)
    except pint.DimensionalityError as exc:
        raise ValueError(
            f"incompatible units: {from_unit_id!r} cannot convert to {to_unit_id!r}"
        ) from exc


def convert(values: float | Sequence[float], from_unit_id: str, to_unit_id: str) -> object:
    """Convert ``values`` from one controlled unit ID to another. Accepts a
    scalar or a sequence; returns the same shape via Pint's magnitude."""
    from_unit = parse_registered_unit(from_unit_id)
    to_unit = parse_registered_unit(to_unit_id)
    quantity = _registry().Quantity(values, from_unit)
    try:
        converted = quantity.to(to_unit)
    except pint.DimensionalityError as exc:
        raise ValueError(
            f"incompatible units: {from_unit_id!r} cannot convert to {to_unit_id!r}"
        ) from exc
    return converted.magnitude


CoordinateType = Literal["surface", "height_above_ground", "pressure", "model_level"]
PositiveDirection = Literal["up", "down"]

_PHYSICAL_LEVEL_TYPES: frozenset[str] = frozenset(
    {"height_above_ground", "pressure", "model_level"}
)


class VerticalDefinition(BaseModel):
    """Section 4.3: vertical coordinate definition. ``value``/``unit_id``
    are required together for non-surface physical levels."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "vertical-definition.v1"
    vertical_definition_id: str
    coordinate_type: CoordinateType
    value: float | None = None
    unit_id: str | None = None
    positive_direction: PositiveDirection | None = None

    @model_validator(mode="after")
    def _check_value_and_unit(self) -> VerticalDefinition:
        if self.coordinate_type in _PHYSICAL_LEVEL_TYPES:
            if self.value is None or self.unit_id is None:
                raise ValueError(
                    f"coordinate_type={self.coordinate_type!r} requires both 'value' and 'unit_id'"
                )
        else:
            if self.value is not None or self.unit_id is not None:
                raise ValueError(
                    f"coordinate_type={self.coordinate_type!r} must not set 'value' or 'unit_id'"
                )
        if (self.value is None) != (self.unit_id is None):
            raise ValueError("'value' and 'unit_id' must be supplied together")
        return self
