"""Exact discovery selection consumption, without provider or storage services."""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from datetime import timedelta
from email.utils import format_datetime
from unittest.mock import Mock
from uuid import UUID

import numpy as np
import pytest

from mesoforge.application import batch_forecast, selected_forecast
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import _write_prepared_file
from mesoforge.forecasting.recipes import with_surface_fields
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.support.phase1_fixture_transports import FixedClock, RecordingSleeper
from tests.unit.application.test_current_model_set import DECISION, NOW, TARGET, _select
from tests.unit.application.test_prepared_shadow import frame, geographic_frame
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    prepare_fixture_guidance,
)
from tests.unit.application.test_prepared_temperature import (
    TARGET as PREPARED_TARGET,
)

LOCATIONS = [{"lat": 45.8, "lon": -93.1}, {"lat": 45.9, "lon": -93.0}]


def test_surface_selection_rechecks_optional_field_evidence_against_retained_inventory(
    selection, monkeypatch
):
    path, original = selection
    report = deepcopy(original)
    report["surface_fields"] = True
    report["contributor_configuration"] = with_surface_fields(IFS_CONFIGURATION).model_dump(
        mode="json"
    )
    missing = {
        field: "native field absent" for field in selected_forecast.SURFACE_MODEL_FIELDS["HRRR"][1:]
    }
    for model in report["models"]:
        for probe in _selected_candidate(report, model)["probes"]:
            probe.update(extra_messages=[], missing_fields=deepcopy(missing))
    parsed = Mock(return_value=([], missing))
    monkeypatch.setattr(selected_forecast, "_surface_messages", parsed)
    path.write_text(json.dumps(report), encoding="utf-8")
    loaded, _, probes = selected_forecast.load_selection(path, clock=FixedClock(NOW))
    assert loaded == report and len(probes) == parsed.call_count == 120
    _selected_candidate(report, "GFS")["probes"][0]["missing_fields"]["wind_gust_10m"] = (
        "altered evidence"
    )
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from retained inventory"):
        selected_forecast.load_selection(path, clock=FixedClock(NOW))


@pytest.fixture
def selection(tmp_path):
    report = _select(tmp_path)
    # Extend the shared discovery fixture with HTTP fields checked by the pinned
    # transport, so rejection tests cannot pass due to unrelated absent metadata.
    for model in report["models"]:
        for probe in _selected_candidate(report, model)["probes"]:
            for name in ("index", "grib"):
                probe[name].update(
                    last_modified=format_datetime(DECISION, usegmt=True), status_code=200
                )
            probe["selected_message"]["content_bytes"] = 100
    path = tmp_path / "selection" / "selection.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Unit test attempted provider access"))
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    return forbidden


def _selected_candidate(report, model):
    return next(
        row
        for row in report["models"][model]["candidates"]
        if row["cycle"] == report["selected_cycles"][model]
    )


