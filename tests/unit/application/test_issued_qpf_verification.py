"""QPF facts use existing immutable storage; window/analysis never construct forecasts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application.artifacts import ArtifactService, SourceRegistrationRequest
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.issued_qpf_verification import IssuedQpfVerificationService
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.identifiers import ArtifactId, Digest, IssuedForecastId
from mesoforge.contracts.artifacts import Availability
from mesoforge.storage.json import CanonicalJsonSerializer
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)
from tests.unit.verification.test_issued_qpf import (
    CUTOFF,
    LAT,
    LON,
    VALID,
    observation,
    saved_forecast,
)


@pytest.fixture
def case(monkeypatch):
    factory, objects = InMemoryUnitOfWorkFactory(), InMemoryObjectStore()
    config, _ = load_configuration_source(base_path=Path("configs/base.yaml"))
    snapshot = ConfigurationService(factory).register(config)
    artifacts = ArtifactService(
        unit_of_work_factory=factory,
        object_store=objects,
        idempotency_lock=InMemoryIdempotencyLock(),
    )
    template = saved_forecast()
    issuer = ForecastIssuanceService(
        objects,
        factory,
        code_identity={"fixture": True},
        clock=lambda: datetime.fromisoformat(template["issued_at"]),
    )
    record = issuer.issue(template["forecast"], batch_run_id=uuid4(), location_index=0)
    extract, _ = observation()
    raw = CanonicalJsonSerializer().serialize(extract)
    available = VALID + timedelta(hours=2, minutes=1)
    manifest = artifacts.register_source(
        SourceRegistrationRequest(
            source_authority="synthetic-contract-fixture",
            source_locator="fixture://mrms-extraction",
            source_revision="v1",
            artifact_type="mrms-coordinate-extraction",
            artifact_schema_version="mesoforge.mrms-coordinate-extraction.v1",
            media_type="application/json",
            expected_content_digest=Digest.of_bytes(raw),
            created_at=available,
            availability=Availability(
                available_at=available,
                ingested_at=available,
                authority="test",
                method="synthetic-fixture",
            ),
            configuration_snapshot_id=snapshot.configuration_snapshot_id,
            configuration_digest=snapshot.configuration_digest,
            code_revision="a" * 40,
            environment_digest=Digest.of_bytes(b"test"),
        ),
        raw,
    )

    def find(self, *, latitude, longitude, start_valid_time, end_valid_time, limit):
        return tuple(
            m
            for m in factory.artifacts.values()
            if m.artifact_type == "issued-qpf-verification"
            and m.attributes["latitude"] == latitude
            and m.attributes["longitude"] == longitude
            and start_valid_time
            <= datetime.fromisoformat(m.attributes["valid_time"])
            < end_valid_time
        )[:limit]

    with factory() as uow:
        monkeypatch.setattr(
            type(uow.artifacts), "find_issued_qpf_verifications", find, raising=False
        )
    clock = Mock(return_value=CUTOFF)
    service = IssuedQpfVerificationService(
        artifacts,
        issuer=issuer,
        unit_of_work_factory=factory,
        configuration=lambda: snapshot,
        code_identity={"git_commit": "a" * 40},
        clock=clock,
    )
    return SimpleNamespace(
        factory=factory,
        objects=objects,
        issuer=issuer,
        record=record,
        extraction=manifest,
        service=service,
        clock=clock,
    )


def test_immutable_facts_idempotent_across_retry_clock_and_missing_opportunity(case):
    original = deepcopy(case.factory.issued_forecasts)
    payload = case.issuer.read(case.record.issued_forecast_id)
    first = case.service.verify(
        IssuedForecastId(str(case.record.issued_forecast_id)),
        VALID,
        extraction_id=case.extraction.artifact_id,
    )
    assert first["result"]["status"] == "verified"
    assert first["result"]["qpf_error_mm"] == 1
    case.clock.return_value = CUTOFF + timedelta(days=1)
    again = case.service.verify(
        case.record.issued_forecast_id, VALID, extraction_id=case.extraction.artifact_id
    )
    assert again["already_existing"] is True
    assert again["verification_id"] == first["verification_id"]
    assert again["result"] == first["result"]
    missing = case.service.verify(case.record.issued_forecast_id, VALID + timedelta(hours=1))
    assert missing["result"]["status"] == "excluded"
    assert "observation_missing" in missing["result"]["reasons"]
    assert case.service.read(ArtifactId(first["verification_id"]))["result"] == first["result"]
    assert case.factory.issued_forecasts == original
    assert case.issuer.read(case.record.issued_forecast_id) == payload


def test_window_preserves_stages_and_analysis_reads_no_forecast_or_writes(case, monkeypatch):
    kwargs = dict(
        latitude=LAT,
        longitude=LON,
        start_valid_time=VALID,
        end_valid_time=VALID + timedelta(hours=2),
        stages=("baseline", "final_issued"),
    )
    result = case.service.verify_window(**kwargs, extraction_ids=[case.extraction.artifact_id] * 2)
    assert result["summary"] == {"verified": 2, "excluded": 2}
    assert len(result["results"]) == 4
    repeated = case.service.verify_window(**kwargs, extraction_ids=[case.extraction.artifact_id])
    assert repeated["already_existing"] == 4
    state = deepcopy((case.factory.artifacts, case.factory.activities, case.objects.objects))
    monkeypatch.setattr(
        case.issuer, "read", Mock(side_effect=AssertionError("analysis must not read forecast"))
    )
    monkeypatch.setattr(
        case.objects, "put_if_absent", Mock(side_effect=AssertionError("analysis wrote"))
    )
    analysis = case.service.analyze_window(**kwargs)
    payload_analysis = case.service.analyze_window(**kwargs, payload_only=True)
    assert analysis["canonicalization"] == payload_analysis["canonicalization"]
    assert analysis["stages"] == payload_analysis["stages"]
    assert analysis["eligible_issued_stage_opportunities"] == 4
    assert analysis["storage"]["payload_bytes_read"] == 0
    assert state == (case.factory.artifacts, case.factory.activities, case.objects.objects)


def test_window_continues_after_one_unreadable_issuance(case, monkeypatch):
    record = case.issuer.issue(saved_forecast()["forecast"], batch_run_id=uuid4(), location_index=0)
    original = case.issuer.read

    def read(identifier):
        if identifier == case.record.issued_forecast_id:
            raise ValueError("unreadable first version")
        return original(identifier)

    monkeypatch.setattr(case.issuer, "read", read)
    result = case.service.verify_window(
        latitude=LAT,
        longitude=LON,
        start_valid_time=VALID,
        end_valid_time=VALID + timedelta(hours=1),
        extraction_ids=[case.extraction.artifact_id],
    )
    assert result["summary"] == {"error": 1, "verified": 1}
    assert any(
        r["issued_forecast_id"] == str(record.issued_forecast_id) and r["status"] == "verified"
        for r in result["results"]
    )


def test_analysis_limit_and_empty_window(case):
    kwargs = dict(
        latitude=LAT,
        longitude=LON,
        start_valid_time=VALID + timedelta(days=5),
        end_valid_time=VALID + timedelta(days=6),
    )
    assert case.service.analyze_window(**kwargs)["eligible_issued_stage_opportunities"] == 0
    with pytest.raises(ValueError, match="limit"):
        case.service.analyze_window(**kwargs, limit=0)


def test_future_cutoff_rejected_before_persistence(case):
    with pytest.raises(ValueError, match="future"):
        case.service.verify(
            case.record.issued_forecast_id, VALID, cutoff=CUTOFF + timedelta(hours=1)
        )
    assert not any(
        m.artifact_type == "issued-qpf-verification" for m in case.factory.artifacts.values()
    )
