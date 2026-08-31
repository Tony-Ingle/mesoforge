"""``matched-pairs.v1`` and ``verification-report.v1`` contracts (plan
Sections 3.8/3.9, Tasks 10/11).

``MatchedPairRow`` is one row per expected (station, lead) pair -- 21
rows for the Grasston 3-station x 7-lead configuration -- even when no
observation matched. ``MetricRow``/``VerificationReport`` are the
downstream computed-metric artifacts: one row per (stratum, metric)
combination across overall/by-lead/by-station strata.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


# Additive Phase 2 contracts.  The v1 classes above intentionally remain
# unchanged and continue to reject the v2 schema versions.
FieldStatusV2 = Literal[
    "forecast_missing_or_invalid",
    "no_report_within_tolerance",
    "revision_after_cutoff",
    "station_metadata_conflict",
    "observation_qc_rejected",
    "field_missing",
    "calm_direction_excluded",
    "precipitation_interval_mismatch",
    "matched",
]
AvailabilityStateV2 = Literal["complete", "fallback", "unavailable", "inconsistent"]


class MatchedPairRowV2(BaseModel):
    """One immutable Phase 2 station/target-horizon row with all named fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["matched-pairs.v2"] = "matched-pairs.v2"
    station_id: StationId
    target_horizon_hours: int
    valid_time: UtcInstant
    precipitation_interval_start: UtcInstant
    precipitation_interval_end: UtcInstant
    baseline_artifact_id: ArtifactId
    observations_artifact_id: ArtifactId
    matching_policy_id: MatchingPolicyId
    matching_policy_digest: Digest
    verification_cutoff: UtcInstant
    availability_state: AvailabilityStateV2
    selected_logical_observation_digest: Digest | None = None
    selected_revision_digest: Digest | None = None
    selected_event_time: UtcInstant | None = None
    selected_provider_available_at: UtcInstant | None = None
    delta_seconds: float | None = None

    forecast_temperature_k: float | None = None
    forecast_dew_point_k: float | None = None
    forecast_eastward_wind_m_s: float | None = None
    forecast_northward_wind_m_s: float | None = None
    forecast_wind_speed_m_s: float | None = None
    forecast_wind_from_direction_degrees: float | None = None
    forecast_wind_gust_m_s: float | None = None
    forecast_qpf_kg_m2: float | None = None
    forecast_pop_probability: float | None = None
    observed_temperature_k: float | None = None
    observed_dew_point_k: float | None = None
    observed_eastward_wind_m_s: float | None = None
    observed_northward_wind_m_s: float | None = None
    observed_wind_speed_m_s: float | None = None
    observed_wind_from_direction_degrees: float | None = None
    observed_wind_gust_m_s: float | None = None
    observed_qpf_kg_m2: float | None = None

    row_status: RowStatus
    temperature_status: FieldStatusV2
    dew_point_status: FieldStatusV2
    eastward_component_status: FieldStatusV2
    northward_component_status: FieldStatusV2
    wind_speed_status: FieldStatusV2
    wind_direction_status: FieldStatusV2
    gust_status: FieldStatusV2
    qpf_status: FieldStatusV2
    pop_status: FieldStatusV2

    @model_validator(mode="after")
    def _validate_v2_row(self) -> MatchedPairRowV2:
        if not 1 <= self.target_horizon_hours <= 36:
            raise ValueError("target_horizon_hours must be in 1..36")
        statuses = tuple(
            getattr(self, f"{name}_status")
            for name in (
                "temperature",
                "dew_point",
                "eastward_component",
                "northward_component",
                "wind_speed",
                "wind_direction",
                "gust",
                "qpf",
                "pop",
            )
        )
        expected = "matched_any_field" if "matched" in statuses else "matched_no_fields"
        if self.row_status != expected:
            raise ValueError(f"row_status must be {expected!r} for field statuses")
        pairs = (
            ("temperature", self.forecast_temperature_k, self.observed_temperature_k),
            ("dew_point", self.forecast_dew_point_k, self.observed_dew_point_k),
            (
                "eastward_component",
                self.forecast_eastward_wind_m_s,
                self.observed_eastward_wind_m_s,
            ),
            (
                "northward_component",
                self.forecast_northward_wind_m_s,
                self.observed_northward_wind_m_s,
            ),
            ("wind_speed", self.forecast_wind_speed_m_s, self.observed_wind_speed_m_s),
            (
                "wind_direction",
                self.forecast_wind_from_direction_degrees,
                self.observed_wind_from_direction_degrees,
            ),
            ("gust", self.forecast_wind_gust_m_s, self.observed_wind_gust_m_s),
            ("qpf", self.forecast_qpf_kg_m2, self.observed_qpf_kg_m2),
        )
        for name, forecast, observed in pairs:
            if getattr(self, f"{name}_status") == "matched" and (
                forecast is None or observed is None
            ):
                raise ValueError(f"{name} matched status requires forecast and observed values")
        if self.pop_status == "matched" and (
            self.forecast_pop_probability is None or self.observed_qpf_kg_m2 is None
        ):
            raise ValueError("pop matched status requires probability and observed QPF")
        for name, value in self.__dict__.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if (
            self.forecast_pop_probability is not None
            and not 0 <= self.forecast_pop_probability <= 1
        ):
            raise ValueError("forecast_pop_probability must be in [0, 1]")
        for name in ("forecast_qpf_kg_m2", "observed_qpf_kg_m2"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.precipitation_interval_end != self.valid_time:
            raise ValueError("forecast precipitation interval must end at valid_time")
        if self.precipitation_interval_end - self.precipitation_interval_start != timedelta(
            hours=1
        ):
            raise ValueError("forecast precipitation interval must be exactly one hour")
        for name in (
            "forecast_wind_speed_m_s",
            "observed_wind_speed_m_s",
            "forecast_wind_gust_m_s",
            "observed_wind_gust_m_s",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name in (
            "forecast_wind_from_direction_degrees",
            "observed_wind_from_direction_degrees",
        ):
            value = getattr(self, name)
            if value is not None and not 0 <= value < 360:
                raise ValueError(f"{name} must be in [0, 360)")
        if self.delta_seconds is not None and self.delta_seconds < 0:
            raise ValueError("delta_seconds must be nonnegative")
        return self


StratumKindV2 = Literal["overall", "by_target_horizon", "by_station", "by_availability_state"]


class MetricRowV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    metric_name: str
    unit_id: str
    stratum_kind: StratumKindV2
    stratum_value: str | int | None = None
    value: float | None = None
    null_reason: str | None = None
    sample_count: int
    missing_counts: dict[str, int]
    label: Literal["conditional_on_reported_gust"] | None = None
    details: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_metric(self) -> MetricRowV2:
        if self.sample_count < 0 or any(value < 0 for value in self.missing_counts.values()):
            raise ValueError("metric counts must be nonnegative")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("metric value must be finite")
        pending = list(self.details.values())
        while pending:
            member = pending.pop()
            if isinstance(member, float) and not math.isfinite(member):
                raise ValueError("metric details must contain only finite JSON numbers")
            if isinstance(member, dict):
                pending.extend(member.values())
            elif isinstance(member, (list, tuple)):
                pending.extend(member)
        if (self.value is None) != (self.null_reason is not None):
            raise ValueError("null metrics require a reason; valued metrics prohibit one")
        return self


class VerificationReportV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["verification-report.v2"] = "verification-report.v2"
    metric_set_id: MetricSetId
    baseline_artifact_id: ArtifactId
    matched_pairs_artifact_id: ArtifactId
    matching_policy_id: MatchingPolicyId
    matching_policy_digest: Digest
    verification_cutoff: UtcInstant
    rows: tuple[MetricRowV2, ...]

    @model_validator(mode="after")
    def _check_complete_strata(self) -> VerificationReportV2:
        if not self.rows:
            raise ValueError("verification-report.v2 rows must not be empty")
        kinds = {row.stratum_kind for row in self.rows}
        required = {"overall", "by_target_horizon", "by_station", "by_availability_state"}
        if kinds != required:
            raise ValueError("verification-report.v2 requires all four stratum kinds")
        horizons = {
            row.stratum_value for row in self.rows if row.stratum_kind == "by_target_horizon"
        }
        stations = {row.stratum_value for row in self.rows if row.stratum_kind == "by_station"}
        if horizons != set(range(1, 37)):
            raise ValueError("verification-report.v2 requires target horizons 1..36")
        if stations != {"station.kcbg", "station.kjmr", "station.kros"}:
            raise ValueError("verification-report.v2 requires all three canonical stations")
        return self
