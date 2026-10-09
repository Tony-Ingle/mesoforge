"""Bounded deterministic editing of MesoForge fields, never native evidence.

Edits are copy-on-write transactions. The caller owns checkpoint selection and
point extraction; this module has no provider, storage, or application access.
Spatial smoothing is one conservative graph-diffusion pass within the selected
editable nodes, not temporal redistribution or an area-integrated mass claim.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from mesoforge.common.qpf_intervals import validate_qpf_intervals
from mesoforge.contracts.serialization import canonical_json_digest
from mesoforge.forecasting.coherence import BASELINE_COHERENCE, DEW_POINT, QPF, RH, TEMPERATURE
from mesoforge.forecasting.field_blend import FIELD_REGISTRY, field_edit_contract
from mesoforge.forecasting.scalar_blend import ConsistencyError, check_dew_point_consistency
from mesoforge.forecasting.surface import SurfaceBlendError, relative_humidity_percent

# Existing coherence outputs rederived from an edited field; nothing else may change.
EDIT_DEPENDENTS = {TEMPERATURE: (TEMPERATURE, DEW_POINT, RH), QPF: (QPF,)}

TOOL_VERSION = "mesoforge.field-edit.v1"


class FieldEditError(ValueError):
    """Rejected edit; the caller's previous valid grid remains unchanged."""


def cell_identity(cell: dict[str, Any]) -> str:
    return f"{cell['x_index']}:{cell['y_index']}"


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FieldEditError("Field edits require finite numerical values")
    return float(value)


def _time(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FieldEditError("Field edits require ISO timestamps") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise FieldEditError("Field edits require timezone-aware timestamps")
    return result.astimezone(UTC)


def _fields(hour: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], hour.get("surface", {}).get("fields", {}))


def grid_values_digest(grid: dict[str, Any]) -> str:
    """Hash numerical/state content, not recipe labels or large native evidence.

    This identifies reversals even when accepted-edit metadata differs. Geometry,
    timing, missingness and categorical values remain part of the identity.
    """
    keys = (
        "value",
        "unit",
        "status",
        "missing_reasons",
        "interval_start",
        "interval_end",
        "interval_closure",
        "temporal_semantics",
        "supported_types",
        "category",
    )
    return str(
        canonical_json_digest(
            {
                "geometry": grid["geometry"],
                "cells": [
                    {
                        "cell": cell_identity(cell),
                        **(
                            {
                                "qpf_intervals": [
                                    {key: row[key] for key in keys if key in row}
                                    for row in cell["qpf_intervals"]
                                ]
                            }
                            if "qpf_intervals" in cell
                            else {}
                        ),
                        "hours": [
                            {
                                "valid_time": hour["valid_time"],
                                "temperature": hour.get("temperature"),
                                "fields": {
                                    name: {key: field[key] for key in keys if key in field}
                                    for name, field in _fields(hour).items()
                                },
                            }
                            for hour in cell["hours"]
                        ],
                    }
                    for cell in grid["cells"]
                ],
            }
        )
    )


def _validate_field(field: str, record: dict[str, Any], hour: dict[str, Any]) -> None:
    if record.get("unit") != FIELD_REGISTRY[field].output_units:
        raise FieldEditError(f"Unsupported {field} units")
    value = record.get("value")
    if value is not None:
        value = _number(value)
        bounds = field_edit_contract(field)
        if (bounds.minimum is not None and value < bounds.minimum) or (
            bounds.maximum is not None and value > bounds.maximum
        ):
            raise FieldEditError(f"{field} value is outside its current field bounds")
    if field == QPF:
        start, end = _time(record.get("interval_start")), _time(record.get("interval_end"))
        if (
            end - start != timedelta(hours=1)
            or end != _time(hour["valid_time"])
            or record.get("interval_closure") != "left_open_right_closed"
            or record.get("temporal_semantics") != "accumulation"
        ):
            raise FieldEditError("QPF requires its exact (start,end] one-hour event")
    elif hour.get("temperature", {}).get("value") != record.get("value"):
        raise FieldEditError("Temperature and surface field representations disagree")


