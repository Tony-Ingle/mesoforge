"""Explicit scientific forecast windows, independent of local calendar-day cards.

This contract does not select a production default or extend provider capability.
Readers decide which historical schema permits an absent horizon declaration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

FORECAST_HORIZON_SCHEMA = "mesoforge.forecast-horizon.v1"


@dataclass(frozen=True, slots=True)
class ForecastHorizon:
    """A supported elapsed-time window containing hourly state valid times."""

    duration_hours: int
    state_step_hours: int = 1

    def __post_init__(self) -> None:
        if type(self.duration_hours) is not int or self.duration_hours not in (36, 120):
            raise ValueError("Forecast duration must be exactly 36 or 120 integer hours")
        if type(self.state_step_hours) is not int or self.state_step_hours != 1:
            raise ValueError("Forecast state cadence must be exactly one integer hour")

    @property
    def leads(self) -> tuple[int, ...]:
        return tuple(range(1, self.duration_hours + 1))

    def payload(self) -> dict[str, str | int]:
        return {
            "schema": FORECAST_HORIZON_SCHEMA,
            "duration_hours": self.duration_hours,
            "state_step_hours": self.state_step_hours,
        }

    def validate_hour_rows(
        self, rows: Sequence[Mapping[str, Any]], reference_time: datetime
    ) -> None:
        """Require exact ordered leads and aware valid times; never repair a window."""
        if (
            not isinstance(reference_time, datetime)
            or reference_time.tzinfo is None
            or reference_time.utcoffset() is None
        ):
            raise ValueError("Forecast reference time must be timezone-aware")
        reference = reference_time.astimezone(UTC)
        if reference.minute or reference.second or reference.microsecond:
            raise ValueError("Forecast reference time must be an exact UTC hour")
        if len(rows) != self.duration_hours:
            raise ValueError("Forecast hour rows do not cover the declared horizon")
        for lead, row in zip(self.leads, rows, strict=True):
            if type(row.get("horizon_hours")) is not int or row["horizon_hours"] != lead:
                raise ValueError("Forecast hour leads must match the declared ordered horizon")
            text = row.get("valid_time")
            if not isinstance(text, str):
                raise ValueError("Forecast valid time must be an aware ISO timestamp")
            try:
                valid = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError("Forecast valid time must be an aware ISO timestamp") from exc
            if valid.tzinfo is None or valid.utcoffset() is None:
                raise ValueError("Forecast valid time must be timezone-aware")
            if valid.astimezone(UTC) != reference + timedelta(hours=lead):
                raise ValueError("Forecast valid time must equal reference plus its exact lead")


LEGACY_HORIZON = ForecastHorizon(36)
FIVE_DAY_HORIZON = ForecastHorizon(120)


def horizon_for(document: Mapping[str, Any]) -> ForecastHorizon:
    """Read an explicit contract; absence alone retains the historical 36-hour window.

    Callers own the known-legacy boundary. Null, partial or unknown declarations
    are corrupt evidence, not permission to silently apply a current default.
    """
    if "forecast_horizon" not in document:
        return LEGACY_HORIZON
    value = document["forecast_horizon"]
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "duration_hours",
        "state_step_hours",
    }:
        raise ValueError("Forecast horizon requires its exact versioned metadata")
    if value["schema"] != FORECAST_HORIZON_SCHEMA:
        raise ValueError("Unsupported forecast horizon schema")
    return ForecastHorizon(value["duration_hours"], value["state_step_hours"])
