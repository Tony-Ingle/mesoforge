"""Focused native-interval QPF preparation checks using retained GRIB fixtures."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import prepared_temperature
from mesoforge.application.prepared_qpf import QPF_VARIABLE, prepare_qpf_run, required_qpf_leads
from mesoforge.guidance.precipitation import compute_bucket_start
from tests.fixtures import gfs_grib, hrrr_grib
from tests.unit.application.test_prepared_temperature import (
    GFS_CYCLE,
    TARGET,
    FixtureClock,
    FixtureSleeper,
    SurfaceFixtureTransport,
    normalize_temperature_messages,
    phase2_configuration,
    temperature_payload,
)


def _qpf(model: str, lead: int, amount: float, *, start: int | None = None) -> bytes:
    if model == "HRRR":
        return hrrr_grib.make_apcp_message(
            forecast_hour=lead,
            values_kg_m2=np.full((hrrr_grib.NY, hrrr_grib.NX), amount),
            cycle_date="20260830",
            cycle_hour=12,
        )
    return gfs_grib.make_apcp_message(
        start_step=compute_bucket_start(lead) if start is None else start,
        end_step=lead,
        values_kg_m2=np.full((gfs_grib.NY, gfs_grib.NX), amount),
        cycle_date="20260830",
        cycle_hour=6,
    )


def _normalize(model: str, payloads: dict[int, bytes | tuple[bytes, ...]]) -> xr.Dataset:
    settings = phase2_configuration()
    age = 0 if model == "HRRR" else 6
    return normalize_temperature_messages(
        model=model,
        settings=settings.hrrr if model == "HRRR" else settings.gfs,
        target_reference_time=TARGET,
        source_cycle=TARGET if model == "HRRR" else GFS_CYCLE,
        payloads_by_lead={hour + age: temperature_payload(model, hour) for hour in (1, 2, 3)},
        target_horizon_hours=(1, 2, 3),
        qpf_payloads=payloads,
    )


@pytest.mark.parametrize("model", ("HRRR", "GFS"))
def test_native_intervals_align_units_zero_small_amount_and_conserve(model: str) -> None:
    amounts = (0.0, 0.125, 0.375) if model == "HRRR" else (0.0, 0.125, 0.5)
    age = 0 if model == "HRRR" else 6
    result = _normalize(
        model, {hour + age: _qpf(model, hour + age, value) for hour, value in enumerate(amounts, 1)}
    )
    field = result[QPF_VARIABLE]
    assert field.attrs["units"] == "kg/m^2"
    assert field.attrs["temporal_semantics"] == "accumulation"
    assert field.attrs["interval_closure"] == "left_open_right_closed"
    bounds = result[field.attrs["interval_bounds"]].values
    expected_starts = np.array(
        ["2026-08-30T12", "2026-08-30T13", "2026-08-30T14"], dtype="datetime64[ns]"
    )
    np.testing.assert_array_equal(bounds[:, 0], expected_starts)
    np.testing.assert_array_equal(bounds[:, 1], expected_starts + np.timedelta64(1, "h"))
    for hour, expected in enumerate((0.0, 0.125, 0.375)):
        np.testing.assert_array_equal(field.values[hour], expected)
    np.testing.assert_array_equal(field.sum("source_lead_time"), 0.5)
    # 0.125 mm is about 0.0049 inches and must not be cleaned to zero.
    assert np.all(field.values[1] > 0)
    metadata = json.loads(result.attrs["qpf_metadata_json"])
    assert all(row["status"] == "available" for row in metadata.values())
    assert metadata[str(age + 3)]["parents"][0]["source_cycle"] == (
        TARGET if model == "HRRR" else GFS_CYCLE
    ).isoformat().replace("+00:00", "Z")
    if model == "GFS":
        assert metadata["7"]["reset"] is True
        assert metadata["8"]["reset"] is False
        assert [row["source_lead_hours"] for row in metadata["9"]["parents"]] == [9, 8]
        assert metadata["9"]["parents"][0]["interval_start"] == "2026-08-30T12:00:00Z"


def test_missing_and_incompatible_bucket_intervals_do_not_become_zero() -> None:
    missing = _normalize("GFS", {7: _qpf("GFS", 7, 0), 9: _qpf("GFS", 9, 0.5)})
    np.testing.assert_array_equal(missing[QPF_VARIABLE][0], 0)
    assert np.isnan(missing[QPF_VARIABLE][1:]).all()
    reasons = json.loads(missing.attrs["field_missing_reasons_json"])[QPF_VARIABLE]
    assert "parent at source lead 8 is missing" in reasons["9"][0]
    wrong_bucket = _normalize(
        "GFS", {7: _qpf("GFS", 7, 0), 8: _qpf("GFS", 8, 0.125, start=0), 9: _qpf("GFS", 9, 0.5)}
    )
    assert np.isnan(wrong_bucket[QPF_VARIABLE][1:]).all()
    assert "startStep=6" in json.loads(wrong_bucket.attrs["qpf_metadata_json"])["8"]["reason"]


def test_gfs_requires_parent_before_first_requested_lead() -> None:
    assert required_qpf_leads("GFS", (8, 9, 10)) == (7, 8, 9, 10)
    assert required_qpf_leads("GFS", (7, 8, 9)) == (7, 8, 9)
    assert required_qpf_leads("HRRR", (8, 9, 10)) == (8, 9, 10)
    # Force a native bucket dependency on a source lead preceding the selected window.
    config = phase2_configuration()

    actual = normalize_temperature_messages(
        model="GFS",
        settings=config.gfs,
        target_reference_time=TARGET + timedelta(hours=1),
        source_cycle=GFS_CYCLE,
        payloads_by_lead={lead: temperature_payload("GFS", lead - 6) for lead in (8, 9, 10)},
        target_horizon_hours=(1, 2, 3),
        qpf_payloads={
            lead: _qpf("GFS", lead, value)
            for lead, value in ((7, 0.125), (8, 0.375), (9, 0.875), (10, 1.0))
        },
    )
    for index, expected in enumerate((0.25, 0.5, 0.125)):
        np.testing.assert_array_equal(actual[QPF_VARIABLE][index], expected)
    assert [
        row["source_lead_hours"]
        for row in json.loads(actual.attrs["qpf_metadata_json"])["8"]["parents"]
    ] == [8, 7]


def test_same_shape_shifted_qpf_grid_is_explicitly_unavailable() -> None:
    import eccodes

    payload = _qpf("GFS", 7, 1.0)
    handle = eccodes.codes_new_from_message(payload)
    try:
        for key in ("longitudeOfFirstGridPointInDegrees", "longitudeOfLastGridPointInDegrees"):
            eccodes.codes_set(handle, key, eccodes.codes_get(handle, key) + 0.25)
        shifted = bytes(eccodes.codes_get_message(handle))
    finally:
        eccodes.codes_release(handle)
    result = _normalize("GFS", {7: shifted})
    assert np.isnan(result[QPF_VARIABLE]).all()
    assert (
        "native cells/projection differ"
        in json.loads(result.attrs["qpf_metadata_json"])["7"]["reason"]
    )


class QpfFixtureTransport(SurfaceFixtureTransport):
    def __init__(self, horizons: tuple[int, ...] = (1, 2, 3)) -> None:
        super().__init__(horizons)
        for (model, hour), (full, index) in list(self.products.items()):
            lead = hour if model == "HRRR" else hour + 6
            start = lead - 1 if model == "HRRR" else compute_bucket_start(lead)
            increments = (0.0, 0.125, 0.375)
            amount = (
                increments[(hour - 1) % 3]
                if model == "HRRR"
                else sum(
                    increments[(h - 1) % 3] for h in range(hour - (lead - start) + 1, hour + 1)
                )
            )
            date = "2026083012" if model == "HRRR" else "2026083006"
            index += f"6:{len(full)}:d={date}:APCP:surface:{start}-{lead} hour acc fcst:\n".encode()
            full += _qpf(model, lead, amount)
            self.products[model, hour] = full, index


def test_enrichment_retains_original_surface_provenance_and_rebuilds_without_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, enriched, replay = (tmp_path / name for name in ("source", "enriched", "replay"))
    config, clock, sleeper = phase2_configuration(), FixtureClock(), FixtureSleeper()
    original = prepared_temperature.prepare_temperature_guidance(
        source,
        configuration=config,
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=QpfFixtureTransport(),
        clock=clock,
        sleeper=sleeper,
        target_horizon_hours=(1, 2, 3),
        surface_fields=True,
    )
    source_bytes = (source / "manifest.json").read_bytes()
    transport = QpfFixtureTransport()
    result = prepared_temperature.enrich_prepared_qpf(
        source, enriched, configuration=config, transport=transport, clock=clock, sleeper=sleeper
    )
    assert (source / "manifest.json").read_bytes() == source_bytes
    assert result["inputs"] == original["inputs"]
    assert len(result["qpf_inputs"]) == 6
    assert result["qpf_acquisition"]["recorded_at"] == "2026-09-01T12:00:00Z"
    assert len([call for call in transport.calls if call[2] is not None]) == 6
    for row in result["qpf_inputs"]:
        assert row["cycle"] in ("2026-08-30T12:00:00Z", "2026-08-30T06:00:00Z")
        assert row["grib_retrieved_at"] != row["grib_available_at"]
        for message in row["messages"]:
            raw = (enriched / message["raw_file"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == message["raw_sha256"]
    monkeypatch.setattr(
        prepared_temperature,
        "_acquire_qpf_model",
        lambda *args, **kwargs: pytest.fail("Offline rebuild attempted acquisition"),
    )
    repeated = prepared_temperature.rebuild_temperature_guidance(
        enriched, replay, configuration=config, clock=clock
    )
    assert repeated["downloaded_bytes"] == 0
    assert repeated["qpf_inputs"] == result["qpf_inputs"]
    for model in ("HRRR", "GFS"):
        with xr.open_dataset(source / f"{model}.nc", engine="h5netcdf") as old:
            with xr.open_dataset(enriched / f"{model}.nc", engine="h5netcdf") as new:
                for variable in old.data_vars:
                    xr.testing.assert_identical(old[variable], new[variable])
                with xr.open_dataset(replay / f"{model}.nc", engine="h5netcdf") as rebuilt:
                    xr.testing.assert_identical(new, rebuilt)
    damaged = result["qpf_inputs"][0]["messages"][0]
    (enriched / damaged["raw_file"]).write_bytes(b"corrupt fixture")
    with pytest.raises(ValueError, match="QPF checksum"):
        prepared_temperature.rebuild_temperature_guidance(
            enriched, tmp_path / "bad", configuration=config, clock=clock
        )
    assert not (tmp_path / "bad").exists()


def test_negative_native_parent_cannot_produce_a_positive_hourly_amount() -> None:
    actual = _normalize(
        "GFS", {lead: _qpf("GFS", lead, value) for lead, value in ((7, -2), (8, -1), (9, 1))}
    )
    assert np.isnan(actual[QPF_VARIABLE]).all()
    metadata = json.loads(actual.attrs["qpf_metadata_json"])
    assert all(row["missing_cell_count"] == gfs_grib.NY * gfs_grib.NX for row in metadata.values())
    assert (
        metadata["9"]["parents"][1]["invalid_negative_parent_cell_count"]
        == gfs_grib.NY * gfs_grib.NX
    )
    assert json.loads(actual.attrs["field_missing_reasons_json"])[QPF_VARIABLE] == {}


def test_invalid_native_cell_does_not_disable_other_cells_in_the_prepared_region() -> None:
    previous = np.zeros((gfs_grib.NY, gfs_grib.NX))
    previous[0, 0] = -2
    previous[1, 1] = 2
    current = np.ones(previous.shape)
    current[0, 0] = -1
    payloads = {
        lead: gfs_grib.make_apcp_message(
            start_step=6, end_step=lead, values_kg_m2=values, cycle_date="20260830", cycle_hour=6
        )
        for lead, values in ((7, previous), (8, current))
    }
    actual = _normalize("GFS", payloads)
    expected = np.ones(previous.shape)
    expected[0, 0] = expected[1, 1] = np.nan
    np.testing.assert_array_equal(actual[QPF_VARIABLE][1], expected)
    assert "8" not in json.loads(actual.attrs["field_missing_reasons_json"])[QPF_VARIABLE]
    metadata = json.loads(actual.attrs["qpf_metadata_json"])["8"]
    assert metadata["missing_cell_count"] == 2
    assert metadata["invalid_nonmonotonic_difference_cell_count"] == 1


def test_single_bucket_bitmap_missing_cell_does_not_trigger_duplicate_ambiguity() -> None:
    import eccodes

    handle = eccodes.codes_new_from_message(_qpf("GFS", 7, 1.0))
    try:
        eccodes.codes_set(handle, "bitmapPresent", 1)
        eccodes.codes_set(handle, "missingValue", 9999)
        values = np.ones((gfs_grib.NY, gfs_grib.NX))
        values[0, 0] = 9999
        eccodes.codes_set_array(handle, "values", values.ravel())
        bitmap_payload = bytes(eccodes.codes_get_message(handle))
    finally:
        eccodes.codes_release(handle)
    actual = _normalize("GFS", {7: bitmap_payload, 8: _qpf("GFS", 8, 2), 9: _qpf("GFS", 9, 3)})
    expected = np.ones(values.shape)
    expected[0, 0] = np.nan
    for index in (0, 1):
        np.testing.assert_array_equal(actual[QPF_VARIABLE][index], expected)
    np.testing.assert_array_equal(actual[QPF_VARIABLE][2], 1)
    assert json.loads(actual.attrs["field_missing_reasons_json"])[QPF_VARIABLE] == {}
    parent = json.loads(actual.attrs["qpf_metadata_json"])["7"]["parents"][0]
    assert parent["candidate_count"] == 1
    assert parent["duplicate_candidates_equivalent"] is None


@pytest.mark.parametrize("divergent", (False, True))
def test_early_gfs_dual_candidate_equivalence_is_checked_and_recorded(divergent: bool) -> None:
    payloads = {
        lead: (
            _qpf("GFS", lead, amount),
            _qpf("GFS", lead, amount + (0.125 if divergent and lead == 2 else 0)),
        )
        for lead, amount in ((1, 0.125), (2, 0.5), (3, 0.875))
    }
    actual = normalize_temperature_messages(
        model="GFS",
        settings=phase2_configuration().gfs,
        target_reference_time=GFS_CYCLE,
        source_cycle=GFS_CYCLE,
        target_horizon_hours=(1, 2, 3),
        payloads_by_lead={
            lead: gfs_grib.make_instantaneous_message(
                canonical_variable_id="air_temperature_2m",
                forecast_hour=lead,
                values=np.full((gfs_grib.NY, gfs_grib.NX), 280.0),
                cycle_date="20260830",
                cycle_hour=6,
            )
            for lead in (1, 2, 3)
        },
        qpf_payloads=payloads,
    )
    metadata = json.loads(actual.attrs["qpf_metadata_json"])
    assert metadata["1"]["parents"][0]["candidate_count"] == 2
    assert metadata["1"]["parents"][0]["duplicate_candidates_equivalent"] is True
    if divergent:
        assert np.isnan(actual[QPF_VARIABLE][1:]).all()
        assert "divergent APCP candidates" in metadata["2"]["reason"]
    else:
        for index, expected in enumerate((0.125, 0.375, 0.375)):
            np.testing.assert_array_equal(actual[QPF_VARIABLE][index], expected)


def test_qpf_current_preparation_and_cli_offline_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, prepared_run = tmp_path / "source", tmp_path / "selected"
    config = phase2_configuration()
    manifest = prepared_temperature.prepare_temperature_guidance(
        source,
        configuration=config,
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        transport=QpfFixtureTransport(),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
        target_horizon_hours=(1, 2, 3),
        surface_fields=True,
        qpf_fields=True,
    )
    assert manifest["qpf_fields"] is True
    assert len(manifest["qpf_inputs"]) == 6
    prepared_run.mkdir()
    metadata = {"selection": {"source_configuration": config.model_dump(mode="json")}}
    (prepared_run / "preparation.json").write_text(
        json.dumps(
            {"directory": str(source), "shadow_directories": {}, "current_model_set": metadata}
        )
    )
    monkeypatch.setattr(
        prepared_temperature,
        "BoundedHttpTransport",
        lambda *args, **kwargs: pytest.fail("Offline CLI opened network transport"),
    )
    result = prepare_qpf_run(prepared_run, tmp_path / "replayed", from_raw=True)
    assert result["downloaded_bytes"] == 0
    assert result["current_model_set"] == metadata
    assert result == json.loads((tmp_path / "replayed/preparation.json").read_bytes())
    for model in ("HRRR", "GFS"):
        with xr.open_dataset(source / f"{model}.nc", engine="h5netcdf") as old:
            with xr.open_dataset(
                Path(result["directory"]) / f"{model}.nc", engine="h5netcdf"
            ) as new:
                xr.testing.assert_identical(old, new)
