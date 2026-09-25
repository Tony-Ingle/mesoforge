"""Present saved numerical baselines and explicit correction/AI stage outcomes."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from mesoforge.catalog.units import convert
from mesoforge.forecasting.cloud_cover import CLOUD, sky_category, validate_active_cloud_field
from mesoforge.forecasting.visibility import visibility_miles

_QPF = "liquid_equivalent_precipitation_amount_1h"
_POP = "probability_of_precipitation_1h"


def _display_pop(field: dict[str, Any]) -> dict[str, Any]:
    """Display the native probability's percentage without changing its event window."""
    if field["unit"] != "1":
        raise ValueError("Hourly report requires canonical PoP as a fraction")
    value = field["value"]
    if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("Hourly report requires PoP within [0, 1]")
    return {
        "value": None if value is None else value * 100,
        "unit": "percent",
        "interval_start": field["interval_start"],
        "interval_end": field["interval_end"],
        "interval_closure": field["interval_closure"],
        "threshold": deepcopy(field["threshold"]),
    }


def _display_qpf(field: dict[str, Any]) -> dict[str, Any]:
    """Liquid-equivalent depth in inches, retaining exact accumulation bounds."""
    if field["unit"] != "kg/m^2":
        raise ValueError("Hourly report requires canonical QPF in kg/m^2")
    value = field["value"]
    return {
        "value": None if value is None else value / 25.4,
        "unit": "inch",
        "interval_start": field["interval_start"],
        "interval_end": field["interval_end"],
        "interval_closure": "left_open_right_closed",
    }


def _display_temperature(temperature: dict[str, Any]) -> dict[str, Any]:
    """Fahrenheit is a display value; the persisted scientific temperature stays K."""
    if temperature["unit"] != "K":
        raise ValueError("Hourly report requires the forecast's canonical Kelvin temperatures")
    value = temperature["value"]
    celsius = None if value is None else cast(float, convert(value, "K", "degC"))
    return {"value": None if celsius is None else celsius * 9 / 5 + 32, "unit": "degF"}


def build_hourly_report(
    forecast: dict[str, Any], *, display_timezone: str = "UTC"
) -> dict[str, Any]:
    """Describe a newly issued version without changing or recalculating its baseline.

    The optional timezone controls presentation only. UTC is the default; this
    function does not claim to discover a location's timezone from its coordinate.
    Verification of an earlier issued version never verifies this new version.
    """
    timezone = ZoneInfo(display_timezone)
    stage = forecast.get("learning_stage", {})
    correction = stage.get("overlay", {}).get("correction", {})
    corrected_points = (
        {row["valid_time"]: row for row in correction["point_values"]}
        if correction.get("status") == "applied"
        else {}
    )
    hours = []
    for index, hour in enumerate(forecast["hours"]):
        valid = datetime.fromisoformat(hour["valid_time"])
        if valid.tzinfo is None:
            raise ValueError("Hourly report valid times must include their UTC offset")
        contributors = {}
        for source_list, role in (("sources", "active"), ("shadow_sources", "shadow")):
            for source_index, source in enumerate(hour.get(source_list, [])):
                contributors[source["model"]] = {
                    **deepcopy(source),
                    "role": role,
                    "display_temperature": _display_temperature(source["temperature"]),
                    "provenance_ref": f"#/hours/{index}/{source_list}/{source_index}",
                }
        corrected_point = corrected_points.get(hour["valid_time"])
        raw = deepcopy(
            corrected_point["baseline_temperature"] if corrected_point else hour["temperature"]
        )
        hours.append(
            {
                "horizon_hours": hour["horizon_hours"],
                "valid_time_utc": valid.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "valid_time_local": valid.astimezone(timezone).isoformat(),
                "baseline_ref": f"#/hours/{index}",
                "raw_numerical_temperature": raw,
                "raw_display_temperature": _display_temperature(raw),
                "contributors": contributors,
                "missing_reasons": deepcopy(hour["missing_reasons"]),
                "bias_correction": {
                    "status": "not_implemented",
                    "applied_delta": {"value": 0.0, "unit": "K"},
                    "reason": "Deterministic site-bias correction stage not implemented yet",
                },
                "ai_adjustment": {
                    "action": "not_run",
                    "applied_delta": {"value": 0.0, "unit": "K"},
                    "reason": "AI forecast-desk stage not implemented yet",
                },
                "final_temperature": deepcopy(raw),
                "final_display_temperature": _display_temperature(raw),
                "delivery_status": "not_delivered",
                "verification": {
                    "status": "not_yet_verified",
                    "reason": "This newly issued forecast version has not been verified",
                },
            }
        )
        if corrected_point is not None:
            hours[-1].update(
                bias_correction={
                    "status": "applied" if corrected_point["applied_delta_k"] else "no_op",
                    "applied_delta": {"value": corrected_point["applied_delta_k"], "unit": "K"},
                    "reason": (
                        "Explicit active deterministic temperature policy; no promotion inferred"
                    ),
                    "policy": deepcopy(correction["policy"]),
                    "stage_id": stage["variant_id"],
                    "parent_stage_id": stage["parent_stage_id"],
                },
                final_temperature=deepcopy(hour["temperature"]),
                final_display_temperature=_display_temperature(hour["temperature"]),
            )
        if "surface" in hour:
            hours[-1]["surface"] = deepcopy(hour["surface"])
            hours[-1]["final_surface_fields"] = deepcopy(hour["surface"]["fields"])
            if _QPF in hour["surface"]["fields"]:
                hours[-1]["display_qpf"] = _display_qpf(hour["surface"]["fields"][_QPF])
            if _POP in hour["surface"]["fields"]:
                hours[-1]["display_pop"] = _display_pop(hour["surface"]["fields"][_POP])
    return {
        "latitude": forecast["latitude"],
        "longitude": forecast["longitude"],
        "target_reference_time": forecast["target_reference_time"],
        "data_kind": forecast["data_kind"],
        "notice": forecast["notice"],
        "display_timezone": display_timezone,
        "timezone_note": "Presentation timezone; not inferred from forecast coordinates",
        "baseline_ref": "#/hours",
        "provenance_ref": "#",
        "hours": hours,
    }


