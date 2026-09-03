"""Deterministic model/variable availability and explicit fallback
selection (plan Section 4.1, Task 7).

Consumes the set of models with a successfully aligned value at one
(variable, location, target_horizon) and the configured
``FallbackWeightTable`` to select the exact approved contributor row.
Never renormalizes weights dynamically and never invents a row for an
unsupported model-availability combination.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mesoforge.catalog.configuration import FallbackWeightRow, FallbackWeightTable
from mesoforge.common.errors import MesoForgeError

VariableAvailabilityState = Literal["complete", "fallback", "unavailable", "inconsistent"]
RunState = Literal["complete", "degraded", "invalid"]

_ALL_MODELS = ("HRRR", "NBM", "GFS")

_DETERMINISTIC_VARIABLE_IDS = frozenset(
    {
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_gust_10m",
        "liquid_equivalent_precipitation_amount_1h",
    }
)


class AvailabilityError(MesoForgeError):
    """Raised when availability evaluation encounters an unsupported
    combination the configured fallback table has no row for."""


@dataclass(frozen=True, slots=True)
class VariableAvailability:
    variable_id: str
    location: str
    target_horizon: int
    state: VariableAvailabilityState
    available_models: tuple[str, ...]
    fallback_row: FallbackWeightRow | None
    reason: str | None = None


def evaluate_scalar_vector_availability(
    *,
    table: FallbackWeightTable,
    variable_id: str,
    location: str,
    target_horizon: int,
    available_models: frozenset[str],
    inconsistent_models: frozenset[str] = frozenset(),
) -> VariableAvailability:
    """Section 4.1: evaluate one (variable, location, target_horizon)
    availability state.

    ``available_models`` is the set of model families with a
    successfully aligned, scientifically consistent value at this
    exact point (post disqualification). A model present in
    ``inconsistent_models`` is treated as unavailable for this
    variable/point.

    Disqualification scope is decided by the caller, at the smallest
    scientifically coupled unit: a source gust below its own sustained
    speed rejects that model's U/V/gust tuple at that station/horizon,
    while coverage, geometry, time-identity, lineage, or a documented
    widespread-quality failure escalates to the model cycle (Codex
    review ``t_1564b30c``). Either way this function only ever selects
    an explicitly approved fallback row for whatever set remains; it
    never renormalizes weights and never invents a row.
    """
    usable = frozenset(available_models) - frozenset(inconsistent_models)

    if not usable:
        return VariableAvailability(
            variable_id=variable_id,
            location=location,
            target_horizon=target_horizon,
            state="unavailable",
            available_models=(),
            fallback_row=None,
            reason="no model produced a usable value at this point",
        )

    try:
        row = table.row_for(available_models=tuple(usable), horizon=target_horizon)
    except KeyError as exc:
        raise AvailabilityError(
            f"no approved fallback row exists for available_models={sorted(usable)!r} at "
            f"horizon={target_horizon!r} (variable={variable_id!r}, location={location!r})"
        ) from exc

    state: VariableAvailabilityState = (
        "complete" if usable == frozenset(_ALL_MODELS) else "fallback"
    )
    return VariableAvailability(
        variable_id=variable_id,
        location=location,
        target_horizon=target_horizon,
        state=state,
        available_models=tuple(m for m in _ALL_MODELS if m in usable),
        fallback_row=row,
    )


def evaluate_pop_availability(
    *, location: str, target_horizon: int, nbm_available: bool
) -> VariableAvailability:
    """Section 4.5: PoP is NBM passthrough only, weight 1.0. NBM
    unavailable makes PoP explicitly unavailable -- never synthesized
    from deterministic HRRR/GFS precipitation."""
    if nbm_available:
        return VariableAvailability(
            variable_id="probability_of_precipitation_1h",
            location=location,
            target_horizon=target_horizon,
            state="complete",
            available_models=("NBM",),
            fallback_row=None,
        )
    return VariableAvailability(
        variable_id="probability_of_precipitation_1h",
        location=location,
        target_horizon=target_horizon,
        state="unavailable",
        available_models=(),
        fallback_row=None,
        reason="NBM unavailable; PoP has no approved alternate contributor",
    )


@dataclass(frozen=True, slots=True)
class RunAvailabilitySummary:
    state: RunState
    used_fallback: bool
    invalid_variables: tuple[tuple[str, str, int], ...]


def evaluate_run_state(availabilities: tuple[VariableAvailability, ...]) -> RunAvailabilitySummary:
    """Section 4.1: the run result is ``complete`` when every variable
    is complete; ``degraded`` when each deterministic variable has an
    approved result (complete or fallback) but at least one used
    fallback, or PoP is unavailable; ``invalid`` when a deterministic
    variable has no result (``unavailable``/``inconsistent``) at any
    point."""
    used_fallback = False
    invalid: list[tuple[str, str, int]] = []

    for availability in availabilities:
        is_deterministic = availability.variable_id in _DETERMINISTIC_VARIABLE_IDS
        if availability.state == "fallback":
            used_fallback = True
        elif availability.state in ("unavailable", "inconsistent"):
            if is_deterministic:
                invalid.append(
                    (availability.variable_id, availability.location, availability.target_horizon)
                )
            elif availability.variable_id == "probability_of_precipitation_1h":
                used_fallback = True  # PoP unavailable alone yields degraded, not invalid

    if invalid:
        return RunAvailabilitySummary(
            state="invalid", used_fallback=used_fallback, invalid_variables=tuple(invalid)
        )
    if used_fallback:
        return RunAvailabilitySummary(state="degraded", used_fallback=True, invalid_variables=())
    return RunAvailabilitySummary(state="complete", used_fallback=False, invalid_variables=())
