"""Explicit initial 120-hour influence priors, not measured skill coefficients.

Role budgets avoid giving a family an extra vote merely because another related
model is available. Lead, freshness and native-horizon factors are transparent
smooth priors. Actual decoded eligibility remains a prerequisite, never inferred
from this catalogue or a nominal model range.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from mesoforge.catalog.native_horizons import native_field_contract
from mesoforge.forecasting.surface import SurfaceBlendError

PROVISIONAL_MULTIMODEL_POLICY: Final = "mesoforge.provisional-multimodel-120h.v1"
PROVISIONAL_MODELS = ("HRRR", "RAP", "GFS", "IFS", "NBM")
_T = "air_temperature_2m"
_D = "dew_point_temperature_2m"
_W = "wind_10m"
_G = "wind_gust_10m"
_Q = "liquid_equivalent_precipitation_amount_1h"
_C = "cloud_area_fraction"
POP6 = "probability_of_precipitation_6h"
POP6_THRESHOLD = MappingProxyType({"value": 0.254, "unit": "kg/m^2", "comparison": "gt"})
_LABELS = {
    _T: "temperature",
    _D: "dew-point",
    _W: "vector-wind",
    _G: "gust",
    _Q: "qpf",
    _C: "total-cloud",
    POP6: "six-hour-precipitation-probability",
}


def aware_time(value: Any) -> datetime:
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SurfaceBlendError("Provisional source context requires an aware timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise SurfaceBlendError("Provisional source context requires an aware timestamp")
    return instant.astimezone(UTC)


def _smoothstep(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * (3.0 - 2.0 * value)


def _update_hours(model: str, cycle: datetime) -> int:
    if model == "HRRR":
        return 6 if cycle.hour in (0, 6, 12, 18) else 1
    if model == "RAP":
        return 6 if cycle.hour in (3, 9, 15, 21) else 1
    return 6 if model in ("GFS", "IFS", "GEFS") else 1


@dataclass(frozen=True, slots=True)
class ProvisionalFieldPolicy:
    field_id: str

    @property
    def models(self) -> tuple[str, ...]:
        return ("NBM", "GEFS") if self.field_id == POP6 else PROVISIONAL_MODELS

    @property
    def policy_id(self) -> str:
        return f"mesoforge.{_LABELS[self.field_id]}-role-blend.v1"

    def role(self, model: str) -> str:
        if model == "NBM":
            return "meta_model"
        if self.field_id == POP6:
            return "ensemble"
        if self.field_id == _C:
            return "native_deterministic"
        if self.field_id == _Q:
            return "convection_permitting" if model == "HRRR" else "broader_deterministic"
        return "mesoscale" if model in ("HRRR", "RAP") else "global"

    def role_prior(self, role: str, lead: int) -> float:
        if self.field_id in (_C, POP6):
            return 2.0 if role == "meta_model" else 1.0
        transition = _smoothstep((lead - 18) / 30)
        if role in ("mesoscale", "convection_permitting"):
            return 2.0 - transition
        if role in ("global", "broader_deterministic"):
            return 1.0 + transition
        return 1.0

    def payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "family": PROVISIONAL_MULTIMODEL_POLICY,
            "field": self.field_id,
            "status": "provisional_operational_prior_not_skill_calibrated",
            "model_roles": {model: self.role(model) for model in self.models},
            "role_priors_at_leads": {
                str(lead): {
                    role: self.role_prior(role, lead)
                    for role in sorted({self.role(model) for model in self.models})
                }
                for lead in (1, 18, 48, 120)
            },
            "lead_transition": "constant role priors"
            if self.field_id in (_C, POP6)
            else "smoothstep from lead18 to48",
            "within_role": "fixed equal division among registered role members before eligibility",
            "freshness": "1/(1+cycle_age_hours/update_cycle_hours)",
            "maximum_cycle_age_hours": 24,
            "native_expiry": (
                "smoothstep((last_native_lead+1-source_lead)/(update_cycle_hours+1)); "
                "reject beyond last native lead"
            ),
            "missing_policy": (
                "renormalize eligible positive influence only; unavailable when empty"
            ),
            "skill_claim": False,
        }


def provisional_policy(field_id: str) -> ProvisionalFieldPolicy:
    if field_id not in _LABELS:
        raise SurfaceBlendError(f"No provisional field policy: {field_id}")
    return ProvisionalFieldPolicy(field_id)


def provisional_weights(
    field_id: str,
    *,
    lead: int,
    available_models: tuple[str, ...],
    source_context: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return normalized influence and explicit eligibility/factor provenance."""
    if type(lead) is not int or not 1 <= lead <= 120:
        raise SurfaceBlendError("Provisional lead must be an integer from 1 through 120")
    policy = provisional_policy(field_id)
    catalogue_field = {_W: "eastward_wind_10m", _Q: "liquid_equivalent_precipitation_amount"}.get(
        field_id, field_id
    )
    rows: dict[str, Any] = {}
    for model in policy.models:
        row: dict[str, Any] = {"role": policy.role(model), "eligible": False, "reasons": []}
        rows[model] = row
        if model not in available_models:
            row["reasons"].append("normalized field unavailable or rejected")
            continue
        context = source_context.get(model, {})
        try:
            if context.get("missing_reasons"):
                raise SurfaceBlendError("source context has explicit missingness")
            cycle, reference = (
                aware_time(context.get("cycle")),
                aware_time(context.get("reference_time")),
            )
            if cycle > reference:
                raise SurfaceBlendError("source cycle is after forecast reference")
            if (reference - cycle).total_seconds() > 24 * 3600:
                raise SurfaceBlendError("source cycle exceeds the 24-hour eligibility lookback")
            target = reference + timedelta(hours=lead)
            source_lead = (target - cycle).total_seconds() / 3600
            if "source_lead_hours" in context and (
                type(context["source_lead_hours"]) not in (int, float)
                or context["source_lead_hours"] != source_lead
            ):
                raise SurfaceBlendError("source lead disagrees with cycle/reference/target")
            if context.get("available_at") is not None:
                if aware_time(context["available_at"]) > aware_time(
                    context.get("information_cutoff")
                ):
                    raise SurfaceBlendError("source availability exceeds pinned information cutoff")
            contract = native_field_contract(model, catalogue_field)
            plan = contract.plan(cycle, target)
            if plan.status not in ("native", "bracketed"):
                raise SurfaceBlendError(f"native time support: {plan.status}")
            last = contract.native_leads(cycle)[-1]
            update = _update_hours(model, cycle)
            age = (reference - cycle).total_seconds() / 3600
            freshness = 1.0 / (1.0 + age / update)
            taper = _smoothstep((last + 1 - source_lead) / (update + 1))
            row.update(
                eligible=True,
                cycle=cycle.isoformat().replace("+00:00", "Z"),
                reference_time=reference.isoformat().replace("+00:00", "Z"),
                source_lead_hours=source_lead,
                cycle_age_hours=age,
                native_last_lead_hours=last,
                update_cycle_hours=update,
                freshness_factor=freshness,
                native_horizon_factor=taper,
                role_prior=policy.role_prior(row["role"], lead),
                native_plan=plan.status,
            )
        except (ValueError, SurfaceBlendError) as exc:
            row["reasons"].append(str(exc))
    references = {row["reference_time"] for row in rows.values() if row["eligible"]}
    if len(references) > 1:
        for row in rows.values():
            if row["eligible"]:
                row["eligible"] = False
                row["reasons"].append("contributor forecast reference times disagree")
    # Fixed allocation avoids doubling a surviving sibling's raw influence as
    # another sibling expires. Only the final eligible total is renormalized.
    counts = {
        role: sum(policy.role(model) == role for model in policy.models)
        for role in {policy.role(model) for model in policy.models}
    }
    influence = {}
    for model, row in rows.items():
        if row["eligible"]:
            row["within_role_members"] = counts[row["role"]]
            row["raw_influence"] = (
                row["role_prior"]
                / counts[row["role"]]
                * row["freshness_factor"]
                * row["native_horizon_factor"]
            )
            influence[model] = row["raw_influence"]
    total = math.fsum(influence.values())
    weights = {model: value / total for model, value in influence.items()} if total else {}
    for model, row in rows.items():
        row["applied_weight"] = weights.get(model, 0.0)
    return weights, {"policy": policy.payload(), "contributors": rows}
