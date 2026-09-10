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
from mesoforge.catalog.sources import (
    AviationWeatherSettings,
    GfsSourceSettings,
    HrrrPhase2SourceSettings,
    HrrrSourceSettings,
    NbmSourceSettings,
)
from mesoforge.catalog.stations import StationDefinition
from mesoforge.catalog.units import VerticalDefinition
from mesoforge.catalog.variables import VariableDefinition
from mesoforge.common.identifiers import (
    ConfigurationSnapshotId,
    Digest,
    FallbackRowId,
    MatchingPolicyId,
    MetricSetId,
    ModelCycleSelectionPolicyId,
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


class ModelCycleSelectionPolicy(BaseModel):
    """Section 1.2, ``phase2-cycle-selection.v1``: the exact per-model
    allowed cycle hours/max age/completion deadline table. Values are
    also individually pinned on each source-settings model; this
    policy is the top-level named/versioned/digestible record of the
    same table referenced by cycle-selection code and the run-spec."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["model-cycle-selection-policy.v1"] = "model-cycle-selection-policy.v1"
    policy_id: ModelCycleSelectionPolicyId
    target_horizons: tuple[int, ...]

    @model_validator(mode="after")
    def _check_target_horizons(self) -> ModelCycleSelectionPolicy:
        if self.target_horizons != tuple(range(1, 37)):
            raise ValueError(
                f"target_horizons must be exactly integers 1..36, got {self.target_horizons!r}"
            )
        return self


_MODEL_ORDER = ("HRRR", "NBM", "GFS")
_WEIGHT_SUM_TOLERANCE = 1e-12


class FallbackWeightRow(BaseModel):
    """One row of Section 4.2/4.4's approved fallback weight tables:
    an exact ``(available_set, horizon_band)`` combination and its
    literal, immutable ``(HRRR, NBM, GFS)`` weight triple."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    row_id: FallbackRowId
    available_models: tuple[Literal["HRRR", "NBM", "GFS"], ...]
    horizon_band: Literal["h01-h18", "h19-h36"]
    weights: tuple[float, float, float]

    @model_validator(mode="after")
    def _check_available_models(self) -> FallbackWeightRow:
        if len(set(self.available_models)) != len(self.available_models):
            raise ValueError(f"available_models must not repeat, got {self.available_models!r}")
        if not self.available_models:
            raise ValueError("available_models must be non-empty")
        if any(m not in _MODEL_ORDER for m in self.available_models):
            raise ValueError(f"available_models must be a subset of {_MODEL_ORDER!r}")
        return self

    @model_validator(mode="after")
    def _check_weights(self) -> FallbackWeightRow:
        if any(w < 0 for w in self.weights):
            raise ValueError(f"weights must be nonnegative, got {self.weights!r}")
        total = sum(self.weights)
        if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"weights must sum to exactly 1 within {_WEIGHT_SUM_TOLERANCE!r}, got "
                f"{self.weights!r} summing to {total!r}"
            )
        for model, weight in zip(_MODEL_ORDER, self.weights, strict=True):
            is_available = model in self.available_models
            if is_available and weight <= 0:
                raise ValueError(
                    f"available model {model!r} must have a strictly positive weight in row "
                    f"{self.row_id!r}, got {weight!r}"
                )
            if not is_available and weight != 0:
                raise ValueError(
                    f"unavailable model {model!r} must have weight exactly 0 in row "
                    f"{self.row_id!r}, got {weight!r}"
                )
        return self

    @property
    def digest(self) -> Digest:
        payload = self.model_dump(mode="json")
        return Digest.of_bytes(jcs.canonicalize(payload))


