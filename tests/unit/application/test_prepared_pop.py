"""Native NBM probability retention/replay, using existing provider/GRIB fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import prepared_pop
from mesoforge.application.prepared_pop import NATIVE_PERCENT, POP_VARIABLE, THRESHOLD
from mesoforge.catalog.domains import BoundingBox
from mesoforge.guidance.acquisition_v2 import acquire_nbm_lead
from tests.fixtures import nbm_grib
from tests.support.phase2_provider_transports import build_nbm_transport
from tests.support.phase2_source_settings import make_nbm_settings
from tests.unit.application.test_prepared_temperature import FixtureClock, FixtureSleeper

CYCLE = datetime(2026, 8, 30, 12, tzinfo=UTC)
HOURS = (1, 2, 3)


def _payload(lead: int, percent: float, **kwargs: object) -> bytes:
    return nbm_grib.make_pop01_message(
        forecast_hour=lead,
        values_percent=np.full((nbm_grib.NY, nbm_grib.NX), percent),
        **kwargs,  # type: ignore[arg-type]
    )


def _normalize(payloads: dict[int, bytes], *, target: datetime = CYCLE) -> xr.Dataset:
    return prepared_pop.normalize_pop_messages(
        settings=make_nbm_settings(),
        target_reference_time=target,
        source_cycle=CYCLE,
        payloads_by_lead=payloads,
        areas=(None,),
        target_horizon_hours=HOURS,
    )[0]


def _prepare(directory: Path, *, areas: tuple[BoundingBox, ...] = ()) -> dict:
    settings = make_nbm_settings()
    transport = build_nbm_transport(settings, cycle=CYCLE, leads=HOURS, base_value=273.0)
    acquired = [
        acquire_nbm_lead(
            settings,
            transport=transport,
            clock=FixtureClock(),
            sleeper=FixtureSleeper(),
            cycle_date=CYCLE.date(),
            cycle_hour=CYCLE.hour,
            forecast_hour=lead,
            cycle_deadline=CYCLE,
            canonical_variables=(POP_VARIABLE,),
        )
        for lead in HOURS
    ]
    return prepared_pop.prepare_pop_guidance(
        directory,
        settings=settings,
        target_reference_time=CYCLE,
        source_cycle=CYCLE,
        acquired_inputs=acquired,
        area=areas[0] if areas else None,
        areas=areas[1:],
        clock=FixtureClock(),
        target_horizon_hours=HOURS,
        selection_evidence={"object_validation": [{"fixture": "pinned native PoP"}]},
    )


def test_native_percent_event_and_intervals_remain_distinct_from_qpf() -> None:
    result = _normalize({i: _payload(i, amount) for i, amount in enumerate((0, 25, 100), 1)})
    for index, expected in enumerate((0, 0.25, 1)):
        np.testing.assert_array_equal(result[POP_VARIABLE][index], expected)
        np.testing.assert_array_equal(result[NATIVE_PERCENT][index], expected * 100)
    field = result[POP_VARIABLE]
    assert field.attrs["temporal_semantics"] == "probability"
    assert field.attrs["unit_id"] == "1"
    assert field.attrs["probability_comparison"] == "gt"
    assert field.attrs["probability_threshold_kg_m2"] == 0.254
    bounds = result[field.attrs["interval_bounds"]].values
    starts = np.array(["2026-08-30T12", "2026-08-30T13", "2026-08-30T14"], dtype="datetime64[ns]")
    np.testing.assert_array_equal(bounds[:, 0], starts)
    np.testing.assert_array_equal(bounds[:, 1], starts + np.timedelta64(1, "h"))
    metadata = json.loads(result.attrs["pop_metadata_json"])
    assert metadata["1"]["threshold"] == THRESHOLD
    assert metadata["1"]["grib_event_keys"]["probabilityType"] == 1
    assert metadata["1"]["grib_event_keys"]["scaledValueOfUpperLimit"] == 254


def test_source_age_aligns_native_intervals_by_valid_time() -> None:
    result = _normalize({i: _payload(i, 25) for i in (3, 4, 5)}, target=CYCLE + timedelta(hours=2))
    np.testing.assert_array_equal(
        result.source_lead_time, np.array((3, 4, 5), dtype="timedelta64[h]")
    )
    assert result.source_valid_time.values[0] == np.datetime64("2026-08-30T15")
    assert result[f"{POP_VARIABLE}_interval_bounds"].values[0, 0] == np.datetime64("2026-08-30T14")


def test_missing_and_non_probability_guidance_are_not_zero_or_reinterpreted() -> None:
    qpf = nbm_grib.make_apcp_deterministic_message(
        forecast_hour=3, values_kg_m2=np.ones((nbm_grib.NY, nbm_grib.NX))
    )
    result = _normalize({1: _payload(1, 0), 3: qpf})
    np.testing.assert_array_equal(result[POP_VARIABLE][0], 0)
    assert np.isnan(result[POP_VARIABLE][1:]).all()
    reasons = json.loads(result.attrs["field_missing_reasons_json"])[POP_VARIABLE]
    assert "no retained native one-hour PoP" in reasons["2"][0]
    assert "3" in reasons


@pytest.mark.parametrize("mutation,value", [("scaledValueOfUpperLimit", 255), ("startStep", 0)])
def test_wrong_threshold_or_longer_interval_is_explicitly_unavailable(
    mutation: str, value: int
) -> None:
    import eccodes

    handle = eccodes.codes_new_from_message(_payload(2, 50))
    try:
        eccodes.codes_set(handle, mutation, value)
        invalid = bytes(eccodes.codes_get_message(handle))
    finally:
        eccodes.codes_release(handle)
    result = _normalize({1: _payload(1, 25), 2: invalid, 3: _payload(3, 25)})
    assert np.isnan(result[POP_VARIABLE][1]).all()
    np.testing.assert_array_equal(result[POP_VARIABLE][0], 0.25)
    assert json.loads(result.attrs["field_missing_reasons_json"])[POP_VARIABLE]["2"]


def test_bitmap_and_invalid_percent_remain_cell_local() -> None:
    import eccodes

    handle = eccodes.codes_new_from_message(_payload(1, 25))
    try:
        values = np.full(nbm_grib.NY * nbm_grib.NX, 25.0)
        values[0], values[1] = 9999, 101
        eccodes.codes_set(handle, "bitmapPresent", 1)
        eccodes.codes_set(handle, "missingValue", 9999)
        eccodes.codes_set_array(handle, "values", values)
        payload = bytes(eccodes.codes_get_message(handle))
    finally:
        eccodes.codes_release(handle)
    result = _normalize({1: payload, 2: _payload(2, 25), 3: _payload(3, 25)})
    assert np.isnan(result[POP_VARIABLE].values[0].ravel()[:2]).all()
    np.testing.assert_array_equal(result[POP_VARIABLE].values[0].ravel()[2:], 0.25)
    assert result[NATIVE_PERCENT].values[0].ravel()[1] == 101
    assert "1" not in json.loads(result.attrs["field_missing_reasons_json"])[POP_VARIABLE]
    metadata = json.loads(result.attrs["pop_metadata_json"])["1"]
    assert metadata["invalid_percent_cell_count"] == 1
    assert metadata["missing_cell_count"] == 2


def test_retained_native_inputs_provenance_exact_offline_replay_and_integrity(
    tmp_path: Path,
) -> None:
    descriptor = _prepare(tmp_path / "source")
    original = prepared_pop.load_pop_guidance(descriptor, CYCLE)[0]
    manifest = original[2]
    assert manifest["manifest_sha256"] == descriptor["manifest_sha256"]
    assert manifest["selection_evidence"]["object_validation"]
    assert len(manifest["inputs"]) == 3
    assert all(row["source_grib_url"] and row["grib_retrieved_at"] for row in manifest["inputs"])
    # Replay re-decodes raw messages; a damaged old prepared file is irrelevant.
    (Path(descriptor["directory"]) / "NBM.nc").write_bytes(b"not used by raw replay")
    replay = prepared_pop.rebuild_pop_guidance(Path(descriptor["directory"]), tmp_path / "replay")
    rebuilt = prepared_pop.load_pop_guidance(replay, np.datetime64("2026-08-30T12"))[0]
    xr.testing.assert_identical(original[0], rebuilt[0])
    assert rebuilt[2]["inputs"] == manifest["inputs"]
    assert rebuilt[2]["downloaded_bytes"] == 0
    assert rebuilt[2]["source_metadata"] == manifest["source_metadata"]
    raw = tmp_path / "replay" / rebuilt[2]["inputs"][0]["raw_file"]
    raw.write_bytes(raw.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="checksum"):
        prepared_pop.load_pop_guidance(replay, CYCLE)


def test_multiple_views_share_one_raw_input_set_and_decode_once(
    tmp_path: Path, monkeypatch
) -> None:
    areas = (
        BoundingBox(south=45.7, north=45.8, west=-93.2, east=-93.1),
        BoundingBox(south=45.8, north=45.9, west=-93.1, east=-93.0),
    )
    decode = prepared_pop.decode_selected_message
    calls = []

    def observed(*args, **kwargs):
        calls.append(kwargs["forecast_hour"])
        return decode(*args, **kwargs)

    monkeypatch.setattr(prepared_pop, "decode_selected_message", observed)
    descriptor = _prepare(tmp_path / "source", areas=areas)
    views = prepared_pop.load_pop_guidance(descriptor, CYCLE)
    assert len(views) == 2
    assert calls == [1, 2, 3]
    assert len(list((tmp_path / "source" / "raw").glob("*.grib2"))) == 3
    assert views[0][2]["inputs"] == views[1][2]["inputs"]
    assert views[0][2]["manifest_sha256"] == views[1][2]["manifest_sha256"]


def test_no_supported_coordinates_and_unavailable_descriptor_need_no_transport(
    tmp_path: Path,
) -> None:
    preparation = {
        "current_model_set": {
            "selection": {
                "source_configuration": {"nbm": make_nbm_settings().model_dump(mode="json")},
                "surface_fields": True,
                "target_reference_time": CYCLE.isoformat(),
                "horizon_hours": list(HOURS),
            }
        }
    }
    result = prepared_pop.prepare_pop_attachment(
        preparation, tmp_path / "unused", locations=[{"lat": 100, "lon": 0}, {"lat": 0, "lon": 0}]
    )
    assert result["status"] == "unavailable"
    assert result["downloaded_bytes"] == 0
    assert not (tmp_path / "unused").exists()
    assert prepared_pop.load_pop_guidance(result, CYCLE) == []


def test_attachment_reuses_covering_retained_guidance_without_provider_calls(
    tmp_path: Path,
) -> None:
    descriptor = _prepare(tmp_path / "NBM")
    source = tmp_path / "control"
    source.mkdir()
    area = BoundingBox(south=45.7, north=45.8, west=-93.2, east=-93.1)
    (source / "manifest.json").write_text(json.dumps({"prepared_area": area.model_dump()}))
    preparation = {
        "directory": str(source),
        "pop_guidance": descriptor,
        "current_model_set": {
            "selection": {
                "surface_fields": True,
                "source_configuration": {"nbm": make_nbm_settings().model_dump(mode="json")},
                "target_reference_time": CYCLE.isoformat(),
                "horizon_hours": list(HOURS),
            }
        },
    }
    result = prepared_pop.prepare_pop_attachment(preparation, tmp_path / "unused")
    assert result["reused"] is True
    assert result["directory"] == descriptor["directory"]
    assert result["downloaded_bytes"] == 0
    assert not (tmp_path / "unused").exists()


def test_wrapper_offline_rebuild_preserves_surface_references_and_counts_once(
    tmp_path: Path,
) -> None:
    descriptor = _prepare(tmp_path / "NBM")
    # Only raw source files and the metadata are necessary for rebuilding.
    (tmp_path / "NBM" / "NBM.nc").unlink()
    original = {
        "directory": str(tmp_path / "unchanged-control"),
        "shadow_directories": {"RAP": "unchanged-RAP", "IFS": "unchanged-IFS"},
        "current_model_set": {
            "selection": {
                "surface_fields": True,
                "target_reference_time": CYCLE.isoformat().replace("+00:00", "Z"),
            }
        },
        "retained_raw_bytes": 123456 + descriptor["retained_raw_bytes"],
        "pop_bytes_in_totals": {"retained_raw_bytes": descriptor["retained_raw_bytes"]},
        "pop_guidance": descriptor,
    }
    source = tmp_path / "source"
    source.mkdir()
    original_bytes = json.dumps(original).encode()
    (source / "preparation.json").write_bytes(original_bytes)
    result = prepared_pop.prepare_pop_run(source, tmp_path / "replayed-run", from_raw=True)
    for key in ("directory", "shadow_directories", "current_model_set", "retained_raw_bytes"):
        assert result[key] == original[key]
    assert result["downloaded_bytes"] == 0
    assert (source / "preparation.json").read_bytes() == original_bytes
    assert prepared_pop.load_pop_guidance(result["pop_guidance"], CYCLE)
    assert not (tmp_path / "replayed-run" / "unchanged-control").exists()


def test_temperature_only_attachment_is_rejected_before_provider_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="surface forecast"):
        prepared_pop.prepare_pop_attachment(
            {"current_model_set": {"selection": {"surface_fields": False}}}, tmp_path / "unused"
        )
    assert not (tmp_path / "unused").exists()
