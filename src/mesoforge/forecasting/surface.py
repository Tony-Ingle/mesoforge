"""Surface fields from the retained Phase 2 operators and approved fallback rows.

Temperature is supplied by the unchanged temperature control. RH is diagnostic
relative humidity over liquid water (including below freezing), using the Bolton
equation documented by UCAR CEOP:
https://archive.eol.ucar.edu/projects/ceop/dm/documents/refdata_report/eqns.html
It is neither a separately weighted model RH nor humidity relative to ice.
"""

from __future__ import annotations

import math
from typing import Any

from mesoforge.catalog.configuration import FallbackWeightRow, Phase2BlendConfiguration
from mesoforge.common.errors import MesoForgeError
from mesoforge.forecasting.gust_blend import (
    GustDisqualificationError,
    blend_gust,
    validate_source_gust,
)
from mesoforge.forecasting.scalar_blend import (
    ConsistencyError,
    Contribution,
    blend_scalar,
    check_dew_point_consistency,
)
from mesoforge.forecasting.vector_blend import blend_vector

_ACTIVE_MODELS = ("HRRR", "GFS")
_TABLE_MODEL_ORDER = ("HRRR", "NBM", "GFS")
_TEMPERATURE = "air_temperature_2m"
_DEW_POINT = "dew_point_temperature_2m"
_U = "eastward_wind_10m"
_V = "northward_wind_10m"
_GUST = "wind_gust_10m"
RH_POLICY = "bolton-1980-relative-humidity-liquid-water.v1"
RH_SOURCE = "https://archive.eol.ucar.edu/projects/ceop/dm/documents/refdata_report/eqns.html"


class SurfaceBlendError(MesoForgeError, ValueError):
    """A diagnostic cannot be calculated from scientifically valid inputs."""


def _usable(value: float | None, lower: float, upper: float) -> bool:
    return (
        value is not None
        and not isinstance(value, bool)
        and math.isfinite(value)
        and lower <= value <= upper
    )


def relative_humidity_percent(*, temperature_k: float, dew_point_k: float) -> float:
    """Return 100 e(Td)/es(T), the Bolton liquid-water approximation.

    Canonical Phase 2 temperature bounds apply. In degrees Celsius,
    e(x) = 6.112 exp(17.67 x / (x + 243.5)) hPa. The identical 6.112
    factor cancels in this ratio. Invalid or supersaturated results are
    rejected, never clipped to 100 percent.
    """
    if not _usable(temperature_k, 150.0, 340.0) or not _usable(dew_point_k, 150.0, 340.0):
        raise SurfaceBlendError("RH requires finite temperature/dew point within [150, 340] K")
    if dew_point_k > temperature_k:
        raise SurfaceBlendError("RH unavailable: dew point exceeds temperature")
    temperature_c = temperature_k - 273.15
    dew_point_c = dew_point_k - 273.15
    humidity = 100.0 * math.exp(
        17.67 * dew_point_c / (dew_point_c + 243.5)
        - 17.67 * temperature_c / (temperature_c + 243.5)
    )
    if not math.isfinite(humidity) or not 0.0 <= humidity <= 100.0:
        raise SurfaceBlendError("RH diagnostic is outside [0, 100] percent")
    return humidity


def _field(
    value: float | None,
    unit: str,
    *,
    policy: str,
    reasons: list[str] | None = None,
    row: FallbackWeightRow | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "missing_reasons": reasons or [],
        "status": status
        or ("unavailable" if value is None else "fallback" if row else "available"),
        "weights": (
            {
                model: weight
                for model, weight in zip(_TABLE_MODEL_ORDER, row.weights, strict=True)
                if weight > 0
            }
            if row
            else {}
        ),
        "policy": policy,
        "row_id": str(row.row_id) if row else None,
        "row_sha256": str(row.digest) if row else None,
    }


def _contributions(values: dict[str, float], row: FallbackWeightRow) -> tuple[Contribution, ...]:
    return tuple(
        Contribution(model=model, value=values[model], weight=weight)
        for model, weight in zip(_TABLE_MODEL_ORDER, row.weights, strict=True)
        if weight > 0
    )


