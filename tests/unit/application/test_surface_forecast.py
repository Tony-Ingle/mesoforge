"""Point extraction and surface-field integration over prepared inputs only."""

from __future__ import annotations

import math
from copy import deepcopy

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.point_forecast import PreparedPointForecast, prepare_demo_files
from mesoforge.application.surface_forecast import FIELD_UNITS, extract_surface_hour
from tests.unit.application.test_prepared_temperature import phase2_configuration

TARGET = np.datetime64("2026-09-11T12:00:00", "ns")
T, DEW = "air_temperature_2m", "dew_point_temperature_2m"
U, V, GUST = "eastward_wind_10m", "northward_wind_10m", "wind_gust_10m"
RH, SPEED, DIRECTION = "relative_humidity_2m", "wind_speed_10m", "wind_from_direction_10m"
CRS = pyproj.CRS.from_epsg(4326)


@pytest.fixture(scope="module")
def configuration():
    return phase2_configuration().blend_configuration


def _dataset(model, *, age=0, hours=(1, 2, 19), values=None):
    defaults = {
        "HRRR": {T: 290.15, DEW: 280.15, U: 0.0, V: -10.0, GUST: 12.0},
        "GFS": {T: 300.15, DEW: 290.15, U: -10.0, V: 0.0, GUST: 14.0},
        "RAP": {T: 310.0, DEW: 305.0, U: 30.0, V: 40.0, GUST: 60.0},
        "IFS": {T: 300.0, DEW: 285.0, U: 6.0, V: -8.0},
    }
    fields = values if values is not None else defaults[model]
    cycle = TARGET - np.timedelta64(age, "h")
    leads = np.asarray([hour + age for hour in hours], dtype="timedelta64[h]")
    dataset = xr.Dataset(
        data_vars={
            variable: (
                ("source_lead_time", "y", "x"),
                np.full((len(hours), 2, 2), value, dtype=np.float64),
                {"unit_id": FIELD_UNITS[variable], "units": FIELD_UNITS[variable]},
            )
            for variable, value in fields.items()
        },
        coords={
            "x": [-94.0, -93.0],
            "y": [44.0, 45.0],
            "forecast_reference_time": cycle,
            "source_lead_time": leads,
            "source_valid_time": ("source_lead_time", cycle + leads),
        },
        attrs={"wind_reference": "earth_relative"},
    )
    return dataset


def _case(horizon=1):
    datasets = {
        "HRRR": (_dataset("HRRR"), CRS, None),
        "GFS": (_dataset("GFS", age=6), CRS, None),
    }
    sources = [_temperature_source(model, row[0], horizon) for model, row in datasets.items()]
    return datasets, sources


def _temperature_source(model, dataset, horizon):
    cycle = dataset.forecast_reference_time.values[()]
    valid = TARGET + np.timedelta64(horizon, "h")
    present = bool(np.any(dataset.source_valid_time.values == valid))
    return {
        "model": model,
        "cycle": str(np.datetime_as_string(cycle, unit="s")) + "Z",
        "source_lead_hours": int((valid - cycle) / np.timedelta64(1, "h")) if present else None,
        "weight": {"HRRR": 0.7, "GFS": 0.3}.get(model, 0.0),
        "temperature": {"value": float(dataset[T][0, 0, 0]) if present else None, "unit": "K"},
        "missing_reasons": [] if present else ["No native temperature at this valid time"],
    }


def _extract(configuration, datasets, sources, *, horizon=1, selection=None):
    return extract_surface_hour(
        datasets=datasets,
        temperature_sources=sources,
        temperature_k=293.15,
        latitude=44.5,
        longitude=-93.5,
        horizon=horizon,
        target_reference_time=TARGET,
        configuration=configuration,
        selection=selection,
    )


