"""Bounded product summaries from an immutable final forecast canvas.

This boundary cannot extend a horizon or fill a field. The current 36-hour product
is explicit; five complete local days still require independently sufficient saved
coverage. Longer fixtures do not approve a longer numerical forecast policy.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from mesoforge.common.errors import IntegrityError
from mesoforge.common.horizon import FIVE_DAY_HORIZON, horizon_for
from mesoforge.common.qpf_intervals import summarize_qpf_intervals, validate_qpf_intervals
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.condition_wording import WORDING_POLICY
from mesoforge.forecasting.conditions import RULESET_ID, _describe_hour
from mesoforge.forecasting.probability_events import six_hour_events, six_hour_summary
from mesoforge.forecasting.transitions import build_transitions

DOCUMENT_POLICY = "mesoforge-five-local-day-presentation.v1"
HOURS_DOCUMENT_POLICY = "mesoforge-36-hour-presentation.v1"
ROLLING_DOCUMENT_POLICY = "mesoforge-120-hour-presentation.v1"
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
    duration = (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600
    expected = int(duration)
    if expected != duration:
        raise ForecastCoverageError("A presentation boundary cannot split a native hourly interval")
    if len(hours) != expected:
        raise ForecastCoverageError("A presentation interval is incomplete")
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


def _saved_revision(saved: dict[str, Any]) -> Any:
    """Use retained issuance/final-stage identity, never the rendering checkout."""
    identity = saved.get("code_identity", {})
    if isinstance(identity, dict) and "git_commit" in identity:
        return identity["git_commit"]
    stage = saved["forecast"].get("learning_stage")
    identity = stage.get("code_identity") if isinstance(stage, dict) else None
    return (
        identity.get("git_commit", "unavailable") if isinstance(identity, dict) else "unavailable"
    )


def build_forecast_document(
    saved: dict[str, Any], *, location: dict[str, Any], days: int = 5, hours: int | None = None
) -> dict[str, Any]:
    """Build a bounded hourly outlook or five complete local days from saved fields.

    The interval belongs to its local starting day: an amount ending at midnight
    closes the preceding day. Hourly instantaneous extrema use those same endpoint
    samples and are labeled hourly extrema, not continuous daily maxima/minima.
    A mid-day rolling 120-hour forecast cannot cover five full local days. It fails
    rather than silently clipping the overnight portion or labeling partial days full.
    DST day lengths are determined by the IANA zone, not an assumed 24-hour day.
    """
    if days != 5:
        raise ValueError("This product supports exactly five complete local days")
    if hours not in (None, 36, FIVE_DAY_HORIZON.duration_hours) or isinstance(hours, bool):
        raise ValueError("The explicit hourly product supports exactly 36 or 120 hours")
    hourly_product = hours == 36
    rolling_product = hours == FIVE_DAY_HORIZON.duration_hours
    zone = ZoneInfo(location.get("display_timezone", "UTC"))
    forecast = saved["forecast"]
    source_hours = forecast["hours"]
    if rolling_product and (
        horizon_for(forecast) != FIVE_DAY_HORIZON
        or horizon_for(forecast.get("local_grid_baseline", {})) != FIVE_DAY_HORIZON
        or len(source_hours) != FIVE_DAY_HORIZON.duration_hours
    ):
        raise ForecastCoverageError(
            "The 120-hour product requires an explicit complete saved horizon"
        )
    if hourly_product and len(source_hours) < 36:
        raise ForecastCoverageError("The 36-hour product requires all 36 saved forecast hours")
    if not hourly_product and not rolling_product and len(source_hours) < 119:
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
    if rolling_product:
        # Equal elapsed forecast periods, not calendar days: local endpoints may
        # shift by an hour at DST. Every scientific interval remains represented.
        boundaries = [
            (reference + _HOUR * index * 24).astimezone(zone) for index in range(days + 1)
        ]
    elif hourly_product:
        start, end = reference, reference + 36 * _HOUR
        boundaries = [local_reference]
        # Only local midnights strictly inside the saved 36-hour window split cards.
        # UTC comparisons keep DST folds from duplicating or dropping an hour.
        for i in range(1, 4):
            midnight = datetime.combine(local_reference.date() + timedelta(days=i), time(0), zone)
            if start < midnight.astimezone(UTC) < end:
                boundaries.append(midnight)
        boundaries.append(end.astimezone(zone))
    else:
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
    qpf_intervals = None
    if "qpf_intervals" in forecast:
        if center.get("qpf_intervals") != forecast["qpf_intervals"]:
            raise IntegrityError("Saved point QPF intervals differ from final grid center")
        qpf_intervals = validate_qpf_intervals(
            forecast["qpf_intervals"], start=reference, end=times[-1]
        )
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
    display_hours = [_display_hour(hour) for hour in described]
    summaries = [
        _daily_summary(
            [
                hour
                for hour in display_hours
                if left.astimezone(UTC) < _time(hour["valid_time"]) <= right.astimezone(UTC)
            ],
            left,
            right,
        )
        for left, right in zip(boundaries, boundaries[1:], strict=False)
    ]
    for period_index, row in enumerate(summaries, 1):
        left, right = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
        row["partial_local_day"] = left.timetz().replace(tzinfo=None) != time(
            0
        ) or right.timetz().replace(tzinfo=None) != time(0)
        if rolling_product:
            row["forecast_period"] = period_index
    summary = _daily_summary(display_hours, boundaries[0], boundaries[-1])
    pop6_events = six_hour_events(source_hours)
    if pop6_events:
        for row in [*summaries, summary]:
            row["native_six_hour_pop"] = six_hour_summary(
                pop6_events, start=_time(row["start"]), end=_time(row["end"])
            )
    if qpf_intervals is not None:
        for row in [*summaries, summary]:
            amount = summarize_qpf_intervals(
                qpf_intervals, start=_time(row["start"]), end=_time(row["end"])
            )
            row["qpf_kg_m2"] = amount["total_kg_m2"]
            row["qpf_event_coverage"] = amount
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
    span_label = "36 hours" if hourly_product else "120 hours" if rolling_product else "five days"
    headline = "Your 36-hour weather outlook" if hourly_product else "Your five-day weather outlook"
    if len(available_highs) == len(summaries) and len(available_lows) == len(summaries):
        low = (min(available_lows) - 273.15) * 1.8 + 32
        high = (max(available_highs) - 273.15) * 1.8 + 32
        headline = f"Temperatures from {low:.0f} to {high:.0f} F across {span_label}"
    return {
        "document_policy": HOURS_DOCUMENT_POLICY
        if hourly_product
        else ROLLING_DOCUMENT_POLICY
        if rolling_product
        else DOCUMENT_POLICY,
        "product_title": "36-Hour Weather Outlook"
        if hourly_product
        else "5-Day Weather Outlook"
        if rolling_product
        else "5-Day Forecast",
        "summary_kind": "covered_local_day_portions"
        if hourly_product
        else "five_elapsed_24_hour_forecast_periods"
        if rolling_product
        else "five_complete_local_days",
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
        "hours": display_hours,
        **({"six_hour_pop_events": pop6_events} if pop6_events else {}),
        **(
            {
                "qpf_intervals": [
                    {
                        key: row[key]
                        for key in (
                            "interval_start",
                            "interval_end",
                            "interval_closure",
                            "value",
                            "unit",
                        )
                    }
                    for row in qpf_intervals
                    if _time(row["interval_start"]) < end and _time(row["interval_end"]) > start
                ]
            }
            if qpf_intervals is not None
            else {}
        ),
        "days": summaries,
        "summary": summary,
        "headline": headline,
        "transitions": [item["text"] for item in transitions["rendering"]["items"]],
        "ai": {
            "provider": desk.get("provider"),
            "model": desk.get("model"),
            "outcome": outcome,
            "display_status": ai_status,
        },
        "revision": _saved_revision(saved),
        "notes": [
            "High/low are extrema of hourly forecast samples, including overnight hours.",
            "Each card covers 24 elapsed forecast hours, not a complete local calendar day. "
            "Local clock times account for daylight-saving changes."
            if rolling_product
            else "Cards cover only their stated intervals; partial dates do not imply "
            "full-day coverage.",
            "Precipitation chance is the maximum native hourly PoP, not a daily probability. "
            "Each hourly event is more than 0.01 inches of liquid precipitation.",
            *(
                [
                    "Six-hour PoP is the maximum available whole six-hour event chance, "
                    "not an hourly or daily probability. Native windows are never redistributed."
                ]
                if pop6_events
                else []
            ),
            "Liquid totals sum whole native events only. Events crossing a card boundary "
            "are shown on the timing chart but never split; affected card totals are unavailable."
            if qpf_intervals is not None
            else "Liquid totals sum exact, consecutive hourly intervals. Missing hours "
            "make the interval total unavailable; they are never treated as zero.",
            "Wind is the vector mean of hourly U/V. Conditions show the most frequent "
            "approved hourly wording; transitions describe changes separately.",
        ],
    }
