"""Deterministic local-time period grouping of structured transition facts.

This is presentation aggregation only: it groups the facts that
``transitions.build_transitions`` already produced into local 12-hour periods and
renders them concisely. It derives no new weather, no period maxima, no dominant
categories and no totals, and it never infers anything through a gap.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mesoforge.forecasting.transitions import describe_window

SCHEMA_VERSION = "mesoforge.period-summary.v1"
TEMPLATE_VERSION = "period-text.v1"
DAY_START = time(6)
NIGHT_START = time(18)
PERIOD_POLICY: dict[str, Any] = {
    "id": "mesoforge-period-summary.v1",
    "purpose": "presentation_grouping_of_transition_facts_not_a_period_forecast",
    "boundaries": {
        "convention": "local_12_hour_day_night",
        "day": "06:00-18:00 local",
        "night": "18:00-06:00 local",
        "closure": "left_closed_right_open",
        "partial_periods": "the first and last periods may begin or end inside the forecast",
        "note": "A presentation convention on wall-clock local time, not a daylight "
        "definition; daylight-saving changes make a period 11 or 13 hours.",
    },
    "hour_membership": "an hourly endpoint belongs to the period containing its local valid time",
    "fact_membership": "a transition belongs to the period containing the local time of its "
    "window end, the first hour holding the new state; a window starting in an earlier "
    "period is flagged crosses_period_start and its exact window is retained",
    "time_labels": {
        "basis": "every hourly endpoint inside the window (start excluded, end included) "
        "must fall in one band; otherwise the explicit clock-time phrase is used",
        "bands_by_local_endpoint_hour": {
            "late <previous day> night": "01-03",
            "early <day> morning": "04-06",
            "<day> morning": "07-12 (early 07-09, late 10-12)",
            "<day> afternoon": "13-18 (early 13-15, late 16-18)",
            "<day> evening": "19-24 (early 19-21, late 22-24)",
        },
        "qualifier": "early/late only when every endpoint is in that half of the band",
    },
    "combination": {
        "coincident": "identical windows (same start and end)",
        "rules": [
            {
                "facts": ["precipitation_wording_onset", "sky_trend:increasing_clouds"],
                "sentence": "<label> developing and clouds increasing <when>",
            },
            {
                "facts": ["precipitation_wording_ending", "sky_trend:clearing"],
                "sentence": "<label> ending and becoming <category> <when>",
            },
        ],
        "otherwise": "separate sentences in window order; unrendered facts stay omitted "
        "with their transition reasons",
    },
    "aggregations": "none: no period maxima, dominant categories, totals or representative "
    "conditions are derived",
}


def _utc(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Times must include a timezone")
    return instant


def _iso_utc(instant: datetime) -> str:
    return instant.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def _wall(date: Any, clock: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(date, clock, tzinfo=zone)


def _period_bounds(kind: str, start: datetime, zone: ZoneInfo) -> tuple[datetime, str, datetime]:
    """Return (start, kind, end) and the next period's start/kind via wall-clock dates."""
    if kind == "day":
        return start, kind, _wall(start.date(), NIGHT_START, zone)
    return start, kind, _wall(start.date() + timedelta(days=1), DAY_START, zone)


def _first_period(first_local: datetime, zone: ZoneInfo) -> tuple[str, datetime]:
    # Compare instants in UTC: aware datetimes sharing one ZoneInfo compare by wall clock.
    first = first_local.astimezone(UTC)
    day_start = _wall(first_local.date(), DAY_START, zone)
    night_start = _wall(first_local.date(), NIGHT_START, zone)
    if day_start.astimezone(UTC) <= first < night_start.astimezone(UTC):
        return "day", day_start
    if first >= night_start.astimezone(UTC):
        return "night", night_start
    return "night", _wall(first_local.date() - timedelta(days=1), NIGHT_START, zone)


def _endpoints(window: dict[str, Any], zone: ZoneInfo) -> list[datetime]:
    start, end = _utc(window["start"]).astimezone(UTC), _utc(window["end"]).astimezone(UTC)
    points: list[datetime] = []
    cursor = start
    while cursor < end:
        cursor = cursor + timedelta(hours=1)
        points.append(min(cursor, end).astimezone(zone))
    return points


