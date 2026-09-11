"""Offline local-grid retention, HTTP reads and existing immutable issuance."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application import prepared_local_grid
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_local_grid import PreparedLocalGrids, prepare_local_grids
from tests.unit.application.test_forecast_issuance import memory_service as memory_service
from tests.unit.application.test_local_surface_grid import LATITUDE, LONGITUDE
from tests.unit.application.test_local_surface_grid import prepared_surface as prepared_surface


@pytest.fixture()
def preparation(tmp_path, prepared_surface, monkeypatch):
    config = tmp_path / "locations.json"
    config.write_text(
        json.dumps(
            {
                "locations": [
                    {"lat": LATITUDE, "lon": LONGITUDE, "name": "Minneapolis"},
                    {"lat": 999, "lon": LONGITUDE},
                    {"lat": 44.99, "lon": -93.25},
                ]
            }
        )
    )
    run = tmp_path / "prepared"
    run.mkdir()
    evidence = {
        "selection": {
            "surface_fields": True,
            "contributor_configuration": prepared_surface._configuration.model_dump(mode="json"),
            "selected_cycles": {"HRRR": "2026-09-11T12:00:00Z", "GFS": "2026-09-11T06:00:00Z"},
        }
    }
    report = {
        "directory": "existing-guidance",
        "shadow_directories": {},
        "current_model_set": evidence,
    }
    (run / "preparation.json").write_text(json.dumps(report))
    loaded = Mock(spec=PreparedPointForecast)
    loaded.horizon_hours = tuple(range(1, 37))
    loaded._manifest = {"current_model_set": deepcopy(evidence)}
    loaded._surface_configuration = prepared_surface._surface_configuration
    loaded.forecast.side_effect = prepared_surface.forecast
    loader = Mock(return_value=loaded)
    monkeypatch.setattr(prepared_local_grid, "load_prepared", loader)
    return config, run, tmp_path / "grids", loader


def test_retained_grid_repeat_api_read_and_immutable_storage(
    preparation, prepared_surface, memory_service, monkeypatch
):
    config, run, output, loader = preparation
    with (
        patch("requests.Session", side_effect=AssertionError("No downloads")),
        patch("xarray.open_dataset", side_effect=AssertionError("Already loaded guidance")),
    ):
        first = prepare_local_grids(config, run, output)
        assert [row["status"] for row in first["results"]] == ["ok", "error", "ok"]
        assert loader.call_count == 1
        assert first["downloaded_bytes"] == 0
        assert loader.return_value.forecast.call_count == 2
        assert len(list(output.glob("*.json.gz"))) == 2
        retained = {path.name: path.read_bytes() for path in output.iterdir()}
        repeated = prepare_local_grids(config, run, output)
        assert loader.call_count == 2  # One shared load per run, never per node/location.
        assert all(repeated["results"][i]["retained_artifact_reused"] for i in (0, 2))
        assert retained == {path.name: path.read_bytes() for path in output.iterdir()}
    saved = PreparedLocalGrids.from_directory(output)
    forecast = saved.forecast(latitude=LATITUDE, longitude=LONGITUDE)
    assert len(forecast["hours"]) == 36
    assert len(forecast["local_grid_baseline"]["cells"]) == 9
    assert (
        forecast["hours"]
        == prepared_surface._forecast_column(latitude=LATITUDE, longitude=LONGITUDE)["hours"]
    )
    # Existing storage retains the whole grid, without another history/authority.
    service, factory, objects = memory_service
    first_id = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    second_id = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert first_id.issued_forecast_id != second_id.issued_forecast_id
    assert service.read(first_id.issued_forecast_id)["forecast"] == forecast
    assert service.read(second_id.issued_forecast_id)["forecast"] == forecast
    start = datetime.fromisoformat(forecast["hours"][0]["valid_time"])
    selected = service.select_hours(
        latitude=LATITUDE,
        longitude=LONGITUDE,
        start_valid_time=start,
        end_valid_time=start + timedelta(hours=1),
    )["results"]
    assert len(selected) == 2
    for row in selected:
        assert row["hour"] == forecast["hours"][0]
        assert row["forecast_context"]["local_grid"] == forecast["local_grid"]
        assert "local_grid_baseline" not in row["forecast_context"]
    before = len(factory.issued_forecasts), len(objects.objects)
    monkeypatch.setattr(
        PreparedPointForecast, "_forecast_column", Mock(side_effect=AssertionError("No regridding"))
    )
    with TestClient(api.create_app(output)) as client:
        for _ in range(2):
            response = client.get("/forecast", params={"lat": LATITUDE, "lon": LONGITUDE})
            assert response.status_code == 200
            assert response.json() == forecast
        missing = client.get("/forecast", params={"lat": LATITUDE + 0.001, "lon": LONGITUDE})
        assert missing.status_code == 409  # Preparation required, not an unsupported domain.
        invalid = client.get("/forecast", params={"lat": 100, "lon": LONGITUDE})
        assert invalid.status_code == 422
    assert (len(factory.issued_forecasts), len(objects.objects)) == before
    forecast["local_grid_baseline"]["cells"][4]["hours"][0]["temperature"]["value"] = 0
    assert service.read(first_id.issued_forecast_id)["forecast"] != forecast
    # Any accidental retained-byte modification is rejected at startup.
    artifact = next(output.glob("*.json.gz"))
    artifact.write_bytes(artifact.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        PreparedLocalGrids.from_directory(output)


@pytest.mark.parametrize("mismatch", ["cycles", "weights"])
def test_reject_changed_selection_or_unapproved_control(preparation, mismatch):
    config, run, output, loader = preparation
    path = run / "preparation.json"
    report = json.loads(path.read_text())
    selection = report["current_model_set"]["selection"]
    if mismatch == "cycles":
        selection["selected_cycles"]["HRRR"] = "2026-09-10T12:00:00Z"
    else:
        recipe = selection["contributor_configuration"]["control_recipe"]
        recipe["contributors"][0]["weight"] = 0.5
        recipe["contributors"][1]["weight"] = 0.5
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="evidence|control recipe"):
        prepare_local_grids(config, run, output)
    loader.return_value.forecast.assert_not_called()
    assert not output.exists()


def test_native_surface_api_requires_explicit_grid_preparation(
    prepared_surface, tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "load_prepared", lambda _: prepared_surface)
    forbid = Mock(side_effect=AssertionError("A GET must not construct local fields"))
    monkeypatch.setattr(PreparedPointForecast, "forecast", forbid)
    with TestClient(api.create_app(tmp_path)) as client:
        response = client.get("/forecast", params={"lat": LATITUDE, "lon": LONGITUDE})
        assert response.status_code == 409
        assert "Prepare local surface grids" in response.json()["error"]
    forbid.assert_not_called()
