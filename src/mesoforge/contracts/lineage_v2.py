"""``variable-lineage.v2`` contract (plan Section 5.1; Codex re-review
finding 3).

Phase 1's ``variable-lineage.v1`` (``contracts.lineage``) maps one HRRR
cycle's fixed 3-variable/7-lead slice. Phase 2 canonical guidance is
assembled per model from a *selected* cycle whose leads depend on that
cycle's age, from three models with different native grids, and from
per-lead field sets that include plural (dual-parent) precipitation
records. ``variable-lineage.v2`` is the Phase 2 manifest recording, for
every canonical variable and every source lead actually decoded:

- the retained provider index artifact and every selected GRIB message
  artifact that fed the value (plural for GFS's dual-parent APCP);
- the exact inventory rows, message numbers, and byte ranges;
- the resolved index/GRIB URLs and the endpoint the bytes came from;
- the selector expression, decode backend/arguments, unit conversion,
  and wind-rotation policy actually applied;
- the source grid profile identity and the canonical output grid.

``validate_canonical_guidance_v2`` (``guidance.canonical_v2``) can now be
given a decoded manifest of this shape, so a canonical artifact can no
longer satisfy validation by pointing ``variable_lineage_manifest_id`` at
an arbitrary artifact whose ID merely has the right syntax.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import (
    ArtifactId,
    CanonicalRetentionPolicyId,
    ConfigurationSnapshotId,
    GridId,
    VariableId,
)

Phase2ModelId = Literal["hrrr", "nbm", "gfs"]
WindRotationPolicy = Literal["identity", "grid-to-earth-pyproj.v1", "speed-direction-to-uv.v1"]


class VariableLineageEntryV2(BaseModel):
    """One canonical-variable/source-lead slice's complete Phase 2 source
    lineage. ``selected_grib_artifact_ids`` is a tuple because a single
    canonical value may legitimately derive from more than one selected
    message (GFS's duplicate bucket/continuous APCP parents)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonical_variable_id: VariableId
    source_lead_hours: int
    index_artifact_id: ArtifactId
    selected_grib_artifact_ids: tuple[ArtifactId, ...]
    message_numbers: tuple[int, ...]
    byte_ranges: tuple[tuple[int, int], ...]
    inventory_rows: tuple[str, ...]
    source_cycle: str
    endpoint: str
    resolved_index_url: str
    resolved_grib_url: str
    selector_expression: str
    decode_backend: Literal["cfgrib"] = "cfgrib"
    decode_backend_kwargs: dict[str, object]
    unit_conversion: str
    wind_rotation_policy: WindRotationPolicy | None = None
    source_grid_profile_id: GridId
    source_grid_id: GridId
    output_grid_id: GridId

    @model_validator(mode="after")
    def _check_parallel_message_identity(self) -> VariableLineageEntryV2:
        if self.source_lead_hours < 0:
            raise ValueError(f"source_lead_hours must be nonnegative, got {self.source_lead_hours}")
        if not self.selected_grib_artifact_ids:
            raise ValueError(
                f"{self.canonical_variable_id!r} lead {self.source_lead_hours} must reference at "
                "least one selected GRIB message artifact"
            )
        counts = {
            "selected_grib_artifact_ids": len(self.selected_grib_artifact_ids),
            "message_numbers": len(self.message_numbers),
            "byte_ranges": len(self.byte_ranges),
            "inventory_rows": len(self.inventory_rows),
        }
        if len(set(counts.values())) != 1:
            raise ValueError(
                "selected message artifacts, message numbers, byte ranges, and inventory rows "
                f"must be parallel per selected message, got {counts!r}"
            )
        for start, end in self.byte_ranges:
            if start < 0 or end <= start:
                raise ValueError(
                    f"byte range ({start}, {end}) must be a nonempty [start, end) interval"
                )
        if not self.selector_expression:
            raise ValueError("selector_expression must not be empty")
        if not self.unit_conversion:
            raise ValueError("unit_conversion must not be empty")
        if not self.source_grid_profile_id:
            raise ValueError("source_grid_profile_id must not be empty")
        return self


class CanonicalRetentionPolicyV2(BaseModel):
    """The bounded-retention policy under which a Phase 2 canonical
    artifact was cut from its model's full native grid (owner
    architecture decision).

    Phase 2 no longer retains full native grids: it retains the
    configured inclusive domain bbox plus ``halo_cells`` complete source
    cells on every side. The policy is recorded here, in lineage, while
    the *resolved* index window for a specific native grid is recorded
    on the canonical dataset itself -- together they state exactly which
    provider cells the artifact carries and why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_id: CanonicalRetentionPolicyId = CanonicalRetentionPolicyId("bbox-halo-subset.v1")
    halo_cells: int
    bbox_south: float
    bbox_north: float
    bbox_west: float
    bbox_east: float

    @model_validator(mode="after")
    def _check_policy(self) -> CanonicalRetentionPolicyV2:
        if str(self.policy_id) != "bbox-halo-subset.v1":
            raise ValueError(
                "Phase 2 approves exactly one canonical retention policy, "
                f"'bbox-halo-subset.v1'; got {self.policy_id!r}"
            )
        if self.halo_cells < 1:
            raise ValueError(
                "halo_cells must be at least 1 so every bilinear corner the domain can "
                f"require is retained, got {self.halo_cells}"
            )
        if self.bbox_south >= self.bbox_north:
            raise ValueError("bbox_south must be strictly less than bbox_north")
        if self.bbox_west >= self.bbox_east:
            raise ValueError("bbox_west must be strictly less than bbox_east")
        return self


class VariableLineageManifestV2(BaseModel):
    """Section 5.1: ``variable-lineage.v2``. Rejects a manifest that does
    not carry exactly one complete entry for every expected canonical
    variable at every expected source lead, or whose entries disagree
    with the manifest's own model/grid identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["variable-lineage.v2"] = "variable-lineage.v2"
    model: Phase2ModelId
    grid_id: GridId
    source_grid_profile_id: GridId
    forecast_reference_time: str
    configuration_snapshot_id: ConfigurationSnapshotId
    entries: tuple[VariableLineageEntryV2, ...]
    expected_canonical_variable_ids: tuple[VariableId, ...]
    expected_source_lead_hours: tuple[int, ...]
    canonical_retention_policy: CanonicalRetentionPolicyV2

    @model_validator(mode="after")
    def _check_completeness_and_identity(self) -> VariableLineageManifestV2:
        if not self.expected_canonical_variable_ids:
            raise ValueError("expected_canonical_variable_ids must be non-empty")
        if not self.expected_source_lead_hours:
            raise ValueError("expected_source_lead_hours must be non-empty")

        seen: set[tuple[str, int]] = set()
        for entry in self.entries:
            key = (str(entry.canonical_variable_id), entry.source_lead_hours)
            if key in seen:
                raise ValueError(f"duplicate variable/lead entry: {key!r}")
            seen.add(key)
            if str(entry.output_grid_id) != str(self.grid_id):
                raise ValueError(
                    f"entry {key!r} declares output_grid_id {entry.output_grid_id!r}, which does "
                    f"not match the manifest grid_id {self.grid_id!r}"
                )
            if entry.source_grid_profile_id != self.source_grid_profile_id:
                raise ValueError(
                    f"entry {key!r} declares source_grid_profile_id "
                    f"{entry.source_grid_profile_id!r}, which does not match the manifest "
                    f"{self.source_grid_profile_id!r}"
                )

        expected = {
            (str(variable_id), lead)
            for variable_id in self.expected_canonical_variable_ids
            for lead in self.expected_source_lead_hours
        }
        missing = expected - seen
        if missing:
            raise ValueError(
                f"variable-lineage.v2 manifest is missing {len(missing)} expected variable/lead "
                f"entries, e.g. {sorted(missing)[:8]!r}"
            )
        extra = seen - expected
        if extra:
            raise ValueError(
                f"variable-lineage.v2 manifest has {len(extra)} unexpected variable/lead entries "
                f"not in the expected set, e.g. {sorted(extra)[:8]!r}"
            )
        return self