class FallbackWeightTable(BaseModel):
    """A complete Section 4.2 (scalar/vector) or Section 4.4 (QPF)
    fallback weight table: exactly one row per non-empty subset of
    ``{HRRR, NBM, GFS}`` (7 subsets) x both horizon bands (14 rows
    total), with each row's own digest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_id: str
    rows: tuple[FallbackWeightRow, ...]

    @model_validator(mode="after")
    def _check_complete_coverage(self) -> FallbackWeightTable:
        all_subsets = [
            tuple(s)
            for r in range(1, len(_MODEL_ORDER) + 1)
            for s in _combinations_in_model_order(r)
        ]
        expected = {(subset, band) for subset in all_subsets for band in ("h01-h18", "h19-h36")}
        seen = {(tuple(row.available_models), row.horizon_band) for row in self.rows}
        if seen != expected:
            missing = expected - seen
            extra = seen - expected
            raise ValueError(
                f"fallback weight table {self.table_id!r} must have exactly one row per "
                f"available-model-subset x horizon-band combination; missing={sorted(missing)!r} "
                f"extra={sorted(extra)!r}"
            )
        return self

    def row_for(self, *, available_models: tuple[str, ...], horizon: int) -> FallbackWeightRow:
        band: Literal["h01-h18", "h19-h36"] = "h01-h18" if horizon <= 18 else "h19-h36"
        ordered = tuple(m for m in _MODEL_ORDER if m in available_models)
        for row in self.rows:
            if tuple(row.available_models) == ordered and row.horizon_band == band:
                return row
        raise KeyError(
            f"no fallback row for available_models={ordered!r}, horizon={horizon!r} "
            f"(band={band!r}) in table {self.table_id!r}"
        )


def _combinations_in_model_order(size: int) -> list[tuple[str, ...]]:
    from itertools import combinations

    return [combo for combo in combinations(_MODEL_ORDER, size)]


class GustBlendPolicy(BaseModel):
    """Section 4.3: gust floor/epsilon tolerances, sharing the same
    fallback table as scalar/vector wind."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gust-blend-policy.v1"] = "gust-blend-policy.v1"
    shortfall_floor_tolerance_m_s: float = 0.1
    final_epsilon_floor_m_s: float = 1e-6
    valid_max_m_s: float = 100.0

    @model_validator(mode="after")
    def _check_tolerances(self) -> GustBlendPolicy:
        if not (0 < self.shortfall_floor_tolerance_m_s < 1.0):
            raise ValueError("shortfall_floor_tolerance_m_s must be a small positive value")
        if not (0 < self.final_epsilon_floor_m_s < 1e-3):
            raise ValueError("final_epsilon_floor_m_s must be a small positive value")
        if self.valid_max_m_s <= 0:
            raise ValueError("valid_max_m_s must be positive")
        return self


class PopBlendPolicy(BaseModel):
    """Section 4.5: PoP is NBM-passthrough only, weight exactly 1.0."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["pop-blend-policy.v1"] = "pop-blend-policy.v1"
    sole_contributor: Literal["NBM"] = "NBM"
    weight: float = 1.0

    @model_validator(mode="after")
    def _check_weight(self) -> PopBlendPolicy:
        if self.weight != 1.0:
            raise ValueError(f"PoP weight must be exactly 1.0, got {self.weight!r}")
        return self


class Phase2BlendConfiguration(BaseModel):
    """Section 4: the complete deterministic blend configuration --
    scalar/vector, gust, QPF, and PoP policies."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["phase2-blend-configuration.v1"] = "phase2-blend-configuration.v1"
    scalar_vector_table: FallbackWeightTable
    qpf_table: FallbackWeightTable
    gust_policy: GustBlendPolicy
    pop_policy: PopBlendPolicy

    @model_validator(mode="after")
    def _check_table_ids(self) -> Phase2BlendConfiguration:
        if self.scalar_vector_table.table_id != "phase2-scalar-vector-fallback.v1":
            raise ValueError(
                "scalar_vector_table.table_id must be exactly "
                f"'phase2-scalar-vector-fallback.v1', got {self.scalar_vector_table.table_id!r}"
            )
        if self.qpf_table.table_id != "phase2-qpf-fallback.v1":
            raise ValueError(
                f"qpf_table.table_id must be exactly 'phase2-qpf-fallback.v1', got "
                f"{self.qpf_table.table_id!r}"
            )
        return self


