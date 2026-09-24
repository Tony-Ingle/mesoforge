"""Point extraction and surface-field integration over prepared inputs only."""

from __future__ import annotations

import json
import math
from copy import deepcopy

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.point_forecast import PreparedPointForecast, prepare_demo_files
from mesoforge.application.probability_forecast import extract_probability_hour
from mesoforge.application.surface_forecast import FIELD_UNITS, extract_surface_inputs
from mesoforge.forecasting.field_blend import FieldBlendEngine
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION
from tests.unit.application.test_prepared_temperature import phase2_configuration

TARGET = np.datetime64("2026-09-11T12:00:00", "ns")
T, DEW = "air_temperature_2m", "dew_point_temperature_2m"
U, V, GUST = "eastward_wind_10m", "northward_wind_10m", "wind_gust_10m"
RH, SPEED, DIRECTION = "relative_humidity_2m", "wind_speed_10m", "wind_from_direction_10m"
QPF = "liquid_equivalent_precipitation_amount_1h"
POP = "probability_of_precipitation_1h"
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


def _add_qpf(dataset, amounts):
    """Add native-grid hourly depths and exact bounds to the existing surface fixture."""
    values = np.asarray(amounts, dtype=np.float64)
    if values.ndim == 0:
        values = np.full(dataset.sizes["source_lead_time"], values)
    shape = (dataset.sizes["source_lead_time"], dataset.sizes["y"], dataset.sizes["x"])
    dataset[QPF] = (
        ("source_lead_time", "y", "x"),
        np.broadcast_to(values[:, None, None], shape).copy(),
        {
            "unit_id": "kg/m^2",
            "temporal_semantics": "accumulation",
            "interval_closure": "left_open_right_closed",
        },
    )
    ends = dataset.source_valid_time.values.astype("datetime64[ns]")
    dataset[f"{QPF}_interval_bounds"] = (
        ("source_lead_time", "bounds"),
        np.column_stack((ends - np.timedelta64(1, "h"), ends)),
    )


def _pop_entry(*, hours=(1, 2, 19), age=3, fraction=0.4):
    dataset = _dataset("GFS", age=age, hours=hours)
    dataset = dataset.drop_vars(list(dataset.data_vars))
    values = np.full((len(hours), 2, 2), fraction, dtype=np.float64)
    dataset[POP] = (
        ("source_lead_time", "y", "x"),
        values,
        {
            "unit_id": "1",
            "units": "1",
            "temporal_semantics": "probability",
            "interval_closure": "left_open_right_closed",
            "probability_threshold_kg_m2": 0.254,
            "probability_comparison": "gt",
            "probability_type": 1,
        },
    )
    dataset["native_probability_percent"] = (
        ("source_lead_time", "y", "x"),
        values * 100,
    )
    ends = dataset.source_valid_time.values.astype("datetime64[ns]")
    dataset[f"{POP}_interval_bounds"] = (
        ("source_lead_time", "bounds"),
        np.column_stack((ends - np.timedelta64(1, "h"), ends)),
    )
    cycle = str(np.datetime_as_string(dataset.forecast_reference_time.values, unit="s")) + "Z"
    rows = [
        {
            "model": "NBM",
            "cycle": cycle,
            "source_lead_hours": horizon + age,
            "valid_time": str(
                np.datetime_as_string(TARGET + np.timedelta64(horizon, "h"), unit="s")
            )
            + "Z",
            "raw_sha256": "e" * 64,
            "index_sha256": "f" * 64,
            "source_grib_url": f"https://provider.invalid/nbm-f{horizon + age:03}.grib2",
        }
        for horizon in hours
    ]
    dataset.attrs["pop_metadata_json"] = json.dumps(
        {str(horizon + age): {"native_unit": "percent", "missing_reasons": []} for horizon in hours}
    )
    manifest = {
        "inputs": rows,
        "manifest_sha256": "a" * 64,
        "prepared_files": {"NBM": {"sha256": "b" * 64}},
    }
    return dataset, CRS, manifest


