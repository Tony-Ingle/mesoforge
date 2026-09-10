"""Automatic preparation through issuance, using generated GRIB and memory storage."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest

from mesoforge.application import batch_forecast, cycle_selection, prepared_temperature
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.guidance.acquisition_v2 import acquire_gfs_lead, acquire_hrrr_phase2_lead
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.unit.application.test_batch_forecast import FIRST, LAST, OUTSIDE, write_config
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    GFS_CYCLE,
    TARGET,
    VARIABLE,
    FixtureSleeper,
    FixtureTransport,
    phase2_configuration,
    prepare_fixture_guidance,
)

EXECUTION = TARGET + timedelta(minutes=40)


class CurrentFixtureClock:
    def now(self) -> datetime:
        return EXECUTION


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("Automatic batch attempted real network access"))
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch):
    objects = InMemoryObjectStore()
    factory = InMemoryUnitOfWorkFactory()
    service = ForecastIssuanceService(
        objects,
        factory,
        code_identity={"test": "generated GRIB with automatic selection evidence"},
        clock=lambda: EXECUTION,
    )
    monkeypatch.setattr(batch_forecast, "create_issuer", lambda: service)
    monkeypatch.setattr(batch_forecast, "SystemClock", CurrentFixtureClock)
    monkeypatch.setattr(prepared_temperature, "SystemClock", CurrentFixtureClock)
    return service, factory, objects


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _acquired_selection(transport: FixtureTransport) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    """Acquire selected generated messages exactly as discovery does, without a provider."""
    configuration = phase2_configuration()
    acquisitions: dict[str, list[Any]] = {}
    report: dict[str, Any] = {
        "mode": "automatic",
        "status": "selected",
        "execution_time": _iso(EXECUTION),
        "completed_at": _iso(EXECUTION),
        "target_reference_time": _iso(TARGET),
        "first_valid_time": _iso(TARGET + timedelta(hours=1)),
        "last_valid_time": _iso(TARGET + timedelta(hours=36)),
        "selected_cycles": {"HRRR": _iso(TARGET), "GFS": _iso(GFS_CYCLE)},
        "candidates": {},
    }
    for model, cycle, age, acquire, settings in (
        ("HRRR", TARGET, 0, acquire_hrrr_phase2_lead, configuration.hrrr),
        ("GFS", GFS_CYCLE, 6, acquire_gfs_lead, configuration.gfs),
    ):
        acquisitions[model] = [
            acquire(
                settings,
                transport=transport,
                clock=CurrentFixtureClock(),
                sleeper=FixtureSleeper(),
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=horizon + age,
                cycle_deadline=EXECUTION,
                canonical_variables=(VARIABLE,),
            )
            for horizon in EXTENDED_HORIZONS
        ]
        report["candidates"][model] = [
            {
                "cycle": _iso(cycle),
                "status": "selected",
                "reason": "Every required generated temperature message acquired and decoded",
                "source_lead_hours": [horizon + age for horizon in EXTENDED_HORIZONS],
            }
        ]
    return acquisitions, report


def test_automatic_batch_reuses_selected_inputs_and_retains_selection_through_offline_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], issuer
) -> None:
    locations = [FIRST, OUTSIDE, LAST]
    config = write_config(tmp_path, locations)
    transport = FixtureTransport(EXTENDED_HORIZONS)
    acquisitions, selection = _acquired_selection(transport)
    calls_after_discovery = list(transport.calls)
    assert sum(requested is not None for _, _, requested in transport.calls) == 72
    transport.close = Mock()
    selector = Mock(return_value=(acquisitions, selection))
    monkeypatch.setattr(cycle_selection, "select_current_guidance", selector)
    monkeypatch.setattr(prepared_temperature, "BoundedHttpTransport", lambda: transport)
    preparation = Mock(wraps=prepared_temperature.prepare_locations)
    monkeypatch.setattr(batch_forecast, "prepare_locations", preparation)

    assert (
        batch_forecast.main(["--config", str(config), "--output-dir", str(tmp_path / "runs")]) == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    preparation.assert_called_once_with(
        locations,
        tmp_path / "runs",
        target_reference_time=None,
        hrrr_cycle=None,
        gfs_cycle=None,
    )
    selector.assert_called_once()
    assert len(selector.call_args.kwargs["areas"]) == 1
    assert transport.calls == calls_after_discovery, "Selected messages must not be acquired twice"
    transport.close.assert_called_once()
    rows = payload["results"]
    assert [row["status"] for row in rows] == ["ok", "error", "ok"]
    assert rows[1]["error"]["code"] == "unsupported_coordinate"
    assert len(payload["coverage"]["regions"]) == 1
    service, factory, objects = issuer
    assert len(factory.issued_forecasts) == len(objects.objects) == 2
    source = Path(payload["preparation"]["directory"]) / "source"
    original = (source / "manifest.json").read_bytes()
    manifest = json.loads(original)
    assert manifest["cycle_selection"] == selection
    assert len(manifest["inputs"]) == 72
    for row in (rows[0], rows[2]):
        forecast = row["forecast"]
        saved = service.read(UUID(row["issued"]["issued_forecast_id"]))["forecast"]
        assert saved == forecast
        assert forecast["cycle_selection"]["selected_cycles"] == selection["selected_cycles"]
        assert forecast["cycle_selection"]["candidates"] == selection["candidates"]
        assert len(forecast["hours"]) == 36
        for horizon, hour in enumerate(forecast["hours"], start=1):
            # Independent constant-grid blend: .7*(279+h) + .3*(289+h) = 282+h K.
            assert hour["temperature"] == {
                "value": pytest.approx(282 + horizon, abs=1e-6, rel=0),
                "unit": "K",
            }
            assert hour["valid_time"] == _iso(TARGET + timedelta(hours=horizon))
            assert datetime.fromisoformat(hour["valid_time"]) > EXECUTION
            assert hour["missing_reasons"] == []
            assert [source["source_lead_hours"] for source in hour["sources"]] == [
                horizon,
                horizon + 6,
            ]
            assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]
            for model_index, model_source in enumerate(hour["sources"]):
                evidence = next(
                    item
                    for item in manifest["inputs"]
                    if item["model"] == model_source["model"]
                    and item["valid_time"] == hour["valid_time"]
                )
                acquisition = {
                    key: evidence[key]
                    for key in (
                        "raw_bytes",
                        "index_sha256",
                        "index_bytes",
                        "source_index_url",
                        "byte_start",
                        "byte_end",
                        "endpoint",
                        "grib_retrieved_at",
                        "index_retrieved_at",
                        "grib_available_at",
                        "index_available_at",
                        "grib_last_modified",
                        "index_last_modified",
                        "etag",
                    )
                }
                assert model_source["acquisition"] == acquisition
                assert model_source["raw_sha256"] == evidence["raw_sha256"]
                assert model_source["source_url"] == evidence["source_grib_url"]
                assert model_source["cycle"] == evidence["cycle"]
                assert model_source["source_lead_hours"] == evidence["source_lead_hours"]
                assert (
                    saved["hours"][horizon - 1]["sources"][model_index]["acquisition"]
                    == acquisition
                )

    forbidden = Mock(side_effect=AssertionError("Offline rebuilding attempted acquisition"))
    monkeypatch.setattr(transport, "get", forbidden)
    monkeypatch.setattr(transport, "head", forbidden)
    rebuilt = prepared_temperature.rebuild_temperature_guidance(
        source,
        tmp_path / "rebuilt",
        configuration=phase2_configuration(),
        clock=CurrentFixtureClock(),
    )
    assert rebuilt["downloaded_bytes"] == 0
    assert rebuilt["cycle_selection"] == selection
    assert rebuilt["inputs"] == manifest["inputs"]
    assert rebuilt["source_manifest_sha256"] == hashlib.sha256(original).hexdigest()
    restored = PreparedPointForecast.from_directory(tmp_path / "rebuilt").forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    assert restored["hours"] == rows[0]["forecast"]["hours"]
    assert restored["cycle_selection"] == rows[0]["forecast"]["cycle_selection"]
    assert (source / "manifest.json").read_bytes() == original
    assert len(factory.issued_forecasts) == len(objects.objects) == 2
    forbidden.assert_not_called()


def test_automatic_batch_cannot_issue_after_first_valid_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issuer
) -> None:
    guidance = tmp_path / "guidance"
    prepare_fixture_guidance(guidance, EXTENDED_HORIZONS)
    monkeypatch.setattr(
        batch_forecast, "SystemClock", lambda: Mock(now=lambda: TARGET + timedelta(hours=1))
    )
    result = batch_forecast.run_batch(
        write_config(tmp_path, [FIRST, LAST]), guidance, require_future_hours=True
    )
    assert [row["status"] for row in result["results"]] == ["error", "error"]
    for row in result["results"]:
        assert row["error"]["code"] == "forecast_failed"
        assert "expired before issuance" in row["error"]["message"]
        assert "issued" not in row
    assert issuer[1].issued_forecasts == {}
    assert issuer[2].objects == {}


def test_explicit_override_still_prepares_once_and_allows_historical_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], issuer
) -> None:
    config = write_config(tmp_path, [FIRST, OUTSIDE, LAST])
    data_dir = tmp_path / "prepared"
    prepare = Mock(return_value={"directory": str(data_dir)})
    run = Mock(return_value={"results": []})
    monkeypatch.setattr(batch_forecast, "prepare_locations", prepare)
    monkeypatch.setattr(batch_forecast, "run_batch", run)
    assert (
        batch_forecast.main(
            [
                "--config",
                str(config),
                "--output-dir",
                str(tmp_path),
                "--target-reference-time",
                _iso(TARGET),
                "--hrrr-cycle",
                _iso(TARGET),
                "--gfs-cycle",
                _iso(GFS_CYCLE),
            ]
        )
        == 0
    )
    assert capsys.readouterr().err == ""
    prepare.assert_called_once_with(
        [FIRST, OUTSIDE, LAST],
        tmp_path,
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
    )
    run.assert_called_once_with(config, data_dir, issuer=issuer[0], require_future_hours=False)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--output-dir", "prepared", "--hrrr-cycle", _iso(TARGET)],
        [
            "--output-dir",
            "prepared",
            "--target-reference-time",
            _iso(TARGET),
            "--gfs-cycle",
            _iso(GFS_CYCLE),
        ],
        ["--data-dir", "prepared", "--target-reference-time", _iso(TARGET)],
        ["--data-dir", "prepared", "--output-dir", "new-prepared"],
    ],
)
def test_partial_or_conflicting_cycle_arguments_fail_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    forbidden = Mock(side_effect=AssertionError("Invalid arguments attempted preparation/storage"))
    monkeypatch.setattr(batch_forecast, "create_issuer", forbidden)
    monkeypatch.setattr(batch_forecast, "prepare_locations", forbidden)
    with pytest.raises(SystemExit) as exc:
        batch_forecast.main(["--config", str(tmp_path / "locations.json"), *arguments])
    assert exc.value.code == 2
    forbidden.assert_not_called()
