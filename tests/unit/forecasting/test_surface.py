"""Focused surface diagnostics against literal approved rows and independent examples."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mesoforge.catalog.configuration import Phase2BlendConfiguration, load_configuration_source
from mesoforge.forecasting.field_blend import (
    FIELD_REGISTRY,
    QPF,
    RH,
    WIND,
    BlendState,
    FieldBlendEngine,
)
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION
from mesoforge.forecasting.surface import (
    RH_POLICY,
    SurfaceBlendError,
    relative_humidity_percent,
)

T = "air_temperature_2m"
D = "dew_point_temperature_2m"
U = "eastward_wind_10m"
V = "northward_wind_10m"
G = "wind_gust_10m"


@pytest.fixture(scope="module")
def configuration() -> Phase2BlendConfiguration:
    root = Path(__file__).resolve().parents[3]
    loaded, _ = load_configuration_source(
        base_path=root / "configs/base.yaml",
        additional_overlay_paths=(
            root / "configs/phase1-grasston.yaml",
            root / "configs/phase2-grasston.yaml",
        ),
    )
    assert loaded.phase2 is not None
    return loaded.phase2.blend_configuration


def _blend(
    *,
    contributors: dict[str, dict[str, float | None]],
    horizon: int,
    configuration: Phase2BlendConfiguration,
) -> dict:
    engine = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)
    state = BlendState(horizon=horizon, contributors=contributors)
    return {"fields": engine.surface_fields(state), "source_validation": state.source_validation}


def _sources() -> dict[str, dict[str, float | None]]:
    return {
        "HRRR": {T: 300.0, D: 280.0, U: 3.0, V: 4.0, G: 8.0},
        "GFS": {T: 310.0, D: 290.0, U: 6.0, V: 8.0, G: 12.0},
    }


@pytest.mark.parametrize(
    "horizon,dew,u,v,gust,weights",
    [
        (1, 283.0, 3.9, 5.2, 9.2, {"HRRR": 0.7, "GFS": 0.3}),
        (18, 283.0, 3.9, 5.2, 9.2, {"HRRR": 0.7, "GFS": 0.3}),
        (19, 284.0, 4.2, 5.6, 9.6, {"HRRR": 0.6, "GFS": 0.4}),
        (36, 284.0, 4.2, 5.6, 9.6, {"HRRR": 0.6, "GFS": 0.4}),
    ],
)
def test_exact_approved_rows_preserve_temperature(
    configuration: Phase2BlendConfiguration,
    horizon: int,
    dew: float,
    u: float,
    v: float,
    gust: float,
    weights: dict[str, float],
) -> None:
    fields = _blend(contributors=_sources(), horizon=horizon, configuration=configuration)["fields"]
    assert fields[T]["value"] == 303.0
    assert fields[D]["value"] == pytest.approx(dew)
    assert fields[U]["value"] == pytest.approx(u)
    assert fields[V]["value"] == pytest.approx(v)
    assert fields[G]["value"] == pytest.approx(gust)
    assert fields[D]["weights"] == fields[U]["weights"] == fields[G]["weights"] == weights
    assert fields[D]["row_sha256"]
    assert fields[T]["unit"] == fields[D]["unit"] == "K"
    assert fields[U]["unit"] == fields[G]["unit"] == "m/s"
    assert fields["relative_humidity_2m"]["unit"] == "%"
    assert fields["wind_from_direction_10m"]["unit"] == "degree"


def test_rh_ratio_matches_independent_vapor_pressure_example() -> None:
    # UCAR CEOP equations: at 20C es=23.36947123406443 hPa;
    # at 10C e=12.271695993898764 hPa. Their ratio is 52.5116545 percent.
    assert relative_humidity_percent(temperature_k=293.15, dew_point_k=283.15) == pytest.approx(
        52.5116545, abs=1e-7
    )
    assert relative_humidity_percent(temperature_k=263.15, dew_point_k=263.15) == 100.0


@pytest.mark.parametrize(
    "temperature,dew",
    [(300.0, 301.0), (float("nan"), 280.0), (280.0, float("inf")), (140.0, 130.0)],
)
def test_rh_rejects_invalid_without_clamping(temperature: float, dew: float) -> None:
    with pytest.raises(SurfaceBlendError):
        relative_humidity_percent(temperature_k=temperature, dew_point_k=dew)


def test_vector_wrap_is_not_direction_average(configuration: Phase2BlendConfiguration) -> None:
    sources = _sources()
    sources["HRRR"].update({U: 30.0, V: -30.0, G: 80.0})
    sources["GFS"].update({U: -70.0, V: -30.0, G: 80.0})
    fields = _blend(contributors=sources, horizon=1, configuration=configuration)["fields"]
    assert fields[U]["value"] == 0.0
    assert fields[V]["value"] == -30.0
    assert fields["wind_speed_10m"]["value"] == 30.0
    assert fields["wind_from_direction_10m"]["value"] == 0.0


def test_opposing_vectors_cancel_and_calm_direction_is_missing(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"].update({U: 20.0, V: 0.0, G: 40.0})
    sources["GFS"].update({U: -30.0, V: 0.0, G: 40.0})
    fields = _blend(contributors=sources, horizon=19, configuration=configuration)["fields"]
    assert fields["wind_speed_10m"]["value"] == 0.0
    assert fields["wind_from_direction_10m"]["value"] is None
    assert "calm" in fields["wind_from_direction_10m"]["missing_reasons"][-1]


def test_missing_gust_uses_approved_coupled_fallback_only(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"][G] = None
    result = _blend(contributors=sources, horizon=1, configuration=configuration)
    fields = result["fields"]
    assert fields[D]["value"] == 283.0  # Independent moisture survives wind exclusion.
    assert fields[U]["value"] == 6.0
    assert fields[V]["value"] == 8.0
    assert fields[G]["value"] == 12.0
    assert fields[U]["weights"] == fields[G]["weights"] == {"GFS": 1.0}
    assert fields[G]["missing_reasons"]
    assert result["source_validation"]["HRRR"]["wind_gust"]["rejection_scope"] == (
        "coupled-wind-gust-point"
    )


def test_gust_validation_floor_is_recorded_without_mutating_sources(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"][G] = 4.95
    original = deepcopy(sources)
    result = _blend(contributors=sources, horizon=1, configuration=configuration)
    validation = result["source_validation"]["HRRR"]["wind_gust"]
    assert validation["source_gust_m_s"] == 4.95
    assert validation["validated_gust_m_s"] == 5.0
    assert validation["source_gust_floor_applied"] is True
    assert result["fields"][G]["value"] == pytest.approx(7.1)
    assert sources == original


def test_material_gust_failure_rejects_whole_tuple(configuration: Phase2BlendConfiguration) -> None:
    sources = _sources()
    sources["HRRR"][G] = 4.0
    fields = _blend(contributors=sources, horizon=1, configuration=configuration)["fields"]
    assert fields[U]["weights"] == fields[V]["weights"] == fields[G]["weights"] == {"GFS": 1.0}


def test_shadow_values_cannot_change_active_baseline(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    control = _blend(contributors=sources, horizon=36, configuration=configuration)
    sources.update({"RAP": {T: 200.0, D: 190.0, U: 90.0, V: 0.0, G: 100.0}, "IFS": {}})
    with_shadows = _blend(contributors=sources, horizon=36, configuration=configuration)
    assert with_shadows["fields"] == control["fields"]


def test_late_dew_inconsistency_preserves_temperature(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"].update({T: 280.0, D: 280.0})
    sources["GFS"].update({T: 300.0, D: 300.0})
    fields = _blend(contributors=sources, horizon=19, configuration=configuration)["fields"]
    assert fields[T]["value"] == 286.0
    assert fields[D]["value"] is None  # 60/40 dew=288 K exceeds unchanged 70/30 T=286 K.
    assert fields[D]["status"] == "inconsistent"
    assert fields["relative_humidity_2m"]["value"] is None
    assert "exceeds" in fields[D]["missing_reasons"][-1]


def test_missing_baseline_temperature_does_not_create_rh(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"][T] = None
    fields = _blend(contributors=sources, horizon=1, configuration=configuration)["fields"]
    assert fields[T]["value"] is None
    assert fields[D]["value"] is None
    assert fields["relative_humidity_2m"]["value"] is None
    assert fields[U]["value"] is not None


def test_no_guidance_and_no_cloud_policy_are_explicit(
    configuration: Phase2BlendConfiguration,
) -> None:
    fields = _blend(contributors={}, horizon=1, configuration=configuration)["fields"]
    for field in fields.values():
        assert field["value"] is None
        assert field["missing_reasons"]
    assert fields["cloud_area_fraction"]["policy"] == "no-approved-cloud-blend-policy"


def test_registry_selects_existing_policy_identities(
    configuration: Phase2BlendConfiguration,
) -> None:
    engine = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)
    assert set(FIELD_REGISTRY) == {
        T,
        D,
        WIND,
        G,
        QPF,
        RH,
        "cloud_area_fraction",
        "probability_of_precipitation_6h",
    }
    assert engine.policy_for(T) is DEFAULT_CONFIGURATION.control_recipe
    assert engine.policy_for(D) is configuration.scalar_vector_table
    assert engine.policy_for(WIND) is configuration.scalar_vector_table
    assert engine.policy_for(G) is configuration.scalar_vector_table
    assert engine.policy_for(QPF) is configuration.qpf_table
    assert engine.policy_for(RH) == RH_POLICY
    with pytest.raises(KeyError):
        engine.policy_for("unregistered_weather_field")
    with pytest.raises(TypeError):
        FIELD_REGISTRY["unregistered_weather_field"] = FIELD_REGISTRY[T]


def test_dispatch_dependencies_are_order_independent_and_preserve_native_evidence(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"][G] = 4.95
    original = deepcopy(sources)
    engine = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)
    first = BlendState(horizon=19, contributors=sources)
    gust = deepcopy(engine.blend_field(G, first))
    humidity = deepcopy(engine.blend_field(RH, first))
    wind = deepcopy(engine.blend_field(WIND, first))
    fields = engine.surface_fields(first)
    normal_order = engine.surface_fields(BlendState(horizon=19, contributors=deepcopy(sources)))
    assert fields == normal_order
    assert gust == fields[G] == engine.blend_field(G, first)
    assert humidity == fields[RH] == engine.blend_field(RH, first)
    assert wind == {key: fields[key] for key in (U, V, "wind_speed_10m", "wind_from_direction_10m")}
    assert sources == original
    assert first.source_validation["HRRR"]["wind_gust"]["source_gust_m_s"] == 4.95
    assert first.source_validation["HRRR"]["wind_gust"]["validated_gust_m_s"] == 5.0


def _precipitation(horizon: int, hrrr: float | None, gfs: float | None) -> dict:
    end = datetime(2026, 9, 11, tzinfo=UTC) + timedelta(hours=horizon)
    start = end - timedelta(hours=1)
    return {
        model: {
            "value": value,
            "unit": "kg/m^2",
            "temporal_semantics": "accumulation",
            "interval_start": start.isoformat().replace("+00:00", "Z"),
            "interval_end": end.isoformat().replace("+00:00", "Z"),
            "interval_closure": "left_open_right_closed",
            "missing_reasons": [] if value is not None else [f"{model}: no exact hourly interval"],
            "source_cycle": "2026-09-11T00:00:00Z",
            "source_lead_hours": horizon,
            "provenance": {"prepared_sha256": model.lower() + "-retained-evidence"},
        }
        for model, value in (("HRRR", hrrr), ("GFS", gfs))
    }


@pytest.mark.parametrize(
    "horizon,amount,weights,row_id",
    [
        (18, 1.6, {"HRRR": 0.7, "GFS": 0.3}, "qpf.hg.h01-h18"),
        (19, 1.8, {"HRRR": 0.6, "GFS": 0.4}, "qpf.hg.h19-h36"),
    ],
)
def test_qpf_dispatch_keeps_exact_interval_policy_and_evidence(
    configuration: Phase2BlendConfiguration,
    horizon: int,
    amount: float,
    weights: dict[str, float],
    row_id: str,
) -> None:
    native = _precipitation(horizon, 1.0, 3.0)
    original = deepcopy(native)
    engine = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)
    field = engine.blend_field(
        QPF, BlendState(horizon=horizon, contributors=_sources(), precipitation=native)
    )
    assert field["value"] == pytest.approx(amount)
    assert field["weights"] == weights
    assert field["policy"] == "phase2-qpf-fallback.v1"
    assert field["row_id"] == row_id
    assert field["row_sha256"] == str(
        configuration.qpf_table.row_for(available_models=("HRRR", "GFS"), horizon=horizon).digest
    )
    for key in ("unit", "temporal_semantics", "interval_start", "interval_end", "interval_closure"):
        assert field[key] == native["HRRR"][key]
    assert field["status"] == "available"
    assert native == original


def test_qpf_dispatch_distinguishes_zero_missing_and_explicit_fallback(
    configuration: Phase2BlendConfiguration,
) -> None:
    engine = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)

    def blend(hrrr: float | None, gfs: float | None) -> dict:
        return engine.blend_field(
            QPF,
            BlendState(
                horizon=1, contributors=_sources(), precipitation=_precipitation(1, hrrr, gfs)
            ),
        )

    zero = blend(0.0, 0.0)
    assert zero["value"] == 0.0
    assert zero["missing_reasons"] == []
    fallback = blend(None, 3.0)
    assert fallback["value"] == 3.0
    assert fallback["weights"] == {"GFS": 1.0}
    assert fallback["status"] == "fallback"
    assert fallback["missing_reasons"] == ["HRRR: no exact hourly interval"]
    missing = blend(None, None)
    assert missing["value"] is None
    assert missing["weights"] == {}
    assert missing["status"] == "unavailable"
    assert missing["missing_reasons"] == [
        "HRRR: no exact hourly interval",
        "GFS: no exact hourly interval",
    ]


@pytest.mark.parametrize(
    "key,incompatible",
    [
        ("interval_start", "2026-09-10T23:00:00Z"),
        ("interval_end", "2026-09-11T02:00:00Z"),
        ("unit", "in"),
        ("temporal_semantics", "instantaneous"),
        ("interval_closure", "left_closed_right_open"),
    ],
)
def test_qpf_dispatch_never_combines_incompatible_accumulation_metadata(
    configuration: Phase2BlendConfiguration, key: str, incompatible: str
) -> None:
    native = _precipitation(1, 1.0, 3.0)
    native["GFS"][key] = incompatible
    original = deepcopy(native)
    field = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration).blend_field(
        QPF, BlendState(horizon=1, contributors=_sources(), precipitation=native)
    )
    assert field["value"] == 1.0
    assert field["weights"] == {"HRRR": 1.0}
    assert field["status"] == "fallback"
    assert field["missing_reasons"] == ["GFS: incompatible QPF accumulation metadata"]
    assert field["interval_start"] == "2026-09-11T00:00:00Z"
    assert field["interval_end"] == "2026-09-11T01:00:00Z"
    assert native == original


def test_historical_temperature_only_recipe_keeps_extended_horizon_compatibility(
    configuration: Phase2BlendConfiguration,
) -> None:
    state = BlendState(horizon=42, contributors=_sources())
    temperature = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION).blend_field(T, state)
    assert temperature["value"] == 303.0
    assert temperature["policy"] == "temperature_control_v1"
    assert temperature["weights"] == {"HRRR": 0.7, "GFS": 0.3}
    with pytest.raises(SurfaceBlendError, match="integer from 1 through 36"):
        FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration).blend_field(
            D, BlendState(horizon=42, contributors=_sources())
        )
