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
from typing import Any

import jcs
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.catalog.grids import GridDefinition
from mesoforge.catalog.units import VerticalDefinition
from mesoforge.catalog.variables import VariableDefinition
from mesoforge.common.identifiers import ConfigurationSnapshotId, Digest

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


class MesoForgeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "mesoforge-config.v1"
    grids: tuple[GridDefinition, ...] = ()
    vertical_definitions: tuple[VerticalDefinition, ...] = ()
    variables: tuple[VariableDefinition, ...] = ()
    artifact_store: ArtifactStoreSettings
    metadata_store: MetadataStoreSettings

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
