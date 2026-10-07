"""Five complete local-day summaries from an immutable final forecast canvas.

This boundary cannot extend a horizon or fill a field. The current 36-hour product
deliberately fails its coverage gate. Longer fixtures exercise presentation only,
not approval of a longer numerical forecast policy.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from mesoforge.common.errors import IntegrityError
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.condition_wording import WORDING_POLICY
from mesoforge.forecasting.conditions import RULESET_ID, _describe_hour
from mesoforge.forecasting.transitions import build_transitions

DOCUMENT_POLICY = "mesoforge-five-local-day-presentation.v1"
_HOUR = timedelta(hours=1)


class ForecastCoverageError(ValueError):
    """A saved issuance cannot support the requested complete calendar-day product."""


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Saved forecast times must be timezone-aware")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _numeric(component: dict[str, Any], unit: str) -> float | None:
    if component.get("state") != "known":
        return None
    value = component.get("value")
    if (
        component.get("unit") != unit
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        raise ValueError("Known presentation component has invalid units or value")
    if not math.isfinite(value):
        raise ValueError("Known presentation component has invalid units or value")
    return float(value)


def _display_hour(hour: dict[str, Any]) -> dict[str, Any]:
    """Whitelist final active values, retaining unrounded numerical/event identity."""
    components = hour["components"]
    row: dict[str, Any] = {
        "valid_time": hour["valid_time"],
        "condition": hour["rendering"]["text"],
        "sky": components["sky"].get("sky_category")
        if components["sky"]["state"] == "known"
        else None,
    }
    for name, unit in (
        ("temperature", "K"),
        ("dew_point", "K"),
        ("wind_u", "m/s"),
        ("wind_v", "m/s"),
        ("wind_speed", "m/s"),
        ("wind_gust", "m/s"),
        ("qpf", "kg/m^2"),
        ("pop", "1"),
    ):
        component = components[name]
        row[name] = _numeric(component, unit)
        if name in ("qpf", "pop"):
            row[f"{name}_interval"] = component.get("interval")
            row[f"{name}_threshold"] = component.get("threshold")
    return row


def _complete_values(hours: list[dict[str, Any]], field: str) -> list[float] | None:
    values = [hour[field] for hour in hours]
    return None if any(value is None for value in values) else values


def _daily_summary(hours: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    expected = int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 3600)
    if len(hours) != expected:
        raise ForecastCoverageError("A local calendar day is incomplete")
    temperatures = _complete_values(hours, "temperature")
    gusts = _complete_values(hours, "wind_gust")
    qpf = _complete_values(hours, "qpf")
    pop = _complete_values(hours, "pop")
    u, v = _complete_values(hours, "wind_u"), _complete_values(hours, "wind_v")
    mean_u = sum(u) / len(u) if u is not None else None
    mean_v = sum(v) / len(v) if v is not None else None
    wind_speed = wind_direction = None
    if mean_u is not None and mean_v is not None:
        wind_speed = math.hypot(mean_u, mean_v)
        if wind_speed != 0:
            wind_direction = math.degrees(math.atan2(-mean_u, -mean_v)) % 360
    # Most frequent approved hourly wording; ties go to earliest occurrence.
    # This is a labeled summary, never a new daily occurrence/probability forecast.
    texts = [hour["condition"] for hour in hours]
    counts = Counter(texts)
    dominant = max(counts, key=lambda text: counts[text])
    return {
        "date": start.date().isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours": expected,
        "high_k": max(temperatures) if temperatures is not None else None,
        "low_k": min(temperatures) if temperatures is not None else None,
        "max_gust_mps": max(gusts) if gusts is not None else None,
        "qpf_kg_m2": sum(qpf) if qpf is not None else None,
        "maximum_hourly_pop": max(pop) if pop is not None else None,
        "mean_wind_u_mps": mean_u,
        "mean_wind_v_mps": mean_v,
        "vector_mean_wind_speed_mps": wind_speed,
        "vector_mean_wind_direction": wind_direction,
        "most_frequent_hourly_condition": dominant,
        "condition_hours": counts[dominant],
        "availability": {
            name: {
                "available_hours": sum(hour[name] is not None for hour in hours),
                "expected_hours": expected,
            }
            for name in ("temperature", "dew_point", "wind_speed", "wind_gust", "qpf", "pop", "sky")
        },
    }


def _validated_point(saved: dict[str, Any], location: dict[str, Any]) -> dict[str, Any]:
    forecast = saved["forecast"]
    if (forecast["latitude"], forecast["longitude"]) != (location["lat"], location["lon"]):
        raise IntegrityError("Saved forecast does not match the configured location")
    grid = forecast.get("local_grid_baseline", {})
    if grid.get("version") != "mesoforge.local-surface-baseline.v2":
        raise IntegrityError("A saved final nested local grid is required")
    if str(canonical_json_digest(grid)) != forecast.get("local_grid", {}).get("sha256"):
        raise IntegrityError("Saved final grid digest differs from its point attachment")
    centers = [cell for cell in grid["cells"] if cell.get("is_forecast_point")]
    if len(centers) != 1 or centers[0]["hours"] != forecast["hours"]:
        raise IntegrityError("Saved point is not the exact final grid center")
    if (centers[0]["latitude"], centers[0]["longitude"]) != (location["lat"], location["lon"]):
        raise IntegrityError("Saved final grid center differs from the configured location")
    return cast(dict[str, Any], centers[0])


def build_forecast_document(
    saved: dict[str, Any], *, location: dict[str, Any], days: int = 5
) -> dict[str, Any]:
    """Build five complete local days, never extending/reissuing an existing forecast.

    The interval belongs to its local starting day: an amount ending at midnight
    closes the preceding day. Hourly instantaneous extrema use those same endpoint
    samples and are labeled hourly extrema, not continuous daily maxima/minima.
    A mid-day rolling 120-hour forecast cannot cover five full local days. It fails
    rather than silently clipping the overnight portion or labeling partial days full.
    DST day lengths are determined by the IANA zone, not an assumed 24-hour day.
    """
    if days != 5:
        raise ValueError("This product supports exactly five complete local days")
    zone = ZoneInfo(location.get("display_timezone", "UTC"))
    forecast = saved["forecast"]
    source_hours = forecast["hours"]
    if len(source_hours) < 119:
        raise ForecastCoverageError(
            "Five complete local days are required; the saved forecast has "
            f"{len(source_hours)} hours. Presentation cannot extend its scientific horizon."
        )
    reference = _time(forecast["target_reference_time"])
    times = [_time(hour["valid_time"]) for hour in source_hours]
    if any(
        instant != reference + _HOUR * index or hour["horizon_hours"] != index
        for index, (instant, hour) in enumerate(zip(times, source_hours, strict=True), 1)
    ):
        raise IntegrityError("Saved hours must form the exact continuous reference-hour sequence")
    local_reference = reference.astimezone(zone)
    first_date = local_reference.date()
    if local_reference.timetz().replace(tzinfo=None) != time(0):
        first_date += timedelta(days=1)
    boundaries = [
        datetime.combine(first_date + timedelta(days=i), time(0), zone) for i in range(days + 1)
    ]
    start, end = boundaries[0].astimezone(UTC), boundaries[-1].astimezone(UTC)
    if reference > start or times[-1] < end:
        raise ForecastCoverageError(
            "Saved coverage does not include five complete local calendar days; "
            "a rolling 120-hour window can include partial first/last days."
        )
    center = _validated_point(saved, location)
    described = [
        _describe_hour(
            hour,
            "/forecast/hours/" + str(index),
            cell_available=center.get("status") == "calculated",
            cell_reasons=tuple(center.get("missing_reasons", [])),
        )
        for index, hour in enumerate(source_hours)
        if start < _time(hour["valid_time"]) <= end
    ]
    hours = [_display_hour(hour) for hour in described]
    summaries = [
        _daily_summary(
            [
                hour
                for hour in hours
                if left.astimezone(UTC) < _time(hour["valid_time"]) <= right.astimezone(UTC)
            ],
            left,
            right,
        )
        for left, right in zip(boundaries, boundaries[1:], strict=False)
    ]
    # Reuse the existing deterministic transition detector/rendering unchanged.
    transitions = build_transitions(
        {
            "center_point": {"hours": described},
            "input": {},
            "schema_version": "mesoforge.weather-condition-preview.v2",
            "ruleset_id": RULESET_ID,
            "wording_policy": WORDING_POLICY,
            "scope": {"requested": "point"},
        },
        display_timezone=zone.key,
        timezone_source="configured_location",
    )
    fixture = forecast.get("data_kind") != "real_prepared_guidance"
    desk = forecast.get("ai_desk", {})
    outcome = desk.get("completion_reason")
    edits = len(desk.get("accepted_recipes", []))
    if outcome == "no_edit":
        ai_status = "Reviewed - no changes justified"
    elif outcome == "complete":
        ai_status = f"Reviewed - {edits} validated edit" + ("s" if edits != 1 else "")
    elif desk:
        ai_status = "Fallback - latest validated forecast retained"
    else:
        ai_status = "AI assessment not available"
    available_highs = [day["high_k"] for day in summaries if day["high_k"] is not None]
    available_lows = [day["low_k"] for day in summaries if day["low_k"] is not None]
    headline = "Your five-day weather outlook"
    if len(available_highs) == days and len(available_lows) == days:
        low = (min(available_lows) - 273.15) * 1.8 + 32
        high = (max(available_highs) - 273.15) * 1.8 + 32
        headline = f"Temperatures from {low:.0f} to {high:.0f} F across five days"
    return {
        "document_policy": DOCUMENT_POLICY,
        "issued_forecast_id": saved["issued_forecast_id"],
        "issued_payload_digest": str(canonical_json_digest(saved)),
        "fixture": fixture,
        "location": {
            "name": location.get("name", "Configured location"),
            "lat": location["lat"],
            "lon": location["lon"],
        },
        "display_timezone": zone.key,
        "issued_at": _iso(_time(saved["issued_at"])),
        "valid_start": _iso(start),
        "valid_end": _iso(end),
        "source_valid_start": _iso(reference),
        "source_valid_end": _iso(times[-1]),
        "hours": hours,
        "days": summaries,
        "headline": headline,
        "transitions": [item["text"] for item in transitions["rendering"]["items"]],
        "ai": {
            "provider": desk.get("provider"),
            "model": desk.get("model"),
            "outcome": outcome,
            "display_status": ai_status,
        },
        "revision": saved.get("code_identity", {}).get("git_commit", "unavailable"),
        "notes": [
            "High/low are extrema of hourly forecast samples, including overnight hours.",
            "Precipitation chance is the maximum native hourly PoP, not a daily probability. "
            "Each hourly event is more than 0.01 inches of liquid precipitation.",
            "Daily liquid totals sum exact, consecutive hourly intervals. Missing hours "
            "make the daily total unavailable; they are never treated as zero.",
            "Wind is the vector mean of hourly U/V. Conditions show the most frequent "
            "approved hourly wording; transitions describe changes separately.",
        ],
    }