def _extract_pop(configuration, entry, *, horizon=1):
    return extract_probability_hour(
        entry,
        latitude=44.5,
        longitude=-93.5,
        horizon=horizon,
        target_reference_time=TARGET,
        policy=configuration.pop_policy,
    )


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
    state, contributors = extract_surface_inputs(
        datasets=datasets,
        temperature_sources=sources,
        latitude=44.5,
        longitude=-93.5,
        horizon=horizon,
        target_reference_time=TARGET,
        selection=selection,
    )
    engine = FieldBlendEngine(contributors=DEFAULT_CONFIGURATION, phase2=configuration)
    return {
        "fields": engine.surface_fields(state),
        "source_validation": state.source_validation,
        "contributors": contributors,
    }


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
    assert fields[T]["value"] == 293.15  # Existing 70/30 temperature policy remains unchanged.
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


@pytest.mark.parametrize(
    "horizon,expected,weights",
    [
        (1, 2.9, {"HRRR": 0.7, "GFS": 0.3}),
        (19, 3.2, {"HRRR": 0.6, "GFS": 0.4}),
    ],
)
def test_qpf_aligns_actual_intervals_across_cycles_and_uses_approved_rows(
    configuration, horizon, expected, weights
):
    datasets, sources = _case(horizon)
    before = _extract(configuration, datasets, sources, horizon=horizon)
    for model, amount in (("HRRR", 2.0), ("GFS", 5.0)):
        _add_qpf(datasets[model][0], amount)
    result = _extract(configuration, datasets, sources, horizon=horizon)
    qpf = result["fields"][QPF]
    assert qpf["value"] == pytest.approx(expected)
    assert qpf["unit"] == "kg/m^2" and qpf["weights"] == weights
    assert qpf["policy"] == "phase2-qpf-fallback.v1"
    assert qpf["row_id"] == f"qpf.hg.{'h01-h18' if horizon <= 18 else 'h19-h36'}"
    assert qpf["row_sha256"]
    expected_end = TARGET + np.timedelta64(horizon, "h")
    expected_start = expected_end - np.timedelta64(1, "h")
    for field in (qpf, *(v["fields"][QPF] for v in result["contributors"].values())):
        assert field["interval_start"] == str(np.datetime_as_string(expected_start, unit="s")) + "Z"
        assert field["interval_end"] == str(np.datetime_as_string(expected_end, unit="s")) + "Z"
        assert field["interval_closure"] == "left_open_right_closed"
        assert field["temporal_semantics"] == "accumulation"
    native = result["contributors"]
    assert native["HRRR"]["fields"][QPF]["source_lead_hours"] == horizon
    assert native["GFS"]["fields"][QPF]["source_lead_hours"] == horizon + 6
    assert native["GFS"]["fields"][QPF]["source_cycle"] == "2026-09-11T06:00:00Z"
    assert {v: field for v, field in result["fields"].items() if v != QPF} == {
        v: field for v, field in before["fields"].items() if v != QPF
    }


def test_qpf_mismatched_and_missing_intervals_remain_explicit(configuration):
    datasets, sources = _case()
    for model in datasets:
        _add_qpf(datasets[model][0], 3.0)
    # Equal valid times do not make a three-hour total compatible with a one-hour depth.
    datasets["HRRR"][0][f"{QPF}_interval_bounds"].values[0, 0] -= np.timedelta64(2, "h")
    result = _extract(configuration, datasets, sources)
    assert result["fields"][QPF]["value"] == pytest.approx(3.0)
    assert result["fields"][QPF]["weights"] == {"GFS": 1.0}
    assert result["fields"][QPF]["status"] == "fallback"
    hrrr = result["contributors"]["HRRR"]["fields"][QPF]
    assert hrrr["value"] is None
    assert any("exact one-hour" in reason for reason in hrrr["missing_reasons"])
    datasets["GFS"][0][f"{QPF}_interval_bounds"].values[0, 1] += np.timedelta64(1, "h")
    missing = _extract(configuration, datasets, sources)["fields"][QPF]
    assert missing["value"] is None and missing["weights"] == {}
    assert missing["status"] == "unavailable" and missing["missing_reasons"]