def _temperature_text(temperature: dict[str, Any]) -> str:
    value = temperature["value"]
    return "missing" if value is None else f"{value:.1f}"


def render_hourly_report(report: dict[str, Any]) -> str:
    """Render every stored hour, without calculating any bias or AI correction."""
    if any("surface" in hour for hour in report["hours"]):
        return _render_surface_report(report)
    applied = any(hour["bias_correction"]["status"] == "applied" for hour in report["hours"])
    lines = [
        f"Forecast at {report['latitude']}, {report['longitude']}",
        "",
        report["notice"],
        f"Reference: {report['target_reference_time']}. "
        f"Local/display timezone: {report['display_timezone']}. {report['timezone_note']}.",
        (
            "Raw temperatures retain the original numerical baseline. An explicitly active "
            "deterministic temperature policy supplies the displayed bias delta and final "
            "temperature. AI action is not_run, nudge 0; no AI stage ran. Delivery has not run."
            if applied
            else "Temperatures are displayed in °F; original unrounded Kelvin values remain in the "
            "saved numerical baseline. Bias and AI deltas are 0 because neither stage is "
            "implemented. AI action is not_run; reason: "
            "AI forecast-desk stage not implemented yet. "
            "Final equals the raw numerical blend; delivery has not run. "
            "Each newly issued hour is not_yet_verified; "
            "earlier versions' verification is separate."
        ),
        "",
        "| Hour | UTC valid time | Local/display valid time | Raw °F | HRRR °F | GFS °F | "
        "RAP °F | IFS °F | Bias Δ°F | AI action / Δ°F | Final °F | Verification |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    missing = []
    for hour in report["hours"]:
        values = []
        for model in ("HRRR", "GFS", "RAP", "IFS"):
            source = hour["contributors"].get(model)
            values.append(
                "missing" if source is None else _temperature_text(source["display_temperature"])
            )
            if source is None:
                missing.append(f"Hour {hour['horizon_hours']} {model}: contributor not present")
            elif source["temperature"]["value"] is None:
                missing.append(
                    f"Hour {hour['horizon_hours']} {model}: " + "; ".join(source["missing_reasons"])
                )
        bias = hour["bias_correction"]["applied_delta"]["value"] * 9 / 5
        nudge = hour["ai_adjustment"]["applied_delta"]["value"] * 9 / 5
        cells = [
            str(hour["horizon_hours"]),
            hour["valid_time_utc"],
            hour["valid_time_local"],
            _temperature_text(hour["raw_display_temperature"]),
            *values,
            f"{bias:.1f}",
            f"{hour['ai_adjustment']['action']} / {nudge:.1f}",
            _temperature_text(hour["final_display_temperature"]),
            hour["verification"]["status"],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if missing:
        lines.extend(["", "Missing guidance (no interpolation or weight redistribution):", ""])
        lines.extend(f"- {reason}" for reason in missing)
    return "\n".join(lines) + "\n"


def _surface_value(fields: dict[str, Any], variable: str) -> str:
    field = fields.get(variable)
    if field is None or field["value"] is None:
        return "unavailable"
    value = field["value"]
    if variable == _QPF:
        return f"{_display_qpf(field)['value']:.6g}"
    if variable == _POP:
        return f"{_display_pop(field)['value']:.6g}"
    if field["unit"] == "K":
        return _temperature_text(_display_temperature(field))
    if field["unit"] == "m/s":
        return f"{value * 3600 / 1609.344:.1f}"
    if variable == "wind_from_direction_10m":
        points = (
            "N",
            "NNE",
            "NE",
            "ENE",
            "E",
            "ESE",
            "SE",
            "SSE",
            "S",
            "SSW",
            "SW",
            "WSW",
            "W",
            "WNW",
            "NW",
            "NNW",
        )
        return f"{points[math.floor((value + 11.25) / 22.5) % 16]} ({value:.0f}°)"
    return f"{value:.1f}"


def _interval_cells(fields: dict[str, Any], variable: str) -> list[str]:
    field = fields.get(variable, {})
    return [str(field.get(key) or "unavailable") for key in ("interval_start", "interval_end")]


def _cloud_cells(field: dict[str, Any] | None, valid_time: str) -> tuple[list[str], list[str]]:
    """Present only the approved saved active cloud field, without promoting evidence."""
    if field is None:
        return ["unavailable", "unavailable"], ["Active cloud field was not saved"]
    try:
        validate_active_cloud_field(field, valid_time=valid_time)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return ["unavailable", "unavailable"], [
            *field.get("missing_reasons", []),
            f"Active NBM cloud unavailable: {exc}",
        ]
    percent = field["cloud_percentage"]
    return [f"{percent:.6g}", sky_category(percent).replace("_", " ")], []


def _hour_ranges(hours: list[int]) -> str:
    """Keep repeated native-period gaps readable without hiding which hours lack data."""
    values = sorted(set(hours))
    ranges = []
    index = 0
    while index < len(values):
        start = end = values[index]
        index += 1
        while index < len(values) and values[index] == end + 1:
            end = values[index]
            index += 1
        ranges.append(str(start) if start == end else f"{start}–{end}")
    return ", ".join(ranges)


def _probability_event_text(source: dict[str, Any]) -> str:
    threshold = source["threshold"]
    comparator = {"gt": ">", "ge": "≥", "lt": "<", "le": "≤"}.get(
        threshold["comparison"], threshold["comparison"]
    )
    return f"{comparator} {threshold['value']:g} {threshold['unit']}"


def _probability_support_text(support: Any) -> str:
    if isinstance(support, dict):
        return "; ".join(f"{key}={value}" for key, value in sorted(support.items()))
    return str(support)


def _render_probability_shadows(hours: list[dict[str, Any]]) -> list[str]:
    """Keep each native event separate from the delivered hourly NBM probability."""
    if not any("probability_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native-period probability shadows",
        "",
        "All contributors below have zero active weight. NBM remains the sole active "
        "hourly PoP source. A row appears at its native interval end; six-hour and "
        "24-hour probabilities are not hourly probabilities and are not split or filled. "
        "Threshold, interval and event spatial support must match before comparing sources.",
        "",
        "| End hour | Source / event | Cycle / source lead h | Native interval start UTC | "
        "Native interval end UTC | Interval closure | Threshold | Spatial support | "
        "Probability % | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    missing: dict[str, list[int]] = {}
    excluded: dict[str, list[int]] = {}
    paired = []
    for hour in hours:
        horizon = hour["horizon_hours"]
        guidance = hour.get("surface", {}).get("probability_guidance", {})
        for source in guidance.get("contributors", []):
            if source["value"] is None:
                reasons = source.get("missing_reasons", []) or ["Native probability unavailable"]
                missing.setdefault(f"{source['source_id']}: {'; '.join(reasons)}", []).append(
                    horizon
                )
                continue
            probability = _display_pop(source)["value"]
            cells = [
                str(horizon),
                f"{source['source_id']} / {source['event_id']}",
                f"{source['source_cycle']} / {source['source_lead_hours']}",
                source["interval_start"],
                source["interval_end"],
                source["interval_closure"],
                _probability_event_text(source),
                _probability_support_text(source["spatial_support"]),
                f"{probability:.6g}",
                source["status"],
            ]
            lines.append("| " + " | ".join(cells) + " |")
        for comparison in guidance.get("comparisons", []):
            left, right = comparison["left"], comparison["right"]
            # A missing native endpoint is already described in the availability
            # summary, without manufacturing an interval to compare.
            if left["event_id"] is None or right["event_id"] is None:
                continue
            identity = (
                f"{left['source_id']} / {left['event_id']} minus "
                f"{right['source_id']} / {right['event_id']}"
            )
            if comparison["status"] == "comparable":
                delta = comparison["delta"]
                if comparison["unit"] != "1" or delta is None or not math.isfinite(delta):
                    raise ValueError(
                        "Probability disagreement requires a finite fraction difference"
                    )
                paired.append(f"| {horizon} | {identity} | {delta * 100:+.6g} |")
            else:
                reason = f"{identity}: {comparison['status']}; " + "; ".join(comparison["reasons"])
                excluded.setdefault(reason, []).append(horizon)
    lines.extend(
        [
            "",
            "Matched probability disagreements (percentage points, left minus right; "
            "descriptive differences, not verification scores):",
            "",
        ]
    )
    if paired:
        lines.extend(
            ["| End hour | Compared source / event | Difference pp |", "| --- | --- | --- |"]
        )
        lines.extend(paired)
    else:
        lines.append("No comparable native probability pairs are available.")
    for title, grouped in (
        ("Native probability availability", missing),
        ("Probability comparison exclusions", excluded),
    ):
        if grouped:
            lines.extend(["", title + ":", ""])
            lines.extend(
                f"- Hours {_hour_ranges(values)}: {reason}" for reason, values in grouped.items()
            )
    return lines


def _render_surface_report(report: dict[str, Any]) -> str:
    """Present the unchanged numerical surface baseline and native contributors."""
    columns: tuple[str, ...] = (
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "wind_from_direction_10m",
        "wind_gust_10m",
    )
    surface_columns = columns
    has_qpf = any(_QPF in hour.get("surface", {}).get("fields", {}) for hour in report["hours"])
    has_pop = any(_POP in hour.get("surface", {}).get("fields", {}) for hour in report["hours"])
    has_cloud = any(CLOUD in hour.get("surface", {}).get("fields", {}) for hour in report["hours"])
    if has_qpf:
        columns += (_QPF,)
    if has_pop:
        columns += (_POP,)
    qpf_headers = " QPF in | Accumulation start UTC (exclusive) | End UTC (inclusive) |"
    qpf_separator = " --- | --- | --- |"
    pop_headers = " Native-period PoP % | PoP start UTC (exclusive) | PoP end UTC (inclusive) |"
    applied = any(hour["bias_correction"]["status"] == "applied" for hour in report["hours"])
    lines = [
        f"Surface forecast at {report['latitude']}, {report['longitude']}",
        "",
        f"Reference: {report['target_reference_time']}. "
        f"Display zone: {report['display_timezone']}.",
        "Temperature remains HRRR/GFS 70/30. Dew point and coupled vector wind/gust "
        "use the retained Phase 2 rows: HRRR/GFS 70/30 at hours 1–18 and 60/40 at "
        "19–36 when both are eligible; approved single-model fallbacks are labeled. "
        "RAP/IFS are zero-weight shadows. "
        "RH is derived over liquid water from temperature/dew point.",
        (
            "The surface table shows the coherent final fields after the explicitly active "
            "temperature correction. The structured hourly report retains raw temperature, "
            "policy/stage identity and each applied delta separately. AI action: not_run, "
            "nudge 0. Delivery has not run; previous versions' verification is separate."
            if applied
            else "Bias correction: not_implemented, applied delta 0. AI action: not_run, nudge 0; "
            "AI forecast-desk stage not implemented yet. Final surface fields equal the "
            "numerical baseline. Delivery has not run. New issued hours are not_yet_verified; "
            "verification of previous versions is separate."
        ),
        "Active cloud cover uses the approved temporary native NBM total-cloud baseline when "
        "the saved field is eligible. Missing or invalid NBM guidance has no shadow fallback. "
        "Separate native cloud evidence is shown below when prepared. "
        "IFS instantaneous gust: unavailable because its published gust is an interval maximum. "
        "Native three-hourly IFS gaps are preserved. Stored Kelvin, m/s, degree and percent "
        "values are unrounded, with source cycles, leads, raw hashes, rules and exclusion reasons.",
        "",
        "| Hour | UTC valid time | Local/display valid time | T °F | Td °F | RH % | "
        "Wind mph | From | Gust mph |"
        + (qpf_headers if has_qpf else "")
        + (pop_headers if has_pop else "")
        + (" NBM cloud % | Sky category |" if has_cloud else "")
        + " Missing / exclusions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
        + (qpf_separator if has_qpf else "")
        + (qpf_separator if has_pop else "")
        + (" --- | --- |" if has_cloud else "")
        + " --- |",
    ]
    if has_qpf:
        lines.insert(
            -3,
            "QPF is liquid-equivalent accumulation over (start, end], displayed in inches; "
            "original unrounded kg/m² values and exact intervals remain stored. It is not "
            "instantaneous precipitation, precipitation probability or precipitation type. "
            "QPF uses approved Phase 2 HRRR/GFS 70/30 weights at hours 1–18 and 60/40 at "
            "19–36 when both are eligible, with explicit approved fallbacks. Small positive "
            "amounts remain positive; zero and unavailable are distinct.",
        )
    if has_pop:
        lines.insert(
            -3,
            "NBM PoP is the probability of liquid-equivalent accumulation strictly greater "
            "than 0.254 kg/m² (0.01 inch) over the displayed native (start, end] period. "
            "The current product has one-hour periods. NBM is the sole active probability source "
            "with weight 1.0; PoP is not derived from deterministic QPF and does not identify "
            "precipitation type. Missing native periods stay unavailable; no new probability "
            "windows are synthesized. Percent display retains the original fraction in storage.",
        )
    reasons: dict[str, list[int]] = {}
    for hour in report["hours"]:
        surface = hour.get("surface", {})
        fields = surface.get("fields", {})
        issues = [f"{v}: unavailable" for v in columns if fields.get(v, {}).get("value") is None]
        for variable in columns:
            field = fields.get(variable, {})
            if field.get("value") is not None:
                weights = field.get("weights", {})
                if len(weights) == 1 and variable != _POP:
                    issues.append(f"{variable}: {next(iter(weights))}-only fallback")
                elif field.get("missing_reasons"):
                    issues.append(f"{variable}: contributor exclusions (see below)")
            for reason in field.get("missing_reasons", []):
                reasons.setdefault(f"{variable}: {reason}", []).append(hour["horizon_hours"])
        cloud_cells, cloud_reasons = _cloud_cells(fields.get(CLOUD), hour["valid_time_utc"])
        if has_cloud:
            for reason in cloud_reasons:
                reasons.setdefault(f"{CLOUD}: {reason}", []).append(hour["horizon_hours"])
        cells = [
            str(hour["horizon_hours"]),
            hour["valid_time_utc"],
            hour["valid_time_local"],
            *(_surface_value(fields, v) for v in surface_columns),
            *([_surface_value(fields, _QPF), *_interval_cells(fields, _QPF)] if has_qpf else []),
            *([_surface_value(fields, _POP), *_interval_cells(fields, _POP)] if has_pop else []),
            *(cloud_cells if has_cloud else []),
            ("; ".join(issues) if issues else "none")
            + ("; cloud unavailable" if cloud_reasons else ""),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    models = sorted(
        {model for h in report["hours"] for model in h.get("surface", {}).get("contributors", {})}
    )
    for model in models:
        native = next(
            h["surface"]["contributors"][model]
            for h in report["hours"]
            if model in h.get("surface", {}).get("contributors", {})
        )
        probability_only = set(native.get("fields", {})) == {_POP}
        native_columns = () if probability_only else surface_columns
        native_qpf = has_qpf and not probability_only
        native_pop = probability_only
        native_cycle = (
            native["fields"][_POP].get("source_cycle", "unavailable")
            if probability_only
            else native["cycle"]
        )
        native_header = "| Hour | Source lead |"
        native_separator = "| --- | --- |"
        if not probability_only:
            native_header += " T °F | Td °F | RH % | Wind mph | From | Gust mph |"
            native_separator += " --- | --- | --- | --- | --- | --- |"
        lines.extend(
            [
                "",
                f"### {model} native contributor ({native['role']}; cycle {native_cycle})",
                "",
                native_header
                + (qpf_headers if native_qpf else "")
                + (pop_headers if native_pop else ""),
                native_separator
                + (qpf_separator if native_qpf else "")
                + (qpf_separator if native_pop else ""),
            ]
        )
        for hour in report["hours"]:
            source = hour.get("surface", {}).get("contributors", {}).get(model, {})
            fields = source.get("fields", {})
            cells = [
                str(hour["horizon_hours"]),
                str(
                    fields.get(_POP, {}).get("source_lead_hours", "unavailable")
                    if probability_only
                    else source.get("source_lead_hours") or "unavailable"
                ),
                *(_surface_value(fields, v) for v in native_columns),
                *(
                    [_surface_value(fields, _QPF), *_interval_cells(fields, _QPF)]
                    if native_qpf
                    else []
                ),
                *(
                    [_surface_value(fields, _POP), *_interval_cells(fields, _POP)]
                    if native_pop
                    else []
                ),
            ]
            lines.append("| " + " | ".join(cells) + " |")
            for variable, field in fields.items():
                for reason in field.get("missing_reasons", []):
                    reasons.setdefault(f"{model} {variable}: {reason}", []).append(
                        hour["horizon_hours"]
                    )
    if reasons:
        lines.extend(["", "Explicit missingness and scientific exclusions:", ""])
        for reason, hours in reasons.items():
            lines.append(f"- Hours {', '.join(str(h) for h in sorted(set(hours)))}: {reason}")
    lines.extend(_render_probability_shadows(report["hours"]))
    lines.extend(_render_precipitation_type(report["hours"]))
    lines.extend(_render_snowfall(report["hours"]))
    lines.extend(_render_snowfall_amount(report["hours"]))
    lines.extend(_render_cloud_guidance(report["hours"]))
    lines.extend(_render_visibility_guidance(report["hours"]))
    lines.extend(_render_thunder_guidance(report["hours"]))
    lines.extend(_render_ice_guidance(report["hours"]))
    return "\n".join(lines) + "\n"


def _render_ice_guidance(hours: list[dict[str, Any]]) -> list[str]:
    """Keep accreted-ice mass and freezing-rain liquid distinct in their native intervals."""
    if not any("ice_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native ice and freezing-rain-liquid evidence",
        "",
        "Both active fields remain unavailable: no ice-accretion or freezing-rain-liquid "
        "blend policy is approved. All native contributors have zero active weight. FICEAC "
        "is elevated flat-surface ice-accretion mass equivalent in kg/m², not geometric or "
        "radial ice thickness, ground ice or road icing. "
        "FRZR is freezing-rain liquid equivalent in kg/m², not the amount of ice that accretes. "
        "No 1:1 liquid-to-ice conversion, assumed ice density, or new ice-accretion algorithm "
        "is applied. These quantities remain separate from total QPF, precipitation type and "
        "snowfall. Native intervals and source accumulation parents remain traceable; "
        "a six-hour amount is not relabeled as an hourly amount.",
        "",
        "| Hour | Source / product | Quantity | Native interval kg/m² | Native value / unit | "
        "Cycle / source lead h | Native interval start UTC | Native interval end UTC | "
        "Interval closure | Status / reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    exclusions: dict[str, list[int]] = {}
    differences: list[str] = []
    for hour in hours:
        guidance = hour.get("surface", {}).get("ice_guidance", {})
        for source in guidance.get("contributors", []):
            value = source["value"]
            if source["unit"] != "kg/m^2" or (
                value is not None and (not math.isfinite(value) or value < 0)
            ):
                raise ValueError(
                    "Ice report requires finite nonnegative mass equivalents in kg/m^2"
                )
            native = source.get("native_value")
            reason = "; ".join(source.get("missing_reasons", []))
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        f"{source['source_id']} / {source.get('product', 'unavailable')}",
                        source["quantity_kind"],
                        "unavailable" if value is None else f"{value:.6g}",
                        "unavailable"
                        if native is None
                        else f"{native:.6g} {source['native_unit']}",
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        source.get("interval_start") or "unavailable",
                        source.get("interval_end") or "unavailable",
                        source.get("interval_closure") or "unavailable",
                        source["status"] + (f"; {reason}" if reason else ""),
                    ]
                )
                + " |"
            )
        available = {
            source["source_id"]
            for source in guidance.get("contributors", [])
            if source["value"] is not None
        }
        for comparison in guidance.get("comparisons", []):
            if comparison["status"] == "comparable":
                difference = comparison["difference_left_minus_right"]
                if (
                    comparison["unit"] != "kg/m^2"
                    or difference is None
                    or not math.isfinite(difference)
                ):
                    raise ValueError("Ice comparison requires a finite mass-equivalent difference")
                differences.append(
                    f"| {hour['horizon_hours']} | {' minus '.join(comparison['source_ids'])} | "
                    f"{difference:+.6g} |"
                )
            if comparison["status"] == "incompatible" and set(comparison["source_ids"]).issubset(
                available
            ):
                identity = " versus ".join(comparison["source_ids"])
                reason = identity + ": " + "; ".join(comparison["missing_reasons"])
                exclusions.setdefault(reason, []).append(hour["horizon_hours"])
    if differences:
        lines.extend(
            [
                "",
                "Matching native quantity/interval differences are descriptive, not skill scores:",
                "",
                "| Hour | Compared sources | Difference kg/m² |",
                "| --- | --- | --- |",
                *differences,
            ]
        )
    if exclusions:
        lines.extend(["", "Native quantity/interval comparison exclusions:", ""])
        lines.extend(
            f"- Hours {_hour_ranges(indices)}: {reason}" for reason, indices in exclusions.items()
        )
    return lines


def _thunder_percent(row: dict[str, Any]) -> str:
    value = row["value"]
    if row["unit"] != "1" or (
        value is not None and (not math.isfinite(value) or not 0 <= value <= 1)
    ):
        raise ValueError("Thunder report requires finite probabilities within [0, 1]")
    percent = None if value is None else value * 100
    if row.get("display_percent") != percent:
        raise ValueError("Thunder display must reproduce the native probability")
    return "unavailable" if percent is None else f"{percent:.6g}"


def _render_thunder_guidance(hours: list[dict[str, Any]]) -> list[str]:
    """Preserve each native thunder event rather than inventing hourly probabilities."""
    if not any("thunder_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native thunder probability",
        "",
        "Temporary baseline: the matching native one-hour NBM thunder probability, unchanged. "
        "Other native periods remain separate zero-weight evidence. Three-hour and six-hour "
        "events are not converted to hourly probabilities; missing events stay unavailable. "
        "Source-cell event definitions and spatial support are retained, including any "
        "unresolved radius or lightning threshold. This is not an exact-point lightning "
        "probability, severe-weather probability, precipitation probability, or a complete "
        "weather-condition string. No probability is derived from CAPE, QPF or cloud cover.",
        "",
        "| Hour | Active thunder % | Source | Native interval start UTC | "
        "Native interval end UTC | Interval closure | Status / reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    native_rows: list[str] = []
    excluded: dict[str, list[int]] = {}
    for hour in hours:
        guidance = hour.get("surface", {}).get("thunder_guidance")
        if guidance is None:
            continue
        field = guidance["field"]
        reason = "; ".join(field.get("missing_reasons", []))
        lines.append(
            "| "
            + " | ".join(
                [
                    str(hour["horizon_hours"]),
                    _thunder_percent(field),
                    field.get("source_id") or "unavailable",
                    field.get("interval_start") or "unavailable",
                    field.get("interval_end") or "unavailable",
                    field.get("interval_closure") or "unavailable",
                    field["status"] + (f"; {reason}" if reason else ""),
                ]
            )
            + " |"
        )
        for source in guidance["contributors"]:
            reason = "; ".join(source.get("missing_reasons", []))
            native_value = source.get("native_value")
            native_rows.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        f"{source['source_id']} / {source.get('product', 'unavailable')}",
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        source.get("interval_start") or "unavailable",
                        source.get("interval_end") or "unavailable",
                        source.get("interval_closure") or "unavailable",
                        _thunder_percent(source),
                        "unavailable"
                        if native_value is None
                        else f"{native_value:.6g} {source['native_unit']}",
                        str(source["active_weight"]),
                        _probability_support_text(source.get("event_definition", "unavailable")),
                        _probability_support_text(source.get("spatial_support", "unavailable")),
                        source["status"] + (f"; {reason}" if reason else ""),
                    ]
                )
                + " |"
            )
        available = {
            source["source_id"]
            for source in guidance["contributors"]
            if source["value"] is not None
        }
        for comparison in guidance["comparisons"]:
            if comparison["status"] == "incompatible" and set(comparison["source_ids"]).issubset(
                available
            ):
                identity = " versus ".join(comparison["source_ids"])
                reason = identity + ": " + "; ".join(comparison["missing_reasons"])
                excluded.setdefault(reason, []).append(hour["horizon_hours"])
    lines.extend(
        [
            "",
            "Native thunder contributors and event semantics:",
            "",
            "| Hour | Source / product | Cycle / source lead h | Native interval start UTC | "
            "Native interval end UTC | Interval closure | Thunder % | Native value / unit | "
            "Active weight | Event definition | Spatial support | Status / reason |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            *native_rows,
        ]
    )
    if excluded:
        lines.extend(["", "Native event comparison exclusions:", ""])
        lines.extend(
            f"- Hours {_hour_ranges(indices)}: {reason}" for reason, indices in excluded.items()
        )
    return lines


