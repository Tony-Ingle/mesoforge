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
from mesoforge.application.issuance import ForecastIssuanceService
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
