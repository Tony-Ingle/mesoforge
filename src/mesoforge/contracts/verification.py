"""``matched-pairs.v1`` contract (plan Section 3.8, Task 10).

One row per expected (station, lead) pair -- 21 rows for the Grasston
3-station x 7-lead configuration -- even when no observation matched.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import ArtifactId, Digest, MatchingPolicyId, StationId
from mesoforge.common.time import UtcInstant

RowStatus = Literal["matched_any_field", "matched_no_fields"]

FieldStatus = Literal[
    "forecast_missing_or_invalid",
    "no_report_within_tolerance",
    "revision_after_cutoff",
    "station_metadata_conflict",
    "observation_qc_rejected",
    "temperature_missing",
    "wind_speed_missing",
    "wind_direction_missing_or_variable",
    "calm_direction_excluded",
    "matched",
]


class MatchedPairRow(BaseModel):
    """A single expected station/lead verification pair (plan Section
    3.8). ``*_status`` fields follow the exact mutually-exclusive
    precedence order defined by the plan; ``sample_count`` per field is
    derived later (Task 11) by counting rows with status ``matched``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["matched-pairs.v1"] = "matched-pairs.v1"
    station_id: StationId
    lead_hours: int
    valid_time: UtcInstant
    baseline_artifact_id: ArtifactId
    observations_artifact_id: ArtifactId
    matching_policy_id: MatchingPolicyId
    matching_policy_digest: Digest
    verification_cutoff: UtcInstant

    selected_logical_observation_digest: Digest | None = None
    selected_revision_digest: Digest | None = None
    selected_event_time: UtcInstant | None = None
    selected_provider_available_at: UtcInstant | None = None
    delta_seconds: float | None = None

    forecast_temperature_k: float | None = None
    forecast_eastward_wind_m_s: float | None = None
    forecast_northward_wind_m_s: float | None = None
    forecast_wind_speed_m_s: float | None = None
    forecast_wind_from_direction_degrees: float | None = None

    observed_temperature_k: float | None = None
    observed_eastward_wind_m_s: float | None = None
    observed_northward_wind_m_s: float | None = None
    observed_wind_speed_m_s: float | None = None
    observed_wind_from_direction_degrees: float | None = None

    row_status: RowStatus
    temperature_status: FieldStatus
    eastward_component_status: FieldStatus
    northward_component_status: FieldStatus
    wind_speed_status: FieldStatus
    wind_direction_status: FieldStatus

    @model_validator(mode="after")
    def _check_row_status_matches_field_statuses(self) -> MatchedPairRow:
        field_statuses = (
            self.temperature_status,
            self.eastward_component_status,
            self.northward_component_status,
            self.wind_speed_status,
            self.wind_direction_status,
        )
        any_matched = "matched" in field_statuses
        expected = "matched_any_field" if any_matched else "matched_no_fields"
        if self.row_status != expected:
            raise ValueError(
                f"row_status {self.row_status!r} inconsistent with field statuses "
                f"{field_statuses!r}; expected {expected!r}"
            )
        if self.lead_hours < 0:
            raise ValueError(f"lead_hours must be nonnegative, got {self.lead_hours}")
        return self
