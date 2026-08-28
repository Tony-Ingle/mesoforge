"""Phase 1 station catalog contracts (plan Section 3.2).

``StationDefinition`` is the planning-time, versioned, configuration-
pinned expectation for one station: its provider ICAO ID, expected
coordinates/elevation, and declared METAR capability. It is not the
runtime effective snapshot -- ``StationCatalogSnapshot`` (added in Task
6 alongside the normalization logic that builds it from the retained
AviationWeather stationinfo response) is the immutable, source-derived
record that actually governs HRRR point extraction and station
identity for a run.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mesoforge.common.identifiers import StationId

_ICAO_MIN_LENGTH = 3
_ICAO_MAX_LENGTH = 4


class StationDefinition(BaseModel):
    """Section 3.2: planning-time expected station identity/metadata.
    Because AviationWeather.gov does not expose exposure/instrument
    identity, both fields are explicitly ``"unknown"`` rather than
    invented (plan Section 3.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["station-definition.v1"] = "station-definition.v1"
    station_id: StationId
    provider_icao_id: str
    expected_latitude: float
    expected_longitude: float
    expected_elevation_m: float
    site_name: str
    provider_site_types: tuple[str, ...]
    provider_priority: int
    exposure_identity: Literal["unknown"] = "unknown"
    instrument_identity: Literal["unknown"] = "unknown"

    @field_validator("provider_icao_id")
    @classmethod
    def _check_icao_shape(cls, value: str) -> str:
        if not (_ICAO_MIN_LENGTH <= len(value) <= _ICAO_MAX_LENGTH) or not value.isupper():
            raise ValueError(
                f"provider_icao_id must be {_ICAO_MIN_LENGTH}-{_ICAO_MAX_LENGTH} uppercase "
                f"letters/digits, got {value!r}"
            )
        if not value.isalnum():
            raise ValueError(f"provider_icao_id must be alphanumeric, got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_coordinates(self) -> StationDefinition:
        if not math.isfinite(self.expected_latitude) or not (
            -90.0 <= self.expected_latitude <= 90.0
        ):
            raise ValueError(
                f"expected_latitude must be finite and in [-90, 90], got {self.expected_latitude!r}"
            )
        if not math.isfinite(self.expected_longitude) or not (
            -180.0 <= self.expected_longitude <= 180.0
        ):
            raise ValueError(
                "expected_longitude must be finite and in [-180, 180], got "
                f"{self.expected_longitude!r}"
            )
        if not math.isfinite(self.expected_elevation_m):
            raise ValueError(
                f"expected_elevation_m must be finite, got {self.expected_elevation_m!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_metar_capability(self) -> StationDefinition:
        if "METAR" not in self.provider_site_types:
            raise ValueError(
                f"provider_site_types must declare METAR capability, got "
                f"{self.provider_site_types!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_priority(self) -> StationDefinition:
        if self.provider_priority < 0:
            raise ValueError("provider_priority must be nonnegative")
        return self