@pytest.mark.parametrize("amount", [0.0, 0.254, 1e-12])
def test_qpf_zero_and_tiny_positive_amounts_are_not_missing_or_thresholded(configuration, amount):
    datasets, sources = _case()
    for model in datasets:
        _add_qpf(datasets[model][0], amount)
    result = _extract(configuration, datasets, sources)
    assert result["fields"][QPF]["value"] == pytest.approx(amount, rel=1e-12, abs=0)
    assert result["fields"][QPF]["missing_reasons"] == []
    for model in datasets:
        datasets[model][0][QPF].values[0, 0, 0] = np.nan
    missing = _extract(configuration, datasets, sources)["fields"][QPF]
    assert missing["value"] is None and missing["missing_reasons"]


def test_qpf_negative_native_corner_cannot_hide_behind_positive_interpolation(configuration):
    datasets, sources = _case()
    for model in datasets:
        _add_qpf(datasets[model][0], 3.0)
    datasets["HRRR"][0][QPF].values[0, 0, 0] = -1.0
    # At the center these four corners would average to +2 kg/m². The negative
    # native corner still invalidates this model's interpolated depth.
    result = _extract(configuration, datasets, sources)
    hrrr = result["contributors"]["HRRR"]["fields"][QPF]
    assert hrrr["value"] is None and hrrr["missing_reasons"]
    assert result["fields"][QPF]["value"] == 3.0
    assert result["fields"][QPF]["weights"] == {"GFS": 1.0}
    assert result["fields"][QPF]["status"] == "fallback"


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("unit_id", "inch"),
        ("temporal_semantics", "instantaneous"),
        ("interval_closure", "left_closed_right_open"),
    ],
)
def test_qpf_rejects_incompatible_units_or_temporal_semantics(configuration, attribute, value):
    datasets, sources = _case()
    for model in datasets:
        _add_qpf(datasets[model][0], 1.0)
    datasets["HRRR"][0][QPF].attrs[attribute] = value
    result = _extract(configuration, datasets, sources)
    assert result["contributors"]["HRRR"]["fields"][QPF]["value"] is None
    assert result["contributors"]["HRRR"]["fields"][QPF]["missing_reasons"]
    assert result["fields"][QPF]["weights"] == {"GFS": 1.0}


def test_qpf_temporal_rollups_conserve_hourly_depth_across_weight_change(configuration):
    hours = tuple(range(1, 37))
    datasets = {
        model: (_dataset(model, age=age, hours=hours), CRS, None)
        for model, age in (("HRRR", 0), ("GFS", 6))
    }
    _add_qpf(datasets["HRRR"][0], [hour / 10.0 for hour in hours])
    _add_qpf(datasets["GFS"][0], [hour / 5.0 for hour in hours])
    values = []
    previous_end = None
    for hour in hours:
        sources = [_temperature_source(model, row[0], hour) for model, row in datasets.items()]
        qpf = _extract(configuration, datasets, sources, horizon=hour)["fields"][QPF]
        if previous_end is not None:
            assert qpf["interval_start"] == previous_end
        previous_end = qpf["interval_end"]
        expected = hour * (0.13 if hour <= 18 else 0.14)
        assert qpf["value"] == pytest.approx(expected)
        values.append(qpf["value"])
    six_hour_totals = [math.fsum(values[start : start + 6]) for start in range(0, 36, 6)]
    expected_36h = 0.13 * sum(range(1, 19)) + 0.14 * sum(range(19, 37))
    assert math.fsum(values) == pytest.approx(expected_36h)
    assert math.fsum(six_hour_totals) == pytest.approx(expected_36h)
    assert math.fsum(values[15:21]) == pytest.approx(0.13 * (16 + 17 + 18) + 0.14 * (19 + 20 + 21))


