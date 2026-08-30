"""Immutable Phase 1 domain definition (plan Section 3.2).

A ``DomainDefinition`` pins a fixed geographic bounding box, an
authoritative center point that must be the exact arithmetic midpoint of
that box, and the canonical ordered station membership. Domain
membership is a planning-time, versioned decision (plan Section 1.4):
changing station membership requires a new domain ID/configuration
version, never a runtime nearest-station reselection.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import StationId

_ALLOWED_LONGITUDE_RANGE = (-180.0, 180.0)
_MIDPOINT_TOLERANCE_DEGREES = 1e-9


class BoundingBox(BaseModel):
    """Inclusive WGS84 bounding box, longitude convention ``[-180, 180]``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    south: float
    north: float
    west: float
    east: float

    @model_validator(mode="after")
    def _check_ranges(self) -> BoundingBox:
        for name, value in (
            ("south", self.south),
            ("north", self.north),
            ("west", self.west),
            ("east", self.east),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
        if not (-90.0 <= self.south <= 90.0):
            raise ValueError(f"south must be in [-90, 90], got {self.south!r}")
        if not (-90.0 <= self.north <= 90.0):
            raise ValueError(f"north must be in [-90, 90], got {self.north!r}")
        if self.south >= self.north:
            raise ValueError("south must be strictly less than north")
        low, high = _ALLOWED_LONGITUDE_RANGE
        if not (low <= self.west <= high):
            raise ValueError(f"west must be in [{low}, {high}], got {self.west!r}")
        if not (low <= self.east <= high):
            raise ValueError(f"east must be in [{low}, {high}], got {self.east!r}")
        if self.west >= self.east:
            raise ValueError(
                "west must be strictly less than east (Phase 1 does not support "
                "an antimeridian-crossing bbox)"
            )
        return self

    @property
    def center_latitude(self) -> float:
        return (self.south + self.north) / 2.0

    @property
    def center_longitude(self) -> float:
        return (self.west + self.east) / 2.0

    def contains(self, *, latitude: float, longitude: float) -> bool:
        return (self.south <= latitude <= self.north) and (self.west <= longitude <= self.east)


class DomainDefinition(BaseModel):
    """Section 3.2: fixed geography, authoritative center, and station
    membership for one Phase 1 forecast domain."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["domain-definition.v1"] = "domain-definition.v1"
    domain_id: str
    center_latitude: float
    center_longitude: float
    bbox: BoundingBox
    crs: Literal["EPSG:4326"] = "EPSG:4326"
    longitude_convention: Literal["minus_180_to_180"] = "minus_180_to_180"
    station_ids: tuple[StationId, ...]

    @model_validator(mode="after")
    def _check_center_is_bbox_midpoint(self) -> DomainDefinition:
        if not math.isfinite(self.center_latitude) or not math.isfinite(self.center_longitude):
            raise ValueError("center_latitude and center_longitude must be finite")
        if abs(self.center_latitude - self.bbox.center_latitude) > _MIDPOINT_TOLERANCE_DEGREES:
            raise ValueError(
                f"center_latitude {self.center_latitude!r} must equal the bbox's "
                f"arithmetic midpoint latitude {self.bbox.center_latitude!r}"
            )
        if abs(self.center_longitude - self.bbox.center_longitude) > _MIDPOINT_TOLERANCE_DEGREES:
            raise ValueError(
                f"center_longitude {self.center_longitude!r} must equal the bbox's "
                f"arithmetic midpoint longitude {self.bbox.center_longitude!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_station_membership(self) -> DomainDefinition:
        if len(self.station_ids) == 0:
            raise ValueError("station_ids must contain at least one station")
        if len(set(self.station_ids)) != len(self.station_ids):
            raise ValueError("station_ids must not contain duplicates")
        sorted_ids = tuple(sorted(self.station_ids))
        if self.station_ids != sorted_ids:
            raise ValueError(
                f"station_ids must be in canonical lexicographic order, expected "
                f"{sorted_ids!r}, got {self.station_ids!r}"
            )
        return self