def _render_visibility_guidance(hours: list[dict[str, Any]]) -> list[str]:
    """Display native horizontal visibility without inferring a weather phenomenon."""
    if not any("visibility_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native visibility evidence",
        "",
        "All visibility contributors have zero active weight. The active visibility baseline "
        "is unavailable because no retained blend rule is approved. Values describe native "
        "model horizontal visibility at the surface. Original unrounded metres, native units "
        "and source provenance remain stored; statute miles are a display conversion. "
        "This evidence does not identify fog, precipitation type or intensity, or a complete "
        "weather-condition string. Missing native times are not filled.",
        "",
        "| Hour | Source / product | Cycle / source lead h | UTC valid time | Visibility m | "
        "Visibility statute mi | Native value / unit | Status / reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    differences: list[str] = []
    for hour in hours:
        guidance = hour.get("surface", {}).get("visibility_guidance", {})
        for source in guidance.get("contributors", []):
            value = source["value"]
            if source["unit"] != "m" or (
                value is not None and (not math.isfinite(value) or value < 0)
            ):
                raise ValueError("Visibility report requires finite nonnegative metres")
            miles = None if value is None else visibility_miles(value)
            if source.get("display_miles") != miles:
                raise ValueError("Visibility display must reproduce the native metre value")
            native = source.get("native_value")
            native_text = (
                "unavailable" if native is None else f"{native:.6g} {source['native_unit']}"
            )
            reasons = "; ".join(source.get("missing_reasons", []))
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        f"{source['model']} / {source.get('product', 'unavailable')}",
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        source["valid_time"],
                        "unavailable" if value is None else f"{value:.6g}",
                        "unavailable" if miles is None else f"{miles:.6g}",
                        native_text,
                        source["status"] + (f"; {reasons}" if reasons else ""),
                    ]
                )
                + " |"
            )
        for comparison in guidance.get("comparisons", []):
            if comparison["status"] != "comparable":
                continue
            difference = comparison["difference_left_minus_right"]
            if comparison["unit"] != "m" or difference is None or not math.isfinite(difference):
                raise ValueError("Visibility disagreement requires finite metre differences")
            differences.append(
                f"| {hour['horizon_hours']} | {' minus '.join(comparison['models'])} | "
                f"{difference:+.6g} |"
            )
    lines.extend(
        [
            "",
            "Same-valid-time visibility disagreements are descriptive differences, not skill "
            "scores; native model diagnostics and spatial resolutions remain distinct.",
            "",
        ]
    )
    lines.extend(
        ["| Hour | Compared models | Difference m |", "| --- | --- | --- |", *differences]
        if differences
        else ["No compatible native visibility pairs are available."]
    )
    return lines