@pytest.mark.parametrize(
    "horizon,dew,u,v,gust,weights",
    [
        (1, 283.15, -3.0, -7.0, 12.6, {"HRRR": 0.7, "GFS": 0.3}),
        (19, 284.15, -4.0, -6.0, 12.8, {"HRRR": 0.6, "GFS": 0.4}),
    ],
)
def test_valid_time_alignment_preserves_temperature_and_uses_approved_surface_weights(
    configuration, horizon, dew, u, v, gust, weights
):
    datasets, sources = _case(horizon)
    result = _extract(configuration, datasets, sources, horizon=horizon)
    fields = result["fields"]
    assert fields[T]["value"] == 293.15  # Existing 70/30 temperature supplied unchanged.
    for variable, expected in ((DEW, dew), (U, u), (V, v), (GUST, gust)):
        assert fields[variable]["value"] == pytest.approx(expected)
        assert fields[variable]["weights"] == weights
    assert fields[SPEED]["value"] == pytest.approx(math.hypot(u, v))
    expected_direction = math.degrees(math.atan2(-u, -v)) % 360.0
    assert fields[DIRECTION]["value"] == pytest.approx(expected_direction)
    assert fields[DIRECTION]["value"] != pytest.approx(90.0 * weights["GFS"])
    assert fields[DEW]["unit"] == "K"
    assert fields[SPEED]["unit"] == fields[GUST]["unit"] == "m/s"
    assert fields[DIRECTION]["unit"] == "degree"
    contributors = result["contributors"]
    assert contributors["HRRR"]["source_lead_hours"] == horizon
    assert contributors["GFS"]["source_lead_hours"] == horizon + 6
    assert contributors["GFS"]["cycle"] == "2026-09-11T06:00:00Z"
    assert fields["cloud_area_fraction"]["value"] is None
    assert "no retained approved" in fields["cloud_area_fraction"]["missing_reasons"][0]


def test_rh_uses_blended_temperature_and_dew_point_not_an_average_of_model_rh(configuration):
    datasets, sources = _case()
    result = _extract(configuration, datasets, sources)
    # Independent vapor-pressure calculation at 20 C / 10 C, before taking the ratio.
    saturation = 6.112 * math.exp(17.67 * 20.0 / 263.5)
    vapor = 6.112 * math.exp(17.67 * 10.0 / 253.5)
    assert result["fields"][RH]["value"] == pytest.approx(100.0 * vapor / saturation)
    assert result["fields"][RH]["unit"] == "%"
    native = result["contributors"]
    independently_weighted_rh = (
        native["HRRR"]["fields"][RH]["value"] * 0.7 + native["GFS"]["fields"][RH]["value"] * 0.3
    )
    assert result["fields"][RH]["value"] != pytest.approx(independently_weighted_rh, abs=0.001)


def test_ifs_native_gap_and_shadow_values_never_change_the_active_surface(configuration):
    datasets, sources = _case(horizon=2)
    control = _extract(configuration, datasets, sources, horizon=2)
    for model, age, hours in (("RAP", 3, (1, 2, 19)), ("IFS", 6, (3,))):
        dataset = _dataset(model, age=age, hours=hours)
        datasets[model] = (dataset, CRS, None)
        sources.append(_temperature_source(model, dataset, 2))
    result = _extract(configuration, datasets, sources, horizon=2)
    assert result["fields"] == control["fields"]
    assert result["contributors"]["RAP"]["role"] == "shadow"
    assert result["contributors"]["RAP"]["fields"][U]["value"] == 30.0
    for variable in (DEW, U, V, GUST):
        field = result["contributors"]["IFS"]["fields"][variable]
        assert field["value"] is None
        assert "no native guidance" in field["missing_reasons"][0]
    assert result["contributors"]["IFS"]["source_lead_hours"] is None


def test_missing_native_corner_and_gust_use_explicit_field_and_coupled_fallback(configuration):
    datasets, sources = _case()
    hrrr = datasets["HRRR"][0]
    hrrr[DEW].values[0, 0, 0] = np.nan
    hrrr[GUST].values[0, 0, 0] = np.nan
    result = _extract(configuration, datasets, sources)
    assert result["fields"][T]["value"] == 293.15
    for variable, expected in ((DEW, 290.15), (U, -10.0), (V, 0.0), (GUST, 14.0)):
        assert result["fields"][variable]["value"] == pytest.approx(expected)
        assert result["fields"][variable]["weights"] == {"GFS": 1.0}
        assert result["fields"][variable]["missing_reasons"]
    native = result["contributors"]["HRRR"]["fields"]
    assert native[DEW]["value"] is None
    assert "finite native grid corners" in native[DEW]["missing_reasons"][0]
    assert native[U]["value"] == 0.0 and native[V]["value"] == -10.0
    assert (
        result["source_validation"]["HRRR"]["wind_gust"]["rejection_scope"]
        == "coupled-wind-gust-point"
    )


