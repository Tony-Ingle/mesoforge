"""Local baseline geometry, retained surface science, and exact-center replay."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock, patch
from uuid import uuid4

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.local_surface_grid import (
    SurfaceGridGeometry,
    build_local_surface_grid,
    derive_grid_geometry,
    extract_grid_point,
)
from mesoforge.application.point_forecast import (
    PreparedPointForecast,
    _ShadowView,
    prepare_demo_files,
)
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.application.snowfall_amount_forecast import AMOUNT
from mesoforge.application.snowfall_forecast import SNOW, extract_snowfall_contributors
from mesoforge.application.spatial_coverage import CoverageRequiredError
from mesoforge.application.spatial_preparation import (
    PreparedRegions,
    attach_cloud_guidance,
    attach_ice_guidance,
    attach_snowfall_amount_guidance,
    attach_snowfall_guidance,
    attach_thunder_guidance,
    attach_visibility_guidance,
)
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from mesoforge.forecasting.cloud_cover import CLOUD, CLOUD_ACTIVE_POLICY
from mesoforge.forecasting.ice import FLAT_ICE, FRZR
from mesoforge.forecasting.recipes import with_surface_fields
from tests.unit.application.test_cloud_cover import cloud_view
from tests.unit.application.test_forecast_issuance import memory_service as memory_service
from tests.unit.application.test_ice import ice_view
from tests.unit.application.test_precipitation_type import type_view
from tests.unit.application.test_prepared_temperature import phase2_configuration
from tests.unit.application.test_probability_contributors import (
    _event as probability_event,
)
from tests.unit.application.test_probability_contributors import (
    _view as probability_view,
)
from tests.unit.application.test_snowfall_amount_forecast import amount_view
from tests.unit.application.test_snowfall_forecast import snow_view
from tests.unit.application.test_surface_forecast import (
    CRS,
    DEW,
    DIRECTION,
    GUST,
    POP,
    QPF,
    RH,
    SPEED,
    TARGET,
    T,
    U,
    V,
    _add_qpf,
    _dataset,
    _pop_entry,
)
from tests.unit.application.test_thunder import thunder_view
from tests.unit.application.test_visibility import visibility_view

LATITUDE, LONGITUDE = 44.98859, -93.25557
HOURS = tuple(range(1, 37))
FIELDS = (T, DEW, RH, U, V, SPEED, DIRECTION, GUST)
TEST_GEOMETRY = SurfaceGridGeometry(
    context_half_width_cells=2, editable_half_width_cells=1, spacing_m=3000.0
)


def _gradient(latitude, longitude):
    return 1.5 * (latitude - LATITUDE) + 2.0 * (longitude - LONGITUDE)


def _type_views():
    views = []
    for model in ("HRRR", "GFS"):
        view = type_view(model)
        event = view.manifest["events"][0]
        events = []
        for hour in HOURS:
            valid = str(np.datetime_as_string(TARGET + np.timedelta64(hour, "h"), unit="s")) + "Z"
            events.append(
                {
                    **event,
                    "source_cycle": str(np.datetime_as_string(TARGET, unit="s")) + "Z",
                    "source_lead_hours": hour,
                    "valid_time": valid,
                }
            )
        view.manifest["events"] = events
        dataset = xr.concat(
            [view.dataset.isel(event=0, drop=True)] * 36, dim="event"
        ).assign_coords(event=list(range(36)), x=[-94.0, -92.0], y=[44.8, 45.2])
        dataset.rain.values[:, 1, :] = 0
        dataset.snow.values[:, 1, :] = 1
        views.append(replace(view, dataset=dataset))
    return views


def _spatial_dataset(model, *, age=0, hours=HOURS):
    dataset = _dataset(model, age=age, hours=hours).assign_coords(x=[-94.0, -92.0], y=[44.0, 46.0])
    longitude, latitude = np.meshgrid(dataset.x.values, dataset.y.values)
    for variable in (T, DEW):
        dataset[variable].values += _gradient(latitude, longitude)
    return dataset


def _snow_views(model="HRRR"):
    native = []
    for horizon in HOURS:
        valid = str(np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")) + "Z"
        view = snow_view(model, amount=horizon / 10, end=valid)
        view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
        longitude, latitude = np.meshgrid(view.dataset.x.values, view.dataset.y.values)
        # An independent spatially varying accumulation; both representations agree.
        for variable in ("amount", "native_end_amount"):
            view.dataset[variable].values += 0.01 * _gradient(latitude, longitude)
        native.append(view)
    return [
        replace(
            native[0],
            dataset=xr.concat(
                [view.dataset.isel(event=0, drop=True) for view in native], dim="event"
            ).assign_coords(event=list(range(36))),
            manifest={
                **native[0].manifest,
                "events": [view.manifest["events"][0] for view in native],
            },
        )
    ]


def _spatial_probability_views(*, end_horizon=6):
    """Reuse native-event fixtures with one shared spatial gradient per source."""
    views = []
    for source, fraction, duration in (
        ("NBM_NATIVE6", 0.4, 6),
        ("GEFS_NATIVE6", 0.7, 6),
        ("ENS_NATIVE24", 0.6, 24),
    ):
        end = TARGET + np.timedelta64(end_horizon, "h")
        event = probability_event(hours=duration)
        event.update(
            interval_start=str(np.datetime_as_string(end - np.timedelta64(duration, "h"), unit="s"))
            + "Z",
            interval_end=str(np.datetime_as_string(end, unit="s")) + "Z",
            source_lead_hours=int(
                (end - np.datetime64("2026-09-11T00:00:00")) / np.timedelta64(1, "h")
            ),
        )
        view = probability_view(source, fraction=fraction, events=[event])
        view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
        longitude, latitude = np.meshgrid(view.dataset.x.values, view.dataset.y.values)
        view.dataset.probability.values += 0.02 * _gradient(latitude, longitude)
        views.append(view)
    return views


def _amount_views():
    views = []
    for model in ("HRRR", "RAP", "NBM"):
        native = []
        for horizon in HOURS:
            valid = (
                str(np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")) + "Z"
            )
            view = amount_view(model, amount=horizon / 100, valid=valid)
            view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
            native.append(view)
        views.append(
            replace(
                native[0],
                dataset=xr.concat(
                    [view.dataset.isel(event=0, drop=True) for view in native], dim="event"
                ).assign_coords(event=list(range(36))),
                manifest={
                    **native[0].manifest,
                    "events": [view.manifest["events"][0] for view in native],
                },
            )
        )
    return views


def _cloud_views():
    views = []
    for model, offset in (("HRRR", 20), ("GFS", 40), ("RAP", 50), ("IFS", 60), ("NBM", 70)):
        native = []
        for horizon in HOURS:
            if model == "IFS" and horizon % 3:
                continue
            valid = (
                str(np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")) + "Z"
            )
            factor = 100 if model == "IFS" else 1
            view = cloud_view(
                model,
                amount=offset,
                end=valid,
                native_unit="1" if model == "IFS" else "percent",
                factor=factor,
            )
            view.manifest["events"][0].update(
                source_cycle=str(np.datetime_as_string(TARGET, unit="s")) + "Z",
                source_lead_hours=horizon,
            )
            view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
            longitude, latitude = np.meshgrid(view.dataset.x.values, view.dataset.y.values)
            view.dataset.cloud_cover.values += _gradient(latitude, longitude)
            view.dataset.native_cloud_cover.values += _gradient(latitude, longitude) / factor
            native.append(view)
        views.append(
            replace(
                native[0],
                dataset=xr.concat(
                    [view.dataset.isel(event=0, drop=True) for view in native], dim="event"
                ).assign_coords(event=list(range(len(native)))),
                manifest={
                    **native[0].manifest,
                    "events": [view.manifest["events"][0] for view in native],
                },
            )
        )
    return views


def _visibility_views():
    views = []
    for model, offset in (("HRRR", 3000), ("GFS", 5000), ("RAP", 7000), ("NBM", 8000)):
        native = []
        for horizon in HOURS:
            valid = (
                str(np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")) + "Z"
            )
            view = visibility_view(model, amount=offset + horizon, end=valid)
            view.manifest["events"][0].update(
                source_cycle=str(np.datetime_as_string(TARGET, unit="s")) + "Z",
                source_lead_hours=horizon,
            )
            view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
            longitude, latitude = np.meshgrid(view.dataset.x.values, view.dataset.y.values)
            view.dataset.visibility.values += 100 * _gradient(latitude, longitude)
            view.dataset.native_visibility.values += 100 * _gradient(latitude, longitude)
            native.append(view)
        views.append(
            replace(
                native[0],
                dataset=xr.concat(
                    [view.dataset.isel(event=0, drop=True) for view in native], dim="event"
                ).assign_coords(event=list(range(36))),
                manifest={
                    **native[0].manifest,
                    "events": [view.manifest["events"][0] for view in native],
                },
            )
        )
    return views


def _thunder_views():
    views = []
    for source_id, duration, offset in (
        ("NBM_1H", 1, 0.25),
        ("NBM_3H", 3, 0.5),
        ("NBM_6H", 6, 0.7),
    ):
        native = []
        for horizon in HOURS:
            if horizon % duration:
                continue
            valid = (
                str(np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")) + "Z"
            )
            view = thunder_view(source_id, amount=offset + horizon / 1000, end=valid)
            view.manifest["events"][0].update(
                source_cycle=str(np.datetime_as_string(TARGET, unit="s")) + "Z",
                source_lead_hours=horizon,
            )
            view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
            longitude, latitude = np.meshgrid(view.dataset.x.values, view.dataset.y.values)
            view.dataset.thunder_probability.values += 0.01 * _gradient(latitude, longitude)
            view.dataset.native_probability.values += _gradient(latitude, longitude)
            native.append(view)
        views.append(
            replace(
                native[0],
                dataset=xr.concat(
                    [view.dataset.isel(event=0, drop=True) for view in native], dim="event"
                ).assign_coords(event=list(range(len(native)))),
                manifest={
                    **native[0].manifest,
                    "events": [view.manifest["events"][0] for view in native],
                },
            )
        )
    return views


def _ice_views():
    views = []
    for source_id, duration, offset in (
        ("NBM_FICEAC_1H", 1, 0.5),
        ("NBM_FICEAC_6H", 6, 1.5),
        ("HRRR_FRZR", 1, 1.0),
        ("RAP_FRZR", 1, 1.25),
    ):
        native = []
        for horizon in HOURS:
            if horizon % duration:
                continue
            valid = (
                str(np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")) + "Z"
            )
            view = ice_view(source_id, amount=offset + horizon / 100, end=valid)
            view.dataset.coords.update({"x": [-94.0, -92.0], "y": [44.0, 46.0]})
            longitude, latitude = np.meshgrid(view.dataset.x.values, view.dataset.y.values)
            view.dataset.native_end_amount.values += 0.01 * _gradient(latitude, longitude)
            parent = (
                view.dataset.native_start_amount.values
                if "native_start_amount" in view.dataset
                else 0.0
            )
            view.dataset.amount.values[:] = view.dataset.native_end_amount.values - parent
            native.append(view)
        views.append(
            replace(
                native[0],
                dataset=xr.concat(
                    [view.dataset.isel(event=0, drop=True) for view in native], dim="event"
                ).assign_coords(event=list(range(len(native)))),
                manifest={
                    **native[0].manifest,
                    "events": [view.manifest["events"][0] for view in native],
                },
            )
        )
    return views


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
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=calculate,
            geometry=TEST_GEOMETRY,
        )
    return grid, calculate, originals


@pytest.fixture(scope="module")
def legacy_grid(calculated_grid):
    """Retain the original v1 shape without relying on Git or a rewritten artifact."""
    current = calculated_grid[0]
    source_geometry = current["geometry"]
    geometry = {
        key: deepcopy(source_geometry[key]) for key in ("center", "crs_wkt2", "wind_reference")
    }
    geometry.update(
        dimensions={"x": 3, "y": 3},
        spacing_m={"x": 3000.0, "y": 3000.0},
        x_m=[-3000.0, 0.0, 3000.0],
        y_m=[-3000.0, 0.0, 3000.0],
        latitude=[row[1:4] for row in source_geometry["latitude"][1:4]],
        longitude=[row[1:4] for row in source_geometry["longitude"][1:4]],
    )
    geometry["extent"] = {
        "south": min(value for row in geometry["latitude"] for value in row),
        "north": max(value for row in geometry["latitude"] for value in row),
        "west": min(value for row in geometry["longitude"] for value in row),
        "east": max(value for row in geometry["longitude"] for value in row),
    }
    cells = []
    for cell in current["cells"]:
        if 1 <= cell["x_index"] <= 3 and 1 <= cell["y_index"] <= 3:
            old_cell = {
                key: deepcopy(cell[key])
                for key in ("latitude", "longitude", "status", "missing_reasons", "hours")
            }
            old_cell.update(x_index=cell["x_index"] - 1, y_index=cell["y_index"] - 1)
            cells.append(old_cell)
    return {
        "version": "mesoforge.local-surface-baseline.v1",
        "policy": "experimental-centered-3x3-3km.v1",
        "transformation": deepcopy(current["transformation"]),
        "geometry": geometry,
        "forecast_context": deepcopy(current["forecast_context"]),
        "cells": cells,
    }


def test_coordinate_only_geometry_and_shared_source_reuse(prepared_surface, calculated_grid):
    grid, calculate, originals = calculated_grid
    geometry = grid["geometry"]
    assert geometry["center"] == {"latitude": LATITUDE, "longitude": LONGITUDE}
    assert geometry["dimensions"] == {"x": 5, "y": 5}
    assert geometry["spacing_m"] == {"x": 3000.0, "y": 3000.0}
    assert geometry["x_m"] == geometry["y_m"] == [-6000.0, -3000.0, 0.0, 3000.0, 6000.0]
    assert geometry["wind_reference"] == "earth_relative"
    assert geometry["node_location"] == "sample_at_node_center"
    assert calculate.call_count == 25
    assert calculate.call_args_list[0].kwargs == {"latitude": LATITUDE, "longitude": LONGITUDE}
    assert len({tuple(call.kwargs.values()) for call in calculate.call_args_list}) == 25
    geod = pyproj.Geod(ellps="WGS84")
    for cell in grid["cells"]:
        _, _, distance = geod.inv(LONGITUDE, LATITUDE, cell["longitude"], cell["latitude"])
        expected = 3000.0 * math.hypot(cell["x_index"] - 2, cell["y_index"] - 2)
        assert distance == pytest.approx(expected, abs=1e-6)
        assert cell["status"] == "calculated" and not cell["missing_reasons"]
        assert len(cell["hours"]) == 36
    for model, dataset in prepared_surface._guidance.items():
        xr.testing.assert_identical(dataset, originals[model])
    for model, views in prepared_surface._shadow_views.items():
        xr.testing.assert_identical(views[0].dataset, originals[model])
    assert grid["transformation"]["source_sha256"]["application/local_surface_grid.py"]


@pytest.mark.parametrize("context_half,editable_half,spacing", [(2, 1, 3000.0), (3, 2, 6000.0)])
def test_nested_domains_and_boundary_distance_support_future_taper_without_edits(
    context_half, editable_half, spacing
):
    specification = SurfaceGridGeometry(context_half, editable_half, spacing)
    calculate = Mock(
        side_effect=lambda **point: {
            **point,
            "hours": [{"horizon_hours": 1, "temperature": {"value": 280.0, "unit": "K"}}],
        }
    )
    grid = build_local_surface_grid(
        latitude=LATITUDE, longitude=LONGITUDE, calculate_column=calculate, geometry=specification
    )
    geometry = grid["geometry"]
    assert geometry == derive_grid_geometry(
        latitude=LATITUDE, longitude=LONGITUDE, geometry=specification
    )
    size, editable_size = 2 * context_half + 1, 2 * editable_half + 1
    domains = geometry["domains"]
    assert domains["context"]["dimensions"] == {"x": size, "y": size}
    assert domains["editable"]["dimensions"] == {"x": editable_size, "y": editable_size}
    assert domains["context"]["node_count"] == size**2
    assert domains["editable"]["node_count"] == editable_size**2
    for domain_name, half_width in (("context", context_half), ("editable", editable_half)):
        edge = half_width * spacing
        assert domains[domain_name]["bounds_m"] == {
            "x_min": -edge,
            "x_max": edge,
            "y_min": -edge,
            "y_max": edge,
        }
        assert domains[domain_name]["boundary_included"] is True
    assert geometry["editable_boundary"]["distance_metric"] == (
        "signed_euclidean_distance_in_projected_meters"
    )
    assert geometry["editable_boundary"]["taper_policy"] == "not_implemented"
    assert domains["context"]["extent"] == geometry["extent"]
    for edge in ("south", "west"):
        assert domains["context"]["extent"][edge] < domains["editable"]["extent"][edge]
    for edge in ("north", "east"):
        assert domains["context"]["extent"][edge] > domains["editable"]["extent"][edge]
    assert sum(cell["inside_editable_domain"] for cell in grid["cells"]) == editable_size**2
    assert sum(cell["context_only"] for cell in grid["cells"]) == size**2 - editable_size**2
    assert sum(cell["is_forecast_point"] for cell in grid["cells"]) == 1
    assert calculate.call_count == size**2
    signed_distances = {}
    for cell in grid["cells"]:
        x, y = cell["x_index"] - context_half, cell["y_index"] - context_half
        inside = abs(x) <= editable_half and abs(y) <= editable_half
        assert cell["inside_editable_domain"] is inside
        assert cell["context_only"] is not inside
        assert cell["is_forecast_point"] is (x == y == 0)
        if inside:
            expected_distance = spacing * (editable_half - max(abs(x), abs(y)))
        else:
            expected_distance = -spacing * math.hypot(
                max(abs(x) - editable_half, 0), max(abs(y) - editable_half, 0)
            )
        distance = cell["signed_distance_to_editable_boundary_m"]
        assert distance == pytest.approx(expected_distance)
        signed_distances[x, y] = distance
        assert cell["hours"][0]["temperature"]["value"] == 280.0
        assert "taper_weight" not in cell
    for (x, y), distance in signed_distances.items():
        assert signed_distances[-x, -y] == distance
    assert signed_distances[0, 0] == spacing * editable_half
    assert signed_distances[editable_half, 0] == 0.0
    assert signed_distances[context_half, 0] < 0.0
    point = extract_grid_point(grid, latitude=LATITUDE, longitude=LONGITUDE)
    assert point["local_grid"]["point_extraction"]["x_index"] == context_half
    assert point["local_grid"]["point_extraction"]["y_index"] == context_half


@pytest.mark.parametrize(
    "context_half,editable_half,spacing",
    [
        (1, 1, 3000.0),
        (1, 2, 3000.0),
        (0, 0, 3000.0),
        (3, 0, 3000.0),
        (3, -1, 3000.0),
        (2.5, 1, 3000.0),
        (3, True, 3000.0),
        (3, 1, 0.0),
        (3, 1, -3000.0),
        (3, 1, math.nan),
        (3, 1, math.inf),
    ],
)
def test_invalid_geometry_is_rejected(context_half, editable_half, spacing):
    with pytest.raises((ValueError, TypeError)):
        SurfaceGridGeometry(context_half, editable_half, spacing)


def test_geometry_configuration_is_immutable():
    with pytest.raises(FrozenInstanceError):
        TEST_GEOMETRY.spacing_m = 1000.0


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
    assert (
        result["hours"]
        == extract_grid_point(calculated_grid[0], latitude=LATITUDE, longitude=LONGITUDE)["hours"]
    )
    center = next(cell for cell in grid["cells"] if cell["is_forecast_point"])
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
    assert calculate.call_count == 25
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
    manifest = deepcopy(prepared_surface._manifest)
    manifest["qpf_inputs"] = []
    for model, dataset in datasets.items():
        _add_qpf(dataset, [0.125] * 36)
        lead = int(dataset.source_lead_time.values[0] / np.timedelta64(1, "h"))
        parent = {"model": model, "source_lead_hours": lead}
        dataset.attrs["qpf_metadata_json"] = json.dumps({str(lead): {"parents": [parent]}})
        dataset["qpf_finite_precision_floor_applied"] = xr.zeros_like(dataset[QPF], dtype=np.uint8)
        manifest["qpf_inputs"].append({**parent, "messages": [{"raw_sha256": "f" * 64}]})
    prepared = replace(
        prepared_surface,
        _guidance=datasets,
        _manifest=manifest,
        _horizons=(1,),
        _pop_views=[_pop_entry(hours=(1,))],
        _pop_guidance={"status": "prepared"},
        _probability_views=_spatial_probability_views(end_horizon=1),
        _type_views=_type_views(),
        _type_guidance={"status": "prepared"},
        _snow_views=[*_snow_views(), *_snow_views("RAP")],
        _snow_guidance={"status": "prepared"},
        _snow_amount_views=_amount_views(),
        _snow_amount_guidance={"status": "prepared"},
        _cloud_views=_cloud_views(),
        _cloud_guidance={"status": "prepared"},
        _visibility_views=_visibility_views(),
        _visibility_guidance={"status": "prepared"},
        _thunder_views=_thunder_views(),
        _thunder_guidance={"status": "prepared"},
        _ice_views=_ice_views(),
        _ice_guidance={"status": "prepared"},
    )
    result = prepared.forecast(latitude=LATITUDE, longitude=LONGITUDE)
    assert result["hours"][0]["temperature"]["value"] is not None
    center_qpf = result["hours"][0]["surface"]["contributors"]["HRRR"]["fields"][QPF]
    assert center_qpf["value"] == pytest.approx(0.125)
    assert center_qpf["spatial_extraction"]
    assert "finite_precision_floor_at_source_corners" in center_qpf["normalization"]
    center_pop = result["hours"][0]["surface"]["fields"][POP]
    assert center_pop["value"] == pytest.approx(0.4)
    assert center_pop["spatial_extraction"]["native_fraction_at_source_corners"] == [0.4] * 4
    center_probabilities = result["hours"][0]["surface"]["probability_guidance"]
    center_amounts = result["hours"][0]["surface"]["snowfall_amount_guidance"]
    center_cloud = result["hours"][0]["surface"]["cloud_guidance"]
    center_visibility = result["hours"][0]["surface"]["visibility_guidance"]
    center_thunder = result["hours"][0]["surface"]["thunder_guidance"]
    center_ice = result["hours"][0]["surface"]["ice_guidance"]
    assert center_ice["contributors"][0]["value"] == pytest.approx(
        0.51 + 0.01 * _gradient(44.0, -94.0)
    )
    assert center_thunder["field"]["value"] == pytest.approx(0.251 + 0.01 * _gradient(44.0, -94.0))
    assert center_visibility["contributors"][0]["value"] == pytest.approx(
        3001 + 100 * _gradient(44.0, -94.0)
    )
    assert center_cloud["contributors"][0]["value"] == pytest.approx(20)
    assert center_cloud["field"]["value"] == pytest.approx(0.7)
    assert center_cloud["field"]["cloud_percentage"] == pytest.approx(70)
    assert center_amounts["native_contributors"][0]["value"] == pytest.approx(0.01)
    assert center_amounts["derived_contributors"][0]["value"] is not None
    assert center_amounts["derived_contributors"][0]["diagnostic_ratio"] is not None
    assert [source["value"] for source in center_probabilities["contributors"]] == pytest.approx(
        [0.4, 0.7, 0.6]
    )
    paired = [
        pair for pair in center_probabilities["comparisons"] if pair["status"] == "comparable"
    ]
    assert len(paired) == 1 and paired[0]["delta"] == pytest.approx(-0.3)
    cells = result["local_grid_baseline"]["cells"]
    assert sum(cell["status"] == "unavailable" for cell in cells) == len(cells) - 1
    for cell in cells:
        if cell["is_forecast_point"]:
            continue
        assert cell["missing_reasons"]
        hour = cell["hours"][0]
        assert hour["temperature"]["value"] is None
        for name, field in hour["surface"]["fields"].items():
            if name == "precipitation_type":
                assert field["value"] == "unavailable" and field["missing_reasons"]
                assert field["supported_types"] == []
                evidence = hour["surface"]["precipitation_type_guidance"]
                assert all(not row["native_values"] for row in evidence["contributors"])
                continue
            assert field["value"] is None and field["missing_reasons"]
            assert field["weights"] == {}
            assert "spatial_extraction" not in field
        for contributor in hour["surface"]["contributors"].values():
            assert all(field["value"] is None for field in contributor["fields"].values())
            for field in contributor["fields"].values():
                assert "spatial_extraction" not in field
                assert "finite_precision_floor_at_source_corners" not in field.get(
                    "normalization", {}
                )
        unavailable = hour["surface"]["contributors"]["HRRR"]["fields"][QPF]
        assert unavailable["interval_start"] == center_qpf["interval_start"]
        assert unavailable["provenance"] == center_qpf["provenance"]
        for field in (
            hour["surface"]["fields"][POP],
            hour["surface"]["contributors"]["NBM"]["fields"][POP],
        ):
            assert field["source_cycle"] == center_pop["source_cycle"]
            assert field["interval_start"] == center_pop["interval_start"]
            assert field["interval_end"] == center_pop["interval_end"]
            assert field["provenance"] == center_pop["provenance"]
        unavailable_probabilities = hour["surface"]["probability_guidance"]
        for source, center_source in zip(
            unavailable_probabilities["contributors"],
            center_probabilities["contributors"],
            strict=True,
        ):
            assert source["value"] is None and source["status"] == "unavailable"
            assert source["missing_reasons"] == cell["missing_reasons"]
            assert "spatial_extraction" not in source
            for identity in (
                "event_id",
                "source_id",
                "source_cycle",
                "provenance",
                "threshold",
                "interval_start",
                "interval_end",
            ):
                assert source[identity] == center_source[identity]
        for pair in unavailable_probabilities["comparisons"]:
            assert pair["delta"] is None and pair["status"] == "unavailable"
            assert pair["left"]["value"] is None and pair["right"]["value"] is None
            assert pair["reasons"] == cell["missing_reasons"]
        snowfall = hour["surface"]["snowfall_guidance"]
        center_snow = result["hours"][0]["surface"]["snowfall_guidance"]
        assert snowfall["field"]["value"] is None
        for source, center_source in zip(
            snowfall["contributors"], center_snow["contributors"], strict=True
        ):
            assert source["value"] is None and source["status"] == "unavailable"
            assert "spatial_extraction" not in source
            assert "extraction_coordinate" not in source
            assert source.get("provenance") == center_source.get("provenance")
            assert source["interval_start"] == center_source["interval_start"]
        for pair in snowfall["comparisons"]:
            assert pair["difference_left_minus_right"] is None
            assert pair["status"] == "unavailable"
        amounts = hour["surface"]["snowfall_amount_guidance"]
        assert amounts["field"]["value"] is None
        for group in ("native_contributors", "native_slr", "derived_contributors"):
            for source, central in zip(amounts[group], center_amounts[group], strict=True):
                assert source["value"] is None and source["status"] == "unavailable"
                assert "spatial_extraction" not in source and "diagnostic_ratio" not in source
                assert "diagnostic_ratio_status" not in source
                assert "extraction_coordinate" not in source
                assert source.get("provenance") == central.get("provenance")
                assert source.get("interval_start") == central.get("interval_start")
        for pair in amounts["comparisons"]:
            assert pair["difference_left_minus_right"] is None
            assert pair["status"] == "unavailable"
        clouds = hour["surface"]["cloud_guidance"]
        assert clouds["field"] == hour["surface"]["fields"][CLOUD]
        assert clouds["field"]["native_value"] is None
        assert clouds["field"]["cloud_percentage"] is None
        assert clouds["field"]["sky_category"] is None
        assert clouds["field"]["active_weight"] == 0
        assert clouds["field"]["provenance"] == center_cloud["field"]["provenance"]
        for source, central in zip(
            clouds["contributors"], center_cloud["contributors"], strict=True
        ):
            assert source["value"] is None and source["native_value"] is None
            assert source["sky_category"] is None and source["status"] == "unavailable"
            assert source["active_weight"] == 0
            assert source["missing_reasons"] == cell["missing_reasons"]
            assert "spatial_extraction" not in source and "extraction_coordinate" not in source
            assert source.get("provenance") == central.get("provenance")
            assert source["valid_time"] == central["valid_time"]
        for pair in clouds["comparisons"]:
            assert pair["difference_left_minus_right"] is None
            assert pair["status"] == "unavailable"
        visibility = hour["surface"]["visibility_guidance"]
        for source, central in zip(
            visibility["contributors"], center_visibility["contributors"], strict=True
        ):
            assert source["value"] is None and source["native_value"] is None
            assert source["display_miles"] is None and source["status"] == "unavailable"
            assert source["missing_reasons"] == cell["missing_reasons"]
            assert "spatial_extraction" not in source and "extraction_coordinate" not in source
            assert source.get("provenance") == central.get("provenance")
            assert source["valid_time"] == central["valid_time"]
        for pair in visibility["comparisons"]:
            assert pair["difference_left_minus_right"] is None and pair["status"] == "unavailable"
        thunder = hour["surface"]["thunder_guidance"]
        for source, central in zip(
            [thunder["field"], *thunder["contributors"]],
            [center_thunder["field"], *center_thunder["contributors"]],
            strict=True,
        ):
            assert source["value"] is None and source["native_value"] is None
            assert source["display_percent"] is None and source["active_weight"] == 0
            assert source["missing_reasons"] == cell["missing_reasons"]
            assert "spatial_extraction" not in source and "extraction_coordinate" not in source
            for key in (
                "provenance",
                "interval_start",
                "interval_end",
                "event_definition",
                "spatial_support",
            ):
                assert source.get(key) == central.get(key)
        for pair in thunder["comparisons"]:
            assert pair["difference_left_minus_right"] is None and pair["status"] == "unavailable"
        ice = hour["surface"]["ice_guidance"]
        for field in ice["fields"].values():
            assert field["value"] is None and field["status"] == "unavailable"
            assert field["missing_reasons"] == cell["missing_reasons"] and field["weights"] == {}
        for source, central in zip(ice["contributors"], center_ice["contributors"], strict=True):
            assert source["value"] is None and source["native_value"] is None
            assert (
                source["active_weight"] == 0
                and source["missing_reasons"] == cell["missing_reasons"]
            )
            assert "spatial_extraction" not in source and "extraction_coordinate" not in source
            for key in ("provenance", "quantity_kind", "interval_start", "interval_end"):
                assert source.get(key) == central.get(key)
        for pair in ice["comparisons"]:
            assert pair["difference_left_minus_right"] is None and pair["status"] == "unavailable"
    with pytest.raises(CoverageRequiredError):
        prepared.forecast(latitude=LATITUDE + 0.1, longitude=LONGITUDE)


def test_type_spans_both_domains_without_changing_surface_values(prepared_surface, calculated_grid):
    views = _type_views()
    prepared = replace(prepared_surface, _type_views=views, _type_guidance={"status": "prepared"})
    grid = build_local_surface_grid(
        latitude=LATITUDE,
        longitude=LONGITUDE,
        calculate_column=prepared._forecast_column,
        geometry=TEST_GEOMETRY,
    )
    roles = set()
    for cell, old in zip(grid["cells"], calculated_grid[0]["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        for hour, prior in zip(cell["hours"], old["hours"], strict=True):
            ptype = hour["surface"]["fields"]["precipitation_type"]
            assert ptype["value"] == ("snow" if cell["latitude"] > 45 else "rain")
            assert ptype["unit"] == "category"
            unchanged = deepcopy(hour)
            unchanged["surface"]["fields"].pop("precipitation_type")
            unchanged["surface"].pop("precipitation_type_guidance")
            assert unchanged == prior
    assert roles == {True, False}
    encoded = canonical_json_bytes(grid)
    restored = json.loads(encoded)
    point = extract_grid_point(restored, latitude=LATITUDE, longitude=LONGITUDE)
    assert point["hours"] == next(c["hours"] for c in grid["cells"] if c["is_forecast_point"])
    point["hours"][0]["surface"]["fields"]["precipitation_type"]["value"] = "changed"
    assert canonical_json_bytes(restored) == encoded


def test_snowfall_spans_both_domains_and_point_without_changing_existing_fields(
    prepared_surface, calculated_grid, monkeypatch, memory_service
):
    views = _snow_views()
    before = views[0].dataset.copy(deep=True)
    descriptor = {
        "status": "prepared",
        "source_status": {"GFS": {"missing_reasons": ["No native SWE; rate is a different field"]}},
    }
    loader = Mock(return_value=views)
    monkeypatch.setattr("mesoforge.application.prepared_snowfall.load_snowfall_guidance", loader)
    regions = attach_snowfall_guidance(
        PreparedRegions([prepared_surface, prepared_surface], {}), descriptor
    )
    loader.assert_called_once_with(descriptor, target_reference_time=TARGET)
    assert regions.regions[0]._snow_views is regions.regions[1]._snow_views
    assert prepared_surface._snow_guidance is None
    prepared = regions.regions[0]
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Reuse loaded guidance")),
        patch("requests.Session", side_effect=AssertionError("No per-cell acquisition")),
    ):
        grid = build_local_surface_grid(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=prepared._forecast_column,
            geometry=TEST_GEOMETRY,
        )
    roles = set()
    for cell, prior_cell in zip(grid["cells"], calculated_grid[0]["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        for hour, prior in zip(cell["hours"], prior_cell["hours"], strict=True):
            snow = hour["surface"]["snowfall_guidance"]
            assert snow["field"]["value"] is None and snow["field"]["weights"] == {}
            assert snow["field"]["status"] == "policy_unavailable"
            native = snow["contributors"][0]
            assert native["value"] == pytest.approx(
                hour["horizon_hours"] / 10 + 0.01 * _gradient(cell["latitude"], cell["longitude"])
            )
            assert native["interval_end"] == hour["valid_time"]
            assert native["active_weight"] == 0 and native["provenance"]
            assert "different field" in snow["contributors"][1]["missing_reasons"][0]
            unchanged = deepcopy(hour)
            unchanged["surface"]["fields"].pop(SNOW)
            unchanged["surface"].pop("snowfall_guidance")
            assert unchanged == prior
    assert roles == {True, False}
    xr.testing.assert_identical(views[0].dataset, before)
    encoded = canonical_json_bytes(grid)
    restored = json.loads(encoded)
    point = extract_grid_point(restored, latitude=LATITUDE, longitude=LONGITUDE)
    assert point["hours"] == next(
        cell["hours"] for cell in grid["cells"] if cell["is_forecast_point"]
    )
    assert point["snowfall_guidance"] == descriptor
    service, factory, objects = memory_service
    issued = service.issue(point, batch_run_id=uuid4(), location_index=0)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    stored = len(factory.issued_forecasts), len(objects.objects)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    assert (len(factory.issued_forecasts), len(objects.objects)) == stored
    point["hours"][0]["surface"]["snowfall_guidance"]["contributors"][0]["value"] = -999
    assert canonical_json_bytes(restored) == encoded
    assert service.read(issued.issued_forecast_id)["forecast"] != point


def test_snowfall_amounts_reuse_swe_across_both_domains_and_preserve_immutable_readback(
    prepared_surface, calculated_grid, monkeypatch, memory_service
):
    swe = _snow_views("RAP")
    amounts = _amount_views()
    originals = [view.dataset.copy(deep=True) for view in [*swe, *amounts]]
    snow_descriptor = {"status": "prepared"}
    descriptor = {"status": "prepared", "source_status": {}}
    baseline = replace(prepared_surface, _snow_views=swe, _snow_guidance=snow_descriptor)
    loader = Mock(return_value=amounts)
    monkeypatch.setattr(
        "mesoforge.application.prepared_snowfall_amount.load_snowfall_amount_guidance", loader
    )
    regions = attach_snowfall_amount_guidance(PreparedRegions([baseline, baseline], {}), descriptor)
    loader.assert_called_once_with(descriptor, target_reference_time=TARGET)
    assert regions.regions[0]._snow_amount_views is regions.regions[1]._snow_amount_views
    assert all(region._snow_views is swe for region in regions.regions)
    assert baseline._snow_amount_guidance is None
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Already loaded all guidance")),
        patch("requests.Session", side_effect=AssertionError("No acquisition per grid cell")),
    ):
        grid = build_local_surface_grid(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=regions.regions[0]._forecast_column,
            geometry=TEST_GEOMETRY,
        )
    roles = set()
    for cell, old_cell in zip(grid["cells"], calculated_grid[0]["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        for hour, old in zip(cell["hours"], old_cell["hours"], strict=True):
            evidence = hour["surface"]["snowfall_amount_guidance"]
            assert evidence["field"]["value"] is None and evidence["field"]["weights"] == {}
            assert evidence["field"]["unit"] == "m"
            native = {row["model"]: row for row in evidence["native_contributors"]}
            for model in ("HRRR", "RAP", "NBM"):
                assert native[model]["value"] == pytest.approx(hour["horizon_hours"] / 100)
                assert native[model]["active_weight"] == 0 and native[model]["provenance"]
            assert evidence["native_slr"][0]["value"] == pytest.approx(12, abs=1e-12)
            assert evidence["derived_contributors"][0]["value"] is not None
            assert evidence["derived_contributors"][0]["spatial_extraction"]
            original_swe = extract_snowfall_contributors(
                swe,
                latitude=cell["latitude"],
                longitude=cell["longitude"],
                valid_time=hour["valid_time"],
            )
            assert hour["surface"]["snowfall_guidance"] == original_swe
            unchanged = deepcopy(hour)
            for name in (AMOUNT, SNOW):
                unchanged["surface"]["fields"].pop(name)
            for name in ("snowfall_amount_guidance", "snowfall_guidance"):
                unchanged["surface"].pop(name)
            assert unchanged == old
    assert roles == {True, False}
    for before, view in zip(originals, [*swe, *amounts], strict=True):
        xr.testing.assert_identical(before, view.dataset)
    encoded = canonical_json_bytes(grid)
    restored = json.loads(encoded)
    point = extract_grid_point(restored, latitude=LATITUDE, longitude=LONGITUDE)
    assert point["hours"] == next(c["hours"] for c in grid["cells"] if c["is_forecast_point"])
    assert point["snowfall_guidance"] == snow_descriptor
    assert point["snowfall_amount_guidance"] == descriptor
    service, factory, objects = memory_service
    issued = service.issue(point, batch_run_id=uuid4(), location_index=0)
    before = len(factory.issued_forecasts), len(objects.objects)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    assert (len(factory.issued_forecasts), len(objects.objects)) == before
    point["hours"][0]["surface"]["snowfall_amount_guidance"]["native_contributors"][0]["value"] = -1
    assert service.read(issued.issued_forecast_id)["forecast"] != point
    assert canonical_json_bytes(restored) == encoded


def test_nbm_cloud_baseline_reuses_guidance_and_preserves_every_other_field(
    prepared_surface, calculated_grid, monkeypatch, memory_service
):
    views = _cloud_views()
    originals = [view.dataset.copy(deep=True) for view in views]
    descriptor = {"status": "prepared", "source_status": {}}
    loader = Mock(return_value=views)
    monkeypatch.setattr("mesoforge.application.prepared_cloud.load_cloud_guidance", loader)
    regions = attach_cloud_guidance(
        PreparedRegions([prepared_surface, prepared_surface], {}), descriptor
    )
    loader.assert_called_once_with(descriptor, target_reference_time=TARGET)
    assert regions.regions[0]._cloud_views is regions.regions[1]._cloud_views
    assert prepared_surface._cloud_guidance is None
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Guidance already loaded")),
        patch("requests.Session", side_effect=AssertionError("No provider calls")),
    ):
        grid = build_local_surface_grid(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=regions.regions[0]._forecast_column,
            geometry=TEST_GEOMETRY,
        )
        replay = build_local_surface_grid(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=regions.regions[1]._forecast_column,
            geometry=TEST_GEOMETRY,
        )
    assert canonical_json_bytes(grid) == canonical_json_bytes(replay)
    roles, observed_cloud = set(), []
    for cell, old_cell in zip(grid["cells"], calculated_grid[0]["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        assert len(cell["hours"]) == 36
        gradient = _gradient(cell["latitude"], cell["longitude"])
        for hour, old in zip(cell["hours"], old_cell["hours"], strict=True):
            evidence = hour["surface"]["cloud_guidance"]
            sources = {source["model"]: source for source in evidence["contributors"]}
            for model, offset in (("HRRR", 20), ("GFS", 40), ("RAP", 50), ("NBM", 70)):
                source = sources[model]
                assert source["value"] == pytest.approx(offset + gradient)
                assert source["unit"] == "percent" and source["provenance"]
                assert source["active_weight"] == (1 if model == "NBM" else 0)
                assert source["spatial_extraction"] and source["sky_category"]
            if hour["horizon_hours"] % 3:
                assert sources["IFS"]["value"] is None and sources["IFS"]["missing_reasons"]
            else:
                assert sources["IFS"]["value"] == pytest.approx(60 + gradient)
            control_pair = next(
                row for row in evidence["comparisons"] if row["models"] == ["HRRR", "GFS"]
            )
            assert control_pair["difference_left_minus_right"] == pytest.approx(-20)
            assert evidence["active_policy"] == CLOUD_ACTIVE_POLICY
            active = hour["surface"]["fields"][CLOUD]
            assert active == evidence["field"]
            assert active["value"] == sources["NBM"]["value"] / 100
            assert active["cloud_percentage"] == sources["NBM"]["value"]
            assert active["weights"] == {"NBM": 1.0} and active["unit"] == "1"
            assert active["provenance"] == sources["NBM"]["provenance"]
            assert active["sky_category"] == sources["NBM"]["sky_category"]
            old_fields = deepcopy(hour)
            old_fields["surface"].pop("cloud_guidance")
            old_fields["surface"]["fields"][CLOUD] = deepcopy(old["surface"]["fields"][CLOUD])
            assert old_fields == old
        observed_cloud.append(
            cell["hours"][0]["surface"]["cloud_guidance"]["contributors"][0]["value"]
        )
    assert roles == {True, False} and max(observed_cloud) > min(observed_cloud)
    for before, view in zip(originals, views, strict=True):
        xr.testing.assert_identical(before, view.dataset)
    point = extract_grid_point(
        json.loads(canonical_json_bytes(grid)), latitude=LATITUDE, longitude=LONGITUDE
    )
    assert point["hours"] == next(
        cell["hours"] for cell in grid["cells"] if cell["is_forecast_point"]
    )
    assert point["cloud_guidance"] == descriptor
    assert point["hours"][0]["surface"]["cloud_guidance"]["contributors"][0][
        "value"
    ] == pytest.approx(20)
    assert point["hours"][0]["surface"]["fields"][CLOUD]["value"] == pytest.approx(0.7)
    assert point["hours"][0]["surface"]["fields"][CLOUD]["cloud_percentage"] == pytest.approx(70)
    service, factory, objects = memory_service
    issued = service.issue(point, batch_run_id=uuid4(), location_index=0)
    before = len(factory.issued_forecasts), len(objects.objects)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    assert (len(factory.issued_forecasts), len(objects.objects)) == before


def test_visibility_reuses_guidance_preserves_cloud_and_winter_fields_and_replays(
    prepared_surface, monkeypatch, memory_service
):
    views = _visibility_views()
    originals = [view.dataset.copy(deep=True) for view in views]
    baseline = replace(
        prepared_surface,
        _cloud_views=_cloud_views(),
        _cloud_guidance={"status": "prepared"},
        _type_views=_type_views(),
        _type_guidance={"status": "prepared"},
        _snow_views=[*_snow_views(), *_snow_views("RAP")],
        _snow_guidance={"status": "prepared"},
        _snow_amount_views=_amount_views(),
        _snow_amount_guidance={"status": "prepared"},
        _pop_views=[_pop_entry(hours=HOURS)],
        _pop_guidance={"status": "prepared"},
        _probability_views=_spatial_probability_views(),
    )
    descriptor = {"status": "prepared", "source_status": {}}
    loader = Mock(return_value=views)
    monkeypatch.setattr(
        "mesoforge.application.prepared_visibility.load_visibility_guidance", loader
    )
    regions = attach_visibility_guidance(PreparedRegions([baseline, baseline], {}), descriptor)
    loader.assert_called_once_with(descriptor, target_reference_time=TARGET)
    assert regions.regions[0]._visibility_views is regions.regions[1]._visibility_views
    assert baseline._visibility_guidance is None
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Guidance already loaded")),
        patch("requests.Session", side_effect=AssertionError("No provider calls")),
    ):
        previous, grid, replay = [
            build_local_surface_grid(
                latitude=LATITUDE,
                longitude=LONGITUDE,
                calculate_column=prepared._forecast_column,
                geometry=TEST_GEOMETRY,
            )
            for prepared in (baseline, *regions.regions)
        ]
    assert canonical_json_bytes(grid) == canonical_json_bytes(replay)
    roles, observed_values = set(), []
    for cell, old_cell in zip(grid["cells"], previous["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        assert len(cell["hours"]) == 36
        # Independently identify the closest source coordinate; visibility uses no smoothing.
        nearest_latitude = min((44.0, 46.0), key=lambda value: abs(cell["latitude"] - value))
        nearest_longitude = min((-94.0, -92.0), key=lambda value: abs(cell["longitude"] - value))
        gradient = 100 * _gradient(nearest_latitude, nearest_longitude)
        for hour, old in zip(cell["hours"], old_cell["hours"], strict=True):
            evidence = hour["surface"]["visibility_guidance"]
            assert evidence["field"]["value"] is None and evidence["field"]["weights"] == {}
            assert (
                evidence["field"]["unit"] == "m"
                and evidence["field"]["status"] == "policy_unavailable"
            )
            sources = {source["model"]: source for source in evidence["contributors"]}
            for model, offset in (("HRRR", 3000), ("GFS", 5000), ("RAP", 7000), ("NBM", 8000)):
                source = sources[model]
                expected = offset + hour["horizon_hours"] + gradient
                assert source["value"] == pytest.approx(expected)
                assert source["display_miles"] == pytest.approx(expected / 1609.344)
                assert source["native_value"] == pytest.approx(expected)
                assert source["unit"] == "m" and source["provenance"]
                assert source["active_weight"] == 0
                assert source["spatial_extraction"]["method"] == "nearest_native_grid_cell"
            assert sources["IFS"]["value"] is None and sources["IFS"]["missing_reasons"]
            pair = next(row for row in evidence["comparisons"] if row["models"] == ["HRRR", "GFS"])
            assert pair["difference_left_minus_right"] == pytest.approx(-2000)
            unchanged = deepcopy(hour)
            unchanged["surface"].pop("visibility_guidance")
            unchanged["surface"]["fields"].pop("visibility")
            assert unchanged == old
        observed_values.append(
            cell["hours"][0]["surface"]["visibility_guidance"]["contributors"][0]["value"]
        )
    assert roles == {True, False} and max(observed_values) > min(observed_values)
    for before, view in zip(originals, views, strict=True):
        xr.testing.assert_identical(before, view.dataset)
    point = extract_grid_point(
        json.loads(canonical_json_bytes(grid)), latitude=LATITUDE, longitude=LONGITUDE
    )
    assert point["hours"] == next(
        cell["hours"] for cell in grid["cells"] if cell["is_forecast_point"]
    )
    assert point["visibility_guidance"] == descriptor
    service, factory, objects = memory_service
    issued = service.issue(point, batch_run_id=uuid4(), location_index=0)
    before = len(factory.issued_forecasts), len(objects.objects)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    assert (len(factory.issued_forecasts), len(objects.objects)) == before


def test_thunder_native_periods_share_guidance_preserve_all_previous_fields_and_replay(
    prepared_surface, monkeypatch, memory_service
):
    views = _thunder_views()
    originals = [view.dataset.copy(deep=True) for view in views]
    baseline = replace(
        prepared_surface,
        _cloud_views=_cloud_views(),
        _cloud_guidance={"status": "prepared"},
        _type_views=_type_views(),
        _type_guidance={"status": "prepared"},
        _snow_views=[*_snow_views(), *_snow_views("RAP")],
        _snow_guidance={"status": "prepared"},
        _snow_amount_views=_amount_views(),
        _snow_amount_guidance={"status": "prepared"},
        _pop_views=[_pop_entry(hours=HOURS)],
        _pop_guidance={"status": "prepared"},
        _probability_views=_spatial_probability_views(),
        _visibility_views=_visibility_views(),
        _visibility_guidance={"status": "prepared"},
    )
    descriptor = {"status": "prepared", "source_status": {}}
    loader = Mock(return_value=views)
    monkeypatch.setattr("mesoforge.application.prepared_thunder.load_thunder_guidance", loader)
    regions = attach_thunder_guidance(PreparedRegions([baseline, baseline], {}), descriptor)
    loader.assert_called_once_with(descriptor, target_reference_time=TARGET)
    assert regions.regions[0]._thunder_views is regions.regions[1]._thunder_views
    assert baseline._thunder_guidance is None
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Guidance already loaded")),
        patch("requests.Session", side_effect=AssertionError("No provider calls")),
    ):
        previous, grid, replay = [
            build_local_surface_grid(
                latitude=LATITUDE,
                longitude=LONGITUDE,
                calculate_column=prepared._forecast_column,
                geometry=TEST_GEOMETRY,
            )
            for prepared in (baseline, *regions.regions)
        ]
    assert canonical_json_bytes(grid) == canonical_json_bytes(replay)
    roles, sampled = set(), []
    for cell, old_cell in zip(grid["cells"], previous["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        assert len(cell["hours"]) == 36
        latitude = min((44.0, 46.0), key=lambda value: abs(cell["latitude"] - value))
        longitude = min((-94.0, -92.0), key=lambda value: abs(cell["longitude"] - value))
        gradient = 0.01 * _gradient(latitude, longitude)
        for hour, old in zip(cell["hours"], old_cell["hours"], strict=True):
            evidence = hour["surface"]["thunder_guidance"]
            field = evidence["field"]
            expected = 0.25 + hour["horizon_hours"] / 1000 + gradient
            assert field["value"] == pytest.approx(expected)
            assert field["unit"] == "1" and field["display_percent"] == pytest.approx(
                expected * 100
            )
            assert field["weights"] == {"NBM": 1.0} and field["policy"]["temporary"]
            sources = {source["source_id"]: source for source in evidence["contributors"]}
            for source_id, duration, offset in (
                ("NBM_1H", 1, 0.25),
                ("NBM_3H", 3, 0.5),
                ("NBM_6H", 6, 0.7),
            ):
                source = sources[source_id]
                if hour["horizon_hours"] % duration:
                    assert source["value"] is None and source["missing_reasons"]
                    assert source["active_weight"] == 0
                    continue
                expected_source = offset + hour["horizon_hours"] / 1000 + gradient
                assert source["value"] == pytest.approx(expected_source)
                assert source["native_value"] == pytest.approx(expected_source * 100)
                assert source["duration_hours"] == duration and source["provenance"]
                assert source["active_weight"] == (1 if source_id == "NBM_1H" else 0)
                assert source["spatial_extraction"]["method"] == "nearest_native_grid_cell"
                assert source["event_definition"]["physical_threshold"] is None
                assert source["spatial_support"]["radius_km"] is None
            assert all(
                pair["difference_left_minus_right"] is None for pair in evidence["comparisons"]
            )
            assert all(pair["status"] == "incompatible" for pair in evidence["comparisons"])
            unchanged = deepcopy(hour)
            unchanged["surface"].pop("thunder_guidance")
            unchanged["surface"]["fields"].pop("probability_of_thunder_1h")
            assert unchanged == old
        sampled.append(cell["hours"][0]["surface"]["thunder_guidance"]["field"]["value"])
    assert roles == {True, False} and max(sampled) > min(sampled)
    for before, view in zip(originals, views, strict=True):
        xr.testing.assert_identical(before, view.dataset)
    point = extract_grid_point(
        json.loads(canonical_json_bytes(grid)), latitude=LATITUDE, longitude=LONGITUDE
    )
    assert point["hours"] == next(
        cell["hours"] for cell in grid["cells"] if cell["is_forecast_point"]
    )
    assert point["thunder_guidance"] == descriptor
    service, factory, objects = memory_service
    issued = service.issue(point, batch_run_id=uuid4(), location_index=0)
    before = len(factory.issued_forecasts), len(objects.objects)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    assert (len(factory.issued_forecasts), len(objects.objects)) == before


def test_ice_guidance_shares_native_intervals_preserves_all_previous_fields_and_replays(
    prepared_surface, monkeypatch, memory_service
):
    views = _ice_views()
    originals = [view.dataset.copy(deep=True) for view in views]
    datasets = {
        model: source.copy(deep=True) for model, source in prepared_surface._guidance.items()
    }
    manifest = deepcopy(prepared_surface._manifest)
    manifest["qpf_inputs"] = []
    for model, dataset in datasets.items():
        _add_qpf(dataset, [horizon / 10 for horizon in HOURS])
        metadata = {}
        for lead_time in dataset.source_lead_time.values:
            lead = int(lead_time / np.timedelta64(1, "h"))
            parent = {"model": model, "source_lead_hours": lead, "raw_sha256": "f" * 64}
            metadata[str(lead)] = {"parents": [parent]}
            manifest["qpf_inputs"].append({**parent, "messages": [{"raw_sha256": "f" * 64}]})
        dataset.attrs["qpf_metadata_json"] = json.dumps(metadata)
    baseline = replace(
        prepared_surface,
        _guidance=datasets,
        _manifest=manifest,
        _cloud_views=_cloud_views(),
        _cloud_guidance={"status": "prepared"},
        _type_views=_type_views(),
        _type_guidance={"status": "prepared"},
        _snow_views=[*_snow_views(), *_snow_views("RAP")],
        _snow_guidance={"status": "prepared"},
        _snow_amount_views=_amount_views(),
        _snow_amount_guidance={"status": "prepared"},
        _pop_views=[_pop_entry(hours=HOURS)],
        _pop_guidance={"status": "prepared"},
        _probability_views=_spatial_probability_views(),
        _visibility_views=_visibility_views(),
        _visibility_guidance={"status": "prepared"},
        _thunder_views=_thunder_views(),
        _thunder_guidance={"status": "prepared"},
    )
    descriptor = {"status": "prepared", "source_status": {}}
    loader = Mock(return_value=views)
    monkeypatch.setattr("mesoforge.application.prepared_ice.load_ice_guidance", loader)
    regions = attach_ice_guidance(PreparedRegions([baseline, baseline], {}), descriptor)
    loader.assert_called_once_with(descriptor, target_reference_time=TARGET)
    assert regions.regions[0]._ice_views is regions.regions[1]._ice_views
    assert baseline._ice_guidance is None
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Guidance already loaded")),
        patch("requests.Session", side_effect=AssertionError("No provider calls")),
    ):
        previous, grid, replay = [
            build_local_surface_grid(
                latitude=LATITUDE,
                longitude=LONGITUDE,
                calculate_column=prepared._forecast_column,
                geometry=TEST_GEOMETRY,
            )
            for prepared in (baseline, *regions.regions)
        ]
    assert canonical_json_bytes(grid) == canonical_json_bytes(replay)
    roles, sampled = set(), []
    for cell, old_cell in zip(grid["cells"], previous["cells"], strict=True):
        roles.add(cell["inside_editable_domain"])
        assert len(cell["hours"]) == 36
        latitude = min((44.0, 46.0), key=lambda value: abs(cell["latitude"] - value))
        longitude = min((-94.0, -92.0), key=lambda value: abs(cell["longitude"] - value))
        gradient = 0.01 * _gradient(latitude, longitude)
        for hour, old in zip(cell["hours"], old_cell["hours"], strict=True):
            evidence = hour["surface"]["ice_guidance"]
            assert set(evidence["fields"]) == {FLAT_ICE, FRZR}
            for field in evidence["fields"].values():
                assert field["value"] is None and field["weights"] == {}
                assert field["unit"] == "kg/m^2" and field["status"] == "policy_unavailable"
            sources = {source["source_id"]: source for source in evidence["contributors"]}
            for source_id, duration, offset in (
                ("NBM_FICEAC_1H", 1, 0.5),
                ("NBM_FICEAC_6H", 6, 1.5),
                ("HRRR_FRZR", 1, 1.0),
                ("RAP_FRZR", 1, 1.25),
            ):
                source = sources[source_id]
                if hour["horizon_hours"] % duration:
                    assert source["value"] is None and source["missing_reasons"]
                    continue
                expected = offset + hour["horizon_hours"] / 100 + gradient
                assert source["value"] == pytest.approx(expected)
                assert source["native_value"] == pytest.approx(expected)
                assert source["duration_hours"] == duration and source["provenance"]
                assert source["active_weight"] == 0
                assert source["spatial_extraction"]["method"] == "nearest_native_grid_cell"
                assert "display_inches" not in source
            liquid = sources["HRRR_FRZR"]
            assert liquid["quantity_kind"] != sources["NBM_FICEAC_1H"]["quantity_kind"]
            if hour["horizon_hours"] > 1:
                assert liquid["spatial_extraction"]["native_start_amount"] == 2
                assert liquid["spatial_extraction"]["native_end_amount"] - 2 == liquid["value"]
            pair = next(
                row
                for row in evidence["comparisons"]
                if row["source_ids"] == ["HRRR_FRZR", "RAP_FRZR"]
            )
            assert pair["difference_left_minus_right"] == pytest.approx(-0.25)
            unrelated = next(
                row
                for row in evidence["comparisons"]
                if row["source_ids"] == ["NBM_FICEAC_1H", "HRRR_FRZR"]
            )
            assert (
                unrelated["status"] == "incompatible"
                and unrelated["difference_left_minus_right"] is None
            )
            assert old["surface"]["fields"][QPF]["value"] is not None
            unchanged = deepcopy(hour)
            unchanged["surface"].pop("ice_guidance")
            for name in (FLAT_ICE, FRZR):
                unchanged["surface"]["fields"].pop(name)
            assert unchanged == old
        sampled.append(cell["hours"][0]["surface"]["ice_guidance"]["contributors"][0]["value"])
    assert roles == {True, False} and max(sampled) > min(sampled)
    for before, view in zip(originals, views, strict=True):
        xr.testing.assert_identical(before, view.dataset)
    point = extract_grid_point(
        json.loads(canonical_json_bytes(grid)), latitude=LATITUDE, longitude=LONGITUDE
    )
    assert point["hours"] == next(
        cell["hours"] for cell in grid["cells"] if cell["is_forecast_point"]
    )
    assert point["ice_guidance"] == descriptor
    service, factory, objects = memory_service
    issued = service.issue(point, batch_run_id=uuid4(), location_index=0)
    before = len(factory.issued_forecasts), len(objects.objects)
    assert service.read(issued.issued_forecast_id)["forecast"] == point
    assert (len(factory.issued_forecasts), len(objects.objects)) == before


def test_qpf_spans_both_domains_and_exact_point_replays_with_provenance(prepared_surface):
    datasets = {
        model: source.copy(deep=True) for model, source in prepared_surface._guidance.items()
    }
    manifest = deepcopy(prepared_surface._manifest)
    manifest["qpf_inputs"] = []
    for model, factor in (("HRRR", 0.1), ("GFS", 0.2)):
        dataset = datasets[model]
        _add_qpf(dataset, [horizon * factor for horizon in HOURS])
        longitude, latitude = np.meshgrid(dataset.x.values, dataset.y.values)
        dataset[QPF].values += factor / 10 * _gradient(latitude, longitude)
        metadata = {}
        for lead_time in dataset.source_lead_time.values:
            lead = int(lead_time / np.timedelta64(1, "h"))
            parent = {"model": model, "source_lead_hours": lead, "raw_sha256": "f" * 64}
            metadata[str(lead)] = {"parents": [parent]}
            manifest["qpf_inputs"].append({**parent, "messages": [{"raw_sha256": "f" * 64}]})
        dataset.attrs["qpf_metadata_json"] = json.dumps(metadata)
    prepared = replace(prepared_surface, _guidance=datasets, _manifest=manifest)
    numerical_before = prepared._forecast_column(latitude=LATITUDE, longitude=LONGITUDE)
    pop_dataset, pop_crs, pop_manifest = _pop_entry(hours=HOURS)
    pop_dataset = pop_dataset.assign_coords(x=[-94.0, -92.0], y=[44.0, 46.0])
    longitude, latitude = np.meshgrid(pop_dataset.x.values, pop_dataset.y.values)
    pop_dataset[POP].values += 0.02 * _gradient(latitude, longitude)
    pop_dataset.native_probability_percent.values[:] = pop_dataset[POP].values * 100
    pop_original = pop_dataset.copy(deep=True)
    prepared = replace(
        prepared,
        _pop_views=[(pop_dataset, pop_crs, pop_manifest)],
        _pop_guidance={"status": "prepared", "source": "NBM fixture"},
    )
    active_pop_before = prepared._forecast_column(latitude=LATITUDE, longitude=LONGITUDE)
    probability_views = _spatial_probability_views()
    probability_originals = [view.dataset.copy(deep=True) for view in probability_views]
    prepared = replace(prepared, _probability_views=probability_views)
    calculate = Mock(wraps=prepared._forecast_column)
    with (
        patch("xarray.open_dataset", side_effect=AssertionError("Already loaded guidance")),
        patch("requests.Session", side_effect=AssertionError("No per-node downloads")),
    ):
        grid = build_local_surface_grid(
            latitude=LATITUDE,
            longitude=LONGITUDE,
            calculate_column=calculate,
            geometry=TEST_GEOMETRY,
        )
    assert calculate.call_count == 25
    assert sum(cell["inside_editable_domain"] for cell in grid["cells"]) == 9
    assert sum(cell["context_only"] for cell in grid["cells"]) == 16
    for cell in grid["cells"]:
        gradient = _gradient(cell["latitude"], cell["longitude"])
        assert len(cell["hours"]) == 36
        for hour in cell["hours"]:
            horizon = hour["horizon_hours"]
            qpf = hour["surface"]["fields"][QPF]
            factor = 0.13 if horizon <= 18 else 0.14
            assert qpf["value"] == pytest.approx(factor * (horizon + gradient / 10), abs=1e-12)
            assert qpf["unit"] == "kg/m^2"
            end = TARGET + np.timedelta64(horizon, "h")
            assert qpf["interval_end"] == str(np.datetime_as_string(end, unit="s")) + "Z"
            assert (
                qpf["interval_start"]
                == str(np.datetime_as_string(end - np.timedelta64(1, "h"), unit="s")) + "Z"
            )
            native = hour["surface"]["contributors"]
            pop = hour["surface"]["fields"][POP]
            assert pop["value"] == pytest.approx(0.4 + 0.02 * gradient, abs=1e-12)
            assert pop["weights"] == {"NBM": 1.0}
            assert pop["threshold"] == {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}
            assert pop["interval_start"] == qpf["interval_start"]
            assert pop["interval_end"] == qpf["interval_end"]
            assert native["NBM"]["role"] == "field_source"
            assert set(native["NBM"]["fields"]) == {POP}
            assert native["NBM"]["fields"][POP]["value"] == pop["value"]
            guidance = hour["surface"]["probability_guidance"]
            assert len(guidance["contributors"]) == 3
            paired = [pair for pair in guidance["comparisons"] if pair["status"] == "comparable"]
            if horizon == 6:
                assert [source["value"] for source in guidance["contributors"]] == pytest.approx(
                    [base + 0.02 * gradient for base in (0.4, 0.7, 0.6)], abs=1e-12
                )
                assert len(paired) == 1
                assert paired[0]["left"]["source_id"] == "NBM_NATIVE6"
                assert paired[0]["right"]["source_id"] == "GEFS_NATIVE6"
                assert paired[0]["delta"] == pytest.approx(-0.3, abs=1e-12)
                for source, duration in zip(guidance["contributors"], (6, 6, 24), strict=True):
                    assert source["status"] == "available"
                    assert source["interval_end"] == hour["valid_time"]
                    expected_start = end - np.timedelta64(duration, "h")
                    assert (
                        source["interval_start"]
                        == str(np.datetime_as_string(expected_start, unit="s")) + "Z"
                    )
                    assert source["provenance"]["raw_sha256"] == "a" * 64
                    assert source["manifest_sha256"] == "b" * 64
                    assert (
                        source["spatial_extraction"]["method"] == "native_grid_bilinear_probability"
                    )
                assert all(
                    pair["delta"] is None for pair in guidance["comparisons"] if pair not in paired
                )
            else:
                assert not paired
                assert all(pair["delta"] is None for pair in guidance["comparisons"])
                for source in guidance["contributors"]:
                    assert source["value"] is None and source["status"] == "unavailable"
                    assert "no temporal filling" in source["missing_reasons"][0]
                    assert source["available_native_intervals"]
            assert all(
                source["role"] == "shadow" and source["active_weight"] == 0
                for source in guidance["contributors"]
            )
            assert (
                native["NBM"]["fields"][POP]["provenance"]["source_inputs"][0]["raw_sha256"]
                == "e" * 64
            )
            for model in ("HRRR", "GFS"):
                field = native[model]["fields"][QPF]
                assert field["provenance"]["prepared_sha256"] == "c" * 64
                assert field["normalization"]["parents"][0]["raw_sha256"] == "f" * 64
                assert field["spatial_extraction"]["method"] == "native_grid_bilinear_depth"
            for model in ("RAP", "IFS"):
                assert native[model]["fields"][QPF]["value"] is None
                assert native[model]["fields"][QPF]["missing_reasons"]
                assert native[model]["role"] == "shadow"
    encoded = canonical_json_bytes(grid)
    with patch.object(
        PreparedPointForecast, "_forecast_column", side_effect=AssertionError("Grid replay")
    ):
        first = extract_grid_point(json.loads(encoded), latitude=LATITUDE, longitude=LONGITUDE)
        second = extract_grid_point(json.loads(encoded), latitude=LATITUDE, longitude=LONGITUDE)
    assert first == second and canonical_json_bytes(first["local_grid_baseline"]) == encoded
    center = next(cell for cell in grid["cells"] if cell["is_forecast_point"])
    assert first["hours"] == center["hours"]
    for hour, before in zip(first["hours"], numerical_before["hours"], strict=True):
        assert hour["temperature"] == before["temperature"]
        for variable in FIELDS:
            assert hour["surface"]["fields"][variable] == before["surface"]["fields"][variable]
        assert hour["surface"]["fields"][QPF] == before["surface"]["fields"][QPF]
        for model in before["surface"]["contributors"]:
            for variable in FIELDS:
                assert (
                    hour["surface"]["contributors"][model]["fields"][variable]
                    == before["surface"]["contributors"][model]["fields"][variable]
                )
    assert QPF not in prepared_surface._guidance["HRRR"]
    xr.testing.assert_identical(pop_dataset, pop_original)
    for before, after in zip(active_pop_before["hours"], first["hours"], strict=True):
        assert before["surface"]["fields"] == after["surface"]["fields"]
        assert before["surface"]["contributors"] == after["surface"]["contributors"]
    for original, view in zip(probability_originals, probability_views, strict=True):
        xr.testing.assert_identical(original, view.dataset)