def _render_cloud_guidance(hours: list[dict[str, Any]]) -> list[str]:
    """Keep each native contributor distinct from the validated active NBM field."""
    if not any("cloud_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native cloud-cover evidence",
        "",
        "The approved temporary active baseline uses only eligible saved NBM total-cloud "
        "guidance. Other models retain zero active weight; they cannot replace missing NBM. "
        "Historical evidence without an approved saved active field remains evidence only. "
        "Each percentage represents "
        "native total cloud cover, not a sum or substitution of cloud layers. Native intervals "
        "and missing times are preserved. Categories describe each unrounded native percentage; "
        "they are not a complete weather-condition string or an observed opaque-sky amount.",
        "",
        "| Hour | Source / product | Cycle / source lead h | UTC valid time | Definition / "
        "vertical extent | Cloud % | Sky category | Native value / unit | Status / reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    differences: list[str] = []
    for hour in hours:
        guidance = hour.get("surface", {}).get("cloud_guidance", {})
        for source in guidance.get("contributors", []):
            value = source["value"]
            if source["unit"] != "percent" or (
                value is not None and (not math.isfinite(value) or not 0 <= value <= 100)
            ):
                raise ValueError("Cloud report requires finite percentages within [0, 100]")
            native = source.get("native_value")
            native_text = (
                "unavailable" if native is None else f"{native:.6g} {source['native_unit']}"
            )
            reasons = "; ".join(source.get("missing_reasons", []))
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        f"{source['model']} / {source.get('product', 'unavailable')}",
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        source["valid_time"],
                        f"{source['cloud_definition']} / {source['vertical_extent']}",
                        "unavailable" if value is None else f"{value:.6g}",
                        (source.get("sky_category") or "unavailable").replace("_", " "),
                        native_text,
                        source["status"] + (f"; {reasons}" if reasons else ""),
                    ]
                )
                + " |"
            )
        for comparison in guidance.get("comparisons", []):
            if comparison["status"] != "comparable":
                continue
            difference = comparison["difference_left_minus_right"]
            if (
                comparison["unit"] != "percentage_point"
                or difference is None
                or not math.isfinite(difference)
            ):
                raise ValueError("Cloud disagreement requires finite percentage-point differences")
            differences.append(
                f"| {hour['horizon_hours']} | {' minus '.join(comparison['models'])} | "
                f"{difference:+.6g} |"
            )
    lines.extend(
        [
            "",
            "Same-valid-time cloud disagreements are descriptive percentage-point differences, "
            "not skill scores; model cloud parameterizations and native resolution differ.",
            "",
        ]
    )
    lines.extend(
        ["| Hour | Compared models | Difference pp |", "| --- | --- | --- |", *differences]
        if differences
        else ["No compatible native cloud pairs are available."]
    )
    return lines


