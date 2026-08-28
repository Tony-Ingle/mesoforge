"""Immutable grid definition contract (plan Section 4.4).

``grid_id`` is globally unique for a fixed, immutable definition; the
``definition_digest`` (JCS/SHA-256 over all fields except ``grid_id``)
detects any attempt to reuse an ID with different coordinates or
metadata. There is no separate mutable "revision" concept in Phase 0: a
changed grid always requires a new, distinct ``grid_id``.
"""

from __future__ import annotations

import math
from typing import Literal

import jcs
import pyproj
from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import Digest, GridId

CoordinateReference = Literal["cell_center", "cell_bounds"]
LongitudeConvention = Literal["minus_180_to_180", "zero_to_360"]
Orientation = Literal["x_east_y_north", "x_east_y_south", "other"]
SpatialSupport = Literal["point", "cell_mean", "cell_total", "categorical_cell"]

# Fields that participate in the definition digest (everything except
# grid_id itself, which is the mutable-looking label that must not be
# allowed to change identity).
_DIGEST_FIELDS: tuple[str, ...] = (
    "schema_version",
    "crs_wkt2",
    "shape_y",
    "shape_x",
    "x_coordinates",
    "y_coordinates",
    "coordinate_reference",
    "longitude_convention",
    "orientation",
    "orientation_note",
    "spatial_support",
    "domain_geometry_artifact_id",
    "terrain_artifact_id",
    "land_sea_mask_artifact_id",
)


def _is_finite_strictly_monotonic(values: tuple[float, ...]) -> bool:
    if any(not math.isfinite(v) for v in values):
        return False
    if len(values) < 2:
        return True
    increasing = all(b > a for a, b in zip(values, values[1:], strict=False))
    decreasing = all(b < a for a, b in zip(values, values[1:], strict=False))
    return increasing or decreasing


class GridDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["grid-definition.v1"] = "grid-definition.v1"
    grid_id: GridId
    crs_wkt2: str
    shape_y: int
    shape_x: int
    x_coordinates: tuple[float, ...]
    y_coordinates: tuple[float, ...]
    coordinate_reference: CoordinateReference
    longitude_convention: LongitudeConvention
    orientation: Orientation
    orientation_note: str | None = None
    spatial_support: SpatialSupport
    domain_geometry_artifact_id: str | None = None
    terrain_artifact_id: str | None = None
    land_sea_mask_artifact_id: str | None = None

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> GridDefinition:
        normalized_wkt2 = pyproj.CRS.from_user_input(self.crs_wkt2).to_wkt("WKT2_2019")
        object.__setattr__(self, "crs_wkt2", normalized_wkt2)

        if self.shape_y <= 0 or self.shape_x <= 0:
            raise ValueError("shape_y and shape_x must be positive integers")

        if len(self.y_coordinates) != self.shape_y:
            raise ValueError(
                f"y_coordinates length {len(self.y_coordinates)} does not match "
                f"shape_y {self.shape_y}"
            )
        if len(self.x_coordinates) != self.shape_x:
            raise ValueError(
                f"x_coordinates length {len(self.x_coordinates)} does not match "
                f"shape_x {self.shape_x}"
            )
        if not _is_finite_strictly_monotonic(self.x_coordinates):
            raise ValueError("x_coordinates must be finite and strictly monotonic")
        if not _is_finite_strictly_monotonic(self.y_coordinates):
            raise ValueError("y_coordinates must be finite and strictly monotonic")

        if self.orientation == "other" and not self.orientation_note:
            raise ValueError("orientation='other' requires a non-empty orientation_note")
        if self.orientation != "other" and self.orientation_note:
            raise ValueError("orientation_note is only permitted when orientation='other'")

        return self

    @property
    def definition_digest(self) -> Digest:
        payload = {field: getattr(self, field) for field in _DIGEST_FIELDS}
        canonical_bytes = jcs.canonicalize(payload)
        return Digest.of_bytes(canonical_bytes)
