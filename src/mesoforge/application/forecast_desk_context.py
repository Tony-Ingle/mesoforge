"""Bounded views of pinned weather evidence; never a database or network tool."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from mesoforge.common.horizon import LEGACY_HORIZON, horizon_for
from mesoforge.common.identifiers import Digest
from mesoforge.common.qpf_intervals import (
    interval_time,
    summarize_qpf_intervals,
    validate_qpf_intervals,
)
from mesoforge.contracts.forecast_desk import MAX_INSPECTION_ROWS, TOOLS
from mesoforge.contracts.forecast_variants import instant
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.coherence import QPF, RELATIONSHIP_REGISTRY, TEMPERATURE
from mesoforge.forecasting.ice import FLAT_ICE, FRZR
from mesoforge.forecasting.probability_events import six_hour_events, six_hour_summary
from mesoforge.forecasting.provisional_policy import POP6

CONTEXT_VERSION = "mesoforge.forecast-desk-context.v1"
EXTENDED_CONTEXT_VERSION = "mesoforge.forecast-desk-context.v2"
_FIELD_KEYS = (
    "value",
    "unit",
    "status",
    "category",
    "policy",
    "interval_start",
    "interval_end",
    "interval_closure",
    "threshold",
    "event",
    "event_definition",
    "spatial_definition",
    "missing_reasons",
    "weights",
    "supported_types",
    "cycle",
    "source_cycle",
    "source_lead_hours",
    "valid_time",
    "native_valid_time",
    "source",
    "product",
    "model",
    "provider",
    "source_id",
    "method",
    "method_metadata",
    "native_quantity",
    "quantity_kind",
    "native_unit",
    "native_value",
    "native_parameter",
    "temporal_semantics",
    "interval_role",
    "hydrometeor_scope",
    "role",
    "active_weight",
    "diagnostic_ratio",
    "diagnostic_ratio_status",
    "profile_time_approximation",
    "profile_metadata",
    "evidence_only",
    "evidence_path",
    "native_values",
    "encoding",
    "conditional_type_fractions",
    "event_id",
    "accumulation_hours",
    "accumulation_duration_hours",
    "event_duration_hours",
    "spatial_support",
    "population",
    "method_description",
    "accretion_geometry",
    "definition_note",
)

# Read projections of the existing saved canvas, not a second field/policy catalog.
# Synthetic view labels already occur in the coherence registry; absent payloads
# remain absent evidence, never inferred native values or newly delivered fields.
_EVIDENCE_PATHS = {
    "snowfall_water_equivalent_amount": ("snowfall_guidance", "contributors"),
    "snowfall_amount": ("snowfall_amount_guidance", "native_contributors"),
    "shadow_kuchera_snowfall_amount": ("snowfall_amount_guidance", "derived_contributors"),
    "snow_to_liquid_ratio": ("snowfall_amount_guidance", "native_slr"),
    FLAT_ICE: ("ice_guidance", "contributors"),
    FRZR: ("ice_guidance", "contributors"),
}


_PRIVATE_KEYS = frozenset(
    {
        "provenance",
        "endpoint",
        "etag",
        "url",
        "uri",
        "path",
        "file",
        "messages",
        "headers",
        "manifest_sha256",
        "authorization",
        "credential",
    }
)


def _private_key(key: str) -> bool:
    """Acquisition/storage detail nested under a whitelisted key is never projected."""
    lowered = key.lower()
    return (
        lowered in _PRIVATE_KEYS
        or lowered.startswith("raw_")
        or lowered.endswith(("_url", "_uri", "_file", "_path", "_etag", "_endpoint"))
    )


_URL = re.compile(r"(?i)\b(?:https?|s3|ftp|file)://\S+")


class DeskContextBudgetError(ValueError):
    """The pinned compact context cannot fit its configured byte budget."""


def _small(value: Any, depth: int = 0) -> Any:
    """Only whitelisted meteorological values reach this bounded JSON projection."""
    if depth > 4:
        return {"status": "detail_omitted", "truncated": True}
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        # Free-text notes can embed acquisition URLs from upstream exceptions.
        value = _URL.sub("[url omitted]", value)
        return (
            value
            if len(value) <= 384
            else {
                "text_prefix": value[:384],
                "truncated": True,
                "original_characters": len(value),
            }
        )
    if isinstance(value, list | tuple):
        items = [_small(item, depth + 1) for item in value[:16]]
        return (
            items
            if len(value) <= 16
            else {
                "items": items,
                "truncated": True,
                "original_items": len(value),
            }
        )
    if isinstance(value, dict):
        public = [(str(k), v) for k, v in value.items() if not _private_key(str(k))]
        mapping_items = {k: _small(v, depth + 1) for k, v in public[:20] if len(k) <= 80}
        if len(public) != len(value):
            mapping_items["private_acquisition_detail_omitted"] = True
        return (
            mapping_items
            if len(public) <= 20 and all(len(k) <= 80 for k, _ in public)
            else {
                "items": mapping_items,
                "truncated": True,
                "original_items": len(value),
            }
        )
    return None


DIRECTION = "wind_from_direction_10m"
_EVENT_KEYS = (
    "unit",
    "interval_start",
    "interval_end",
    "threshold",
    "event_definition",
    "spatial_definition",
)


def minimum_arc(values: list[float]) -> tuple[float, float, float]:
    """Smallest compass arc containing every bearing: (start, end, width degrees)."""
    bearings = sorted(v % 360 for v in values)
    if len(bearings) == 1:
        return bearings[0], bearings[0], 0.0
    gaps = [(b - a, i + 1) for i, (a, b) in enumerate(zip(bearings, bearings[1:], strict=False))]
    gaps.append((bearings[0] + 360 - bearings[-1], 0))
    largest, start_index = max(gaps)
    return bearings[start_index], bearings[start_index - 1], 360 - largest


def _direction_change(a: float, b: float) -> float:
    difference = abs(a - b) % 360
    return min(difference, 360 - difference)


def comparable_groups(field: str, native: dict[str, Any]) -> list[dict[str, Any]]:
    """Contributors compared only with others sharing unit, interval and event definition.

    No unit conversion or cross-event pooling. Fewer than two members is not a spread.
    """
    groups: dict[str, dict[str, Any]] = {}
    for identity, row in sorted(native.items()):
        value = row.get("value")
        if not _number(value):
            continue
        key = json.dumps({k: row.get(k) for k in _EVENT_KEYS}, sort_keys=True, default=str)
        group = groups.setdefault(key, {"unit": row.get("unit"), "members": {}})
        group["members"][identity] = value
    result = []
    for group in groups.values():
        values = list(group["members"].values())
        if len(values) < 2:
            continue
        spread = minimum_arc(values)[2] if field == DIRECTION else max(values) - min(values)
        result.append({**group, "spread": spread})
    return result


def field_view(field: dict[str, Any]) -> dict[str, Any]:
    return {key: _small(field[key]) for key in _FIELD_KEYS if key in field}


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _available(field: dict[str, Any]) -> bool:
    value = field.get("value")
    return (
        field.get("status") not in {"unavailable", "unsupported", "policy_unavailable"}
        and value is not None
        and value != "unavailable"
        and (_number(value) or isinstance(value, str | list | dict))
    )


def cell_id(cell: dict[str, Any]) -> str:
    return f"{cell['x_index']}:{cell['y_index']}"


def fields_at(hour: dict[str, Any]) -> dict[str, Any]:
    surface = hour.get("surface", {})
    result = {TEMPERATURE: hour.get("temperature", {}), **surface.get("fields", {})}
    for name, (path, collection) in _EVIDENCE_PATHS.items():
        guidance = surface.get(path, {})
        native = guidance.get("fields", {}).get(name)
        if native is None and collection in {"contributors", "native_contributors"}:
            native = guidance.get("field")
        # A ratio or derived-method collection has no approved active counterpart.
        # Do not pick its first member as a delivered forecast.
        result[name] = {
            **(
                native
                or {
                    "value": None,
                    "status": "policy_unavailable" if guidance else "unavailable",
                    "missing_reasons": [
                        "No active field; inspect separately retained evidence"
                        if guidance
                        else "Evidence family not retained in pinned grid"
                    ],
                }
            ),
            "evidence_only": True,
            "evidence_path": f"surface.{path}.{collection}",
        }
    return result


def contributors_at(hour: dict[str, Any], field: str) -> dict[str, Any]:
    result = {
        model: {
            **field_view(row),
            **field_view(row.get("fields", {}).get(field, {})),
        }
        for model, row in hour.get("surface", {}).get("contributors", {}).items()
        if isinstance(row, dict) and field in row.get("fields", {})
    }
    if field == POP6:
        for model, native in (
            hour.get("surface", {}).get("fields", {}).get(field, {}).get("contributors", {}).items()
        ):
            result[str(native.get("source_id", model))] = field_view(native)
    if field == TEMPERATURE:
        for key in ("sources", "shadow_sources"):
            for row in hour.get(key, []):
                if "value" not in result.get(row["model"], {}):
                    result[row["model"]] = {
                        **field_view(row),
                        **field_view(row.get("temperature", {})),
                    }
    if field in _EVIDENCE_PATHS:
        path, collection = _EVIDENCE_PATHS[field]
        guidance = hour.get("surface", {}).get(path, {})
        rows = guidance.get(collection, [])
        if field in {FLAT_ICE, FRZR}:
            quantity = guidance.get("fields", {}).get(field, {}).get("quantity_kind")
            rows = [
                row for row in rows if quantity is not None and row.get("quantity_kind") == quantity
            ]
        return {
            (
                f"{row.get('source_id', row.get('model', 'unknown'))}:"
                f"{row.get('method', collection)}:{i}"
            ): {
                **field_view(row),
                "evidence_only": True,
            }
            for i, row in enumerate(rows[:12])
            if isinstance(row, dict)
        }
    # Temporary/evidence families preserve distinct products from the same model.
    for key, guidance in hour.get("surface", {}).items():
        if not key.endswith("_guidance") or not isinstance(guidance, dict):
            continue
        family = guidance.get("field", {})
        if not (
            key == "probability_guidance" and field == "probability_of_precipitation_1h"
        ) and family != hour.get("surface", {}).get("fields", {}).get(field):
            continue
        rows = guidance.get("contributors", [])
        if isinstance(rows, list):
            for index, row in enumerate(rows[:12]):
                identity = str(row.get("source_id", row.get("model", f"{key}:{index}")))
                if identity in result:
                    identity = f"{identity}:{key}:{index}"
                result[identity] = field_view(row)
    return result


def dependencies(field: str) -> list[dict[str, Any]]:
    return [
        asdict(row)
        for row in RELATIONSHIP_REGISTRY.values()
        if field in row.inputs or field in row.outputs
    ]


def _pin(forecast: dict[str, Any]) -> dict[str, Any]:
    lineage = forecast["baseline_snapshot"]
    cutoff = instant(lineage["forecast_analysis_cutoff"])
    if lineage.get("information_cutoff", {}).get("status") != "proven":
        raise ValueError("Pinned input availability is unproven")
    for key in ("background_analysis_cutoff", "built_at", "completed_at", "published_at"):
        if instant(lineage[key]) > cutoff:
            raise ValueError("Pinned baseline follows the forecast information cutoff")
    stage = forecast.get("learning_stage", {})
    if not stage.get("variant_id") or stage.get("transformation_type") != "deterministic_corrected":
        raise ValueError("Forecast desk requires a pinned deterministic corrected stage")
    if (
        stage.get("baseline_snapshot_id") != lineage["baseline_snapshot_id"]
        or instant(stage["analysis_cutoff"]) != cutoff
    ):
        raise ValueError("Corrected stage does not belong to the pinned evidence")
    return {
        "baseline_snapshot_id": lineage["baseline_snapshot_id"],
        "prepared_snapshot_id": lineage["prepared_snapshot_id"],
        "corrected_stage_id": stage.get("variant_id"),
        "analysis_cutoff": cutoff.isoformat(),
        "reference_time": forecast["target_reference_time"],
        "parent_grid_sha256": forecast["local_grid"]["sha256"],
    }


def _scalar_facts(mapping: Any) -> dict[str, Any]:
    """Numbers, flags, short labels and short label lists; governance prose is omitted."""
    if not isinstance(mapping, dict):
        return {}
    return {
        str(key): value
        for key, value in mapping.items()
        if value is None
        or type(value) in (bool, int)
        or (isinstance(value, float) and math.isfinite(value))
        or (isinstance(value, str) and len(value) <= 120)
        or (
            isinstance(value, list)
            and len(value) <= 12
            and all(isinstance(item, str) and len(item) <= 80 for item in value)
        )
    }


def _qpf_timing(hours: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-contributor point QPF onset/peak/total on the active hourly event only."""
    profiles: dict[str, dict[str, Any]] = {}
    for hour in hours:
        active = fields_at(hour).get(QPF, {})
        for identity, row in contributors_at(hour, QPF).items():
            if not _number(row.get("value")) or any(
                row.get(key) != active.get(key)
                for key in ("unit", "interval_start", "interval_end")
            ):
                continue
            value = row["value"]
            profile = profiles.setdefault(
                identity,
                {"unit": row.get("unit"), "available_hours": 0, "total": 0.0, "peak": None},
            )
            profile["available_hours"] += 1
            profile["total"] += value
            if value > 0 and "first_positive_valid_time" not in profile:
                profile["first_positive_valid_time"] = hour["valid_time"]
            if profile["peak"] is None or value > profile["peak"]["value"]:
                profile["peak"] = {"value": value, "valid_time": hour["valid_time"]}
    return {
        "basis": "point contributors on the active 1-hour interval/unit; totals exclude missing",
        "contributors": dict(sorted(profiles.items())[:12]),
    }


