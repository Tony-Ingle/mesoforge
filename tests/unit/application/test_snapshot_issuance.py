"""Snapshot issuance boundaries; scientific grid calculations have separate coverage."""

from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from mesoforge.application import forecast_from_snapshot as fast
from mesoforge.application.issuance import ForecastExpiredError, ForecastIssuanceService
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory

REFERENCE = datetime(2026, 9, 17, 12, tzinfo=UTC)
REQUEST = REFERENCE + timedelta(minutes=30)
LOCATIONS = [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 45.85, "lon": -93.05},
    {"lat": 45.9, "lon": -93.0},
]


def fixture_forecast(latitude: float, longitude: float) -> dict[str, Any]:
    """Minimal saved canvas; this fixture makes no scientific/provider claims."""
    return {
        "latitude": latitude,
        "longitude": longitude,
        "target_reference_time": REFERENCE.isoformat(),
        "data_kind": "synthetic_demonstration",
        "hours": [
            {
                "horizon_hours": hour,
                "valid_time": (REFERENCE + timedelta(hours=hour)).isoformat(),
                "temperature": {"value": 280.0 + hour, "units": "K"},
            }
            for hour in range(1, 37)
        ],
    }


def install_snapshot(monkeypatch: pytest.MonkeyPatch, root: Path) -> Mock:
    """Isolate storage orchestration from disk/grid work; retain the real issuance path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {
                        "model": "HRRR",
                        "grib_available_at": REFERENCE.isoformat(),
                        "index_available_at": REFERENCE.isoformat(),
                        "grib_retrieved_at": REFERENCE.isoformat(),
                        "index_retrieved_at": REFERENCE.isoformat(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    preparation = {
        "directory": str(root),
        "current_model_set": {"selection": {"decision_time": REFERENCE.isoformat()}},
    }
    manifest = {
        "snapshot_id": "synthetic-issuance-fixture",
        "completed_at": REFERENCE.isoformat(),
        "coverage": {
            "reference_time": REFERENCE.isoformat(),
            "decision_time": REFERENCE.isoformat(),
            "first_valid_time": (REFERENCE + timedelta(hours=1)).isoformat(),
            "last_valid_time": (REFERENCE + timedelta(hours=36)).isoformat(),
            "prepared_hours": 36,
            "policy": {"id": "fixture"},
        },
        "contributors": {"NBM": {"products": {}}},
        "field_policies": {},
    }
    pointer = {"published_at": REFERENCE.isoformat(), "manifest_sha256": "0" * 64}
    monkeypatch.setattr(fast, "resolve_latest_complete", lambda _: (pointer, manifest, root))
    monkeypatch.setattr(fast, "verify_prepared_run", lambda _: preparation)
    monkeypatch.setattr(
        fast, "coverage_for", lambda *_: {"usable": True, "nbm_active_products": {}}
    )
    calculate = Mock(side_effect=lambda **coords: fixture_forecast(**coords))
    prepared = SimpleNamespace(reference_view=lambda _: SimpleNamespace(forecast=calculate))
    monkeypatch.setattr(fast, "load_preparation", lambda _: prepared)
    monkeypatch.setattr(fast, "build_hourly_report", lambda *_args, **_kwargs: {})
    return calculate


def test_lookup_failure_isolated_after_success_and_before_later_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calculate = install_snapshot(monkeypatch, tmp_path)
    objects = InMemoryObjectStore()
    uow = InMemoryUnitOfWorkFactory()
    service = ForecastIssuanceService(objects, uow, code_identity={}, clock=lambda: REQUEST)
    lookup = service.find_versions

    def find_versions(**coords):
        if coords["latitude"] == LOCATIONS[1]["lat"]:
            raise RuntimeError("private storage details must not escape")
        return lookup(**coords)

    monkeypatch.setattr(service, "find_versions", find_versions)
    result = fast.forecast_from_snapshot(
        tmp_path, LOCATIONS, request_time=REQUEST, issue=True, issuer=service, run_lock=nullcontext
    )
    assert [row["status"] for row in result["results"]] == ["ok", "error", "ok"]
    assert result["results"][1]["error"]["code"] == "issuance_lookup_failed"
    assert "private storage" not in str(result)
    assert result["summary"] == {"ok": 2, "issued": 2, "skipped": 0, "failed": 1}
    assert calculate.call_count == 2
    assert len(objects.objects) == len(uow.issued_forecasts) == 2
    for index in (0, 2):
        row = result["results"][index]
        saved = service.read(UUID(row["issued"]["issued_forecast_id"]))
        assert saved["forecast"] == row["forecast"]
        assert saved["forecast"]["prepared_snapshot"]["issuance_mode"] == "primary"


def test_read_only_forecast_does_not_acquire_storage_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_snapshot(monkeypatch, tmp_path)
    forbidden = Mock(side_effect=AssertionError("Read-only request acquired an issuance lock"))
    monkeypatch.setattr(fast, "acquire_issuance_run_lock", forbidden)
    result = fast.forecast_from_snapshot(tmp_path, LOCATIONS[:1], request_time=REQUEST)
    assert result["summary"]["ok"] == 1
    assert result["results"][0]["forecast"]["prepared_snapshot"]["issuance_mode"] is None
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "cutoff",
    [(REQUEST + timedelta(seconds=1)).isoformat(), REQUEST.replace(tzinfo=None).isoformat()],
)
def test_issuance_refuses_invalid_analysis_cutoff_before_any_storage_write(cutoff: str) -> None:
    objects = InMemoryObjectStore()
    uow = InMemoryUnitOfWorkFactory()
    service = ForecastIssuanceService(objects, uow, code_identity={}, clock=lambda: REQUEST)
    forecast = fixture_forecast(45.8, -93.1)
    forecast["prepared_snapshot"] = {"forecast_analysis_cutoff": cutoff}
    with pytest.raises(ValueError, match="cutoff"):
        service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert not objects.objects and not uow.issued_forecasts


def test_historical_issuance_without_analysis_cutoff_reads_back_unchanged() -> None:
    service = ForecastIssuanceService(
        InMemoryObjectStore(), InMemoryUnitOfWorkFactory(), code_identity={}, clock=lambda: REQUEST
    )
    forecast = fixture_forecast(45.8, -93.1)
    original = deepcopy(forecast)
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert forecast == original
    assert service.read(record.issued_forecast_id)["forecast"] == original


@pytest.mark.parametrize("offset_microseconds", [-1, 0, 1])
def test_prospective_baseline_requires_first_hour_strictly_after_issuance_clock(
    offset_microseconds: int,
) -> None:
    objects, uow = InMemoryObjectStore(), InMemoryUnitOfWorkFactory()
    first_valid = REFERENCE + timedelta(hours=1)
    issued_at = first_valid + timedelta(microseconds=offset_microseconds)
    service = ForecastIssuanceService(objects, uow, code_identity={}, clock=lambda: issued_at)
    forecast = fixture_forecast(45.8, -93.1)
    forecast["baseline_snapshot"] = {
        "reference_time_source": "request_hour",
        "forecast_analysis_cutoff": REQUEST.isoformat(),
    }
    original = deepcopy(forecast)
    if offset_microseconds >= 0:
        with pytest.raises(ForecastExpiredError, match="no longer future"):
            service.issue(forecast, batch_run_id=uuid4(), location_index=0)
        assert not objects.objects and not objects.metadata
        assert not uow.stored_objects and not uow.issued_forecasts
    else:
        record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
        assert record.issued_at == issued_at
        assert service.read(record.issued_forecast_id)["forecast"] == original
    assert forecast == original


@pytest.mark.parametrize("reference_source", [None, "explicit"])
def test_explicit_replay_and_legacy_baseline_issuance_remain_readable(
    reference_source: str | None,
) -> None:
    service = ForecastIssuanceService(
        InMemoryObjectStore(),
        InMemoryUnitOfWorkFactory(),
        code_identity={},
        clock=lambda: REFERENCE + timedelta(days=2),
    )
    forecast = fixture_forecast(45.8, -93.1)
    forecast["baseline_snapshot"] = {"forecast_analysis_cutoff": REQUEST.isoformat()}
    if reference_source is not None:
        forecast["baseline_snapshot"]["reference_time_source"] = reference_source
    original = deepcopy(forecast)
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert service.read(record.issued_forecast_id)["forecast"] == original
    assert forecast == original


@pytest.mark.parametrize("expiry_phase", ["desk", "presentation"])
def test_hour_expiring_during_desk_or_presentation_is_not_issued_or_retried(
    monkeypatch: pytest.MonkeyPatch,
    expiry_phase: str,
) -> None:
    """The real issuance boundary rechecks time after both expensive operations."""
    objects, uow = InMemoryObjectStore(), InMemoryUnitOfWorkFactory()
    clock = SimpleNamespace(now=REQUEST)
    service = ForecastIssuanceService(objects, uow, code_identity={}, clock=lambda: clock.now)
    extract = Mock(side_effect=fixture_forecast)

    def stage(forecast):
        if expiry_phase == "desk" and forecast["latitude"] == LOCATIONS[1]["lat"]:
            clock.now = REFERENCE + timedelta(hours=1)
        return forecast, {"status": "no_policy"}

    desk = Mock(side_effect=stage)

    def present(forecast, **_kwargs):
        if expiry_phase == "presentation" and forecast["latitude"] == LOCATIONS[1]["lat"]:
            clock.now = REFERENCE + timedelta(hours=1)
        return {"text": "fixture"}

    monkeypatch.setattr(fast, "build_hourly_report", present)
    result = fast._deliver_locations(
        SimpleNamespace(forecast=extract),
        LOCATIONS,
        reference_time=REFERENCE,
        display_timezone="UTC",
        issue=True,
        issuer=service,
        reissue=False,
        run_lock=nullcontext,
        lineage={
            "baseline_snapshot": {
                "reference_time_source": "request_hour",
                "forecast_analysis_cutoff": REQUEST.isoformat(),
            }
        },
        build_timing_key="baseline_extraction_seconds",
        stage_processor=desk,
    )
    assert result["summary"] == {"ok": 1, "issued": 1, "skipped": 0, "failed": 2}
    assert [row["status"] for row in result["results"]] == ["ok", "error", "error"]
    for row in result["results"][1:]:
        assert row["error"]["code"] == "forecast_expired"
        assert "issued" not in row
        assert len(row["forecast"]["hours"]) == 36
        assert row["forecast"]["target_reference_time"] == REFERENCE.isoformat()
    assert extract.call_count == desk.call_count == len(LOCATIONS)
    assert len(objects.objects) == len(uow.stored_objects) == len(uow.issued_forecasts) == 1
