"""Versioned shadow policy data using the existing field-specific kernel contracts.

No policy in this contract can become active. The caller explicitly chooses it for
background comparison; absence of a candidate leaves production unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mesoforge.catalog.configuration import FallbackWeightTable
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.coherence import DEW_POINT, GUST, QPF, TEMPERATURE, WIND
from mesoforge.forecasting.recipes import Recipe


class CandidateBlendPolicy(BaseModel):
    """One field override; current missing-data/table rules remain explicit data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["mesoforge.candidate-blend-policy.v1"] = (
        "mesoforge.candidate-blend-policy.v1"
    )
    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    field: str
    parent_policy: str = Field(min_length=1)
    parameters: Recipe | FallbackWeightTable
    lead_start: int = Field(default=1, ge=1, le=36)
    lead_end: int = Field(default=36, ge=1, le=36)
    missing_behavior: Literal["require_all", "approved_subset_row"]
    creation_source: str = Field(min_length=1)
    evidence_cutoff: datetime
    created_at: datetime
    activated_at: datetime | None = None
    lifecycle_role: Literal["candidate", "shadow"] = "candidate"
    provenance: dict[str, Any]

    @field_validator("evidence_cutoff", "created_at", "activated_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Candidate policy times require explicit timezones")
            return value.astimezone(UTC)
        return None

    @model_validator(mode="after")
    def _contract(self) -> CandidateBlendPolicy:
        if self.field not in (TEMPERATURE, DEW_POINT, WIND, GUST, QPF):
            raise ValueError("Candidate field has no existing FieldBlendEngine kernel")
        if self.lead_start > self.lead_end:
            raise ValueError("Candidate lead interval is reversed")
        if self.evidence_cutoff > self.created_at:
            raise ValueError("Candidate cannot use evidence from after its creation")
        if self.activated_at is not None and self.activated_at < self.created_at:
            raise ValueError("Shadow activation cannot precede candidate creation")
        if self.lifecycle_role == "shadow" and self.activated_at is None:
            raise ValueError("Shadow policy requires its explicit activation time")
        if self.field == TEMPERATURE:
            if not isinstance(self.parameters, Recipe) or self.parameters.field != self.field:
                raise ValueError("Temperature candidate requires its scalar recipe")
            if (self.parameters.name, self.parameters.version) != (self.policy_id, self.version):
                raise ValueError("Candidate identity differs from recipe identity")
            if self.missing_behavior != self.parameters.missing_policy:
                raise ValueError("Candidate missing behavior differs from recipe")
        else:
            if not isinstance(self.parameters, FallbackWeightTable):
                raise ValueError("Candidate requires the existing fallback weight table")
            if self.parameters.table_id != self.policy_id:
                raise ValueError("Candidate identity differs from table identity")
            if self.missing_behavior != "approved_subset_row":
                raise ValueError("Fallback table requires explicit subset-row missing behavior")
        return self

    @property
    def digest(self) -> str:
        return str(canonical_json_digest(self.model_dump(mode="json")))

    def validate_execution(self, analysis_cutoff: datetime) -> None:
        """Neither future training evidence nor a future policy may enter a replay."""
        if analysis_cutoff.tzinfo is None or analysis_cutoff.utcoffset() is None:
            raise ValueError("Candidate execution requires an aware analysis cutoff")
        for label, value in (
            ("evidence", self.evidence_cutoff),
            ("creation", self.created_at),
            ("activation", self.activated_at),
        ):
            if value is not None and value > analysis_cutoff:
                raise ValueError(f"Candidate {label} time exceeds forecast analysis cutoff")

    @property
    def contributors(self) -> tuple[str, ...]:
        if isinstance(self.parameters, Recipe):
            return tuple(item.model for item in self.parameters.contributors)
        return tuple(
            sorted({model for row in self.parameters.rows for model in row.available_models})
        )
