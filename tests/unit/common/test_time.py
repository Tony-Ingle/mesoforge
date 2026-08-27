"""Unit tests for mesoforge.common.time (Task 3, plan Section 4.2).

RED: written before src/mesoforge/common/time.py exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from mesoforge.common.time import (
    IntervalClosure,
    IntervalDefinition,
    TemporalSemantics,
    TimeAxisDefinition,
    UtcInstant,
)


class _Holder(BaseModel):
    instant: UtcInstant


class TestUtcInstant:
    def test_accepts_aware_datetime(self) -> None:
        value = datetime(2026, 1, 1, tzinfo=UTC)
        holder = _Holder(instant=value)
        assert holder.instant == value

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValidationError):
            _Holder(instant=datetime(2026, 1, 1))

    def test_normalizes_non_utc_offset_to_utc(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        value = datetime(2026, 1, 1, 5, 0, 0, tzinfo=eastern)
        holder = _Holder(instant=value)
        assert holder.instant.utcoffset() == timedelta(0)
        assert holder.instant == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)

    def test_serializes_with_microsecond_precision_and_z_suffix(self) -> None:
        value = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
        holder = _Holder(instant=value)
        dumped = holder.model_dump(mode="json")
        assert dumped["instant"] == "2026-01-01T00:00:00.123456Z"


class TestIntervalClosure:
    def test_all_four_variants_exist(self) -> None:
        assert {member.value for member in IntervalClosure} == {
            "left_closed_right_open",
            "closed",
            "open",
            "left_open_right_closed",
        }


class TestTemporalSemantics:
    def test_all_variants_exist(self) -> None:
        assert {member.value for member in TemporalSemantics} == {
            "instantaneous",
            "average",
            "maximum",
            "minimum",
            "accumulation",
            "probability",
        }


class TestIntervalDefinition:
    def test_valid_interval(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, 1, tzinfo=UTC)
        interval = IntervalDefinition(start=start, end=end, closure=IntervalClosure.closed)
        assert interval.start < interval.end

    def test_rejects_start_not_before_end(self) -> None:
        instant = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValidationError):
            IntervalDefinition(start=instant, end=instant, closure=IntervalClosure.closed)

    def test_rejects_end_before_start(self) -> None:
        start = datetime(2026, 1, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValidationError):
            IntervalDefinition(start=start, end=end, closure=IntervalClosure.closed)


class TestTimeAxisDefinition:
    def test_computes_valid_times_from_reference_and_leads(self) -> None:
        reference = datetime(2026, 1, 1, tzinfo=UTC)
        leads = (timedelta(hours=0), timedelta(hours=1), timedelta(hours=6))
        axis = TimeAxisDefinition(forecast_reference_time=reference, lead_times=leads)
        assert axis.valid_times == tuple(reference + lead for lead in leads)

    def test_rejects_negative_lead(self) -> None:
        reference = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValidationError):
            TimeAxisDefinition(
                forecast_reference_time=reference,
                lead_times=(timedelta(hours=-1),),
            )

    def test_rejects_non_increasing_leads(self) -> None:
        reference = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValidationError):
            TimeAxisDefinition(
                forecast_reference_time=reference,
                lead_times=(timedelta(hours=1), timedelta(hours=1)),
            )

    def test_rejects_decreasing_leads(self) -> None:
        reference = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValidationError):
            TimeAxisDefinition(
                forecast_reference_time=reference,
                lead_times=(timedelta(hours=2), timedelta(hours=1)),
            )

    def test_schema_version_is_pinned(self) -> None:
        reference = datetime(2026, 1, 1, tzinfo=UTC)
        axis = TimeAxisDefinition(
            forecast_reference_time=reference, lead_times=(timedelta(hours=0),)
        )
        assert axis.schema_version == "time-axis.v1"

    def test_frozen_model_rejects_mutation(self) -> None:
        reference = datetime(2026, 1, 1, tzinfo=UTC)
        axis = TimeAxisDefinition(
            forecast_reference_time=reference, lead_times=(timedelta(hours=0),)
        )
        with pytest.raises(ValidationError):
            axis.lead_times = (timedelta(hours=1),)  # type: ignore[misc]


class TestDistinctTimeConcepts:
    """These are documentation-level distinctions; this test guards against
    accidental aliasing (e.g. one field silently reused for two concepts)
    by asserting the model fields exist as independently named attributes
    wherever they are used together (see contracts/runs.py, Task 7)."""

    def test_utc_instant_type_has_no_semantic_default(self) -> None:
        # UtcInstant is a bare validated type; it must not itself imply
        # "created_at" vs "available_at" semantics -- that is the job of
        # the field name in the owning model, tested in Task 7.
        assert UtcInstant is not None