def test_qpf_parent_provenance_survives_repeated_extraction_without_mutation(configuration):
    datasets, sources = _case(horizon=2)
    for model, (dataset, crs, _) in tuple(datasets.items()):
        _add_qpf(dataset, 1.0)
        lead = 2 if model == "HRRR" else 8
        parents = [{"model": model, "source_lead_hours": lead, "raw_sha256": "a" * 64}]
        if model == "GFS":
            parents.append({"model": model, "source_lead_hours": 7, "raw_sha256": "b" * 64})
        dataset.attrs["qpf_metadata_json"] = json.dumps({str(lead): {"parents": parents}})
        manifest = {
            "inputs": [{"model": model, "source_lead_hours": lead, "extra_messages": []}],
            "qpf_inputs": [
                {**parent, "messages": [{"raw_sha256": parent["raw_sha256"]}]} for parent in parents
            ],
            "prepared_files": {model: {"sha256": "c" * 64}},
        }
        datasets[model] = dataset, crs, manifest
    originals = {model: dataset.copy(deep=True) for model, (dataset, _, _) in datasets.items()}
    first = _extract(configuration, datasets, sources, horizon=2)
    assert first == _extract(configuration, datasets, sources, horizon=2)
    for model, (dataset, _, manifest) in datasets.items():
        field = first["contributors"][model]["fields"][QPF]
        assert field["value"] == 1.0
        assert (
            field["normalization"]
            == json.loads(dataset.attrs["qpf_metadata_json"])["2" if model == "HRRR" else "8"]
        )
        assert field["provenance"]["prepared_sha256"] == "c" * 64
        assert field["provenance"]["source_inputs"] == manifest["qpf_inputs"]
        xr.testing.assert_identical(dataset, originals[model])
    # Mutating presentation evidence must not change retained datasets/manifests.
    first["contributors"]["GFS"]["fields"][QPF]["provenance"]["source_inputs"][0][
        "messages"
    ].clear()
    assert datasets["GFS"][2]["qpf_inputs"][0]["messages"]
    datasets["GFS"][2]["qpf_inputs"] = []
    unavailable = _extract(configuration, datasets, sources, horizon=2)
    assert unavailable["contributors"]["GFS"]["fields"][QPF]["value"] is None
    assert unavailable["fields"][QPF]["weights"] == {"HRRR": 1.0}


def test_pop_matches_native_hour_event_and_cycle_then_preserves_passthrough_provenance(
    configuration,
):
    entry = _pop_entry()
    dataset, _, manifest = entry
    dataset[POP].values[0] = [[0.1, 0.3], [0.5, 0.7]]
    dataset.native_probability_percent.values[0] = [[10, 30], [50, 70]]
    original = dataset.copy(deep=True)
    original_manifest = deepcopy(manifest)
    baseline, native = _extract_pop(configuration, entry)
    assert baseline["value"] == pytest.approx((10 + 30 + 50 + 70) / 400)
    assert native["value"] == baseline["value"]
    assert baseline["weights"] == {"NBM": 1.0}
    assert baseline["policy"] == configuration.pop_policy.model_dump(mode="json")
    assert baseline["threshold"] == {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}
    assert baseline["interval_start"] == "2026-09-11T12:00:00Z"
    assert baseline["interval_end"] == "2026-09-11T13:00:00Z"
    assert baseline["source_cycle"] == "2026-09-11T09:00:00Z"
    assert baseline["source_lead_hours"] == 4
    assert native["spatial_extraction"]["native_percent_at_source_corners"] == [10, 30, 50, 70]
    assert native["provenance"]["source_inputs"] == [manifest["inputs"][0]]
    assert native["provenance"]["prepared_sha256"] == "b" * 64
    assert native["normalization"]["native_unit"] == "percent"
    assert (baseline, native) == _extract_pop(configuration, entry)
    baseline["provenance"]["source_inputs"][0]["raw_sha256"] = "changed"
    assert native["provenance"]["source_inputs"][0]["raw_sha256"] == "e" * 64
    assert manifest == original_manifest
    xr.testing.assert_identical(dataset, original)


