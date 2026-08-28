"""``VariableLineageManifest`` contract (plan Section 3.4).

Maps every canonical Phase 1 variable and lead to its exact source
provenance: source artifacts, inventory row, GRIB identifying keys,
selector expression, decode backend, unit conversion, and wind rotation
policy. Produced *before* HRRR normalization and referenced by
``variable_lineage_manifest_id`` on the canonical guidance dataset.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import ArtifactId, GridId, VariableId


class VariableLineageEntry(BaseModel):
    """One canonical-variable/lead slice's complete source lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonical_variable_id: VariableId
    lead_hours: int
    index_artifact_id: ArtifactId
    selected_grib_artifact_id: ArtifactId
    source_cycle: str
    source_product: Literal["sfc"] = "sfc"
    resolved_url: str
    source_revision: str
    message_number: int
    byte_start: int
    byte_end: int
    inventory_row: str
    grib_keys: dict[str, object]
    selector_expression: str
    decode_backend: Literal["cfgrib"] = "cfgrib"
    decode_backend_kwargs: dict[str, object]
    unit_conversion: str
    wind_rotation_policy: Literal["identity", "grid-to-earth-pyproj.v1"] | None = None
    grid_relative: bool | None = None
    source_grid_id: GridId
    output_grid_id: GridId
    spatial_subset_y_start: int
    spatial_subset_y_end: int
    spatial_subset_x_start: int
    spatial_subset_x_end: int


class VariableLineageManifest(BaseModel):
    """Section 3.4: ``variable-lineage.v1``. Rejects a missing or
    duplicate variable/lead entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["variable-lineage.v1"] = "variable-lineage.v1"
    entries: tuple[VariableLineageEntry, ...]
    expected_canonical_variable_ids: tuple[VariableId, ...]
    expected_lead_hours: tuple[int, ...]

    @model_validator(mode="after")
    def _check_completeness_and_no_duplicates(self) -> VariableLineageManifest:
        seen: set[tuple[str, int]] = set()
        for entry in self.entries:
            key = (entry.canonical_variable_id, entry.lead_hours)
            if key in seen:
                raise ValueError(f"duplicate variable/lead entry: {key!r}")
            seen.add(key)

        expected = {
            (variable_id, lead)
            for variable_id in self.expected_canonical_variable_ids
            for lead in self.expected_lead_hours
        }
        missing = expected - seen
        if missing:
            raise ValueError(
                f"variable lineage manifest is missing {len(missing)} expected "
                f"variable/lead entries: {sorted(missing)!r}"
            )
        extra = seen - expected
        if extra:
            raise ValueError(
                f"variable lineage manifest has {len(extra)} unexpected variable/lead "
                f"entries not in the expected set: {sorted(extra)!r}"
            )
        return self
