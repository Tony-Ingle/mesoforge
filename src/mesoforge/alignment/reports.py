"""``point-extraction-report.v1`` contract (plan Section 3.6).

The immutable spatial-weight artifact referenced by the baseline and
lineage. Records every station's source ``(y,x)`` indices, coordinates,
weights, extracted values, algorithm/library version, and no-
extrapolation status, for all expected station/lead/variable triples.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import GridId, StationId, VariableId


class ExtractionWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    w00: float
    w01: float
    w10: float
    w11: float


class PointExtractionRecord(BaseModel):
    """One station/lead/variable's complete bilinear extraction
    provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    station_id: StationId
    lead_hours: int
    canonical_variable_id: VariableId
    source_y0: int
    source_y1: int
    source_x0: int
    source_x1: int
    station_projected_x: float
    station_projected_y: float
    weights: ExtractionWeights
    value: float
    extrapolated: Literal[False] = False


class PointExtractionReport(BaseModel):
    """Section 3.6: ``point-extraction-report.v1``. Covers all expected
    station/lead/variable triples (21 station/lead outputs x 3
    variables for Phase 1's 3 stations x 7 leads)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["point-extraction-report.v1"] = "point-extraction-report.v1"
    source_grid_id: GridId
    algorithm: Literal["bilinear-native-grid.v1"] = "bilinear-native-grid.v1"
    pyproj_version: str
    station_elevation_policy: Literal["ignored-in-phase1"] = "ignored-in-phase1"
    records: tuple[PointExtractionRecord, ...]
    expected_station_ids: tuple[StationId, ...]
    expected_lead_hours: tuple[int, ...]
    expected_variable_ids: tuple[VariableId, ...]

    @model_validator(mode="after")
    def _check_completeness(self) -> PointExtractionReport:
        seen: set[tuple[str, int, str]] = set()
        for record in self.records:
            key = (record.station_id, record.lead_hours, record.canonical_variable_id)
            if key in seen:
                raise ValueError(f"duplicate extraction record: {key!r}")
            seen.add(key)

        expected = {
            (station_id, lead, variable_id)
            for station_id in self.expected_station_ids
            for lead in self.expected_lead_hours
            for variable_id in self.expected_variable_ids
        }
        missing = expected - seen
        if missing:
            raise ValueError(
                f"point extraction report is missing {len(missing)} expected records: "
                f"{sorted(missing)!r}"
            )
        extra = seen - expected
        if extra:
            raise ValueError(
                f"point extraction report has {len(extra)} unexpected records: {sorted(extra)!r}"
            )
        return self
