"""Canonical time primitives shared across all MesoForge control-plane
contracts (plan Section 4.2).

``UtcInstant`` distinguishes itself from ``datetime`` only by validation:
it is a type annotation used inside Pydantic models, not a class to
instantiate directly. The concrete time concepts it is used for
(``forecast_reference_time``, ``forecast_issue_time``,
``information_cutoff``, ``created_at``, ``available_at``, ``ingested_at``,
``registered_at``) are distinguished by field name in the owning models
(``contracts/runs.py``, ``contracts/artifacts.py``), never by this type.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (naive datetimes are rejected)")
    return value.astimezone(UTC)


UtcInstant = Annotated[datetime, AfterValidator(_require_aware_utc)]


class IntervalClosure(StrEnum):
    left_closed_right_open = "left_closed_right_open"
    closed = "closed"
    open = "open"
    left_open_right_closed = "left_open_right_closed"


class TemporalSemantics(StrEnum):
    instantaneous = "instantaneous"
    average = "average"
    maximum = "maximum"
    minimum = "minimum"
    accumulation = "accumulation"
    probability = "probability"


class IntervalDefinition(BaseModel):
    """A `start < end` span with an explicit closure. No implicit timezone
    conversion happens beyond the standard UtcInstant normalization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start: UtcInstant
    end: UtcInstant
    closure: IntervalClosure

    @model_validator(mode="after")
    def _check_start_before_end(self) -> IntervalDefinition:
        if not self.start < self.end:
            raise ValueError(
                f"IntervalDefinition requires start < end, got {self.start!r} >= {self.end!r}"
            )
        return self


class TimeAxisDefinition(BaseModel):
    """Section 4.2: forecast reference time plus a strictly increasing,
    nonnegative sequence of lead times; valid times are derived, never
    independently supplied (except where a caller-supplied ``valid_times``
    is validated to equal this invariant exactly -- Task 7/contracts)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "time-axis.v1"
    forecast_reference_time: UtcInstant
    lead_times: tuple[timedelta, ...]

    @model_validator(mode="after")
    def _check_lead_times(self) -> TimeAxisDefinition:
        if len(self.lead_times) == 0:
            raise ValueError("lead_times must contain at least one value")
        previous: timedelta | None = None
        for lead in self.lead_times:
            if lead < timedelta(0):
                raise ValueError(f"lead_times must be nonnegative, got {lead!r}")
            if previous is not None and lead <= previous:
                raise ValueError(
                    f"lead_times must be strictly increasing, got {previous!r} followed by {lead!r}"
                )
            previous = lead
        return self

    @property
    def valid_times(self) -> tuple[datetime, ...]:
        return tuple(self.forecast_reference_time + lead for lead in self.lead_times)
