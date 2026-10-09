"""Read approved native probability events without inventing hourly probabilities."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from mesoforge.common.qpf_intervals import interval_time
from mesoforge.forecasting.provisional_policy import (
    POP6,
    POP6_THRESHOLD,
    PROVISIONAL_MULTIMODEL_POLICY,
    provisional_policy,
)


def six_hour_events(hours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project final active event fields at their exact endpoint; ignore shadows.

    Non-endpoint hours may omit the field or retain an explicit unavailable row.
    An available field with contradictory timing/units/policy is not publishable.
    """
    result = []
    seen = set()
    for hour in hours:
        field = hour.get("surface", {}).get("fields", {}).get(POP6)
        if not field or field.get("role") != "active_blended_baseline":
            continue
        if field.get("value") is None and not field.get("interval_start"):
            continue
        if (
            field.get("policy_family") != PROVISIONAL_MULTIMODEL_POLICY
            or field.get("policy") != provisional_policy(POP6).policy_id
        ):
            continue
        left, right = (interval_time(field[key]) for key in ("interval_start", "interval_end"))
        if (
            right - left != timedelta(hours=6)
            or right != interval_time(hour["valid_time"])
            or field.get("unit") != "1"
            or field.get("temporal_semantics") != "probability"
            or field.get("interval_closure") != "left_open_right_closed"
            or field.get("threshold") != POP6_THRESHOLD
            or field.get("event_duration_hours") != 6
            or field.get("spatial_support") != {"kind": "grid_point"}
        ):
            raise ValueError("Native six-hour probability contradicts its approved event contract")
        value = field.get("value")
        if field.get("status") != ("unavailable" if value is None else "available"):
            raise ValueError("Native six-hour probability availability contradicts its value")
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
        ):
            raise ValueError("Native six-hour probability must be a fraction or unavailable")
        identity = (left, right)
        if identity in seen:
            raise ValueError("Native six-hour probability event is repeated")
        seen.add(identity)
        result.append(
            {
                key: field[key]
                for key in (
                    "interval_start",
                    "interval_end",
                    "interval_closure",
                    "unit",
                    "threshold",
                    "policy",
                )
            }
            | {"value": value}
        )
    return result


def six_hour_summary(
    events: list[dict[str, Any]], *, start: datetime, end: datetime
) -> dict[str, Any]:
    """Maximum of available fully contained six-hour events, never a daily chance."""
    contained = [
        row
        for row in events
        if start <= interval_time(row["interval_start"])
        and interval_time(row["interval_end"]) <= end
    ]
    values = [row["value"] for row in contained if row["value"] is not None]
    crossing = sum(
        interval_time(row["interval_start"]) < end
        and interval_time(row["interval_end"]) > start
        and row not in contained
        for row in events
    )
    return {
        "maximum_available_six_hour_pop": max(values) if values else None,
        "available_events": len(values),
        "retained_contained_events": len(contained),
        "boundary_crossing_events": crossing,
        "basis": "available_whole_six_hour_events_not_hourly_or_daily_probability",
    }