def validate_grid(grid: dict[str, Any]) -> dict[str, Any]:
    """Read-only final validation of current editable fields and saved timing.

    Missing inputs stay missing. No baseline science or native blends rerun here.
    Historical grids without a currently supported field remain inspect-only.
    """
    seen = set()
    timing: list[datetime] | None = None
    count = 0
    for cell in grid["cells"]:
        identity = cell_identity(cell)
        if identity in seen:
            raise FieldEditError("Duplicate local-grid cell identity")
        seen.add(identity)
        times = [_time(row["valid_time"]) for row in cell["hours"]]
        if not times or times != sorted(set(times)) or (timing is not None and times != timing):
            raise FieldEditError("Local-grid valid times are duplicated, unordered or unaligned")
        timing = times
        if "qpf_intervals" in cell:
            try:
                events = validate_qpf_intervals(
                    cell["qpf_intervals"], start=times[0] - timedelta(hours=1), end=times[-1]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise FieldEditError("Invalid canonical QPF interval partition") from exc
            hourly = {
                _time(hour["valid_time"]): _fields(hour).get(QPF, {}) for hour in cell["hours"]
            }
            for event in events:
                start, end = (_time(event[key]) for key in ("interval_start", "interval_end"))
                if end - start == timedelta(hours=1):
                    if event["value"] != hourly[end].get("value"):
                        raise FieldEditError(
                            "Canonical hourly QPF differs from its saved hourly field"
                        )
                elif any(
                    hourly[time].get("value") is not None for time in times if start < time <= end
                ):
                    raise FieldEditError("Coarse QPF cannot be represented as hourly amounts")
        for hour in cell["hours"]:
            fields = _fields(hour)
            for name in (TEMPERATURE, QPF):
                if name in fields:
                    _validate_field(name, fields[name], hour)
            humidity = fields.get(RH, {}).get("value")
            if humidity is not None and not 0 <= _number(humidity) <= 100:
                raise FieldEditError("Relative humidity is outside its current bounds")
            _validate_humidity(fields)
            count += 1
    if not seen:
        raise FieldEditError("Local grid is empty")
    return {"status": "passed", "tool_version": TOOL_VERSION, "cell_hours": count}


def _validate_humidity(fields: dict[str, Any]) -> None:
    """Current T/Td/RH rules on every present triple, using the existing kernels only."""
    temperature = fields.get(TEMPERATURE, {}).get("value")
    dew = fields.get(DEW_POINT, {}).get("value")
    if temperature is None or dew is None:
        return
    try:
        check_dew_point_consistency(temperature_k=_number(temperature), dew_point_k=_number(dew))
    except ConsistencyError as exc:
        raise FieldEditError("Saved dew point exceeds temperature") from exc
    humidity = fields.get(RH, {}).get("value")
    if humidity is None:
        return
    try:
        expected = relative_humidity_percent(temperature_k=temperature, dew_point_k=dew)
    except SurfaceBlendError as exc:
        raise FieldEditError("Relative humidity cannot be derived from saved T/Td") from exc
    if abs(_number(humidity) - expected) > 1e-6:
        raise FieldEditError("Relative humidity differs from its current T/Td derivation")


def validate_edit_scope(
    parent: dict[str, Any], candidate: dict[str, Any], recipes: list[dict[str, Any]]
) -> None:
    """Prove only recipe-changed editable cell-hours and their dependents differ.

    Context-only cells, geometry, native contributors, other fields and hour metadata
    must equal the parent. Copy-on-write sharing makes unchanged objects identical.
    """
    if candidate is parent:
        if recipes:
            raise FieldEditError("Accepted recipes require a changed candidate grid")
        return
    allowed: dict[tuple[str, str], set[str]] = {}
    for recipe in recipes:
        field = recipe["proposal"]["field"]
        for change in recipe["changes"]:
            allowed.setdefault((change["cell_id"], change["valid_time"]), set()).update(
                EDIT_DEPENDENTS.get(field, (field,))
            )
    if {key: value for key, value in parent.items() if key != "cells"} != {
        key: value for key, value in candidate.items() if key != "cells"
    }:
        raise FieldEditError("Edited grid metadata differs from its parent")
    before_cells = {cell_identity(cell): cell for cell in parent["cells"]}
    if [cell_identity(cell) for cell in candidate["cells"]] != list(before_cells):
        raise FieldEditError("Edited grid cells differ from its parent")
    for cell in candidate["cells"]:
        identity = cell_identity(cell)
        before = before_cells[identity]
        if cell is before:
            continue
        if {k: v for k, v in cell.items() if k not in {"hours", "qpf_intervals"}} != {
            k: v for k, v in before.items() if k not in {"hours", "qpf_intervals"}
        } or len(cell["hours"]) != len(before["hours"]):
            raise FieldEditError("Edited cell metadata differs from its parent")
        old_events, new_events = before.get("qpf_intervals"), cell.get("qpf_intervals")
        if old_events != new_events:
            if (
                not isinstance(old_events, list)
                or not isinstance(new_events, list)
                or len(old_events) != len(new_events)
            ):
                raise FieldEditError("Edited canonical QPF event inventory changed")
            for old_event, new_event in zip(old_events, new_events, strict=True):
                event_permitted = next(
                    (
                        fields
                        for (cell_key, valid), fields in allowed.items()
                        if cell_key == identity and _time(valid) == _time(old_event["interval_end"])
                    ),
                    set(),
                )
                if old_event != new_event and _time(old_event["interval_end"]) - _time(
                    old_event["interval_start"]
                ) != timedelta(hours=1):
                    raise FieldEditError("Coarse QPF events are inspect-only")
                if QPF not in event_permitted and old_event != new_event:
                    raise FieldEditError("Canonical QPF changed outside accepted edit scope")
                if {k: v for k, v in old_event.items() if k != "value"} != {
                    k: v for k, v in new_event.items() if k != "value"
                }:
                    raise FieldEditError("Native QPF event metadata changed")
        for old, new in zip(before["hours"], cell["hours"], strict=True):
            if new is old:
                continue
            permitted = allowed.get((identity, new["valid_time"]))
            if not permitted:
                raise FieldEditError("A cell-hour outside the accepted recipes changed")
            for key in set(old) | set(new):
                if key == "surface" or (key == "temperature" and TEMPERATURE in permitted):
                    continue
                if old.get(key) != new.get(key):
                    raise FieldEditError("Edited hour metadata differs from its parent")
            old_surface, new_surface = old["surface"], new["surface"]
            for key in set(old_surface) | set(new_surface):
                if key != "fields" and old_surface.get(key) is not new_surface.get(key):
                    if old_surface.get(key) != new_surface.get(key):
                        raise FieldEditError("Native evidence differs from its parent")
            old_fields, new_fields = old_surface["fields"], new_surface["fields"]
            if set(old_fields) != set(new_fields):
                raise FieldEditError("Edited field inventory differs from its parent")
            for name in old_fields:
                if name not in permitted and old_fields[name] != new_fields[name]:
                    raise FieldEditError(f"{name} changed outside its accepted edit scope")


def _proposal(grid: dict[str, Any], proposal: dict[str, Any]) -> tuple[str, set[str], set[str]]:
    required = {
        "field",
        "operation",
        "valid_times",
        "cell_ids",
        "parameters",
        "rationale",
        "evidence_refs",
    }
    if set(proposal) - (required | {"taper"}) or required - set(proposal):
        raise FieldEditError("Unknown or missing edit proposal keys")
    field = proposal["field"]
    if (
        not isinstance(field, str)
        or proposal["operation"] not in field_edit_contract(field).operations
    ):
        raise FieldEditError("Field is inspect-only or operation is unsupported")
    for name in ("valid_times", "cell_ids", "evidence_refs"):
        values = proposal[name]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item or len(item) > 256 for item in values)
            or len(values) != len(set(values))
        ):
            raise FieldEditError(f"{name} must be a nonempty unique bounded string list")
    if (
        len(proposal["valid_times"]) > 36
        or len(proposal["cell_ids"]) > 49
        or len(proposal["evidence_refs"]) > 32
    ):
        raise FieldEditError("Edit proposal exceeds current time/cell/evidence limits")
    if not isinstance(proposal["rationale"], str) or not 1 <= len(proposal["rationale"]) <= 2000:
        raise FieldEditError("A bounded edit rationale is required")
    cells = {cell_identity(cell): cell for cell in grid["cells"]}
    selected, times = set(proposal["cell_ids"]), set(proposal["valid_times"])
    if not selected <= cells.keys():
        raise FieldEditError("Edit contains an unknown cell")
    for identity in selected:
        cell = cells[identity]
        if cell.get("inside_editable_domain") is not True or cell.get("context_only") is not False:
            raise FieldEditError("Editing context-only cells is forbidden")
        if not times <= {row["valid_time"] for row in cell["hours"]}:
            raise FieldEditError("Edit valid time is outside the pinned forecast")
    return field, selected, times