def _band(point: datetime) -> tuple[str, str | None, Any]:
    hour = point.hour + (1 if point.minute or point.second else 0)
    if hour == 0:
        hour = 24
    if hour <= 3:
        return "night", None, point.date() - timedelta(days=1)
    if hour <= 6:
        return "early_morning", None, point.date()
    if hour <= 12:
        return "morning", "early" if hour <= 9 else "late", point.date()
    if hour <= 18:
        return "afternoon", "early" if hour <= 15 else "late", point.date()
    date = point.date() - timedelta(days=1) if hour == 24 else point.date()
    return "evening", "early" if hour <= 21 else "late", date


def time_label(window: dict[str, Any], zone: ZoneInfo) -> dict[str, Any] | None:
    """A natural local-time descriptor only when the whole window sits in one band."""
    points = _endpoints(window, zone)
    bands = [_band(point) for point in points]
    if not points or len({(band, date) for band, _, date in bands}) != 1:
        return None
    band, _, date = bands[0]
    qualifiers = {qualifier for _, qualifier, _ in bands}
    qualifier = next(iter(qualifiers)) if len(qualifiers) == 1 else None
    weekday = date.strftime("%A")
    if band == "night":
        label = f"late {weekday} night"
    elif band == "early_morning":
        label = f"early {weekday} morning"
    else:
        label = f"{qualifier + ' ' if qualifier else ''}{weekday} {band}"
    return {
        "label": label,
        "band": band,
        "qualifier": qualifier,
        "weekday": weekday,
        "local_endpoints": [point.isoformat() for point in points],
    }


def _phrase(fact: dict[str, Any]) -> str | None:
    kind = fact["type"]
    if kind == "precipitation_wording_onset":
        return f"{fact['label']} developing"
    if kind == "precipitation_wording_ending":
        return f"{fact['label']} ending"
    if kind == "precipitation_type_change" and fact["status"] == "known":
        return f"{fact['from_label']} changing to {fact['to_label']}"
    if kind == "sky_trend":
        if fact["direction"] == "clearing":
            return f"becoming {fact['to_category'].replace('_', ' ')}"
        return "clouds increasing"
    return None


def _combined_phrase(first: dict[str, Any], second: dict[str, Any]) -> str | None:
    kinds = {first["type"], second["type"]}
    sky = first if first["type"] == "sky_trend" else second
    precipitation = second if sky is first else first
    if (
        kinds == {"precipitation_wording_onset", "sky_trend"}
        and sky["direction"] == "increasing_clouds"
    ):
        return f"{precipitation['label']} developing and clouds increasing"
    if kinds == {"precipitation_wording_ending", "sky_trend"} and sky["direction"] == "clearing":
        category = sky["to_category"].replace("_", " ")
        return f"{precipitation['label']} ending and becoming {category}"
    return None


def _sentence(text: str) -> str:
    return text[0].upper() + text[1:] + "."


def _sentences(members: list[tuple[int, dict[str, Any]]], zone: ZoneInfo) -> list[dict[str, Any]]:
    rendered = [(index, fact) for index, fact in members if _phrase(fact) is not None]
    rendered.sort(key=lambda item: (_utc(item[1]["window"]["end"]), item[0]))
    sentences: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for position, (index, fact) in enumerate(rendered):
        if index in consumed:
            continue
        partner = next(
            (
                (other_index, other)
                for other_index, other in rendered[position + 1 :]
                if other_index not in consumed
                and other["window"] == fact["window"]
                and _combined_phrase(fact, other) is not None
            ),
            None,
        )
        label = time_label(fact["window"], zone)
        when = label["label"] if label else describe_window(fact["window"], zone)
        if partner is not None:
            other_index, other = partner
            consumed.update({index, other_index})
            sentences.append(
                {
                    "text": _sentence(f"{_combined_phrase(fact, other)} {when}"),
                    "facts": [index, other_index],
                    "combined": True,
                    "time_label": label,
                    "explicit_window_phrase": label is None,
                }
            )
            continue
        consumed.add(index)
        sentences.append(
            {
                "text": _sentence(f"{_phrase(fact)} {when}"),
                "facts": [index],
                "combined": False,
                "time_label": label,
                "explicit_window_phrase": label is None,
            }
        )
    return sentences


