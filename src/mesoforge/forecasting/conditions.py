"""Read-only, versioned descriptions of an already issued numerical grid (RFC 6.7).

No meteorological values are recalculated here. Native evidence cannot stand in
for an active field, and an instantaneous state never becomes an interval event.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from mesoforge.common.errors import IntegrityError
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.cloud_cover import CLOUD, sky_category, validate_active_cloud_field
from mesoforge.forecasting.condition_wording import WORDING_POLICY, build_wording
from mesoforge.forecasting.thunder import ACTIVE_POLICY, validate_thunder_event

RULESET_ID = "saved-active-fields-condition-preview.v3"
SCHEMA_VERSION = "mesoforge.weather-condition-preview.v2"
TEMPLATE_VERSION = "compositional-conditions-text.v1"
# Which saved cells a preview returns; the exact forecast point is always described.
SCOPES = ("point", "editable", "grid")
_CELL_SELECTION = {
    "point": "none_center_point_only",
    "editable": "inside_editable_domain",
    "grid": "all_cells",
}
_SURFACE_POLICY = "phase2-scalar-vector-fallback.v1"
_QPF_POLICY = "phase2-qpf-fallback.v1"
_RH_POLICY = "bolton-1980-relative-humidity-liquid-water.v1"
_TYPE_POLICY = "temporary-hrrr-gfs-native-type-agreement.v1"

# Bounds are the retained scientific contracts, not weather-word thresholds.
_FIELDS = {
    "sky": (CLOUD, "1", 0.0, 1.0),
    "temperature": ("air_temperature_2m", "K", 150.0, 340.0),
    "dew_point": ("dew_point_temperature_2m", "K", 150.0, 340.0),
    "relative_humidity": ("relative_humidity_2m", "%", 0.0, 100.0),
    "wind_u": ("eastward_wind_10m", "m/s", -100.0, 100.0),
    "wind_v": ("northward_wind_10m", "m/s", -100.0, 100.0),
    "wind_speed": ("wind_speed_10m", "m/s", 0.0, None),
    "wind_direction": ("wind_from_direction_10m", "degree", 0.0, 360.0),
    "wind_gust": ("wind_gust_10m", "m/s", 0.0, 100.0),
    "qpf": ("liquid_equivalent_precipitation_amount_1h", "kg/m^2", 0.0, None),
    "pop": ("probability_of_precipitation_1h", "1", 0.0, 1.0),
    "precipitation_type": ("precipitation_type", "category", None, None),
    "thunder": ("probability_of_thunder_1h", "1", 0.0, 1.0),
}
_EXCLUDED = {
    "visibility": ("visibility", "visibility_guidance", "active_visibility_policy_missing"),
    "swe": ("snowfall_water_equivalent_amount", "snowfall_guidance", "active_swe_policy_missing"),
    "snowfall": ("snowfall_amount", "snowfall_amount_guidance", "active_snowfall_policy_missing"),
    "kuchera_snowfall": (None, "snowfall_amount_guidance", "derived_snowfall_is_evidence_only"),
    "native_slr": (None, "snowfall_amount_guidance", "native_slr_is_evidence_only"),
    "freezing_rain_liquid": (
        "freezing_rain_liquid_equivalent_amount",
        "ice_guidance",
        "active_ice_policy_missing",
    ),
    "flat_ice": ("flat_ice_accretion_mass_equivalent", "ice_guidance", "active_ice_policy_missing"),
}
_METADATA = (
    "weights",
    "row_id",
    "row_sha256",
    "derived_from",
    "saturation_reference",
    "threshold",
    "source_event",
    "event_definition",
    "spatial_support",
    "spatial_extraction",
    "source_cycle",
    "source_lead_hours",
    "model",
    "source_id",
    "provider",
    "product",
    "duration_hours",
    "probability_method",
    "supported_types",
    "contributor_disagreement",
    "disagreement_evidence",
    "final_gust_epsilon_floor_applied",
)


class ConditionsPreviewUnavailableError(ValueError):
    """A saved artifact lacks the supported grid needed for this preview."""


def _time(value: Any) -> datetime:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Saved times must include a timezone")
    return instant


def _unavailable(reason: str, *, refs: list[str] | None = None) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "value": None,
        "unit": None,
        "valid_time": None,
        "temporal_semantics": None,
        "interval": None,
        "source_policy": None,
        "rule_id": RULESET_ID,
        "reasons": [reason],
        "evidence_refs": refs or [],
    }


def _reject(component: dict[str, Any], reason: str) -> None:
    component.update(state="unavailable", value=None)
    component["reasons"].append(reason)


def _weights_match(field: dict[str, Any], horizon: int) -> bool:
    expected = {"HRRR": 0.7, "GFS": 0.3} if horizon <= 18 else {"HRRR": 0.6, "GFS": 0.4}
    return field.get("weights") in (expected, {"HRRR": 1.0}, {"GFS": 1.0})


def _policy_allowed(name: str, field: dict[str, Any], horizon: int) -> bool:
    if field.get("role") in ("shadow", "evidence_only") or field.get("active_weight") == 0:
        return False
    policy = field.get("policy")
    if name == "temperature":
        return policy == "unchanged-temperature-control"
    if name == "relative_humidity":
        return policy == _RH_POLICY and field.get("saturation_reference") == "liquid_water"
    if name == "precipitation_type":
        return (
            isinstance(policy, dict)
            and policy.get("id") == _TYPE_POLICY
            and policy.get("required_sources") == ["HRRR", "GFS"]
        )
    if name == "pop":
        return (
            isinstance(policy, dict)
            and policy.get("schema_version") == "pop-blend-policy.v1"
            and policy.get("sole_contributor") == "NBM"
            and policy.get("weight") == 1.0
            and field.get("model") == "NBM"
            and field.get("weights") == {"NBM": 1.0}
        )
    if name == "thunder":
        definition = field.get("event_definition")
        support = field.get("spatial_support", {})
        return (
            policy == ACTIVE_POLICY
            and field.get("source_id") == "NBM_1H"
            and field.get("model") == "NBM"
            and field.get("provider") == "NOAA"
            and field.get("product") == "NBM CONUS core native 1-hour probability of thunder"
            and field.get("weights") == {"NBM": 1.0}
            and definition
            == {
                "id": "nbm_native_probability_of_thunder",
                "parameter": "TSTM",
                "physical_threshold": None,
                "threshold_status": "provider_defined_not_encoded",
            }
            and isinstance(support, dict)
            and support.get("kind") == "provider_native_probability_grid"
            and support.get("geometry_status") == "not_encoded"
            and support.get("radius_km") is None
        )
    expected_policy = _QPF_POLICY if name == "qpf" else _SURFACE_POLICY
    return policy == expected_policy and _weights_match(field, horizon)


def _validate_time(name: str, field: dict[str, Any], valid: str) -> None:
    instant = _time(valid)
    if _time(field.get("valid_time", valid)) != instant:
        raise ValueError("Field valid time does not match the saved grid hour")
    if name in ("qpf", "pop", "thunder"):
        semantics = {"qpf": "accumulation", "pop": "probability", "thunder": "interval_probability"}
        start, end = _time(field["interval_start"]), _time(field["interval_end"])
        if (
            field.get("temporal_semantics") != semantics[name]
            or field.get("interval_closure") != "left_open_right_closed"
            or end != instant
            or end - start != timedelta(hours=1)
        ):
            raise ValueError("Requires the exact saved hourly interval and native semantics")
        if name == "pop" and field.get("threshold") != {
            "value": 0.254,
            "unit": "kg/m^2",
            "comparison": "gt",
        }:
            raise ValueError("Unsupported PoP threshold/comparator; no event substitution")
        if name == "thunder":
            validate_thunder_event(field)
    elif (
        field.get("temporal_semantics", "instantaneous") != "instantaneous"
        or field.get("interval_start") is not None
        or field.get("interval_end") is not None
    ):
        raise ValueError("Instantaneous field cannot inherit an accumulation/event interval")


def _component(
    name: str, field: dict[str, Any] | None, hour: dict[str, Any], pointer: str
) -> dict[str, Any]:
    if field is None:
        return _unavailable("active_field_not_saved")
    component = {
        "state": "known",
        "value": deepcopy(field.get("value")),
        "unit": field.get("unit"),
        "valid_time": field.get("valid_time", hour["valid_time"]),
        "temporal_semantics": field.get("temporal_semantics", "instantaneous"),
        "interval": {
            "start": field.get("interval_start"),
            "end": field.get("interval_end"),
            "closure": field.get("interval_closure"),
        }
        if field.get("interval_start") is not None or field.get("interval_end") is not None
        else None,
        "source_policy": deepcopy(field.get("policy")),
        "rule_id": RULESET_ID,
        "saved_status": field.get("status"),
        "reasons": list(field.get("missing_reasons", [])),
        "evidence_refs": [pointer],
        **{key: deepcopy(field[key]) for key in _METADATA if key in field},
    }
    if name == "sky":
        component.update(cloud_percentage=None, sky_category=None)
        for key in (
            "sky_category_policy",
            "cloud_definition",
            "vertical_extent",
            "native_parameter",
            "native_vertical_binding",
            "native_unit",
            "native_value",
            "native_step_hours",
            "model_version",
            "provenance",
            "manifest_sha256",
            "prepared_file",
        ):
            if key in field:
                component[key] = deepcopy(field[key])
        try:
            validate_active_cloud_field(field, valid_time=hour["valid_time"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            _reject(component, f"ineligible_saved_active_cloud: {exc}")
            return component
        component.update(
            cloud_percentage=field["cloud_percentage"],
            sky_category=sky_category(field["cloud_percentage"]),
        )
        return component
    if not _policy_allowed(name, field, hour["horizon_hours"]):
        _reject(component, "saved_active_policy_unavailable_or_not_supported")
        return component
    try:
        _validate_time(name, field, hour["valid_time"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        _reject(component, f"incompatible_temporal_or_event_semantics: {exc}")
        return component
    if field.get("unit") != _FIELDS[name][1]:
        _reject(component, "unsupported_saved_unit; no implicit conversion")
        return component
    value = field.get("value")
    if name == "precipitation_type":
        if value in ("unknown", "ambiguous", "unavailable") and field.get("status") == value:
            component["state"] = value
        elif (
            value not in ("rain", "snow", "freezing_rain", "ice_pellets", "mixed")
            or field.get("status") != "interim_baseline"
        ):
            _reject(component, "unsupported_saved_type_state")
        component["reasons"].append("instantaneous_endpoint_type_not_an_interval_occurrence")
        return component
    if value is None or field.get("status") not in ("available", "fallback"):
        _reject(component, "active_field_unavailable")
        return component
    _, _, low, high = _FIELDS[name]
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or (low is not None and value < low)
        or (high is not None and value > high)
        or (name == "wind_direction" and value == 360)
    ):
        _reject(component, "invalid_saved_value; no clipping or recalculation")
    return component


def _describe_hour(
    hour: dict[str, Any],
    pointer: str,
    *,
    cell_available: bool = True,
    cell_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    surface = hour.get("surface", {})
    fields = surface.get("fields", {})
    components = {
        name: _component(name, fields.get(spec[0]), hour, f"{pointer}/surface/fields/{spec[0]}")
        for name, spec in _FIELDS.items()
    }
    direction = components["wind_direction"]
    if (
        direction["state"] == "unavailable"
        and direction["value"] is None
        and direction.get("saved_status") == "unavailable"
        and _policy_allowed(
            "wind_direction", fields.get("wind_from_direction_10m", {}), hour["horizon_hours"]
        )
        and direction.get("unit") == "degree"
        and direction.get("interval") is None
        and direction.get("valid_time") == hour["valid_time"]
        and all(
            components[name]["state"] == "known" and components[name]["value"] == 0
            for name in ("wind_u", "wind_v", "wind_speed")
        )
        and direction.get("temporal_semantics") == "instantaneous"
    ):
        direction.update(state="not_applicable")
        direction["reasons"] = list(fields["wind_from_direction_10m"].get("missing_reasons", []))
        direction["reasons"].append("exactly_calm_saved_wind_has_no_direction")
        direction["evidence_refs"].extend(
            components[name]["evidence_refs"][0] for name in ("wind_u", "wind_v", "wind_speed")
        )
    if components["relative_humidity"]["state"] == "known" and any(
        components[name]["state"] != "known" for name in ("temperature", "dew_point")
    ):
        _reject(
            components["relative_humidity"], "saved_temperature_dew_point_prerequisite_unavailable"
        )
    for name, (field_name, attachment, reason) in _EXCLUDED.items():
        excluded = []
        if field_name in fields:
            excluded.append(f"{pointer}/surface/fields/{field_name}")
        if attachment in surface:
            excluded.append(f"{pointer}/surface/{attachment}")
        components[name] = {**_unavailable(reason), "excluded_evidence": excluded}
    for name, reason in (
        ("fog", "no_approved_fog_cause_rule"),
        ("occurrence", "no_categorical_occurrence_rule"),
        ("intensity", "no_approved_intensity_rule"),
        ("transitions", "no_approved_interval_transition_rule"),
    ):
        components[name] = _unavailable(reason)
    if not cell_available:
        for name in _FIELDS:
            _reject(components[name], "saved_grid_cell_unavailable")
            components[name]["reasons"].extend(cell_reasons)
        if "cloud_percentage" in components["sky"]:
            components["sky"].update(cloud_percentage=None, sky_category=None)
    presentation = build_wording(components)
    components["wind_descriptor"] = deepcopy(presentation["wind"])
    result = {
        "presentation": presentation,
        "horizon_hours": hour["horizon_hours"],
        "valid_time": hour["valid_time"],
        "components": components,
        "assessment": {
            "availability": "partial"
            if any(c["state"] == "known" for c in components.values())
            else "unavailable",
            "confidence": {"value": None, "status": "not_calibrated"},
            "note": (
                "Numeric amount, probability event and endpoint type are separate; "
                "rendering applicability does not replace native evidence or time semantics."
            ),
        },
    }
    result["rendering"] = {
        "template_version": TEMPLATE_VERSION,
        "locale": "en",
        "timezone": "UTC",
        "text": str(presentation["text"]),
    }
    return result


def render_condition_hour(hour: dict[str, Any]) -> str:
    """Compose approved wording from saved, validated active components only."""
    return str(build_wording(hour["components"])["text"])


def build_conditions_preview(saved: dict[str, Any], *, scope: str = "point") -> dict[str, Any]:
    """Describe the exact center and the cells the scope selects from one saved grid.

    The point scope returns no grid cells; editable and grid scopes select cells by
    their saved domain markers. Checksums establish attachment identity, not a
    promise that every field is available or that any weather-word policy has been
    scientifically verified.
    """
    if scope not in SCOPES:
        raise ValueError(f"Unsupported conditions scope {scope!r}; use one of {', '.join(SCOPES)}")
    forecast = saved.get("forecast", {})
    grid = forecast.get("local_grid_baseline")
    if not isinstance(grid, dict) or grid.get("version") != "mesoforge.local-surface-baseline.v2":
        raise ConditionsPreviewUnavailableError(
            "Saved forecast has no supported nested local grid; no grid will be regenerated"
        )
    if saved.get("schema_version") != "issued-forecast.v1" or not saved.get("issued_forecast_id"):
        raise ConditionsPreviewUnavailableError(
            "Preview requires an exact saved issued-forecast.v1 payload"
        )
    grid_digest = str(canonical_json_digest(grid))
    if forecast.get("local_grid", {}).get("sha256") != grid_digest:
        raise IntegrityError("Saved grid checksum does not match its point attachment")
    geometry = grid["geometry"]
    target = geometry["point_target"]
    nx, ny = geometry["dimensions"]["x"], geometry["dimensions"]["y"]
    if type(nx) is not int or type(ny) is not int or nx <= 0 or ny <= 0:
        raise IntegrityError("Saved grid dimensions are invalid")
    expected_indices = {(x, y) for y in range(ny) for x in range(nx)}
    seen: set[tuple[int, int]] = set()
    for cell in grid["cells"]:
        x, y = cell["x_index"], cell["y_index"]
        if (
            type(x) is not int
            or type(y) is not int
            or (x, y) not in expected_indices
            or (x, y) in seen
        ):
            raise IntegrityError("Saved grid cell indices are duplicated or outside its geometry")
        seen.add((x, y))
        if (cell["latitude"], cell["longitude"]) != (
            geometry["latitude"][y][x],
            geometry["longitude"][y][x],
        ):
            raise IntegrityError("Saved grid cell coordinates disagree with its geometry")
        if cell.get("status") not in ("calculated", "unavailable"):
            raise IntegrityError("Saved grid cell has an unsupported availability status")
    if seen != expected_indices:
        raise IntegrityError("Saved grid does not contain its complete declared lattice")
    reference = _time(forecast["target_reference_time"])
    cells = []
    center = None
    editable_cells = 0
    for index, cell in enumerate(grid["cells"]):
        hours = cell["hours"]
        if [hour["horizon_hours"] for hour in hours] != list(range(1, 37)):
            raise ConditionsPreviewUnavailableError(
                "Every saved grid cell must contain hours 1..36"
            )
        if any(
            _time(hour["valid_time"]) != reference + timedelta(hours=hour["horizon_hours"])
            for hour in hours
        ):
            raise IntegrityError("Saved grid hour and target reference times disagree")
        is_center = (cell["x_index"], cell["y_index"]) == (target["x_index"], target["y_index"])
        editable = cell.get("inside_editable_domain") is True
        editable_cells += editable
        selected = scope == "grid" or (scope == "editable" and editable)
        if not (selected or is_center):
            continue
        result = {key: deepcopy(value) for key, value in cell.items() if key != "hours"}
        result["hours"] = [
            _describe_hour(
                hour,
                f"/forecast/local_grid_baseline/cells/{index}/hours/{hour_index}",
                cell_available=cell["status"] == "calculated",
                cell_reasons=tuple(cell.get("missing_reasons", [])),
            )
            for hour_index, hour in enumerate(hours)
        ]
        if selected:
            cells.append(result)
        if is_center:
            if center is not None or cell["hours"] != forecast["hours"]:
                raise IntegrityError("Saved point column differs from the unique grid center")
            if (cell["latitude"], cell["longitude"]) != (
                forecast["latitude"],
                forecast["longitude"],
            ):
                raise IntegrityError("Saved grid center and point coordinates disagree")
            center = result
    if center is None:
        raise ConditionsPreviewUnavailableError("Saved grid has no exact point target")
    return {
        "schema_version": SCHEMA_VERSION,
        "ruleset_id": RULESET_ID,
        "scope": {
            "requested": scope,
            "cell_selection": _CELL_SELECTION[scope],
            "cells_returned": len(cells),
            "grid_cells": len(grid["cells"]),
            "editable_cells": editable_cells,
        },
        "wording_policy": deepcopy(WORDING_POLICY),
        "input": {
            "issued_forecast_id": saved["issued_forecast_id"],
            "issued_at": saved["issued_at"],
            "issued_payload_digest": str(canonical_json_digest(saved)),
            "grid_digest": grid_digest,
            "grid_version": grid["version"],
            "field_stage": "numerical_baseline",
            "target_reference_time": forecast["target_reference_time"],
            "code_identity": deepcopy(saved.get("code_identity")),
            "grid_transformation": deepcopy(grid.get("transformation")),
            "forecast_context_ref": "/forecast/local_grid_baseline/forecast_context",
            "data_kind": forecast.get("data_kind"),
            "notice": forecast.get("notice"),
        },
        "geometry": deepcopy(geometry),
        "cells": cells,
        "center_point": {
            "latitude": center["latitude"],
            "longitude": center["longitude"],
            "x_index": center["x_index"],
            "y_index": center["y_index"],
            "method": "exact_center_node",
            "hours": deepcopy(center["hours"]),
        },
    }
