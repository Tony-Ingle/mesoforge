"""``blend-contribution-manifest.v1`` and forecast artifact contracts
(plan Section 4.7/5.1, Task 10).

One deterministic row per ``(variable, location, target_horizon)``
with ordered ContributorRecord entries, availability state/fallback
row id, unrounded float64 sum, and reconstruction within a configured
absolute tolerance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import FallbackRowId, StationId

VariableAvailabilityStateLiteral = Literal["complete", "fallback", "unavailable", "inconsistent"]
RunStateLiteral = Literal["complete", "degraded", "invalid"]

_RECONSTRUCTION_TOLERANCE = 1e-12


class ContributorRecord(BaseModel):
    """One ordered model contribution to a blended value."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: Literal["HRRR", "NBM", "GFS"]
    source_cycle_reference_time: str
    source_forecast_hour: int
    artifact_id: str
    aligned_value: float
    configured_weight: float
    weighted_contribution: float
    quality_bits: int = 0

    @model_validator(mode="after")
    def _check_weighted_contribution(self) -> ContributorRecord:
        expected = self.aligned_value * self.configured_weight
        if abs(expected - self.weighted_contribution) > 1e-9:
            raise ValueError(
                f"weighted_contribution {self.weighted_contribution!r} does not equal "
                f"aligned_value * configured_weight ({expected!r})"
            )
        return self


class ExcludedContributorRecord(BaseModel):
    """One model that was *eligible* for this row but excluded from it.

    Section 4.1/4.3 disqualification is fail-closed, but the scope of a
    disqualification is not always the whole cycle: a source gust below
    its own sustained speed invalidates the coupled wind/gust tuple at
    that exact station/horizon, not the model's independent temperature,
    dew point, PoP, or QPF guidance (Codex review ``t_1564b30c``).
    Whichever scope applied, the evidence has to survive into the
    product, so this record carries the cause, the scope, the exact
    source values, the tolerance they were judged against, and every
    canonical field the exclusion removed. The row it lives on already
    names the model's station, horizon, and valid time, so those are
    never duplicated here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: Literal["HRRR", "NBM", "GFS"]
    reason: str
    scope: Literal["coupled-wind-gust-point", "model-cycle"]
    detail: str
    affected_variable_ids: tuple[str, ...]
    source_gust_m_s: float | None = None
    source_sustained_speed_m_s: float | None = None
    shortfall_m_s: float | None = None
    shortfall_floor_tolerance_m_s: float | None = None

    @model_validator(mode="after")
    def _check_affected(self) -> ExcludedContributorRecord:
        if not self.affected_variable_ids:
            raise ValueError("an exclusion record must name at least one affected variable")
        if len(set(self.affected_variable_ids)) != len(self.affected_variable_ids):
            raise ValueError("affected_variable_ids must not repeat a variable")
        if not self.reason or not self.detail:
            raise ValueError("an exclusion record must carry a reason and a detail")
        return self


class BlendContributionRow(BaseModel):
    """One ``(variable, location, target_horizon)`` row of the
    contribution manifest (Section 4.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    variable_id: str
    location: StationId
    target_horizon: int
    target_valid_time: str
    operator_id: str
    availability_state: VariableAvailabilityStateLiteral
    fallback_row_id: FallbackRowId | None
    contributors: tuple[ContributorRecord, ...]
    excluded_contributors: tuple[ExcludedContributorRecord, ...] = ()
    unrounded_sum: float
    serialized_output: float
    gust_floor_applied: bool = False
    final_gust_epsilon_floor_applied: bool = False
    probability_deterministic_tension: str | None = None

    @model_validator(mode="after")
    def _check_reconstructable(self) -> BlendContributionRow:
        if self.availability_state in ("unavailable", "inconsistent"):
            return self
        reconstructed = sum(c.weighted_contribution for c in self.contributors)
        if abs(reconstructed - self.unrounded_sum) > _RECONSTRUCTION_TOLERANCE:
            raise ValueError(
                f"contribution row for ({self.variable_id!r}, {self.location!r}, "
                f"{self.target_horizon!r}) does not reconstruct: sum of weighted "
                f"contributions {reconstructed!r} != unrounded_sum {self.unrounded_sum!r} "
                f"within {_RECONSTRUCTION_TOLERANCE!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_target_horizon(self) -> BlendContributionRow:
        if not (1 <= self.target_horizon <= 36):
            raise ValueError(f"target_horizon must be in 1..36, got {self.target_horizon!r}")
        return self


class BlendContributionManifest(BaseModel):
    """Section 4.7/5.1: ``blend-contribution-manifest.v1``. Must
    independently reconstruct every forecast value; rejects a missing/
    duplicate row."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["blend-contribution-manifest.v1"] = "blend-contribution-manifest.v1"
    rows: tuple[BlendContributionRow, ...]
    expected_variable_ids: tuple[str, ...]
    expected_locations: tuple[StationId, ...]
    expected_target_horizons: tuple[int, ...]
    configuration_digest: str
    code_revision: str
    environment_digest: str
    lockfile_digest: str

    @model_validator(mode="after")
    def _check_completeness(self) -> BlendContributionManifest:
        seen: set[tuple[str, str, int]] = set()
        for row in self.rows:
            key = (row.variable_id, row.location, row.target_horizon)
            if key in seen:
                raise ValueError(f"duplicate contribution row: {key!r}")
            seen.add(key)

        expected = {
            (variable_id, location, horizon)
            for variable_id in self.expected_variable_ids
            for location in self.expected_locations
            for horizon in self.expected_target_horizons
        }
        missing = expected - seen
        if missing:
            raise ValueError(
                f"contribution manifest is missing {len(missing)} expected rows: "
                f"{sorted(missing)!r}"
            )
        extra = seen - expected
        if extra:
            raise ValueError(
                f"contribution manifest has {len(extra)} unexpected rows: {sorted(extra)!r}"
            )
        return self


class IdentityBiasCorrection(BaseModel):
    """Section 5.1: ``identity-bias-correction.v1`` -- explicit
    all-zero correction for Phase 2. No learned adjustment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["identity-bias-correction.v1"] = "identity-bias-correction.v1"
    correction_id: Literal["identity.v1"] = "identity.v1"
    applied_delta: float = 0.0

    @model_validator(mode="after")
    def _check_zero_delta(self) -> IdentityBiasCorrection:
        if self.applied_delta != 0.0:
            raise ValueError(
                f"IdentityBiasCorrection.applied_delta must be exactly 0.0, got "
                f"{self.applied_delta!r}"
            )
        return self
