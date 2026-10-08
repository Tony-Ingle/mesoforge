"""Provisional influence is explicit; eligibility never fabricates source events."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.forecasting.cloud_cover import CLOUD, sky_category
from mesoforge.forecasting.field_blend import QPF, RH, BlendState, FieldBlendEngine
from mesoforge.forecasting.provisional_policy import (
    POP6,
    POP6_THRESHOLD,
    PROVISIONAL_MODELS,
    PROVISIONAL_MULTIMODEL_POLICY,
    provisional_policy,
    provisional_weights,
)
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    PROVISIONAL_CONFIGURATION,
    ContributorConfiguration,
    with_surface_fields,
)
from mesoforge.forecasting.surface import SurfaceBlendError, relative_humidity_percent
from tests.unit.forecasting.test_surface import D, G, T, U, V
from tests.unit.forecasting.test_surface import configuration as configuration

CYCLE = datetime(2026, 10, 8, tzinfo=UTC)


def probability_state():
    end = CYCLE + timedelta(hours=6)
    return BlendState(
        6,
        {},
        source_context={
            model: {"cycle": CYCLE, "reference_time": CYCLE} for model in ("NBM", "GEFS")
        },
        probability_interval=(CYCLE, end),
        probabilities={
            model: {
                "source_id": source_id,
                "value": value,
                "unit": "1",
                "source_cycle": CYCLE.isoformat(),
                "source_lead_hours": 6,
                "interval_start": CYCLE.isoformat(),
                "interval_end": end.isoformat(),
                "interval_closure": "left_open_right_closed",
                "threshold": dict(POP6_THRESHOLD),
                "spatial_support": {"kind": "grid_point"},
                "missing_reasons": [],
                "manifest_sha256": model,
            }
            for model, source_id, value in (("NBM", "NBM_6H", 0.3), ("GEFS", "GEFS_6H", 0.6))
        },
    )


def test_six_hour_probability_blends_identical_events_without_hourly_conversion(configuration):
    state = probability_state()
    before = deepcopy(state.probabilities)
    result = _engine(configuration).blend_field(POP6, state)
    assert result["weights"] == {"NBM": 2 / 3, "GEFS": 1 / 3}
    assert result["value"] == pytest.approx(0.4)
    assert result["event_duration_hours"] == 6
    assert result["threshold"] == dict(POP6_THRESHOLD)
    assert result["contributors"] == before == state.probabilities
    assert result["policy"] == provisional_policy(POP6).policy_id
    assert result["source_influence"]["policy"]["skill_claim"] is False
    assert result["source_influence"]["policy"]["lead_transition"] == "constant role priors"
    assert _engine(configuration).policy_for(POP6).models == ("NBM", "GEFS")
    from mesoforge.forecasting.field_blend import field_edit_contract

    assert not field_edit_contract(POP6).operations
    with pytest.raises(SurfaceBlendError, match="explicit provisional"):
        FieldBlendEngine(DEFAULT_CONFIGURATION, configuration).blend_field(
            POP6, probability_state()
        )


@pytest.mark.parametrize(
    "change",
    [
        {"threshold": {"value": 1.0, "unit": "kg/m^2", "comparison": "ge"}},
        {"spatial_support": {"kind": "grid_box_mean"}},
        {"source_id": "ECMWF_ENS_24H"},
        {"interval_start": (CYCLE - timedelta(hours=18)).isoformat()},
        {"source_cycle": (CYCLE - timedelta(hours=6)).isoformat()},
        {"value": 1.01},
        {"value": None},
        {"missing_reasons": ["unavailable"]},
    ],
)
def test_six_hour_probability_excludes_incompatible_or_missing_evidence(configuration, change):
    state = probability_state()
    state.probabilities["GEFS"].update(change)
    result = _engine(configuration).blend_field(POP6, state)
    assert result["value"] == 0.3
    assert result["weights"] == {"NBM": 1.0}
    assert result["contributor_exclusions"]["GEFS"]


def test_six_hour_probability_preserves_zero_and_enforces_freshness_and_cutoff(configuration):
    state = probability_state()
    state.probabilities["GEFS"]["value"] = 0.0
    state.probabilities["NBM"]["value"] = None
    assert _engine(configuration).blend_field(POP6, state)["value"] == 0.0
    state = probability_state()
    state.source_context["GEFS"].update(
        available_at=CYCLE + timedelta(minutes=1),
        information_cutoff=CYCLE,
    )
    result = _engine(configuration).blend_field(POP6, state)
    assert result["weights"] == {"NBM": 1.0}
    assert "availability exceeds" in str(
        result["source_influence"]["contributors"]["GEFS"]["reasons"]
    )
    state = probability_state()
    state.probabilities.clear()
    result = _engine(configuration).blend_field(POP6, state)
    assert result["value"] is None and result["status"] == "unavailable"
    state = probability_state()
    state.probability_interval = (CYCLE, CYCLE + timedelta(hours=1))
    with pytest.raises(SurfaceBlendError, match="exact whole-hour"):
        _engine(configuration).blend_field(POP6, state)


def _context(reference=CYCLE):
    return {model: {"cycle": CYCLE, "reference_time": reference} for model in PROVISIONAL_MODELS}


def _sources():
    return {
        model: {
            T: 285.0 + index,
            D: 275.0 + index,
            U: 3.0,
            V: 4.0,
            G: None if model == "IFS" else 10.0,
            CLOUD: index / 4,
        }
        for index, model in enumerate(PROVISIONAL_MODELS)
    }


def _engine(configuration):
    return FieldBlendEngine(
        PROVISIONAL_CONFIGURATION, configuration, policy_family=PROVISIONAL_MULTIMODEL_POLICY
    )


def test_explicit_policy_uses_all_compatible_sources_without_mutating_evidence(configuration):
    sources = _sources()
    before = deepcopy(sources)
    state = BlendState(1, sources, source_context=_context())
    fields = _engine(configuration).surface_fields(state)
    expected = {"HRRR": 0.25, "RAP": 0.25, "GFS": 0.125, "IFS": 0.125, "NBM": 0.25}
    assert fields[T]["weights"] == expected
    assert fields[T]["value"] == sum(
        sources[model][T] * weight for model, weight in expected.items()
    )
    assert fields[T]["policy"] == provisional_policy(T).policy_id
    assert fields[D]["weights"] == expected
    assert fields[RH]["value"] == relative_humidity_percent(
        temperature_k=fields[T]["value"], dew_point_k=fields[D]["value"]
    )
    assert fields[U]["value"] == 3
    assert fields[V]["value"] == 4
    assert fields["wind_speed_10m"]["value"] == 5
    assert "IFS" in fields[U]["weights"]
    assert "IFS" not in fields[G]["weights"]
    assert fields[G]["value"] == pytest.approx(10, abs=2e-15)
    assert (
        state.source_validation["IFS"]["wind_gust"]["rejection_scope"]
        == "instantaneous_gust_only_unavailable"
    )
    assert fields[CLOUD]["unit"] == "1"
    assert fields[CLOUD]["value"] == fields[CLOUD]["normalized_fraction"]
    assert fields[CLOUD]["cloud_percentage"] == fields[CLOUD]["value"] * 100
    assert fields[CLOUD]["sky_category"] == sky_category(fields[CLOUD]["cloud_percentage"])
    assert fields[T]["source_influence"]["policy"]["skill_claim"] is False
    assert sources == before


def test_hour120_expires_short_range_and_renormalizes_available_long_range(configuration):
    state = BlendState(120, _sources(), source_context=_context())
    fields = _engine(configuration).surface_fields(state)
    assert set(fields[T]["weights"]) == {"GFS", "IFS", "NBM"}
    assert fields[T]["weights"] == {"GFS": 1 / 3, "IFS": 1 / 3, "NBM": 1 / 3}
    for model in ("HRRR", "RAP"):
        assert not fields[T]["source_influence"]["contributors"][model]["eligible"]
        assert "outside_native_horizon" in str(
            fields[T]["source_influence"]["contributors"][model]["reasons"]
        )
    assert fields[RH]["value"] is not None


def test_dynamic_fraction_weights_preserve_unit_bounds_without_clipping(configuration):
    sources = _sources()
    for row in sources.values():
        row[CLOUD] = 1.0
    state = BlendState(40, sources, source_context=_context(CYCLE + timedelta(hours=7)))
    cloud = _engine(configuration).blend_field(CLOUD, state)
    assert cloud["value"] == 1.0
    assert cloud["cloud_percentage"] == 100.0
    probability = probability_state()
    for row in probability.probabilities.values():
        row["value"] = 1.0
    assert _engine(configuration).blend_field(POP6, probability)["value"] == 1.0


def test_fixed_family_allocation_has_no_sibling_rebudget_jump_at_expiry():
    context = _context(CYCLE + timedelta(hours=3))
    context["RAP"]["cycle"] = CYCLE + timedelta(hours=3)
    # Forecast leads45/46 are HRRR source leads48/49; RAP remains at45/46.
    before = provisional_weights(
        T, lead=45, available_models=PROVISIONAL_MODELS, source_context=context
    )[1]["contributors"]
    after = provisional_weights(
        T, lead=46, available_models=PROVISIONAL_MODELS, source_context=context
    )[1]["contributors"]
    assert before["HRRR"]["native_horizon_factor"] > 0
    assert after["HRRR"]["eligible"] is False
    assert before["RAP"]["within_role_members"] == after["RAP"]["within_role_members"] == 2
    # Changing lead priors/taper is explicit; loss of HRRR never doubles RAP's allocation.
    for row in (before["RAP"], after["RAP"]):
        assert (
            row["raw_influence"]
            == row["role_prior"] / 2 * row["freshness_factor"] * row["native_horizon_factor"]
        )


def test_freshness_declines_smoothly_but_old_cycles_are_ineligible():
    scores = []
    for age in (0, 6, 12, 24):
        weights, evidence = provisional_weights(
            T,
            lead=1,
            available_models=("GFS",),
            source_context={
                "GFS": {"cycle": CYCLE, "reference_time": CYCLE + timedelta(hours=age)}
            },
        )
        assert weights == {"GFS": 1}
        scores.append(evidence["contributors"]["GFS"]["freshness_factor"])
    assert scores == [1, 0.5, 1 / 3, 0.2]
    weights, evidence = provisional_weights(
        T,
        lead=1,
        available_models=("GFS",),
        source_context={"GFS": {"cycle": CYCLE, "reference_time": CYCLE + timedelta(hours=25)}},
    )
    assert weights == {}
    assert "24-hour" in str(evidence["contributors"]["GFS"]["reasons"])


def test_missing_context_future_availability_and_source_lead_disagreement_are_explicit(
    configuration,
):
    context = _context()
    context["HRRR"] = {}
    context["RAP"]["source_lead_hours"] = 99
    context["GFS"].update(available_at=CYCLE + timedelta(hours=1), information_cutoff=CYCLE)
    result = _engine(configuration).blend_field(
        T, BlendState(1, _sources(), source_context=context)
    )
    assert set(result["weights"]) == {"IFS", "NBM"}
    for model in ("HRRR", "RAP", "GFS"):
        assert result["source_influence"]["contributors"][model]["reasons"]


def test_missing_instantaneous_gust_never_rejects_valid_ifs_wind_or_invents_gust(configuration):
    sources = {"IFS": _sources()["IFS"]}
    fields = _engine(configuration).surface_fields(
        BlendState(90, sources, source_context=_context())
    )
    assert fields[U]["value"] == 3
    assert fields[G]["value"] is None
    assert fields[G]["missing_reasons"]


def test_differing_gust_subset_cannot_force_new_floor(configuration):
    sources = {
        "GFS": {T: 280.0, D: 270.0, U: 0.0, V: 1.0, G: 2.0},
        "IFS": {T: 280.0, D: 270.0, U: 0.0, V: 50.0, G: None},
    }
    fields = _engine(configuration).surface_fields(
        BlendState(3, sources, source_context=_context())
    )
    assert fields["wind_speed_10m"]["value"] > 2
    assert fields[G]["value"] is None
    assert not fields[G]["final_gust_epsilon_floor_applied"]
    assert "invariant" in str(fields[G]["missing_reasons"])


def _qpf(value, start, end):
    return {
        "value": value,
        "unit": "kg/m^2",
        "interval_start": start,
        "interval_end": end,
        "interval_closure": "left_open_right_closed",
        "temporal_semantics": "accumulation",
        "missing_reasons": [],
    }


def test_qpf_exact_common_event_zero_missing_and_no_hrrr_anchor(configuration):
    end = CYCLE + timedelta(hours=123)
    start = end - timedelta(hours=3)
    native = {
        "GFS": _qpf(0.0, start, end),
        "IFS": _qpf(3.0, start, end),
        "NBM": _qpf(9.0, end - timedelta(hours=1), end),
    }
    # Three-hour event can end at a120h forecast lead from a3h-old source cycle.
    state = BlendState(
        120,
        {},
        precipitation=native,
        source_context=_context(CYCLE + timedelta(hours=3)),
        precipitation_interval=(start, end),
    )
    before = deepcopy(native)
    result = _engine(configuration).blend_field(QPF, state)
    assert result["value"] == 1.5
    assert result["weights"] == {"GFS": 0.5, "IFS": 0.5}
    assert result["accumulation_duration_hours"] == 3
    assert result["interval_start"] == "2026-10-13T00:00:00Z"
    assert "interval" in result["contributor_exclusions"]["NBM"]
    assert native == before
    missing = _engine(configuration).blend_field(
        QPF,
        BlendState(
            120,
            {},
            source_context=_context(),
            precipitation_interval=(CYCLE + timedelta(hours=119), CYCLE + timedelta(hours=120)),
        ),
    )
    assert missing["value"] is None
    assert missing["weights"] == {}


def test_qpf_cannot_guess_target_or_split_contributor_window(configuration):
    engine = _engine(configuration)
    with pytest.raises(SurfaceBlendError, match="explicit target"):
        engine.blend_field(QPF, BlendState(1, {}))
    end = CYCLE + timedelta(hours=6)
    native = {"GFS": _qpf(6.0, CYCLE, end)}
    result = engine.blend_field(
        QPF,
        BlendState(
            6,
            {},
            precipitation=native,
            source_context=_context(),
            precipitation_interval=(end - timedelta(hours=1), end),
        ),
    )
    assert result["value"] is None
    assert "interval" in result["contributor_exclusions"]["GFS"]


def test_legacy_remains_default_and_policy_selection_rejects_unknown(configuration):
    legacy = FieldBlendEngine(DEFAULT_CONFIGURATION, configuration)
    state = BlendState(1, _sources())
    result = legacy.blend_field(T, state)
    assert result["policy"] == "temperature_control_v1"
    assert result["weights"] == {"HRRR": 0.7, "GFS": 0.3}
    with pytest.raises(SurfaceBlendError, match="Unknown"):
        FieldBlendEngine(DEFAULT_CONFIGURATION, configuration, policy_family="guess")
    with pytest.raises(SurfaceBlendError, match="through 120"):
        _engine(configuration).blend_field(
            T, BlendState(121, _sources(), source_context=_context())
        )


def test_dynamic_configuration_does_not_fabricate_a_fixed_control_recipe():
    assert "field_policy_family" not in DEFAULT_CONFIGURATION.model_dump(mode="json")
    assert (
        ContributorConfiguration.model_validate_json(DEFAULT_CONFIGURATION.model_dump_json())
        == DEFAULT_CONFIGURATION
    )
    assert PROVISIONAL_CONFIGURATION.control_recipe is None
    assert PROVISIONAL_CONFIGURATION.recipes() == ()
    assert PROVISIONAL_CONFIGURATION.field_policy_family == PROVISIONAL_MULTIMODEL_POLICY
    assert set(PROVISIONAL_CONFIGURATION.model_map()) == set(PROVISIONAL_MODELS)
    assert with_surface_fields(PROVISIONAL_CONFIGURATION) is PROVISIONAL_CONFIGURATION
    assert (
        ContributorConfiguration.model_validate_json(PROVISIONAL_CONFIGURATION.model_dump_json())
        == PROVISIONAL_CONFIGURATION
    )
    with pytest.raises(ValueError, match="control recipe"):
        ContributorConfiguration(models=DEFAULT_CONFIGURATION.models, control_recipe=None)
    with pytest.raises(ValueError, match="fixed scalar"):
        ContributorConfiguration(
            models=PROVISIONAL_CONFIGURATION.models,
            control_recipe=DEFAULT_CONFIGURATION.control_recipe,
            field_policy_family=PROVISIONAL_MULTIMODEL_POLICY,
        )


def test_native_last_lead_keeps_positive_influence_without_extrapolation():
    weights, evidence = provisional_weights(
        T, lead=48, available_models=("HRRR",), source_context=_context()
    )
    assert weights == {"HRRR": 1}
    assert 0 < evidence["contributors"]["HRRR"]["native_horizon_factor"] < 1
    assert (
        provisional_weights(T, lead=49, available_models=("HRRR",), source_context=_context())[0]
        == {}
    )


def test_mixed_reference_times_do_not_blend_different_forecast_targets():
    context = _context()
    context["GFS"]["reference_time"] = CYCLE + timedelta(hours=1)
    weights, evidence = provisional_weights(
        T, lead=1, available_models=("HRRR", "GFS"), source_context=context
    )
    assert weights == {}
    assert "reference times disagree" in str(evidence)


def test_encoded_hourly_claim_cannot_override_native_ifs_or_late_gfs_time_support(configuration):
    engine = _engine(configuration)
    for model, source_end in (("IFS", 3), ("GFS", 123)):
        reference = CYCLE if source_end == 3 else CYCLE + timedelta(hours=3)
        end = CYCLE + timedelta(hours=source_end)
        start = end - timedelta(hours=1)
        lead = int((end - reference).total_seconds() / 3600)
        result = engine.blend_field(
            QPF,
            BlendState(
                lead,
                {},
                precipitation={model: _qpf(1.0, start, end)},
                source_context=_context(reference),
                precipitation_interval=(start, end),
            ),
        )
        assert result["value"] is None
        assert "no splitting" in result["contributor_exclusions"][model]