def _period_point_summary(
    point: list[tuple[str, Any]], name: str, period_peaks: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Five bounded elapsed-day summaries; no invented occurrence/intensity thresholds."""
    result = []
    for offset in range(0, len(point), 24):
        group = point[offset : offset + 24]
        values = [value for _, value in group if _number(value)]
        row: dict[str, Any] = {
            "period": offset // 24 + 1,
            "numeric_hours": len(values),
            "expected_hours": len(group),
            "range": list(minimum_arc(values))
            if values and name == DIRECTION
            else [min(values), max(values)]
            if values
            else None,
            "maximum_comparable_disagreement": period_peaks.get(offset // 24),
        }
        if name == QPF:
            # This is an explicitly incomplete sum when hours are absent; never
            # a replacement for the native interval amounts or a complete total.
            row["sum_of_available_hourly_amounts"] = sum(values)
            row["positive_hours"] = sum(value > 0 for value in values)
        elif not values:
            states = Counter(str(value) for _, value in group)
            row["states"] = dict(sorted(states.items()))
        result.append(row)
    return result


def _qpf_event_summary(rows: list[dict[str, Any]], reference: Any, duration: int) -> dict[str, Any]:
    """Compact period context from native events; coarse events remain inspect-only."""
    amounts = [row for row in rows if row["value"] is not None]
    maximum = max(amounts, key=lambda row: row["value"]) if amounts else None
    return {
        "event_count": len(rows),
        "event_durations_hours": sorted(
            {
                (
                    interval_time(row["interval_end"]) - interval_time(row["interval_start"])
                ).total_seconds()
                / 3600
                for row in rows
            }
        ),
        "maximum_native_event": field_view(maximum) if maximum else None,
        "horizon": summarize_qpf_intervals(
            rows, start=reference, end=reference + timedelta(hours=duration)
        ),
        "periods": [
            {
                "period": offset // 24 + 1,
                **summarize_qpf_intervals(
                    rows,
                    start=reference + timedelta(hours=offset),
                    end=reference + timedelta(hours=offset + 24),
                ),
            }
            for offset in range(0, duration, 24)
        ],
        "semantics": "Canonical partition; do not add hourly amounts again. "
        "Coarse events are inspect-only; no temporal splitting.",
    }


def _native_event_at(cell: dict[str, Any], valid_time: str) -> dict[str, Any] | None:
    instant_value = interval_time(valid_time)
    return next(
        (
            row
            for row in cell.get("qpf_intervals", [])
            if interval_time(row["interval_start"])
            < instant_value
            <= interval_time(row["interval_end"])
        ),
        None,
    )


def build_context(
    forecast: dict[str, Any],
    *,
    max_bytes: int = 65536,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """No source paths, provider secrets, arbitrary metadata or raw provenance blobs."""
    from mesoforge.forecasting.field_blend import field_edit_contract

    pin = _pin(forecast)
    grid = forecast["local_grid_baseline"]
    cells = grid["cells"]
    horizon = horizon_for(forecast)
    if horizon_for(grid) != horizon:
        raise ValueError("Desk forecast and saved grid horizon differ")
    if not cells or len(cells) > 4096:
        raise ValueError("Desk requires a bounded complete saved grid")
    reference = instant(forecast["target_reference_time"])
    horizon.validate_hour_rows(forecast["hours"], reference)
    for cell in cells:
        horizon.validate_hour_rows(cell["hours"], reference)
        if "qpf_intervals" in cell:
            validate_qpf_intervals(
                cell["qpf_intervals"],
                start=reference,
                end=reference + timedelta(hours=horizon.duration_hours),
            )
    extended = horizon != LEGACY_HORIZON
    names = sorted({name for cell in cells for hour in cell["hours"] for name in fields_at(hour)})
    if len(names) > 64:
        raise ValueError("Desk field inventory exceeds context contract")
    summaries: dict[str, Any] = {}
    editable_relevant = False
    for name in names:
        values: list[tuple[float, str, str]] = []
        available = missing = known = 0
        states: Counter[str] = Counter()
        evidence_states: Counter[str] = Counter()
        evidence_sources: set[str] = set()
        evidence_units: set[str] = set()
        evidence_available = 0
        spread: float | None = None
        spread_unit: str | None = None
        peak = None
        comparable_cell_hours = 0
        units: set[str] = set()
        period_peaks: dict[int, dict[str, Any]] = {}
        for cell in cells:
            for hour_index, hour in enumerate(cell["hours"]):
                field = fields_at(hour).get(name, {})
                value = field.get("value")
                units.add(str(field.get("unit", "unavailable")))
                if _number(value):
                    values.append((value, hour["valid_time"], cell_id(cell)))
                    if (
                        name in (QPF, "probability_of_precipitation_1h", POP6)
                        and value > 0
                        and cell.get("inside_editable_domain") is True
                    ):
                        editable_relevant = True
                if _available(field):
                    available += 1
                    if _number(value) or value not in ("unknown", "unavailable"):
                        known += 1
                else:
                    missing += 1
                states[
                    str(field.get("status", "available" if value is not None else "unavailable"))
                ] += 1
                native = contributors_at(hour, name)
                for identity, row in native.items():
                    evidence_sources.add(identity)
                    evidence_units.add(str(row.get("unit", "unavailable")))
                    evidence_states[str(row.get("status", "unspecified"))] += 1
                evidence_available += int(any(_available(row) for row in native.values()))
                groups = comparable_groups(name, native)
                if groups:
                    comparable_cell_hours += 1
                    widest = max(groups, key=lambda group: group["spread"])
                    period = hour_index // 24
                    if extended and widest["spread"] > period_peaks.get(period, {}).get(
                        "spread", -1
                    ):
                        period_peaks[period] = {
                            "spread": widest["spread"],
                            "unit": widest["unit"],
                            "valid_time": hour["valid_time"],
                            "cell_id": cell_id(cell),
                        }
                    if spread is None or widest["spread"] > spread:
                        spread, spread_unit = widest["spread"], widest["unit"]
                        peak = {"valid_time": hour["valid_time"], "cell_id": cell_id(cell)}
        point = [
            (h["valid_time"], fields_at(h).get(name, {}).get("value")) for h in forecast["hours"]
        ]
        numeric_point = [(t, v) for t, v in point if _number(v)]
        direction = name == DIRECTION
        arc = minimum_arc([v[0] for v in values]) if direction and values else None
        summary: dict[str, Any] = {
            "units": sorted(units),
            "available_cell_hours": available,
            "known_value_cell_hours": known,
            "numerical_cell_hours": len(values),
            "missing_cell_hours": missing,
            "states": dict(sorted(states.items())),
            "range": (
                {"minimum_containing_arc": [arc[0], arc[1]], "arc_degrees": arc[2]}
                if arc is not None
                else [min(v[0] for v in values), max(v[0] for v in values)]
                if values
                else None
            ),
            "spatial_maximum": {
                "value": max(values)[0],
                "valid_time": max(values)[1],
                "cell_id": max(values)[2],
            }
            if values and not direction
            else None,
            "point_first_last": [numeric_point[0], numeric_point[-1]] if numeric_point else None,
            "maximum_comparable_contributor_spread": spread,
            "spread_status": "available" if spread is not None else "no_comparable_pairs",
            "spread_unit": spread_unit,
            "comparable_contributor_cell_hours": comparable_cell_hours,
            "spread_semantics": "minimum_containing_arc_degrees"
            if direction
            else "same_unit_interval_event_max_minus_min",
            "spread_peak": peak,
            "edit_contract": (
                asdict(field_edit_contract(name))
                if field_edit_contract(name).operations
                else {"operations": ()}
            ),
            "native_evidence": {
                "cell_hours_with_available_evidence": evidence_available,
                "source_identities": sorted(evidence_sources),
                "units": sorted(evidence_units),
                "row_states": dict(sorted(evidence_states.items())),
            },
            "evidence_only": name in _EVIDENCE_PATHS,
        }
        if numeric_point and not direction:
            high = max(numeric_point, key=lambda row: row[1])
            low = min(numeric_point, key=lambda row: row[1])
            summary["point_extremes"] = {
                "maximum": {"value": high[1], "valid_time": high[0]},
                "minimum": {"value": low[1], "valid_time": low[0]},
            }
        if direction and len(numeric_point) > 1:
            changes = [
                (_direction_change(a[1], b[1]), b[0])
                for a, b in zip(numeric_point, numeric_point[1:], strict=False)
            ]
            degrees, when = max(changes)
            summary["point_largest_hourly_direction_change"] = {
                "degrees": degrees,
                "valid_time": when,
            }
        if name == QPF:
            summary["point_sum_of_available_hourly_amounts"] = sum(v for _, v in numeric_point)
            if not extended:
                summary["positive_point_hours"] = [t for t, v in numeric_point if v > 0]
            summary["missing_hours_excluded_from_sum"] = len(point) - len(numeric_point)
            summary["point_contributor_timing"] = _qpf_timing(forecast["hours"])
            if "qpf_intervals" in forecast:
                centers = [cell for cell in cells if cell["is_forecast_point"]]
                if (
                    len(centers) != 1
                    or centers[0].get("qpf_intervals") != forecast["qpf_intervals"]
                ):
                    raise ValueError("Desk point QPF partition differs from pinned grid center")
                summary["point_native_events"] = _qpf_event_summary(
                    forecast["qpf_intervals"], reference, horizon.duration_hours
                )
        if extended:
            summary["point_forecast_periods"] = _period_point_summary(point, name, period_peaks)
        if name == POP6:
            events = six_hour_events(forecast["hours"])
            summary["point_native_events"] = {
                "event_duration_hours": 6,
                "interpretation": "whole native six-hour event probabilities; inspect-only; "
                "never hourly or daily probabilities",
                **six_hour_summary(
                    events, start=reference, end=reference + timedelta(hours=horizon.duration_hours)
                ),
                "periods": [
                    {
                        "period": offset // 24 + 1,
                        **six_hour_summary(
                            events,
                            start=reference + timedelta(hours=offset),
                            end=reference + timedelta(hours=offset + 24),
                        ),
                    }
                    for offset in range(0, horizon.duration_hours, 24)
                ],
            }
        summaries[name] = summary
    relevant = any(
        isinstance(summaries.get(name, {}).get("range"), list) and summaries[name]["range"][1] > 0
        for name in (QPF, "probability_of_precipitation_1h", POP6)
    )
    for cell in cells:
        positive_event = any(
            _number(row.get("value")) and row["value"] > 0 for row in cell.get("qpf_intervals", [])
        )
        relevant = relevant or positive_event
        editable_relevant = editable_relevant or (positive_event and cell["inside_editable_domain"])
    geometry = grid["geometry"]
    value = {
        "schema_version": EXTENDED_CONTEXT_VERSION if extended else CONTEXT_VERSION,
        "pinned_evidence": pin,
        "location": {"latitude": forecast["latitude"], "longitude": forecast["longitude"]},
        "horizon_hours": horizon.duration_hours,
        "valid_times": [h["valid_time"] for h in forecast["hours"]],
        "geometry": {
            key: geometry[key] for key in ("dimensions", "spacing_m", "domains", "point_target")
        },
        "cells": [
            {
                "cell_id": cell_id(c),
                "latitude": c["latitude"],
                "longitude": c["longitude"],
                "editable": c["inside_editable_domain"],
                # Taper weight is smoothstep(distance / width_m): zero on the edge.
                **(
                    {"taper_edge_distance_m": c.get("signed_distance_to_editable_boundary_m")}
                    if c["inside_editable_domain"]
                    else {}
                ),
            }
            for c in cells
        ],
        "fields": summaries,
        "unavailable_fields": [
            name for name in names if not summaries[name]["available_cell_hours"]
        ],
        "precipitation_relevant": bool(relevant),
        "precipitation_relevant_in_editable_domain": editable_relevant,
        "precipitation_relevance_basis": (
            "any positive saved QPF or PoP; no intensity/skill threshold"
        ),
        "native_evidence_interpretation": (
            "Contributors are separate retained products and evidence, never replacement "
            "forecasts; no promotion or cross-event pooling. Inspect-only fields have no "
            "edit operations."
        ),
        # Compact relationship index; inspect_dependencies returns the full records.
        "dependencies": [
            {
                key: getattr(row, key)
                for key in ("relationship_id", "kind", "status", "inputs", "outputs")
            }
            for row in RELATIONSHIP_REGISTRY.values()
        ],
        "inspection_contract": {
            "allowed_regions": ["point", "editable", "context"],
            "cell_ids": "optional list of cell_id values narrowing the region; null for all",
            "maximum_requested_rows": MAX_INSPECTION_ROWS,
            "maximum_requested_valid_times": 36,
            "tools": {
                "summarize_field": "one row per valid time: counts, min/max/mean (arc for "
                "direction) or category counts over the selected cells, plus the point value",
                "inspect_baseline": "one row per cell/hour: the complete saved MesoForge field",
                "inspect_contributors": "inspect_baseline plus every retained contributor row",
                "inspect_disagreement": "one row per cell/hour: MesoForge value and contributor "
                "groups sharing unit/interval/event, with member values and spread",
                "inspect_verification_history": "cutoff-proven verification summary (temperature "
                "station-proxy only; QPF verification is not yet desk evidence)",
                "inspect_dependencies": "registered cross-field relationships and their status",
            },
            "ordering": "saved grid cell order, then saved valid-time order",
            "output_limits": (
                "Runtime byte cap includes metadata and evidence reference; fewer rows may fit. "
                "Check returned_rows, total_matching_rows and truncation_reasons. "
                "Prefer one valid time and point region or cell_ids for detailed contributors."
            ),
        },
        "verification": {
            "status": "unavailable",
            "reason": "No cutoff-proven compact evidence supplied",
        },
    }
    if extended:
        value["forecast_periods"] = [
            {
                "period": offset // 24 + 1,
                "start": (reference + timedelta(hours=offset)).isoformat(),
                "end": (reference + timedelta(hours=offset + 24)).isoformat(),
                "semantics": "elapsed_forecast_hours_not_calendar_day",
            }
            for offset in range(0, horizon.duration_hours, 24)
        ]
    if evidence is not None:
        # Accept only the existing cutoff-filtered site-analysis summary contract.
        # Unproven or later evidence is excluded (never shown), not a desk failure.
        proof = evidence.get("evaluation", {})
        try:
            proven = (
                proof.get("evidence_availability") == "verified_input_cutoff_and_fact_registration"
                and instant(proof["evidence_cutoff"]) <= instant(pin["analysis_cutoff"])
                and evidence.get("coordinate") == value["location"]
            )
        except (KeyError, TypeError, ValueError):
            proven = False
        value["verification"] = (
            {
                "status": "available",
                "scope": "temperature site verification (station proxy)",
                "evidence_cutoff": proof["evidence_cutoff"],
                "evidence_policy": (evidence.get("evidence_policy") or {}).get("id"),
                "correction_readiness": {
                    **_scalar_facts(evidence.get("correction_readiness")),
                    "lead_buckets": {
                        str(bucket): _scalar_facts(row)
                        for bucket, row in (
                            (evidence.get("correction_readiness") or {}).get("lead_buckets") or {}
                        ).items()
                    },
                },
            }
            if proven
            else {
                "status": "unavailable",
                "reason": "Verification evidence availability is not proven before cutoff",
            }
        )
    if len(canonical_json_bytes(value)) > max_bytes:
        raise DeskContextBudgetError("Prepared context exceeds configured byte budget")
    return {**value, "context_digest": str(canonical_json_digest(value))}


def task_queue(context: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    fields = context["fields"]
    ordered = sorted(
        fields,
        key=lambda name: (
            0 if name == QPF and context["precipitation_relevant"] else 1,
            0 if fields[name]["available_cell_hours"] else 1,
            0 if fields[name]["edit_contract"]["operations"] else 1,
            0 if name == TEMPERATURE else 1,
            0 if (fields[name]["maximum_comparable_contributor_spread"] or 0) > 0 else 1,
            name,
        ),
    )
    return [
        {
            "field": name,
            "priority": index + 1,
            "basis": (
                "precipitation QPF first; availability, editability, temperature, "
                "presence of disagreement, stable order; no cross-unit spread ranking"
            ),
        }
        for index, name in enumerate(ordered[:limit])
    ]


def _field_summary(field: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [row["value"] for row in rows if _number(row.get("value"))]
    categories = Counter(
        str(row.get("value"))
        for row in rows
        if isinstance(row.get("value"), str) and _available(row)
    )
    summary: dict[str, Any] = {
        "selected_cells": len(rows),
        "available": sum(_available(row) for row in rows),
        "numerical": len(values),
        "units": sorted({str(row.get("unit", "unavailable")) for row in rows}),
    }
    if values and field == DIRECTION:
        start, end, width = minimum_arc(values)
        summary["minimum_containing_arc"] = {"start": start, "end": end, "degrees": width}
    elif values:
        summary.update(minimum=min(values), maximum=max(values), mean=sum(values) / len(values))
    if categories:
        summary["categories"] = dict(categories.most_common(6))
    return summary


def inspect_evidence(
    forecast: dict[str, Any],
    context: dict[str, Any],
    request: dict[str, Any],
    *,
    max_bytes: int,
    state_digest: Digest | None = None,
) -> dict[str, Any]:
    """Strict finite meteorological projection. No URLs, paths, queries or executable tools."""
    if state_digest is not None:
        Digest(state_digest)
    tool, field = request["tool"], request["field"]
    times, region, limit = request["valid_times"], request["region"], request["max_rows"]
    selected_cells = request.get("cell_ids")
    if tool not in TOOLS or field not in context["fields"]:
        raise ValueError("Unsupported inspection tool or field")
    if (
        not times
        or len(times) > 36
        or len(set(times)) != len(times)
        or not set(times) <= set(context["valid_times"])
    ):
        raise ValueError("Inspection time selection is outside the pinned horizon")
    if (
        region not in {"context", "editable", "point"}
        or type(limit) is not int
        or not 1 <= limit <= MAX_INSPECTION_ROWS
    ):
        raise ValueError("Inspection region/output bound is invalid")
    known_cells = {row["cell_id"] for row in context["cells"]}
    if selected_cells is not None and (
        not isinstance(selected_cells, list)
        or not 1 <= len(selected_cells) <= len(known_cells)
        or len(set(selected_cells)) != len(selected_cells)
        or not set(selected_cells) <= known_cells
    ):
        raise ValueError("Inspection cell selection is not part of the pinned grid")
    cells = [
        cell
        for cell in forecast["local_grid_baseline"]["cells"]
        if not (region == "editable" and not cell["inside_editable_domain"])
        and not (region == "point" and not cell["is_forecast_point"])
        and (selected_cells is None or cell_id(cell) in selected_cells)
    ]
    rows: list[dict[str, Any]] = []
    if tool == "inspect_verification_history":
        rows = [context["verification"]]
    elif tool == "inspect_dependencies":
        rows = dependencies(field)
    elif tool == "summarize_field":
        point = next(
            (c for c in forecast["local_grid_baseline"]["cells"] if c["is_forecast_point"]), None
        )
        for valid_time in [t for t in context["valid_times"] if t in times]:
            selected = [
                fields_at(hour).get(field, {})
                for cell in cells
                for hour in cell["hours"]
                if hour["valid_time"] == valid_time
            ]
            point_value = (
                next(
                    (
                        fields_at(hour).get(field, {}).get("value")
                        for hour in point["hours"]
                        if hour["valid_time"] == valid_time
                    ),
                    None,
                )
                if point is not None
                else None
            )
            rows.append(
                {
                    "valid_time": valid_time,
                    "field": field,
                    "point_value": _small(point_value),
                    **_field_summary(field, selected),
                }
            )
    else:
        for cell in cells:
            for hour in cell["hours"]:
                if hour["valid_time"] not in times:
                    continue
                active = fields_at(hour).get(field, {})
                native_event = _native_event_at(cell, hour["valid_time"]) if field == QPF else None
                event_projection = (
                    {
                        "native_qpf_event": {
                            **field_view(native_event),
                            "interpretation": "whole native event, not hourly redistribution",
                        }
                    }
                    if native_event is not None
                    else {}
                )
                if tool == "inspect_disagreement":
                    groups = comparable_groups(field, contributors_at(hour, field))
                    rows.append(
                        {
                            "cell_id": cell_id(cell),
                            "valid_time": hour["valid_time"],
                            "field": field,
                            "mesoforge_value": _small(active.get("value")),
                            "unit": active.get("unit"),
                            "comparable_groups": groups[:6],
                            "status": "available" if groups else "no_comparable_pairs",
                            **event_projection,
                        }
                    )
                    continue
                row = {
                    "cell_id": cell_id(cell),
                    "valid_time": hour["valid_time"],
                    "field": field,
                    "baseline": field_view(active),
                    **event_projection,
                }
                if tool == "inspect_contributors":
                    row["contributors"] = contributors_at(hour, field)
                    if native_event is not None:
                        row["native_event_contributors"] = {
                            identity: field_view(contributor)
                            for identity, contributor in native_event.get(
                                "contributors", {}
                            ).items()
                        }
                rows.append(row)
    total = len(rows)
    result: dict[str, Any] = {
        "request": request,
        "rows": rows[:limit],
        "total_matching_rows": total,
        "returned_rows": min(total, limit),
        "max_output_bytes": max_bytes,
        "truncated": total > limit,
        "truncation_reasons": ["row_limit"] if total > limit else [],
        "context_digest": context["context_digest"],
        "current_state_digest": state_digest,
        "status": (
            str(context["verification"].get("status", "unavailable"))
            if tool == "inspect_verification_history"
            else "available"
            if total
            else "no_matching_rows"
        ),
    }

    def sealed_result() -> dict[str, Any]:
        return {**result, "evidence_ref": str(canonical_json_digest(result))}

    # Retain whole scientific rows. The digest/envelope is part of the hard cap,
    # and an oversized row is a tool limitation rather than a failed forecast.
    if len(canonical_json_bytes(sealed_result())) > max_bytes and result["rows"]:
        candidates = result["rows"]
        result["truncated"] = True
        result["truncation_reasons"] = [*result["truncation_reasons"], "byte_budget"]
        result["narrower_request_hint"] = (
            "Request one valid time with region=point or explicit cell_ids for contributors. "
            "If one complete row still cannot fit, use inspect_disagreement or "
            "summarize_field; this tool cannot increase its byte cap."
        )
        low, high = 0, len(candidates) - 1  # Largest whole-row prefix that fits.
        while low < high:
            middle = (low + high + 1) // 2
            result["rows"], result["returned_rows"] = candidates[:middle], middle
            if len(canonical_json_bytes(sealed_result())) <= max_bytes:
                low = middle
            else:
                high = middle - 1
        result["rows"], result["returned_rows"] = candidates[:low], low
    if total and not result["rows"]:
        result["status"] = "output_budget_exceeded"
        result["reason"] = "No complete evidence row fits the configured tool byte budget"
    sealed = sealed_result()
    if len(canonical_json_bytes(sealed)) > max_bytes:
        raise ValueError("Inspection result metadata exceeds tool byte budget")
    return sealed
