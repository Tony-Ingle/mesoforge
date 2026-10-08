"""Issuance ordering and complete forecast retention, using explicit memory doubles."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.issuance_encoding import decode_issuance, encode_issuance
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.horizon import FIVE_DAY_HORIZON
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.unit.application.test_prepared_temperature import (
    EXTENDED_HORIZONS,
    TARGET,
    prepare_fixture_guidance,
)

ISSUED_AT = datetime(2026, 9, 10, 12, tzinfo=UTC)
CODE_IDENTITY = {"git_commit": "a" * 40, "working_tree_dirty": False}


def test_extended_issuance_retains_horizon_and_selects_late_hours_without_recalculation(
    memory_service,
) -> None:
    from tests.unit.presentation.test_forecast_product import rolling_saved

    forecast = rolling_saved()["forecast"]
    service, factory, objects = memory_service
    record = service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert record.forecast_horizon_hours == FIVE_DAY_HORIZON.duration_hours
    saved = service.read(record.issued_forecast_id)
    assert saved["forecast"] == forecast
    assert saved["forecast_horizon_hours"] == FIVE_DAY_HORIZON.duration_hours
    raw = objects.objects[record.content_digest]
    assert raw.startswith(b"\x1f\x8b")
    assert record.payload_digest == canonical_json_digest(saved)
    assert record.content_digest == Digest.of_bytes(raw) != record.payload_digest
    assert len(raw) < len(canonical_json_bytes(saved)) / 4
    assert encode_issuance(saved) == (raw, record.payload_digest)
    envelope = json.loads(gzip.decompress(raw))
    assert envelope["tables"]["source_documents"] == []
    before = dict(objects.objects)
    selection = service.select_hours(
        latitude=forecast["latitude"],
        longitude=forecast["longitude"],
        start_valid_time=record.target_reference_time + timedelta(hours=119),
        end_valid_time=record.target_reference_time + timedelta(hours=121),
    )
    assert [row["hour"]["horizon_hours"] for row in selection["results"]] == [119, 120]
    assert selection["version_scan"]["versions_read"] == 1
    assert objects.objects == before and len(factory.issued_forecasts) == 1
    # A corrupt searchable header cannot alter the immutable object's meaning.
    factory.issued_forecasts[record.issued_forecast_id] = record.model_copy(
        update={"forecast_horizon_hours": 36}
    )
    with pytest.raises(IntegrityError, match="horizon differs"):
        service.read(record.issued_forecast_id)


@pytest.mark.parametrize("tamper", ["schema", "checksum", "value", "external", "malformed"])
def test_compact_issuance_rejects_tampering_without_external_file_reads(tamper, monkeypatch):
    from tests.unit.presentation.test_forecast_product import rolling_saved

    saved = rolling_saved()
    raw, digest = encode_issuance(saved)
    envelope = json.loads(gzip.decompress(raw))
    if tamper == "schema":
        envelope["schema"] = "future-unapproved-codec"
    elif tamper == "checksum":
        envelope["forecast_payload_digest"] = str(Digest.of_bytes(b"other issuance"))
    elif tamper == "value":
        envelope["payload"]["forecast"]["latitude"] += 1
    elif tamper == "external":
        envelope["tables"]["source_documents"] = [{"path": "private-file", "sha256": str(digest)}]
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("external source read"))
    modified = (
        b"unreadable" if tamper == "malformed" else gzip.compress(canonical_json_bytes(envelope))
    )
    with pytest.raises(IntegrityError, match="compact issued"):
        decode_issuance(modified, expected_logical_digest=digest)


def test_long_issuance_requires_explicit_horizon_and_exact_valid_time(memory_service) -> None:
    from tests.unit.presentation.test_forecast_product import rolling_saved

    service, factory, objects = memory_service
    forecast = rolling_saved()["forecast"]
    del forecast["forecast_horizon"]
    with pytest.raises(ValueError, match=r"1..36"):
        service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    forecast["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    forecast["hours"][-1]["valid_time"] = forecast["hours"][-2]["valid_time"]
    with pytest.raises(ValueError, match="exact lead"):
        service.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert not factory.issued_forecasts and not objects.objects


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
    assert first.forecast_horizon_hours == 36
    assert first.forecast_payload_digest is None
    assert first.payload_digest == first.content_digest
    legacy = first.model_dump()
    del legacy["forecast_horizon_hours"]
    assert IssuedForecastRecord.model_validate(legacy) == first
    for duration in (True, 0, 37, 121):
        with pytest.raises(ValidationError):
            IssuedForecastRecord.model_validate({**legacy, "forecast_horizon_hours": duration})
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
        assert "forecast_horizon_hours" not in envelope
        assert canonical_json_bytes(envelope) == store.objects[record.content_digest]
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