def blend_surface(
    *,
    temperature_k: float | None,
    contributors: dict[str, dict[str, float | None]],
    horizon: int,
    configuration: Phase2BlendConfiguration,
) -> dict[str, Any]:
    """Blend new fields without mutating contributors or recalculating temperature.

    Only HRRR/GFS enter the approved Phase 2 fallback table. RAP/IFS retain
    their native values in the caller's contributor record and have no active
    influence. U/V/gust share one validated contributor set and one weight row.
    """
    if isinstance(horizon, bool) or horizon not in range(1, 37):
        raise SurfaceBlendError("surface forecast horizon must be an integer from 1 through 36")
    table = configuration.scalar_vector_table
    policy = table.table_id
    source_validation: dict[str, Any] = {}
    dew_values: dict[str, float] = {}
    u_values: dict[str, float] = {}
    v_values: dict[str, float] = {}
    gust_values: dict[str, float] = {}
    dew_reasons: list[str] = []
    wind_reasons: list[str] = []
    for model in sorted(set(contributors) | set(_ACTIVE_MODELS)):
        source = contributors.get(model, {})
        source_temperature = source.get(_TEMPERATURE)
        dew = source.get(_DEW_POINT)
        source_dew_reasons: list[str] = []
        if not _usable(dew, 150.0, 340.0):
            source_dew_reasons.append(f"{model}: dew point missing/nonfinite/outside [150, 340] K")
        elif not _usable(source_temperature, 150.0, 340.0):
            source_dew_reasons.append(f"{model}: temperature unavailable for dew-point consistency")
        else:
            assert source_temperature is not None and dew is not None
            try:
                check_dew_point_consistency(temperature_k=source_temperature, dew_point_k=dew)
            except ConsistencyError as exc:
                source_dew_reasons.append(f"{model}: {exc}")
        if model in _ACTIVE_MODELS:
            dew_reasons.extend(source_dew_reasons)
            if not source_dew_reasons:
                assert dew is not None
                dew_values[model] = dew

        u, v, gust = source.get(_U), source.get(_V), source.get(_GUST)
        source_wind_reasons = [
            f"{model}: {variable} missing/nonfinite/outside canonical bounds"
            for variable, value, low, high in (
                (_U, u, -100.0, 100.0),
                (_V, v, -100.0, 100.0),
                (_GUST, gust, 0.0, configuration.gust_policy.valid_max_m_s),
            )
            if not _usable(value, low, high)
        ]
        validated_gust: float | None = None
        source_floor = False
        sustained = math.hypot(u, v) if u is not None and v is not None else None
        if not source_wind_reasons:
            assert gust is not None and sustained is not None
            try:
                validation = validate_source_gust(
                    gust_m_s=gust,
                    sustained_speed_m_s=sustained,
                    shortfall_floor_tolerance_m_s=(
                        configuration.gust_policy.shortfall_floor_tolerance_m_s
                    ),
                )
                validated_gust = validation.validated_gust_m_s
                source_floor = validation.source_gust_floor_applied
            except GustDisqualificationError as exc:
                source_wind_reasons.append(f"{model}: {exc}")
        if model in _ACTIVE_MODELS:
            wind_reasons.extend(source_wind_reasons)
            if not source_wind_reasons:
                assert u is not None and v is not None and validated_gust is not None
                u_values[model], v_values[model], gust_values[model] = u, v, validated_gust
        source_validation[model] = {
            "dew_point": {
                "status": "rejected" if source_dew_reasons else "accepted",
                "missing_reasons": source_dew_reasons,
            },
            "wind_gust": {
                "status": "rejected" if source_wind_reasons else "accepted",
                "missing_reasons": source_wind_reasons,
                "rejection_scope": "coupled-wind-gust-point" if source_wind_reasons else None,
                "source_gust_m_s": gust if _usable(gust, 0.0, 100.0) else None,
                "source_sustained_speed_m_s": (
                    sustained if sustained is not None and math.isfinite(sustained) else None
                ),
                "validated_gust_m_s": validated_gust,
                "source_gust_floor_applied": source_floor,
            },
        }

    temperature_valid = _usable(temperature_k, 150.0, 340.0)
    fields = {
        _TEMPERATURE: _field(
            temperature_k if temperature_valid else None,
            "K",
            policy="unchanged-temperature-control",
            reasons=[] if temperature_valid else ["active temperature baseline unavailable"],
        )
    }
    dew_row = (
        table.row_for(available_models=tuple(dew_values), horizon=horizon) if dew_values else None
    )
    blended_dew = (
        blend_scalar(_contributions(dew_values, dew_row)).blended_value if dew_row else None
    )
    dew_status = None
    if blended_dew is not None:
        if not temperature_valid:
            blended_dew = None
            dew_reasons.append("active temperature unavailable for blended dew-point consistency")
        else:
            assert temperature_k is not None
            try:
                check_dew_point_consistency(temperature_k=temperature_k, dew_point_k=blended_dew)
            except ConsistencyError as exc:
                blended_dew = None
                dew_status = "inconsistent"
                dew_reasons.append(str(exc))
    fields[_DEW_POINT] = _field(
        blended_dew, "K", policy=policy, reasons=dew_reasons, row=dew_row, status=dew_status
    )
    humidity: float | None = None
    humidity_reasons: list[str] = []
    if temperature_valid and blended_dew is not None:
        assert temperature_k is not None
        try:
            humidity = relative_humidity_percent(
                temperature_k=temperature_k, dew_point_k=blended_dew
            )
        except SurfaceBlendError as exc:
            humidity_reasons.append(str(exc))
    else:
        humidity_reasons.append(
            "RH requires available, consistent baseline temperature and dew point"
        )
    fields["relative_humidity_2m"] = _field(
        humidity, "%", policy=RH_POLICY, reasons=humidity_reasons
    )
    fields["relative_humidity_2m"].update(
        derived_from=[_TEMPERATURE, _DEW_POINT],
        saturation_reference="liquid_water",
        source=RH_SOURCE,
    )

    wind_row = (
        table.row_for(available_models=tuple(u_values), horizon=horizon) if u_values else None
    )
    wind_values: dict[str, float | None] = dict.fromkeys(
        (_U, _V, "wind_speed_10m", "wind_from_direction_10m", _GUST)
    )
    final_floor = False
    if wind_row is not None:
        vector = blend_vector(
            eastward_contributions=_contributions(u_values, wind_row),
            northward_contributions=_contributions(v_values, wind_row),
        )
        gust_result = blend_gust(
            contributions=_contributions(gust_values, wind_row),
            blended_sustained_speed_m_s=vector.speed_m_s,
            final_epsilon_floor_m_s=configuration.gust_policy.final_epsilon_floor_m_s,
            valid_max_m_s=configuration.gust_policy.valid_max_m_s,
        )
        final_floor = gust_result.final_gust_epsilon_floor_applied
        wind_values.update(
            {
                _U: vector.eastward_m_s,
                _V: vector.northward_m_s,
                "wind_speed_10m": vector.speed_m_s,
                "wind_from_direction_10m": vector.direction_degrees,
                _GUST: gust_result.blended_gust_m_s,
            }
        )
    for variable, value in wind_values.items():
        reasons = list(wind_reasons)
        if variable == "wind_from_direction_10m" and wind_row is not None and value is None:
            reasons.append("wind direction undefined for exactly calm blended wind")
        fields[variable] = _field(
            value,
            "degree" if variable == "wind_from_direction_10m" else "m/s",
            policy=policy,
            reasons=reasons,
            row=wind_row,
        )
    fields[_GUST]["final_gust_epsilon_floor_applied"] = final_floor
    fields["cloud_area_fraction"] = _field(
        None,
        "1",
        policy="no-approved-cloud-blend-policy",
        reasons=["cloud/sky cover has no retained approved normalization and blend policy"],
    )
    return {"fields": fields, "source_validation": source_validation}
