"""Immutable metadata for an explicitly issued coordinate forecast."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    content_digest: Digest
