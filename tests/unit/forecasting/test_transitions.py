"""Evolution facts come only from consecutive available endpoints; text never exceeds them."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.forecasting.transitions import SKY_SCALE, TRANSITION_POLICY, build_transitions
from mesoforge.storage.json import CanonicalJsonSerializer

TARGET = datetime(2026, 9, 16, 22, tzinfo=UTC)
LABELS = {
    "rain": "rain",
    "snow": "snow",
    "freezing_rain": "freezing rain",
    "ice_pellets": "sleet",
    "mixed": "mixed precipitation",
}
JSON = CanonicalJsonSerializer()


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def hour(index, *, precip="dry", qualifier=None, ptype=None, sky="mostly_cloudy"):
    """One already-described point hour at the transition detector's input boundary.

    precip: dry (not applicable), trace (applicable, wording omitted), worded
    (applicable with a probability band) or unavailable. ptype: None/unknown,
    ambiguous or a known type. sky: a category or None for unavailable.
    """
    valid = _iso(TARGET + timedelta(hours=index + 1))
    ref = f"/forecast/local_grid_baseline/cells/24/hours/{index}"
    if precip == "dry":
        state, relevant, band = "not_applicable", False, "omit"
        type_state, type_value = "not_applicable", None
    elif precip == "unavailable":
        state, relevant, band = "unavailable", None, None
        type_state, type_value = "unavailable", None
    else:
        state, relevant = "known", True
        band = "omit" if precip == "trace" else (qualifier or "chance")
        if ptype in (None, "unknown"):
            type_state, type_value = "unknown", "unknown"
        elif ptype == "ambiguous":
            type_state, type_value = "ambiguous", "ambiguous"
        else:
            type_state, type_value = "known", ptype
    worded = band not in (None, "omit")
    label = LABELS.get(ptype, "precipitation") if type_state == "known" else "precipitation"
    return {
        "horizon_hours": index + 1,
        "valid_time": valid,
        "components": {
            "sky": {
                "state": "known" if sky else "unavailable",
                "sky_category": sky,
                "evidence_refs": [f"{ref}/surface/fields/cloud_area_fraction"],
            }
        },
        "presentation": {
            "precipitation": {
                "state": state,
                "relevant": relevant,
                "probability_qualifier": band,
                "type": {
                    "state": type_state,
                    "value": type_value,
                    "evidence_refs": [f"{ref}/surface/fields/precipitation_type"],
                },
                "type_label": label if worded else None,
                "evidence_refs": [f"{ref}/surface/fields/probability_of_precipitation_1h"],
            },
            "sky": {
                "state": "known" if sky else "unavailable",
                "evidence_refs": [f"{ref}/surface/fields/cloud_area_fraction"],
            },
        },
        "rendering": {"text": "synthetic"},
    }


def preview(hours):
    return {
        "schema_version": "mesoforge.weather-condition-preview.v2",
        "ruleset_id": "saved-active-fields-condition-preview.v3",
        "scope": {"requested": "point"},
        "wording_policy": {"id": "mesoforge-condition-wording.v1"},
        "input": {
            "issued_forecast_id": "ed771bc5-c8da-4371-99bc-cf59d28e8ad8",
            "issued_payload_digest": "sha256:" + "a" * 64,
            "target_reference_time": _iso(TARGET),
        },
        "center_point": {"hours": hours},
    }


def sky_sequence(categories):
    return preview([hour(i, sky=category) for i, category in enumerate(categories)])


def window(result, index):
    fact = result["transitions"][index]
    return fact["window"]["start"], fact["window"]["end"], fact["window"]["closure"]


def test_dry_then_worded_rain_is_an_onset_known_only_within_one_hourly_interval():
    hours = [hour(i) for i in range(3)] + [
        hour(i, precip="worded", ptype="rain") for i in range(3, 6)
    ]
    result = build_transitions(preview(hours))
    assert [f["type"] for f in result["transitions"]] == [
        "precipitation_onset",
        "precipitation_wording_onset",
    ]
    for index in range(2):
        assert window(result, index) == (
            "2026-09-17T01:00:00Z",
            "2026-09-17T02:00:00Z",
            "left_open_right_closed",
        )
        fact = result["transitions"][index]
        assert fact["window"]["hours"] == 1
        assert fact["hour_refs"] == ["center_point.hours[2]", "center_point.hours[3]"]
        assert fact["status"] == "known" and fact["policy_id"] == TRANSITION_POLICY["id"]
        assert fact["label"] == "rain"
        assert fact["evidence_refs"] == [
            "/forecast/local_grid_baseline/cells/24/hours/2/surface/fields/probability_of_precipitation_1h",
            "/forecast/local_grid_baseline/cells/24/hours/3/surface/fields/probability_of_precipitation_1h",
        ]
    assert result["transitions"][0]["previous"]["state"] == {
        "occurrence": "not_applicable",
        "wording": "omitted",
    }
    assert result["transitions"][0]["next"]["state"] == {
        "occurrence": "applicable",
        "wording": "chance",
    }
    assert result["rendering"]["items"] == [
        {"transition": 1, "text": "Rain developing between 1 AM and 2 AM Thursday"}
    ]
    assert result["rendering"]["omitted"] == [
        {"transition": 0, "reason": "applicability_transitions_are_structured_only"}
    ]
    assert result["rendering"]["text"] == "Rain developing between 1 AM and 2 AM Thursday."


def test_worded_precipitation_then_dry_is_an_ending_labelled_from_the_last_worded_hour():
    hours = [hour(i, precip="worded", ptype="snow", qualifier="likely") for i in range(3)]
    hours += [hour(i) for i in range(3, 6)]
    result = build_transitions(preview(hours))
    assert [f["type"] for f in result["transitions"]] == [
        "precipitation_ending",
        "precipitation_wording_ending",
    ]
    assert window(result, 1) == (
        "2026-09-17T01:00:00Z",
        "2026-09-17T02:00:00Z",
        "left_open_right_closed",
    )
    assert result["rendering"]["items"] == [
        {"transition": 1, "text": "Snow ending between 1 AM and 2 AM Thursday"}
    ]


def test_trace_precipitation_is_applicable_but_only_worded_hours_render():
    hours = [hour(0), hour(1, precip="trace"), hour(2, precip="trace"), hour(3, precip="worded")]
    result = build_transitions(preview(hours))
    assert [(f["type"], f["window"]["end"]) for f in result["transitions"]] == [
        ("precipitation_onset", "2026-09-17T00:00:00Z"),
        ("precipitation_wording_onset", "2026-09-17T02:00:00Z"),
    ]
    assert result["transitions"][0]["label"] == "precipitation"
    assert result["rendering"]["items"] == [
        {"transition": 1, "text": "Precipitation developing between 1 AM and 2 AM Thursday"}
    ]
    assert [s["precipitation_wording"] for s in result["hourly_states"]] == [
        "omitted",
        "omitted",
        "omitted",
        "chance",
    ]


@pytest.mark.parametrize(
    "before,after,text",
    [
        ("rain", "snow", "Rain changing to snow between 1 AM and 2 AM Thursday"),
        ("snow", "rain", "Snow changing to rain between 1 AM and 2 AM Thursday"),
        ("rain", "mixed", "Rain changing to mixed precipitation between 1 AM and 2 AM Thursday"),
        ("mixed", "snow", "Mixed precipitation changing to snow between 1 AM and 2 AM Thursday"),
        ("freezing_rain", "rain", "Freezing rain changing to rain between 1 AM and 2 AM Thursday"),
        ("ice_pellets", "snow", "Sleet changing to snow between 1 AM and 2 AM Thursday"),
    ],
)
def test_known_type_changes_are_rendered_with_project_labels(before, after, text):
    hours = [hour(i, precip="worded", ptype=before) for i in range(3)]
    hours += [hour(i, precip="worded", ptype=after) for i in range(3, 6)]
    result = build_transitions(preview(hours))
    assert [f["type"] for f in result["transitions"]] == ["precipitation_type_change"]
    fact = result["transitions"][0]
    assert fact["status"] == "known"
    assert fact["previous"]["state"] == {"state": "known", "value": before}
    assert fact["next"]["state"] == {"state": "known", "value": after}
    assert window(result, 0) == (
        "2026-09-17T01:00:00Z",
        "2026-09-17T02:00:00Z",
        "left_open_right_closed",
    )
    assert result["rendering"]["items"] == [{"transition": 0, "text": text}]


def test_rain_ambiguous_snow_stays_two_conservative_facts_and_is_not_rendered():
    hours = [hour(0, precip="worded", ptype="rain"), hour(1, precip="worded", ptype="rain")]
    hours += [hour(2, precip="worded", ptype="ambiguous")]
    hours += [hour(3, precip="worded", ptype="snow"), hour(4, precip="worded", ptype="snow")]
    result = build_transitions(preview(hours))
    facts = result["transitions"]
    assert [(f["type"], f["status"]) for f in facts] == [
        ("precipitation_type_change", "ambiguous"),
        ("precipitation_type_change", "ambiguous"),
    ]
    assert facts[0]["previous"]["state"] == {"state": "known", "value": "rain"}
    assert facts[0]["next"]["state"] == {"state": "ambiguous", "value": None}
    assert facts[1]["previous"]["state"] == {"state": "ambiguous", "value": None}
    assert facts[1]["next"]["state"] == {"state": "known", "value": "snow"}
    assert [f["window"]["end"] for f in facts] == ["2026-09-17T01:00:00Z", "2026-09-17T02:00:00Z"]
    assert result["rendering"]["items"] == []
    assert [o["reason"] for o in result["rendering"]["omitted"]] == [
        "type_change_involves_unresolved_state"
    ] * 2
    assert not any(
        f["previous"]["state"]["value"] == "rain" and f["next"]["state"]["value"] == "snow"
        for f in facts
    )


def test_unknown_and_ambiguous_endpoints_never_become_known_claims():
    hours = [
        hour(0, precip="worded", ptype="unknown"),
        hour(1, precip="worded", ptype="ambiguous"),
        hour(2, precip="worded", ptype="unknown"),
        hour(3, precip="worded", ptype="rain"),
        hour(4, precip="worded", ptype="unknown"),
    ]
    result = build_transitions(preview(hours))
    facts = result["transitions"]
    assert [(f["type"], f["status"], f["window"]["end"]) for f in facts] == [
        ("precipitation_type_change", "unknown", "2026-09-17T02:00:00Z"),
        ("precipitation_type_change", "unknown", "2026-09-17T03:00:00Z"),
    ]
    assert result["rendering"]["items"] == []
    assert [s["precipitation_type"]["state"] for s in result["hourly_states"]] == [
        "unknown",
        "ambiguous",
        "unknown",
        "known",
        "unknown",
    ]


def test_unavailable_hours_break_every_track_and_are_reported_as_gaps():
    hours = [
        hour(0, precip="worded", ptype="rain", sky="cloudy"),
        hour(1, precip="unavailable", sky=None),
        hour(2, sky="clear"),
        hour(3, sky="clear"),
        hour(4, sky="clear"),
    ]
    result = build_transitions(preview(hours))
    assert result["transitions"] == []
    assert {(g["track"], g["hour_ref"]) for g in result["gaps"]} == {
        ("precipitation_occurrence", "center_point.hours[1]"),
        ("precipitation_wording", "center_point.hours[1]"),
        ("precipitation_type", "center_point.hours[1]"),
        ("sky", "center_point.hours[1]"),
    }
    assert result["hourly_states"][1]["precipitation_occurrence"] == "unavailable"
    assert result["hourly_states"][1]["sky"] == {
        "state": "unavailable",
        "category": None,
        "level": None,
    }


def test_first_and_last_hours_are_boundaries_not_transitions():
    assert build_transitions(preview([hour(0, precip="worded", ptype="rain")]))["transitions"] == []
    result = build_transitions(preview([hour(0), hour(1, precip="worded", ptype="rain")]))
    assert [f["window"]["start"] for f in result["transitions"]] == ["2026-09-16T23:00:00Z"] * 2
    late = sky_sequence(["mostly_cloudy"] * 4 + ["clear", "clear"])
    assert build_transitions(late)["transitions"] == []


def test_clearing_needs_two_levels_and_persistence_and_starts_at_the_last_reference_hour():
    result = build_transitions(sky_sequence(["mostly_cloudy"] * 3 + ["mostly_clear"] * 3))
    fact = result["transitions"][0]
    assert [f["type"] for f in result["transitions"]] == ["sky_trend"]
    assert fact["direction"] == "clearing"
    assert (fact["from_category"], fact["to_category"], fact["levels_changed"]) == (
        "mostly_cloudy",
        "mostly_clear",
        2,
    )
    assert fact["persistence_hours"] == 3 and fact["confirmed_through"] == "center_point.hours[5]"
    assert window(result, 0) == (
        "2026-09-17T01:00:00Z",
        "2026-09-17T02:00:00Z",
        "left_open_right_closed",
    )
    assert fact["previous"]["state"] == {"state": "known", "category": "mostly_cloudy", "level": 3}
    assert fact["next"]["state"] == {"state": "known", "category": "mostly_clear", "level": 1}
    assert result["rendering"]["items"] == [
        {"transition": 0, "text": "Becoming mostly clear between 1 AM and 2 AM Thursday"}
    ]


def test_increasing_clouds_is_the_mirror_trend():
    result = build_transitions(sky_sequence(["mostly_clear"] * 2 + ["mostly_cloudy"] * 3))
    fact = result["transitions"][0]
    assert fact["direction"] == "increasing_clouds"
    assert (fact["from_category"], fact["to_category"]) == ("mostly_clear", "mostly_cloudy")
    assert result["rendering"]["items"][0]["text"] == (
        "Becoming mostly cloudy between 12 AM and 1 AM Thursday"
    )


@pytest.mark.parametrize(
    "categories",
    [
        [
            "mostly_cloudy",
            "mostly_cloudy",
            "mostly_clear",
            "mostly_cloudy",
            "mostly_cloudy",
            "mostly_cloudy",
        ],
        [
            "mostly_cloudy",
            "mostly_cloudy",
            "mostly_clear",
            "partly_cloudy",
            "mostly_cloudy",
            "mostly_cloudy",
        ],
        ["mostly_cloudy", "mostly_cloudy", "mostly_clear", "mostly_clear", "mostly_cloudy"],
        ["cloudy", "mostly_cloudy", "cloudy", "mostly_cloudy", "cloudy", "mostly_cloudy"],
    ],
)
def test_short_wobbles_and_one_level_changes_are_not_trends(categories):
    assert build_transitions(sky_sequence(categories))["transitions"] == []


def test_slow_drift_window_runs_from_the_last_hour_at_the_reference_level():
    categories = [
        "mostly_cloudy",
        "partly_cloudy",
        "partly_cloudy",
        "mostly_clear",
        "mostly_clear",
        "mostly_clear",
    ]
    result = build_transitions(sky_sequence(categories))
    fact = result["transitions"][0]
    assert fact["hour_refs"] == ["center_point.hours[0]", "center_point.hours[3]"]
    assert fact["window"]["hours"] == 3
    assert result["rendering"]["items"][0]["text"] == (
        "Becoming mostly clear between 11 PM Wednesday and 2 AM Thursday"
    )


def test_after_a_confirmed_trend_the_new_level_is_the_reference():
    categories = ["cloudy"] * 2 + ["mostly_clear"] * 3 + ["cloudy"] * 3
    result = build_transitions(sky_sequence(categories))
    assert [
        (f["direction"], f["from_category"], f["to_category"]) for f in result["transitions"]
    ] == [
        ("clearing", "cloudy", "mostly_clear"),
        ("increasing_clouds", "mostly_clear", "cloudy"),
    ]


def test_sky_scale_is_the_existing_category_order():
    assert SKY_SCALE == ("clear", "mostly_clear", "partly_cloudy", "mostly_cloudy", "cloudy")
    assert TRANSITION_POLICY["tracks"]["sky"]["scale"] == list(SKY_SCALE)


def test_display_timezone_changes_only_rendered_clock_times():
    hours = [hour(i) for i in range(6)] + [
        hour(i, precip="worded", ptype="rain") for i in range(6, 8)
    ]
    utc = build_transitions(preview(hours))
    chicago = build_transitions(
        preview(hours), display_timezone="America/Chicago", timezone_source="request"
    )
    assert utc["transitions"] == chicago["transitions"]
    assert utc["rendering"]["items"][0]["text"] == "Rain developing between 4 AM and 5 AM Thursday"
    assert chicago["rendering"]["items"][0]["text"] == (
        "Rain developing between 11 PM Wednesday and 12 AM Thursday"
    )
    assert chicago["display_timezone"] == {"name": "America/Chicago", "source": "request"}
    assert chicago["rendering"]["timezone"] == "America/Chicago"
    with pytest.raises(ValueError, match="timezone"):
        build_transitions(preview(hours), display_timezone="Mars/Olympus")


def test_facts_are_ordered_by_window_end_then_track():
    hours = [hour(0, sky="cloudy"), hour(1, sky="cloudy")]
    hours += [hour(i, precip="worded", ptype="rain", sky="mostly_clear") for i in range(2, 5)]
    result = build_transitions(preview(hours))
    assert [(f["window"]["end"], f["track"]) for f in result["transitions"]] == [
        ("2026-09-17T01:00:00Z", "precipitation_occurrence"),
        ("2026-09-17T01:00:00Z", "precipitation_wording"),
        ("2026-09-17T01:00:00Z", "sky"),
    ]


def test_output_replays_byte_identically_and_leaves_the_preview_unchanged():
    hours = [hour(i, sky="cloudy") for i in range(3)]
    hours += [hour(i, precip="worded", ptype="snow", sky="clear") for i in range(3, 6)]
    source = preview(hours)
    before = JSON.serialize(source)
    first = build_transitions(source)
    second = build_transitions(JSON.deserialize(before))
    assert JSON.serialize(first) == JSON.serialize(second)
    assert JSON.serialize(source) == before
    assert first["schema_version"] == "mesoforge.weather-transitions.v1"
    assert first["transition_policy"] == TRANSITION_POLICY
    assert first["input"]["conditions_scope"] == "point" and first["input"]["hours"] == 6
    assert len(first["hourly_states"]) == 6
    assert deepcopy(first["transitions"]) == first["transitions"]
