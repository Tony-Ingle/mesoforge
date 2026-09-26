"""Finite orchestration of current cross-field contracts, without new weather rules.

Native eligibility must precede the affected blends. Baseline constraints and
diagnostics follow those blends. Conceptual future links are metadata, not
executable graph edges. Native evidence and historical serialized fields remain
unchanged; reports reference their existing reasons, exclusions and floor flags.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from mesoforge.forecasting.gust_blend import GustDisqualificationError, validate_source_gust
from mesoforge.forecasting.ice import FLAT_ICE, FRZR
from mesoforge.forecasting.scalar_blend import ConsistencyError, check_dew_point_consistency
from mesoforge.forecasting.snowfall_amount import KUCHERA_METHOD
from mesoforge.forecasting.surface import (
    RH_POLICY,
    RH_SOURCE,
    SurfaceBlendError,
    _usable,
    relative_humidity_percent,
)
from mesoforge.forecasting.thunder import THUNDER

if TYPE_CHECKING:
    from mesoforge.catalog.configuration import Phase2BlendConfiguration
    from mesoforge.forecasting.field_blend import BlendState, FieldBlendEngine

COHERENCE_VERSION = "mesoforge.baseline-coherence.v1"
TEMPERATURE = "air_temperature_2m"
DEW_POINT = "dew_point_temperature_2m"
WIND = "wind_10m"
GUST = "wind_gust_10m"
QPF = "liquid_equivalent_precipitation_amount_1h"
RH = "relative_humidity_2m"
_U = "eastward_wind_10m"
_V = "northward_wind_10m"
_ACTIVE_MODELS = ("HRRR", "GFS")
_DEW_KERNEL = "scalar_blend.check_dew_point_consistency"
_VECTOR_KERNEL = "vector_blend.blend_vector"


class CoherenceError(SurfaceBlendError):
    """The finite current coherence contract could not be executed or proven."""


@dataclass(frozen=True)
class Relationship:
    relationship_id: str
    kind: Literal["constraint", "derivation", "dependency"]
    status: Literal["enforced", "dependency_only", "external_evidence"]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    requires: tuple[str, ...] = ()
    phase: Literal["source_eligibility", "baseline", "evidence"] = "baseline"
    policy_ids: tuple[str, ...] = ()


_RELATIONSHIPS = (
    Relationship(
        "native_source_consistency",
        "constraint",
        "enforced",
        (TEMPERATURE, DEW_POINT, _U, _V, GUST),
        ("eligible_native_dew_point", "eligible_native_wind_gust"),
        phase="source_eligibility",
        policy_ids=(_DEW_KERNEL, "gust-blend-policy.v1"),
    ),
    Relationship(
        "blended_dew_point_consistency",
        "constraint",
        "enforced",
        (TEMPERATURE, DEW_POINT),
        (DEW_POINT,),
        ("native_source_consistency",),
        policy_ids=(_DEW_KERNEL,),
    ),
    Relationship(
        "relative_humidity",
        "derivation",
        "enforced",
        (TEMPERATURE, DEW_POINT),
        (RH,),
        ("blended_dew_point_consistency",),
        policy_ids=(RH_POLICY,),
    ),
    Relationship(
        "wind_vector_diagnostic",
        "derivation",
        "enforced",
        (_U, _V),
        ("wind_speed_10m", "wind_from_direction_10m"),
        ("native_source_consistency",),
        policy_ids=(_VECTOR_KERNEL,),
    ),
    Relationship(
        "blended_gust_consistency",
        "constraint",
        "enforced",
        ("wind_speed_10m", GUST),
        (GUST,),
        ("wind_vector_diagnostic",),
        policy_ids=("gust-blend-policy.v1",),
    ),
    *(
        Relationship(identity, "dependency", "dependency_only", inputs, ())
        for identity, inputs in (
            ("qpf_probability", (QPF, "probability_of_precipitation_1h")),
            ("qpf_precipitation_type", (QPF, "precipitation_type")),
            ("qpf_thunder", (QPF, THUNDER)),
            ("qpf_snow_ice", (QPF, "snowfall_water_equivalent_amount", FLAT_ICE)),
            ("ptype_thermal_structure", ("precipitation_type", "temperature_profile")),
            (
                "snowfall_swe_slr",
                ("snowfall_amount", "snowfall_water_equivalent_amount", "snow_to_liquid_ratio"),
            ),
            (
                "freezing_rain_thermal_ice",
                (FRZR, "temperature_profile", FLAT_ICE),
            ),
            (
                "visibility_moisture_cloud_precipitation_fog",
                ("visibility", RH, "cloud_area_fraction", QPF, "fog_evidence"),
            ),
        )
    ),
    Relationship(
        "native_kuchera_evidence",
        "derivation",
        "external_evidence",
        ("native_temperature_profile", "native_surface_pressure", "native_snowfall_swe"),
        ("shadow_kuchera_snowfall_amount",),
        phase="evidence",
        policy_ids=(str(KUCHERA_METHOD["id"]),),
    ),
)
RELATIONSHIP_REGISTRY = MappingProxyType({row.relationship_id: row for row in _RELATIONSHIPS})


def relationship_order(registry: Mapping[str, Relationship]) -> tuple[str, ...]:
    """Topologically order executable relationships; conceptual links do not execute.

    Fixed current relationship order, then identity, breaks ties independently
    of the caller's mapping insertion order. Dependencies refer to relationship
    stages, not cyclic conceptual field links. Every node executes at most once.
    """
    priority = {row.relationship_id: index for index, row in enumerate(_RELATIONSHIPS)}
    pending = {
        key: row
        for key, row in sorted(
            registry.items(), key=lambda item: (priority.get(item[0], len(priority)), item[0])
        )
        if row.status == "enforced"
    }
    for key, row in pending.items():
        if key != row.relationship_id or any(
            dependency not in pending for dependency in row.requires
        ):
            raise CoherenceError(f"Invalid executable coherence dependencies for {key}")
    ordered: list[str] = []
    while pending:
        ready = next(
            (key for key, row in pending.items() if all(item in ordered for item in row.requires)),
            None,
        )
        if ready is None:
            raise CoherenceError("Coherence dependency cycle: " + ", ".join(pending))
        ordered.append(ready)
        del pending[ready]
    return tuple(ordered)


EXECUTION_ORDER = relationship_order(RELATIONSHIP_REGISTRY)
_FIELD_RELATIONSHIPS = {
    DEW_POINT: "blended_dew_point_consistency",
    RH: "relative_humidity",
    WIND: "wind_vector_diagnostic",
    GUST: "blended_gust_consistency",
}


def framework_metadata() -> dict[str, Any]:
    """Small baseline-level identities; no duplicate native contributor blobs."""
    return {
        "framework_version": COHERENCE_VERSION,
        "phase": "baseline",
        "execution_order": list(EXECUTION_ORDER),
        "relationships": {key: asdict(row) for key, row in RELATIONSHIP_REGISTRY.items()},
        "future_rules_enforced": False,
    }


@dataclass
class ValidatedSources:
    dew: dict[str, float]
    u: dict[str, float]
    v: dict[str, float]
    gust: dict[str, float]
    dew_reasons: list[str]
    wind_reasons: list[str]


def _validate_sources(
    state: BlendState, configuration: Phase2BlendConfiguration
) -> ValidatedSources:
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
    state.validated = ValidatedSources(
        dew_values, u_values, v_values, gust_values, dew_reasons, wind_reasons
    )
    return state.validated


def _event(
    identity: str,
    *,
    available: bool,
    actions: list[str],
    refs: list[str],
    changed: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "evaluated" if available else "unavailable",
        "actions": actions,
        "policy_ids": list(RELATIONSHIP_REGISTRY[identity].policy_ids),
        "evidence_refs": refs,
        "changed_fields": changed or [],
        "derived_fields": list(RELATIONSHIP_REGISTRY[identity].outputs)
        if "derived" in actions
        else [],
    }


class CoherenceEngine:
    """Execute the finite current baseline graph over one aligned cell/hour state."""

    def apply_local_fields(
        self, fields: dict[str, dict[str, Any]], *, changed_fields: tuple[str, ...]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Recheck affected existing local diagnostics without native reblending.

        Temperature executes its existing diagnostics. QPF has registered future
        dependencies only: report them, without inventing enforcement. The
        baseline's source eligibility and wind/gust results remain untouched.
        Missing/inconsistent dew point and RH follow the same baseline kernels.
        """
        if set(changed_fields) - {TEMPERATURE, QPF}:
            raise CoherenceError("Only temperature and QPF local transformations are supported")
        if not changed_fields:
            return fields, {"version": COHERENCE_VERSION, "relationships": {}}
        if TEMPERATURE not in changed_fields:
            return fields, {
                "version": COHERENCE_VERSION,
                "phase": "local_field_edit",
                "status": "passed",
                "relationships": {
                    name: {"status": "unimplemented", "enforced": False}
                    for name, relationship in RELATIONSHIP_REGISTRY.items()
                    if QPF in relationship.inputs and relationship.status == "dependency_only"
                },
                "future_rules_enforced": False,
            }
        working = {
            **fields,
            DEW_POINT: {
                **fields[DEW_POINT],
                "missing_reasons": list(fields[DEW_POINT]["missing_reasons"]),
            },
        }
        events = {
            "blended_dew_point_consistency": self._check_dew(working),
            "relative_humidity": self._derive_rh(working),
        }
        return working, {
            "version": COHERENCE_VERSION,
            "phase": "local_temperature_correction",
            "status": "passed",
            "relationships": events,
            "future_rules_enforced": False,
        }

    def apply_baseline(
        self,
        blend_engine: FieldBlendEngine,
        state: BlendState,
        *,
        fields: tuple[str, ...] | None = None,
    ) -> dict[str, dict[str, Any]]:
        targets = fields or (TEMPERATURE, DEW_POINT, RH, WIND, GUST)
        required = {
            _FIELD_RELATIONSHIPS[target] for target in targets if target in _FIELD_RELATIONSHIPS
        }
        # Dependencies only point backward in the already-validated finite order.
        for identity in reversed(EXECUTION_ORDER):
            if identity in required:
                required.update(RELATIONSHIP_REGISTRY[identity].requires)
        for identity in EXECUTION_ORDER:
            if identity not in required:
                continue
            prior = state.coherence_events.get(identity)
            if prior is not None:
                if prior["status"] == "failed":
                    raise CoherenceError(f"Required relationship previously failed: {identity}")
                continue
            clock = time.perf_counter()
            blend_before = state.blend_seconds
            try:
                event = getattr(self, "_" + identity)(blend_engine, state)
            except Exception as exc:
                state.coherence_events[identity] = {
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
                raise
            finally:
                state.coherence_seconds[identity] = max(
                    0.0, time.perf_counter() - clock - (state.blend_seconds - blend_before)
                )
            state.coherence_events[identity] = event
        for target in targets:
            if target not in state.results:
                blend_engine._blend_raw(target, state)
        if fields is None:
            if state.precipitation:
                blend_engine._blend_raw(QPF, state)
            self.report(state)  # A full state must prove every required operation completed.
            collector = _COLLECTOR.get()
            if collector is not None and not state.coherence_collected:
                collector.add(state)
                state.coherence_collected = True
        return state.results

    @staticmethod
    def report(state: BlendState) -> dict[str, Any]:
        for identity in EXECUTION_ORDER:
            if state.coherence_events.get(identity, {}).get("status") not in (
                "evaluated",
                "unavailable",
            ):
                raise CoherenceError(
                    f"Required current coherence relationship incomplete: {identity}"
                )
        return {
            "version": COHERENCE_VERSION,
            "status": "passed",
            "relationships": {key: state.coherence_events[key] for key in EXECUTION_ORDER},
            "unimplemented": [
                key for key, row in RELATIONSHIP_REGISTRY.items() if row.status == "dependency_only"
            ],
            "external_evidence": [
                key
                for key, row in RELATIONSHIP_REGISTRY.items()
                if row.status == "external_evidence"
            ],
        }

    def _native_source_consistency(
        self, engine: FieldBlendEngine, state: BlendState
    ) -> dict[str, Any]:
        if engine.phase2 is None:
            raise CoherenceError("Prepared Phase 2 policy configuration required")
        sources = _validate_sources(state, engine.phase2)
        rejected = any(
            row[kind]["status"] == "rejected"
            for row in state.source_validation.values()
            for kind in ("dew_point", "wind_gust")
        )
        floors = any(
            row["wind_gust"]["source_gust_floor_applied"]
            for row in state.source_validation.values()
        )
        return _event(
            "native_source_consistency",
            available=bool(sources.dew or sources.u),
            actions=[
                "validated",
                *(["excluded"] if rejected else []),
                *(["adjusted"] if floors else []),
            ],
            refs=["surface/source_validation", "surface/contributors"],
            changed=["working_native_gust"] if floors else [],
        )

    def _blended_dew_point_consistency(
        self, engine: FieldBlendEngine, state: BlendState
    ) -> dict[str, Any]:
        engine._blend_raw(TEMPERATURE, state)
        engine._blend_raw(DEW_POINT, state)
        return self._check_dew(state.results)

    @staticmethod
    def _check_dew(fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
        temperature = fields[TEMPERATURE]["value"]
        dew = fields[DEW_POINT]
        before = dew["value"]
        if before is not None:
            if not _usable(temperature, 150.0, 340.0):
                dew.update(value=None, status="unavailable")
                dew["missing_reasons"].append(
                    "active temperature unavailable for blended dew-point consistency"
                )
            else:
                try:
                    check_dew_point_consistency(temperature_k=temperature, dew_point_k=before)
                except ConsistencyError as exc:
                    dew.update(value=None, status="inconsistent")
                    dew["missing_reasons"].append(str(exc))
        changed = before is not None and dew["value"] is None
        return _event(
            "blended_dew_point_consistency",
            available=dew["value"] is not None,
            actions=[
                "excluded" if changed else "validated" if before is not None else "unavailable"
            ],
            refs=[f"surface/fields/{TEMPERATURE}", f"surface/fields/{DEW_POINT}"],
            changed=[DEW_POINT] if changed else [],
        )

    def _relative_humidity(self, engine: FieldBlendEngine, state: BlendState) -> dict[str, Any]:
        return self._derive_rh(state.results)

    @staticmethod
    def _derive_rh(fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
        temperature = fields[TEMPERATURE]["value"]
        dew = fields[DEW_POINT]["value"]
        value = None
        reasons: list[str] = []
        if _usable(temperature, 150.0, 340.0) and dew is not None:
            try:
                value = relative_humidity_percent(temperature_k=temperature, dew_point_k=dew)
            except SurfaceBlendError as exc:
                reasons.append(str(exc))
        else:
            reasons.append("RH requires available, consistent baseline temperature and dew point")
        fields[RH] = {
            "value": value,
            "unit": "%",
            "missing_reasons": reasons,
            "status": "available" if value is not None else "unavailable",
            "weights": {},
            "policy": RH_POLICY,
            "row_id": None,
            "row_sha256": None,
            "derived_from": [TEMPERATURE, DEW_POINT],
            "saturation_reference": "liquid_water",
            "source": RH_SOURCE,
        }
        return _event(
            "relative_humidity",
            available=value is not None,
            actions=["derived" if value is not None else "unavailable"],
            refs=[f"surface/fields/{name}" for name in (TEMPERATURE, DEW_POINT, RH)],
        )

    def _wind_vector_diagnostic(
        self, engine: FieldBlendEngine, state: BlendState
    ) -> dict[str, Any]:
        wind = engine._blend_raw(WIND, state)
        available = wind["wind_speed_10m"]["value"] is not None
        return _event(
            "wind_vector_diagnostic",
            available=available,
            actions=["derived" if available else "unavailable"],
            refs=[f"surface/fields/{name}" for name in wind],
        )

    def _blended_gust_consistency(
        self, engine: FieldBlendEngine, state: BlendState
    ) -> dict[str, Any]:
        gust = engine._blend_raw(GUST, state)
        available = gust["value"] is not None
        floor = gust["final_gust_epsilon_floor_applied"]
        return _event(
            "blended_gust_consistency",
            available=available,
            actions=["adjusted" if floor else "validated" if available else "unavailable"],
            refs=["surface/fields/wind_speed_10m", f"surface/fields/{GUST}"],
            changed=[GUST] if floor else [],
        )


BASELINE_COHERENCE = CoherenceEngine()


@dataclass
class CoherenceAudit:
    """Build-local reporting only; never controls meteorological calculations."""

    states: int = 0
    outcomes: dict[str, Counter[str]] = field(default_factory=dict)
    actions: dict[str, Counter[str]] = field(default_factory=dict)
    relationship_seconds: dict[str, float] = field(default_factory=dict)
    blend_seconds: float = 0.0

    @property
    def coherence_seconds(self) -> float:
        return sum(self.relationship_seconds.values())

    def add(self, state: BlendState) -> None:
        report = BASELINE_COHERENCE.report(state)
        self.states += 1
        for identity, event in report["relationships"].items():
            self.outcomes.setdefault(identity, Counter())[event["status"]] += 1
            self.actions.setdefault(identity, Counter()).update(event["actions"])
            self.relationship_seconds[identity] = (
                self.relationship_seconds.get(identity, 0.0) + state.coherence_seconds[identity]
            )
        self.blend_seconds += state.blend_seconds

    def validate(self, expected_states: int) -> None:
        if self.states != expected_states or any(
            sum(self.outcomes.get(identity, {}).values()) != expected_states
            for identity in EXECUTION_ORDER
        ):
            raise CoherenceError(
                f"Required baseline coherence execution incomplete: expected {expected_states} "
                f"calculated cell-hours, observed {self.states}"
            )

    def report(self) -> dict[str, Any]:
        return {
            "version": COHERENCE_VERSION,
            "status": "passed",
            "calculated_cell_hours": self.states,
            "relationships": {
                key: {
                    "outcomes": dict(sorted(self.outcomes.get(key, {}).items())),
                    "actions": dict(sorted(self.actions.get(key, {}).items())),
                    "policy_ids": list(RELATIONSHIP_REGISTRY[key].policy_ids),
                }
                for key in EXECUTION_ORDER
            },
            "unimplemented": [
                key for key, row in RELATIONSHIP_REGISTRY.items() if row.status == "dependency_only"
            ],
            "external_evidence": [
                key
                for key, row in RELATIONSHIP_REGISTRY.items()
                if row.status == "external_evidence"
            ],
            "evidence_location": "Saved cell/hour surface fields and source_validation",
        }


_COLLECTOR: ContextVar[CoherenceAudit | None] = ContextVar("baseline_coherence_audit", default=None)


@contextmanager
def collect_baseline_coherence() -> Iterator[CoherenceAudit]:
    audit = CoherenceAudit()
    token = _COLLECTOR.set(audit)
    try:
        yield audit
    finally:
        _COLLECTOR.reset(token)
