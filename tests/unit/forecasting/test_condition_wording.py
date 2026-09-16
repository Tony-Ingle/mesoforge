"""Product wording is deterministic and never promotes native evidence into occurrence."""

from copy import deepcopy

import pytest

from mesoforge.forecasting.condition_wording import WORDING_POLICY, build_wording
from mesoforge.storage.json import CanonicalJsonSerializer

VALID = "2026-09-14T00:00:00Z"
START = "2026-09-13T23:00:00Z"
MPH_TO_METRES_PER_SECOND = 0.44704


@pytest.fixture
def components():
    """Already-validated active components, at the pure renderer's input boundary."""

    def component(name, value, unit, *, state="known", interval=False):
        return {
            "state": state,
            "value": value,
            "unit": unit,
            "valid_time": VALID,
            "interval": {
                "start": START,
                "end": VALID,
                "closure": "left_open_right_closed",
            }
            if interval
            else None,
            "source_policy": {"id": f"existing-approved-{name}.v1"},
            "reasons": [],
            "evidence_refs": [f"/forecast/local_grid_baseline/cells/24/hours/0/{name}"],
        }

    return {
        "sky": {
            **component("sky", 0.02, "1"),
            "sky_category": "clear",
            "cloud_percentage": 2.0,
        },
        "pop": {
            **component("pop", 0.0, "1", interval=True),
            "threshold": {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"},
            "model": "NBM",
        },
        "qpf": component("qpf", 0.0, "kg/m^2", interval=True),
        "precipitation_type": {
            **component("precipitation_type", "unknown", "category", state="unknown"),
            "supported_types": [],
            "reasons": ["instantaneous_endpoint_type_not_an_interval_occurrence"],
        },
        "thunder": {
            **component("thunder", 0.0, "1", interval=True),
            "event_definition": {"id": "nbm_native_probability_of_thunder"},
            "spatial_support": {
                "kind": "provider_native_probability_grid",
                "geometry_status": "not_encoded",
                "radius_km": None,
            },
        },
        "wind_speed": component("wind_speed", 0.0, "m/s"),
        "wind_gust": component("wind_gust", 0.0, "m/s"),
        "wind_direction": component("wind_direction", None, "degree", state="not_applicable"),
    }


@pytest.mark.parametrize(
    "probability,qualifier,phrase_fragment",
    [
        (0.0, "omit", None),
        (0.199999999, "omit", None),
        (0.2, "slight_chance", "slight chance"),
        (0.299999999, "slight_chance", "slight chance"),
        (0.3, "chance", "chance"),
        (0.599999999, "chance", "chance"),
        (0.6, "likely", "likely"),
        (0.799999999, "likely", "likely"),
        (0.8, "direct", "rain"),
        (1.0, "direct", "rain"),
    ],
)
def test_pop_uses_unrounded_fraction_at_every_boundary(
    components, probability, qualifier, phrase_fragment
):
    components["pop"]["value"] = probability
    components["qpf"]["value"] = 0.01
    components["precipitation_type"].update(state="known", value="rain")
    before = deepcopy(components)
    result = build_wording(components)
    precipitation = result["precipitation"]
    assert precipitation["relevant"] is True
    assert precipitation["probability_qualifier"] == qualifier
    if phrase_fragment is None:
        assert precipitation["phrase"] is None
        assert "rain" not in result["text"].lower()
    else:
        assert phrase_fragment in precipitation["phrase"].lower()
        assert "(" not in result["text"]
        assert precipitation["type_scope"] == WORDING_POLICY["pop"]["type_scope"]
        if qualifier == "direct":
            assert "chance" not in precipitation["phrase"]
            assert "likely" not in precipitation["phrase"]
    assert components == before


@pytest.mark.parametrize("probability", [0.0, 0.199999999])
def test_known_dry_is_rendering_not_applicable_without_erasing_native_unknown(
    components, probability
):
    components["pop"]["value"] = probability
    before = deepcopy(components)
    result = build_wording(components)
    assert result["text"] == "Clear"
    assert result["precipitation"]["state"] == "not_applicable"
    assert result["precipitation"]["relevant"] is False
    assert result["precipitation"]["type"]["state"] == "not_applicable"
    assert result["precipitation"]["reasons"]
    assert components == before
    assert components["precipitation_type"]["state"] == "unknown"


@pytest.mark.parametrize("native_type", ["rain", "snow", "freezing_rain", "ice_pellets", "mixed"])
def test_native_flag_alone_cannot_manufacture_precipitation(components, native_type):
    components["precipitation_type"].update(state="known", value=native_type)
    result = build_wording(components)
    assert result["precipitation"]["relevant"] is False
    assert result["precipitation"]["phrase"] is None
    assert result["text"] == "Clear"
    assert components["precipitation_type"]["value"] == native_type


@pytest.mark.parametrize("qpf", [0.000000001, 0.01, 1.0])
def test_positive_qpf_is_relevant_but_does_not_override_pop_wording(components, qpf):
    components["qpf"]["value"] = qpf
    result = build_wording(components)
    precipitation = result["precipitation"]
    assert precipitation["relevant"] is True
    assert precipitation["type"]["state"] == "unknown"
    assert precipitation["phrase"] is None
    assert result["text"] == "Clear"


@pytest.mark.parametrize("qpf", [0.0, 0.001])
def test_missing_pop_never_becomes_zero_or_an_invented_probability_phrase(components, qpf):
    components["qpf"]["value"] = qpf
    components["pop"].update(state="unavailable", value=None, reasons=["missing active NBM"])
    result = build_wording(components)
    precipitation = result["precipitation"]
    assert precipitation["relevant"] is (True if qpf > 0 else None)
    assert precipitation["probability_qualifier"] is None
    assert precipitation["phrase"] is None
    assert precipitation["type"]["state"] == "unknown"
    assert result["text"] == "Clear"


@pytest.mark.parametrize("probability,relevant", [(0.0, None), (0.2, True)])
def test_missing_qpf_does_not_prove_dryness_but_pop_can_support_occurrence(
    components, probability, relevant
):
    components["qpf"].update(state="unavailable", value=None)
    components["pop"]["value"] = probability
    precipitation = build_wording(components)["precipitation"]
    assert precipitation["relevant"] is relevant
    assert precipitation["type"]["state"] == "unknown"
    if relevant:
        assert "slight chance of precipitation" in precipitation["phrase"].lower()
    else:
        assert precipitation["state"] == "unavailable"
        assert precipitation["phrase"] is None


@pytest.mark.parametrize(
    "native_type,label",
    [
        ("rain", "rain"),
        ("snow", "snow"),
        ("freezing_rain", "freezing rain"),
        ("ice_pellets", "sleet"),
        ("mixed", "mixed precipitation"),
    ],
)
def test_supported_types_are_endpoint_labels_not_entire_interval_claims(
    components, native_type, label
):
    components["pop"]["value"] = 0.5
    components["precipitation_type"].update(state="known", value=native_type)
    result = build_wording(components)
    precipitation = result["precipitation"]
    assert label in precipitation["phrase"].lower()
    assert precipitation["type_label"] == label
    assert precipitation["type_scope"] == WORDING_POLICY["pop"]["type_scope"]
    assert precipitation["type"] == components["precipitation_type"]
    assert precipitation["type"]["interval"] is None
    assert components["pop"]["interval"] == {
        "start": START,
        "end": VALID,
        "closure": "left_open_right_closed",
    }


@pytest.mark.parametrize("state", ["unknown", "ambiguous", "unavailable"])
def test_missing_or_ambiguous_type_preserves_distinction_and_uses_generic_precipitation(
    components, state
):
    components["pop"]["value"] = 0.65
    components["precipitation_type"].update(state=state, value=state)
    result = build_wording(components)
    precipitation = result["precipitation"]
    assert precipitation["type"]["state"] == state
    assert "precipitation" in precipitation["phrase"].lower()
    assert "likely" in precipitation["phrase"].lower()
    assert "rain" not in result["text"].lower()
    assert "snow" not in result["text"].lower()


@pytest.mark.parametrize(
    "probability,phrase",
    [
        (0.0, None),
        (0.099999999, None),
        (0.1, "thunder possible"),
        (0.299999999, "thunder possible"),
        (0.3, "chance of thunderstorms"),
        (0.599999999, "chance of thunderstorms"),
        (0.6, "thunderstorms likely"),
        (1.0, "thunderstorms likely"),
    ],
)
def test_thunder_boundaries_retain_event_footprint_uncertainty(components, probability, phrase):
    components["thunder"]["value"] = probability
    before = deepcopy(components["thunder"])
    result = build_wording(components)
    if phrase is None:
        assert result["thunder"]["phrase"] is None
        assert "thunder" not in result["text"].lower()
    else:
        assert result["thunder"]["phrase"] == phrase
        assert phrase in result["text"].lower()
        assert "(" not in result["text"]
    qualification = WORDING_POLICY["thunder"]["qualification"]
    assert result["thunder"]["event_definition_uncertainty"] == qualification
    assert components["thunder"] == before


def test_missing_active_thunder_cannot_use_shadow_probability(components):
    components["thunder"].update(state="unavailable", value=None)
    components["thunder_shadow_3h"] = {"state": "known", "value": 1.0, "unit": "1"}
    result = build_wording(components)
    assert result["thunder"]["state"] == "unavailable"
    assert result["thunder"]["phrase"] is None
    assert "thunder" not in result["text"].lower()


@pytest.mark.parametrize(
    "speed_mph,gust_mph,descriptor",
    [
        (0.0, 0.0, None),
        (14.9999999, 24.9999999, None),
        (15.0, 20.0, "breezy"),
        (0.0, 25.0, "breezy"),
        (24.9999999, 34.9999999, "breezy"),
        (25.0, 30.0, "windy"),
        (0.0, 35.0, "windy"),
        (25.0, 35.0, "windy"),
    ],
)
def test_wind_thresholds_are_mph_converted_to_native_units_without_rounding(
    components, speed_mph, gust_mph, descriptor
):
    components["wind_speed"]["value"] = speed_mph * MPH_TO_METRES_PER_SECOND
    components["wind_gust"]["value"] = gust_mph * MPH_TO_METRES_PER_SECOND
    before = deepcopy(components)
    result = build_wording(components)
    assert result["wind"]["value"] == descriptor
    if descriptor:
        assert result["text"] == f"Clear and {descriptor}"
        assert "hazard" not in result["text"].lower()
    else:
        assert result["wind"]["phrase"] is None
        assert result["text"] == "Clear"
    assert components == before


@pytest.mark.parametrize(
    "known_field,mph,descriptor",
    [
        ("wind_speed", 0.0, None),
        ("wind_gust", 0.0, None),
        ("wind_speed", 15.0, "breezy"),
        ("wind_gust", 25.0, "breezy"),
        ("wind_speed", 25.0, "windy"),
        ("wind_gust", 35.0, "windy"),
    ],
)
def test_partial_wind_can_support_a_descriptor_but_cannot_prove_no_descriptor(
    components, known_field, mph, descriptor
):
    missing_field = "wind_gust" if known_field == "wind_speed" else "wind_speed"
    components[known_field]["value"] = mph * MPH_TO_METRES_PER_SECOND
    components[missing_field].update(state="unavailable", value=None)
    result = build_wording(components)
    assert result["wind"]["value"] == descriptor
    assert result["wind"]["state"] == ("known" if descriptor else "unavailable")


def test_precipitation_thunder_and_wind_compose_in_order_without_hazard_or_intensity(components):
    components["sky"]["sky_category"] = "mostly_cloudy"
    components["pop"]["value"] = 0.35
    components["precipitation_type"].update(state="known", value="rain")
    components["thunder"]["value"] = 0.4
    components["wind_gust"]["value"] = 35 * MPH_TO_METRES_PER_SECOND
    result = build_wording(components)
    assert result["text"] == (
        "Mostly cloudy with a chance of rain and chance of thunderstorms and windy"
    )
    text = result["text"].lower()
    assert text.index("rain") < text.index("thunder") < text.index("windy")
    assert result["sky"]["rendered"] is True
    for unsupported in ("heavy", "moderate", "light rain", "warning", "advisory", "changing"):
        assert unsupported not in text


@pytest.mark.parametrize(
    "sky_category,pop,native_type,thunder,gust_mph,expected",
    [
        ("clear", 0.0, "unknown", 0.0, 0.0, "Clear"),
        ("mostly_cloudy", 0.1, "unknown", 0.0, 25.0, "Mostly cloudy and breezy"),
        ("partly_cloudy", 0.25, "rain", 0.0, 0.0, "Partly cloudy with a slight chance of rain"),
        ("cloudy", 0.45, "unknown", 0.0, 0.0, "Cloudy with a chance of precipitation"),
        ("cloudy", 0.7, "rain", 0.0, 0.0, "Rain likely"),
        ("cloudy", 0.65, "snow", 0.0, 25.0, "Snow likely and breezy"),
        (None, 0.0, "unknown", 0.4, 0.0, "Chance of thunderstorms"),
        ("cloudy", 0.1, "unknown", 0.7, 35.0, "Thunderstorms likely and windy"),
        ("cloudy", 0.9, "rain", 0.7, 35.0, "Rain and thunderstorms likely and windy"),
        ("cloudy", 0.85, "unknown", 0.0, 0.0, "Precipitation"),
        (None, 0.0, "unknown", 0.0, 0.0, "Weather conditions unavailable."),
    ],
)
def test_approved_shapes_come_from_composition_not_special_cases(
    components, sky_category, pop, native_type, thunder, gust_mph, expected
):
    if sky_category is None:
        components["sky"].update(state="unavailable", value=None, sky_category=None)
    else:
        components["sky"]["sky_category"] = sky_category
    components["pop"]["value"] = pop
    if native_type != "unknown":
        components["precipitation_type"].update(state="known", value=native_type)
    components["thunder"]["value"] = thunder
    components["wind_gust"]["value"] = gust_mph * MPH_TO_METRES_PER_SECOND
    before = deepcopy(components)
    result = build_wording(components)
    assert result["text"] == expected
    assert components == before


def test_dominant_precipitation_or_thunder_omits_sky_text_but_retains_known_sky(components):
    components["sky"]["sky_category"] = "cloudy"
    components["pop"]["value"] = 0.6
    components["precipitation_type"].update(state="known", value="rain")
    result = build_wording(components)
    assert result["text"] == "Rain likely"
    assert result["sky"]["state"] == "known" and result["sky"]["phrase"] == "cloudy"
    assert result["sky"]["rendered"] is False
    assert result["sky"]["reasons"] == ["sky_omitted_from_text_precipitation_at_least_likely"]
    assert result["sky"]["component_refs"] == ["components.sky"]
    assert components["sky"]["state"] == "known"
    components["pop"]["value"] = 0.599999999
    result = build_wording(components)
    assert result["text"] == "Cloudy with a chance of rain"
    assert result["sky"]["rendered"] is True and result["sky"]["reasons"] == []
    components["pop"]["value"] = 0.0
    components["thunder"]["value"] = 0.6
    result = build_wording(components)
    assert result["text"] == "Thunderstorms likely"
    assert result["sky"]["reasons"] == ["sky_omitted_from_text_thunder_at_least_likely"]


def test_direct_band_with_unresolved_type_states_generic_precipitation_only(components):
    components["sky"]["sky_category"] = "cloudy"
    components["pop"]["value"] = 0.8
    components["precipitation_type"].update(state="ambiguous", value="ambiguous")
    result = build_wording(components)
    precipitation = result["precipitation"]
    assert result["text"] == "Precipitation"
    assert precipitation["probability_qualifier"] == "direct"
    assert precipitation["type_label"] == "precipitation"
    assert precipitation["type"]["state"] == "ambiguous"
    assert "native_type_ambiguous_no_specific_type_claim" in precipitation["reasons"]


def test_no_supported_component_returns_explicit_unavailable_text(components):
    for component in components.values():
        component.update(state="unavailable", value=None)
    components["sky"]["sky_category"] = None
    result = build_wording(components)
    assert result["text"] == "Weather conditions unavailable."
    for name in ("sky", "precipitation", "thunder", "wind"):
        assert result[name]["rendered"] is False and result[name]["phrase"] is None
        assert result[name]["state"] == "unavailable" and result[name]["reasons"]


def test_missing_active_sky_never_uses_shadow_cloud_or_visibility_to_fill_text(components):
    components["sky"].update(state="unavailable", value=None, sky_category=None)
    components["cloud_shadow"] = {"state": "known", "value": 1.0, "sky_category": "cloudy"}
    components["visibility"] = {"state": "unavailable", "value": None, "evidence": 0.0}
    components["pop"]["value"] = 0.4
    result = build_wording(components)
    text = result["text"].lower()
    assert "chance of precipitation" in text
    for unsupported in ("clear", "cloud", "fog", "visibility"):
        assert unsupported not in text


def test_missing_active_pop_does_not_promote_incompatible_shadow_window_or_winter_fields(
    components,
):
    components["pop"].update(state="unavailable", value=None)
    components["qpf"].update(state="unavailable", value=None)
    components["pop_shadow_6h"] = {
        "state": "known",
        "value": 1.0,
        "interval": {"start": "2026-09-13T18:00:00Z", "end": VALID},
    }
    for name in ("snowfall", "kuchera_snowfall", "freezing_rain_liquid", "flat_ice"):
        components[name] = {"state": "unavailable", "value": None, "native_evidence": 10.0}
    result = build_wording(components)
    assert result["precipitation"]["state"] == "unavailable"
    assert result["precipitation"]["relevant"] is None
    assert result["precipitation"]["phrase"] is None
    assert result["text"] == "Clear"


def test_versioned_output_replays_byte_identically_and_does_not_alias_native_type(components):
    components["pop"]["value"] = 0.3
    before = deepcopy(components)
    serializer = CanonicalJsonSerializer()
    first = build_wording(components)
    second = build_wording(components)
    assert first["policy_id"] == "mesoforge-condition-wording.v1"
    assert first["policy_id"] in WORDING_POLICY.values()
    assert serializer.serialize(first) == serializer.serialize(second)
    assert components == before
    first["precipitation"]["type"]["reasons"].append("changed rendered copy")
    assert components == before
    assert second["precipitation"]["type"] == before["precipitation_type"]
