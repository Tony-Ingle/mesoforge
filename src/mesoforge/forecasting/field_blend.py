"""Current V2 field-policy registry and one scientific dispatch boundary.

Policies remain the existing immutable recipe/table objects. The dispatcher owns
field orchestration, not a universal blending equation or a new policy format.
Native evidence is read-only; only per-hour dependency results are cached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from mesoforge.catalog.configuration import (
    FallbackWeightRow,
    FallbackWeightTable,
    Phase2BlendConfiguration,
)
from mesoforge.forecasting.gust_blend import (
    GustDisqualificationError,
    blend_gust,
    validate_source_gust,
)
from mesoforge.forecasting.precipitation_blend import blend_qpf
from mesoforge.forecasting.recipes import ContributorConfiguration, Recipe, evaluate_recipe
from mesoforge.forecasting.scalar_blend import (
    ConsistencyError,
    Contribution,
    blend_scalar,
    check_dew_point_consistency,
)
from mesoforge.forecasting.surface import (
    RH_POLICY,
    RH_SOURCE,
    SurfaceBlendError,
    _usable,
    relative_humidity_percent,
)
from mesoforge.forecasting.vector_blend import blend_vector

TEMPERATURE = "air_temperature_2m"
DEW_POINT = "dew_point_temperature_2m"
WIND = "wind_10m"
GUST = "wind_gust_10m"
QPF = "liquid_equivalent_precipitation_amount_1h"
RH = "relative_humidity_2m"
_U = "eastward_wind_10m"
_V = "northward_wind_10m"
_ACTIVE_MODELS = ("HRRR", "GFS")
_TABLE_MODEL_ORDER = ("HRRR", "NBM", "GFS")


@dataclass(frozen=True)
class FieldDefinition:
    """Only metadata required by today's field-specific execution."""

    field_id: str
    semantic_kind: Literal["scalar", "vector", "gust", "accumulation", "derived"]
    output_units: str
    execution: str
    policy_binding: Literal["control_recipe", "scalar_vector_table", "qpf_table", "rh"]
    missing_behavior: str
    dependencies: tuple[str, ...] = ()


FIELD_REGISTRY = MappingProxyType(
    {
        TEMPERATURE: FieldDefinition(
            TEMPERATURE, "scalar", "K", "_temperature", "control_recipe", "require_all"
        ),
        DEW_POINT: FieldDefinition(
            DEW_POINT,
            "scalar",
            "K",
            "_dew_point",
            "scalar_vector_table",
            "approved_subset_row",
            (TEMPERATURE,),
        ),
        RH: FieldDefinition(
            RH,
            "derived",
            "%",
            "_humidity",
            "rh",
            "require_consistent_temperature_and_dew_point",
            (TEMPERATURE, DEW_POINT),
        ),
        WIND: FieldDefinition(
            WIND,
            "vector",
            "m/s; degree",
            "_wind",
            "scalar_vector_table",
            "approved_subset_row_after_coupled_wind_gust_qc",
        ),
        GUST: FieldDefinition(
            GUST,
            "gust",
            "m/s",
            "_gust",
            "scalar_vector_table",
            "same_validated_subset_as_wind",
            (WIND,),
        ),
        QPF: FieldDefinition(
            QPF,
            "accumulation",
            "kg/m^2",
            "_qpf",
            "qpf_table",
            "approved_subset_row_for_exact_hourly_intervals",
        ),
    }
)


@dataclass
class _Validated:
    dew: dict[str, float]
    u: dict[str, float]
    v: dict[str, float]
    gust: dict[str, float]
    dew_reasons: list[str]
    wind_reasons: list[str]


@dataclass
class BlendState:
    """Extracted values at one point/valid hour; never shared across hours/cells.

    The caller retains the full native provenance records. No input values are
    edited by dispatch, including working gust floors and source exclusions.
    """

    horizon: int
    contributors: dict[str, dict[str, float | None]]
    precipitation: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    source_validation: dict[str, Any] = field(default_factory=dict, init=False)
    validated: _Validated | None = field(default=None, init=False)


def _weights(row: FallbackWeightRow | None) -> dict[str, float]:
    return (
        {
            model: weight
            for model, weight in zip(_TABLE_MODEL_ORDER, row.weights, strict=True)
            if weight > 0
        }
        if row
        else {}
    )


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
        "weights": _weights(row),
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


