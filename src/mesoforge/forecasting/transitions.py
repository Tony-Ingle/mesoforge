"""Deterministic weather evolution between consecutive hours of one saved issuance.

Input is the point-scoped conditions preview: nothing here recalculates a field,
reads a grid cell, or infers anything through an unavailable hour. A change is known
only to occur within the interval between the two hourly endpoints that differ, and
every structured fact stays separate from the text rendered for it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mesoforge.forecasting.condition_wording import WORDING_POLICY

SKY_SCALE = ("clear", "mostly_clear", "partly_cloudy", "mostly_cloudy", "cloudy")
WINDOW_CLOSURE = "left_open_right_closed"
TRACK_ORDER = ("precipitation_occurrence", "precipitation_wording", "precipitation_type", "sky")
TRANSITION_POLICY: dict[str, Any] = {
    "id": "mesoforge-transition-policy.v1",
    "purpose": "deterministic_endpoint_change_detection_not_forecast_skill",
    "window": {
        "closure": WINDOW_CLOSURE,
        "meaning": "The change occurs after the previous hourly endpoint and at or before "
        "the next one; no sub-hourly timing is inferred from any field.",
    },
    "gaps": "An unavailable component at either endpoint breaks that track's sequence; "
    "nothing is inferred across it and the sequence restarts at the next available hour.",
    "tracks": {
        "precipitation_occurrence": {
            "source": "presentation.precipitation state and relevance (applicability policy)",
            "states": ["not_applicable", "applicable", "unavailable"],
            "transitions": ["precipitation_onset", "precipitation_ending"],
            "rendered": False,
        },
        "precipitation_wording": {
            "source": "presentation.precipitation.probability_qualifier (presentation bands)",
            "states": ["omitted", "slight_chance", "chance", "likely", "direct", "unavailable"],
            "transitions": ["precipitation_wording_onset", "precipitation_wording_ending"],
            "band_changes_between_worded_hours": "not_an_event",
            "rendered": True,
        },
        "precipitation_type": {
            "source": "presentation.precipitation.type (active endpoint p-type policy)",
            "states": ["known:<type>", "unknown", "ambiguous", "not_applicable", "unavailable"],
            "transitions": ["precipitation_type_change"],
            "rule": "Consecutive applicable endpoints whose (state, value) differ, with at least "
            "one endpoint known. unknown<->ambiguous changes are not events. A change touching "
            "an unresolved endpoint keeps that status and is never rendered.",
            "rendered": "known_to_known_only",
        },
        "sky": {
            "source": "components.sky.sky_category (active NBM sky policy)",
            "scale": list(SKY_SCALE),
            "minimum_change_levels": 2,
            "persistence_hours": 3,
            "transitions": ["sky_trend"],
            "rule": "From a reference level, an hour at least minimum_change_levels away in one "
            "direction starts a trend only if it and the following persistence_hours-1 hours "
            "are all available and at least one level away on that side. The window runs "
            "from the last hour at or beyond the reference on the other side to that hour; "
            "the trend hour then becomes the reference. One-level wobbles are not events.",
            "rendered": True,
        },
    },
    "ordering": "window end, then track order",
}
TEMPLATE_VERSION = "transition-text.v1"
_TYPE_LABELS: dict[str, str] = dict(WORDING_POLICY["pop"]["type_labels"])
_GENERIC_LABEL = str(WORDING_POLICY["pop"]["generic_label"])
_CELL_HOURS = "center_point"


def _time(value: str) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Saved valid times must include a timezone")
    return instant


def _hour_state(index: int, hour: dict[str, Any]) -> dict[str, Any]:
    """Project one described hour onto the tracked states without changing it."""
    precipitation = hour["presentation"]["precipitation"]
    if precipitation["state"] == "not_applicable":
        occurrence = "not_applicable"
    elif precipitation["state"] == "known" and precipitation.get("relevant") is True:
        occurrence = "applicable"
    else:
        occurrence = "unavailable"
    qualifier = precipitation.get("probability_qualifier")
    if occurrence == "unavailable":
        wording = "unavailable"
    elif occurrence == "applicable" and qualifier not in (None, "omit"):
        wording = str(qualifier)
    else:
        wording = "omitted"
    native_type = precipitation["type"]
    if occurrence == "applicable":
        type_state = str(native_type["state"])
        type_value = native_type.get("value") if type_state == "known" else None
    else:
        type_state, type_value = occurrence, None
    sky = hour["components"]["sky"]
    category = sky.get("sky_category")
    sky_known = sky["state"] == "known" and category in SKY_SCALE
    return {
        "hour_ref": f"{_CELL_HOURS}.hours[{index}]",
        "horizon_hours": hour["horizon_hours"],
        "valid_time": hour["valid_time"],
        "precipitation_occurrence": occurrence,
        "precipitation_wording": wording,
        "precipitation_label": precipitation.get("type_label") or _GENERIC_LABEL,
        "precipitation_type": {"state": type_state, "value": type_value},
        "sky": {
            "state": "known" if sky_known else "unavailable",
            "category": category if sky_known else None,
            "level": SKY_SCALE.index(category) if sky_known else None,
        },
        "evidence_refs": {
            "precipitation": list(precipitation.get("evidence_refs", [])),
            "precipitation_type": list(native_type.get("evidence_refs", [])),
            "sky": list(hour["presentation"]["sky"].get("evidence_refs", [])),
        },
    }


def _endpoint(state: dict[str, Any], track: str) -> dict[str, Any]:
    value: Any
    if track == "precipitation_type":
        value = deepcopy(state["precipitation_type"])
    elif track == "sky":
        value = deepcopy(state["sky"])
    elif track == "precipitation_wording":
        value = {"wording": state["precipitation_wording"], "label": state["precipitation_label"]}
    else:
        value = {
            "occurrence": state["precipitation_occurrence"],
            "wording": state["precipitation_wording"],
        }
    return {
        "hour_ref": state["hour_ref"],
        "horizon_hours": state["horizon_hours"],
        "valid_time": state["valid_time"],
        "state": value,
    }


def _fact(
    kind: str,
    track: str,
    previous: dict[str, Any],
    nxt: dict[str, Any],
    *,
    status: str,
    reasons: list[str],
    evidence_key: str,
    **extra: Any,
) -> dict[str, Any]:
    start, end = _time(previous["valid_time"]), _time(nxt["valid_time"])
    return {
        "type": kind,
        "track": track,
        "status": status,
        "policy_id": TRANSITION_POLICY["id"],
        "window": {
            "start": previous["valid_time"],
            "end": nxt["valid_time"],
            "closure": WINDOW_CLOSURE,
            "hours": (end - start).total_seconds() / 3600,
        },
        "previous": _endpoint(previous, track),
        "next": _endpoint(nxt, track),
        "hour_refs": [previous["hour_ref"], nxt["hour_ref"]],
        "evidence_refs": sorted(
            set(previous["evidence_refs"][evidence_key]) | set(nxt["evidence_refs"][evidence_key])
        ),
        "reasons": reasons,
        **extra,
    }


def _gap(track: str, state: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "track": track,
        "hour_ref": state["hour_ref"],
        "valid_time": state["valid_time"],
        "reason": reason,
    }


def _occurrence_and_wording(
    states: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for state in states:
        if state["precipitation_occurrence"] == "unavailable":
            gaps.append(
                _gap("precipitation_occurrence", state, "precipitation_applicability_unavailable")
            )
            gaps.append(
                _gap("precipitation_wording", state, "precipitation_applicability_unavailable")
            )
    for previous, nxt in zip(states, states[1:], strict=False):
        before, after = previous["precipitation_occurrence"], nxt["precipitation_occurrence"]
        if "unavailable" in (before, after):
            continue
        if (before, after) == ("not_applicable", "applicable"):
            facts.append(
                _fact(
                    "precipitation_onset",
                    "precipitation_occurrence",
                    previous,
                    nxt,
                    status="known",
                    reasons=["applicability_policy_not_applicable_then_applicable"],
                    evidence_key="precipitation",
                    label=nxt["precipitation_label"],
                )
            )
        elif (before, after) == ("applicable", "not_applicable"):
            facts.append(
                _fact(
                    "precipitation_ending",
                    "precipitation_occurrence",
                    previous,
                    nxt,
                    status="known",
                    reasons=["applicability_policy_applicable_then_not_applicable"],
                    evidence_key="precipitation",
                    label=previous["precipitation_label"],
                )
            )
        worded_before = previous["precipitation_wording"] != "omitted"
        worded_after = nxt["precipitation_wording"] != "omitted"
        if not worded_before and worded_after:
            facts.append(
                _fact(
                    "precipitation_wording_onset",
                    "precipitation_wording",
                    previous,
                    nxt,
                    status="known",
                    reasons=[f"probability_band_omitted_then_{nxt['precipitation_wording']}"],
                    evidence_key="precipitation",
                    label=nxt["precipitation_label"],
                )
            )
        elif worded_before and not worded_after:
            facts.append(
                _fact(
                    "precipitation_wording_ending",
                    "precipitation_wording",
                    previous,
                    nxt,
                    status="known",
                    reasons=[f"probability_band_{previous['precipitation_wording']}_then_omitted"],
                    evidence_key="precipitation",
                    label=previous["precipitation_label"],
                )
            )
    return facts, gaps


def _type_changes(
    states: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    tracked = {"known", "unknown", "ambiguous"}
    for state in states:
        if state["precipitation_type"]["state"] == "unavailable":
            gaps.append(_gap("precipitation_type", state, "precipitation_type_unavailable"))
    for previous, nxt in zip(states, states[1:], strict=False):
        before, after = previous["precipitation_type"], nxt["precipitation_type"]
        if before["state"] not in tracked or after["state"] not in tracked:
            continue
        if (before["state"], before["value"]) == (after["state"], after["value"]):
            continue
        if "known" not in (before["state"], after["state"]):
            continue
        if before["state"] == after["state"] == "known":
            status, reasons = "known", ["active_endpoint_type_changed_between_known_types"]
        elif "ambiguous" in (before["state"], after["state"]):
            status, reasons = "ambiguous", ["one_endpoint_type_is_ambiguous_no_direct_type_claim"]
        else:
            status, reasons = "unknown", ["one_endpoint_type_is_unknown_no_direct_type_claim"]
        facts.append(
            _fact(
                "precipitation_type_change",
                "precipitation_type",
                previous,
                nxt,
                status=status,
                reasons=reasons,
                evidence_key="precipitation_type",
                from_label=_TYPE_LABELS.get(before["value"] or "", _GENERIC_LABEL),
                to_label=_TYPE_LABELS.get(after["value"] or "", _GENERIC_LABEL),
            )
        )
    return facts, gaps


def _sky_trends(states: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    policy = TRANSITION_POLICY["tracks"]["sky"]
    minimum, persistence = int(policy["minimum_change_levels"]), int(policy["persistence_hours"])
    levels = [state["sky"]["level"] for state in states]
    facts: list[dict[str, Any]] = []
    gaps = [
        _gap("sky", state, "active_sky_unavailable")
        for state in states
        if state["sky"]["state"] != "known"
    ]
    count = len(states)
    position = 0
    while position < count and levels[position] is None:
        position += 1
    if position >= count:
        return facts, gaps
    reference_index, reference = position, levels[position]
    position += 1
    while position < count:
        level = levels[position]
        if level is None:
            position += 1
            while position < count and levels[position] is None:
                position += 1
            if position < count:
                reference_index, reference = position, levels[position]
                position += 1
            continue
        assert reference is not None
        delta = level - reference
        if abs(delta) >= minimum:
            sign = 1 if delta > 0 else -1
            following = range(position + 1, position + persistence)
            confirmed = all(
                k < count and levels[k] is not None and (levels[k] - reference) * sign >= 1
                for k in following
            )
            if confirmed:
                start = position - 1
                while start > reference_index and (levels[start] - reference) * sign >= 1:  # type: ignore[operator]
                    start -= 1
                previous, nxt = states[start], states[position]
                facts.append(
                    _fact(
                        "sky_trend",
                        "sky",
                        previous,
                        nxt,
                        status="known",
                        reasons=[
                            f"moved_{abs(delta)}_levels_from_{SKY_SCALE[reference]}_and_persisted_"
                            f"{persistence}_hours"
                        ],
                        evidence_key="sky",
                        direction="clearing" if sign < 0 else "increasing_clouds",
                        from_category=SKY_SCALE[reference],
                        to_category=SKY_SCALE[level],
                        levels_changed=abs(delta),
                        persistence_hours=persistence,
                        confirmed_through=states[position + persistence - 1]["hour_ref"],
                    )
                )
                reference_index, reference = position, level
        position += 1
    return facts, gaps


def _clock(instant: datetime) -> str:
    hour = instant.hour % 12 or 12
    minute = f":{instant.minute:02d}" if instant.minute else ""
    return f"{hour}{minute} {'AM' if instant.hour < 12 else 'PM'}"


def _between(window: dict[str, Any], zone: ZoneInfo) -> str:
    start, end = _time(window["start"]).astimezone(zone), _time(window["end"]).astimezone(zone)
    if start.date() == end.date():
        return f"between {_clock(start)} and {_clock(end)} {start.strftime('%A')}"
    return f"between {_clock(start)} {start.strftime('%A')} and {_clock(end)} {end.strftime('%A')}"


def _render(facts: list[dict[str, Any]], zone: ZoneInfo, timezone_name: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for index, fact in enumerate(facts):
        when = _between(fact["window"], zone)
        kind = fact["type"]
        text: str | None = None
        reason: str | None = None
        if kind == "precipitation_wording_onset":
            text = f"{fact['label']} developing {when}"
        elif kind == "precipitation_wording_ending":
            text = f"{fact['label']} ending {when}"
        elif kind == "precipitation_type_change":
            if fact["status"] == "known":
                text = f"{fact['from_label']} changing to {fact['to_label']} {when}"
            else:
                reason = "type_change_involves_unresolved_state"
        elif kind == "sky_trend":
            text = f"Becoming {fact['to_category'].replace('_', ' ')} {when}"
        else:
            reason = "applicability_transitions_are_structured_only"
        if text is not None:
            items.append({"transition": index, "text": text[0].upper() + text[1:]})
        else:
            omitted.append({"transition": index, "reason": reason})
    return {
        "template_version": TEMPLATE_VERSION,
        "timezone": timezone_name,
        "items": items,
        "omitted": omitted,
        "text": " ".join(item["text"] + "." for item in items),
    }


def build_transitions(
    preview: dict[str, Any], *, display_timezone: str = "UTC", timezone_source: str = "default_utc"
) -> dict[str, Any]:
    """Detect evolution across the point-scoped hours of one conditions preview."""
    try:
        zone = ZoneInfo(display_timezone)
    except (KeyError, ValueError, OSError) as exc:
        raise ValueError(f"Unknown display timezone {display_timezone!r}") from exc
    hours = preview[_CELL_HOURS]["hours"]
    states = [_hour_state(index, hour) for index, hour in enumerate(hours)]
    for previous, nxt in zip(states, states[1:], strict=False):
        if _time(nxt["valid_time"]) <= _time(previous["valid_time"]):
            raise ValueError("Preview hours must be in increasing valid-time order")
    facts: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for detector in (_occurrence_and_wording, _type_changes, _sky_trends):
        detected, missing = detector(states)
        facts.extend(detected)
        gaps.extend(missing)
    facts.sort(key=lambda fact: (_time(fact["window"]["end"]), TRACK_ORDER.index(fact["track"])))
    return {
        "schema_version": "mesoforge.weather-transitions.v1",
        "transition_policy": deepcopy(TRANSITION_POLICY),
        "input": {
            **deepcopy(preview["input"]),
            "conditions_schema_version": preview["schema_version"],
            "conditions_ruleset_id": preview["ruleset_id"],
            "wording_policy_id": preview["wording_policy"]["id"],
            "conditions_scope": preview["scope"]["requested"],
            "hours": len(hours),
        },
        "display_timezone": {"name": display_timezone, "source": timezone_source},
        "hourly_states": [
            {key: value for key, value in state.items() if key != "evidence_refs"}
            for state in states
        ],
        "gaps": gaps,
        "transitions": facts,
        "rendering": _render(facts, zone, display_timezone),
    }
