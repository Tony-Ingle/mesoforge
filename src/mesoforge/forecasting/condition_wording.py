"""Versioned presentation of validated active components; no forecast calculation.

Call only after the saved-field policy, unit and temporal gates in conditions.py.
Thresholds describe this product's wording, not hazards or calibrated skill.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

WORDING_POLICY: dict[str, Any] = {
    "id": "mesoforge-condition-wording.v1",
    "purpose": "initial_presentation_rules_not_hazard_thresholds",
    "pop": {
        "unit": "1",
        "lower_inclusive_bands": [
            [0.0, "omit"],
            [0.2, "slight_chance"],
            [0.3, "chance"],
            [0.6, "likely"],
            [0.8, "direct"],
        ],
        "event": "active_native_hourly_only",
        "dry_rendering": "pop_below_0.2_and_active_hourly_qpf_exactly_zero",
        "relevant": "positive_active_hourly_qpf_or_pop_at_least_0.2",
        "positive_qpf_below_pop_threshold": "relevant_but_omit_text",
        "missing_pop": "omit_precipitation_text_no_probability_from_qpf",
        "type_scope": "instantaneous_interval_endpoint_not_whole_event",
        "type_labels": {
            "rain": "rain",
            "snow": "snow",
            "freezing_rain": "freezing rain",
            "ice_pellets": "sleet",
            "mixed": "mixed precipitation",
        },
        "generic_label": "precipitation",
    },
    "thunder": {
        "unit": "1",
        "lower_inclusive_bands": [
            [0.0, "omit"],
            [0.1, "possible"],
            [0.3, "chance"],
            [0.6, "likely"],
        ],
        "phrases": {
            "possible": "thunder possible",
            "chance": "chance of thunderstorms",
            "likely": "thunderstorms likely",
        },
        "event": "active_native_hourly_only",
        "qualification": "native NBM event; footprint unresolved",
    },
    "wind": {
        "threshold_unit": "mph",
        "metres_per_second_per_mph": 0.44704,
        "breezy": {"sustained_at_least": 15.0, "gust_at_least": 25.0},
        "windy": {"sustained_at_least": 25.0, "gust_at_least": 35.0},
        "precedence": ["windy", "breezy"],
    },
    "composition": {
        "order": ["sky", "precipitation", "thunder", "wind"],
        "sky_omitted_for_qualifiers": ["likely", "direct"],
        "joiners": {"sky_weather": "with", "weather": "and", "wind": "and"},
        "no_supported_text": "Weather conditions unavailable.",
    },
    "disabled": ["fog", "visibility", "intensity", "transitions", "winter_amounts"],
}


def _band(value: float, bands: list[Any]) -> Any:
    return next(label for threshold, label in reversed(bands) if value >= threshold)


def _result(components: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "phrase": None,
        "rendered": False,
        "reasons": [],
        "source_policy": WORDING_POLICY["id"],
        "evidence_refs": [ref for name in names for ref in components[name]["evidence_refs"]],
        "component_refs": [f"components.{name}" for name in names],
    }


def _precipitation(components: dict[str, Any]) -> dict[str, Any]:
    pop, qpf, native = (components[key] for key in ("pop", "qpf", "precipitation_type"))
    policy = WORDING_POLICY["pop"]
    result = _result(components, ("pop", "qpf", "precipitation_type"))
    result.update(
        relevant=None,
        probability_qualifier=None,
        type=deepcopy(native),
        type_label=None,
        event_interval=deepcopy(pop["interval"]),
        type_scope=policy["type_scope"],
    )
    has_pop, has_qpf = pop["state"] == "known", qpf["state"] == "known"
    if has_pop and pop["value"] < 0.2 and has_qpf and qpf["value"] == 0:
        result.update(state="not_applicable", relevant=False)
        result["type"].update(state="not_applicable", value=None)
        result["type"]["reasons"].append("rendering_only_low_pop_and_zero_qpf")
        result["reasons"].append("active_hourly_pop_below_20_percent_and_zero_qpf")
    elif (has_pop and pop["value"] >= 0.2) or (has_qpf and qpf["value"] > 0):
        result.update(state="known", relevant=True)
    else:
        result["reasons"].append("insufficient_active_occurrence_inputs")
    if not has_pop:
        result["reasons"].append("active_hourly_pop_unavailable_no_probability_substitution")
        return result
    qualifier = _band(pop["value"], policy["lower_inclusive_bands"])
    result["probability_qualifier"] = qualifier
    if qualifier == "omit":
        result["reasons"].append("pop_below_wording_threshold_even_if_qpf_positive")
        return result
    label = (
        policy["type_labels"].get(native["value"], policy["generic_label"])
        if native["state"] == "known"
        else policy["generic_label"]
    )
    if label == policy["generic_label"]:
        result["reasons"].append(f"native_type_{native['state']}_no_specific_type_claim")
    result["type_label"] = label
    result["phrase"] = {
        "slight_chance": f"a slight chance of {label}",
        "chance": f"a chance of {label}",
        "likely": f"{label} likely",
        "direct": label,
    }[qualifier]
    result["rendered"] = True
    return result


def _thunder(components: dict[str, Any]) -> dict[str, Any]:
    native = components["thunder"]
    policy = WORDING_POLICY["thunder"]
    result = _result(components, ("thunder",))
    result.update(
        probability_qualifier=None,
        event_interval=deepcopy(native["interval"]),
        event_definition_uncertainty=policy["qualification"],
    )
    if native["state"] != "known":
        result["reasons"].append("active_hourly_thunder_unavailable")
        return result
    qualifier = _band(native["value"], policy["lower_inclusive_bands"])
    result["probability_qualifier"] = qualifier
    if qualifier == "omit":
        result["state"] = "not_applicable"
        result["reasons"].append("thunder_below_10_percent_wording_threshold")
        return result
    result.update(state="known", phrase=policy["phrases"][qualifier], rendered=True)
    return result


def _wind(components: dict[str, Any]) -> dict[str, Any]:
    result = _result(components, ("wind_speed", "wind_gust"))
    result.update(value=None, unit="category", valid_time=components["wind_speed"]["valid_time"])
    policy = WORDING_POLICY["wind"]
    for descriptor in policy["precedence"]:
        for name, threshold in (
            ("wind_speed", "sustained_at_least"),
            ("wind_gust", "gust_at_least"),
        ):
            component = components[name]
            if (
                component["state"] == "known"
                and component["value"]
                >= policy[descriptor][threshold] * policy["metres_per_second_per_mph"]
            ):
                result.update(state="known", value=descriptor, phrase=descriptor, rendered=True)
                result["reasons"].append(f"{name}_meets_{descriptor}_presentation_threshold")
                return result
    if all(components[name]["state"] == "known" for name in ("wind_speed", "wind_gust")):
        result["state"] = "not_applicable"
        result["reasons"].append("wind_below_presentation_thresholds")
    else:
        result["reasons"].append("missing_wind_component_cannot_confirm_below_thresholds")
    return result


def _sky(components: dict[str, Any], dominant: list[str]) -> dict[str, Any]:
    native = components["sky"]
    result = _result(components, ("sky",))
    result["category"] = native.get("sky_category")
    if native["state"] != "known":
        result["reasons"].append("active_sky_unavailable_no_shadow_cloud_substitution")
        return result
    result.update(state="known", phrase=native["sky_category"].replace("_", " "))
    if dominant:
        result["reasons"].append(f"sky_omitted_from_text_{'_'.join(dominant)}_at_least_likely")
    else:
        result["rendered"] = True
    return result


def build_wording(components: dict[str, Any]) -> dict[str, Any]:
    """Preserve native states; determine applicability in a separate presentation object."""
    precipitation, thunder, wind = (
        _precipitation(components),
        _thunder(components),
        _wind(components),
    )
    composition = WORDING_POLICY["composition"]
    joiners = composition["joiners"]
    dominant = [
        name
        for name, part in (("precipitation", precipitation), ("thunder", thunder))
        if part["rendered"]
        and part["probability_qualifier"] in composition["sky_omitted_for_qualifiers"]
    ]
    sky = _sky(components, dominant)
    weather = f" {joiners['weather']} ".join(
        part["phrase"] for part in (precipitation, thunder) if part["rendered"]
    )
    lead = sky["phrase"] if sky["rendered"] else ""
    if lead and weather:
        text = f"{lead} {joiners['sky_weather']} {weather}"
    else:
        text = lead or weather
        if text.startswith("a "):
            text = text[2:]
    if wind["rendered"]:
        text = f"{text} {joiners['wind']} {wind['phrase']}" if text else str(wind["phrase"])
    text = text[0].upper() + text[1:] if text else composition["no_supported_text"]
    return {
        "policy_id": WORDING_POLICY["id"],
        "sky": sky,
        "precipitation": precipitation,
        "thunder": thunder,
        "wind": wind,
        "text": text,
    }