def test_provider_missing_field_evidence_prevents_substitution_from_unselected_values(
    configuration,
):
    datasets, sources = _case()
    selection = {"models": {}}
    for model, source in zip(datasets, sources, strict=True):
        selection["models"][model] = {
            "candidates": [
                {
                    "status": "metadata_complete",
                    "probes": [
                        {
                            "source_lead_hours": source["source_lead_hours"],
                            "missing_fields": {DEW: "Provider dew point absent at decision time"}
                            if model == "HRRR"
                            else {},
                        }
                    ],
                }
            ]
        }
    result = _extract(configuration, datasets, sources, selection=selection)
    field = result["contributors"]["HRRR"]["fields"][DEW]
    assert field["value"] is None
    assert field["missing_reasons"] == ["Provider dew point absent at decision time"]
    assert result["fields"][DEW]["weights"] == {"GFS": 1.0}


def test_repeated_point_extraction_preserves_native_inputs_and_per_field_provenance(configuration):
    datasets, sources = _case()
    originals = {model: dataset.copy(deep=True) for model, (dataset, _, _) in datasets.items()}
    original_sources = deepcopy(sources)
    for model, (dataset, crs, _) in tuple(datasets.items()):
        source = next(row for row in sources if row["model"] == model)
        manifest = {
            "inputs": [
                {
                    "model": model,
                    "cycle": source["cycle"],
                    "source_lead_hours": source["source_lead_hours"],
                    "valid_time": "2026-09-11T13:00:00Z",
                    "source_grib_url": f"https://provider.invalid/{model}.grib2",
                    "extra_messages": [
                        {
                            "canonical_variable_id": variable,
                            "raw_sha256": str(index) * 64,
                            "raw_file": f"raw/{model}-{variable}.grib2",
                            "byte_start": index * 100,
                            "byte_end": (index + 1) * 100,
                        }
                        for index, variable in enumerate((DEW, U, V, GUST), start=1)
                    ],
                }
            ],
            "prepared_files": {model: {"sha256": "a" * 64}},
        }
        datasets[model] = dataset, crs, manifest
    first = _extract(configuration, datasets, sources)
    second = _extract(configuration, datasets, sources)
    assert first == second
    assert sources == original_sources
    for model, (dataset, _, manifest) in datasets.items():
        xr.testing.assert_identical(dataset, originals[model])
        evidence = first["contributors"][model]["fields"][DEW]["provenance"]
        assert evidence["raw_sha256"] == "1" * 64
        assert evidence["prepared_sha256"] == "a" * 64
        assert evidence["source_lead_hours"] == manifest["inputs"][0]["source_lead_hours"]
        assert evidence["valid_time"] == "2026-09-11T13:00:00Z"
        assert evidence["wind_reference"] == "earth_relative"


@pytest.mark.parametrize(
    "variable,unit,reference,reason",
    [
        (DEW, "degC", "earth_relative", "invalid canonical units"),
        (U, "mph", "earth_relative", "invalid canonical units"),
        (U, "m/s", "grid_relative", "earth-relative"),
    ],
)
def test_point_loader_rejects_invalid_surface_units_or_unrotated_winds(
    tmp_path, variable, unit, reference, reason
):
    prepare_demo_files(tmp_path)
    path = tmp_path / "GFS.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        dataset = opened.load()
    dataset[variable] = xr.full_like(dataset[T], 1.0)
    dataset[variable].attrs = {"unit_id": unit, "units": unit}
    dataset.attrs["wind_reference"] = reference
    dataset.to_netcdf(path, engine="h5netcdf")
    with pytest.raises(ValueError, match=reason):
        PreparedPointForecast.from_directory(tmp_path)
