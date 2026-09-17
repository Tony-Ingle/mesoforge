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
from mesoforge.application.accumulation_status import accumulation_status
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.application.prepared_ifs import IFS_CONFIGURATION
from mesoforge.application.prepared_pop import load_pop_guidance, prepare_pop_guidance
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import (
    _write_prepared_file,
    prepare_temperature_guidance,
)
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.guidance.acquisition_v2 import acquire_nbm_lead
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy, PostgresIdempotencyLock
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
from tests.support.phase2_provider_transports import build_nbm_transport
from tests.support.phase2_source_settings import make_nbm_settings
from tests.unit.application.test_batch_forecast import FIRST, LAST, OUTSIDE, write_config
from tests.unit.application.test_prepared_observations import _record
from tests.unit.application.test_prepared_qpf import QpfFixtureTransport
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


def prepared_current_fixture(
    directory: Path, *, surface_fields: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the retained fixture acquisition/normalization helpers, with a later reference."""
    control = directory / "control"
    manifest = prepare_temperature_guidance(
        control,
        configuration=phase2_configuration(),
        target_reference_time=CURRENT_TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        target_horizon_hours=EXTENDED_HORIZONS,
        transport=(QpfFixtureTransport if surface_fields else FixtureTransport)(
            tuple(range(7, 43))
        ),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
        surface_fields=surface_fields,
        qpf_fields=surface_fields,
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
    if surface_fields:
        selection["surface_fields"] = True
        selection["qpf_fields"] = True
        selection["source_configuration"] = phase2_configuration().model_dump(mode="json")
        selection["models"] = {
            model: {
                "candidates": [
                    {
                        "status": "metadata_complete",
                        "probes": [{"source_lead_hours": lead} for lead in leads],
                    }
                ]
            }
            for model, leads in (
                ("HRRR", range(7, 43)),
                ("GFS", range(13, 49)),
                ("RAP", range(1, 37)),
                ("IFS", range(3, 37, 3)),
            )
        }
        manifest["surface_blend_configuration"] = (
            phase2_configuration().blend_configuration.model_dump(mode="json")
        )
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
        extra = None
        if surface_fields:
            # Provider-shaped decoded fixtures retain each shadow's native time grid.
            extra = {}
            for variable, value, unit in (
                ("dew_point_temperature_2m", 260.0, "K"),
                ("eastward_wind_10m", 3.0, "m/s"),
                ("northward_wind_10m", 4.0, "m/s"),
                ("wind_gust_10m", 8.0, "m/s"),
            ):
                if model == "IFS" and variable == "wind_gust_10m":
                    continue  # Native interval gust has no instantaneous mapping.
                extra[variable] = {}
                for lead, temperature in decoded.items():
                    field = temperature.copy(
                        data=np.full(
                            temperature.shape,
                            value + lead if variable == "dew_point_temperature_2m" else value,
                        )
                    )
                    field.attrs.update(GRIB_units=unit, GRIB_uvRelativeToGrid=0)
                    extra[variable][lead] = field
        dataset = normalize_shadow_temperature(
            decoded,
            model=model,
            cycle=CURRENT_TARGET,
            target=CURRENT_TARGET,
            decoded_surface=extra,
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
        # Only valid coordinates without a version for this window reach preparation.
        assert locations == [configured_locations[0], configured_locations[2]]
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
    # Same decision window again (a scheduler retry): verification replays without new
    # facts and the guard skips issuance, so no competing version is created.
    repeated = forward_run.run_forward(
        config, tmp_path / "repeat", issuer=issuer, display_timezone="America/Chicago"
    )
    assert [row["status"] for row in repeated["results"]] == [
        "skipped_already_issued",
        "error",
        "skipped_already_issued",
    ]
    assert repeated["summary"]["issued"] == 0
    assert repeated["summary"]["skipped"] == 2
    assert repeated["summary"]["failed"] == 1
    for index in (0, 2):
        skipped = repeated["results"][index]["skipped"]
        assert skipped["existing_issued_forecast_ids"] == [
            first["results"][index]["issued"]["issued_forecast_id"]
        ]
        assert skipped["target_reference_time"] == selection["target_reference_time"]
        assert "issued" not in repeated["results"][index]
    repeated_verification = repeated["results"][0]["verification"]
    assert repeated_verification["summary"]["verified"] == 0
    assert repeated_verification["summary"]["already_existing"] == 4
    assert repeated_verification["downloaded_bytes"] == 0
    assert repeated["results"][2]["verification"]["status"] == "nothing_to_verify"
    assert complete_storage_inventory(migrated_dsn, object_store) == after_first
    assert events == ["discover", "prepare", "discover"]
    assert not (tmp_path / "repeat" / "issuance-locations.json").exists()

    # An explicit reissue adds versions for the same window; history stays untouched.
    reissued = forward_run.run_forward(
        config,
        tmp_path / "reissue",
        issuer=issuer,
        display_timezone="America/Chicago",
        reissue=True,
    )
    assert [row["status"] for row in reissued["results"]] == ["ok", "error", "ok"]
    assert reissued["results"][0]["verification"]["summary"]["already_existing"] == 4
    assert len(transport.get_calls) == 1
    metadata_provider.assert_called_once_with(FIRST["lat"], FIRST["lon"])
    assert events == ["discover", "prepare", "discover", "discover", "prepare"]
    assert discover_provider.call_count == 3 and prepare_provider.call_count == 2
    assert json.loads((tmp_path / "reissue" / "issuance-locations.json").read_text()) == {
        "locations": [configured_locations[0], configured_locations[2]]
    }

    saved_ids = set()
    for run in (first, reissued):
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
    for name in ("first", "reissue"):
        assert json.loads((tmp_path / name / "result.json").read_text())["summary"]["issued"] == 2
        assert "not_run" in (tmp_path / name / "hourly-report.md").read_text(encoding="utf-8")
    assert "Issuance skipped" in (tmp_path / "repeat" / "hourly-report.md").read_text()

    # Read-only accumulation status over the same storage, from metadata alone.
    factory = lambda: PostgresUnitOfWork(migrated_dsn)  # noqa: E731
    status = accumulation_status(
        FIRST["lat"], FIRST["lon"], now=DECISION, unit_of_work_factory=factory
    )
    assert status["issuances"]["count"] == 4
    assert status["issuances"]["distinct_target_reference_times"] == 2
    assert status["issuances"]["targets_with_multiple_versions"] == 2
    assert status["issuances"]["earliest_issued_at"] == "2026-08-30T12:00:00Z"
    assert status["issuances"]["latest_issued_at"] == "2026-08-30T18:30:00Z"
    # Historical versions: hours 13..18Z are eligible at 18:30Z and one retained
    # acquisition (12:45-18:15Z) covers them all; only 13Z and 15Z had records.
    assert status["hours"] == {
        "total": 144,
        "eligible": 12,
        "verified": 4,
        "pending": 132,
        "no_retained_observations": 0,
        "retained_observations_without_fact": 8,
    }
    assert status["verified_by_lead_bucket"] == {"1-6": 4, "7-18": 0, "19-36": 0}
    assert status["verification_facts"]["count"] == 4
    assert status["verification_facts"]["hours_with_multiple_facts"] == 0
    assert status["verification_facts"]["facts_without_usable_attributes"] == 0
    assert status["observation_coverage"]["retained_acquisitions"] == 1
    assert status["station_evidence"]["source"] == "retained_metar_acquisition"
    assert "KROS" in status["station_evidence"]["station_ids"]
    assert [row["hours"]["verified"] for row in status["per_issuance"]] == [2, 2, 0, 0]
    assert [row["hours"]["pending"] for row in status["per_issuance"]] == [30, 30, 36, 36]
    assert status == accumulation_status(
        FIRST["lat"], FIRST["lon"], now=DECISION, unit_of_work_factory=factory
    )
    untouched = accumulation_status(
        LAST["lat"], LAST["lon"], now=DECISION, unit_of_work_factory=factory
    )
    assert untouched["issuances"]["count"] == 2
    assert untouched["hours"]["verified"] == 0 and untouched["hours"]["pending"] == 72
    assert untouched["station_evidence"]["source"] is None
    assert complete_storage_inventory(migrated_dsn, object_store) == after_repeat


def test_overlapping_forward_run_is_refused_by_the_shared_lock(
    tmp_path: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application import forward_run

    forbidden = Mock(side_effect=AssertionError("An overlapping run must not verify or issue"))
    for name in ("_discover", "verify_previous", "run_selected_batch"):
        monkeypatch.setattr(forward_run, name, forbidden)
    config = write_config(tmp_path, [FIRST])
    issuer = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity=verification_tests.CODE_IDENTITY,
        clock=lambda: DECISION,
    )
    holder = PostgresIdempotencyLock(migrated_dsn)
    with holder.try_acquire(forward_run.FORWARD_RUN_LOCK):
        with pytest.raises(AdvisoryLockBusy):
            forward_run.run_forward(config, tmp_path / "overlap", issuer=issuer)
        assert not (tmp_path / "overlap").exists()
        code = forward_run.main(
            ["--config", str(config), "--output-dir", str(tmp_path / "overlap-cli")]
        )
        assert code == 3
        assert not (tmp_path / "overlap-cli").exists()
    forbidden.assert_not_called()
    # Once released, the same call proceeds to real work: verification, then discovery
    # (both refused by the fixture and isolated as this run's own errors).
    released = forward_run.run_forward(config, tmp_path / "after", issuer=issuer)
    assert forbidden.call_count == 2
    assert released["results"][0]["verification"]["status"] == "error"
    assert released["issuance_error"]["code"] == "current_issuance_failed"
    assert (tmp_path / "after" / "result.json").is_file()


def test_surface_forward_run_saves_exact_fields_and_preserves_older_temperature_version(
    tmp_path: Path,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mesoforge.application import forward_run, selected_forecast

    monkeypatch.setattr(forward_run, "datetime", DecisionClock)
    monkeypatch.setattr(batch_forecast, "SystemClock", lambda: FixedClock(DECISION))
    issuer = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity=verification_tests.CODE_IDENTITY,
        clock=lambda: DECISION,
    )
    _, older_preparation = prepared_current_fixture(tmp_path / "older-temperature")
    older_guidance = PreparedPointForecast.from_directory(
        Path(older_preparation["directory"]),
        configuration=IFS_CONFIGURATION,
        shadow_directories={
            model: Path(path) for model, path in older_preparation["shadow_directories"].items()
        },
    )
    older_baselines = {
        index: older_guidance.forecast(latitude=location["lat"], longitude=location["lon"])
        for index, location in ((0, FIRST), (2, LAST))
    }
    older_forecast = older_baselines[0]
    older_record = issuer.issue(older_forecast, batch_run_id=uuid4(), location_index=0)
    older_saved = issuer.read(older_record.issued_forecast_id)

    selection, preparation = prepared_current_fixture(
        tmp_path / "surface-guidance", surface_fields=True
    )
    pop_variable = "probability_of_precipitation_1h"
    nbm_settings = make_nbm_settings()
    nbm_transport = build_nbm_transport(
        nbm_settings, cycle=CURRENT_TARGET, leads=EXTENDED_HORIZONS, base_value=10.0
    )
    nbm_inputs = [
        acquire_nbm_lead(
            nbm_settings,
            transport=nbm_transport,
            clock=FixtureClock(),
            sleeper=FixtureSleeper(),
            cycle_date=CURRENT_TARGET.date(),
            cycle_hour=CURRENT_TARGET.hour,
            forecast_hour=horizon,
            cycle_deadline=FixtureClock().now(),
            canonical_variables=(pop_variable,),
        )
        for horizon in EXTENDED_HORIZONS
    ]
    assert len(nbm_inputs) == 36
    assert all(len(row.selected_messages) == 1 for row in nbm_inputs)
    preparation["pop_guidance"] = prepare_pop_guidance(
        tmp_path / "nbm-probability",
        settings=nbm_settings,
        target_reference_time=CURRENT_TARGET,
        source_cycle=CURRENT_TARGET,
        acquired_inputs=nbm_inputs,
        area=None,
        clock=FixtureClock(),
        selection_evidence={"fixture_notice": "Synthetic NBM GRIB inputs; no real discovery claim"},
    )
    prepared_nbm = load_pop_guidance(preparation["pop_guidance"], CURRENT_TARGET)[0][0]
    for index, horizon in enumerate(EXTENDED_HORIZONS):
        np.testing.assert_allclose(prepared_nbm[pop_variable][index], (20 + horizon) / 100)

    def discover(directory: Path) -> dict[str, Any]:
        directory.mkdir()
        (directory / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
        return selection

    discovery = Mock(side_effect=discover)
    preparation_call = Mock(return_value=preparation)
    monkeypatch.setattr(forward_run, "_discover", discovery)
    monkeypatch.setattr(selected_forecast, "prepare_selected", preparation_call)
    config = write_config(tmp_path, [{**FIRST, "name": "Surface fixture"}, OUTSIDE, LAST])
    # The older temperature-only version shares this target, so the decision-window
    # guard would skip FIRST; this test deliberately adds a richer version beside it.
    result = forward_run.run_forward(
        config,
        tmp_path / "surface-forward",
        issuer=issuer,
        display_timezone="America/Chicago",
        reissue=True,
    )
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"], result
    assert result["summary"]["issued"] == 2
    discovery.assert_called_once()
    preparation_call.assert_called_once()
    assert preparation_call.call_args.kwargs["include_pop"] is True
    assert preparation_call.call_args.args[0] == [{**FIRST, "name": "Surface fixture"}, LAST]
    assert result["preparation"]["downloaded_bytes"] == 0
    assert result["coverage"]["downloaded_bytes"] == 0
    assert "issued" not in result["results"][1]
    before_readback = complete_storage_inventory(migrated_dsn, object_store)
    assert len(before_readback["tables"]["issued_forecasts"]) == 3
    for index in (0, 2):
        row = result["results"][index]
        assert row["verification"]["status"] == "nothing_to_verify"
        assert row["verification"]["downloaded_bytes"] == 0
        forecast = row["forecast"]
        saved = issuer.read(UUID(row["issued"]["issued_forecast_id"]))
        assert saved["forecast"] == forecast
        assert forecast["current_model_set"] == preparation["current_model_set"]
        grid = saved["forecast"]["local_grid_baseline"]
        domains = grid["geometry"]["domains"]
        assert grid["geometry"]["dimensions"] == domains["context"]["dimensions"]
        assert len(grid["cells"]) == domains["context"]["node_count"]
        assert (
            sum(cell["inside_editable_domain"] for cell in grid["cells"])
            == domains["editable"]["node_count"]
        )
        assert domains["context"]["node_count"] > domains["editable"]["node_count"]
        assert any(cell["context_only"] for cell in grid["cells"])
        assert all(len(cell["hours"]) == 36 for cell in grid["cells"])
        for cell in grid["cells"]:
            for hour in cell["hours"]:
                qpf = hour["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]
                assert qpf["value"] == pytest.approx(
                    (0.0, 0.125, 0.375)[(hour["horizon_hours"] - 1) % 3]
                )
                assert qpf["interval_end"] == hour["valid_time"]
                assert datetime.fromisoformat(qpf["interval_end"]) - datetime.fromisoformat(
                    qpf["interval_start"]
                ) == timedelta(hours=1)
                pop = hour["surface"]["fields"][pop_variable]
                assert pop["value"] == pytest.approx((20 + hour["horizon_hours"]) / 100)
                assert pop["unit"] == "1" and pop["weights"] == {"NBM": 1.0}
                assert pop["interval_start"] == qpf["interval_start"]
                assert pop["interval_end"] == qpf["interval_end"]
                assert pop["threshold"] == {"value": 0.254, "unit": "kg/m^2", "comparison": "gt"}
                nbm = hour["surface"]["contributors"]["NBM"]
                assert nbm["role"] == "field_source"
                assert set(nbm["fields"]) == {pop_variable}
                assert nbm["fields"][pop_variable]["value"] == pop["value"]
        center = next(cell for cell in grid["cells"] if cell["is_forecast_point"])
        assert center["hours"] == forecast["hours"]
        assert forecast["local_grid"]["point_extraction"]["method"] == "exact_center_node"
        assert len(forecast["hours"]) == len(forecast["hourly_report"]["hours"]) == 36
        for hour, report, old_hour in zip(
            forecast["hours"],
            forecast["hourly_report"]["hours"],
            older_baselines[index]["hours"],
            strict=True,
        ):
            horizon = hour["horizon_hours"]
            # Compare the identical native temperature messages at the identical
            # coordinate. Different points have different bilinear float rounding
            # even on constant grids; comparing LAST to FIRST is not a regression check.
            assert hour["temperature"] == old_hour["temperature"]
            for source, old_source in zip(hour["sources"], old_hour["sources"], strict=True):
                assert source["raw_sha256"] == old_source["raw_sha256"]
                assert source["temperature"] == old_source["temperature"]
            assert hour["temperature"]["value"] == pytest.approx(288 + horizon, abs=1e-6)
            assert (
                report["raw_numerical_temperature"]
                == report["final_temperature"]
                == (hour["temperature"])
            )
            assert report["surface"] == hour["surface"]
            fields = hour["surface"]["fields"]
            assert fields["air_temperature_2m"]["value"] == hour["temperature"]["value"]
            assert fields["dew_point_temperature_2m"]["value"] == pytest.approx(276 + horizon)
            assert fields["dew_point_temperature_2m"]["unit"] == "K"
            assert fields["relative_humidity_2m"]["unit"] == "%"
            assert fields["eastward_wind_10m"]["value"] == pytest.approx(3.0)
            assert fields["northward_wind_10m"]["value"] == pytest.approx(4.0)
            assert fields["wind_speed_10m"]["value"] == pytest.approx(5.0)
            assert fields["wind_from_direction_10m"]["value"] == pytest.approx(216.869897645844)
            assert fields["wind_gust_10m"]["value"] == pytest.approx(8.0)
            assert fields["wind_gust_10m"]["unit"] == "m/s"
            expected_weights = (
                {"HRRR": 0.7, "GFS": 0.3} if horizon <= 18 else {"HRRR": 0.6, "GFS": 0.4}
            )
            assert fields["dew_point_temperature_2m"]["weights"] == expected_weights
            assert fields["wind_gust_10m"]["weights"] == expected_weights
            qpf = fields["liquid_equivalent_precipitation_amount_1h"]
            assert qpf["weights"] == expected_weights
            assert report["display_qpf"]["value"] == qpf["value"] / 25.4
            assert report["display_qpf"]["interval_start"] == qpf["interval_start"]
            pop = fields[pop_variable]
            assert report["display_pop"]["value"] == pytest.approx(20 + horizon)
            assert report["display_pop"]["interval_end"] == pop["interval_end"]
            assert pop["provenance"]["prepared_sha256"]
            assert pop["provenance"]["source_inputs"][0]["raw_sha256"]
            assert pop["provenance"]["source_inputs"][0]["source_lead_hours"] == horizon
            assert pop["source_cycle"] == CURRENT_TARGET.isoformat().replace("+00:00", "Z")
            assert fields["cloud_area_fraction"]["value"] is None
            assert fields["cloud_area_fraction"]["missing_reasons"]
            contributors = hour["surface"]["contributors"]
            for model, lead_offset in (("HRRR", 6), ("GFS", 12)):
                source = contributors[model]
                assert source["source_lead_hours"] == horizon + lead_offset
                dew = source["fields"]["dew_point_temperature_2m"]
                assert dew["value"] == pytest.approx(276 + horizon)
                assert dew["provenance"]["raw_sha256"]
                assert dew["provenance"]["source_lead_hours"] == horizon + lead_offset
                assert dew["provenance"]["cycle"] == selection["selected_cycles"][model].replace(
                    "+00:00", "Z"
                )
            for model in ("RAP", "IFS"):
                assert contributors[model]["role"] == "shadow"
                dew = contributors[model]["fields"]["dew_point_temperature_2m"]
                if model == "IFS" and horizon % 3:
                    assert dew["value"] is None
                    assert dew["missing_reasons"]
                else:
                    assert dew["value"] == pytest.approx(260 + horizon)
            assert contributors["IFS"]["fields"]["wind_gust_10m"]["value"] is None
            assert contributors["IFS"]["fields"]["wind_gust_10m"]["missing_reasons"]
        # Independent high-precision e(Td)/es(T): T=15.85C, Td=3.85C.
        assert forecast["hours"][0]["surface"]["fields"]["relative_humidity_2m"][
            "value"
        ] == pytest.approx(44.71519736562392)
    assert issuer.read(older_record.issued_forecast_id) == older_saved
    assert complete_storage_inventory(migrated_dsn, object_store) == before_readback
    assert "surface" not in older_saved["forecast"]["hours"][0]
    report_text = (tmp_path / "surface-forward" / "hourly-report.md").read_text(encoding="utf-8")
    assert "Native-period PoP %" in report_text
    assert "NBM native contributor (field_source; cycle" in report_text