def _parameters(proposal: dict[str, Any]) -> tuple[float, float | None]:
    operation = proposal["operation"]
    key = {"add": "delta", "scale": "factor", "smooth": "strength"}[operation]
    params = proposal["parameters"]
    if not isinstance(params, dict) or set(params) != {key}:
        raise FieldEditError(f"{operation} requires only {key}")
    value = _number(params[key])
    limits = field_edit_contract(proposal["field"]).intervention_limits
    for parameter_name, low, high in limits:
        if parameter_name == f"{operation}.{key}" and not low <= value <= high:
            raise FieldEditError("Edit exceeds the versioned editor intervention limit")
    if operation == "scale" and value < 0:
        raise FieldEditError("QPF scale must be nonnegative")
    if operation == "smooth" and not 0 <= value <= 1:
        raise FieldEditError("Smoothing strength must be between zero and one")
    taper = proposal.get("taper")
    width = None
    if taper is not None:
        if not isinstance(taper, dict) or set(taper) != {"width_m"}:
            raise FieldEditError("Taper requires only width_m")
        width = _number(taper["width_m"])
        if width <= 0:
            raise FieldEditError("Taper width must be positive")
    return value, width


def _taper(cell: dict[str, Any], width: float | None) -> float:
    if width is None:
        return 1.0
    distance = _number(cell["signed_distance_to_editable_boundary_m"])
    fraction = min(1.0, max(0.0, distance / width))
    return fraction * fraction * (3.0 - 2.0 * fraction)


