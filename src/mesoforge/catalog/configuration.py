"""Immutable configuration resolution (plan Section 4.7).

YAML -> strict Pydantic model -> RFC 8785/JCS canonical JSON -> SHA-256
snapshot digest. Only the canonical JSON of the validated model
participates in the digest; YAML comments/whitespace/key order never
affect identity, and only the three named operational override paths are
permitted after loading.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Literal

import jcs
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.catalog.domains import DomainDefinition
from mesoforge.catalog.grids import GridDefinition
from mesoforge.catalog.sources import AviationWeatherSettings, HrrrSourceSettings
from mesoforge.catalog.stations import StationDefinition
from mesoforge.catalog.units import VerticalDefinition
from mesoforge.catalog.variables import VariableDefinition
from mesoforge.common.identifiers import (
    ConfigurationSnapshotId,
    Digest,
    MatchingPolicyId,
    MetricSetId,
)

_PERMITTED_OVERRIDE_PATHS: frozenset[str] = frozenset(
    {
        "artifact_store.endpoint_reference",
        "artifact_store.bucket",
        "metadata_store.dsn_environment_variable",
    }
)


class ArtifactStoreSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    endpoint_reference: str
    bucket: str


class MetadataStoreSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dsn_environment_variable: str


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    content_digest: Digest


class PointExtractionPolicy(BaseModel):
    """Section 3.2: bilinear native-grid station point extraction, no
    extrapolation, exact four-corner finite requirement, and the
    interpolation halo width used when subsetting canonical guidance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["point-extraction-policy.v1"] = "point-extraction-policy.v1"
    policy_id: Literal["bilinear-native-grid.v1"] = "bilinear-native-grid.v1"
    allow_extrapolation: Literal[False] = False
    require_four_corners_finite: Literal[True] = True
    halo_cells: int = 1
    weight_sum_tolerance: float = 1e-12

    @model_validator(mode="after")
    def _check_halo(self) -> PointExtractionPolicy:
        if self.halo_cells != 1:
            raise ValueError("Phase 1 requires exactly a one-cell interpolation halo")
        if not (0 < self.weight_sum_tolerance < 1e-6):
            raise ValueError("weight_sum_tolerance must be a small positive value")
        return self


class ObservationNormalizationPolicy(BaseModel):
    """Section 3.7: unit conversions and range/completeness checks
    applied when normalizing raw METAR JSON records."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["observation-normalization-policy.v1"] = (
        "observation-normalization-policy.v1"
    )
    policy_id: str
    temperature_valid_min_k: float = 180.0
    temperature_valid_max_k: float = 340.0
    wind_speed_valid_min_m_s: float = 0.0
    wind_speed_valid_max_m_s: float = 100.0
    max_receipt_before_event_minutes: float = 5.0
    station_coordinate_tolerance_degrees: float = 0.02
    station_elevation_tolerance_m: float = 30.0

    @model_validator(mode="after")
    def _check_ranges(self) -> ObservationNormalizationPolicy:
        if self.temperature_valid_min_k >= self.temperature_valid_max_k:
            raise ValueError("temperature_valid_min_k must be < temperature_valid_max_k")
        if self.wind_speed_valid_min_m_s < 0:
            raise ValueError("wind_speed_valid_min_m_s must be nonnegative")
        if self.wind_speed_valid_max_m_s <= self.wind_speed_valid_min_m_s:
            raise ValueError("wind_speed_valid_max_m_s must be > wind_speed_valid_min_m_s")
        if self.max_receipt_before_event_minutes < 0:
            raise ValueError("max_receipt_before_event_minutes must be nonnegative")
        if self.station_coordinate_tolerance_degrees <= 0:
            raise ValueError("station_coordinate_tolerance_degrees must be positive")
        if self.station_elevation_tolerance_m <= 0:
            raise ValueError("station_elevation_tolerance_m must be positive")
        return self


class MatchingPolicy(BaseModel):
    """Section 3.8: as-of, symmetric-inclusive-tolerance forecast/
    observation matching, and the calm-wind direction-eligibility
    threshold shared with verification metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["matching-policy.v1"] = "matching-policy.v1"
    matching_policy_id: MatchingPolicyId
    tolerance_minutes: float = 15.0
    calm_threshold_m_s: float = 1.5

    @model_validator(mode="after")
    def _check_thresholds(self) -> MatchingPolicy:
        if self.tolerance_minutes <= 0:
            raise ValueError("tolerance_minutes must be positive")
        if self.calm_threshold_m_s <= 0:
            raise ValueError("calm_threshold_m_s must be positive")
        return self

    @property
    def digest(self) -> Digest:
        payload = self.model_dump(mode="json")
        return Digest.of_bytes(jcs.canonicalize(payload))