def _render_precipitation_type(hours: list[dict[str, Any]]) -> list[str]:
    if not any("precipitation_type_guidance" in h.get("surface", {}) for h in hours):
        return []
    lines = [
        "",
        "### Native precipitation-type evidence",
        "",
        "Temporary baseline: HRRR/GFS must agree on complete instantaneous native flags. "
        "Disagreement is ambiguous; multiple asserted types remain mixed. "
        "Zero flags mean no classified type, not a dry-weather assertion. "
        "RAP/IFS/NBM evidence stays separate; NBM percentages are conditional on precipitation, "
        "not PoP and not categorical votes. Native categories use nearest-cell extraction. "
        "No type is inferred from temperature, QPF or PoP. No snowfall/ice amount is implied.",
        "",
        "| Hour | UTC valid time | Interim type | Active supported types | Disagreement | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for hour in hours:
        field = hour.get("surface", {}).get("precipitation_type_guidance", {}).get("field", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(hour["horizon_hours"]),
                    hour["valid_time_utc"],
                    str(field.get("value", "unavailable")),
                    ", ".join(field.get("supported_types", [])) or "none classified",
                    "yes; see source evidence"
                    if field.get("contributor_disagreement")
                    else "none among available classifications",
                    "; ".join(field.get("missing_reasons", [])) or "HRRR/GFS agree; temporary rule",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| Hour | Source | Status / native types | Native values | Cycle / lead | Reasons |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for hour in hours:
        for source in (
            hour.get("surface", {}).get("precipitation_type_guidance", {}).get("contributors", [])
        ):
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        f"{source['model']} ({source['role']})",
                        source["status"]
                        + ": "
                        + (", ".join(source["supported_types"]) or "none classified"),
                        json.dumps(source["native_values"], sort_keys=True)
                        + " "
                        + source.get("native_unit", ""),
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        "; ".join(source["missing_reasons"]),
                    ]
                )
                + " |"
            )
    return lines


