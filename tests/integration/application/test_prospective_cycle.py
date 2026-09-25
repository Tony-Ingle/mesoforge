"""Operator composition preserves real baseline issuance and storage idempotency."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from mesoforge.application import baseline_snapshot, forward_verification, prospective_cycle
from mesoforge.application.forecast_from_baseline import forecast_from_baseline
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.forecasting.coherence import CoherenceEngine
from mesoforge.forecasting.field_blend import FieldBlendEngine
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from tests.integration.application import test_batch_issuance as storage_tests
from tests.unit.application import test_baseline_snapshot as baseline_tests
from tests.unit.application.test_batch_forecast import FIRST, LAST
from tests.unit.application.test_prepared_temperature import TARGET

pytestmark = pytest.mark.integration
baseline_case = baseline_tests.baseline_case
migrated_dsn = storage_tests.migrated_dsn
object_store = storage_tests.object_store
configured_retrieval_storage = storage_tests.configured_retrieval_storage


def test_prospective_cycle_pins_once_isolates_locations_and_repeat_preserves_storage(
    baseline_case,
    migrated_dsn: str,
    object_store: S3ArtifactObjectStore,
    configured_retrieval_storage: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock only acquisition/build boundaries; exercise real extraction/locking/storage."""
    pointer = baseline_case["result"]["pointer"]
    now = datetime.fromisoformat(pointer["published_at"]) + timedelta(seconds=1)
    service = ForecastIssuanceService(
        object_store,
        lambda: PostgresUnitOfWork(migrated_dsn),
        code_identity={"test": "prospective-cycle-integration"},
        clock=lambda: now,
    )
    locations = [
        {**FIRST, "id": "first", "name": "First", "display_timezone": "America/Chicago"},
        {"lat": 45.85, "lon": -93.05, "id": "uncovered"},
        {**LAST, "id": "last", "name": "Last", "display_timezone": "America/Chicago"},
    ]
    config = tmp_path / "locations.json"
    config.write_text(json.dumps({"locations": locations}), encoding="utf-8")
    refreshed = Mock(
        return_value={
            "status": "published",
            "snapshot_id": baseline_case["prepared"][1]["snapshot_id"],
            "latest_complete": baseline_case["prepared"][0],
            "downloaded_bytes": 0,
        }
    )
    built = Mock(return_value=baseline_case["result"])
    monkeypatch.setattr(prospective_cycle, "refresh_guidance", refreshed)
    monkeypatch.setattr(prospective_cycle, "build_baseline", built)
    original_load = baseline_snapshot.load_baseline

    def load(_root, **kwargs):
        return original_load(baseline_case["baseline"], **kwargs)

    monkeypatch.setattr(prospective_cycle, "load_baseline", load)
    pinned_calls = []

    def deliver(_root, configured, **kwargs):
        pinned_calls.append(kwargs["baseline_pointer"])
        return forecast_from_baseline(baseline_case["baseline"], configured, **kwargs)

    monkeypatch.setattr(prospective_cycle, "forecast_from_baseline", deliver)
    verification_calls = []

    def verify(latitude, longitude, **kwargs):
        verification_calls.append((latitude, longitude, kwargs["now"]))
        if latitude == FIRST["lat"]:
            raise OSError("Retained observation service unavailable")
        return {
            "temperature": {"status": "nothing_to_verify"},
            "qpf": {"status": "nothing_to_verify"},
        }

    monkeypatch.setattr(forward_verification, "verify_previous_fields", verify)
    forbidden = Mock(side_effect=AssertionError("Location job reran baseline science"))
    monkeypatch.setattr(FieldBlendEngine, "blend_field", forbidden)
    monkeypatch.setattr(CoherenceEngine, "apply_baseline", forbidden)

    first = prospective_cycle.run_prospective_cycle(
        config,
        tmp_path / "operator",
        clock=lambda: now,
        replay_reference_time=TARGET,
        issuer=service,
    )
    assert first["summary"] == {"ok": 2, "issued": 2, "skipped": 0, "failed": 1}
    assert first["results"][0]["previous_verification"]["status"] == "error"
    assert first["results"][1]["error"]["code"] == "coverage_required"
    assert first["results"][2]["previous_verification"]["qpf"]["status"] == "nothing_to_verify"
    assert len(verification_calls) == 3
    assert all(call[2] == now for call in verification_calls)
    refreshed.assert_called_once()
    built.assert_called_once()
    assert pinned_calls == [pointer]
    saved_versions = []
    for index in (0, 2):
        issued_id = UUID(first["results"][index]["issued"]["issued_forecast_id"])
        saved = service.read(issued_id)
        forecast = saved["forecast"]
        assert (
            forecast["baseline_snapshot"]["baseline_snapshot_id"] == pointer["baseline_snapshot_id"]
        )
        assert (
            forecast["baseline_snapshot"]["prepared_snapshot_id"]
            == baseline_case["prepared"][1]["snapshot_id"]
        )
        assert len(forecast["hours"]) == 36
        coords = (saved["latitude"], saved["longitude"])
        assert forecast["hours"] == baseline_case["expected"][coords]["hours"]
        assert forecast["hourly_report"]["display_timezone"] == "America/Chicago"
        saved_versions.append((issued_id, saved))
    before = storage_tests.storage_inventory(migrated_dsn, object_store)
    assert [len(rows) for rows in before] == [2, 2, 2]

    # The uncovered middle domain remains an explicit failure. Successfully issued
    # locations retain their primary version rather than creating duplicate objects.
    repeat = prospective_cycle.run_prospective_cycle(
        config,
        tmp_path / "operator",
        clock=lambda: now,
        replay_reference_time=TARGET,
        issuer=service,
    )
    assert repeat["summary"] == {"ok": 0, "issued": 0, "skipped": 2, "failed": 1}
    assert repeat["results"][1]["error"]["code"] == "coverage_required"
    assert pinned_calls == [pointer, pointer]
    assert storage_tests.storage_inventory(migrated_dsn, object_store) == before
    assert all(service.read(issued_id) == saved for issued_id, saved in saved_versions)

    # An all-success configured subset requires no second refresh or build in the
    # same decision window; the final guard still rechecks through real storage.
    config.write_text(json.dumps({"locations": [locations[0], locations[2]]}), encoding="utf-8")
    refresh_calls, build_calls = refreshed.call_count, built.call_count
    all_existing = prospective_cycle.run_prospective_cycle(
        config,
        tmp_path / "operator",
        clock=lambda: now,
        replay_reference_time=TARGET,
        issuer=service,
    )
    assert all_existing["status"] == "completed"
    assert all_existing["summary"] == {"ok": 0, "issued": 0, "skipped": 2, "failed": 0}
    assert all_existing["background"]["status"] == "reused_for_already_issued_window"
    assert refreshed.call_count == refresh_calls
    assert built.call_count == build_calls
    assert pinned_calls == [pointer, pointer, pointer]
    assert storage_tests.storage_inventory(migrated_dsn, object_store) == before
    forbidden.assert_not_called()
