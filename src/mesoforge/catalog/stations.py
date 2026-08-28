"""Phase 1 station catalog contracts (plan Section 3.2).

``StationDefinition`` is the planning-time, versioned, configuration-
pinned expectation for one station: its provider ICAO ID, expected
coordinates/elevation, and declared METAR capability. It is not the
runtime effective snapshot -- ``StationCatalogSnapshot`` is the
immutable, source-derived record (built by ``normalize_station_catalog``
from the retained AviationWeather stationinfo response) that actually
governs HRRR point extraction and station identity for a run.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mesoforge.catalog.domains import DomainDefinition
from mesoforge.common.errors import MesoForgeError
from mesoforge.common.identifiers import ArtifactId, StationId
from mesoforge.common.time import UtcInstant

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


class StationCatalogError(MesoForgeError):
    """Raised when the retained AviationWeather stationinfo response
    cannot produce a valid Phase 1 station catalog snapshot: an
    expected station is absent, lacks METAR in ``siteType``, or lies
    outside the domain bbox (plan Section 3.2)."""


class StationRecord(BaseModel):
    """One station's effective, source-derived identity/coordinates in
    a ``StationCatalogSnapshot``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["station-record.v1"] = "station-record.v1"
    station_id: StationId
    provider_icao_id: str
    latitude: float
    longitude: float
    elevation_m: float
    site_name: str
    site_types: tuple[str, ...]


class StationCatalogSnapshot(BaseModel):
    """Section 3.2: ``station-catalog.v1``. Immutable, source-derived
    snapshot of effective station coordinates/identity. Effective from
    its source ``available_at`` until a later reviewed snapshot
    supersedes it (Section 3.2's prospective-effective-interval rule)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["station-catalog.v1"] = "station-catalog.v1"
    domain_id: str
    stations: tuple[StationRecord, ...]
    source_artifact_id: ArtifactId
    effective_from: UtcInstant

    @model_validator(mode="after")
    def _check_station_order(self) -> StationCatalogSnapshot:
        ids = tuple(s.station_id for s in self.stations)
        if ids != tuple(sorted(ids)):
            raise ValueError(
                f"stations must be in canonical lexicographic station_id order, got {ids!r}"
            )
        return self


def normalize_station_catalog(
    *,
    raw_records: list[dict[str, Any]],
    domain: DomainDefinition,
    expected_stations: tuple[StationDefinition, ...],
    source_artifact_id: ArtifactId,
    effective_from: Any,
) -> StationCatalogSnapshot:
    """Build the immutable ``station-catalog.v1`` snapshot from the
    retained AviationWeather ``stationinfo`` JSON records (plan Section
    3.2). Fails closed (``StationCatalogError``) if an expected station
    is absent, lacks ``METAR`` in ``siteType``, or lies outside the
    domain bbox. Duplicate station IDs in the raw response are rejected
    rather than silently deduplicated."""
    by_icao: dict[str, dict[str, Any]] = {}
    for record in raw_records:
        icao = record.get("icaoId")
        if not isinstance(icao, str) or not icao:
            raise StationCatalogError(f"stationinfo record missing a valid icaoId: {record!r}")
        if icao in by_icao:
            raise StationCatalogError(f"duplicate icaoId {icao!r} in stationinfo response")
        by_icao[icao] = record

    errors: list[str] = []
    records: list[StationRecord] = []
    for expected in expected_stations:
        raw = by_icao.get(expected.provider_icao_id)
        if raw is None:
            errors.append(
                f"expected station {expected.provider_icao_id!r} "
                f"({expected.station_id!r}) is absent from the stationinfo response"
            )
            continue

        site_type_raw = raw.get("siteType")
        site_types = _extract_site_types(site_type_raw)
        if "METAR" not in site_types:
            errors.append(
                f"station {expected.provider_icao_id!r} does not declare METAR in "
                f"siteType: {site_type_raw!r}"
            )
            continue

        try:
            latitude = float(raw["lat"])
            longitude = float(raw["lon"])
            elevation = float(raw["elev"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(
                f"station {expected.provider_icao_id!r} has malformed lat/lon/elev: {exc}"
            )
            continue

        if not domain.bbox.contains(latitude=latitude, longitude=longitude):
            errors.append(
                f"station {expected.provider_icao_id!r} coordinates "
                f"({latitude!r}, {longitude!r}) lie outside the domain bbox"
            )
            continue

        records.append(
            StationRecord(
                station_id=expected.station_id,
                provider_icao_id=expected.provider_icao_id,
                latitude=latitude,
                longitude=longitude,
                elevation_m=elevation,
                site_name=str(raw.get("site", expected.site_name)),
                site_types=site_types,
            )
        )

    if errors:
        raise StationCatalogError(
            f"station catalog normalization failed with {len(errors)} problem(s): "
            + "; ".join(errors)
        )

    records.sort(key=lambda r: r.station_id)
    return StationCatalogSnapshot(
        domain_id=domain.domain_id,
        stations=tuple(records),
        source_artifact_id=source_artifact_id,
        effective_from=effective_from,
    )


def _extract_site_types(raw_site_type: Any) -> tuple[str, ...]:
    """AviationWeather's ``siteType`` is documented as an object whose
    keys are the capability flags present for a station (e.g.
    ``{"METAR": true, "TAF": true}``); accept that shape, a plain list
    of strings, or a single delimited string defensively, since the
    live schema is not contractually pinned by an OpenAPI enum."""
    if isinstance(raw_site_type, dict):
        return tuple(sorted(k for k, v in raw_site_type.items() if v))
    if isinstance(raw_site_type, list):
        return tuple(sorted(str(v) for v in raw_site_type))
    if isinstance(raw_site_type, str):
        return tuple(sorted(part.strip() for part in raw_site_type.split(",") if part.strip()))
    return ()