def test_selection_readback_retains_every_required_probe_and_approved_configuration(selection):
    path, original = selection
    report, configuration, probes = selected_forecast.load_selection(path, clock=FixedClock(NOW))
    assert report == original
    assert configuration.model_dump(mode="json") == original["source_configuration"]
    assert len(probes) == 120
    assert {
        model: sum(row["model"] == model for row in probes) for model in original["models"]
    } == {
        "HRRR": 36,
        "GFS": 36,
        "RAP": 36,
        "IFS": 12,
    }
    assert len(report["models"]["IFS"]["expected_native_gaps"]) == 24
    assert not {"hrrr_cycle", "gfs_cycle", "rap_cycle", "ifs_cycle", "target_reference_time"} & set(
        inspect.signature(selected_forecast.prepare_selected).parameters
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "unavailable",
        "missing_cycle",
        "different_cycle",
        "missing_interior_rap",
        "missing_native_ifs",
        "duplicate_probe",
        "wrong_valid_time",
        "changed_weights",
        "late_publication",
        "changed_inventory",
    ],
)
def test_invalid_selection_is_rejected_before_preparation(
    selection, tmp_path, monkeypatch, forbid_network, mutation
):
    path, original = selection
    report = deepcopy(original)
    if mutation == "unavailable":
        report["status"] = "unavailable"
    elif mutation == "missing_cycle":
        del report["selected_cycles"]["IFS"]
    elif mutation == "different_cycle":
        report["selected_cycles"]["GFS"] = "2026-09-11T00:00:00Z"
    elif mutation in {"missing_interior_rap", "missing_native_ifs"}:
        model = "RAP" if mutation == "missing_interior_rap" else "IFS"
        _selected_candidate(report, model)["probes"].pop(3)
    elif mutation == "duplicate_probe":
        _selected_candidate(report, "HRRR")["probes"].append(
            _selected_candidate(report, "HRRR")["probes"][0]
        )
    elif mutation == "wrong_valid_time":
        _selected_candidate(report, "IFS")["probes"][0]["valid_time"] = "2026-09-15T00:00:00Z"
    elif mutation == "changed_weights":
        report["contributor_configuration"]["control_recipe"]["contributors"][0]["weight"] = 0.6
    elif mutation == "late_publication":
        _selected_candidate(report, "RAP")["probes"][0]["grib"]["available_at"] = (
            TARGET + timedelta(hours=1)
        ).isoformat()
    elif mutation == "changed_inventory":
        retained = _selected_candidate(report, "IFS")["probes"][0]["retained_indexes"][0]
        (path.parent / retained["file"]).write_bytes(b"changed provider inventory")
    path.write_text(json.dumps(report), encoding="utf-8")
    preparation = Mock(side_effect=AssertionError("Invalid selection reached preparation"))
    monkeypatch.setattr(selected_forecast, "_acquire_control", preparation)
    clock = FixedClock(NOW)
    with pytest.raises(ValueError):
        selected_forecast.prepare_selected(
            LOCATIONS,
            path,
            tmp_path / "prepared",
            transport=Mock(spec=[]),
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
    preparation.assert_not_called()
    forbid_network.assert_not_called()


def test_expired_selection_never_reaches_preparation(selection, tmp_path, monkeypatch):
    path, _ = selection
    preparation = Mock(side_effect=AssertionError("Expired selection reached preparation"))
    monkeypatch.setattr(selected_forecast, "_acquire_control", preparation)
    clock = FixedClock(TARGET + timedelta(hours=1))
    with pytest.raises(ValueError, match="expir|future|current"):
        selected_forecast.prepare_selected(
            LOCATIONS,
            path,
            tmp_path / "prepared",
            transport=Mock(spec=[]),
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
    preparation.assert_not_called()


def test_selection_changed_after_validation_is_rejected_before_acquisition(
    selection, tmp_path, monkeypatch, forbid_network
):
    path, _ = selection
    load = selected_forecast.load_selection

    def changed_after_read(selection_path, *, clock):
        validated = load(selection_path, clock=clock)
        changed = deepcopy(validated[0])
        changed["reason"] = "Changed after the validated read"
        selection_path.write_text(json.dumps(changed), encoding="utf-8")
        return validated

    loader = Mock(side_effect=changed_after_read)
    acquisition = Mock(side_effect=AssertionError("Changed evidence reached acquisition"))
    monkeypatch.setattr(selected_forecast, "load_selection", loader)
    monkeypatch.setattr(selected_forecast, "_acquire_control", acquisition)
    clock = FixedClock(NOW)
    with pytest.raises(ValueError, match="changed after validation"):
        selected_forecast.prepare_selected(
            LOCATIONS,
            path,
            tmp_path / "prepared",
            transport=Mock(spec=[]),
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
    loader.assert_called_once()
    acquisition.assert_not_called()
    forbid_network.assert_not_called()


def test_inventory_changed_during_copy_is_revalidated_before_acquisition(
    selection, tmp_path, monkeypatch, forbid_network
):
    path, report = selection
    retained = _selected_candidate(report, "IFS")["probes"][0]["retained_indexes"][0]
    inventory = path.parent / retained["file"]
    copytree = selected_forecast.shutil.copytree

    def changed_during_copy(source, destination):
        # The first load already verified this file. Its later changed bytes
        # must fail validation of the retained copy, before any provider call.
        inventory.write_bytes(b"Changed between initial validation and copying")
        return copytree(source, destination)

    loader = Mock(wraps=selected_forecast.load_selection)
    acquisition = Mock(side_effect=AssertionError("Changed inventory reached acquisition"))
    monkeypatch.setattr(selected_forecast, "load_selection", loader)
    monkeypatch.setattr(selected_forecast.shutil, "copytree", changed_during_copy)
    monkeypatch.setattr(selected_forecast, "_acquire_control", acquisition)
    output = tmp_path / "prepared"
    clock = FixedClock(NOW)
    with pytest.raises(ValueError, match="checksum|byte count"):
        selected_forecast.prepare_selected(
            LOCATIONS,
            path,
            output,
            transport=Mock(spec=[]),
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
    assert loader.call_count == 2
    assert loader.call_args.args[0] == output / "discovery" / "selection.json"
    assert (output / "discovery" / retained["file"]).read_bytes() == inventory.read_bytes()
    acquisition.assert_not_called()
    forbid_network.assert_not_called()


@pytest.mark.parametrize("failure", [None, "RAP missing hour", "IFS missing hour", "IFS cycle"])
def test_preparation_reuses_one_selected_source_set_and_rejects_partial_shadows(
    selection, tmp_path, monkeypatch, failure
):
    selection_path, selection_report = selection
    pinned = Mock(
        spec=selected_forecast.SelectedObjectTransport,
        validations=[{"status": "validated", "fixture": True}],
        downloaded_bytes=120,
    )
    pin = Mock(return_value=pinned)
    monkeypatch.setattr(selected_forecast, "SelectedObjectTransport", pin)
    acquired = {"HRRR": [object()], "GFS": [object()]}
    acquire = Mock(return_value=acquired)
    monkeypatch.setattr(selected_forecast, "_acquire_control", acquire)

    def control(directory, **kwargs):
        directory.mkdir(parents=True)
        assert kwargs["acquired_inputs"] is acquired
        manifest = {"inputs": [], "fixture": True}
        # Match the real helper's existing manifest, including exclusive creation.
        selected_forecast._write_bytes(directory / "manifest.json", json.dumps(manifest).encode())
        return manifest

    prepare_control = Mock(side_effect=control)
    monkeypatch.setattr(selected_forecast, "prepare_temperature_guidance", prepare_control)
    shadow_mocks = {}
    for model in ("RAP", "IFS"):
        expected = [
            int(
                (
                    selected_forecast._time(valid)
                    - selected_forecast._time(selection_report["target_reference_time"])
                ).total_seconds()
                / 3600
            )
            for valid in selection_report["models"][model]["valid_times"]
        ]
        shadow = {
            "selected_cycle": selection_report["selected_cycles"][model],
            "supported_hours": expected,
            "retained_raw_bytes": 0,
        }
        if failure == f"{model} missing hour":
            shadow["supported_hours"] = expected[1:]
        elif failure == f"{model} cycle":
            shadow["selected_cycle"] = "2026-09-11T00:00:00Z"
        shadow_mocks[model] = Mock(return_value=shadow)
        monkeypatch.setattr(selected_forecast, f"prepare_{model.lower()}", shadow_mocks[model])
    location_rows = [LOCATIONS[0], {"lat": 95.0, "lon": -93.0}, LOCATIONS[1]]

    def coverage(locations, source, **kwargs):
        assert locations == location_rows
        manifest = json.loads((source / "manifest.json").read_text())
        assert manifest["fixture"] is True
        assert manifest["current_model_set"]["selection"] == selection_report
        assert manifest["current_model_set"]["object_validation"] == pinned.validations
        assert kwargs["contributor_configuration"] == IFS_CONFIGURATION
        return object(), {"fixture": True}

    cover = Mock(side_effect=coverage)
    monkeypatch.setattr(selected_forecast, "ensure_coverage", cover)
    output = tmp_path / "prepared"
    clock = FixedClock(NOW)
    arguments = {
        "transport": Mock(spec=[]),
        "clock": clock,
        "sleeper": RecordingSleeper(clock),
    }
    if failure:
        with pytest.raises(ValueError, match="no issuance"):
            selected_forecast.prepare_selected(location_rows, selection_path, output, **arguments)
        cover.assert_not_called()
        pinned.assert_complete.assert_not_called()
        assert (output / "failure.json").is_file()
        assert not (output / "preparation.json").exists()
    else:
        result = selected_forecast.prepare_selected(
            location_rows, selection_path, output, **arguments
        )
        assert result["current_model_set"]["selection"] == selection_report
        assert result["downloaded_bytes"] == 120
        assert set(result["shadow_directories"]) == {"RAP", "IFS"}
        assert json.loads((output / "preparation.json").read_text()) == result
        assert (output / "discovery" / "selection.json").read_bytes() == selection_path.read_bytes()
        assert len(list((output / "discovery" / "inventories").iterdir())) == 120
        cover.assert_called_once()
        pinned.assert_complete.assert_called_once()
    pin.assert_called_once()
    assert len(pin.call_args.args[1]) == 120
    acquire.assert_called_once()
    prepare_control.assert_called_once()
    assert prepare_control.call_args.kwargs["hrrr_cycle"].isoformat() == "2026-09-11T06:00:00+00:00"
    assert prepare_control.call_args.kwargs["gfs_cycle"].isoformat() == "2026-09-11T06:00:00+00:00"
    for model, prepare in shadow_mocks.items():
        if failure == "RAP missing hour" and model == "IFS":
            prepare.assert_not_called()
            continue
        prepare.assert_called_once()
        assert prepare.call_args.args[0] == LOCATIONS
        assert prepare.call_args.kwargs["cycle_override"] == selected_forecast._time(
            selection_report["selected_cycles"][model]
        )
        assert prepare.call_args.kwargs["transport"] is pinned


def test_control_acquisition_only_uses_each_selected_endpoint_and_lead(
    selection, tmp_path, monkeypatch
):
    path, _ = selection
    report, configuration, _ = selected_forecast.load_selection(path, clock=FixedClock(NOW))
    calls = {}
    for model, name in (("HRRR", "acquire_hrrr_phase2_lead"), ("GFS", "acquire_gfs_lead")):
        calls[model] = Mock(side_effect=lambda *args, **kwargs: object())
        monkeypatch.setattr(selected_forecast, name, calls[model])
    retain = Mock()
    monkeypatch.setattr(selected_forecast, "_retain_input", retain)
    pinned = Mock(spec=[])
    clock = FixedClock(NOW)
    result = selected_forecast._acquire_control(
        report, configuration, tmp_path / "raw", pinned, clock, RecordingSleeper(clock)
    )
    assert set(result) == {"HRRR", "GFS"}
    assert retain.call_count == 72
    for model, acquire in calls.items():
        assert len(result[model]) == acquire.call_count == 36
        assert [call.kwargs["forecast_hour"] for call in acquire.call_args_list] == list(
            range(5, 41)
        )
        for call in acquire.call_args_list:
            assert call.args[0].endpoint_order == ("fixture",)
            assert call.kwargs["cycle_hour"] == 6
            assert call.kwargs["cycle_date"].isoformat() == "2026-09-11"
            assert call.kwargs["canonical_variables"] == ("air_temperature_2m",)
            assert call.kwargs["transport"] is pinned


def test_selected_batch_prepares_once_and_preserves_shadows_and_evidence_in_immutable_readback(
    tmp_path, monkeypatch, forbid_network
):
    # Acquisition is a separate tested boundary. Generated prepared fixtures exercise
    # the actual point calculation, batch continuation, serialization, and readback.
    control = tmp_path / "control"
    prepare_fixture_guidance(control, EXTENDED_HORIZONS)
    evidence = {
        "selection_sha256": "a" * 64,
        "selection": {
            "status": "selected",
            "decision_time": (PREPARED_TARGET + timedelta(minutes=20)).isoformat(),
            "target_reference_time": PREPARED_TARGET.isoformat(),
            "selected_cycles": {
                "HRRR": PREPARED_TARGET.isoformat(),
                "GFS": (PREPARED_TARGET - timedelta(hours=6)).isoformat(),
                "RAP": PREPARED_TARGET.isoformat(),
                "IFS": PREPARED_TARGET.isoformat(),
            },
        },
        "object_validation": {"status": "validated", "fixture": True},
    }
    path = control / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["current_model_set"] = evidence
    path.write_text(json.dumps(manifest), encoding="utf-8")
    shadows = {}
    for model, factory, leads in (
        ("RAP", frame, range(1, 37)),
        ("IFS", geographic_frame, range(3, 37, 3)),
    ):
        decoded = {}
        time = np.datetime64(PREPARED_TARGET.replace(tzinfo=None), "ns")
        for lead in leads:
            decoded[lead] = factory(lead).assign_coords(
                time=time,
                step=np.timedelta64(lead, "h"),
                valid_time=time + np.timedelta64(lead, "h"),
            )
        dataset = normalize_shadow_temperature(
            decoded, model=model, cycle=PREPARED_TARGET, target=PREPARED_TARGET
        )
        dataset.attrs["data_kind"] = "synthetic_demonstration"
        directory = tmp_path / model
        directory.mkdir()
        _write_prepared_file(directory, model, dataset)
        shadows[model] = str(directory)
    preparation_report = {
        "directory": str(control),
        "shadow_directories": shadows,
        "current_model_set": evidence,
        "downloaded_bytes": 0,
    }
    prepare = Mock(return_value=preparation_report)
    monkeypatch.setattr(selected_forecast, "prepare_selected", prepare)
    clock = FixedClock(PREPARED_TARGET + timedelta(minutes=25))
    monkeypatch.setattr(batch_forecast, "SystemClock", lambda: clock)
    location_rows = [LOCATIONS[0], {"lat": 95.0, "lon": -93.0}, LOCATIONS[1]]
    config_path = tmp_path / "locations.json"
    config_path.write_text(json.dumps({"locations": location_rows}), encoding="utf-8")
    store, records = InMemoryObjectStore(), InMemoryUnitOfWorkFactory()
    issuer = ForecastIssuanceService(
        store, records, code_identity={"test": "selected-batch"}, clock=clock.now
    )
    report = selected_forecast.run_selected_batch(
        config_path, tmp_path / "selection.json", tmp_path / "selected", issuer=issuer, clock=clock
    )
    prepare.assert_called_once()
    assert prepare.call_args.args[0] == location_rows
    results = report["results"]
    assert [row["status"] for row in results] == ["ok", "error", "ok"]
    assert results[1]["error"]["code"] == "unsupported_coordinate"
    assert len(records.issued_forecasts) == 2
    for row in (results[0], results[2]):
        forecast = row["forecast"]
        assert forecast["current_model_set"] == evidence
        assert forecast["contributor_configuration"] == IFS_CONFIGURATION.model_dump(mode="json")
        assert len(forecast["hours"]) == 36
        for hour in forecast["hours"]:
            h = hour["horizon_hours"]
            assert hour["temperature"] == {"value": pytest.approx(282 + h), "unit": "K"}
            assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]
            shadow_values = {source["model"]: source for source in hour["shadow_sources"]}
            assert set(shadow_values) == {"RAP", "IFS"}
            rap, ifs = shadow_values["RAP"], shadow_values["IFS"]
            assert rap["weight"] == ifs["weight"] == 0.0
            assert rap["temperature"]["value"] is not None
            if h % 3:
                assert ifs["temperature"]["value"] is None
                assert ifs["missing_reasons"]
            else:
                assert ifs["temperature"]["value"] is not None
        saved = issuer.read(UUID(row["issued"]["issued_forecast_id"]))
        assert saved["forecast"] == forecast
    assert len(records.issued_forecasts) == 2
    forbid_network.assert_not_called()
