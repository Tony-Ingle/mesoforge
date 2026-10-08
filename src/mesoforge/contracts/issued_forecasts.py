"""Immutable metadata for an explicitly issued coordinate forecast."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mesoforge.common.horizon import LEGACY_HORIZON, ForecastHorizon
from mesoforge.common.identifiers import Digest
from mesoforge.common.time import UtcInstant


class IssuedForecastRecord(BaseModel):
    """Searchable header; the complete forecast lives in verified object storage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["issued-forecast.v1"] = "issued-forecast.v1"
    issued_forecast_id: UUID
    batch_run_id: UUID
    location_index: Annotated[int, Field(ge=0)]
    latitude: Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
    longitude: Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
    issued_at: UtcInstant
    target_reference_time: UtcInstant
    forecast_horizon_hours: int = LEGACY_HORIZON.duration_hours
    # Physical object identity remains content_digest; historical raw JSON has
    # identical logical/physical digests and needs no backfill or object rewrite.
    forecast_payload_digest: Digest | None = None
    content_digest: Digest

    @property
    def payload_digest(self) -> Digest:
        """Canonical decoded issuance identity, independent of its storage encoding."""
        return self.forecast_payload_digest or self.content_digest

    @field_validator("forecast_horizon_hours")
    @classmethod
    def supported_horizon(cls, value: int) -> int:
        """A searchable duration mirrors the payload's shared horizon contract."""
        return ForecastHorizon(value).duration_hours