def apply_edit(
    grid: dict[str, Any], proposal: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one atomic recipe or raise, leaving the input entirely untouched.

    Smoothing uses symmetric four-neighbor exchange within selected present nodes
    with identical intervals. Missing centers stay missing; missing neighbors
    receive no flux. Pairwise taper preserves the selected-node sum up to ordinary
    floating-point arithmetic. Add/scale intentionally change accumulation amount.
    """
    field, selected, times = _proposal(grid, proposal)
    parameter, width = _parameters(proposal)
    operation = proposal["operation"]
    index = {(cell["x_index"], cell["y_index"]): cell for cell in grid["cells"]}
    hours = {
        (cell_identity(cell), hour["valid_time"]): hour
        for cell in grid["cells"]
        if cell_identity(cell) in selected
        for hour in cell["hours"]
        if hour["valid_time"] in times
    }
    changes, updated_cells = [], []
    coherence_reports: dict[str, Any] = {}
    missing_count = 0
    for cell in grid["cells"]:
        identity = cell_identity(cell)
        if identity not in selected:
            updated_cells.append(cell)
            continue
        updated_hours = []
        for hour in cell["hours"]:
            if hour["valid_time"] not in times:
                updated_hours.append(hour)
                continue
            fields = _fields(hour)
            if field not in fields:
                raise FieldEditError("Selected field is unavailable in this saved grid")
            record = fields[field]
            _validate_field(field, record, hour)
            before = record.get("value")
            if before is None:
                missing_count += 1
                updated_hours.append(hour)
                continue
            amount = _number(before)
            taper = _taper(cell, width)
            if operation == "add":
                result = amount + parameter * taper
            elif operation == "scale":
                result = amount * (1.0 + (parameter - 1.0) * taper)
            else:
                fluxes = []
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbor = index.get((cell["x_index"] + dx, cell["y_index"] + dy))
                    if neighbor is None or cell_identity(neighbor) not in selected:
                        continue
                    neighbor_hour = hours[(cell_identity(neighbor), hour["valid_time"])]
                    other = _fields(neighbor_hour).get(field)
                    if other is None:
                        raise FieldEditError(
                            "Smoothing requires the same field at selected neighbors"
                        )
                    _validate_field(field, other, neighbor_hour)
                    if any(
                        other.get(key) != record.get(key)
                        for key in ("interval_start", "interval_end", "interval_closure", "unit")
                    ):
                        raise FieldEditError(
                            "Smoothing cannot combine incompatible accumulation events"
                        )
                    if other["value"] is not None:
                        fluxes.append(
                            (other["value"] - amount) * min(taper, _taper(neighbor, width))
                        )
                result = amount + parameter * math.fsum(fluxes) / 4.0
            if result == amount:
                updated_hours.append(hour)
                continue
            changed_fields = {**fields, field: {**record, "value": result}}
            changed_hour = {**hour, "surface": {**hour["surface"], "fields": changed_fields}}
            if field == TEMPERATURE:
                changed_hour["temperature"] = {**hour["temperature"], "value": result}
            _validate_field(field, changed_fields[field], changed_hour)
            coherent, coherence = BASELINE_COHERENCE.apply_local_fields(
                changed_fields, changed_fields=(field,)
            )
            # Reject rather than silently lose information: the existing kernels mark a
            # dew point above an edited temperature inconsistent (missing) and RH missing.
            for dependent in (DEW_POINT, RH):
                if (
                    dependent in fields
                    and fields[dependent].get("value") is not None
                    and coherent[dependent].get("value") is None
                ):
                    raise FieldEditError(
                        f"Edit would make available {dependent} missing under current "
                        "T/Td/RH rules; dew point is inspect-only and is never clamped"
                    )
            changed_hour["surface"] = {**hour["surface"], "fields": coherent}
            coherence_digest = str(canonical_json_digest(coherence))
            coherence_reports[coherence_digest] = coherence
            affected = (TEMPERATURE, DEW_POINT, RH) if field == TEMPERATURE else (QPF,)
            changes.append(
                {
                    "cell_id": identity,
                    "valid_time": hour["valid_time"],
                    "before": amount,
                    "after": result,
                    "unit": record["unit"],
                    "interval_start": record.get("interval_start"),
                    "interval_end": record.get("interval_end"),
                    "taper_weight": taper,
                    "coherence": coherence_digest,
                    "affected_values": {name: coherent[name].get("value") for name in affected},
                }
            )
            updated_hours.append(changed_hour)
        updated_cell = {**cell, "hours": updated_hours}
        if field == QPF and "qpf_intervals" in cell:
            hourly_changes = {
                _time(row["valid_time"]): row["after"]
                for row in changes
                if row["cell_id"] == identity
            }
            updated_cell["qpf_intervals"] = [
                {**event, "value": hourly_changes[_time(event["interval_end"])]}
                if _time(event["interval_end"]) in hourly_changes
                and _time(event["interval_end"]) - _time(event["interval_start"])
                == timedelta(hours=1)
                else event
                for event in cell["qpf_intervals"]
            ]
        updated_cells.append(updated_cell)
    if not changes:
        raise FieldEditError("Edit is an exact no-op (including missing or zero-taper cells)")
    result_grid = {**grid, "cells": updated_cells}
    validation = validate_grid(result_grid)
    recipe = {
        "tool_version": TOOL_VERSION,
        "proposal": deepcopy(proposal),
        "geometry_sha256": str(canonical_json_digest(grid["geometry"])),
        "input_values_sha256": grid_values_digest(grid),
        "output_values_sha256": grid_values_digest(result_grid),
        "changes": changes,
        "missing_cell_hours_preserved": missing_count,
        "coherence_reports": coherence_reports,
        "validation": validation,
        "smoothing_method": "symmetric_four_neighbor_single_pass"
        if operation == "smooth"
        else None,
        "temporal_redistribution": False,
    }
    return result_grid, recipe


def replay_edits(grid: dict[str, Any], recipes: list[dict[str, Any]]) -> dict[str, Any]:
    """Reproduce accepted checkpoints without any AI/provider or storage calls."""
    for recipe in recipes:
        if recipe.get("tool_version") != TOOL_VERSION:
            raise FieldEditError("Unsupported field-edit tool version")
        if recipe.get("input_values_sha256") != grid_values_digest(grid):
            raise FieldEditError("Edit replay parent values differ from retained evidence")
        grid, calculated = apply_edit(grid, recipe["proposal"])
        if calculated != recipe:
            raise FieldEditError("Edit replay does not match the retained deterministic recipe")
    return grid
