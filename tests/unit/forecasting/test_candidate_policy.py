"""Explicit fixture-only alternatives reuse current kernels and cannot become active."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.catalog.configuration import FallbackWeightTable
from mesoforge.forecasting.candidate_policy import CandidateBlendPolicy
from mesoforge.forecasting.field_blend import (
    DEW_POINT,
    GUST,
    QPF,
    RH,
    WIND,
    BlendState,
    FieldBlendEngine,
)
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, Recipe
from tests.unit.forecasting.test_surface import T, _sources
from tests.unit.forecasting.test_surface import configuration as configuration  # noqa: F401

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def temperature_policy(**changes):
    return CandidateBlendPolicy(
        **{
            "policy_id": "fixture-temperature-equal",
            "version": "1",
            "field": T,
            "parent_policy": "temperature_control_v1/1",
            "parameters": Recipe.model_validate_json(
                json.dumps(
                    {
                        "name": "fixture-temperature-equal",
                        "version": "1",
                        "field": T,
                        "contributors": [
                            {"model": "HRRR", "weight": 0.5},
                            {"model": "GFS", "weight": 0.5},
                        ],
                    }
                )
            ),
            "missing_behavior": "require_all",
            "creation_source": "unit-test fixture, not an evidence-qualified policy",
            "evidence_cutoff": NOW - timedelta(days=1),
            "created_at": NOW,
            "provenance": {"fixture": True},
            **changes,
        }
    )


def test_explicit_recipe_dispatch_preserves_active_and_native_values(configuration):
    native = _sources()
    original = deepcopy(native)
    policy = temperature_policy()
    override = {T: policy.parameters}
    active = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)
    candidate = FieldBlendEngine(
        contributors=DEFAULT_CONFIGURATION, phase2=configuration, policy_overrides=override
    )
    override.clear()  # The engine owns an immutable copy of the explicit selection.
    assert active.blend_field(T, BlendState(horizon=1, contributors=native))["value"] == 303
    result = candidate.blend_field(T, BlendState(horizon=1, contributors=native))
    assert result["value"] == 305
    assert result["policy"] == policy.policy_id
    assert result["policy_version"] == "1"
    assert result["weights"] == {"HRRR": 0.5, "GFS": 0.5}
    native["GFS"][T] = None
    missing = candidate.blend_field(T, BlendState(horizon=1, contributors=native))
    assert missing["value"] is None
    assert missing["missing_models"] == ["GFS"]
    assert missing["weights"] == result["weights"]
    native["GFS"][T] = original["GFS"][T]
    assert native == original
    assert active.blend_field(T, BlendState(horizon=1, contributors=native))["value"] == 303


def test_interval_candidate_uses_same_qpf_kernel_and_missing_rules(configuration):
    data = configuration.qpf_table.model_dump(mode="json")
    data["table_id"] = "fixture-qpf-equal.v1"
    for row in data["rows"]:
        if row["available_models"] == ["HRRR", "GFS"]:
            row["weights"] = [0.5, 0.0, 0.5]
    table = FallbackWeightTable.model_validate_json(json.dumps(data))
    policy = temperature_policy(
        policy_id=table.table_id,
        field=QPF,
        parent_policy=configuration.qpf_table.table_id,
        parameters=table,
        missing_behavior="approved_subset_row",
    )
    engine = FieldBlendEngine(
        contributors=DEFAULT_CONFIGURATION, phase2=configuration, policy_overrides={QPF: table}
    )
    native = {
        model: {
            "value": value,
            "unit": "kg/m^2",
            "temporal_semantics": "interval_accumulation",
            "interval_start": "2026-01-02T00:00:00Z",
            "interval_end": "2026-01-02T01:00:00Z",
            "interval_closure": "(start,end]",
            "missing_reasons": [],
        }
        for model, value in (("HRRR", 0.0), ("GFS", 4.0))
    }
    original = deepcopy(native)
    for lead in (1, 18, 19, 36):
        result = engine.blend_field(
            QPF, BlendState(horizon=lead, contributors=_sources(), precipitation=native)
        )
        assert result["value"] == 2
        assert result["policy"] == policy.policy_id
        assert result["interval_start"] == native["HRRR"]["interval_start"]
        assert result["interval_end"] == native["HRRR"]["interval_end"]
    native["GFS"]["interval_start"] = "2026-01-01T22:00:00Z"
    result = engine.blend_field(
        QPF, BlendState(horizon=1, contributors=_sources(), precipitation=native)
    )
    assert result["value"] == 0
    assert result["weights"] == {"HRRR": 1}
    assert result["missing_reasons"] == ["GFS: incompatible QPF accumulation metadata"]
    assert original["GFS"]["value"] == 4


@pytest.mark.parametrize("field", [DEW_POINT, WIND, GUST])
def test_explicit_field_table_preserves_specialized_surface_kernels(configuration, field):
    data = configuration.scalar_vector_table.model_dump(mode="json")
    data["table_id"] = "fixture-surface-equal.v1"
    for row in data["rows"]:
        if row["available_models"] == ["HRRR", "GFS"]:
            row["weights"] = [0.5, 0.0, 0.5]
    table = FallbackWeightTable.model_validate_json(json.dumps(data))
    engine = FieldBlendEngine(
        contributors=DEFAULT_CONFIGURATION,
        phase2=configuration,
        policy_overrides={field: table},
    )
    fields = engine.surface_fields(BlendState(horizon=1, contributors=_sources()))
    assert fields[T]["value"] == 303
    if field == WIND:
        assert fields["eastward_wind_10m"]["value"] == 4.5
        assert fields["northward_wind_10m"]["value"] == 6
        assert fields["wind_speed_10m"]["value"] == 7.5
        assert fields[GUST]["value"] == 9.2  # Gust keeps its separate active table.
    else:
        assert fields[field]["value"] == (285 if field == DEW_POINT else 10)
    key = "eastward_wind_10m" if field == WIND else field
    assert fields[key]["policy"] == table.table_id
    assert fields[key]["weights"] == {"HRRR": 0.5, "GFS": 0.5}


def test_candidate_lifecycle_identity_and_no_future_leakage():
    policy = temperature_policy(lifecycle_role="shadow", activated_at=NOW + timedelta(hours=1))
    replay = CandidateBlendPolicy.model_validate_json(policy.model_dump_json())
    assert replay.digest == policy.digest
    assert replay.contributors == ("HRRR", "GFS")
    with pytest.raises(ValueError, match="activation"):
        replay.validate_execution(NOW)
    replay.validate_execution(NOW + timedelta(hours=1))
    with pytest.raises(ValueError, match="creation"):
        temperature_policy().validate_execution(NOW - timedelta(hours=1))
    with pytest.raises(ValueError, match="evidence"):
        temperature_policy().validate_execution(NOW - timedelta(days=2))
    with pytest.raises(ValueError, match="timezone"):
        temperature_policy(created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        temperature_policy(lifecycle_role="active")
    with pytest.raises(ValueError, match="activation"):
        temperature_policy(lifecycle_role="shadow")
    with pytest.raises(ValueError, match="identity"):
        temperature_policy(policy_id="a-different-policy")
    with pytest.raises(ValueError, match="kernel"):
        temperature_policy(field=RH)
