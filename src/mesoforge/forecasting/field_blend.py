"""Current V2 field-policy registry and one scientific dispatch boundary.

Policies remain the existing immutable recipe/table objects. The dispatcher owns
field orchestration, not a universal blending equation or a new policy format.
Native evidence is read-only; only per-hour dependency results are cached.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any, Literal

from mesoforge.catalog.configuration import (
    FallbackWeightRow,
    FallbackWeightTable,
    Phase2BlendConfiguration,
)
from mesoforge.catalog.native_horizons import native_field_contract
from mesoforge.forecasting.cloud_cover import CLOUD, SKY_CATEGORY_POLICY, sky_category
from mesoforge.forecasting.coherence import (
    BASELINE_COHERENCE,
    DEW_POINT,
    GUST,
    QPF,
    RH,
    TEMPERATURE,
    WIND,
    CoherenceError,
    ValidatedSources,
)
from mesoforge.forecasting.gust_blend import GustInvariantError, blend_gust
from mesoforge.forecasting.precipitation_blend import blend_qpf
from mesoforge.forecasting.provisional_policy import (
    POP6,
    POP6_THRESHOLD,
    PROVISIONAL_MODELS,
    PROVISIONAL_MULTIMODEL_POLICY,
    ProvisionalFieldPolicy,
    aware_time,
    provisional_policy,
    provisional_weights,
)
from mesoforge.forecasting.recipes import ContributorConfiguration, Recipe, evaluate_recipe
from mesoforge.forecasting.scalar_blend import (
    Contribution,
    blend_scalar,
)
from mesoforge.forecasting.surface import (
    RH_POLICY,
    SurfaceBlendError,
    _usable,
)
from mesoforge.forecasting.vector_blend import blend_vector

_U = "eastward_wind_10m"
_V = "northward_wind_10m"
_ACTIVE_MODELS = ("HRRR", "GFS")
_TABLE_MODEL_ORDER = ("HRRR", "NBM", "GFS")


@dataclass(frozen=True)
class FieldEditContract:
    """Local editor capabilities; absence of a contract means inspection only.

    Bounds describe the existing field contract, not model skill or a materiality
    threshold. Native contributor records are never editing targets.
    """

    inspectable: bool = True
    operations: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    validation: tuple[str, ...] = ()
    intervention_limits: tuple[tuple[str, float, float], ...] = ()


@dataclass(frozen=True)
class FieldDefinition:
    """Only metadata required by today's field-specific execution."""

    field_id: str
    semantic_kind: Literal["scalar", "vector", "gust", "accumulation", "probability", "derived"]
    output_units: str
    execution: str
    policy_binding: Literal[
        "control_recipe",
        "scalar_vector_table",
        "qpf_table",
        "rh",
        "provisional_cloud",
        "provisional_probability",
    ]
    missing_behavior: str
    dependencies: tuple[str, ...] = ()
    editing: FieldEditContract = FieldEditContract()


FIELD_REGISTRY = MappingProxyType(
    {
        TEMPERATURE: FieldDefinition(
            TEMPERATURE,
            "scalar",
            "K",
            "_temperature",
            "control_recipe",
            "require_all",
            editing=FieldEditContract(
                operations=("add",),
                minimum=150.0,
                maximum=340.0,
                validation=("blended_dew_point_consistency", "relative_humidity"),
                intervention_limits=(("add.delta", -5.0, 5.0),),
            ),
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
            "coherence.relative_humidity",
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
            editing=FieldEditContract(
                operations=("add", "scale", "smooth"),
                minimum=0.0,
                validation=("finite_nonnegative", "exact_hourly_interval"),
                intervention_limits=(
                    ("add.delta", -10.0, 10.0),
                    ("scale.factor", 0.0, 2.0),
                    ("smooth.strength", 0.0, 1.0),
                ),
            ),
        ),
        CLOUD: FieldDefinition(
            CLOUD,
            "scalar",
            "1",
            "_cloud",
            "provisional_cloud",
            "eligible_total_cloud_only_no_layer_substitution",
        ),
        POP6: FieldDefinition(
            POP6,
            "probability",
            "1",
            "_probability_6h",
            "provisional_probability",
            "eligible_same_native_six_hour_gridpoint_event_only",
        ),
    }
)


def field_edit_contract(field_id: str) -> FieldEditContract:
    """Temporary/evidence fields remain inspect-only without a new blend path."""
    definition = FIELD_REGISTRY.get(field_id)
    return definition.editing if definition else FieldEditContract()


