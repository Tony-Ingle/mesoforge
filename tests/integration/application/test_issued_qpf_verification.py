"""Real PostgreSQL/MinIO facts from explicit synthetic issued-QPF/MRMS fixtures.

The fixture issuance predates its event. These are persistence/analysis checks,
not evidence of operational forecast skill; no providers or model blends run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.automatic_qpf_verification import accumulate_window
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.issued_qpf_verification import IssuedQpfVerificationService
from mesoforge.application.prepared_mrms import MRMSHourResolver, register_bundle
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.identifiers import ArtifactId
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from tests.integration.application import test_batch_issuance as issuance_tests
from tests.integration.application.test_issued_temperature_verification import (
    assert_forecasts_unchanged,
)
from tests.support.observation_preview import complete_storage_inventory
from tests.unit.application.test_prepared_mrms import bundle
from tests.unit.verification.test_issued_qpf import LAT, LON, VALID, saved_forecast

pytestmark = pytest.mark.integration
migrated_dsn = issuance_tests.migrated_dsn
object_store = issuance_tests.object_store
ROOT = Path(__file__).resolve().parents[3]
IDENTITY = {"git_commit": "a" * 40, "synthetic_test_fixture": True}
WINDOW = {
    "latitude": LAT,
    "longitude": LON,
    "start_valid_time": VALID,
    "end_valid_time": VALID + timedelta(hours=2),
}


@pytest.fixture()
def infrastructure(
    migrated_dsn: str, object_store: S3ArtifactObjectStore, tmp_path: Path
) -> SimpleNamespace:
    factory = lambda: PostgresUnitOfWork(migrated_dsn)  # noqa: E731
    config, _ = load_configuration_source(base_path=ROOT / "configs/base.yaml")
    snapshot = ConfigurationService(factory).register(config)
    artifacts = ArtifactService(
        unit_of_work_factory=factory,
        object_store=object_store,
        idempotency_lock=PostgresIdempotencyLock(migrated_dsn),
    )
    issuer = ForecastIssuanceService(
        object_store,
        factory,
        code_identity=IDENTITY,
        clock=lambda: datetime(2026, 9, 24, 10, 30, tzinfo=UTC),
    )
    verifier = IssuedQpfVerificationService(
        artifacts,
        issuer=issuer,
        unit_of_work_factory=factory,
        configuration=lambda: snapshot,
        code_identity=IDENTITY,
    )
    directory, _, transport = bundle(tmp_path, amount=1.5)
    extraction = register_bundle(
        directory,
        latitude=LAT,
        longitude=LON,
        artifacts=artifacts,
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
    )
    assert len(transport.calls) == 3  # Generated GRIB messages, never external HTTP.
    issued = issuer.issue(saved_forecast()["forecast"], batch_run_id=uuid4(), location_index=0)
    return SimpleNamespace(
        factory=factory,
        artifacts=artifacts,
        issuer=issuer,
        verifier=verifier,
        extraction_id=ArtifactId(extraction["extraction_artifact_id"]),
        issued=issued,
        dsn=migrated_dsn,
        objects=object_store,
    )


def test_concurrent_fact_repeat_and_readback_preserve_one_immutable_result(
    infrastructure: SimpleNamespace,
) -> None:
    env = infrastructure
    original = env.issuer.read(env.issued.issued_forecast_id)
    before = complete_storage_inventory(env.dsn, env.objects)
    ready = Barrier(2)

    def verify() -> dict[str, Any]:
        ready.wait(timeout=20)
        return env.verifier.verify(
            env.issued.issued_forecast_id, VALID, extraction_id=env.extraction_id
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: verify(), range(2)))
    first, second = responses
    assert first["verification_id"] == second["verification_id"]
    assert sorted(row["already_existing"] for row in responses) == [False, True]
    assert first["result"] == second["result"]
    fact = first["result"]
    assert fact["status"] == "verified"
    assert fact["forecast"]["amount_mm"] == 2.5
    assert fact["observation"]["amount_mm"] == 1.5
    assert fact["qpf_error_mm"] == 1.0
    assert fact["issued_forecast_id"] == str(env.issued.issued_forecast_id)
    assert fact["issued_forecast_digest"] == str(env.issued.content_digest)
    assert fact["observation"]["semantic_revision_digest"].startswith("sha256:")
    assert first["byte_size"] < 12_000
    after = complete_storage_inventory(env.dsn, env.objects)
    facts = [
        row
        for row in after["tables"]["artifacts"]
        if row["artifact_type"] == "issued-qpf-verification"
    ]
    assert len(facts) == 1
    assert len(after["objects"]) == len(before["objects"]) + 1
    assert len(after["tables"]["activities"]) == len(before["tables"]["activities"]) + 1
    assert_forecasts_unchanged(before, after, env.objects)
    repeated = env.verifier.verify(
        env.issued.issued_forecast_id, VALID, extraction_id=env.extraction_id
    )
    assert repeated["already_existing"] is True
    assert repeated["result"] == fact
    assert env.verifier.read(ArtifactId(first["verification_id"]))["result"] == fact
    assert env.issuer.read(env.issued.issued_forecast_id) == original
    assert complete_storage_inventory(env.dsn, env.objects) == after


def test_window_preserves_versions_excludes_missing_and_queries_exact_coordinate_interval(
    infrastructure: SimpleNamespace,
) -> None:
    env = infrastructure
    second = env.issuer.issue(saved_forecast()["forecast"], batch_run_id=uuid4(), location_index=1)
    elsewhere = saved_forecast()["forecast"]
    elsewhere["latitude"] = LAT + 0.01
    other = env.issuer.issue(elsewhere, batch_run_id=uuid4(), location_index=2)
    env.verifier.verify(other.issued_forecast_id, VALID)
    before = complete_storage_inventory(env.dsn, env.objects)
    result = env.verifier.verify_window(**WINDOW, extraction_ids=[env.extraction_id])
    assert result["summary"] == {"verified": 2, "excluded": 2}
    assert result["provider_calls"] == result["already_existing"] == 0
    assert {row["issued_forecast_id"] for row in result["results"]} == {
        str(env.issued.issued_forecast_id),
        str(second.issued_forecast_id),
    }
    for row in result["results"]:
        assert row["reasons"] == (["observation_missing"] if row["status"] == "excluded" else [])
    with env.factory() as uow:
        selected = uow.artifacts.find_issued_qpf_verifications(**WINDOW)
        earlier = uow.artifacts.find_issued_qpf_verifications(
            **{**WINDOW, "end_valid_time": VALID + timedelta(hours=1)}
        )
        later = uow.artifacts.find_issued_qpf_verifications(
            **{**WINDOW, "start_valid_time": VALID + timedelta(hours=1)}
        )
        empty = uow.artifacts.find_issued_qpf_verifications(**{**WINDOW, "longitude": LON + 1})
        limited = uow.artifacts.find_issued_qpf_verifications(**WINDOW, limit=1)
    assert len(selected) == 4
    assert len(earlier) == len(later) == 2
    assert not {row.artifact_id for row in earlier} & {row.artifact_id for row in later}
    assert not empty
    assert limited == selected[:1]
    assert {row.artifact_type for row in selected} == {"issued-qpf-verification"}
    after = complete_storage_inventory(env.dsn, env.objects)
    repeated = env.verifier.verify_window(**WINDOW, extraction_ids=[env.extraction_id])
    assert repeated["already_existing"] == 4
    assert repeated["summary"] == result["summary"]
    assert complete_storage_inventory(env.dsn, env.objects) == after
    assert_forecasts_unchanged(before, after, env.objects)


def test_analysis_attributes_equal_verified_payloads_without_writes_or_forecast_reads(
    infrastructure: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = infrastructure
    env.verifier.verify_window(**WINDOW, extraction_ids=[env.extraction_id])
    before = complete_storage_inventory(env.dsn, env.objects)
    forbidden = Mock(
        side_effect=AssertionError("Read-only analysis attempted a write/forecast read")
    )
    monkeypatch.setattr(env.issuer, "read", forbidden)
    monkeypatch.setattr(env.issuer, "issue", forbidden)
    monkeypatch.setattr(env.artifacts, "execute_raw_transformation", forbidden)
    monkeypatch.setattr(env.objects, "put_if_absent", forbidden)
    with monkeypatch.context() as attributes_only:
        attributes_only.setattr(env.artifacts, "load_verified_payload", forbidden)
        fast = env.verifier.analyze_window(**WINDOW, contributors=["HRRR", "GFS"])
    payload = env.verifier.analyze_window(**WINDOW, contributors=["HRRR", "GFS"], payload_only=True)
    assert fast["storage"]["payload_bytes_read"] == 0
    assert fast["storage"]["analytical_attribute_bytes_read"] > 0
    assert payload["storage"]["payload_bytes_read"] > 0
    assert fast["canonicalization"]["stored_facts"] == 2
    assert fast["canonicalization"]["canonical_samples"] == 1
    assert fast["eligible_issued_stage_opportunities"] == 2
    assert fast["canonicalization"]["excluded_facts"][0]["reason"] == "observation_missing"
    summary = fast["stages"]["final_issued"]
    assert summary["overall"]["n"] == 1
    assert summary["overall"]["mae_mm"] == summary["overall"]["rmse_mm"] == 1.0
    for analysis in (fast, payload):
        analysis.pop("storage")
        analysis.pop("analysis_seconds")
    assert fast == payload
    assert complete_storage_inventory(env.dsn, env.objects) == before
    forbidden.assert_not_called()


def test_automatic_accumulation_concurrent_repeat_reuses_source_and_exact_facts(
    infrastructure: SimpleNamespace,
    tmp_path: Path,
) -> None:
    """Concurrent coordinators reuse native evidence and PostgreSQL fact locks."""
    env = infrastructure
    configuration = env.verifier.configuration()
    transport = Mock(side_effect=AssertionError("Retained MRMS must not download again"))
    resolver = MRMSHourResolver(
        tmp_path / "mrms-cache",
        artifacts=env.artifacts,
        unit_of_work_factory=env.factory,
        configuration_snapshot_id=configuration.configuration_snapshot_id,
        configuration_digest=configuration.configuration_digest,
        idempotency_lock=PostgresIdempotencyLock(env.dsn),
        transport=transport,
    )
    before = complete_storage_inventory(env.dsn, env.objects)
    barrier = Barrier(2)

    def resolve(**kwargs):
        barrier.wait(timeout=20)
        return resolver.resolve_hour(**kwargs)

    kwargs = {
        "service": env.verifier,
        "latitude": LAT,
        "longitude": LON,
        "start_valid_time": VALID,
        "end_valid_time": VALID + timedelta(hours=1),
        "max_issuances": 2,
        "max_opportunities": 4,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: accumulate_window(resolve_hour=resolve, **kwargs), range(2))
        )
    ids = [{row["verification_id"] for row in result["results"]} for result in results]
    assert ids[0] == ids[1] and len(ids[0]) == 2  # Baseline/final stay independent.
    assert all(result["summary"] == {"matched": 2} for result in results)
    assert all(result["acquisition"]["provider_calls"] == 0 for result in results)
    after = complete_storage_inventory(env.dsn, env.objects)
    assert len(after["objects"]) == len(before["objects"]) + 2
    assert_forecasts_unchanged(before, after, env.objects)
    forbidden = Mock(side_effect=AssertionError("Complete opportunities must bypass resolution"))
    repeated = accumulate_window(resolve_hour=forbidden, **kwargs)
    assert repeated["summary"] == {"already_existing": 2}
    forbidden.assert_not_called()
    assert complete_storage_inventory(env.dsn, env.objects) == after
    # Another coordinate extracts from the SAME registered raw hour; no new raw artifacts.
    other = resolver.resolve_hour(latitude=LAT + 0.001, longitude=LON, product_time=VALID)
    assert other["acquisition"]["raw_reused"] is True
    assert other["acquisition"]["provider_calls"] == 0
    assert other["extraction_artifact_id"] != str(env.extraction_id)
    latest = complete_storage_inventory(env.dsn, env.objects)
    assert len(latest["objects"]) == len(after["objects"]) + 1
    assert_forecasts_unchanged(before, latest, env.objects)
    transport.assert_not_called()
    transport.get.assert_not_called()
