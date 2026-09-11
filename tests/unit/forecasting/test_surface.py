"""Focused surface diagnostics against literal approved rows and independent examples."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from mesoforge.catalog.configuration import Phase2BlendConfiguration, load_configuration_source
from mesoforge.forecasting.surface import (
    SurfaceBlendError,
    blend_surface,
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
    fields = blend_surface(
        temperature_k=303.0, contributors=_sources(), horizon=horizon, configuration=configuration
    )["fields"]
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
    fields = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=1, configuration=configuration
    )["fields"]
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
    fields = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=19, configuration=configuration
    )["fields"]
    assert fields["wind_speed_10m"]["value"] == 0.0
    assert fields["wind_from_direction_10m"]["value"] is None
    assert "calm" in fields["wind_from_direction_10m"]["missing_reasons"][-1]


def test_missing_gust_uses_approved_coupled_fallback_only(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"][G] = None
    result = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=1, configuration=configuration
    )
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
    result = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=1, configuration=configuration
    )
    validation = result["source_validation"]["HRRR"]["wind_gust"]
    assert validation["source_gust_m_s"] == 4.95
    assert validation["validated_gust_m_s"] == 5.0
    assert validation["source_gust_floor_applied"] is True
    assert result["fields"][G]["value"] == pytest.approx(7.1)
    assert sources == original


def test_material_gust_failure_rejects_whole_tuple(configuration: Phase2BlendConfiguration) -> None:
    sources = _sources()
    sources["HRRR"][G] = 4.0
    fields = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=1, configuration=configuration
    )["fields"]
    assert fields[U]["weights"] == fields[V]["weights"] == fields[G]["weights"] == {"GFS": 1.0}


def test_shadow_values_cannot_change_active_baseline(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    control = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=36, configuration=configuration
    )
    sources.update({"RAP": {T: 200.0, D: 190.0, U: 90.0, V: 0.0, G: 100.0}, "IFS": {}})
    with_shadows = blend_surface(
        temperature_k=303.0, contributors=sources, horizon=36, configuration=configuration
    )
    assert with_shadows["fields"] == control["fields"]


def test_late_dew_inconsistency_preserves_temperature(
    configuration: Phase2BlendConfiguration,
) -> None:
    sources = _sources()
    sources["HRRR"].update({T: 280.0, D: 280.0})
    sources["GFS"].update({T: 300.0, D: 300.0})
    fields = blend_surface(
        temperature_k=286.0, contributors=sources, horizon=19, configuration=configuration
    )["fields"]
    assert fields[T]["value"] == 286.0
    assert fields[D]["value"] is None  # 60/40 dew=288 K exceeds unchanged 70/30 T=286 K.
    assert fields[D]["status"] == "inconsistent"
    assert fields["relative_humidity_2m"]["value"] is None
    assert "exceeds" in fields[D]["missing_reasons"][-1]


def test_missing_baseline_temperature_does_not_create_rh(
    configuration: Phase2BlendConfiguration,
) -> None:
    fields = blend_surface(
        temperature_k=None, contributors=_sources(), horizon=1, configuration=configuration
    )["fields"]
    assert fields[T]["value"] is None
    assert fields[D]["value"] is None
    assert fields["relative_humidity_2m"]["value"] is None
    assert fields[U]["value"] is not None


def test_no_guidance_and_no_cloud_policy_are_explicit(
    configuration: Phase2BlendConfiguration,
) -> None:
    fields = blend_surface(
        temperature_k=None, contributors={}, horizon=1, configuration=configuration
    )["fields"]
    for field in fields.values():
        assert field["value"] is None
        assert field["missing_reasons"]
    assert fields["cloud_area_fraction"]["policy"] == "no-approved-cloud-blend-policy"
