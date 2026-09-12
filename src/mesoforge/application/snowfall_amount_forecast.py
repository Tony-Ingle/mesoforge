"""Native new-snow amounts and a separate profile-based Kuchera comparison."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.spatial import (
    CellIndices,
    PointExtractionError,
    bilinear_interpolate,
    find_enclosing_cell,
    project_station_point,
)
from mesoforge.application.snowfall_forecast import SnowView, extract_snowfall_contributors
from mesoforge.application.spatial_coverage import point_in_grid, validate_coordinate
from mesoforge.forecasting.snowfall_amount import KUCHERA_METHOD, kuchera_ratio

AMOUNT = "snowfall_amount"
QUANTITY = "new_snowfall_amount"
CLOSURE = "left_open_right_closed"


@dataclass(frozen=True)
class AmountView:
    dataset: xr.Dataset
    crs: pyproj.CRS
    manifest: dict[str, Any]


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Snowfall amount requires timezone-aware source and interval times")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _row(model: str, valid: str, method: str = "native") -> dict[str, Any]:
    return {
        "model": model,
        "method": method,
        "value": None,
        "unit": "m",
        "native_quantity": QUANTITY,
        "role": "shadow",
        "active_weight": 0.0,
        "status": "unavailable",
        "valid_time": valid,
        "temporal_semantics": "accumulation",
        "interval_start": None,
        "interval_end": None,
        "interval_closure": CLOSURE,
        "missing_reasons": [],
    }


def _source_metadata(view: AmountView, row: dict[str, Any]) -> None:
    for source in (view.manifest.get("source_metadata", {}), view.manifest):
        for key in (
            "source_cycle",
            "provider",
            "product",
            "native_unit",
            "native_parameter",
            "native_semantics",
            "hydrometeor_scope",
            "documentation",
            "version",
            "licence",
            "attribution",
            "manifest_sha256",
            "prepared_file",
        ):
            if key in source:
                row[key] = deepcopy(source[key])


def _event(view: AmountView, valid: str) -> tuple[int, dict[str, Any]]:
    selected = [
        (index, event)
        for index, event in enumerate(view.manifest["events"])
        if event.get("interval_end") and _time(event["interval_end"]) == _time(valid)
    ]
    if len(selected) != 1:
        raise ValueError("No unique native snowfall event ending now; no temporal filling")
    if view.dataset.sizes.get("event") != len(view.manifest["events"]):
        raise ValueError("Prepared snowfall event axis differs from its retained metadata")
    return selected[0]


def _interval(event: dict[str, Any], valid: str) -> None:
    start, end, cycle = (
        _time(event[key]) for key in ("interval_start", "interval_end", "source_cycle")
    )
    if (
        not cycle <= start < end
        or end != _time(valid)
        or cycle + timedelta(hours=event["source_lead_hours"]) != end
        or event.get("valid_time") is not None
        and _time(event["valid_time"]) != end
        or event.get("native_quantity") != QUANTITY
        or event.get("temporal_semantics") != "accumulation"
        or event.get("interval_closure") != CLOSURE
    ):
        raise ValueError(
            "Native snowfall quantity, accumulation interval, source cycle or lead differs"
        )


def _geometry(
    view: AmountView, latitude: float, longitude: float
) -> tuple[np.ndarray, np.ndarray, float, float, CellIndices]:
    x, y = view.dataset.x.values, view.dataset.y.values
    if any(
        len(axis) < 2
        or not np.isfinite(axis).all()
        or not (np.all(np.diff(axis) > 0) or np.all(np.diff(axis) < 0))
        for axis in (x, y)
    ):
        raise ValueError("Snowfall needs finite strictly monotonic native grid axes")
    px, py = project_station_point(view.crs, latitude=latitude, longitude=longitude)
    cell = find_enclosing_cell(x=x, y=y, station_x=px, station_y=py)
    return x, y, px, py, cell


def _values(array: np.ndarray) -> Any:
    result = np.asarray(array, dtype=object).copy()
    result[~np.isfinite(array)] = None
    return result.tolist()


def _native_amount(
    view: AmountView, *, latitude: float, longitude: float, valid: str
) -> dict[str, Any]:
    row = _row(view.manifest["model"], valid)
    _source_metadata(view, row)
    try:
        index, event = _event(view, valid)
        row.update(deepcopy(event))
        row.pop("profile", None)
        row.pop("native_slr", None)
        row.update(value=None, unit="m", role="shadow", active_weight=0.0, status="unavailable")
        row["missing_reasons"] = list(event.get("missing_reasons", []))
        if row["missing_reasons"]:
            return row
        _interval(event, valid)
        if event.get("native_unit") != "m" or event.get("unit_factor_to_m") != 1:
            raise ValueError(
                "Native new snowfall must declare metres and its exact unit conversion"
            )
        ds = view.dataset
        field = ds.amount
        if field.dims != ("event", "y", "x") or field.attrs.get("units") != "m":
            raise ValueError("Prepared new-snow dimensions/units disagree with metadata")
        x, y, px, py, cell = _geometry(view, latitude, longitude)
        indices = np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])
        corners = field.values[index][indices]
        if not np.isfinite(corners).all() or np.any(corners < 0):
            raise ValueError(
                "All four native snowfall corners must be finite/nonnegative; no clipping"
            )
        native = {}
        parents = ["native_end_amount"]
        if event.get("normalization", {}).get("method") == "native_cumulative_end_minus_start":
            if "native_start_amount" not in ds:
                raise ValueError("Native cumulative snowfall requires its retained start parent")
            parents.append("native_start_amount")
        # Concat can add a NaN start-parent plane to an exact first interval. That
        # plane does not represent an acquired parent and must not invalidate f1.
        for name in parents:
            if name in ds:
                parent = ds[name]
                if parent.dims != field.dims or parent.attrs.get("units") != event["native_unit"]:
                    raise ValueError("Native snowfall parent dimensions/units disagree")
                values = parent.values[index][indices]
                if not np.isfinite(values).all() or np.any(values < 0):
                    raise ValueError("Native snowfall parents must be finite/nonnegative")
                native[name] = values
        if native and (
            "native_end_amount" not in native
            or not np.allclose(
                native["native_end_amount"] - native.get("native_start_amount", 0),
                corners,
                rtol=1e-12,
                atol=1e-12,
            )
        ):
            raise ValueError("Native snowfall parents do not reproduce the prepared accumulation")
        extracted = bilinear_interpolate(
            field=field.values[index], x=x, y=y, station_x=px, station_y=py
        )
        spatial = {
            "method": "native_grid_bilinear_accumulation",
            "source_y": [cell.y0, cell.y1],
            "source_x": [cell.x0, cell.x1],
            "weights": [
                extracted.weights.w00,
                extracted.weights.w01,
                extracted.weights.w10,
                extracted.weights.w11,
            ],
            "amount_m_at_source_corners": corners.ravel().tolist(),
            **{
                name + "_at_source_corners": values.ravel().tolist()
                for name, values in native.items()
            },
        }
        row.update(value=extracted.value, status="available", spatial_extraction=spatial)
    except (KeyError, ValueError, TypeError, IndexError, PointExtractionError) as exc:
        row["missing_reasons"].append(f"Native snowfall amount unavailable: {exc}")
    return row


def _instantaneous(metadata: dict[str, Any], event: dict[str, Any], valid: str) -> None:
    if (
        _time(metadata["source_cycle"]) != _time(event["source_cycle"])
        or metadata["source_lead_hours"] != event["source_lead_hours"]
        or _time(metadata["valid_time"]) != _time(valid)
        or _time(metadata["source_cycle"]) + timedelta(hours=metadata["source_lead_hours"])
        != _time(valid)
        or metadata.get("temporal_semantics") != "instantaneous"
        or metadata.get("interval_start") is not None
        or metadata.get("interval_end") is not None
    ):
        raise ValueError(
            "Native diagnostic/profile must use the exact snowfall source cycle and end time"
        )


def _native_ratio(
    view: AmountView | None, *, latitude: float, longitude: float, valid: str
) -> dict[str, Any]:
    row = _row("NBM", valid, "native_slr")
    row.update(
        unit="1",
        native_quantity="snow_to_liquid_ratio",
        temporal_semantics="instantaneous",
        interval_start=None,
        interval_end=None,
        interval_closure=None,
        note="Native NBM ratio retained separately; it is not applied "
        "to another source's snowfall water",
    )
    if view is None:
        row["missing_reasons"] = ["No retained native NBM snow-to-liquid ratio"]
        return row
    _source_metadata(view, row)
    try:
        index, event = _event(view, valid)
        metadata = event["native_slr"]
        row["source_metadata"] = deepcopy(metadata)
        row["native_unit"] = metadata.get("native_unit")
        row.pop("native_parameter", None)
        row.pop("native_semantics", None)
        _instantaneous(metadata, event, valid)
        if metadata.get("unit") != "1":
            raise ValueError("Native NBM ratio metadata must declare a dimensionless ratio")
        if metadata.get("missing_reasons"):
            row["missing_reasons"] = list(metadata["missing_reasons"])
            return row
        field = view.dataset.native_slr
        if field.dims != ("event", "y", "x") or field.attrs.get("units") != "1":
            raise ValueError("Native NBM ratio dimensions/units differ")
        x, y, px, py, cell = _geometry(view, latitude, longitude)
        corners = field.values[index][np.ix_([cell.y0, cell.y1], [cell.x0, cell.x1])]
        if not np.isfinite(corners).all() or np.any(corners < 0):
            raise ValueError("Native NBM ratios must be finite/nonnegative; no clipping")
        extracted = bilinear_interpolate(
            field=field.values[index], x=x, y=y, station_x=px, station_y=py
        )
        row.update(
            value=extracted.value,
            status="available",
            source_cycle=metadata["source_cycle"],
            source_lead_hours=metadata["source_lead_hours"],
            spatial_extraction={
                "method": "native_grid_bilinear_diagnostic_ratio",
                "source_y": [cell.y0, cell.y1],
                "source_x": [cell.x0, cell.x1],
                "native_ratio_at_source_corners": corners.ravel().tolist(),
                "weights": [
                    extracted.weights.w00,
                    extracted.weights.w01,
                    extracted.weights.w10,
                    extracted.weights.w11,
                ],
            },
        )
    except (KeyError, ValueError, TypeError, IndexError, PointExtractionError) as exc:
        row["missing_reasons"].append(f"Native NBM ratio unavailable: {exc}")
    return row


def _kuchera(
    view: AmountView | None,
    swe_views: list[SnowView],
    *,
    latitude: float,
    longitude: float,
    valid: str,
) -> dict[str, Any]:
    row = _row("RAP", valid, "kuchera")
    row.update(
        method_metadata=deepcopy(KUCHERA_METHOD),
        hydrometeor_scope="snow",
        diagnostic_ratio=None,
        diagnostic_ratio_status="not_calculated",
        diagnostic_ratio_note="Interpolated corner ratios are diagnostic only; snowfall uses "
        "bilinear(corner ratio * corner SWE), not the product of two interpolated fields",
        profile_time_approximation="Instantaneous interval-end profile represents this one-hour "
        "SWE interval; no subhourly or precipitation-weighted profile evolution is available",
    )
    if view is None:
        row["missing_reasons"] = ["No retained RAP vertical temperature profile"]
        return row
    _source_metadata(view, row)
    for key in ("native_unit", "native_parameter", "native_semantics"):
        row.pop(key, None)
    try:
        index, event = _event(view, valid)
        metadata = event["profile"]
        row["profile_metadata"] = deepcopy(metadata)
        _instantaneous(metadata, event, valid)
        if metadata.get("missing_reasons"):
            row["missing_reasons"] = list(metadata["missing_reasons"])
            return row
        if (
            metadata.get("complete") is not True
            or metadata.get("temperature_unit") != "K"
            or metadata.get("surface_pressure_unit") != "Pa"
        ):
            raise ValueError("RAP Kuchera requires complete temperature/pressure profile metadata")
        cycle = _time(event["source_cycle"])
        selected = [
            candidate
            for candidate in swe_views
            if candidate.manifest["model"] == "RAP"
            and candidate.crs == view.crs
            and all(
                np.array_equal(candidate.dataset[axis], view.dataset[axis]) for axis in ("x", "y")
            )
        ]
        eligible = []
        for candidate in selected:
            extracted_swe = extract_snowfall_contributors(
                [candidate], latitude=latitude, longitude=longitude, valid_time=valid
            )
            source = next(item for item in extracted_swe["contributors"] if item["model"] == "RAP")
            if (
                source.get("source_cycle")
                and _time(source["source_cycle"]) == cycle
                and source.get("interval_start")
                and _time(source["interval_start"]) == _time(valid) - timedelta(hours=1)
                and source.get("interval_end")
                and _time(source["interval_end"]) == _time(valid)
            ):
                eligible.append(source)
        if len(eligible) != 1:
            raise ValueError(
                "No unique same-cycle RAP SWE interval and identical native grid for this profile"
            )
        swe = eligible[0]
        row.update(
            interval_start=swe["interval_start"],
            interval_end=swe["interval_end"],
            source_cycle=swe["source_cycle"],
            source_lead_hours=swe["source_lead_hours"],
            provenance={
                "snowfall_water_equivalent": {
                    key: deepcopy(swe.get(key))
                    for key in (
                        "provenance",
                        "manifest_sha256",
                        "prepared_file",
                        "normalization",
                        "native_unit",
                    )
                },
                "temperature_profile": deepcopy(metadata),
            },
        )
        if swe["status"] != "available":
            row["missing_reasons"] = [
                "RAP snowfall water equivalent unavailable",
                *swe["missing_reasons"],
            ]
            return row
        ds = view.dataset
        if not np.array_equal(ds.level.values, metadata["level_hpa"]):
            raise ValueError("RAP profile levels differ from their retained source metadata")
        for name, dims, unit in (
            ("temperature_profile", ("event", "level", "y", "x"), "K"),
            ("temperature_2m", ("event", "y", "x"), "K"),
            ("surface_pressure", ("event", "y", "x"), "Pa"),
        ):
            if ds[name].dims != dims or ds[name].attrs.get("units") != unit:
                raise ValueError("RAP profile dimensions/units disagree with retained metadata")
        x, y, px, py, cell = _geometry(view, latitude, longitude)
        ys, xs = [cell.y0, cell.y1], [cell.x0, cell.x1]
        profile = ds.temperature_profile.values[index][np.ix_(np.arange(ds.sizes["level"]), ys, xs)]
        t2m = ds.temperature_2m.values[index][np.ix_(ys, xs)]
        pressure = ds.surface_pressure.values[index][np.ix_(ys, xs)]
        water = np.asarray(swe["spatial_extraction"]["amount_kg_m2_at_source_corners"]).reshape(
            2, 2
        )
        result = kuchera_ratio(profile, ds.level.values, t2m, pressure)
        row["spatial_extraction"] = {
            "method": "kuchera_at_each_native_corner_then_bilinear_amount",
            "source_y": ys,
            "source_x": xs,
            "pressure_levels_hpa": ds.level.values.tolist(),
            "temperature_profile_k_at_source_corners": _values(profile.reshape(len(ds.level), 4)),
            "temperature_2m_k_at_source_corners": _values(t2m.ravel()),
            "surface_pressure_pa_at_source_corners": _values(pressure.ravel()),
            "aboveground_mask_at_source_corners": result.aboveground_mask.reshape(
                len(ds.level), 4
            ).tolist(),
            "maximum_temperature_k_at_source_corners": _values(
                result.maximum_temperature_k.ravel()
            ),
            "ratio_at_source_corners": _values(result.ratio.ravel()),
            "ratio_status_at_source_corners": np.where(
                ~result.valid_profile,
                "unavailable_profile",
                np.where(
                    result.ratio > 0,
                    "positive_ratio",
                    np.where(water == 0, "inapplicable_zero_swe", "ineligible_nonpositive_ratio"),
                ),
            )
            .ravel()
            .tolist(),
            "swe_kg_m2_at_source_corners": water.ravel().tolist(),
        }
        if not result.valid_profile.all():
            raise ValueError(
                "; ".join(sorted(set(result.missing_reasons[~result.valid_profile].ravel())))
            )
        if np.any((water > 0) & (result.ratio <= 0)):
            raise ValueError(
                "Nonpositive Kuchera ratio for positive native snowfall water equivalent; "
                "no clipping"
            )
        amount = np.where(water == 0, 0, result.ratio * water / 1000.0)
        if not np.isfinite(amount).all() or np.any(amount < 0):
            raise ValueError("Derived snowfall amount is nonfinite/negative")
        extracted = bilinear_interpolate(field=amount, x=x[xs], y=y[ys], station_x=px, station_y=py)
        ratio = bilinear_interpolate(
            field=result.ratio, x=x[xs], y=y[ys], station_x=px, station_y=py
        )
        row["spatial_extraction"].update(
            amount_m_at_source_corners=amount.ravel().tolist(),
            weights=[
                extracted.weights.w00,
                extracted.weights.w01,
                extracted.weights.w10,
                extracted.weights.w11,
            ],
            zero_swe_nonpositive_ratio_corners=np.flatnonzero(
                (water == 0) & (result.ratio <= 0)
            ).tolist(),
        )
        row.update(
            value=extracted.value,
            status="available",
            diagnostic_ratio=ratio.value,
            diagnostic_ratio_status="raw_formula_only_contains_nonpositive_inapplicable_ratios"
            if np.any(result.ratio <= 0)
            else "positive_diagnostic_only",
        )
    except (KeyError, ValueError, TypeError, IndexError, PointExtractionError) as exc:
        row["missing_reasons"].append(f"RAP Kuchera snowfall unavailable: {exc}")
    return row


def _region(
    views: list[AmountView], model: str, latitude: float, longitude: float
) -> AmountView | None:
    candidates = [view for view in views if view.manifest["model"] == model]
    return next(
        (
            view
            for view in candidates
            if point_in_grid(
                latitude, longitude, view.crs, view.dataset.x.values, view.dataset.y.values
            )
        ),
        candidates[0] if candidates else None,
    )


def extract_snowfall_amount_contributors(
    views: list[AmountView],
    *,
    swe_views: list[SnowView],
    latitude: float,
    longitude: float,
    valid_time: str,
    source_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep native amounts, native ratios and Kuchera estimates distinct; no active recipe."""
    validate_coordinate(latitude, longitude)
    target = _time(valid_time)
    native = []
    statuses = source_status or {}
    for model in dict.fromkeys(
        ("HRRR", "GFS", "RAP", "IFS", "NBM", *(v.manifest["model"] for v in views), *statuses)
    ):
        view = _region(views, model, latitude, longitude)
        if view is None:
            row = _row(model, valid_time)
            row.update(deepcopy(statuses.get(model, {})))
            row.update(value=None, unit="m", role="shadow", active_weight=0.0, status="unavailable")
            row["missing_reasons"] = row.get("missing_reasons") or [
                row.get("missing_reason") or "No retained native new-snowfall amount"
            ]
        else:
            row = _native_amount(view, latitude=latitude, longitude=longitude, valid=valid_time)
        native.append(row)
    derived = [
        _kuchera(
            _region(views, "RAP", latitude, longitude),
            swe_views,
            latitude=latitude,
            longitude=longitude,
            valid=valid_time,
        )
    ]
    ratios = [
        _native_ratio(
            _region(views, "NBM", latitude, longitude),
            latitude=latitude,
            longitude=longitude,
            valid=valid_time,
        )
    ]
    comparisons = []
    for left, right in combinations([*native, *derived], 2):
        reasons = []
        if any(row["status"] != "available" for row in (left, right)):
            reasons.append("Both snowfall amount estimates must be available")
        elif (
            any(_time(left[key]) != _time(right[key]) for key in ("interval_start", "interval_end"))
            or left["unit"] != right["unit"]
            or left["native_quantity"] != right["native_quantity"]
        ):
            reasons.append("Native snowfall intervals, units or quantities differ")
        elif (
            not left.get("hydrometeor_scope")
            or not right.get("hydrometeor_scope")
            or left["hydrometeor_scope"] != right["hydrometeor_scope"]
        ):
            reasons.append(
                "Snowfall hydrometeor scopes differ or are unspecified; "
                "snow and snow-plus-sleet are not identical quantities"
            )
        comparisons.append(
            {
                "models": [left["model"], right["model"]],
                "methods": [left["method"], right["method"]],
                "hydrometeor_scopes": [
                    left.get("hydrometeor_scope"),
                    right.get("hydrometeor_scope"),
                ],
                "status": "incompatible" if reasons else "comparable",
                "unit": "m",
                "difference_left_minus_right": None if reasons else left["value"] - right["value"],
                "interval_start": None if reasons else left["interval_start"],
                "interval_end": None if reasons else left["interval_end"],
                "missing_reasons": reasons,
                "interpretation": "Descriptive estimates with different native processing; "
                "no skill or blend inference",
            }
        )
    for row in (*native, *ratios, *derived):
        row["extraction_coordinate"] = {"latitude": latitude, "longitude": longitude}
    return {
        "field": {
            **_row("MesoForge", valid_time, "no_approved_blend"),
            "weights": {},
            "status": "policy_unavailable",
            "interval_start": _iso(target - timedelta(hours=1)),
            "interval_end": _iso(target),
            "interval_role": "nominal_requested_hour_not_an_active_amount",
            "missing_reasons": [
                "No approved snowfall-amount blend policy; native and Kuchera estimates "
                "remain separate shadows"
            ],
        },
        "native_contributors": native,
        "native_slr": ratios,
        "derived_contributors": derived,
        "comparisons": comparisons,
    }
