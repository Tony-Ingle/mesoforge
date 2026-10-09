"""One temperature verification fact from an exact issued hour and retained match."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from mesoforge.common.horizon import horizon_for


def _instant(value: Any) -> datetime | None:
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return instant.astimezone(UTC)


def _temperature(value: dict[str, Any]) -> float | None:
    temperature = value.get("value")
    if (
        value.get("unit") != "K"
        or isinstance(temperature, bool)
        or not isinstance(temperature, (float, int))
        or not math.isfinite(temperature)
    ):
        return None
    return float(temperature)


def forecast_eligibility_reasons(
    forecast: dict[str, Any], issued_at: Any, *, cutoff: datetime
) -> list[str]:
    """Existing forecast-only checks; observation-dependent eligibility is still pending."""
    reasons: list[str] = []
    valid_time = _instant(forecast.get("valid_time"))
    issued_at = _instant(issued_at)
    if valid_time is None or issued_at is None:
        reasons.append("forecast_timestamps_missing_or_not_timezone_aware")
    else:
        if issued_at >= valid_time:
            reasons.append("forecast_not_issued_before_valid_time")
        if valid_time > cutoff:
            reasons.append("forecast_valid_time_after_verification_cutoff")

    if _temperature(forecast.get("temperature", {})) is None:
        reasons.append("forecast_temperature_missing_nonfinite_or_not_kelvin")
    if forecast.get("missing_reasons"):
        reasons.append("forecast_has_explicit_missingness")
    sources = forecast.get("sources", [])
    if not sources:
        reasons.append("forecast_source_cycles_unavailable")
    for source in sources:
        source_cycle = _instant(source.get("cycle"))
        if source_cycle is None:
            reasons.append("source_cycle_missing_or_not_timezone_aware")
        elif issued_at is not None and source_cycle > issued_at:
            reasons.append("source_cycle_after_forecast_issuance")

    return reasons


def evaluate_temperature_verification(
    match: dict[str, Any], *, verification_cutoff: datetime, evaluated_at: datetime
) -> dict[str, Any]:
    """Calculate forecast minus observation only for an eligible retained match.

    The cutoff is a fixed retained-input time, not the wall clock of each retry.
    The wall clock only prevents evaluating inputs or events that are still future.
    No selection, forecast generation, storage, or aggregate scoring happens here.
    """
    cutoff = _instant(verification_cutoff)
    evaluation = _instant(evaluated_at)
    if cutoff is None or evaluation is None:
        raise ValueError("verification_cutoff and evaluated_at must include timezones")

    reasons: list[str] = []
    result: dict[str, Any] = {
        "schema_version": "issued-temperature-verification.v1",
        "status": "ineligible",
        "reasons": reasons,
        "temperature_error": {
            "value": None,
            "unit": "K",
            "definition": "forecast_minus_observation",
        },
        "verification_cutoff": (
            cutoff.isoformat().replace("+00:00", "Z") if match.get("input_provenance") else None
        ),
        "verification_policy": {
            "policy_id": "issued-temperature-verification.v1",
            "field": "air_temperature_2m",
            "error_definition": "forecast_minus_observation",
            "units": "K",
            "matching_rules": match.get("selection_policy"),
            "cutoff": "Fixed availability time of the retained normalized observation artifact.",
            "eligibility_rules": [
                "Forecast issuance strictly precedes forecast valid time and observation time.",
                "Forecast valid time, observation time, provider availability, and ingestion "
                "are at or before the verification cutoff.",
                "Referenced station metadata and normalized observations are available by cutoff.",
                "The verification cutoff is not after the evaluation time.",
                "Saved model source cycles are not after forecast issuance.",
                "The selected observation passes temperature QC and the matching limits.",
                "Both temperatures are finite kelvin values, with no forecast missingness.",
            ],
            "scope": "One station/proxy comparison, not aggregate performance or proven skill.",
            "input_availability_limit": (
                "Only provenance retained in the saved forecast is assessed. Its source cycles "
                "are checked, but acquisition availability/ingestion timestamps are not embedded; "
                "this is not a complete operational input-cutoff audit."
            ),
            "fixture_handling": "Synthetic input labels are retained in the complete match.",
        },
        "match": match,
    }
    if cutoff > evaluation:
        reasons.append("verification_cutoff_after_evaluation")

    forecast = match["forecast"]
    valid_time = _instant(forecast.get("valid_time"))
    issued_at = _instant(match.get("issued_at"))
    forecast_temperature = _temperature(forecast.get("temperature", {}))
    reasons.extend(forecast_eligibility_reasons(forecast, issued_at, cutoff=cutoff))
    context = match.get("forecast_context") or {}
    if "forecast_horizon" in context or "horizon_hours" in forecast:
        try:
            duration = horizon_for(context).duration_hours
        except ValueError:
            duration = 0
        lead = forecast.get("horizon_hours")
        target = _instant(context.get("target_reference_time"))
        if (
            type(lead) is not int
            or not 1 <= lead <= duration
            or target is None
            or valid_time is None
            or (valid_time - target).total_seconds() != lead * 3600
        ):
            reasons.append("forecast_reference_lead_inconsistent")

    selected = match.get("selected")
    if match.get("status") != "matched" or selected is None:
        result["status"] = "unavailable"
        reasons.append(match.get("unavailable_reason") or "No selected temperature observation.")
        result["reasons"] = sorted(set(reasons))
        return result

    observed_temperature = _temperature(selected.get("temperature", {}))
    if observed_temperature is None:
        reasons.append("observation_temperature_missing_nonfinite_or_not_kelvin")
    if selected.get("qc", {}).get("temperature_eligible") is not True:
        reasons.append("observation_temperature_qc_failed")
    observation_time = _instant(selected.get("observation_time"))
    if observation_time is None:
        reasons.append("observation_timestamp_missing_or_not_timezone_aware")
    else:
        if issued_at is not None and observation_time <= issued_at:
            reasons.append("forecast_not_issued_before_observation_time")
        if observation_time > cutoff:
            reasons.append("observation_time_after_verification_cutoff")
        if valid_time is not None and abs((observation_time - valid_time).total_seconds()) > 900:
            reasons.append("observation_outside_15_minute_window")
    distance = selected.get("distance_km")
    if (
        isinstance(distance, bool)
        or not isinstance(distance, (float, int))
        or not math.isfinite(distance)
        or not 0 <= distance <= 50
    ):
        reasons.append("observation_outside_50_km_or_distance_unavailable")

    provenance = selected.get("provenance", {})
    for field in ("provider_available_at", "ingested_at"):
        instant = _instant(provenance.get(field))
        if instant is None:
            reasons.append(f"observation_{field}_missing_or_not_timezone_aware")
        elif instant > cutoff:
            reasons.append(f"observation_{field}_after_verification_cutoff")
    if not provenance.get("revision_digest"):
        reasons.append("observation_revision_unavailable")

    input_provenance = match.get("input_provenance") or {}
    station_manifest = next(
        (
            manifest
            for manifest in input_provenance.get("station_snapshots", [])
            if manifest["artifact_id"] == provenance.get("station_snapshot_artifact_id")
        ),
        None,
    )
    for name, manifest in (
        ("station_snapshot", station_manifest),
        ("normalized_observations", input_provenance.get("observations")),
    ):
        available_at = _instant((manifest or {}).get("availability", {}).get("available_at"))
        if available_at is None:
            reasons.append(f"{name}_availability_missing_or_not_timezone_aware")
        elif available_at > cutoff:
            reasons.append(f"{name}_after_verification_cutoff")

    if not reasons:
        assert forecast_temperature is not None and observed_temperature is not None
        difference = forecast_temperature - observed_temperature
        if not math.isfinite(difference):
            reasons.append("temperature_error_nonfinite")
        else:
            result["status"] = "verified"
            result["temperature_error"]["value"] = difference
    result["reasons"] = sorted(set(reasons))
    return result