def _render_snowfall(hours: list[dict[str, Any]]) -> list[str]:
    if not any("snowfall_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native snowfall water equivalent evidence",
        "",
        "The active snowfall-water-equivalent baseline is unavailable: no approved blend "
        "rule or weights exist. Native contributors have zero active weight. Amounts below "
        "are the liquid-water equivalent of snowfall, not snowfall depth, snowpack depth "
        "or ice accretion. Each row retains its actual accumulation interval (start, end]; "
        "a multi-hour amount is not an hourly amount. No ratio, surface-temperature split "
        "or conversion from precipitation type is applied.",
        "",
        "| Hour | Source | SWE kg/m² | Water equivalent in | UTC start (exclusive) | "
        "UTC end (inclusive) | Cycle / lead | Status / reasons |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for hour in hours:
        guidance = hour.get("surface", {}).get("snowfall_guidance", {})
        field = guidance.get("field", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(hour["horizon_hours"]),
                    "Active baseline",
                    "unavailable",
                    "unavailable",
                    "not applied",
                    "not applied",
                    "none",
                    field.get("status", "unavailable")
                    + ": "
                    + "; ".join(field.get("missing_reasons", [])),
                ]
            )
            + " |"
        )
        for source in guidance.get("contributors", []):
            if source.get("unit") != "kg/m^2":
                raise ValueError("Snowfall report requires water equivalent in kg/m^2")
            value = source.get("value")
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Snowfall report requires nonnegative finite water equivalent")
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        f"{source['model']} ({source['role']})",
                        "unavailable" if value is None else f"{value:.6g}",
                        "unavailable" if value is None else f"{value / 25.4:.6g}",
                        source.get("interval_start") or "unavailable",
                        source.get("interval_end") or "unavailable",
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        source.get("status", "unavailable")
                        + (": " if source.get("missing_reasons") else "")
                        + "; ".join(source.get("missing_reasons", [])),
                    ]
                )
                + " |"
            )
    return lines


