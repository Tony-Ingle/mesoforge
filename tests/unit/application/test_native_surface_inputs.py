"""Native-time extraction remains separate from forecast influence and intervals."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pyproj
import pytest
import xarray as xr

from mesoforge.application.native_surface_inputs import (
    QPF,
    STATE_UNITS,
    extract_native_inputs,
    extract_native_qpf,
    extract_native_state,
    qpf_partition,
)
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.common.qpf_intervals import validate_qpf_intervals
from mesoforge.forecasting.conditions import _component, build_conditions_preview
from mesoforge.forecasting.field_blend import FieldBlendEngine
from mesoforge.forecasting.provisional_policy import PROVISIONAL_MULTIMODEL_POLICY
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, PROVISIONAL_CONFIGURATION
from tests.unit.forecasting.test_surface import configuration as configuration

REFERENCE = np.datetime64("2026-10-08T00:00:00", "ns")
T = "air_temperature_2m"


def native_entry(model="GFS", leads=(0, 3, 6), amounts=(0.0, 2.0, 4.0)):
    times = REFERENCE + np.array(leads, dtype="timedelta64[h]")
    fields = {}
    for name, unit in STATE_UNITS.items():
        base = {
            T: 280.0,
            "dew_point_temperature_2m": 275.0,
            "eastward_wind_10m": 2.0,
            "northward_wind_10m": 1.0,
            "wind_gust_10m": 5.0,
            "cloud_area_fraction": 0.5,
        }[name]
        values = np.full((len(leads), 2, 2), base)
        if name == T:
            values += np.array(leads)[:, None, None]
        fields[name] = (("source_lead_time", "y", "x"), values, {"unit_id": unit})
    fields[QPF] = (
        ("source_lead_time", "y", "x"),
        np.broadcast_to(np.array(amounts)[:, None, None], (len(leads), 2, 2)).copy(),
        {
            "unit_id": "kg/m^2",
            "temporal_semantics": "accumulation",
            "interval_closure": "left_open_right_closed",
        },
    )
    bounds = np.array([(times[max(index - 1, 0)], end) for index, end in enumerate(times)])
    fields[f"{QPF}_interval_bounds"] = (("source_lead_time", "bounds"), bounds)
    ds = xr.Dataset(
        fields,
        coords={
            "x": [-94.0, -92.0],
            "y": [45.0, 47.0],
            "forecast_reference_time": REFERENCE,
            "source_lead_time": np.array(leads, dtype="timedelta64[h]"),
            "source_valid_time": ("source_lead_time", times),
        },
        attrs={"model": model, "data_kind": "synthetic_demonstration"},
    )
    return ds, pyproj.CRS.from_epsg(4326), None


def test_adjacent_native_state_interpolates_but_never_gust_or_absent_endpoint():
    entry = native_entry("IFS")
    value = extract_native_state(
        "IFS",
        T,
        entry,
        valid_time=REFERENCE + np.timedelta64(1, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert value["value"] == 281.0
    assert value["temporal_alignment"]["mode"] == "interpolated"
    assert len(value["native_valid_times"]) == 2
    gust = extract_native_state(
        "IFS",
        "wind_gust_10m",
        entry,
        valid_time=REFERENCE + np.timedelta64(3, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert gust["value"] is None
    broken = (entry[0].isel(source_lead_time=[0, 2]), entry[1], None)
    assert (
        extract_native_state(
            "IFS",
            T,
            broken,
            valid_time=REFERENCE + np.timedelta64(1, "h"),
            latitude=46.0,
            longitude=-93.0,
        )["value"]
        is None
    )


@pytest.mark.parametrize("leads,target,expected", [((48, 50), 49, 283.0), ((125, 128), 126, 282.0)])
def test_nbm_utc_aligned_state_extraction_supports_hourly_cycle_phase(leads, target, expected):
    dataset, crs, _ = native_entry("NBM", leads=leads, amounts=(0, 0))
    cycle = REFERENCE + np.timedelta64(13, "h")
    dataset = dataset.assign_coords(
        forecast_reference_time=cycle,
        source_valid_time=("source_lead_time", cycle + np.array(leads, dtype="timedelta64[h]")),
    )
    dataset[T].values[0] = 280.0
    dataset[T].values[1] = 286.0
    result = extract_native_state(
        "NBM",
        T,
        (dataset, crs, None),
        valid_time=cycle + np.timedelta64(target, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert result["value"] == expected
    assert result["temporal_alignment"]["mode"] == "interpolated"
    assert result["temporal_alignment"]["native_step_hours"] == leads[1] - leads[0]


def test_exact_qpf_composition_never_splits_and_preserves_zero():
    entry = native_entry("IFS", amounts=(0.0, 0.0, 4.0))
    args = dict(latitude=46.0, longitude=-93.0)
    assert (
        extract_native_qpf(
            "IFS", entry, start=REFERENCE, end=REFERENCE + np.timedelta64(3, "h"), **args
        )["value"]
        == 0.0
    )
    total = extract_native_qpf(
        "IFS", entry, start=REFERENCE, end=REFERENCE + np.timedelta64(6, "h"), **args
    )
    assert total["value"] == 4.0
    assert len(total["normalization"]["inputs"]) == 2
    assert (
        extract_native_qpf(
            "IFS", entry, start=REFERENCE, end=REFERENCE + np.timedelta64(1, "h"), **args
        )["value"]
        is None
    )


@pytest.mark.parametrize(
    "field,attrs",
    [
        (T, {"temporal_semantics": "maximum"}),
        ("cloud_area_fraction", {"cloud_definition": "low_cloud_cover"}),
        ("cloud_area_fraction", {"vertical_extent": "low_layer"}),
    ],
)
def test_contradictory_native_state_semantics_cannot_be_relabelled(field, attrs):
    entry = native_entry()
    entry[0][field].attrs.update(attrs)
    result = extract_native_state(
        "GFS",
        field,
        entry,
        valid_time=REFERENCE + np.timedelta64(3, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert result["value"] is None and result["missing_reasons"]


def test_declared_missing_state_and_qpf_remain_missing_despite_numeric_array():
    entry = native_entry()
    entry[0].attrs["field_missing_reasons_json"] = json.dumps(
        {T: {"3": ["native message missing"]}, QPF: {"3": ["native message missing"]}}
    )
    state = extract_native_state(
        "GFS",
        T,
        entry,
        valid_time=REFERENCE + np.timedelta64(3, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    qpf = extract_native_qpf(
        "GFS",
        entry,
        start=REFERENCE,
        end=REFERENCE + np.timedelta64(3, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert state["value"] is None and qpf["value"] is None
    assert "native message missing" in str(qpf["missing_reasons"])
    entry[0].attrs.pop("field_missing_reasons_json")
    entry[0].source_valid_time.values[1] += np.timedelta64(1, "h")
    contradictory = extract_native_qpf(
        "GFS",
        entry,
        start=REFERENCE,
        end=REFERENCE + np.timedelta64(3, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert contradictory["value"] is None
    assert "differs from accumulation end" in str(contradictory["missing_reasons"])


def retained_native_entry(model="NBM"):
    dataset, crs, _ = native_entry(model, leads=(0, 1, 2), amounts=(0, 1, 2))
    cycle = "2026-10-08T00:00:00Z"
    dataset.attrs["data_kind"] = "real_provider_data"
    dataset.attrs["wind_rotation_policy_json"] = json.dumps(
        {str(lead): "nbm_native_speed_direction_to_earth_relative_uv" for lead in (0, 1, 2)}
    )
    dataset.attrs["qpf_metadata_json"] = json.dumps(
        {
            str(lead): {
                "parents": [{"model": model, "source_cycle": cycle, "source_lead_hours": lead}]
            }
            for lead in (1, 2)
        }
    )
    manifest = {
        "prepared_files": {model: {"sha256": "a" * 64}},
        "inputs": [
            {
                "model": model,
                "cycle": cycle,
                "source_lead_hours": lead,
                "valid_time": f"2026-10-08T0{lead}:00:00Z",
                "raw_sha256": "b" * 64,
                "extra_messages": [
                    {"canonical_variable_id": field, "raw_sha256": digest * 64}
                    for field, digest in (
                        ("wind_speed_10m", "c"),
                        ("wind_from_direction_10m", "d"),
                        (QPF, "e"),
                    )
                ],
            }
            for lead in (0, 1, 2)
        ],
    }
    return dataset, crs, manifest


def test_native_nbm_vectors_retain_both_source_messages_and_conversion_identity():
    entry = retained_native_entry()
    result = extract_native_state(
        "NBM",
        "eastward_wind_10m",
        entry,
        valid_time=REFERENCE + np.timedelta64(1, "h"),
        latitude=46.0,
        longitude=-93.0,
    )
    assert result["value"] == 2.0
    refs = result["temporal_alignment"]["endpoints"][0]["evidence_refs"]
    assert refs["wind_speed_10m_raw_sha256"] == "c" * 64
    assert refs["wind_from_direction_10m_raw_sha256"] == "d" * 64
    assert refs["derivation"] == "nbm_native_speed_direction_to_earth_relative_uv"
    entry[2]["inputs"][1]["cycle"] = "2026-10-07T00:00:00Z"
    assert (
        extract_native_state(
            "NBM",
            "eastward_wind_10m",
            entry,
            valid_time=REFERENCE + np.timedelta64(1, "h"),
            latitude=46.0,
            longitude=-93.0,
        )["value"]
        is None
    )


@pytest.mark.parametrize("corruption", ["duplicate", "mixed_cycle", "wrong_valid_time"])
def test_retained_qpf_parent_lineage_is_exact_not_just_a_set_of_leads(corruption):
    entry = retained_native_entry("GFS")
    args = dict(
        start=REFERENCE, end=REFERENCE + np.timedelta64(1, "h"), latitude=46.0, longitude=-93.0
    )
    valid = extract_native_qpf("GFS", entry, **args)
    assert valid["value"] == 1.0
    assert valid["normalization"]["inputs"][0]["evidence_refs"]["raw_1"] == "e" * 64
    metadata = json.loads(entry[0].attrs["qpf_metadata_json"])
    if corruption == "duplicate":
        metadata["1"]["parents"] *= 2
    elif corruption == "mixed_cycle":
        metadata["1"]["parents"][0]["source_cycle"] = "2026-10-07T00:00:00Z"
    else:
        entry[2]["inputs"][1]["valid_time"] = "2026-10-08T02:00:00Z"
    entry[0].attrs["qpf_metadata_json"] = json.dumps(metadata)
    rejected = extract_native_qpf("GFS", entry, **args)
    assert rejected["value"] is None and rejected["missing_reasons"]


def test_partition_does_not_fragment_native_totals_with_sparse_nbm_hours():
    gfs = native_entry("GFS", leads=(0, 1, 2, 3, 6), amounts=(0, 1, 1, 1, 3))
    nbm = native_entry("NBM", leads=(0, 5, 6), amounts=(0, 1, 1))
    bounds = nbm[0][f"{QPF}_interval_bounds"].values
    bounds[1, 0] = REFERENCE + np.timedelta64(4, "h")
    result = qpf_partition({"GFS": gfs, "NBM": nbm}, reference=REFERENCE, duration_hours=6)
    assert [int((end - start) / np.timedelta64(1, "h")) for start, end in result] == [1, 1, 1, 3]


def test_native_inputs_dispatch_all_sources_without_overwriting_evidence(configuration):
    entries = {
        model: native_entry(model, leads=(0, 1, 2, 3), amounts=(0, 1, 1, 1))
        for model in ("HRRR", "RAP", "GFS", "NBM")
    }
    entries["IFS"] = native_entry("IFS")
    state, evidence, sources = extract_native_inputs(
        entries, reference=REFERENCE, horizon=3, latitude=46.0, longitude=-93.0
    )
    before = deepcopy(evidence)
    engine = FieldBlendEngine(
        contributors=DEFAULT_CONFIGURATION,
        phase2=configuration,
        policy_family=PROVISIONAL_MULTIMODEL_POLICY,
    )
    result = engine.surface_fields(state)
    assert set(result[T]["weights"]) == set(entries)
    assert evidence == before
    assert len(sources) == 5
    assert result[T]["value"] == pytest.approx(283.0)
    assert result["relative_humidity_2m"]["value"] is not None


def native_prepared_120(configuration, *, spatial_variation=False):
    """Retained native-cadence fixture, shared with downstream issuance tests.

    GFS is hourly through source+120; IFS remains three-hourly. NBM has
    independent hourly QPF through48, then exact six-hour accumulations at
    six-hour endpoints. These are synthetic values under the real contracts.
    """
    schedules = {
        "HRRR": tuple(range(49)),
        "RAP": tuple(range(22)),
        "GFS": (*range(121), 123, 126),
        "IFS": tuple(range(0, 127, 3)),
        "NBM": (*range(49), *range(51, 127, 3)),
    }
    entries = {
        model: native_entry(model, leads=leads, amounts=(0.0,) * len(leads))
        for model, leads in schedules.items()
    }
    for index, (model, (ds, _, _)) in enumerate(entries.items()):
        ds[T].values[:] = 280.0
        if model == "NBM":
            for row, lead in enumerate(schedules[model]):
                if lead > 48:
                    bounds = ds[f"{QPF}_interval_bounds"].values
                    bounds[row, 0] = (
                        bounds[row, 1] - np.timedelta64(6, "h")
                        if lead % 6 == 0
                        else np.datetime64("NaT", "ns")
                    )
        if spatial_variation:
            gradient = np.array([[0.0, 0.2], [0.3, 0.5]])
            ds[T].values[:] += index + gradient
            ds["dew_point_temperature_2m"].values[:] += index * 0.5 + gradient
            ds["eastward_wind_10m"].values[:] += gradient
            ds["northward_wind_10m"].values[:] += gradient * 0.5
            ds["cloud_area_fraction"].values[:] = 0.25 + gradient * 0.5
            for row, (start, end) in enumerate(ds[f"{QPF}_interval_bounds"].values):
                duration = (end - start) / np.timedelta64(1, "h")
                ds[QPF].values[row] = (
                    (0.1 + index * 0.01 + gradient * 0.02) * float(duration)
                    if np.isfinite(duration)
                    else np.nan
                )
    # Source cycles precede the forecast reference by six hours: GFS native
    # hourly coverage ends at forecast+114, followed by exact3h native events.
    reference = REFERENCE + np.timedelta64(6, "h")
    return PreparedPointForecast(
        {model: entry[0] for model, entry in entries.items()},
        reference,
        {model: entry[1] for model, entry in entries.items()},
        "synthetic_demonstration",
        {"forecast_horizon": FIVE_DAY_HORIZON.payload()},
        None,
        FIVE_DAY_HORIZON.leads,
        PROVISIONAL_CONFIGURATION,
        {},
        configuration,
    )


def test_full_native_column_120_hours_preserves_canonical_events_and_conditions(configuration):
    prepared = native_prepared_120(configuration)
    column = prepared.point_column(latitude=46.0, longitude=-93.0)
    assert len(column["hours"]) == 120
    assert all(hour["temperature"]["value"] == pytest.approx(280.0) for hour in column["hours"])
    assert column["qpf_intervals"][-1]["interval_end"] == "2026-10-13T06:00:00Z"
    last = column["hours"][-1]
    assert "HRRR" not in last["surface"]["fields"][T]["weights"]
    assert last["surface"]["fields"][QPF]["value"] is None
    assert (
        _component("temperature", last["surface"]["fields"][T], last, "/temperature")["state"]
        == "known"
    )
    assert (
        _component("sky", last["surface"]["fields"]["cloud_area_fraction"], last, "/sky")["state"]
        == "known"
    )


def test_native_49_cell_120_hour_canvas_preserves_evidence_and_exact_events(configuration):
    prepared = native_prepared_120(configuration, spatial_variation=True)
    forecast = prepared.forecast(latitude=46.0, longitude=-93.0)
    grid = forecast["local_grid_baseline"]
    assert len(grid["cells"]) == 49
    assert sum(cell["inside_editable_domain"] for cell in grid["cells"]) == 9
    center = next(cell for cell in grid["cells"] if cell["is_forecast_point"])
    assert forecast["hours"] == center["hours"]
    assert forecast["qpf_intervals"] == center["qpf_intervals"]
    reference = datetime.fromisoformat(forecast["target_reference_time"])
    for cell in grid["cells"]:
        assert cell["status"] == "calculated"
        FIVE_DAY_HORIZON.validate_hour_rows(cell["hours"], reference)
        events = validate_qpf_intervals(
            cell["qpf_intervals"], start=reference, end=reference + timedelta(hours=120)
        )
        assert len(events) == 115  # 114 exact hourly events followed by one six-hour event.
        assert all(event["value"] > 0 for event in events)
        assert set(events[-1]["weights"]) == {"GFS", "IFS", "NBM"}
        assert events[-1]["interval_start"] == "2026-10-13T00:00:00Z"
        for hour in cell["hours"]:
            fields = hour["surface"]["fields"]
            temperature = fields[T]
            assert temperature["policy_family"] == PROVISIONAL_MULTIMODEL_POLICY
            assert sum(temperature["weights"].values()) == pytest.approx(1.0)
            assert temperature["value"] >= fields["dew_point_temperature_2m"]["value"]
            assert 0.0 <= fields["relative_humidity_2m"]["value"] <= 100.0
            assert set(hour["surface"]["contributors"]) == {"HRRR", "RAP", "GFS", "IFS", "NBM"}
            for source in hour["sources"]:
                assert source["source_lead_hours"] == hour["horizon_hours"] + 6
            if hour["horizon_hours"] > 42:
                assert "HRRR" not in temperature["weights"]
            if hour["horizon_hours"] > 15:
                assert "RAP" not in temperature["weights"]
            if hour["horizon_hours"] > 114:
                assert fields[QPF]["value"] is None
                assert fields[QPF]["status"] == "unavailable"
    assert (
        grid["cells"][0]["hours"][0]["temperature"]["value"]
        != center["hours"][0]["temperature"]["value"]
    )
    saved = {
        "schema_version": "issued-forecast.v1",
        "issued_forecast_id": "a98ae7e6-8eec-4f1d-8f0e-b8f4c4632940",
        "issued_at": forecast["target_reference_time"],
        "forecast": forecast,
    }
    with patch.object(FieldBlendEngine, "blend_field", side_effect=AssertionError("read-only")):
        preview = build_conditions_preview(saved, scope="grid")
    assert len(preview["cells"]) == 49
    assert len(preview["center_point"]["hours"]) == 120
    assert all(
        hour["components"]["sky"]["state"] == "known"
        for cell in preview["cells"]
        for hour in cell["hours"]
    )
