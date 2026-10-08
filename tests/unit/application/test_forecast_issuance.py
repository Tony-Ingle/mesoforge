"""Issuance ordering and complete forecast retention, using explicit memory doubles."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    TARGET,
    prepare_fixture_guidance,
)

ISSUED_AT = datetime(2026, 9, 10, 12, tzinfo=UTC)
CODE_IDENTITY = {"git_commit": "a" * 40, "working_tree_dirty": False}


@pytest.fixture(scope="module")
def prepared_guidance(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("issuance-guidance")
    prepare_fixture_guidance(directory, EXTENDED_HORIZONS)
    return directory


@pytest.fixture()
def forecast(prepared_guidance: Path) -> dict[str, Any]:
    return PreparedPointForecast.from_directory(prepared_guidance).forecast(
        latitude=45.8, longitude=-93.1
    )


@pytest.fixture()
def memory_service():
    store = InMemoryObjectStore()
    factory = InMemoryUnitOfWorkFactory()
    service = ForecastIssuanceService(
        store, factory, code_identity=CODE_IDENTITY, clock=lambda: ISSUED_AT
    )
    return service, factory, store


def test_two_issuances_preserve_every_field_and_are_distinct_even_at_the_same_time(
    forecast: dict[str, Any], memory_service
) -> None:
    service, factory, store = memory_service
    original = json.loads(json.dumps(forecast))
    first_run, second_run = uuid4(), uuid4()
    first = service.issue(forecast, batch_run_id=first_run, location_index=0)
    assert IssuedForecastRecord.model_validate_json(first.model_dump_json()) == first
    # Native UUIDs are the historical v1 Python/storage contract; arbitrary text
    # must still fail rather than becoming an unchecked identifier boundary.
    for name in ("issued_forecast_id", "batch_run_id"):
        with pytest.raises(ValidationError):
            IssuedForecastRecord.model_validate({**first.model_dump(), name: "not-a-uuid"})
    first_bytes = store.objects[first.content_digest]
    second = service.issue(forecast, batch_run_id=second_run, location_index=0)

    assert first.issued_forecast_id != second.issued_forecast_id
    assert first.batch_run_id == first_run
    assert second.batch_run_id == second_run
    assert first.content_digest != second.content_digest
    assert first.issued_at == second.issued_at == ISSUED_AT
    assert first.target_reference_time == TARGET
    assert (first.latitude, first.longitude) == (45.8, -93.1)
    assert len(factory.issued_forecasts) == len(factory.stored_objects) == len(store.objects) == 2
    assert store.objects[first.content_digest] == first_bytes
    assert first.content_digest == "sha256:" + hashlib.sha256(first_bytes).hexdigest()

    for record in (first, second):
        envelope = service.read(record.issued_forecast_id)
        assert envelope["schema_version"] == "issued-forecast.v1"
        assert envelope["issued_forecast_id"] == str(record.issued_forecast_id)
        assert envelope["batch_run_id"] == str(record.batch_run_id)
        assert envelope["location_index"] == 0
        assert envelope["issued_at"] == "2026-09-10T12:00:00Z"
        assert envelope["target_reference_time"] == "2026-08-30T12:00:00Z"
        assert envelope["latitude"] == 45.8
        assert envelope["longitude"] == -93.1
        assert envelope["code_identity"] == CODE_IDENTITY
        # Full equality covers all hours, values, units, cycles, weights, source/raw/
        # prepared checksums, valid times, notices and explicit missing reasons.
        assert envelope["forecast"] == original
        assert len(envelope["forecast"]["hours"]) == 36
    forecast["hours"][0]["temperature"]["value"] = -100.0
    assert service.read(first.issued_forecast_id)["forecast"] == original
    with pytest.raises(ValidationError, match="frozen"):
        first.latitude = 46.0


def test_missing_guidance_remains_null_with_its_reason_and_original_weights(
    prepared_guidance: Path, tmp_path: Path, memory_service
) -> None:
    directory = tmp_path / "missing-gfs"
    shutil.copytree(prepared_guidance, directory)
    (directory / "GFS.nc").unlink()
    forecast = PreparedPointForecast.from_directory(directory).forecast(
        latitude=45.8, longitude=-93.1
    )
    service, _, _ = memory_service
    issued = service.issue(forecast, batch_run_id=uuid4(), location_index=2)
    saved = service.read(issued.issued_forecast_id)["forecast"]
    assert saved == forecast
    assert len(saved["hours"]) == 36
    for hour in saved["hours"]:
        assert hour["temperature"] == {"value": None, "unit": "K"}
        assert hour["missing_reasons"] == ["GFS: prepared guidance file is missing"]
        assert [source["weight"] for source in hour["sources"]] == [0.7, 0.3]


@pytest.mark.parametrize("failure", ["upload", "readback"])
def test_failed_object_write_or_verification_never_publishes_a_header(
    forecast: dict[str, Any], memory_service, failure: str
) -> None:
    service, factory, store = memory_service
    if failure == "upload":
        store.fail_next_put = True
    else:
        store.corrupt_next_get = True
    with pytest.raises(RuntimeError if failure == "upload" else IntegrityError):
        service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert factory.issued_forecasts == {}
    assert factory.stored_objects == {}


def test_read_fails_explicitly_if_saved_bytes_are_corrupted(
    forecast: dict[str, Any], memory_service
) -> None:
    service, factory, store = memory_service
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    store.objects[record.content_digest] = b"corrupted forecast"
    with pytest.raises(IntegrityError, match="checksum"):
        service.read(record.issued_forecast_id)
    assert factory.issued_forecasts[record.issued_forecast_id] == record


def test_unknown_issued_forecast_is_explicitly_missing(memory_service) -> None:
    service, _, _ = memory_service
    with pytest.raises(NotFound):
        service.read(uuid4())
