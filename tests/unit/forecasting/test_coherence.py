"""Current cross-field relationships preserve science and explain their actions."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from mesoforge.forecasting.coherence import (
    BASELINE_COHERENCE,
    RELATIONSHIP_REGISTRY,
    CoherenceError,
    collect_baseline_coherence,
    relationship_order,
)
from mesoforge.forecasting.field_blend import QPF, RH, WIND, BlendState, FieldBlendEngine
from mesoforge.forecasting.gust_blend import GustInvariantError
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION
from mesoforge.forecasting.thunder import THUNDER
from tests.unit.forecasting.test_surface import D, G, T, U, V, _sources
from tests.unit.forecasting.test_surface import configuration as configuration


def _engine(configuration):
    return FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)


def test_relationship_graph_is_immutable_deterministic_and_does_not_execute_future_rules():
    order = relationship_order(RELATIONSHIP_REGISTRY)
    assert order == relationship_order(dict(reversed(list(RELATIONSHIP_REGISTRY.items()))))
    assert set(order) == {
        name for name, rule in RELATIONSHIP_REGISTRY.items() if rule.status == "enforced"
    }
    assert any(rule.status != "enforced" for rule in RELATIONSHIP_REGISTRY.values())
    for name in order:
        for dependency in RELATIONSHIP_REGISTRY[name].requires:
            assert order.index(dependency) < order.index(name)
    with pytest.raises(TypeError):
        RELATIONSHIP_REGISTRY["invented_policy"] = RELATIONSHIP_REGISTRY[order[0]]
    with pytest.raises(FrozenInstanceError):
        RELATIONSHIP_REGISTRY[order[0]].status = "dependency_only"


@pytest.mark.parametrize("cycle", [False, True])
def test_invalid_relationship_dependencies_fail_before_numerical_work(cycle):
    registry = dict(RELATIONSHIP_REGISTRY)
    first, second = relationship_order(registry)[:2]
    registry[first] = replace(
        registry[first], requires=(second if cycle else "missing_relationship",)
    )
    if cycle:
        registry[second] = replace(registry[second], requires=(first,))
    with pytest.raises(CoherenceError):
        relationship_order(registry)


def test_actions_distinguish_validation_derivation_and_allowed_gust_adjustment(configuration):
    native = _sources()
    native["HRRR"][G] = 4.95
    original = deepcopy(native)
    state = BlendState(horizon=1, contributors=native)
    engine = _engine(configuration)
    BASELINE_COHERENCE.apply_baseline(engine, state)
    fields = engine.surface_fields(state)

    # The source's approved 0.05 m/s working floor does not overwrite native evidence.
    assert fields[T]["value"] == 303.0
    assert fields[D]["value"] == 283.0
    assert fields[G]["value"] == pytest.approx(7.1)
    assert native == original
    assert state.source_validation["HRRR"]["wind_gust"]["source_gust_m_s"] == 4.95
    assert state.source_validation["HRRR"]["wind_gust"]["validated_gust_m_s"] == 5.0
    expected_rh = 100 * math.exp(17.67 * 9.85 / (9.85 + 243.5) - 17.67 * 29.85 / (29.85 + 243.5))
    assert fields[RH]["value"] == pytest.approx(expected_rh, abs=1e-12)

    source = state.coherence_events["native_source_consistency"]
    humidity = state.coherence_events["relative_humidity"]
    dew = state.coherence_events["blended_dew_point_consistency"]
    assert "adjusted" in source["actions"]
    assert "derived" in humidity["actions"]
    assert "validated" in dew["actions"]
    assert source["changed_fields"]
    # A new diagnostic is distinct from adjusting an existing working value.
    assert humidity["derived_fields"] == [RH]
    assert humidity["changed_fields"] == []
    assert dew["changed_fields"] == []
    for event in (source, humidity, dew):
        assert event["policy_ids"]
        assert event["evidence_refs"]
        assert event["status"] == "evaluated"


def test_dew_tolerance_does_not_silently_relax_stricter_rh_contract(configuration):
    native = _sources()
    for source in native.values():
        source[T] = 280.0
        source[D] = 280.0000005
    state = BlendState(horizon=1, contributors=native)
    engine = _engine(configuration)
    BASELINE_COHERENCE.apply_baseline(engine, state)
    fields = engine.surface_fields(state)

    assert fields[T]["value"] == 280.0
    assert fields[D]["value"] > fields[T]["value"]
    assert fields[D]["value"] - fields[T]["value"] < 1e-6
    assert fields[RH]["value"] is None
    assert fields[RH]["missing_reasons"] == ["RH unavailable: dew point exceeds temperature"]
    assert state.coherence_events["blended_dew_point_consistency"]["status"] == "evaluated"
    humidity = state.coherence_events["relative_humidity"]
    assert humidity["status"] == "unavailable"
    assert "adjusted" not in humidity["actions"]


def test_current_inconsistent_dew_is_excluded_without_changing_temperature(configuration):
    native = _sources()
    native["HRRR"].update({T: 280.0, D: 280.0})
    native["GFS"].update({T: 300.0, D: 300.0})
    state = BlendState(horizon=19, contributors=native)
    engine = _engine(configuration)
    BASELINE_COHERENCE.apply_baseline(engine, state)
    fields = engine.surface_fields(state)

    assert fields[T]["value"] == 286.0
    assert fields[D]["value"] is None
    assert fields[D]["status"] == "inconsistent"
    assert fields[D]["weights"] == {"HRRR": 0.6, "GFS": 0.4}
    assert fields[RH]["value"] is None
    event = state.coherence_events["blended_dew_point_consistency"]
    assert "excluded" in event["actions"]
    assert "adjusted" not in event["actions"]


def test_approved_missingness_and_source_exclusion_do_not_fail_baseline_validation(configuration):
    engine = _engine(configuration)
    missing = BlendState(horizon=1, contributors={})
    native = _sources()
    native["HRRR"][G] = 4.0
    original = deepcopy(native)
    fallback = BlendState(horizon=1, contributors=native)
    with collect_baseline_coherence() as audit:
        BASELINE_COHERENCE.apply_baseline(engine, missing)
        BASELINE_COHERENCE.apply_baseline(engine, fallback)
    audit.validate(expected_states=2)

    assert all(event["status"] != "failed" for event in missing.coherence_events.values())
    assert any(event["status"] == "unavailable" for event in missing.coherence_events.values())
    fields = engine.surface_fields(fallback)
    assert fields[U]["weights"] == fields[V]["weights"] == fields[G]["weights"] == {"GFS": 1.0}
    assert "excluded" in fallback.coherence_events["native_source_consistency"]["actions"]
    assert native == original


def test_actual_gust_invariant_failure_is_not_relabelled_approved_missingness(configuration):
    engine = _engine(configuration)
    state = BlendState(horizon=1, contributors=_sources())
    # A corrupted upstream result violates the existing gust-versus-speed invariant.
    state.results[WIND] = {"wind_speed_10m": {"value": 20.0}}
    with collect_baseline_coherence() as audit:
        with pytest.raises(GustInvariantError):
            BASELINE_COHERENCE.apply_baseline(engine, state)
    assert state.coherence_events["blended_gust_consistency"]["status"] == "failed"
    with pytest.raises(CoherenceError):
        audit.validate(expected_states=1)


def test_audit_requires_execution_and_counts_one_state_once(configuration):
    with collect_baseline_coherence() as empty:
        pass
    with pytest.raises(CoherenceError, match="incomplete"):
        empty.validate(expected_states=1)

    state = BlendState(horizon=1, contributors=_sources())
    engine = _engine(configuration)
    with collect_baseline_coherence() as audit:
        BASELINE_COHERENCE.apply_baseline(engine, state)
        original = deepcopy(BASELINE_COHERENCE.report(state))
        BASELINE_COHERENCE.apply_baseline(engine, state)
        engine.surface_fields(state)
    audit.validate(expected_states=1)
    assert audit.report()["calculated_cell_hours"] == 1
    assert BASELINE_COHERENCE.report(state) == original
    for result in audit.report()["relationships"].values():
        assert sum(result["outcomes"].values()) == 1


def test_replay_is_deterministic_and_deferred_relationships_do_not_promote_evidence(configuration):
    engine = _engine(configuration)
    first = BlendState(horizon=19, contributors=_sources())
    evidence = {"value": 0.9, "policy": "evidence_only", "active_weight": 0.0}
    # Zero deterministic QPF must not force a positive thunder probability to
    # zero or change its independent event interval/source role.
    first.results[QPF] = {"value": 0.0, "unit": "kg/m^2"}
    first.results[THUNDER] = deepcopy(evidence)
    second = BlendState(horizon=19, contributors=_sources())
    second.results[QPF] = deepcopy(first.results[QPF])
    second.results[THUNDER] = deepcopy(evidence)
    BASELINE_COHERENCE.apply_baseline(engine, first)
    BASELINE_COHERENCE.apply_baseline(engine, second)
    assert first.results == second.results
    assert first.coherence_events == second.coherence_events
    assert first.results[THUNDER] == evidence
    assert first.results[QPF] == {"value": 0.0, "unit": "kg/m^2"}
    assert not (
        {key for key, rule in RELATIONSHIP_REGISTRY.items() if rule.status != "enforced"}
        & first.coherence_events.keys()
    )
