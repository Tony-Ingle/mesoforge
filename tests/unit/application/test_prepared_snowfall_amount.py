"""Native new-snow intervals retain their parents and replay without provider access."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import prepared_snowfall_amount as prepared
from mesoforge.application.snowfall_amount_forecast import extract_snowfall_amount_contributors
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.http_fetch import FetchError
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.guidance.sources.snowfall_amount import SOURCES
from tests.unit.application.test_snowfall_amount_forecast import CYCLE, amount_view

CYCLE_TIME = datetime.fromisoformat(CYCLE)
TARGET = np.datetime64(CYCLE.removesuffix("Z"), "ns")


def native(model, lead, *, amount=None):
    valid = (CYCLE_TIME + timedelta(hours=lead)).isoformat().replace("+00:00", "Z")
    value = (0.002 if model == "NBM" else lead * 0.001) if amount is None else amount
    view = amount_view(model, value, valid=valid)
    dataset = view.dataset.isel(event=0, drop=True).rename(native_end_amount="native_amount")
    event = deepcopy(view.manifest["events"][0])
    event.update(
        **deepcopy(SOURCES[model]),
        unit="m",
        spatial_support="native_model_grid",
        version={"model_version": "fixture-v1"},
    )
    event.pop("normalization")
    if model != "NBM":
        event["interval_start"] = CYCLE
    else:
        event["interval_start"] = (
            (CYCLE_TIME + timedelta(hours=lead - 1)).isoformat().replace("+00:00", "Z")
        )
    return dataset, view.crs, event


@pytest.fixture
def source(tmp_path, monkeypatch):
    root, control = tmp_path / "original", tmp_path / "control"
    root.mkdir()
    control.mkdir()
    (control / "coverage.json").write_text(
        json.dumps(
            {
                "regions": [
                    {"area": {"south": 44.1, "north": 44.5, "west": -93.9, "east": -93.5}},
                    {"area": {"south": 44.5, "north": 44.9, "west": -93.5, "east": -93.1}},
                ]
            }
        )
    )
    original = {
        "directory": str(control),
        "shadow_directories": {},
        "snowfall_guidance": {"original": "retained SWE must remain unchanged"},
        "ptype_guidance": {"original": "must remain unchanged"},
        "pop_guidance": {"selected_cycle": CYCLE, "original": "must remain unchanged"},
        "current_model_set": {
            "selection": {
                "surface_fields": True,
                "target_reference_time": CYCLE,
                "selected_cycles": {model: CYCLE for model in ("HRRR", "GFS", "RAP", "IFS")},
            }
        },
    }
    (root / "preparation.json").write_text(json.dumps(original))
    calls = []

    def acquire(model, cycle, lead, *, include_profile, **kwargs):
        calls.append((model, lead, include_profile))
        fields = ["amount"]
        if include_profile:
            fields += ["temperature_2m", "surface_pressure"]
            fields += [f"temperature_{level}" for level in range(500, 1001, 25)]
        if model == "NBM":
            fields += ["native_slr"]
        messages, offset = [], 0
        for number, field in enumerate(fields, start=1):
            payload = f"{model}/{lead}/{field}".encode()
            messages.append(
                SelectedMessage(
                    field,
                    IndexRow(number, offset, f"fixture {field}"),
                    offset,
                    offset + len(payload),
                    payload,
                )
            )
            offset += len(payload)
        return Phase2LeadAcquisition(
            model=model,
            cycle_date=cycle.date(),
            cycle_hour=cycle.hour,
            forecast_hour=lead,
            endpoint="fixture",
            resolved_grib_url=f"https://example.invalid/{model}/{lead}",
            resolved_index_url=f"https://example.invalid/{model}/{lead}.idx",
            index_payload=b"fixture native amount/profile index",
            index_attempts=(),
            index_completed_at=cycle,
            selected_messages=tuple(messages),
            grib_attempts=(),
            grib_completed_at=cycle,
            full_object_etag="fixture",
            full_object_last_modified="Fri, 11 Sep 2026 12:00:00 GMT",
            full_object_content_length=offset,
            index_available_at=cycle,
            grib_available_at=cycle,
            index_last_modified=None,
        )

    def decode(payloads, model, cycle, lead):
        assert cycle == CYCLE_TIME
        assert all(value == f"{model}/{lead}/{key}".encode() for key, value in payloads.items())
        return native(model, lead)

    monkeypatch.setattr(prepared, "acquire_amount_lead", acquire)
    monkeypatch.setattr(prepared, "decode_amount_lead", decode)
    return root, original, calls


def build(root, output):
    return prepared.prepare_snowfall_amount_run(
        root, output, transport=SimpleNamespace(downloaded_bytes=0)
    )


def test_shared_regions_and_full_profile_provenance_replay_without_providers(
    tmp_path, monkeypatch, source
):
    root, original, calls = source
    before = (root / "preparation.json").read_bytes()
    first = build(root, tmp_path / "one")
    descriptor = first["snowfall_amount_guidance"]
    assert len(calls) == len(set(calls)) == 93  # HRRR36, RAP21, NBM36; no synthetic f0.
    assert all(lead >= 1 for _, lead, _ in calls)
    assert all(include == (model == "RAP") for model, _, include in calls)
    assert all(first[key] == value for key, value in original.items())
    assert (root / "preparation.json").read_bytes() == before
    assert {s["model"]: s["available_intervals"] for s in descriptor["sources"]} == {
        "HRRR": 36,
        "RAP": 21,
        "NBM": 36,
    }
    views = prepared.load_snowfall_amount_guidance(descriptor, target_reference_time=TARGET)
    assert len(views) == 6
    for lat, lon in [(44.2, -93.8), (44.8, -93.2)]:
        result = extract_snowfall_amount_contributors(
            views,
            swe_views=[],
            latitude=lat,
            longitude=lon,
            valid_time="2026-09-11T13:00:00Z",
            source_status=descriptor["source_status"],
        )
        rows = {row["model"]: row for row in result["native_contributors"]}
        assert rows["HRRR"]["value"] == pytest.approx(0.001, abs=1e-12)
        assert rows["RAP"]["value"] == pytest.approx(0.001, abs=1e-12)
        assert rows["NBM"]["value"] == pytest.approx(0.002, abs=1e-12)
        assert result["native_slr"][0]["value"] == pytest.approx(12, abs=1e-12)
        assert result["field"]["value"] is None and result["field"]["weights"] == {}
        assert rows["GFS"]["value"] is None and rows["IFS"]["value"] is None
    rap = next(view for view in views if view.manifest["model"] == "RAP")
    np.testing.assert_allclose(rap.dataset.amount.values[:21], 0.001, rtol=0, atol=1e-12)
    assert rap.dataset.temperature_profile.shape[1] == 21
    event = rap.manifest["events"][1]
    assert len(event["provenance"]["parents"]) == 2
    assert event["profile"]["provenance"] == event["provenance"]["parents"][-1]
    assert len(event["profile"]["provenance"]["messages"]) == 24
    assert event["profile"]["interval_start"] is None
    assert event["profile"]["interval_end"] is None

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline replay must not construct a transport or acquire guidance")

    monkeypatch.setattr(prepared, "BoundedHttpTransport", forbidden)
    monkeypatch.setattr(prepared, "acquire_amount_lead", forbidden)
    replay = prepared.prepare_snowfall_amount_run(tmp_path / "one", tmp_path / "two", from_raw=True)
    assert replay["snowfall_amount_guidance"]["downloaded_bytes"] == 0 and len(calls) == 93
    reloaded = prepared.load_snowfall_amount_guidance(
        replay["snowfall_amount_guidance"], target_reference_time=TARGET
    )
    for a, b in zip(views, reloaded, strict=True):
        xr.testing.assert_identical(a.dataset, b.dataset)
        assert a.manifest["events"] == b.manifest["events"]
        assert a.manifest["inputs"] == b.manifest["inputs"]
        assert a.manifest["request_failures"] == b.manifest["request_failures"]


def test_missing_native_parent_does_not_erase_available_endpoint_profile(
    tmp_path, monkeypatch, source
):
    root, _, _ = source
    acquire = prepared.acquire_amount_lead

    def missing_parent(model, cycle, lead, **kwargs):
        if model == "RAP" and lead == 1:
            raise FetchError("fixture missing cumulative parent")
        return acquire(model, cycle, lead, **kwargs)

    monkeypatch.setattr(prepared, "acquire_amount_lead", missing_parent)
    result = build(root, tmp_path / "one")
    views = prepared.load_snowfall_amount_guidance(
        result["snowfall_amount_guidance"], target_reference_time=TARGET
    )
    rap = next(view for view in views if view.manifest["model"] == "RAP")
    assert np.isnan(rap.dataset.amount.values[:2]).all()
    assert rap.manifest["events"][1]["missing_reasons"]
    assert rap.manifest["events"][1]["profile"]["complete"]
    assert np.isfinite(rap.dataset.temperature_profile.values[1]).all()
    np.testing.assert_allclose(rap.dataset.amount.values[2:21], 0.001, rtol=0, atol=1e-12)
    assert np.isnan(rap.dataset.amount.values[21:]).all()
    assert all(event["missing_reasons"] for event in rap.manifest["events"][21:])


def test_corrupt_raw_wrong_target_policy_and_source_identity_are_rejected(tmp_path, source):
    root, _, _ = source
    result = build(root, tmp_path / "one")
    descriptor = result["snowfall_amount_guidance"]
    changed = deepcopy(descriptor)
    changed["policy"]["weights"] = {"HRRR": 1}
    with pytest.raises(ValueError, match="policy"):
        prepared.load_snowfall_amount_guidance(changed, target_reference_time=TARGET)
    with pytest.raises(ValueError, match="target"):
        prepared.load_snowfall_amount_guidance(
            descriptor, target_reference_time=TARGET + np.timedelta64(1, "h")
        )
    raw = next((tmp_path / "one" / "RAP" / "raw").glob("*temperature_2m.grib2"))
    raw.write_bytes(b"changed profile evidence")
    with pytest.raises(ValueError):
        prepared.load_snowfall_amount_guidance(descriptor, target_reference_time=TARGET)
    with pytest.raises(ValueError):
        prepared.prepare_snowfall_amount_run(tmp_path / "one", tmp_path / "two", from_raw=True)


def test_acquisition_identity_cannot_be_silently_relabelled(tmp_path, monkeypatch, source):
    root, _, _ = source
    acquire = prepared.acquire_amount_lead
    monkeypatch.setattr(
        prepared, "acquire_amount_lead", lambda *a, **kw: replace(acquire(*a, **kw), model="GFS")
    )
    with pytest.raises(ValueError, match="identity"):
        build(root, tmp_path / "one")


def test_exact_native_interval_keeps_zero_and_missing_separate():
    ds, _, event = native("NBM", 1, amount=[[0, 0.002], [-0.001, np.nan]])
    result, normalized = prepared._interval_amount(ds, event, None, None, event["interval_start"])
    np.testing.assert_equal(result.amount.values, [[0, 0.002], [np.nan, np.nan]])
    xr.testing.assert_identical(result.native_end_amount.rename("native_amount"), ds.native_amount)
    xr.testing.assert_identical(result.native_slr, ds.native_slr)
    assert normalized["native_slr"] == event["native_slr"]
    assert normalized["normalization"]["method"] == "native_exact_interval_amount"
    assert result.amount.attrs["units"] == "m"


def test_native_cumulative_difference_conserves_new_snow_and_retains_both_parents():
    first, _, first_event = native("RAP", 1, amount=0.001)
    second, _, second_event = native("RAP", 2, amount=0.004)
    a, _ = prepared._interval_amount(first, first_event, None, None, CYCLE)
    b, event = prepared._interval_amount(
        second, second_event, first, first_event, first_event["interval_end"]
    )
    np.testing.assert_allclose(a.amount + b.amount, 0.004, rtol=0, atol=1e-12)
    np.testing.assert_allclose(b.amount, 0.003, rtol=0, atol=1e-12)
    xr.testing.assert_identical(b.native_start_amount.rename("native_amount"), first.native_amount)
    xr.testing.assert_identical(b.native_end_amount.rename("native_amount"), second.native_amount)
    xr.testing.assert_identical(b.temperature_profile, second.temperature_profile)
    assert event["profile"] == second_event["profile"]
    assert event["interval_start"] == first_event["interval_end"]
    assert event["interval_end"] == second_event["interval_end"]
    decreasing, _, decrease_event = native("RAP", 2, amount=[[0.001, 0], [np.nan, 0.003]])
    invalid, _ = prepared._interval_amount(
        decreasing, decrease_event, first, first_event, first_event["interval_end"]
    )
    np.testing.assert_allclose(
        invalid.amount, [[0, np.nan], [np.nan, 0.002]], rtol=0, atol=1e-12, equal_nan=True
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_cycle", "2026-09-11T06:00:00Z"),
        ("native_quantity", "snowpack_depth"),
        ("unit", "kg/m^2"),
        ("native_unit", "mm"),
        ("unit_factor_to_m", 0.001),
        ("interval_start", "2026-09-11T11:00:00Z"),
        ("interval_end", "2026-09-11T12:00:00Z"),
        ("interval_closure", "closed"),
        ("hydrometeor_scope", "snow_and_sleet"),
        ("spatial_support", "neighborhood_probability"),
        ("version", {"model_version": "different"}),
    ],
)
def test_incompatible_cumulative_parents_are_rejected(key, value):
    start, _, start_event = native("HRRR", 1)
    end, _, end_event = native("HRRR", 2)
    interval_start = start_event["interval_end"]
    start_event[key] = value
    with pytest.raises(ValueError, match="parents"):
        prepared._interval_amount(end, end_event, start, start_event, interval_start)


def test_parent_array_unit_and_grid_must_match():
    start, _, start_event = native("HRRR", 1)
    end, _, end_event = native("HRRR", 2)
    start.native_amount.attrs["units"] = "mm"
    with pytest.raises(ValueError):
        prepared._interval_amount(end, end_event, start, start_event, start_event["interval_end"])
    start.native_amount.attrs["units"] = "m"
    start = start.assign_coords(x=start.x + 0.1)
    with pytest.raises(ValueError, match="grid"):
        prepared._interval_amount(end, end_event, start, start_event, start_event["interval_end"])


@pytest.mark.parametrize(
    "key,value",
    [
        ("native_quantity", "snowfall_water_equivalent"),
        ("unit", "kg/m^2"),
        ("unit_factor_to_m", 1000),
    ],
)
def test_swe_and_noncanonical_native_amounts_are_not_relabelled_as_depth(key, value):
    ds, _, event = native("NBM", 1)
    event[key] = value
    with pytest.raises(ValueError, match="metres"):
        prepared._interval_amount(ds, event, None, None, event["interval_start"])