class MetricSet(BaseModel):
    """Section 3.9: the named formula set reported by Phase 1
    verification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["metric-set.v1"] = "metric-set.v1"
    metric_set_id: MetricSetId
    metric_names: tuple[str, ...]

    @model_validator(mode="after")
    def _check_metric_names(self) -> MetricSet:
        if len(self.metric_names) == 0:
            raise ValueError("metric_names must be non-empty")
        if len(set(self.metric_names)) != len(self.metric_names):
            raise ValueError("metric_names must not contain duplicates")
        return self


class Phase1Configuration(BaseModel):
    """Section 3.2: the complete Phase 1 Grasston slice configuration,
    nested under the top-level ``MesoForgeConfiguration`` as an optional
    section so Phase 0 configuration remains valid without it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["phase1-configuration.v1"] = "phase1-configuration.v1"
    domain: DomainDefinition
    stations: tuple[StationDefinition, ...]
    hrrr: HrrrSourceSettings
    aviationweather: AviationWeatherSettings
    point_extraction_policy: PointExtractionPolicy
    observation_normalization_policy: ObservationNormalizationPolicy
    matching_policy: MatchingPolicy
    metric_set: MetricSet

    @model_validator(mode="after")
    def _check_station_definitions_match_domain(self) -> Phase1Configuration:
        defined_ids = tuple(s.station_id for s in self.stations)
        if defined_ids != self.domain.station_ids:
            raise ValueError(
                f"Phase 1 'stations' station_ids {defined_ids!r} must exactly match "
                f"domain.station_ids {self.domain.station_ids!r} in the same order"
            )
        for station in self.stations:
            if not self.domain.bbox.contains(
                latitude=station.expected_latitude, longitude=station.expected_longitude
            ):
                raise ValueError(
                    f"station {station.station_id!r} expected coordinates lie outside "
                    "the domain bounding box"
                )
        return self


class MesoForgeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["mesoforge-config.v1"] = "mesoforge-config.v1"
    grids: tuple[GridDefinition, ...] = ()
    vertical_definitions: tuple[VerticalDefinition, ...] = ()
    variables: tuple[VariableDefinition, ...] = ()
    artifact_store: ArtifactStoreSettings
    metadata_store: MetadataStoreSettings
    phase1: Phase1Configuration | None = None

    @model_validator(mode="after")
    def _check_duplicate_ids(self) -> MesoForgeConfiguration:
        _reject_duplicates("grids", (g.grid_id for g in self.grids))
        _reject_duplicates(
            "vertical_definitions", (v.vertical_definition_id for v in self.vertical_definitions)
        )
        _reject_duplicates("variables", (v.variable_id for v in self.variables))
        return self


def _reject_duplicates(label: str, ids: Any) -> None:
    seen: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            raise ValueError(f"duplicate {label[:-1]}_id {item_id!r} in configuration")
        seen.add(item_id)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two mappings: nested dicts merge key-by-key; lists (and
    any other value type) replace wholesale."""
    result = copy.deepcopy(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = _deep_merge(base_value, overlay_value)
        else:
            result[key] = copy.deepcopy(overlay_value)
    return result


def _lists_to_tuples(value: Any) -> Any:
    """Recursively convert lists to tuples so strict-mode Pydantic models
    (which do not coerce list -> tuple) can validate YAML-sourced data.
    Dict values are converted too; scalars pass through unchanged."""
    if isinstance(value, list):
        return tuple(_lists_to_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _lists_to_tuples(item) for key, item in value.items()}
    return value


def load_configuration_source(
    *,
    base_path: Path,
    environment_path: Path | None = None,
) -> tuple[MesoForgeConfiguration, tuple[SourceReference, ...]]:
    """Load, deep-merge, and strictly validate configuration YAML.

    Composition order: required base file, then an optional named
    environment overlay. Maps deep-merge; lists replace wholesale.
    """
    base_text = base_path.read_text(encoding="utf-8")
    merged: dict[str, Any] = yaml.safe_load(base_text) or {}
    source_refs = [
        SourceReference(path=str(base_path), content_digest=Digest.of_bytes(base_text.encode()))
    ]

    if environment_path is not None:
        env_text = environment_path.read_text(encoding="utf-8")
        overlay = yaml.safe_load(env_text) or {}
        merged = _deep_merge(merged, overlay)
        source_refs.append(
            SourceReference(
                path=str(environment_path), content_digest=Digest.of_bytes(env_text.encode())
            )
        )

    configuration = MesoForgeConfiguration.model_validate(_lists_to_tuples(merged))
    return configuration, tuple(source_refs)


def resolve_configuration(
    configuration: MesoForgeConfiguration,
    *,
    overrides: dict[str, Any] | None = None,
) -> MesoForgeConfiguration:
    """Apply an explicit in-process override mapping restricted to exactly
    the three sanctioned operational paths. Any other path raises."""
    if not overrides:
        return configuration

    data = configuration.model_dump(mode="json")
    for path, value in overrides.items():
        if path not in _PERMITTED_OVERRIDE_PATHS:
            raise ValueError(
                f"{path!r} is not a permitted override path; only "
                f"{sorted(_PERMITTED_OVERRIDE_PATHS)} may be overridden"
            )
        section, _, field = path.partition(".")
        data[section][field] = value

    return MesoForgeConfiguration.model_validate(_lists_to_tuples(data))


def compute_configuration_digest(configuration: MesoForgeConfiguration) -> Digest:
    """SHA-256 over the RFC 8785/JCS canonical JSON of the validated model.
    Audit metadata (created_at, source references) is intentionally
    excluded -- only the scientific configuration model itself."""
    payload = configuration.model_dump(mode="json")
    canonical_bytes = jcs.canonicalize(payload)
    return Digest.of_bytes(canonical_bytes)


def compute_configuration_snapshot_id(
    configuration: MesoForgeConfiguration,
) -> ConfigurationSnapshotId:
    return ConfigurationSnapshotId.from_digest(compute_configuration_digest(configuration))
