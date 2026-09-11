"""Presentation of an unchanged numerical forecast and explicitly unrun stages."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from mesoforge.catalog.units import convert


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
        raw = deepcopy(hour["temperature"])
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
    lines = [
        f"Forecast at {report['latitude']}, {report['longitude']}",
        "",
        report["notice"],
        f"Reference: {report['target_reference_time']}. "
        f"Local/display timezone: {report['display_timezone']}. {report['timezone_note']}.",
        "Temperatures are displayed in °F; original unrounded Kelvin values remain in the "
        "saved numerical baseline. Bias and AI deltas are 0 because neither stage is "
        "implemented. AI action is not_run; reason: AI forecast-desk stage not implemented yet. "
        "Final equals the raw numerical blend; delivery has not run. "
        "Each newly issued hour is not_yet_verified; earlier versions' verification is separate.",
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
