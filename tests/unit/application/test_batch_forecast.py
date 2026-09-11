"""Offline batch checks using prepared, generated GRIB fixtures, never provider data."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import numpy as np
import pytest
import xarray as xr

from mesoforge.application import batch_forecast
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast, prepare_demo_files
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    TARGET,
    prepare_fixture_guidance,
)
from tests.unit.test_forecast_api import shadow_configuration, write_shadow

FIRST = {"lat": 45.8, "lon": -93.1}
LAST = {"lat": 45.9, "lon": -93.0}
OUTSIDE = {"lat": 95.0, "lon": -93.27}  # Invalid latitude, not a prepared-region limit.


def write_real_shadow_snapshot(source_directory: Path, directory: Path) -> dict[str, Any]:
    """Generated fixtures in the real-preparation format, with their own retained evidence."""
    directory.mkdir(parents=True)
    original = json.loads((source_directory / "manifest.json").read_text(encoding="utf-8"))
    with xr.open_dataset(source_directory / "GFS.nc", engine="h5netcdf") as opened:
        dataset = opened.load()
    dataset.attrs["model"] = "SYNTH_SHADOW"
    dataset["air_temperature_2m"].values += 10.0
    path = directory / "SYNTH_SHADOW.nc"
    dataset.to_netcdf(path, engine="h5netcdf")
    inputs = [
        {**row, "model": "SYNTH_SHADOW"} for row in original["inputs"] if row["model"] == "GFS"
    ]
    for row in inputs:
        for prefix in ("raw", "index"):
            relative = Path(row[f"{prefix}_file"])
            (directory / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_directory / relative, directory / relative)
    manifest = {
        **original,
        "inputs": inputs,
        "prepared_files": {
            "SYNTH_SHADOW": {
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        },
        "source_metadata": {
            "model_definition": shadow_configuration().models[-1].model_dump(mode="json"),
            "adapter_version": "synthetic_fixture_v1",
            "product": "synthetic-GRIB",
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_separate_shadow_manifest_preserves_control_and_acquisition_readback(
    tmp_path, prepared_batch_data, memory_issuance, monkeypatch
):
    baseline = PreparedPointForecast.from_directory(prepared_batch_data).forecast(
        **{"latitude": FIRST["lat"], "longitude": FIRST["lon"]}
    )
    original_manifest = (prepared_batch_data / "manifest.json").read_bytes()
    shadow_directory = tmp_path / "shadow=guidance"
    manifest = write_real_shadow_snapshot(prepared_batch_data, shadow_directory)
    prepared = PreparedPointForecast.from_directory(
        prepared_batch_data,
        configuration=shadow_configuration(),
        shadow_directories={"SYNTH_SHADOW": shadow_directory},
    )
    forbidden = Mock(side_effect=AssertionError("Requests must use loaded guidance"))
    monkeypatch.setattr(xr, "open_dataset", forbidden)
    forecast = prepared.forecast(latitude=FIRST["lat"], longitude=FIRST["lon"])
    assert forecast["manifest_sha256"] == baseline["manifest_sha256"]
    assert (prepared_batch_data / "manifest.json").read_bytes() == original_manifest
    for original, hour, evidence in zip(
        baseline["hours"], forecast["hours"], manifest["inputs"], strict=True
    ):
        assert {key: value for key, value in hour.items() if key != "shadow_sources"} == original
        source = hour["shadow_sources"][0]
        assert source["temperature"]["value"] == pytest.approx(299 + hour["horizon_hours"])
        assert source["weight"] == 0.0
        assert source["acquisition"] == evidence
        assert source["source_metadata"] == manifest["source_metadata"]
        assert (
            source["manifest_sha256"]
            == hashlib.sha256((shadow_directory / "manifest.json").read_bytes()).hexdigest()
        )
    assert prepared.forecast(latitude=FIRST["lat"], longitude=FIRST["lon"]) == forecast
    issuer = memory_issuance[0]
    issued = issuer.issue(forecast, batch_run_id=UUID(int=1), location_index=0)
    assert issuer.read(issued.issued_forecast_id)["forecast"] == forecast
    forbidden.assert_not_called()


@pytest.mark.parametrize("mutation", [None, "raw", "cycle"])
def test_shadow_regions_require_the_same_retained_cycle_and_raw_identity(
    tmp_path, prepared_batch_data, mutation
):
    root = tmp_path / "shadow"
    manifests = [
        write_real_shadow_snapshot(prepared_batch_data, root / name) for name in ("a", "b")
    ]
    changed = manifests[1]
    if mutation == "raw":
        row = changed["inputs"][0]
        raw = root / "b" / row["raw_file"]
        raw.write_bytes(raw.read_bytes() + b"different retained message")
        row["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        row["raw_bytes"] = raw.stat().st_size
    elif mutation == "cycle":
        path = root / "b" / "SYNTH_SHADOW.nc"
        with xr.open_dataset(path, engine="h5netcdf") as opened:
            dataset = opened.load()
        dataset = dataset.assign_coords(
            forecast_reference_time=dataset.forecast_reference_time.values - np.timedelta64(6, "h"),
            source_lead_time=dataset.source_lead_time.values + np.timedelta64(6, "h"),
        )
        dataset.to_netcdf(path, engine="h5netcdf")
        changed["prepared_files"]["SYNTH_SHADOW"]["sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        cycle = str(np.datetime_as_string(dataset.forecast_reference_time.values, unit="s")) + "Z"
        for row in changed["inputs"]:
            row["cycle"] = cycle
            row["source_lead_hours"] += 6
    (root / "b" / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")
    (root / "coverage.json").write_text(
        json.dumps(
            {
                "regions": [
                    {
                        "area": {"south": 45.5, "north": 46.0, "west": -93.5, "east": -93.0},
                        "directory": name,
                    }
                    for name in ("a", "b")
                ]
            }
        ),
        encoding="utf-8",
    )
    kwargs = {"configuration": shadow_configuration(), "shadow_directories": {"SYNTH_SHADOW": root}}
    if mutation is not None:
        with pytest.raises(ValueError, match="shadow regions disagree on retained input identity"):
            PreparedPointForecast.from_directory(prepared_batch_data, **kwargs)
    else:
        prepared = PreparedPointForecast.from_directory(prepared_batch_data, **kwargs)
        assert prepared.forecast(latitude=FIRST["lat"], longitude=FIRST["lon"])["hours"][0][
            "shadow_sources"
        ][0]["temperature"]["value"] == pytest.approx(300.0)


def test_batch_cli_separate_shadow_snapshot_is_reused_for_supported_locations(
    tmp_path, prepared_batch_data, capsys
):
    config = write_config(tmp_path, [FIRST, OUTSIDE, LAST])
    contributors = tmp_path / "contributors.json"
    contributors.write_text(shadow_configuration().model_dump_json(), encoding="utf-8")
    shadow_directory = tmp_path / "shadow=guidance"
    write_real_shadow_snapshot(prepared_batch_data, shadow_directory)
    assert (
        batch_forecast.main(
            [
                "--config",
                str(config),
                "--data-dir",
                str(prepared_batch_data),
                "--contributors-config",
                str(contributors),
                "--shadow-data",
                f"SYNTH_SHADOW={shadow_directory}",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    successful = [row["forecast"] for row in result["results"] if row["status"] == "ok"]
    for forecast in successful:
        assert len(forecast["hours"]) == 36
        assert all(
            hour["shadow_sources"][0]["temperature"]["value"] is not None
            for hour in forecast["hours"]
        )
    assert (
        successful[0]["hours"][0]["shadow_sources"][0]["manifest_sha256"]
        == successful[1]["hours"][0]["shadow_sources"][0]["manifest_sha256"]
    )


@pytest.mark.parametrize(
    "attachment", ["HRRR=somewhere", "UNREGISTERED=somewhere", "SYNTH_SHADOW", "SYNTH_SHADOW="]
)
def test_cli_rejects_invalid_shadow_attachment_before_preparation(
    tmp_path, capsys, monkeypatch, attachment
):
    contributors = tmp_path / "contributors.json"
    contributors.write_text(shadow_configuration().model_dump_json(), encoding="utf-8")
    forbidden = Mock(side_effect=AssertionError("Invalid attachment must not acquire or prepare"))
    monkeypatch.setattr(batch_forecast, "prepare_locations", forbidden)
    assert (
        batch_forecast.main(
            [
                "--config",
                str(write_config(tmp_path, [FIRST])),
                "--output-dir",
                str(tmp_path / "uncreated-preparation"),
                "--contributors-config",
                str(contributors),
                "--shadow-data",
                attachment,
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "batch_failed"
    forbidden.assert_not_called()


@pytest.mark.parametrize("mutation", ["target", "raw", "prepared", "manifest"])
def test_separate_shadow_evidence_is_validated(tmp_path, prepared_batch_data, mutation):
    shadow_directory = tmp_path / "shadow"
    manifest = write_real_shadow_snapshot(prepared_batch_data, shadow_directory)
    if mutation == "target":
        manifest["target_reference_time"] = "2026-08-30T13:00:00Z"
        (shadow_directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "raw":
        (shadow_directory / manifest["inputs"][0]["raw_file"]).write_bytes(b"corrupt")
    elif mutation == "prepared":
        manifest["prepared_files"]["SYNTH_SHADOW"]["sha256"] = "0" * 64
        (shadow_directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        (shadow_directory / "manifest.json").unlink()
    with pytest.raises(ValueError, match="target time|Checksum mismatch|requires manifest"):
        PreparedPointForecast.from_directory(
            prepared_batch_data,
            configuration=shadow_configuration(),
            shadow_directories={"SYNTH_SHADOW": shadow_directory},
        )


def test_batch_issues_synthetic_shadow_and_configuration_without_changing_control(
    tmp_path,
    prepared_batch_data,
    memory_issuance,
):
    data_dir = tmp_path / "guidance"
    shutil.copytree(prepared_batch_data, data_dir)
    config = write_config(tmp_path, [FIRST, OUTSIDE, LAST])
    baseline = batch_forecast.run_batch(config, data_dir)
    write_shadow(data_dir)
    configuration = shadow_configuration()
    result = batch_forecast.run_batch(config, data_dir, contributor_configuration=configuration)
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    service, factory, _ = memory_issuance
    assert len(factory.issued_forecasts) == 4
    for original, row in zip(baseline["results"], result["results"], strict=True):
        if row["status"] != "ok":
            continue
        assert row["forecast"]["contributor_configuration"] == configuration.model_dump(mode="json")
        saved = service.read(UUID(row["issued"]["issued_forecast_id"]))
        assert saved["forecast"] == row["forecast"]
        assert len(saved["forecast"]["hours"]) == 36
        for old_hour, hour in zip(
            original["forecast"]["hours"], saved["forecast"]["hours"], strict=True
        ):
            assert {
                key: value for key, value in hour.items() if key != "shadow_sources"
            } == old_hour
            shadow = hour["shadow_sources"][0]
            assert shadow["temperature"] == {
                "value": pytest.approx(299 + hour["horizon_hours"]),
                "unit": "K",
            }
            assert shadow["weight"] == 0.0
            assert shadow["data_kind"] == "synthetic_demonstration"
            assert shadow["missing_reasons"] == []
            assert shadow["prepared_sha256"]


def test_batch_cli_reads_contributors_config_and_persists_snapshot(
    tmp_path, prepared_batch_data, capsys, memory_issuance
):
    config = write_config(tmp_path, [FIRST])
    contributors = tmp_path / "contributors.json"
    configuration = shadow_configuration()
    contributors.write_text(configuration.model_dump_json(), encoding="utf-8")
    assert (
        batch_forecast.main(
            [
                "--config",
                str(config),
                "--data-dir",
                str(prepared_batch_data),
                "--contributors-config",
                str(contributors),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    forecast = result["results"][0]["forecast"]
    assert forecast["contributor_configuration"] == configuration.model_dump(mode="json")
    assert all(
        hour["shadow_sources"][0]["temperature"]["value"] is None for hour in forecast["hours"]
    )
    assert all(hour["missing_reasons"] == [] for hour in forecast["hours"])


@pytest.mark.parametrize("change", ["control", "active_model", "extra_active"])
def test_batch_rejects_active_changes_before_any_preparation(tmp_path, monkeypatch, change):
    snapshot = shadow_configuration().model_dump(mode="json")
    if change == "control":
        snapshot["control_recipe"]["contributors"][0]["weight"] = 0.6
        snapshot["control_recipe"]["contributors"][1]["weight"] = 0.4
    elif change == "active_model":
        snapshot["models"][0]["grid_type"] = "either"
    else:
        snapshot["models"][-1]["status"] = "active"
    configuration = ContributorConfiguration.model_validate_json(json.dumps(snapshot))
    forbidden = Mock(side_effect=AssertionError("Invalid control must not load/prepare guidance"))
    monkeypatch.setattr(batch_forecast, "ensure_coverage", forbidden)
    with pytest.raises(ValueError, match="retain|outside"):
        batch_forecast.run_batch(
            write_config(tmp_path, [FIRST]),
            tmp_path / "guidance",
            contributor_configuration=configuration,
        )
    forbidden.assert_not_called()


def test_real_shadow_prepared_checksum_is_verified(tmp_path, prepared_batch_data):
    directory = tmp_path / "guidance"
    shutil.copytree(prepared_batch_data, directory)
    # Generated GRIB fixtures exercise the real-preparation format, not provider data.
    with xr.open_dataset(directory / "GFS.nc", engine="h5netcdf") as opened:
        shadow = opened.load()
    shadow.attrs["model"] = "SYNTH_SHADOW"
    shadow.to_netcdf(directory / "SYNTH_SHADOW.nc", engine="h5netcdf")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["prepared_files"]["SYNTH_SHADOW"] = {"file": "SYNTH_SHADOW.nc", "sha256": "0" * 64}
    manifest["inputs"].extend(
        [{**row, "model": "SYNTH_SHADOW"} for row in manifest["inputs"] if row["model"] == "GFS"]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Checksum mismatch for SYNTH_SHADOW.nc"):
        PreparedPointForecast.from_directory(directory, configuration=shadow_configuration())


@pytest.fixture(autouse=True)
def memory_issuance(monkeypatch: pytest.MonkeyPatch):
    """Exercise issuance with explicit test doubles; these checks need no services."""
    store = InMemoryObjectStore()
    factory = InMemoryUnitOfWorkFactory()
    service = ForecastIssuanceService(
        store,
        factory,
        code_identity={"git_commit": "a" * 40, "working_tree_dirty": False},
        clock=lambda: datetime(2026, 9, 10, 12, tzinfo=UTC),
    )
    monkeypatch.setattr(batch_forecast, "create_issuer", lambda: service)
    return service, factory, store


@pytest.fixture(scope="module")
def prepared_batch_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("batch-guidance")
    prepare_fixture_guidance(directory, EXTENDED_HORIZONS)
    return directory


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("Batch attempted network access"))
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


def write_config(tmp_path: Path, locations: list[Any]) -> Path:
    path = tmp_path / "locations.json"
    path.write_text(json.dumps({"locations": locations}), encoding="utf-8")
    return path


def test_batch_reuses_one_load_and_preserves_36_hour_values_times_and_provenance(
    tmp_path: Path, prepared_batch_data: Path, monkeypatch: pytest.MonkeyPatch, memory_issuance
) -> None:
    config = write_config(tmp_path, [FIRST, OUTSIDE, LAST])
    manifest_bytes = (prepared_batch_data / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    loader = Mock(wraps=PreparedPointForecast.from_directory)
    original_forecast = PreparedPointForecast.forecast
    used_guidance: list[PreparedPointForecast] = []

    def forecast_without_io(
        self: PreparedPointForecast, *, latitude: float, longitude: float
    ) -> dict[str, Any]:
        used_guidance.append(self)
        with monkeypatch.context() as during_forecast:
            forbidden = Mock(side_effect=AssertionError("Forecast reloaded prepared guidance"))
            during_forecast.setattr(Path, "open", forbidden)
            during_forecast.setattr("xarray.open_dataset", forbidden)
            return original_forecast(self, latitude=latitude, longitude=longitude)

    monkeypatch.setattr(PreparedPointForecast, "from_directory", loader)
    monkeypatch.setattr(PreparedPointForecast, "forecast", forecast_without_io)
    result = batch_forecast.run_batch(config, prepared_batch_data)

    loader.assert_called_once_with(
        prepared_batch_data, configuration=DEFAULT_CONFIGURATION, shadow_directories=None
    )
    assert len(used_guidance) == 2
    assert all(item is used_guidance[0] for item in used_guidance)
    rows = result["results"]
    assert [row["index"] for row in rows] == [0, 1, 2]
    assert [row["location"] for row in rows] == [FIRST, OUTSIDE, LAST]
    assert [row["status"] for row in rows] == ["ok", "error", "ok"]
    assert rows[1]["error"]["code"] == "unsupported_coordinate"
    assert "[-90, 90]" in rows[1]["error"]["message"]
    assert "forecast" not in rows[1]
    assert "issued" not in rows[1]
    service, factory, _ = memory_issuance
    assert len(factory.issued_forecasts) == 2
    for row, location in ((rows[0], FIRST), (rows[2], LAST)):
        forecast = row["forecast"]
        issued = row["issued"]
        assert issued["batch_run_id"] == result["batch_run_id"]
        assert issued["location_index"] == row["index"]
        assert service.read(UUID(issued["issued_forecast_id"]))["forecast"] == forecast
        assert forecast["latitude"] == location["lat"]
        assert forecast["longitude"] == location["lon"]
        assert forecast["data_kind"] == manifest["data_kind"]
        assert "fixed prepared inputs" in forecast["notice"]
        assert forecast["target_reference_time"] == "2026-08-30T12:00:00Z"
        assert forecast["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
        assert [hour["horizon_hours"] for hour in forecast["hours"]] == list(EXTENDED_HORIZONS)
        for horizon, hour in zip(EXTENDED_HORIZONS, forecast["hours"], strict=True):
            # Constant fixture grids: .7*(279+h) + .3*(289+h) = 282+h K.
            assert hour["temperature"]["value"] == pytest.approx(282 + horizon, abs=1e-6)
            assert hour["temperature"]["unit"] == "K"
            assert hour["valid_time"] == (TARGET + timedelta(hours=horizon)).isoformat().replace(
                "+00:00", "Z"
            )
            assert hour["missing_reasons"] == []
            assert len(hour["sources"]) == 2
            for source, model, age, weight in zip(
                hour["sources"], ("HRRR", "GFS"), (0, 6), (0.7, 0.3), strict=True
            ):
                evidence = next(
                    item
                    for item in manifest["inputs"]
                    if item["model"] == model and item["valid_time"] == hour["valid_time"]
                )
                assert source == {
                    "model": model,
                    "cycle": "2026-08-30T12:00:00Z" if age == 0 else "2026-08-30T06:00:00Z",
                    "source_lead_hours": horizon + age,
                    "weight": weight,
                    "temperature": {
                        "value": pytest.approx(
                            (279 if model == "HRRR" else 289) + horizon, abs=1e-6
                        ),
                        "unit": "K",
                    },
                    "missing_reasons": [],
                    "raw_sha256": evidence["raw_sha256"],
                    "source_url": evidence["source_grib_url"],
                    "prepared_sha256": manifest["prepared_files"][model]["sha256"],
                }


def test_calculation_failure_does_not_prevent_the_next_location(
    tmp_path: Path, prepared_batch_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_forecast = PreparedPointForecast.forecast

    def fail_first(
        self: PreparedPointForecast, *, latitude: float, longitude: float
    ) -> dict[str, Any]:
        if latitude == FIRST["lat"]:
            raise RuntimeError("Deliberate calculation failure")
        return original_forecast(self, latitude=latitude, longitude=longitude)

    monkeypatch.setattr(PreparedPointForecast, "forecast", fail_first)
    rows = batch_forecast.run_batch(write_config(tmp_path, [FIRST, LAST]), prepared_batch_data)[
        "results"
    ]
    assert rows[0]["error"] == {
        "code": "forecast_failed",
        "message": "RuntimeError: Deliberate calculation failure",
    }
    assert rows[1]["status"] == "ok"
    assert len(rows[1]["forecast"]["hours"]) == 36


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [45.8, -93.1],
        {},
        {"lat": 45.8},
        {"lat": 45.8, "lon": -93.1, "station_id": "KMSP"},
        {"lat": True, "lon": -93.1},
        {"lat": 45.8, "lon": False},
        {"lat": "45.8", "lon": -93.1},
        {"lat": 45.8, "lon": None},
    ],
)
def test_invalid_location_is_an_explicit_error_and_processing_continues(
    tmp_path: Path, prepared_batch_data: Path, invalid: Any
) -> None:
    rows = batch_forecast.run_batch(write_config(tmp_path, [invalid, LAST]), prepared_batch_data)[
        "results"
    ]
    assert rows[0]["location"] == invalid
    assert rows[0]["status"] == "error"
    assert rows[0]["error"]["code"] == "invalid_location"
    assert rows[0]["error"]["message"]
    assert rows[1]["index"] == 1
    assert rows[1]["status"] == "ok"
    assert len(rows[1]["forecast"]["hours"]) == 36


@pytest.mark.parametrize(
    "document",
    [
        "{",
        "[]",
        "{}",
        '{"locations": {"lat": 45.8, "lon": -93.1}}',
        '{"locations": [], "stations": []}',
        '{"locations": [{"lat": NaN, "lon": -93.1}]}',
        '{"locations": [{"lat": 45.8, "lon": Infinity}]}',
        '{"locations": [{"lat": 45.8, "lon": -Infinity}]}',
    ],
)
def test_invalid_config_is_rejected_before_loading_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: str
) -> None:
    config = tmp_path / "locations.json"
    config.write_text(document, encoding="utf-8")
    loader = Mock(side_effect=AssertionError("Invalid config should not load guidance"))
    monkeypatch.setattr(PreparedPointForecast, "from_directory", loader)
    with pytest.raises(ValueError):
        batch_forecast.run_batch(config, tmp_path / "guidance")
    loader.assert_not_called()


def test_old_three_hour_dataset_is_rejected_instead_of_silently_shortening_batch(
    tmp_path: Path,
) -> None:
    guidance = tmp_path / "old-guidance"
    prepare_demo_files(guidance)
    with pytest.raises(ValueError, match="36"):
        batch_forecast.run_batch(write_config(tmp_path, [FIRST]), guidance)


@pytest.mark.parametrize(
    "locations,expected_exit", [([FIRST, LAST], 0), ([FIRST, OUTSIDE, LAST], 1)]
)
def test_cli_returns_complete_repeatable_json_and_a_meaningful_exit_code(
    tmp_path: Path,
    prepared_batch_data: Path,
    capsys: pytest.CaptureFixture[str],
    locations: list[dict[str, float]],
    expected_exit: int,
) -> None:
    config = write_config(tmp_path, locations)
    # Windows PowerShell-created UTF-8 files may begin with a BOM.
    config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8-sig")
    arguments = ["--config", str(config), "--data-dir", str(prepared_batch_data)]
    assert batch_forecast.main(arguments) == expected_exit
    first = capsys.readouterr()
    assert first.err == ""
    rows = json.loads(first.out)["results"]
    assert len(rows) == len(locations)
    assert rows[-1]["location"] == LAST
    assert rows[-1]["status"] == "ok"
    assert len(rows[-1]["forecast"]["hours"]) == 36
    assert batch_forecast.main(arguments) == expected_exit
    repeated = capsys.readouterr()
    assert repeated.err == ""
    first_payload, second_payload = json.loads(first.out), json.loads(repeated.out)
    assert UUID(first_payload["batch_run_id"]) != UUID(second_payload["batch_run_id"])
    for first_row, second_row in zip(
        first_payload["results"], second_payload["results"], strict=True
    ):
        if first_row["status"] == "error":
            assert second_row == first_row
        else:
            assert second_row["forecast"] == first_row["forecast"]
            assert (
                second_row["issued"]["issued_forecast_id"]
                != first_row["issued"]["issued_forecast_id"]
            )


@pytest.mark.parametrize("failure", ["missing_config", "invalid_config", "missing_guidance"])
def test_cli_global_failures_are_json_errors_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    config = tmp_path / "locations.json"
    if failure == "invalid_config":
        config.write_text("{}", encoding="utf-8")
    elif failure == "missing_guidance":
        config = write_config(tmp_path, [FIRST])
    assert batch_forecast.main(["--config", str(config), "--data-dir", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "batch_failed"
    assert error["message"]


def test_cli_isolates_numeric_overflow_and_still_emits_valid_json(
    tmp_path: Path, prepared_batch_data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "locations.json"
    config.write_text(
        '{"locations": ['
        '{"lat": 45.8, "lon": -93.1}, '
        '{"lat": 1e400, "lon": -93.1}, '
        '{"lat": 45.9, "lon": -93.0}]}',
        encoding="utf-8",
    )
    assert (
        batch_forecast.main(["--config", str(config), "--data-dir", str(prepared_batch_data)]) == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Infinity" not in captured.out
    rows = json.loads(captured.out)["results"]
    assert [row["status"] for row in rows] == ["ok", "error", "ok"]
    assert rows[1]["location"] == {"lat": "1e400", "lon": -93.1}
    assert rows[1]["error"]["code"] == "invalid_location"
    assert rows[1]["error"]["message"]
    assert len(rows[0]["forecast"]["hours"]) == 36
    assert len(rows[2]["forecast"]["hours"]) == 36


def test_empty_location_list_has_no_forecast_results(
    tmp_path: Path, prepared_batch_data: Path, memory_issuance
) -> None:
    payload = batch_forecast.run_batch(write_config(tmp_path, []), prepared_batch_data)
    assert payload["results"] == []
    assert UUID(payload["batch_run_id"])
    assert memory_issuance[1].issued_forecasts == {}


def test_upload_failure_has_no_successful_record_and_next_location_is_saved(
    tmp_path: Path, prepared_batch_data: Path, memory_issuance
) -> None:
    _, factory, store = memory_issuance
    store.fail_next_put = True
    payload = batch_forecast.run_batch(write_config(tmp_path, [FIRST, LAST]), prepared_batch_data)
    first, second = payload["results"]
    assert first["status"] == "error"
    assert first["error"]["code"] == "issuance_failed"
    assert "forecast" not in first
    assert "issued" not in first
    assert second["status"] == "ok"
    assert len(second["forecast"]["hours"]) == 36
    assert len(factory.issued_forecasts) == 1
    record = next(iter(factory.issued_forecasts.values()))
    assert (record.latitude, record.longitude) == (LAST["lat"], LAST["lon"])
    assert record.location_index == 1
