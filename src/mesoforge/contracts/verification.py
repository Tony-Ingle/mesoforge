"""``matched-pairs.v1`` and ``verification-report.v1`` contracts (plan
Sections 3.8/3.9, Tasks 10/11).

``MatchedPairRow`` is one row per expected (station, lead) pair -- 21
rows for the Grasston 3-station x 7-lead configuration -- even when no
observation matched. ``MetricRow``/``VerificationReport`` are the
downstream computed-metric artifacts: one row per (stratum, metric)
combination across overall/by-lead/by-station strata.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import (
    ArtifactId,
    Digest,
    MatchingPolicyId,
    MetricSetId,
    StationId,
)
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


StratumKind = Literal["overall", "by_lead", "by_station"]
MetricName = Literal[
    "temperature_bias",
    "temperature_mae",
    "temperature_rmse",
    "eastward_wind_bias",
    "eastward_wind_mae",
    "eastward_wind_rmse",
    "northward_wind_bias",
    "northward_wind_mae",
    "northward_wind_rmse",
    "wind_speed_bias",
    "wind_speed_mae",
    "wind_speed_rmse",
    "wind_direction_mean_absolute_circular_error",
]


class MissingCounts(BaseModel):
    """Missing count broken out by mutually exclusive field-status
    reason (plan Section 3.8/3.9); every non-``matched`` reason a row
    could have carried for this metric's underlying field."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    forecast_missing_or_invalid: int = 0
    no_report_within_tolerance: int = 0
    revision_after_cutoff: int = 0
    station_metadata_conflict: int = 0
    observation_qc_rejected: int = 0
    field_missing: int = 0
    calm_direction_excluded: int = 0

    @model_validator(mode="after")
    def _check_nonnegative(self) -> MissingCounts:
        for name, value in self.__dict__.items():
            if value < 0:
                raise ValueError(f"{name} must be nonnegative, got {value}")
        return self

    @property
    def total(self) -> int:
        return (
            self.forecast_missing_or_invalid
            + self.no_report_within_tolerance
            + self.revision_after_cutoff
            + self.station_metadata_conflict
            + self.observation_qc_rejected
            + self.field_missing
            + self.calm_direction_excluded
        )


class MetricRow(BaseModel):
    """A single computed metric value for one stratum (plan Section
    3.9). ``value`` is ``None`` (JSON null) exactly when
    ``sample_count == 0`` -- never NaN."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    metric_set_id: MetricSetId
    metric_name: MetricName
    unit_id: str
    stratum_kind: StratumKind
    stratum_lead_hours: int | None = None
    stratum_station_id: StationId | None = None
    value: float | None
    sample_count: int
    missing_counts: MissingCounts
    baseline_artifact_id: ArtifactId
    matched_pairs_artifact_id: ArtifactId
    matching_policy_digest: Digest
    matching_policy_id: MatchingPolicyId
    verification_cutoff: UtcInstant

    @model_validator(mode="after")
    def _check_value_null_iff_zero_samples(self) -> MetricRow:
        if self.sample_count == 0 and self.value is not None:
            raise ValueError("value must be null (None) when sample_count == 0")
        if self.sample_count > 0 and self.value is None:
            raise ValueError("value must be non-null when sample_count > 0")
        if self.sample_count < 0:
            raise ValueError(f"sample_count must be nonnegative, got {self.sample_count}")
        return self

    @model_validator(mode="after")
    def _check_stratum_shape(self) -> MetricRow:
        if self.stratum_kind == "overall" and (
            self.stratum_lead_hours is not None or self.stratum_station_id is not None
        ):
            raise ValueError("overall stratum must not carry lead_hours or station_id")
        if self.stratum_kind == "by_lead" and self.stratum_lead_hours is None:
            raise ValueError("by_lead stratum requires stratum_lead_hours")
        if self.stratum_kind == "by_station" and self.stratum_station_id is None:
            raise ValueError("by_station stratum requires stratum_station_id")
        return self


class VerificationReport(BaseModel):
    """The full ``verification-report.v1`` artifact: an ordered
    collection of ``MetricRow`` entries for overall/by-lead/by-station
    strata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["verification-report.v1"] = "verification-report.v1"
    rows: tuple[MetricRow, ...]
