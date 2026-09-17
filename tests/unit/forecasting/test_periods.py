"""Periods only group existing transition facts; text never outruns their windows."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mesoforge.forecasting.periods import PERIOD_POLICY, build_period_summary, time_label
from mesoforge.forecasting.transitions import build_transitions
from mesoforge.storage.json import CanonicalJsonSerializer
from tests.unit.forecasting.test_transitions import TARGET, hour, preview

CHICAGO = "America/Chicago"
JSON = CanonicalJsonSerializer()


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def summarize(hours, zone=CHICAGO):
    transitions = build_transitions(
        preview(hours), display_timezone=zone, timezone_source="request"
    )
    return build_period_summary(transitions)


def shifted(hours, delta):
    """Move every synthetic hour by a whole-hour offset; states are unchanged."""
    for item in hours:
        moved = datetime.fromisoformat(item["valid_time"]) + delta
        item["valid_time"] = _iso(moved)
    return hours


def test_periods_are_local_day_and_night_halves_and_cover_every_hour():
    # TARGET is 22Z; hour 1 is 23Z = 18:00 CDT, so 36 hours are exactly three periods.
    result = summarize([hour(i) for i in range(36)])
    periods = result["periods"]
    assert [(p["id"], p["kind"]) for p in periods] == [
        ("2026-09-16-night", "night"),
        ("2026-09-17-day", "day"),
        ("2026-09-17-night", "night"),
    ]
    assert [p["local_start"] for p in periods] == [
        "2026-09-16T18:00:00-05:00",
        "2026-09-17T06:00:00-05:00",
        "2026-09-17T18:00:00-05:00",
    ]
    assert [p["utc_start"] for p in periods] == [
        "2026-09-16T23:00:00Z",
        "2026-09-17T11:00:00Z",
        "2026-09-17T23:00:00Z",
    ]
    assert [p["utc_end"] for p in periods] == [
        "2026-09-17T11:00:00Z",
        "2026-09-17T23:00:00Z",
        "2026-09-18T11:00:00Z",
    ]
    assert all(p["hours"] == p["expected_hours"] == 12 for p in periods)
    assert all(not p["partial_start"] and not p["partial_end"] for p in periods)
    assert sum(len(p["hour_refs"]) for p in periods) == 36
    assert periods[0]["hour_refs"][0] == "center_point.hours[0]"
    assert periods[2]["hour_refs"][-1] == "center_point.hours[35]"
    assert all(p["status"] == "no_rendered_transitions" and p["text"] == "" for p in periods)
    assert result["schema_version"] == "mesoforge.period-summary.v1"
    assert result["period_policy"] == PERIOD_POLICY
    assert result["display_timezone"] == {"name": CHICAGO, "source": "request"}
    assert result["input"]["transition_policy_id"] == "mesoforge-transition-policy.v1"


def test_partial_first_and_last_periods_are_flagged_without_inventing_hours():
    hours = shifted([hour(i) for i in range(7)], timedelta(hours=2))  # 20:00 .. 02:00 CDT
    periods = summarize(hours)["periods"]
    assert [p["id"] for p in periods] == ["2026-09-16-night"]
    only = periods[0]
    assert only["partial_start"] is True and only["partial_end"] is True
    assert only["hours"] == 7 and only["expected_hours"] == 12
    assert only["local_start"] == "2026-09-16T18:00:00-05:00"
    day_hours = shifted([hour(i) for i in range(5)], timedelta(hours=12))  # 06:00 .. 10:00 CDT
    periods = summarize(day_hours)["periods"]
    assert [p["id"] for p in periods] == ["2026-09-17-day"]
    assert periods[0]["partial_start"] is False and periods[0]["partial_end"] is True


def test_a_night_period_spans_midnight_and_owns_the_first_morning_hours():
    hours = [hour(i) for i in range(9)]  # 18:00 .. 02:00 CDT
    periods = summarize(hours)["periods"]
    assert len(periods) == 1 and periods[0]["kind"] == "night"
    assert periods[0]["hour_refs"] == [f"center_point.hours[{i}]" for i in range(9)]


def test_daylight_saving_end_makes_a_thirteen_hour_night_with_correct_utc_bounds():
    # US DST ends 2026-11-01 at 02:00 local: the night from 18:00 CDT to 06:00 CST is 13 h.
    delta = datetime(2026, 10, 31, 22, tzinfo=UTC) - TARGET
    hours = shifted([hour(i) for i in range(14)], delta)  # 23Z Oct 31 (18:00 CDT) .. 12Z Nov 1
    periods = summarize(hours)["periods"]
    assert [p["id"] for p in periods] == ["2026-10-31-night", "2026-11-01-day"]
    night = periods[0]
    assert (
        night["utc_start"] == "2026-10-31T23:00:00Z" and night["utc_end"] == "2026-11-01T12:00:00Z"
    )
    assert night["local_start"] == "2026-10-31T18:00:00-05:00"
    assert night["local_end"] == "2026-11-01T06:00:00-06:00"
    assert night["expected_hours"] == 13 and night["hours"] == 13
    assert not night["partial_start"] and not night["partial_end"]
    assert periods[1]["hour_refs"] == ["center_point.hours[13]"]


def test_facts_belong_to_the_period_holding_their_window_end_and_keep_exact_windows():
    hours = [hour(i) for i in range(11)] + [
        hour(i, precip="worded", ptype="rain") for i in range(11, 14)
    ]
    hours += [hour(i) for i in range(14, 24)]
    result = summarize(hours)
    night, day = result["periods"]
    # Onset window (04:00, 05:00] CDT ends in the night period; ending (07:00, 08:00] in the day.
    assert [ref["type"] for ref in night["transition_refs"]] == [
        "precipitation_onset",
        "precipitation_wording_onset",
    ]
    assert [ref["type"] for ref in day["transition_refs"]] == [
        "precipitation_ending",
        "precipitation_wording_ending",
    ]
    onset = night["transition_refs"][1]
    assert onset["window"] == result["transitions"][onset["index"]]["window"]
    assert onset["window"]["start"] == "2026-09-17T09:00:00Z"
    assert onset["window"]["end"] == "2026-09-17T10:00:00Z"
    assert onset["crosses_period_start"] is False and onset["rendered"] is True
    assert night["transition_refs"][0]["rendered"] is False
    assert night["omitted_facts"] == [
        {
            "index": night["transition_refs"][0]["index"],
            "type": "precipitation_onset",
            "reason": "applicability_transitions_are_structured_only",
        }
    ]
    assert night["text"] == "Rain developing early Thursday morning."
    assert day["text"] == "Rain ending early Thursday morning."


def test_window_starting_in_the_previous_period_is_flagged():
    categories = ["cloudy"] * 12 + ["mostly_clear"] * 4
    # Trend hour is index 12 (06:00 CDT = day period start); the window starts at 05:00.
    hours = [hour(i, sky=category) for i, category in enumerate(categories)]
    result = summarize(hours)
    night, day = result["periods"]
    assert night["transition_refs"] == []
    trend = day["transition_refs"][0]
    assert trend["type"] == "sky_trend" and trend["crosses_period_start"] is True
    assert trend["window"]["start"] == "2026-09-17T10:00:00Z"
    assert trend["window"]["end"] == "2026-09-17T11:00:00Z"
    assert day["text"] == "Becoming mostly clear early Thursday morning."


def test_coincident_onset_and_increasing_clouds_combine_into_one_sentence():
    hours = [hour(i, sky="mostly_clear") for i in range(2)]
    hours += [hour(i, precip="worded", ptype="rain", sky="mostly_cloudy") for i in range(2, 6)]
    result = summarize(hours)
    period = result["periods"][0]
    assert [ref["type"] for ref in period["transition_refs"]] == [
        "precipitation_onset",
        "precipitation_wording_onset",
        "sky_trend",
    ]
    assert len(period["sentences"]) == 1
    sentence = period["sentences"][0]
    assert sentence["combined"] is True
    assert sentence["facts"] == [1, 2]
    assert sentence["text"] == "Rain developing and clouds increasing early Wednesday evening."
    assert result["transitions"][1]["window"] == result["transitions"][2]["window"]


def test_coincident_ending_and_clearing_combine_and_other_pairs_stay_separate():
    hours = [hour(i, precip="worded", ptype="rain", sky="cloudy") for i in range(2)]
    hours += [hour(i, sky="partly_cloudy") for i in range(2, 6)]
    result = summarize(hours)
    period = result["periods"][0]
    assert [s["text"] for s in period["sentences"]] == [
        "Rain ending and becoming partly cloudy early Wednesday evening."
    ]
    # Onset plus clearing share a window but have no combination rule: two sentences.
    hours = [hour(i, sky="cloudy") for i in range(2)]
    hours += [hour(i, precip="worded", ptype="snow", sky="partly_cloudy") for i in range(2, 6)]
    period = summarize(hours)["periods"][0]
    assert [s["text"] for s in period["sentences"]] == [
        "Snow developing early Wednesday evening.",
        "Becoming partly cloudy early Wednesday evening.",
    ]
    assert all(s["combined"] is False for s in period["sentences"])


def test_non_coincident_events_in_one_period_remain_separate_sentences_in_window_order():
    hours = [hour(i, sky="mostly_clear") for i in range(2)]
    hours += [hour(i, precip="worded", ptype="rain", sky="mostly_clear") for i in range(2, 5)]
    hours += [hour(i, precip="worded", ptype="rain", sky="mostly_cloudy") for i in range(5, 9)]
    period = summarize(hours)["periods"][0]
    assert [s["text"] for s in period["sentences"]] == [
        "Rain developing early Wednesday evening.",
        "Clouds increasing late Wednesday evening.",
    ]
    assert [s["combined"] for s in period["sentences"]] == [False, False]


def test_ambiguous_type_facts_are_kept_but_never_rendered():
    hours = [hour(i, precip="worded", ptype="rain") for i in range(2)]
    hours += [hour(2, precip="worded", ptype="ambiguous")]
    hours += [hour(i, precip="worded", ptype="snow") for i in range(3, 5)]
    period = summarize(hours)["periods"][0]
    assert [ref["status"] for ref in period["transition_refs"]] == ["ambiguous", "ambiguous"]
    assert [o["reason"] for o in period["omitted_facts"]] == [
        "type_change_involves_unresolved_state"
    ] * 2
    assert period["sentences"] == [] and period["status"] == "no_rendered_transitions"


def test_unavailable_hours_are_period_gaps_and_nothing_is_inferred_across_them():
    hours = [hour(i, precip="worded", ptype="rain", sky="cloudy") for i in range(3)]
    hours += [hour(3, precip="unavailable", sky=None)]
    hours += [hour(i, sky="clear") for i in range(4, 8)]
    period = summarize(hours)["periods"][0]
    assert period["transition_refs"] == [] and period["text"] == ""
    assert {gap["track"] for gap in period["gaps"]} == {
        "precipitation_occurrence",
        "precipitation_wording",
        "precipitation_type",
        "sky",
    }
    assert all(gap["hour_ref"] == "center_point.hours[3]" for gap in period["gaps"])
    assert period["availability"]["sky"] == {"known": 7, "unavailable": 1}
    assert period["availability"]["precipitation_occurrence"] == {
        "applicable": 3,
        "unavailable": 1,
        "not_applicable": 4,
    }


@pytest.mark.parametrize(
    "start,end,label",
    [
        ("2026-09-17T02:00:00Z", "2026-09-17T03:00:00Z", "late Wednesday evening"),
        ("2026-09-17T00:00:00Z", "2026-09-17T01:00:00Z", "early Wednesday evening"),
        ("2026-09-17T04:00:00Z", "2026-09-17T05:00:00Z", "late Wednesday evening"),
        ("2026-09-17T07:00:00Z", "2026-09-17T08:00:00Z", "late Wednesday night"),
        ("2026-09-17T10:00:00Z", "2026-09-17T11:00:00Z", "early Thursday morning"),
        ("2026-09-17T13:00:00Z", "2026-09-17T14:00:00Z", "early Thursday morning"),
        ("2026-09-17T16:00:00Z", "2026-09-17T17:00:00Z", "late Thursday morning"),
        ("2026-09-17T19:00:00Z", "2026-09-17T22:00:00Z", "Thursday afternoon"),
        ("2026-09-17T22:00:00Z", "2026-09-17T23:00:00Z", "late Thursday afternoon"),
        ("2026-09-18T00:00:00Z", "2026-09-18T03:00:00Z", "Thursday evening"),
    ],
)
def test_time_labels_cover_every_endpoint_in_one_band(start, end, label):
    hours = round(
        (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600
    )
    window = {"start": start, "end": end, "closure": "left_open_right_closed", "hours": hours}
    result = time_label(window, ZoneInfo(CHICAGO))
    assert result is not None and result["label"] == label


def test_windows_spanning_two_bands_fall_back_to_the_explicit_clock_phrase():
    window = {
        "start": "2026-09-17T21:00:00Z",
        "end": "2026-09-18T00:00:00Z",
        "closure": "left_open_right_closed",
        "hours": 3,
    }
    assert time_label(window, ZoneInfo(CHICAGO)) is None  # 17:00, 18:00, 19:00 CDT
    # A two-hour drift before the trend hour gives a (16:00, 19:00] CDT window.
    categories = ["cloudy"] * 23 + ["mostly_cloudy"] * 2 + ["mostly_clear"] * 4
    hours = [hour(i, sky=category) for i, category in enumerate(categories)]
    result = summarize(hours)
    trend = result["transitions"][0]
    assert (trend["window"]["start"], trend["window"]["end"]) == (window["start"], window["end"])
    sentence = result["periods"][2]["sentences"][0]
    assert sentence["explicit_window_phrase"] is True and sentence["time_label"] is None
    assert sentence["text"] == "Becoming mostly clear between 4 PM and 7 PM Thursday."
    assert result["periods"][2]["transition_refs"][0]["crosses_period_start"] is True


def test_output_replays_byte_identically_and_keeps_utc_in_structured_data():
    hours = [hour(i, sky="cloudy") for i in range(3)]
    hours += [hour(i, precip="worded", ptype="rain", sky="mostly_clear") for i in range(3, 12)]
    transitions = build_transitions(
        preview(hours), display_timezone=CHICAGO, timezone_source="request"
    )
    before = JSON.serialize(transitions)
    first = build_period_summary(transitions)
    second = build_period_summary(JSON.deserialize(before))
    assert JSON.serialize(first) == JSON.serialize(second)
    assert JSON.serialize(transitions) == before
    for period in first["periods"]:
        assert period["utc_start"].endswith("Z") and period["utc_end"].endswith("Z")
        for ref in period["transition_refs"]:
            assert ref["window"]["start"].endswith("Z") and ref["window"]["end"].endswith("Z")
    assert first["transitions"] == transitions["transitions"]
    assert (
        first["text"]
        == "Rain developing early Wednesday evening. Becoming mostly clear early Wednesday evening."
    )
