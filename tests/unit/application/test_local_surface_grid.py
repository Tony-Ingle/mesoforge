"""Local baseline geometry, retained surface science, and exact-center replay."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from unittest.mock import Mock, patch

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.local_surface_grid import build_local_surface_grid, extract_grid_point
from mesoforge.application.point_forecast import (
    PreparedPointForecast,
    _ShadowView,
    prepare_demo_files,
)
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.application.spatial_coverage import CoverageRequiredError
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.recipes import with_surface_fields
from tests.unit.application.test_prepared_temperature import phase2_configuration
from tests.unit.application.test_surface_forecast import (
    CRS,
    DEW,
    DIRECTION,
    GUST,
    RH,
    SPEED,
    TARGET,
    T,
    U,
    V,
    _dataset,
)

LATITUDE, LONGITUDE = 44.98859, -93.25557
HOURS = tuple(range(1, 37))
FIELDS = (T, DEW, RH, U, V, SPEED, DIRECTION, GUST)


def _gradient(latitude, longitude):
    return 1.5 * (latitude - LATITUDE) + 2.0 * (longitude - LONGITUDE)


def _spatial_dataset(model, *, age=0, hours=HOURS):
    dataset = _dataset(model, age=age, hours=hours).assign_coords(x=[-94.0, -92.0], y=[44.0, 46.0])
    longitude, latitude = np.meshgrid(dataset.x.values, dataset.y.values)
    for variable in (T, DEW):
        dataset[variable].values += _gradient(latitude, longitude)
    return dataset


def surface_prepared(directory):
    """Reuse retained field fixtures for API/storage wiring without provider acquisition."""
    prepare_demo_files(directory)
    template = PreparedPointForecast.from_directory(directory)
    datasets = {
        "HRRR": _spatial_dataset("HRRR"),
        "GFS": _spatial_dataset("GFS", age=6),
    }
    inputs = []
    for model, dataset in datasets.items():
        cycle = dataset.forecast_reference_time.values[()]
        for horizon in HOURS:
            valid = TARGET + np.timedelta64(horizon, "h")
            inputs.append(
                {
                    "model": model,
                    "cycle": str(np.datetime_as_string(cycle, unit="s")) + "Z",
                    "source_lead_hours": int((valid - cycle) / np.timedelta64(1, "h")),
                    "valid_time": str(np.datetime_as_string(valid, unit="s")) + "Z",
                    "source_grib_url": f"https://provider.invalid/{model}.grib2",
                    "raw_sha256": "a" * 64,
                    "extra_messages": [
                        {"canonical_variable_id": variable, "raw_sha256": "b" * 64}
                        for variable in (DEW, U, V, GUST)
                    ],
                }
            )
    shadows = {
        model: [
            _ShadowView(
                _spatial_dataset(model, age=age, hours=hours),
                CRS,
                {"prepared_sha256": "d" * 64, "manifest_sha256": "e" * 64},
            )
        ]
        for model, age, hours in (("RAP", 3, HOURS), ("IFS", 6, tuple(range(3, 37, 3))))
    }
    return replace(
        template,
        _guidance=datasets,
        _projections={model: CRS for model in datasets},
        _target_reference_time=TARGET,
        _horizons=HOURS,
        _configuration=with_surface_fields(IFS_CONFIGURATION),
        _surface_configuration=phase2_configuration().blend_configuration,
        _shadow_views=shadows,
        _manifest={
            "inputs": inputs,
            "prepared_files": {model: {"sha256": "c" * 64} for model in datasets},
        },
    )


@pytest.fixture(scope="module")
def prepared_surface(tmp_path_factory):
    return surface_prepared(tmp_path_factory.mktemp("local-surface-source"))


@pytest.fixture(scope="module")
def calculated_grid(prepared_surface):
    originals = {
        model: dataset.copy(deep=True) for model, dataset in prepared_surface._guidance.items()
    }
    originals.update(
        {
            model: views[0].dataset.copy(deep=True)
            for model, views in prepared_surface._shadow_views.items()
        }
    )
    calculate = Mock(wraps=prepared_surface._forecast_column)
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Grid must reuse loaded arrays")),
        patch("requests.Session", side_effect=AssertionError("Grid must not acquire guidance")),
    ):
        grid = build_local_surface_grid(
            latitude=LATITUDE, longitude=LONGITUDE, calculate_column=calculate
        )
    return grid, calculate, originals


def test_coordinate_only_geometry_and_shared_source_reuse(prepared_surface, calculated_grid):
    grid, calculate, originals = calculated_grid
    geometry = grid["geometry"]
    assert geometry["center"] == {"latitude": LATITUDE, "longitude": LONGITUDE}
    assert geometry["dimensions"] == {"x": 3, "y": 3}
    assert geometry["spacing_m"] == {"x": 3000.0, "y": 3000.0}
    assert geometry["x_m"] == geometry["y_m"] == [-3000.0, 0.0, 3000.0]
    assert geometry["wind_reference"] == "earth_relative"
    assert calculate.call_count == 9
    assert calculate.call_args_list[0].kwargs == {"latitude": LATITUDE, "longitude": LONGITUDE}
    assert len({tuple(call.kwargs.values()) for call in calculate.call_args_list}) == 9
    geod = pyproj.Geod(ellps="WGS84")
    for cell in grid["cells"]:
        _, _, distance = geod.inv(LONGITUDE, LATITUDE, cell["longitude"], cell["latitude"])
        expected = 3000.0 * math.hypot(cell["x_index"] - 1, cell["y_index"] - 1)
        assert distance == pytest.approx(expected, abs=1e-6)
        assert cell["status"] == "calculated" and not cell["missing_reasons"]
        assert len(cell["hours"]) == 36
    for model, dataset in prepared_surface._guidance.items():
        xr.testing.assert_identical(dataset, originals[model])
    for model, views in prepared_surface._shadow_views.items():
        xr.testing.assert_identical(views[0].dataset, originals[model])
    assert grid["transformation"]["source_sha256"]["application/local_surface_grid.py"]


def test_every_grid_hour_preserves_surface_science_and_field_specific_weights(calculated_grid):
    grid, _, _ = calculated_grid
    for cell in grid["cells"]:
        gradient = _gradient(cell["latitude"], cell["longitude"])
        for hour in cell["hours"]:
            horizon = hour["horizon_hours"]
            fields = hour["surface"]["fields"]
            late = horizon >= 19
            expected = {
                T: 293.15 + gradient,
                DEW: (284.15 if late else 283.15) + gradient,
                U: -4.0 if late else -3.0,
                V: -6.0 if late else -7.0,
                GUST: 12.8 if late else 12.6,
            }
            weights = {"HRRR": 0.6, "GFS": 0.4} if late else {"HRRR": 0.7, "GFS": 0.3}
            for variable, value in expected.items():
                assert fields[variable]["value"] == pytest.approx(value, abs=1e-10)
            for variable in (DEW, U, V, GUST):
                assert fields[variable]["weights"] == weights
                assert fields[variable]["row_id"] and fields[variable]["row_sha256"]
            assert {source["model"]: source["weight"] for source in hour["sources"]} == {
                "HRRR": 0.7,
                "GFS": 0.3,
            }
            temperature_c, dew_c = expected[T] - 273.15, expected[DEW] - 273.15
            humidity = 100.0 * math.exp(
                17.67 * dew_c / (dew_c + 243.5) - 17.67 * temperature_c / (temperature_c + 243.5)
            )
            assert fields[RH]["value"] == pytest.approx(humidity)
            assert fields[SPEED]["value"] == pytest.approx(math.hypot(expected[U], expected[V]))
            direction = math.degrees(math.atan2(-expected[U], -expected[V])) % 360
            assert fields[DIRECTION]["value"] == pytest.approx(direction)
            assert fields[DIRECTION]["value"] != pytest.approx(90.0 * weights["GFS"])
            assert fields[T]["unit"] == fields[DEW]["unit"] == "K"
            assert fields[RH]["unit"] == "%"
            assert fields[U]["unit"] == fields[V]["unit"] == fields[GUST]["unit"] == "m/s"
            assert fields[DIRECTION]["unit"] == "degree"


def test_native_shadow_times_and_contributor_provenance_survive_on_every_node(calculated_grid):
    grid, _, _ = calculated_grid
    for cell in grid["cells"]:
        for hour in cell["hours"]:
            horizon = hour["horizon_hours"]
            contributors = hour["surface"]["contributors"]
            assert contributors["GFS"]["cycle"] == "2026-09-11T06:00:00Z"
            assert contributors["GFS"]["source_lead_hours"] == horizon + 6
            assert contributors["RAP"]["role"] == contributors["IFS"]["role"] == "shadow"
            assert contributors["RAP"]["fields"][U]["value"] == pytest.approx(30.0, abs=1e-12)
            for source in hour["shadow_sources"]:
                assert source["weight"] == 0.0
                assert source["prepared_sha256"] == "d" * 64
            ifs = contributors["IFS"]
            if horizon % 3:
                assert ifs["source_lead_hours"] is None
                for variable in FIELDS:
                    assert ifs["fields"][variable]["value"] is None
                    assert ifs["fields"][variable]["missing_reasons"]
            else:
                assert ifs["source_lead_hours"] == horizon + 6
                assert ifs["fields"][U]["value"] == pytest.approx(6.0, abs=1e-12)
            assert ifs["fields"][GUST]["value"] is None
            evidence = contributors["HRRR"]["fields"][DEW]["provenance"]
            assert evidence["raw_sha256"] == "b" * 64
            assert evidence["prepared_sha256"] == "c" * 64
            assert evidence["valid_time"] == hour["valid_time"]
            assert evidence["wind_reference"] == "earth_relative"


def test_forecast_uses_grid_center_with_exact_previous_point_equivalence(
    prepared_surface, calculated_grid
):
    previous = prepared_surface._forecast_column(latitude=LATITUDE, longitude=LONGITUDE)
    result = prepared_surface.forecast(latitude=LATITUDE, longitude=LONGITUDE)
    assert result["hours"] == previous["hours"]
    grid = result["local_grid_baseline"]
    assert canonical_json_bytes(grid) == canonical_json_bytes(calculated_grid[0])
    center = grid["cells"][4]
    assert center["latitude"] == LATITUDE and center["longitude"] == LONGITUDE
    assert result["hours"] == center["hours"]
    assert result["local_grid"]["point_extraction"]["method"] == "exact_center_node"
    assert result["local_grid"]["sha256"] == str(canonical_json_digest(grid))
    assert (
        grid["forecast_context"]["contributor_configuration"]
        == previous["contributor_configuration"]
    )


def test_retained_grid_replays_without_source_calculation_and_is_not_mutated(calculated_grid):
    grid, calculate, _ = calculated_grid
    encoded = canonical_json_bytes(grid)
    replay = json.loads(encoded)
    with patch.object(
        PreparedPointForecast, "_forecast_column", side_effect=AssertionError("Replay is grid-only")
    ):
        first = extract_grid_point(replay, latitude=LATITUDE, longitude=LONGITUDE)
        second = extract_grid_point(replay, latitude=LATITUDE, longitude=LONGITUDE)
    assert first == second
    assert canonical_json_bytes(replay) == encoded
    assert calculate.call_count == 9
    first["hours"][0]["surface"]["fields"][T]["value"] = -1000.0
    first["local_grid_baseline"]["cells"][0]["hours"][0]["temperature"]["value"] = -1000.0
    assert canonical_json_bytes(replay) == encoded
    with pytest.raises(CoverageRequiredError, match="center"):
        extract_grid_point(replay, latitude=LATITUDE + 0.001, longitude=LONGITUDE)


def test_missing_native_corners_preserve_explicit_surface_fallback_and_temperature_missingness(
    prepared_surface,
):
    datasets = {
        model: dataset.copy(deep=True) for model, dataset in prepared_surface._guidance.items()
    }
    datasets["HRRR"][DEW].values[0, 0, 0] = np.nan
    datasets["HRRR"][GUST].values[0, 0, 0] = np.nan
    datasets["HRRR"][T].values[1, 0, 0] = np.nan
    prepared = replace(prepared_surface, _guidance=datasets, _horizons=(1, 2))
    grid = prepared.forecast(latitude=LATITUDE, longitude=LONGITUDE)["local_grid_baseline"]
    for cell in grid["cells"]:
        first, second = cell["hours"]
        fields = first["surface"]["fields"]
        for variable in (DEW, U, V, GUST):
            assert fields[variable]["weights"] == {"GFS": 1.0}
            assert fields[variable]["missing_reasons"]
        assert fields[DEW]["value"] == pytest.approx(
            290.15 + _gradient(cell["latitude"], cell["longitude"])
        )
        assert first["surface"]["contributors"]["HRRR"]["fields"][DEW]["value"] is None
        assert second["temperature"]["value"] is None and second["missing_reasons"]
        assert second["surface"]["fields"][RH]["value"] is None


def test_missing_peripheral_coverage_does_not_borrow_center_values_or_fail_center(prepared_surface):
    datasets = {
        model: dataset.assign_coords(
            x=[LONGITUDE - 0.005, LONGITUDE + 0.005],
            y=[LATITUDE - 0.005, LATITUDE + 0.005],
        )
        for model, dataset in prepared_surface._guidance.items()
    }
    prepared = replace(prepared_surface, _guidance=datasets, _horizons=(1,))
    result = prepared.forecast(latitude=LATITUDE, longitude=LONGITUDE)
    assert result["hours"][0]["temperature"]["value"] is not None
    cells = result["local_grid_baseline"]["cells"]
    assert sum(cell["status"] == "unavailable" for cell in cells) == 8
    for index, cell in enumerate(cells):
        if index == 4:
            continue
        assert cell["missing_reasons"]
        hour = cell["hours"][0]
        assert hour["temperature"]["value"] is None
        for field in hour["surface"]["fields"].values():
            assert field["value"] is None and field["missing_reasons"]
            assert field["weights"] == {}
        for contributor in hour["surface"]["contributors"].values():
            assert all(field["value"] is None for field in contributor["fields"].values())
    with pytest.raises(CoverageRequiredError):
        prepared.forecast(latitude=LATITUDE + 0.1, longitude=LONGITUDE)