def _render_snowfall_amount(hours: list[dict[str, Any]]) -> list[str]:
    if not any("snowfall_amount_guidance" in hour.get("surface", {}) for hour in hours):
        return []
    lines = [
        "",
        "### Native and derived snowfall amounts",
        "",
        "Snowfall amount is newly accumulated snow over the stated interval, separate from "
        "SWE and from snow depth already on the ground. The active snowfall-amount baseline "
        "is unavailable because no blend rule or weights are approved. Native model amounts "
        "and the derived Kuchera candidate remain separate zero-weight evidence. Native SLR "
        "is supporting guidance, not a new amount or an active weight. Native accumulation "
        "periods are not split into invented hourly amounts. No snowpack or ice accretion "
        "forecast is delivered. The shown Kuchera ratio is diagnostic; native-cell ratios "
        "multiply native-cell SWE before spatial extraction, not the final point SWE.",
        "",
        "| Hour | Evidence | Source | Snowfall amount in | Diagnostic SLR | "
        "UTC start (exclusive) | UTC end (inclusive) | Cycle / lead | Status / reasons |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    ratios = []
    for hour in hours:
        guidance = hour.get("surface", {}).get("snowfall_amount_guidance", {})
        for group, label in (
            ("native_contributors", "Native amount"),
            ("derived_contributors", "Kuchera derived amount"),
        ):
            for source in guidance.get(group, []):
                value = source.get("value")
                if (
                    source.get("unit") != "m"
                    or value is not None
                    and (not math.isfinite(value) or value < 0)
                ):
                    raise ValueError("Snowfall amount report requires nonnegative metres of snow")
                ratio = source.get("diagnostic_ratio")
                if ratio is not None and not math.isfinite(ratio):
                    raise ValueError("Snowfall amount report requires a finite diagnostic ratio")
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(hour["horizon_hours"]),
                            label,
                            source["model"],
                            "unavailable" if value is None else f"{value / 0.0254:.6g}",
                            "not applicable"
                            if ratio is None
                            else f"{ratio:.6g}:1"
                            + (" (inapplicable to positive SWE)" if ratio <= 0 else ""),
                            source.get("interval_start") or "unavailable",
                            source.get("interval_end") or "unavailable",
                            f"{source.get('source_cycle', 'unavailable')} / "
                            f"{source.get('source_lead_hours', 'unavailable')}",
                            source.get("status", "unavailable")
                            + (": " if source.get("missing_reasons") else "")
                            + "; ".join(source.get("missing_reasons", [])),
                        ]
                    )
                    + " |"
                )
        for source in guidance.get("native_slr", []):
            value = source.get("value")
            if (
                source.get("unit") != "1"
                or value is not None
                and (not math.isfinite(value) or value < 0)
            ):
                raise ValueError("Snowfall ratio report requires a nonnegative dimensionless ratio")
            ratios.append(
                "| "
                + " | ".join(
                    [
                        str(hour["horizon_hours"]),
                        source["model"],
                        "unavailable" if value is None else f"{value:.6g}:1",
                        source.get("valid_time") or hour["valid_time_utc"],
                        f"{source.get('source_cycle', 'unavailable')} / "
                        f"{source.get('source_lead_hours', 'unavailable')}",
                        source.get("status", "unavailable")
                        + (": " if source.get("missing_reasons") else "")
                        + "; ".join(source.get("missing_reasons", [])),
                    ]
                )
                + " |"
            )
    if ratios:
        lines.extend(
            [
                "",
                "Native snow-to-liquid ratio (instantaneous supporting guidance):",
                "",
                "| Hour | Source | Native SLR | UTC valid time | Cycle / lead | Status / reasons |",
                "| --- | --- | --- | --- | --- | --- |",
                *ratios,
            ]
        )
    return lines
