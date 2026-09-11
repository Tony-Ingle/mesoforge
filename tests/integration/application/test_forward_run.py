"""Forward orchestration through real PostgreSQL/MinIO, with generated provider inputs.

The historical clocks and provider-shaped bytes are explicit test fixtures. They
exercise existing verification and persistence, not a claim of measured skill.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import numpy as np
import pytest

from mesoforge.application import automatic_verification, batch_forecast, prepared_observations
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import (
    _write_prepared_file,
    prepare_temperature_guidance,
)
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from tests.integration.application import test_batch_issuance as issuance_tests
from tests.integration.application import test_issued_temperature_verification as verification_tests
from tests.support.observation_preview import complete_storage_inventory
from tests.support.phase1_fixture_transports import (
    FakeHttpResponse,
    FixedClock,
    FixtureAviationWeatherTransport,
)
from tests.unit.application.test_batch_forecast import FIRST, LAST, OUTSIDE, write_config
from tests.unit.application.test_prepared_observations import _record
from tests.unit.application.test_prepared_shadow import frame, geographic_frame
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    GFS_CYCLE,
    TARGET,
    FixtureClock,
    FixtureSleeper,
    FixtureTransport,
    phase2_configuration,
)

pytestmark = pytest.mark.integration

migrated_dsn = issuance_tests.migrated_dsn
object_store = issuance_tests.object_store
prepared_guidance = issuance_tests.prepared_guidance
configured_retrieval_storage = issuance_tests.configured_retrieval_storage
DECISION = TARGET + timedelta(hours=6, minutes=30)
CURRENT_TARGET = TARGET + timedelta(hours=6)


class DecisionClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return DECISION.astimezone(tz) if tz is not None else DECISION.replace(tzinfo=None)


def prepared_current_fixture(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the retained fixture acquisition/normalization helpers, with a later reference."""
    control = directory / "control"
    manifest = prepare_temperature_guidance(
        control,
        configuration=phase2_configuration(),
        target_reference_time=CURRENT_TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        target_horizon_hours=EXTENDED_HORIZONS,
        transport=FixtureTransport(tuple(range(7, 43))),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
    )
    selection = {
        "status": "selected",
        "decision_time": DECISION.isoformat(),
        "target_reference_time": CURRENT_TARGET.isoformat(),
        "selected_cycles": {
            "HRRR": TARGET.isoformat(),
            "GFS": GFS_CYCLE.isoformat(),
            "RAP": CURRENT_TARGET.isoformat(),
            "IFS": CURRENT_TARGET.isoformat(),
        },
        "fixture_notice": "Generated test evidence; no real provider discovery is claimed.",
    }
    evidence = {
        "selection_sha256": str(Digest.of_bytes(json.dumps(selection).encode())),
        "selection": selection,
        "object_validation": [{"matched": True, "fixture_notice": "Generated test evidence"}],
    }
    manifest["current_model_set"] = evidence
    (control / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shadows = {}
    for model, make_frame, leads in (
        ("RAP", frame, range(1, 37)),
        ("IFS", geographic_frame, range(3, 37, 3)),
    ):
        time = np.datetime64(CURRENT_TARGET.replace(tzinfo=None), "ns")
        decoded = {
            lead: make_frame(lead).assign_coords(
                time=time,
                step=np.timedelta64(lead, "h"),
                valid_time=time + np.timedelta64(lead, "h"),
            )
            for lead in leads
        }
        dataset = normalize_shadow_temperature(
            decoded, model=model, cycle=CURRENT_TARGET, target=CURRENT_TARGET
        )
        dataset.attrs["data_kind"] = "synthetic_demonstration"
        shadow = directory / model
        shadow.mkdir()
        _write_prepared_file(shadow, model, dataset)
        shadows[model] = str(shadow)
    return selection, {
        "directory": str(control),
        "shadow_directories": shadows,
        "current_model_set": evidence,
        "downloaded_bytes": 0,
        "retained_raw_bytes": sum(row["raw_bytes"] for row in manifest["inputs"]),
        "fixture_notice": "Offline prepared generated fixture; no real model downloads.",
    }


def test_forward_run_verifies_then_issues_and_reuses_verification_without_mutating_history(
    tmp_path: Path,
    prepared_guidance: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application import forward_run, selected_forecast

    monkeypatch.setattr(forward_run, "datetime", DecisionClock)
    monkeypatch.setattr(automatic_verification, "datetime", DecisionClock)
    monkeypatch.setattr(batch_forecast, "SystemClock", lambda: FixedClock(DECISION))
    monkeypatch.setenv("MESOFORGE_OBSERVATIONS_DIR", str(tmp_path / "observations"))
    _, metadata_provider = verification_tests.persisted_discovery(
        migrated_dsn, object_store, monkeypatch
    )
    transport = FixtureAviationWeatherTransport()
    metar_payload = verification_tests.JSON.serialize(
        [
            _record(
                rawOb=f"SYNTHETIC KROS 30{hour:02d}10Z 18005KT 10SM CLR 20/10 A3000",
                obsTime=int(datetime(2026, 8, 30, hour, 10, tzinfo=UTC).timestamp()),
                reportTime=f"2026-08-30T{hour}:10:00Z",
                receiptTime=f"2026-08-30T{hour}:12:00Z",
            )
            for hour in (13, 15)
        ]
    )
    transport.metar_queue.append(FakeHttpResponse(200, {}, metar_payload))
    monkeypatch.setattr(
        prepared_observations, "RequestsAviationWeatherHttpTransport", lambda: transport
    )
    historical_issuer = verification_tests.make_issuer(migrated_dsn, object_store)
    numerical = PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    historical = [
        historical_issuer.issue(numerical, batch_run_id=uuid4(), location_index=0) for _ in range(2)
    ]
    original_versions = [historical_issuer.read(row.issued_forecast_id) for row in historical]

    selection, preparation = prepared_current_fixture(tmp_path / "current-guidance")
    guidance = PreparedPointForecast.from_directory(
        Path(preparation["directory"]),
        configuration=IFS_CONFIGURATION,
        shadow_directories={
            model: Path(path) for model, path in preparation["shadow_directories"].items()
        },
    )
    baselines = {
        index: guidance.forecast(latitude=location["lat"], longitude=location["lon"])
        for index, location in ((0, FIRST), (2, LAST))
    }
    events = []

    def discover(directory: Path) -> dict[str, Any]:
        # Discovery happens after observation matching/persistence, before new issuance.
        inventory = complete_storage_inventory(migrated_dsn, object_store)
        assert len(verification_tests.verification_artifacts(inventory)) == 4
        events.append("discover")
        directory.mkdir()
        (directory / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
        return selection

    def prepare(locations: list[Any], selection_path: Path, output_directory: Path, **_: Any):
        events.append("prepare")
        assert locations == configured_locations
        assert json.loads(selection_path.read_text(encoding="utf-8")) == selection
        return preparation

    discover_provider = Mock(side_effect=discover)
    prepare_provider = Mock(side_effect=prepare)
    monkeypatch.setattr(forward_run, "_discover", discover_provider)
    monkeypatch.setattr(selected_forecast, "prepare_selected", prepare_provider)
    configured_locations = [{**FIRST, "name": "Optional display name"}, OUTSIDE, LAST]
    config = write_config(tmp_path, configured_locations)
    issuer = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity=verification_tests.CODE_IDENTITY,
        clock=lambda: DECISION,
    )
    first = forward_run.run_forward(
        config, tmp_path / "first", issuer=issuer, display_timezone="America/Chicago"
    )
    assert first["summary"]["issued"] == 2
    assert first["summary"]["failed"] == 1
    assert [row["status"] for row in first["results"]] == ["ok", "error", "ok"]
    assert "issued" not in first["results"][1]
    first_verification = first["results"][0]["verification"]
    assert first_verification["summary"]["verified"] == 4
    assert first_verification["summary"]["unavailable"] == 8
    assert first_verification["summary"]["errors"] == 0
    assert first["results"][2]["verification"]["status"] == "nothing_to_verify"
    assert first["results"][2]["verification"]["downloaded_bytes"] == 0
    after_first = complete_storage_inventory(migrated_dsn, object_store)
    repeated = forward_run.run_forward(
        config, tmp_path / "repeat", issuer=issuer, display_timezone="America/Chicago"
    )
    assert [row["status"] for row in repeated["results"]] == ["ok", "error", "ok"]
    repeated_verification = repeated["results"][0]["verification"]
    assert repeated_verification["summary"]["verified"] == 0
    assert repeated_verification["summary"]["already_existing"] == 4
    assert repeated_verification["downloaded_bytes"] == 0
    assert repeated["results"][2]["verification"]["status"] == "nothing_to_verify"
    assert len(transport.get_calls) == 1
    metadata_provider.assert_called_once_with(FIRST["lat"], FIRST["lon"])
    assert events == ["discover", "prepare", "discover", "prepare"]
    assert discover_provider.call_count == prepare_provider.call_count == 2

    saved_ids = set()
    for run in (first, repeated):
        assert run["preparation"]["downloaded_bytes"] == 0
        assert run["coverage"]["downloaded_bytes"] == 0
        for index in (0, 2):
            row = run["results"][index]
            identifier = UUID(row["issued"]["issued_forecast_id"])
            saved_ids.add(identifier)
            forecast = row["forecast"]
            assert {key: value for key, value in forecast.items() if key != "hourly_report"} == (
                baselines[index]
            )
            assert forecast["current_model_set"] == preparation["current_model_set"]
            assert len(forecast["hours"]) == len(forecast["hourly_report"]["hours"]) == 36
            for hour, report in zip(
                forecast["hours"], forecast["hourly_report"]["hours"], strict=True
            ):
                horizon = hour["horizon_hours"]
                assert hour["temperature"]["value"] == pytest.approx(288 + horizon, abs=1e-6)
                assert report["raw_numerical_temperature"] == hour["temperature"]
                assert report["final_temperature"] == hour["temperature"]
                assert report["bias_correction"]["status"] == "not_implemented"
                assert report["bias_correction"]["applied_delta"] == {"value": 0.0, "unit": "K"}
                assert report["ai_adjustment"]["action"] == "not_run"
                assert report["ai_adjustment"]["applied_delta"] == {"value": 0.0, "unit": "K"}
                assert report["verification"]["status"] == "not_yet_verified"
                assert report["delivery_status"] == "not_delivered"
                assert report["contributors"]["HRRR"]["weight"] == 0.7
                assert report["contributors"]["GFS"]["weight"] == 0.3
                assert report["contributors"]["RAP"]["weight"] == 0
                assert report["contributors"]["IFS"]["weight"] == 0
                assert (report["contributors"]["IFS"]["temperature"]["value"] is None) == (
                    horizon % 3 != 0
                )
            assert issuer.read(identifier)["forecast"] == forecast
    assert len(saved_ids) == 4
    after_repeat = complete_storage_inventory(migrated_dsn, object_store)
    assert len(after_repeat["tables"]["issued_forecasts"]) == 6
    assert verification_tests.verification_artifacts(after_repeat) == (
        verification_tests.verification_artifacts(after_first)
    )
    assert [
        historical_issuer.read(row.issued_forecast_id) for row in historical
    ] == original_versions
    verifier = verification_tests.make_verifier(migrated_dsn, object_store, historical_issuer)
    for row in first_verification["results"]:
        if row.get("verification_id") is None:
            continue
        saved = verifier.read(ArtifactId(row["verification_id"]))["result"]
        assert saved["match"]["issued_forecast_id"] in {
            str(record.issued_forecast_id) for record in historical
        }
        assert saved["match"]["selected"]["temperature"] == {"value": 293.15, "unit": "K"}
    assert complete_storage_inventory(migrated_dsn, object_store) == after_repeat
    for name in ("first", "repeat"):
        assert json.loads((tmp_path / name / "result.json").read_text())["summary"]["issued"] == 2
        assert "not_run" in (tmp_path / name / "hourly-report.md").read_text(encoding="utf-8")