@dataclass
class BlendState:
    """Extracted values at one point/valid hour; never shared across hours/cells.

    The caller retains the full native provenance records. No input values are
    edited by dispatch, including working gust floors and source exclusions.
    """

    horizon: int
    contributors: dict[str, dict[str, float | None]]
    precipitation: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_context: dict[str, dict[str, Any]] = field(default_factory=dict)
    precipitation_interval: tuple[Any, Any] | None = None
    probabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    probability_interval: tuple[Any, Any] | None = None
    results: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    source_validation: dict[str, Any] = field(default_factory=dict, init=False)
    validated: ValidatedSources | None = field(default=None, init=False)
    coherence_events: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    coherence_seconds: dict[str, float] = field(default_factory=dict, init=False)
    blend_seconds: float = field(default=0.0, init=False)
    coherence_collected: bool = field(default=False, init=False)


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


@dataclass(frozen=True)
class FieldBlendEngine:
    """Dispatch the active V2 fields using existing versioned policy definitions.

    A state belongs to this engine for one immutable set of extracted inputs.
    Current cross-field ordering is owned by the finite coherence engine; these
    handlers retain the existing scientific blend kernels. Temperature-only
    historical prepared data needs no Phase 2 configuration.
    """

    contributors: ContributorConfiguration
    phase2: Phase2BlendConfiguration | None = None
    policy_overrides: Mapping[str, Recipe | FallbackWeightTable] = field(default_factory=dict)
    policy_family: str | None = None

    def __post_init__(self) -> None:
        """Explicit background shadow execution cannot mutate the active policy set."""
        if self.policy_family not in (None, PROVISIONAL_MULTIMODEL_POLICY):
            raise SurfaceBlendError("Unknown explicit blend policy family")
        if self.policy_family is not None and self.policy_overrides:
            raise SurfaceBlendError("Legacy candidate overrides cannot alter provisional policies")
        overrides = dict(self.policy_overrides)
        for field_id, policy in overrides.items():
            definition = FIELD_REGISTRY[field_id]
            if definition.policy_binding == "rh":
                raise SurfaceBlendError("RH is diagnostic, not an independently weighted field")
            if definition.policy_binding == "control_recipe":
                if not isinstance(policy, Recipe) or policy.field != field_id:
                    raise SurfaceBlendError(
                        "Temperature override requires a matching scalar recipe"
                    )
                definitions = self.contributors.model_map()
                for contributor in policy.contributors:
                    model = definitions.get(contributor.model)
                    if (
                        model is None
                        or field_id not in model.supported_fields
                        or model.status == "retired"
                    ):
                        raise SurfaceBlendError("Candidate recipe uses unsupported contributor")
            elif not isinstance(policy, FallbackWeightTable):
                raise SurfaceBlendError("This field requires its existing fallback-table contract")
        object.__setattr__(self, "policy_overrides", MappingProxyType(overrides))

    def policy_for(
        self, field_id: str
    ) -> Recipe | FallbackWeightTable | ProvisionalFieldPolicy | str:
        definition = FIELD_REGISTRY[field_id]
        if self.policy_family is not None and field_id != RH:
            return provisional_policy(field_id)
        if field_id in (CLOUD, POP6):
            raise SurfaceBlendError("Field blend requires the explicit provisional policy family")
        if field_id in self.policy_overrides:
            return self.policy_overrides[field_id]
        if definition.policy_binding == "control_recipe":
            if self.contributors.control_recipe is None:
                raise SurfaceBlendError(
                    "Dynamic field configuration requires explicit policy family"
                )
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
        """Produce one field through its current blend and coherence dependencies."""
        FIELD_REGISTRY[field_id]
        if (field_id != TEMPERATURE or self.policy_family is not None) and (
            type(state.horizon) is not int
            or state.horizon not in range(1, 121 if self.policy_family else 37)
        ):
            maximum = 120 if self.policy_family else 36
            raise SurfaceBlendError(
                f"surface forecast horizon must be an integer from 1 through {maximum}"
            )
        if field_id in (TEMPERATURE, QPF, CLOUD, POP6):
            return self._blend_raw(field_id, state)
        BASELINE_COHERENCE.apply_baseline(self, state, fields=(field_id,))
        return state.results[field_id]

    def _blend_raw(self, field_id: str, state: BlendState) -> dict[str, Any]:
        """Kernel dispatch only; source eligibility and ordering belong to coherence."""
        if field_id not in state.results:
            definition = FIELD_REGISTRY[field_id]
            if definition.semantic_kind == "derived":
                raise CoherenceError(f"{field_id}: diagnostic requires coherence execution")
            clock = time.perf_counter()
            try:
                state.results[field_id] = getattr(self, definition.execution)(state)
            finally:
                state.blend_seconds += time.perf_counter() - clock
        return state.results[field_id]

    def _sources(self, state: BlendState) -> ValidatedSources:
        if state.validated is None:
            raise CoherenceError("Native source eligibility must precede dependent blends")
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
        if self.policy_family is not None:
            values = {
                model: source[TEMPERATURE]
                for model, source in state.contributors.items()
                if _usable(source.get(TEMPERATURE), 150.0, 340.0)
            }
            return self._provisional_scalar(TEMPERATURE, values, state, "K")
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
        if self.policy_family is not None:
            return self._provisional_scalar(DEW_POINT, sources.dew, state, "K")
        reasons = list(sources.dew_reasons)
        row = self._row(DEW_POINT, sources.dew, state)
        value = blend_scalar(_contributions(sources.dew, row)).blended_value if row else None
        table = self.policy_for(DEW_POINT)
        assert isinstance(table, FallbackWeightTable)
        return _field(value, "K", policy=table.table_id, reasons=reasons, row=row)

    def _wind(self, state: BlendState) -> dict[str, Any]:
        sources = self._sources(state)
        if self.policy_family is not None:
            weights, provenance = self._provisional_weights(WIND, sources.u, state)
            vector = (
                blend_vector(
                    eastward_contributions=self._weighted(sources.u, weights),
                    northward_contributions=self._weighted(sources.v, weights),
                )
                if weights
                else None
            )
            variables = (_U, _V, "wind_speed_10m", "wind_from_direction_10m")
            numbers = (
                (
                    vector.eastward_m_s,
                    vector.northward_m_s,
                    vector.speed_m_s,
                    vector.direction_degrees,
                )
                if vector
                else (None, None, None, None)
            )
            return {
                variable: self._provisional_result(
                    WIND,
                    value,
                    "degree" if variable == "wind_from_direction_10m" else "m/s",
                    weights,
                    provenance,
                )
                for variable, value in zip(variables, numbers, strict=True)
            }
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
        wind = state.results[WIND]
        if self.policy_family is not None:
            weights, provenance = self._provisional_weights(GUST, sources.gust, state)
            value, floor, reason = None, False, None
            sustained = wind["wind_speed_10m"]["value"]
            if weights and sustained is not None:
                assert self.phase2 is not None
                try:
                    gust = blend_gust(
                        contributions=self._weighted(sources.gust, weights),
                        blended_sustained_speed_m_s=sustained,
                        final_epsilon_floor_m_s=self.phase2.gust_policy.final_epsilon_floor_m_s,
                        valid_max_m_s=self.phase2.gust_policy.valid_max_m_s,
                    )
                    value, floor = gust.blended_gust_m_s, gust.final_gust_epsilon_floor_applied
                except GustInvariantError as exc:
                    reason = str(exc)
            result = self._provisional_result(GUST, value, "m/s", weights, provenance)
            if reason:
                result["missing_reasons"].append(reason)
            result["final_gust_epsilon_floor_applied"] = floor
            return result
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
        if self.policy_family is not None:
            return self._provisional_qpf(state)
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
        if type(state.horizon) is not int or state.horizon not in range(
            1, 121 if self.policy_family else 37
        ):
            maximum = 120 if self.policy_family else 36
            raise SurfaceBlendError(
                f"surface forecast horizon must be an integer from 1 through {maximum}"
            )
        BASELINE_COHERENCE.apply_baseline(self, state)
        fields = {}
        for field_id in FIELD_REGISTRY:
            if field_id in (CLOUD, POP6):
                continue
            if field_id == QPF and not state.precipitation and state.precipitation_interval is None:
                continue
            value = state.results[field_id]
            if field_id == WIND:
                fields.update(value)
            elif field_id == TEMPERATURE and self.policy_family is None:
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
        fields[CLOUD] = (
            self._blend_raw(CLOUD, state)
            if self.policy_family
            else _field(
                None,
                "1",
                policy="no-approved-cloud-blend-policy",
                reasons=["cloud/sky cover has no retained approved normalization and blend policy"],
            )
        )
        return fields

    @staticmethod
    def _weighted(
        values: Mapping[str, float | None], weights: Mapping[str, float]
    ) -> tuple[Contribution, ...]:
        return tuple(
            Contribution(model, float(value), weight)
            for model, weight in weights.items()
            if (value := values[model]) is not None
        )

    def _provisional_weights(
        self, field_id: str, values: Mapping[str, Any], state: BlendState
    ) -> tuple[dict[str, float], dict[str, Any]]:
        return provisional_weights(
            field_id,
            lead=state.horizon,
            available_models=tuple(values),
            source_context=state.source_context,
        )

    @staticmethod
    def _provisional_result(
        field_id: str,
        value: float | None,
        unit: str,
        weights: dict[str, float],
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        result = _field(
            value,
            unit,
            policy=provisional_policy(field_id).policy_id,
            reasons=[]
            if value is not None
            else ["no compatible available blend satisfying current constraints"],
        )
        result.update(
            weights=weights,
            policy_version=1,
            policy_family=PROVISIONAL_MULTIMODEL_POLICY,
            provisional=True,
            source_influence=provenance,
        )
        return result

    def _provisional_scalar(
        self, field_id: str, values: Mapping[str, float | None], state: BlendState, unit: str
    ) -> dict[str, Any]:
        weights, provenance = self._provisional_weights(field_id, values, state)
        value = blend_scalar(self._weighted(values, weights)).blended_value if weights else None
        return self._provisional_result(field_id, value, unit, weights, provenance)

    def _cloud(self, state: BlendState) -> dict[str, Any]:
        if self.policy_family is None:
            raise SurfaceBlendError("Cloud blend requires the explicit provisional policy family")
        values = {
            model: source[CLOUD]
            for model, source in state.contributors.items()
            if _usable(source.get(CLOUD), 0.0, 1.0)
        }
        result = self._provisional_scalar(CLOUD, values, state, "1")
        fraction = result["value"]
        if fraction is not None:
            # A float-normalized weight sum may differ from one by one ULP.
            # Evaluate the convex mean exactly as a ratio, never clip a cloud.
            fraction /= math.fsum(result["weights"].values())
            result["value"] = fraction
        result.update(
            cloud_percentage=None if fraction is None else 100.0 * fraction,
            normalized_fraction=fraction,
            sky_category=None if fraction is None else sky_category(100.0 * fraction),
            sky_category_policy=SKY_CATEGORY_POLICY,
            cloud_definition="total_cloud_cover",
            vertical_extent="entire_atmosphere",
            temporal_semantics="instantaneous",
            interval_start=None,
            interval_end=None,
            role="active_blended_baseline",
        )
        if result["weights"]:
            context = state.source_context[next(iter(result["weights"]))]
            result["valid_time"] = (
                (aware_time(context["reference_time"]) + timedelta(hours=state.horizon))
                .isoformat()
                .replace("+00:00", "Z")
            )
        return result

    def _probability_6h(self, state: BlendState) -> dict[str, Any]:
        """Blend only independently validated probabilities of one identical event."""
        if self.policy_family is None:
            raise SurfaceBlendError("Six-hour PoP requires the explicit provisional policy family")
        if state.probability_interval is None:
            raise SurfaceBlendError("Six-hour PoP requires an explicit target event")
        start, end = map(aware_time, state.probability_interval)
        if end - start != timedelta(hours=6) or any(
            instant.minute or instant.second or instant.microsecond for instant in (start, end)
        ):
            raise SurfaceBlendError("Six-hour PoP requires exact whole-hour (start,end] bounds")
        threshold = dict(POP6_THRESHOLD)
        support = {"kind": "grid_point"}
        values: dict[str, float | None] = {}
        exclusions = {}
        for model, source_id in (("NBM", "NBM_6H"), ("GEFS", "GEFS_6H")):
            native = state.probabilities.get(model, {})
            try:
                value = native.get("value")
                if not _usable(value, 0.0, 1.0) or native.get("missing_reasons"):
                    raise SurfaceBlendError("native probability unavailable or invalid")
                if (
                    native.get("source_id") != source_id
                    or native.get("unit") != "1"
                    or native.get("threshold") != threshold
                    or native.get("spatial_support") != support
                    or native.get("interval_closure") != "left_open_right_closed"
                ):
                    raise SurfaceBlendError("incompatible probability source/threshold/support")
                if native.get("temporal_semantics", "probability") != "probability":
                    raise SurfaceBlendError("native field is not a probability")
                if (
                    aware_time(native.get("interval_start")),
                    aware_time(native.get("interval_end")),
                ) != (start, end):
                    raise SurfaceBlendError("incompatible native probability interval")
                context = state.source_context.get(model, {})
                cycle = aware_time(context.get("cycle"))
                if cycle != aware_time(native.get("source_cycle")) or start < cycle:
                    raise SurfaceBlendError("probability cycle disagrees with pinned source")
                source_lead = (end - cycle).total_seconds() / 3600
                if type(native.get("source_lead_hours")) not in (int, float) or (
                    native["source_lead_hours"] != source_lead
                ):
                    raise SurfaceBlendError("probability source lead differs from event end")
                if (
                    aware_time(context.get("reference_time")) + timedelta(hours=state.horizon)
                    != end
                ):
                    raise SurfaceBlendError("probability event end differs from forecast lead")
                values[model] = value
            except (ValueError, SurfaceBlendError) as exc:
                exclusions[model] = str(exc)
        weights, provenance = self._provisional_weights(POP6, values, state)
        value = blend_scalar(self._weighted(values, weights)).blended_value if weights else None
        if value is not None:
            value /= math.fsum(weights.values())
        result = self._provisional_result(POP6, value, "1", weights, provenance)
        result.update(
            interval_start=start.isoformat().replace("+00:00", "Z"),
            interval_end=end.isoformat().replace("+00:00", "Z"),
            valid_time=end.isoformat().replace("+00:00", "Z"),
            interval_closure="left_open_right_closed",
            temporal_semantics="probability",
            event_duration_hours=6,
            threshold=threshold,
            spatial_support=support,
            role="active_blended_baseline",
            contributor_exclusions=exclusions,
            contributors=deepcopy(state.probabilities),
        )
        return result

    def _provisional_qpf(self, state: BlendState) -> dict[str, Any]:
        if state.precipitation_interval is None:
            raise SurfaceBlendError("Provisional QPF requires an explicit target event interval")
        start, end = map(aware_time, state.precipitation_interval)
        duration = (end - start).total_seconds() / 3600
        if duration not in (1.0, 3.0, 6.0) or any(
            value.minute or value.second or value.microsecond for value in (start, end)
        ):
            raise SurfaceBlendError("Provisional QPF requires an exact1/3/6-hour event")
        values: dict[str, float | None] = {}
        exclusions = {}
        for model in PROVISIONAL_MODELS:
            native = state.precipitation.get(model, {})
            try:
                value = native.get("value")
                if not _usable(value, 0.0, float("inf")) or native.get("missing_reasons"):
                    raise SurfaceBlendError("native amount unavailable or invalid")
                if (
                    native.get("unit") != "kg/m^2"
                    or native.get("temporal_semantics") != "accumulation"
                    or native.get("interval_closure") != "left_open_right_closed"
                ):
                    raise SurfaceBlendError("incompatible native accumulation semantics or unit")
                if (
                    aware_time(native.get("interval_start")),
                    aware_time(native.get("interval_end")),
                ) != (start, end):
                    raise SurfaceBlendError("incompatible native accumulation interval")
                context = state.source_context.get(model, {})
                cycle = aware_time(context.get("cycle"))
                native_cycle = native.get("source_cycle", native.get("cycle"))
                if native_cycle is not None and aware_time(native_cycle) != cycle:
                    raise SurfaceBlendError("native accumulation cycle differs from source context")
                start_lead = (start - cycle).total_seconds() / 3600
                end_lead = (end - cycle).total_seconds() / 3600
                if start_lead < 0:
                    raise SurfaceBlendError("accumulation starts before source cycle")
                if model == "IFS" or (model == "GFS" and end_lead > 120):
                    native_leads = native_field_contract(
                        model, "liquid_equivalent_precipitation_amount"
                    ).native_leads(cycle)
                    if (
                        start_lead != 0 and start_lead not in native_leads
                    ) or end_lead not in native_leads:
                        raise SurfaceBlendError(
                            "accumulation requires complete native boundaries; no splitting"
                        )
                if model == "NBM" and duration == 3 and end_lead > 48:
                    raise SurfaceBlendError(
                        "sparse NBM hourly amounts cannot fill a three-hour interval"
                    )
                if (
                    aware_time(context.get("reference_time")) + timedelta(hours=state.horizon)
                    != end
                ):
                    raise SurfaceBlendError("target interval end disagrees with forecast lead")
                values[model] = value
            except (ValueError, SurfaceBlendError) as exc:
                exclusions[model] = str(exc)
        weights, provenance = self._provisional_weights(QPF, values, state)
        # The existing kernel is a nonnegative scalar weighted mean. Exact-event
        # validation here extends dispatch without splitting native amounts.
        value = blend_qpf(self._weighted(values, weights)) if weights else None
        result = self._provisional_result(QPF, value, "kg/m^2", weights, provenance)
        result.update(
            interval_start=start.isoformat().replace("+00:00", "Z"),
            interval_end=end.isoformat().replace("+00:00", "Z"),
            interval_closure="left_open_right_closed",
            temporal_semantics="accumulation",
            accumulation_duration_hours=int(duration),
            contributor_exclusions=exclusions,
        )
        return result