def _validate_sources(state: BlendState, configuration: Phase2BlendConfiguration) -> _Validated:
    source_validation = state.source_validation
    dew_values: dict[str, float] = {}
    u_values: dict[str, float] = {}
    v_values: dict[str, float] = {}
    gust_values: dict[str, float] = {}
    dew_reasons: list[str] = []
    wind_reasons: list[str] = []
    for model in sorted(set(state.contributors) | set(_ACTIVE_MODELS)):
        source = state.contributors.get(model, {})
        source_temperature = source.get(TEMPERATURE)
        dew = source.get(DEW_POINT)
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

        u, v, gust = source.get(_U), source.get(_V), source.get(GUST)
        source_wind_reasons = [
            f"{model}: {variable} missing/nonfinite/outside canonical bounds"
            for variable, value, low, high in (
                (_U, u, -100.0, 100.0),
                (_V, v, -100.0, 100.0),
                (GUST, gust, 0.0, configuration.gust_policy.valid_max_m_s),
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

    state.validated = _Validated(
        dew_values, u_values, v_values, gust_values, dew_reasons, wind_reasons
    )
    return state.validated


@dataclass(frozen=True)
class FieldBlendEngine:
    """Dispatch the active V2 fields using existing versioned policy definitions.

    A state belongs to this engine for one immutable set of extracted inputs.
    Dependencies are explicit calls in the few specialized handlers, not a new
    graph/coherence framework. Temperature-only historical prepared data needs
    no Phase 2 configuration.
    """

    contributors: ContributorConfiguration
    phase2: Phase2BlendConfiguration | None = None

    def policy_for(self, field_id: str) -> Recipe | FallbackWeightTable | str:
        definition = FIELD_REGISTRY[field_id]
        if definition.policy_binding == "control_recipe":
            return self.contributors.control_recipe
        if definition.policy_binding == "rh":
            return RH_POLICY
        if self.phase2 is None:
            raise SurfaceBlendError(f"{field_id}: prepared Phase 2 policy configuration required")
        return (
            self.phase2.scalar_vector_table
            if definition.policy_binding == "scalar_vector_table"
            else self.phase2.qpf_table
        )

    def blend_field(self, field_id: str, state: BlendState) -> dict[str, Any]:
        """Produce one field from aligned native evidence, caching dependencies."""
        definition = FIELD_REGISTRY[field_id]
        if field_id != TEMPERATURE and (
            isinstance(state.horizon, bool) or state.horizon not in range(1, 37)
        ):
            raise SurfaceBlendError("surface forecast horizon must be an integer from 1 through 36")
        if field_id not in state.results:
            state.results[field_id] = getattr(self, definition.execution)(state)
        return state.results[field_id]

    def _sources(self, state: BlendState) -> _Validated:
        if self.phase2 is None:
            raise SurfaceBlendError("Prepared Phase 2 policy configuration required")
        if state.validated is None:
            return _validate_sources(state, self.phase2)
        return state.validated

    def _row(
        self, field_id: str, values: dict[str, float], state: BlendState
    ) -> FallbackWeightRow | None:
        table = self.policy_for(field_id)
        assert isinstance(table, FallbackWeightTable)
        return (
            table.row_for(available_models=tuple(values), horizon=state.horizon) if values else None
        )

    def _temperature(self, state: BlendState) -> dict[str, Any]:
        recipe = self.policy_for(TEMPERATURE)
        assert isinstance(recipe, Recipe)
        evaluation = evaluate_recipe(
            recipe, {model: values.get(TEMPERATURE) for model, values in state.contributors.items()}
        )
        result = _field(evaluation.value, "K", policy=recipe.name)
        result.update(
            weights={item.model: item.weight for item in recipe.contributors},
            policy_version=recipe.version,
            missing_models=list(evaluation.missing_models),
        )
        return result

    def _dew_point(self, state: BlendState) -> dict[str, Any]:
        sources = self._sources(state)
        reasons = list(sources.dew_reasons)
        row = self._row(DEW_POINT, sources.dew, state)
        value = blend_scalar(_contributions(sources.dew, row)).blended_value if row else None
        temperature = self.blend_field(TEMPERATURE, state)["value"]
        status = None
        if value is not None:
            if not _usable(temperature, 150.0, 340.0):
                value = None
                reasons.append("active temperature unavailable for blended dew-point consistency")
            else:
                try:
                    check_dew_point_consistency(temperature_k=temperature, dew_point_k=value)
                except ConsistencyError as exc:
                    value = None
                    status = "inconsistent"
                    reasons.append(str(exc))
        table = self.policy_for(DEW_POINT)
        assert isinstance(table, FallbackWeightTable)
        return _field(value, "K", policy=table.table_id, reasons=reasons, row=row, status=status)

    def _humidity(self, state: BlendState) -> dict[str, Any]:
        temperature = self.blend_field(TEMPERATURE, state)["value"]
        dew = self.blend_field(DEW_POINT, state)["value"]
        value = None
        reasons: list[str] = []
        if _usable(temperature, 150.0, 340.0) and dew is not None:
            try:
                value = relative_humidity_percent(temperature_k=temperature, dew_point_k=dew)
            except SurfaceBlendError as exc:
                reasons.append(str(exc))
        else:
            reasons.append("RH requires available, consistent baseline temperature and dew point")
        result = _field(value, "%", policy=RH_POLICY, reasons=reasons)
        result.update(
            derived_from=[TEMPERATURE, DEW_POINT],
            saturation_reference="liquid_water",
            source=RH_SOURCE,
        )
        return result

    def _wind(self, state: BlendState) -> dict[str, Any]:
        sources = self._sources(state)
        row = self._row(WIND, sources.u, state)
        values: dict[str, float | None] = dict.fromkeys(
            (_U, _V, "wind_speed_10m", "wind_from_direction_10m")
        )
        if row is not None:
            vector = blend_vector(
                eastward_contributions=_contributions(sources.u, row),
                northward_contributions=_contributions(sources.v, row),
            )
            values.update(
                {
                    _U: vector.eastward_m_s,
                    _V: vector.northward_m_s,
                    "wind_speed_10m": vector.speed_m_s,
                    "wind_from_direction_10m": vector.direction_degrees,
                }
            )
        table = self.policy_for(WIND)
        assert isinstance(table, FallbackWeightTable)
        result = {}
        for variable, value in values.items():
            reasons = list(sources.wind_reasons)
            if variable == "wind_from_direction_10m" and row is not None and value is None:
                reasons.append("wind direction undefined for exactly calm blended wind")
            result[variable] = _field(
                value,
                "degree" if variable == "wind_from_direction_10m" else "m/s",
                policy=table.table_id,
                reasons=reasons,
                row=row,
            )
        return result

    def _gust(self, state: BlendState) -> dict[str, Any]:
        sources = self._sources(state)
        wind = self.blend_field(WIND, state)
        row = self._row(GUST, sources.u, state)
        value = None
        final_floor = False
        if row is not None:
            assert self.phase2 is not None
            gust = blend_gust(
                contributions=_contributions(sources.gust, row),
                blended_sustained_speed_m_s=wind["wind_speed_10m"]["value"],
                final_epsilon_floor_m_s=self.phase2.gust_policy.final_epsilon_floor_m_s,
                valid_max_m_s=self.phase2.gust_policy.valid_max_m_s,
            )
            value = gust.blended_gust_m_s
            final_floor = gust.final_gust_epsilon_floor_applied
        table = self.policy_for(GUST)
        assert isinstance(table, FallbackWeightTable)
        result = _field(
            value, "m/s", policy=table.table_id, reasons=list(sources.wind_reasons), row=row
        )
        result["final_gust_epsilon_floor_applied"] = final_floor
        return result

    def _qpf(self, state: BlendState) -> dict[str, Any]:
        # Extraction has already enforced exact one-hour bounds, units, finite
        # native corners and retained parent-message evidence. Do not rebin here.
        native = state.precipitation
        metadata_keys = (
            "unit",
            "temporal_semantics",
            "interval_start",
            "interval_end",
            "interval_closure",
        )
        result = {key: native["HRRR"][key] for key in metadata_keys}
        usable = {}
        reasons = []
        for model in _ACTIVE_MODELS:
            field_value = native[model]
            reasons.extend(field_value["missing_reasons"])
            if field_value["value"] is not None:
                if any(field_value[key] != result[key] for key in metadata_keys):
                    reasons.append(f"{model}: incompatible QPF accumulation metadata")
                else:
                    usable[model] = field_value["value"]
        row = self._row(QPF, usable, state)
        weights = _weights(row)
        value = blend_qpf(_contributions(usable, row)) if row else None
        table = self.policy_for(QPF)
        assert isinstance(table, FallbackWeightTable)
        result.update(
            value=value,
            weights=weights,
            policy=table.table_id,
            row_id=str(row.row_id) if row else None,
            row_sha256=str(row.digest) if row else None,
            status="unavailable"
            if value is None
            else "available"
            if len(usable) == 2
            else "fallback",
            missing_reasons=reasons,
        )
        return result

    def surface_fields(self, state: BlendState) -> dict[str, Any]:
        """Serialize dispatched fields using the existing issued-grid shape.

        The legacy temperature projection label/bounds remain distinct from the
        actual versioned recipe recorded on the containing forecast. Neither this
        projection nor the historical cloud placeholder is a second blend path.
        """
        fields = {}
        for field_id in FIELD_REGISTRY:
            if field_id == QPF and not state.precipitation:
                continue
            value = self.blend_field(field_id, state)
            if field_id == WIND:
                fields.update(value)
            elif field_id == TEMPERATURE:
                temperature = value["value"]
                valid = _usable(temperature, 150.0, 340.0)
                fields[field_id] = _field(
                    temperature if valid else None,
                    "K",
                    policy="unchanged-temperature-control",
                    reasons=[] if valid else ["active temperature baseline unavailable"],
                )
            else:
                fields[field_id] = value
        fields["cloud_area_fraction"] = _field(
            None,
            "1",
            policy="no-approved-cloud-blend-policy",
            reasons=["cloud/sky cover has no retained approved normalization and blend policy"],
        )
        return fields
