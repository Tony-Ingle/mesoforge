"""Scientific elapsed-time coverage, explicit metadata and historical readback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mesoforge.common.horizon import (
    FIVE_DAY_HORIZON,
    FORECAST_HORIZON_SCHEMA,
    LEGACY_HORIZON,
    ForecastHorizon,
    horizon_for,
)


def _rows(reference: datetime, horizon: ForecastHorizon) -> list[dict]:
    return [
        {
            "horizon_hours": lead,
            "valid_time": (reference.astimezone(UTC) + timedelta(hours=lead)).isoformat(),
        }
        for lead in horizon.leads
    ]


def test_explicit_horizon_roundtrip_does_not_reinterpret_history() -> None:
    assert horizon_for({}) == LEGACY_HORIZON
    assert LEGACY_HORIZON.leads == tuple(range(1, 37))
    assert FIVE_DAY_HORIZON.leads == tuple(range(1, 121))
    for horizon in (LEGACY_HORIZON, FIVE_DAY_HORIZON):
        assert horizon_for({"forecast_horizon": horizon.payload()}) == horizon
        reference = datetime(2026, 10, 8, 12, tzinfo=UTC)
        horizon.validate_hour_rows(_rows(reference, horizon), reference)


@pytest.mark.parametrize("duration", [True, 120.0, "120", 0, 35, 37, 119, 121])
def test_duration_is_supported_and_strict(duration: object) -> None:
    with pytest.raises(ValueError, match="duration"):
        ForecastHorizon(duration)  # type: ignore[arg-type]


@pytest.mark.parametrize("step", [True, 1.0, "1", 0, 3])
def test_state_cadence_is_strictly_hourly(step: object) -> None:
    with pytest.raises(ValueError, match="cadence"):
        ForecastHorizon(120, step)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "declaration",
    [
        None,
        {},
        120,
        {"schema": "unknown", "duration_hours": 120, "state_step_hours": 1},
        {"schema": FORECAST_HORIZON_SCHEMA, "duration_hours": 120},
        {**FIVE_DAY_HORIZON.payload(), "extra": "unknown"},
    ],
)
def test_malformed_metadata_cannot_fall_back_to_legacy(declaration: object) -> None:
    with pytest.raises(ValueError):
        horizon_for({"forecast_horizon": declaration})


def test_elapsed_hours_and_dst_calendar_dates_are_distinct() -> None:
    # America/Chicago falls back during this window: 120 elapsed hours is not
    # five local midnight-to-midnight days, and the UTC axis must not shift.
    reference = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("America/Chicago"))
    rows = _rows(reference, FIVE_DAY_HORIZON)
    FIVE_DAY_HORIZON.validate_hour_rows(rows, reference)
    end = datetime.fromisoformat(rows[-1]["valid_time"])
    assert (end - reference.astimezone(UTC)).total_seconds() == 120 * 3600
    assert end.astimezone(reference.tzinfo).hour == 11


def test_exact_hour_rows_reject_shortened_reordered_or_relabelled_forecasts() -> None:
    reference = datetime(2026, 10, 8, 12, tzinfo=UTC)
    valid = _rows(reference, FIVE_DAY_HORIZON)
    with pytest.raises(ValueError, match="cover"):
        FIVE_DAY_HORIZON.validate_hour_rows(valid[:36], reference)
    with pytest.raises(ValueError, match="ordered"):
        FIVE_DAY_HORIZON.validate_hour_rows(list(reversed(valid)), reference)
    for change in (
        {"horizon_hours": True},
        {"horizon_hours": 1.0},
        {"valid_time": "2026-10-08T14:00:00Z"},
        {"valid_time": "2026-10-08T13:00:00"},
        {"valid_time": "invalid"},
        {"valid_time": None},
    ):
        altered = [{**valid[0], **change}, *valid[1:]]
        with pytest.raises(ValueError):
            FIVE_DAY_HORIZON.validate_hour_rows(altered, reference)
    for bad_reference in (
        reference.replace(tzinfo=None),
        reference + timedelta(minutes=1),
    ):
        with pytest.raises(ValueError):
            FIVE_DAY_HORIZON.validate_hour_rows(valid, bad_reference)
