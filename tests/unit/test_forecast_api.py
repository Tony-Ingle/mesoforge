"""Offline behavior checks for the explicitly synthetic coordinate forecast demo."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

from mesoforge import api
from mesoforge.application.point_forecast import PreparedPointForecast, prepare_demo_files
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.catalog.contributors import ModelDefinition
from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION, ContributorConfiguration
from mesoforge.guidance.sources.hrrr_transport import RequestsHrrrHttpTransport

ROOT = Path(__file__).resolve().parents[2]
NOTICE = "Synthetic demonstration data; not a current weather forecast."


@pytest.fixture()
def prepared_dir(tmp_path: Path) -> Path:
    prepare_demo_files(tmp_path)
    return tmp_path


@pytest.fixture()
def client(prepared_dir: Path) -> Iterator[TestClient]:
    with TestClient(api.create_app(prepared_dir)) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("latitude", "longitude", "first_temperature", "first_hrrr", "first_gfs"),
    [
        (45.8, -93.1, 286.14, 283.2, 293.0),
        (45.625, -93.375, 284.75, 281.75, 291.75),
        (45.5, -93.5, 284.0, 281.0, 291.0),
        (46.0, -93.0, 287.0, 284.0, 294.0),
        (46.0, -93.5, 285.3, 282.0, 293.0),
        (45.5, -93.0, 285.7, 283.0, 292.0),
    ],
)
def test_coordinate_temperatures_match_independent_expected_values(
    client: TestClient,
    latitude: float,
    longitude: float,
    first_temperature: float,
    first_hrrr: float,
    first_gfs: float,
) -> None:
    # Linear synthetic fields interpolate exactly. The expected first-hour values
    # above were calculated separately with the agreed 0.70/0.30 weights.
    response = client.get("/forecast", params={"lat": latitude, "lon": longitude})
    assert response.status_code == 200
    payload = response.json()
    assert payload["data_kind"] == "synthetic_demonstration"
    assert payload["notice"] == NOTICE
    assert payload["latitude"] == latitude
    assert payload["longitude"] == longitude
    assert [hour["temperature"]["value"] for hour in payload["hours"]] == pytest.approx(
        [first_temperature, first_temperature + 1.0, first_temperature + 2.0], abs=1e-10
    )
    assert all(hour["missing_reasons"] == [] for hour in payload["hours"])
    for offset, hour in enumerate(payload["hours"]):
        for source, first in zip(hour["sources"], (first_hrrr, first_gfs), strict=True):
            assert source["temperature"] == {
                "value": pytest.approx(first + offset, abs=1e-10),
                "unit": "K",
            }
            assert source["missing_reasons"] == []


def test_response_preserves_units_valid_times_cycles_and_source_leads(client: TestClient) -> None:
    payload = client.get("/forecast", params={"lat": 45.8, "lon": -93.1}).json()
    assert payload["target_reference_time"] == "2026-08-30T12:00:00Z"
    assert len(payload["hours"]) == 3
    for horizon, hour in enumerate(payload["hours"], start=1):
        assert hour["horizon_hours"] == horizon
        assert hour["valid_time"] == f"2026-08-30T{12 + horizon}:00:00Z"
        assert hour["temperature"]["unit"] == "K"
        assert hour["sources"] == [
            {
                "model": "HRRR",
                "cycle": "2026-08-30T12:00:00Z",
                "source_lead_hours": horizon,
                "weight": 0.7,
                "temperature": {"value": pytest.approx(282.2 + horizon, abs=1e-10), "unit": "K"},
                "missing_reasons": [],
            },
            {
                "model": "GFS",
                "cycle": "2026-08-30T06:00:00Z",
                "source_lead_hours": horizon + 6,
                "weight": 0.3,
                "temperature": {"value": pytest.approx(292.0 + horizon, abs=1e-10), "unit": "K"},
                "missing_reasons": [],
            },
        ]


def test_demo_weights_match_existing_hrrr_gfs_early_horizon_row(client: TestClient) -> None:
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    assert configuration.phase2 is not None
    table = configuration.phase2.blend_configuration.scalar_vector_table
    payload = client.get("/forecast", params={"lat": 45.8, "lon": -93.1}).json()
    for hour in payload["hours"]:
        row = table.row_for(available_models=("HRRR", "GFS"), horizon=hour["horizon_hours"])
        assert row.row_id == "scalar-vector.hg.h01-h18"
        assert row.horizon_band == "h01-h18"
        assert row.weights == (0.7, 0.0, 0.3)
        assert [source["weight"] for source in hour["sources"]] == [row.weights[0], row.weights[2]]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"lat": "45.8"},
        {"lon": "-93.1"},
        {"lat": "-90.0001", "lon": "-93.1"},
        {"lat": "90.0001", "lon": "-93.1"},
        {"lat": "45.8", "lon": "-180.0001"},
        {"lat": "45.8", "lon": "180.0001"},
        {"lat": "nan", "lon": "-93.1"},
        {"lat": "inf", "lon": "-93.1"},
        {"lat": "45.8", "lon": "-inf"},
        {"lat": "north", "lon": "-93.1"},
        {"lat": "45.8", "lon": "west"},
    ],
)
def test_invalid_coordinates_return_labeled_validation_errors(
    client: TestClient, params: dict[str, str]
) -> None:
    response = client.get("/forecast", params=params)
    assert response.status_code == 422
    payload = response.json()
    assert payload["data_kind"] == "synthetic_demonstration"
    assert payload["notice"] == NOTICE


@pytest.mark.parametrize("path", ["/not-a-route", "/forecast/"])
def test_not_found_response_is_also_labeled_as_synthetic(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["data_kind"] == "synthetic_demonstration"
    assert response.json()["notice"] == NOTICE


def test_method_not_allowed_is_labeled(client: TestClient) -> None:
    response = client.post("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 405
    assert response.json()["data_kind"] == "synthetic_demonstration"
    assert response.json()["notice"] == NOTICE


def test_unexpected_error_is_labeled(prepared_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = api.create_app(prepared_dir)
    monkeypatch.setattr(PreparedPointForecast, "forecast", Mock(side_effect=RuntimeError("test")))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 500
    assert response.json()["data_kind"] == "synthetic_demonstration"
    assert response.json()["notice"] == NOTICE


def test_both_files_missing_prevents_startup_instead_of_inventing_times(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HRRR.nc and GFS.nc are missing"):
        api.create_app(tmp_path)


@pytest.mark.parametrize("missing_model", ["HRRR", "GFS"])
def test_missing_model_file_returns_null_without_renormalizing(
    prepared_dir: Path, missing_model: str
) -> None:
    missing_path = prepared_dir / f"{missing_model}.nc"
    missing_path.unlink()
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 200
    for hour in response.json()["hours"]:
        assert hour["temperature"] == {"value": None, "unit": "K"}
        assert hour["missing_reasons"] == [f"{missing_model}: prepared guidance file is missing"]
        assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]
        missing_source = next(
            source for source in hour["sources"] if source["model"] == missing_model
        )
        assert missing_source["cycle"] is None
        assert missing_source["source_lead_hours"] is None
        assert missing_source["temperature"] == {"value": None, "unit": "K"}
        assert missing_source["missing_reasons"] == hour["missing_reasons"]
        retained = next(source for source in hour["sources"] if source["model"] != missing_model)
        expected = (292.0 if missing_model == "HRRR" else 282.2) + hour["horizon_hours"]
        assert retained["temperature"] == {"value": pytest.approx(expected, abs=1e-10), "unit": "K"}
        assert retained["missing_reasons"] == []
    assert not missing_path.exists()


@pytest.mark.parametrize("missing_model", ["HRRR", "GFS"])
def test_missing_source_lead_only_nulls_its_matching_target_hour(
    prepared_dir: Path, missing_model: str
) -> None:
    path = prepared_dir / f"{missing_model}.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        guidance = opened.load()
    guidance.isel(source_lead_time=[0, 2]).to_netcdf(path, engine="h5netcdf")
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 200
    first, missing, last = response.json()["hours"]
    assert first["temperature"]["value"] == pytest.approx(286.14, abs=1e-10)
    assert last["temperature"]["value"] == pytest.approx(288.14, abs=1e-10)
    assert first["missing_reasons"] == last["missing_reasons"] == []
    assert missing["horizon_hours"] == 2
    assert missing["valid_time"] == "2026-08-30T14:00:00Z"
    assert missing["temperature"] == {"value": None, "unit": "K"}
    assert missing["missing_reasons"] == [f"{missing_model}: no guidance for this valid time"]
    assert [source["weight"] for source in missing["sources"]] == [0.7, 0.3]
    for source in missing["sources"]:
        if source["model"] == missing_model:
            assert source["temperature"] == {"value": None, "unit": "K"}
            assert source["missing_reasons"] == missing["missing_reasons"]
        else:
            expected = 294.0 if missing_model == "HRRR" else 284.2
            assert source["temperature"] == {
                "value": pytest.approx(expected, abs=1e-10),
                "unit": "K",
            }
            assert source["missing_reasons"] == []


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf")])
def test_nonfinite_extraction_is_explicit_missingness(prepared_dir: Path, nonfinite: float) -> None:
    path = prepared_dir / "HRRR.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        guidance = opened.load()
    guidance["air_temperature_2m"].values[1, :, :] = nonfinite
    guidance.to_netcdf(path, engine="h5netcdf")
    with TestClient(api.create_app(prepared_dir)) as client:
        response = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
    assert response.status_code == 200
    first, missing, last = response.json()["hours"]
    assert first["temperature"]["value"] == pytest.approx(286.14, abs=1e-10)
    assert last["temperature"]["value"] == pytest.approx(288.14, abs=1e-10)
    assert missing["temperature"] == {"value": None, "unit": "K"}
    assert len(missing["missing_reasons"]) == 1
    assert missing["missing_reasons"][0].startswith("HRRR:")
    assert "finite" in missing["missing_reasons"][0]
    assert [source["weight"] for source in missing["sources"]] == [0.7, 0.3]
    hrrr, gfs = missing["sources"]
    assert hrrr["temperature"] == {"value": None, "unit": "K"}
    assert hrrr["missing_reasons"] == missing["missing_reasons"]
    assert gfs["temperature"] == {"value": pytest.approx(294.0, abs=1e-10), "unit": "K"}
    assert gfs["missing_reasons"] == []


def test_repeated_requests_use_loaded_guidance_without_provider_calls_or_file_changes(
    prepared_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {path.name: path.read_bytes() for path in prepared_dir.iterdir()}
    app = api.create_app(prepared_dir)
    forbidden = Mock(side_effect=AssertionError("Request attempted guidance I/O"))
    monkeypatch.setattr(xr, "open_dataset", forbidden)
    monkeypatch.setattr(RequestsHrrrHttpTransport, "get", forbidden)
    monkeypatch.setattr(RequestsHrrrHttpTransport, "head", forbidden)
    with TestClient(app) as client:
        first = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
        second = client.get("/forecast", params={"lat": 45.8, "lon": -93.1})
        other_coordinate = client.get("/forecast", params={"lat": 45.625, "lon": -93.375})
    assert first.status_code == second.status_code == 200
    assert other_coordinate.status_code == 200
    assert first.content == second.content
    assert first.json()["hours"][0]["temperature"]["value"] == pytest.approx(286.14, abs=1e-10)
    assert other_coordinate.json()["hours"][0]["temperature"]["value"] == pytest.approx(
        284.75, abs=1e-10
    )
    forbidden.assert_not_called()
    assert {path.name: path.read_bytes() for path in prepared_dir.iterdir()} == before


def test_preparation_preserves_existing_files_without_filling_missing_models(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "HRRR.nc"
    existing.write_bytes(b"Existing prepared input owned by the operator")
    prepare_demo_files(tmp_path)
    assert existing.read_bytes() == b"Existing prepared input owned by the operator"
    assert not (tmp_path / "GFS.nc").exists()


def test_prepared_temperature_must_use_kelvin(prepared_dir: Path) -> None:
    path = prepared_dir / "HRRR.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        guidance = opened.load()
    guidance["air_temperature_2m"].attrs["unit_id"] = "degC"
    guidance.to_netcdf(path, engine="h5netcdf")
    with pytest.raises(ValueError, match="temperature must use K"):
        api.create_app(prepared_dir)


@pytest.mark.parametrize(
    ("reference_time", "error"),
    [
        ("2026-08-30T13:00:00Z", "disagree on target_reference_time"),
        ("2026-08-30T12:00:00", "explicit UTC timestamp"),
        ("not-a-time", "Invalid isoformat string"),
    ],
)
def test_invalid_or_inconsistent_target_time_prevents_startup(
    prepared_dir: Path, reference_time: str, error: str
) -> None:
    path = prepared_dir / "GFS.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        guidance = opened.load()
    guidance.attrs["target_reference_time"] = reference_time
    guidance.to_netcdf(path, engine="h5netcdf")
    with pytest.raises(ValueError, match=error):
        api.create_app(prepared_dir)


def test_valid_times_must_match_source_cycle_plus_leads(prepared_dir: Path) -> None:
    path = prepared_dir / "GFS.nc"
    with xr.open_dataset(path, engine="h5netcdf") as opened:
        guidance = opened.load()
    guidance = guidance.assign_coords(
        source_valid_time=(
            "source_lead_time",
            guidance["source_valid_time"].values + np.timedelta64(1, "h"),
        )
    )
    guidance.to_netcdf(path, engine="h5netcdf")
    with pytest.raises(ValueError, match="source cycle, leads and valid times disagree"):
        api.create_app(prepared_dir)


def test_cli_loads_prepared_demo_and_binds_only_to_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    prepare_demo_files(tmp_path)
    api.main(["--data-dir", str(tmp_path), "--port", "8123"])
    run.assert_called_once()
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 8123
    assert (tmp_path / "HRRR.nc").is_file()
    assert (tmp_path / "GFS.nc").is_file()


@pytest.mark.parametrize(
    "latitude,longitude", [(45.4999, -93.1), (46.0001, -93.1), (45.8, -93.5001), (45.8, -92.9999)]
)
def test_small_prepared_subset_requires_preparation_not_unsupported(client, latitude, longitude):
    response = client.get("/forecast", params={"lat": latitude, "lon": longitude})
    assert response.status_code == 409
    assert response.json()["code"] == "coverage_required"
    assert "preparation" in response.json()["error"]


def shadow_configuration(status: str = "shadow") -> ContributorConfiguration:
    definition = ModelDefinition(
        model_id="SYNTH_SHADOW",
        provider="synthetic-fixture",
        family="test",
        domain="fixture",
        supported_fields=("air_temperature_2m",),
        cycle_hours=(0, 6, 12, 18),
        supported_leads=tuple(range(49)),
        status=status,
        grid_type="geographic",
    )
    return ContributorConfiguration(
        models=(*DEFAULT_CONFIGURATION.models, definition),
        control_recipe=DEFAULT_CONFIGURATION.control_recipe,
        comparison_recipes=DEFAULT_CONFIGURATION.comparison_recipes,
    )


def write_shadow(directory: Path, *, mutation: str | None = None) -> Path:
    with xr.open_dataset(directory / "GFS.nc", engine="h5netcdf") as opened:
        dataset = opened.load()
    dataset.attrs.update(model="SYNTH_SHADOW", data_kind="synthetic_demonstration")
    dataset["air_temperature_2m"].values += 10.0
    if mutation == "outside":
        dataset = dataset.assign_coords(x=dataset.x.values + 10.0)
    elif mutation == "missing_hour":
        dataset = dataset.isel(source_lead_time=[0, 2])
    elif mutation == "nonfinite":
        dataset["air_temperature_2m"].values[1, :, :] = np.nan
    elif mutation == "units":
        dataset["air_temperature_2m"].attrs["unit_id"] = "degC"
    path = directory / "SYNTH_SHADOW.nc"
    dataset.to_netcdf(path, engine="h5netcdf")
    return path


@pytest.mark.parametrize("status", ["shadow", "evaluated", "deprecated"])
def test_registered_shadow_extracts_without_changing_active_forecast(
    prepared_dir, monkeypatch, status
):
    import hashlib

    baseline = PreparedPointForecast.from_directory(prepared_dir).forecast(
        latitude=45.8, longitude=-93.1
    )
    path = write_shadow(prepared_dir)
    configuration = shadow_configuration(status)
    prepared = PreparedPointForecast.from_directory(prepared_dir, configuration=configuration)
    expected_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    forbidden = Mock(side_effect=AssertionError("Forecast attempted to load guidance"))
    monkeypatch.setattr(xr, "open_dataset", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    result = prepared.forecast(latitude=45.8, longitude=-93.1)
    for offset, (hour, original) in enumerate(zip(result["hours"], baseline["hours"], strict=True)):
        assert {key: value for key, value in hour.items() if key != "shadow_sources"} == original
        assert hour["shadow_sources"] == [
            {
                "model": "SYNTH_SHADOW",
                "cycle": "2026-08-30T06:00:00Z",
                "source_lead_hours": 7 + offset,
                "weight": 0.0,
                "temperature": {"value": pytest.approx(303.0 + offset), "unit": "K"},
                "missing_reasons": [],
                "data_kind": "synthetic_demonstration",
                "prepared_sha256": expected_digest,
            }
        ]
    assert result["contributor_configuration"] == configuration.model_dump(mode="json")
    assert prepared.forecast(latitude=45.8, longitude=-93.1) == result
    forbidden.assert_not_called()


@pytest.mark.parametrize("mutation", ["missing_file", "outside", "missing_hour", "nonfinite"])
def test_shadow_missingness_cannot_change_active_values_or_eligibility(prepared_dir, mutation):
    baseline = PreparedPointForecast.from_directory(prepared_dir).forecast(
        latitude=45.8, longitude=-93.1
    )
    if mutation != "missing_file":
        write_shadow(prepared_dir, mutation=mutation)
    result = PreparedPointForecast.from_directory(
        prepared_dir, configuration=shadow_configuration()
    ).forecast(latitude=45.8, longitude=-93.1)
    for index, (hour, original) in enumerate(zip(result["hours"], baseline["hours"], strict=True)):
        assert {key: value for key, value in hour.items() if key != "shadow_sources"} == original
        source = hour["shadow_sources"][0]
        if mutation in ("missing_file", "outside") or index == 1:
            assert source["temperature"] == {"value": None, "unit": "K"}
            assert source["missing_reasons"]
        else:
            assert source["temperature"]["value"] is not None
            assert source["missing_reasons"] == []


def test_retired_metadata_does_not_load_new_guidance(prepared_dir):
    (prepared_dir / "SYNTH_SHADOW.nc").write_bytes(b"This must never be opened")
    result = PreparedPointForecast.from_directory(
        prepared_dir, configuration=shadow_configuration("retired")
    ).forecast(latitude=45.8, longitude=-93.1)
    assert all("shadow_sources" not in hour for hour in result["hours"])
    assert result["contributor_configuration"]["models"][-1]["status"] == "retired"


def test_invalid_shadow_units_fail_closed(prepared_dir):
    write_shadow(prepared_dir, mutation="units")
    with pytest.raises(ValueError, match="SYNTH_SHADOW: prepared temperature must use K"):
        PreparedPointForecast.from_directory(prepared_dir, configuration=shadow_configuration())