class Phase2Configuration(BaseModel):
    """Section 0/1: the complete Phase 2 Grasston multi-model baseline
    configuration, nested under the top-level ``MesoForgeConfiguration``
    alongside (never replacing) ``Phase1Configuration``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["phase2-configuration.v1"] = "phase2-configuration.v1"
    domain: DomainDefinition
    stations: tuple[StationDefinition, ...]
    hrrr: HrrrPhase2SourceSettings
    nbm: NbmSourceSettings
    gfs: GfsSourceSettings
    aviationweather: AviationWeatherSettings
    cycle_selection_policy: ModelCycleSelectionPolicy
    point_extraction_policy: PointExtractionPolicy
    observation_normalization_policy: ObservationNormalizationPolicy
    matching_policy: MatchingPolicy
    metric_set: MetricSet
    blend_configuration: Phase2BlendConfiguration
    rrfs_enabled: Literal[False] = False
    precipitation_type_enabled: Literal[False] = False
    ai_adjustment_enabled: Literal[False] = False
    learned_weights_enabled: Literal[False] = False
    publication_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _check_station_definitions_match_domain(self) -> Phase2Configuration:
        defined_ids = tuple(s.station_id for s in self.stations)
        if defined_ids != self.domain.station_ids:
            raise ValueError(
                f"Phase 2 'stations' station_ids {defined_ids!r} must exactly match "
                f"domain.station_ids {self.domain.station_ids!r} in the same order"
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
    phase2: Phase2Configuration | None = None

    @model_validator(mode="after")
    def _check_duplicate_ids(self) -> MesoForgeConfiguration:
        _reject_duplicates("grids", (g.grid_id for g in self.grids))
        _reject_duplicates(
            "vertical_definitions", (v.vertical_definition_id for v in self.vertical_definitions)
        )
        _reject_duplicates("variables", (v.variable_id for v in self.variables))
        return self

    @model_validator(mode="after")
    def _check_phase2_retains_phase1_domain(self) -> MesoForgeConfiguration:
        if self.phase2 is None:
            return self
        if self.phase1 is None:
            raise ValueError("phase2 configuration requires phase1 to also be present")
        if self.phase2.domain != self.phase1.domain:
            raise ValueError(
                "phase2.domain must be byte-identical to the retained phase1.domain "
                "(Phase 2 retains the Grasston domain rather than redefining it)"
            )
        if self.phase2.stations != self.phase1.stations:
            raise ValueError(
                "phase2.stations must be byte-identical to the retained phase1.stations"
            )
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
    additional_overlay_paths: tuple[Path, ...] = (),
) -> tuple[MesoForgeConfiguration, tuple[SourceReference, ...]]:
    """Load, deep-merge, and strictly validate configuration YAML.

    Composition order: required base file, then an optional named
    environment overlay, then any further overlays in
    ``additional_overlay_paths`` order (e.g. a Phase 1 overlay followed
    by a Phase 2 overlay -- Phase 2 configuration requires Phase 1 to
    already be present). Maps deep-merge; lists replace wholesale.
    """
    base_text = base_path.read_text(encoding="utf-8")
    merged: dict[str, Any] = yaml.safe_load(base_text) or {}
    source_refs = [
        SourceReference(path=str(base_path), content_digest=Digest.of_bytes(base_text.encode()))
    ]

    overlay_paths = list(environment_path and [environment_path] or [])
    overlay_paths.extend(additional_overlay_paths)

    for overlay_path in overlay_paths:
        overlay_text = overlay_path.read_text(encoding="utf-8")
        overlay = yaml.safe_load(overlay_text) or {}
        merged = _deep_merge(merged, overlay)
        source_refs.append(
            SourceReference(
                path=str(overlay_path), content_digest=Digest.of_bytes(overlay_text.encode())
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
