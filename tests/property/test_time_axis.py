"""Property-based tests for TimeAxisDefinition's valid-time invariant.

RED: written before src/mesoforge/common/time.py exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.common.time import TimeAxisDefinition

_reference_times = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
).map(lambda dt: dt.replace(tzinfo=UTC))

_increasing_lead_lists = st.lists(
    st.integers(min_value=0, max_value=10_000), min_size=1, max_size=12, unique=True
).map(lambda values: tuple(timedelta(minutes=m) for m in sorted(values)))


@given(reference=_reference_times, leads=_increasing_lead_lists)
@settings(max_examples=100)
def test_valid_time_equals_reference_plus_lead(
    reference: datetime, leads: tuple[timedelta, ...]
) -> None:
    axis = TimeAxisDefinition(forecast_reference_time=reference, lead_times=leads)
    for lead, valid_time in zip(leads, axis.valid_times, strict=True):
        assert valid_time == reference + lead


@given(reference=_reference_times, leads=_increasing_lead_lists)
@settings(max_examples=100)
def test_valid_times_are_strictly_increasing_when_leads_are(
    reference: datetime, leads: tuple[timedelta, ...]
) -> None:
    axis = TimeAxisDefinition(forecast_reference_time=reference, lead_times=leads)
    valid_times = axis.valid_times
    for earlier, later in zip(valid_times, valid_times[1:], strict=False):
        assert earlier < later