def build_period_summary(transitions: dict[str, Any]) -> dict[str, Any]:
    """Group one issuance's transition facts into local day/night presentation periods."""
    zone_name = transitions["display_timezone"]["name"]
    zone = ZoneInfo(zone_name)
    states = transitions["hourly_states"]
    facts = transitions["transitions"]
    omitted = {item["transition"]: item["reason"] for item in transitions["rendering"]["omitted"]}
    if not states:
        raise ValueError("Period summary requires at least one described hour")
    instants = [_utc(state["valid_time"]).astimezone(UTC) for state in states]
    first_local, last_instant = instants[0].astimezone(zone), instants[-1]
    kind, start = _first_period(first_local, zone)
    periods: list[dict[str, Any]] = []
    while start.astimezone(UTC) <= last_instant:
        start, kind, end = _period_bounds(kind, start, zone)
        # Membership and lengths use UTC instants so daylight-saving changes stay exact.
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        member_hours = [
            (state, instant)
            for state, instant in zip(states, instants, strict=True)
            if start_utc <= instant < end_utc
        ]
        member_facts = [
            (index, fact)
            for index, fact in enumerate(facts)
            if start_utc <= _utc(fact["window"]["end"]).astimezone(UTC) < end_utc
        ]
        member_refs = {state["hour_ref"] for state, _ in member_hours}
        availability = {
            "precipitation_occurrence": dict(
                Counter(state["precipitation_occurrence"] for state, _ in member_hours)
            ),
            "precipitation_type": dict(
                Counter(state["precipitation_type"]["state"] for state, _ in member_hours)
            ),
            "sky": dict(Counter(state["sky"]["state"] for state, _ in member_hours)),
        }
        sentences = _sentences(member_facts, zone)
        expected_hours = round((end_utc - start_utc).total_seconds() / 3600)
        periods.append(
            {
                "id": f"{start.date().isoformat()}-{kind}",
                "kind": kind,
                "local_start": start.isoformat(),
                "local_end": end.isoformat(),
                "utc_start": _iso_utc(start),
                "utc_end": _iso_utc(end),
                "expected_hours": expected_hours,
                "hours": len(member_hours),
                "partial_start": bool(member_hours) and member_hours[0][1] > start_utc,
                "partial_end": bool(member_hours)
                and member_hours[-1][1] < end_utc - timedelta(hours=1),
                "hour_refs": [state["hour_ref"] for state, _ in member_hours],
                "availability": availability,
                "gaps": [gap for gap in transitions["gaps"] if gap["hour_ref"] in member_refs],
                "transition_refs": [
                    {
                        "index": index,
                        "type": fact["type"],
                        "track": fact["track"],
                        "status": fact["status"],
                        "window": deepcopy(fact["window"]),
                        "crosses_period_start": _utc(fact["window"]["start"]).astimezone(UTC)
                        < start_utc,
                        "rendered": index not in omitted,
                    }
                    for index, fact in member_facts
                ],
                "omitted_facts": [
                    {"index": index, "type": fact["type"], "reason": omitted[index]}
                    for index, fact in member_facts
                    if index in omitted
                ],
                "sentences": sentences,
                "text": " ".join(sentence["text"] for sentence in sentences),
                "status": "rendered" if sentences else "no_rendered_transitions",
            }
        )
        start = end
        kind = "night" if kind == "day" else "day"
    return {
        "schema_version": SCHEMA_VERSION,
        "period_policy": deepcopy(PERIOD_POLICY),
        "template_version": TEMPLATE_VERSION,
        "input": {
            **deepcopy(transitions["input"]),
            "transition_schema_version": transitions["schema_version"],
            "transition_policy_id": transitions["transition_policy"]["id"],
            "transition_template_version": transitions["rendering"]["template_version"],
        },
        "display_timezone": deepcopy(transitions["display_timezone"]),
        "periods": periods,
        "transitions": deepcopy(facts),
        "gaps": deepcopy(transitions["gaps"]),
        "text": " ".join(period["text"] for period in periods if period["text"]),
    }