@pytest.mark.parametrize("compatible", [True, False])
def test_pop_netcdf_event_attribute_scalars_are_safe_for_immutable_json(configuration, compatible):
    entry = _pop_entry()
    entry[0][POP].attrs.update(
        probability_threshold_kg_m2=np.float64(0.254),
        probability_type=np.int64(1 if compatible else 4),
    )
    baseline, native = _extract_pop(configuration, entry)
    assert (baseline["value"] is not None) is compatible
    assert json.loads(json.dumps(baseline, allow_nan=False)) == baseline
    assert json.loads(json.dumps(native, allow_nan=False)) == native
    assert type(native["source_event"]["probability_type"]) is int


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("probability_threshold_kg_m2", 2.54),
        ("probability_comparison", "ge"),
        ("probability_type", 4),
        ("unit_id", "percent"),
        ("units", "percent"),
        ("temporal_semantics", "accumulation"),
        ("interval_closure", "left_closed_right_open"),
    ],
)
def test_pop_rejects_different_threshold_comparator_units_or_semantics(
    configuration, attribute, value
):
    entry = _pop_entry()
    entry[0][POP].attrs[attribute] = value
    baseline, native = _extract_pop(configuration, entry)
    assert native["value"] is None and native["missing_reasons"]
    assert baseline["value"] is None and baseline["weights"] == {}


@pytest.mark.parametrize("corner", [-0.1, 1.1, np.nan])
def test_pop_invalid_native_corner_is_not_hidden_by_valid_interpolated_probability(
    configuration, corner
):
    entry = _pop_entry()
    entry[0][POP].values[0, 0, 0] = corner
    entry[0].native_probability_percent.values[0, 0, 0] = corner * 100
    baseline, native = _extract_pop(configuration, entry)
    assert baseline["value"] is None and baseline["weights"] == {}
    assert native["missing_reasons"] and "spatial_extraction" not in native


@pytest.mark.parametrize("fraction", [0.0, 1.0])
def test_pop_valid_zero_and_one_are_distinct_from_missing(configuration, fraction):
    baseline, native = _extract_pop(configuration, _pop_entry(fraction=fraction))
    assert baseline["value"] == native["value"] == fraction
    assert baseline["status"] == "available" and baseline["missing_reasons"] == []
    missing, _ = _extract_pop(configuration, None)
    assert missing["value"] is None and missing["status"] == "unavailable"
    assert missing["missing_reasons"]


def test_pop_never_splits_or_carries_a_different_native_probability_period(configuration):
    entry = _pop_entry()
    entry[0][f"{POP}_interval_bounds"].values[0, 0] -= np.timedelta64(5, "h")
    baseline, native = _extract_pop(configuration, entry)
    assert baseline["value"] is None and native["value"] is None
    assert any("exact native one-hour" in reason for reason in native["missing_reasons"])
    missing, _ = _extract_pop(configuration, entry, horizon=3)
    assert missing["value"] is None and missing["missing_reasons"]
    available, _ = _extract_pop(configuration, entry, horizon=2)
    assert available["value"] == 0.4


@pytest.mark.parametrize("failure", ["percent", "cycle", "lead", "valid_time", "missing_input"])
def test_pop_rejects_inconsistent_native_percent_or_source_identity(configuration, failure):
    entry = _pop_entry()
    dataset, _, manifest = entry
    if failure == "percent":
        dataset.native_probability_percent.values[0, 0, 0] = 99
    elif failure == "missing_input":
        manifest["inputs"].pop(0)
    else:
        key = "source_lead_hours" if failure == "lead" else failure
        manifest["inputs"][0][key] = 999 if failure == "lead" else "2026-09-11T10:00:00Z"
    baseline, native = _extract_pop(configuration, entry)
    assert baseline["value"] is None and native["missing_reasons"]
