"""Saved 36-hour final grids use real test PostgreSQL/S3 and fake SMTP only.

Explicit synthetic grids are persisted through the existing issuance contract.
They establish readback/delivery behavior, never an operational forecast or a
five-day science claim. The real-email command still refuses synthetic evidence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mesoforge.application.delivery_artifacts import ArtifactDeliveryJournal
from mesoforge.application.email_delivery import deliver
from mesoforge.application.forecast_delivery import email_content, saved_document, validate_pdf
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import PreparedPointForecast
from mesoforge.common.identifiers import Digest, IssuedForecastId
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.forecasting.field_blend import FieldBlendEngine
from mesoforge.presentation.forecast_document import (
    ForecastCoverageError,
    build_forecast_document,
)
from mesoforge.presentation.forecast_pdf import render_forecast_pdf
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from tests.integration.application import test_issued_qpf_verification as qpf_tests
from tests.integration.application.test_issued_temperature_verification import (
    assert_forecasts_unchanged,
)
from tests.support.observation_preview import complete_storage_inventory
from tests.unit.application.test_email_delivery import arguments
from tests.unit.presentation.test_forecast_product import LOCATION, five_day_saved
from tests.unit.verification.test_issued_qpf import LAT, LON

pytestmark = pytest.mark.integration
migrated_dsn = qpf_tests.migrated_dsn
object_store = qpf_tests.object_store
infrastructure = qpf_tests.infrastructure
configured_retrieval_storage = qpf_tests.issuance_tests.configured_retrieval_storage
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def final_grid_issuance(infrastructure: SimpleNamespace) -> IssuedForecastRecord:
    """Synthetic, 36-hour saved final grid in the dedicated test store only."""
    forecast = five_day_saved(reference="2026-09-24T11:00:00Z", count=36)["forecast"]
    return infrastructure.issuer.issue(forecast, batch_run_id=uuid4(), location_index=1)


def forbid_forecast_work(monkeypatch: pytest.MonkeyPatch) -> Mock:
    from mesoforge.application import forecast_desk

    forbidden = Mock(side_effect=AssertionError("Delivery attempted forecast/AI execution"))
    for owner, method in (
        (ForecastIssuanceService, "issue"),
        (PreparedPointForecast, "forecast"),
        (FieldBlendEngine, "blend_field"),
        (forecast_desk, "run_forecast_desk"),
    ):
        monkeypatch.setattr(owner, method, forbidden)
    return forbidden


def journal(env, revision="a" * 40):
    return ArtifactDeliveryJournal(env.artifacts, env.factory, env.verifier.configuration, revision)


def test_concurrent_fake_delivery_persists_one_intent_result_and_repeat_is_read_only(
    infrastructure: SimpleNamespace,
    final_grid_issuance: IssuedForecastRecord,
    monkeypatch: pytest.MonkeyPatch,
):
    env = infrastructure
    issued_id = final_grid_issuance.issued_forecast_id
    original = env.issuer.read(issued_id)
    before = complete_storage_inventory(env.dsn, env.objects)
    forbidden = forbid_forecast_work(monkeypatch)
    document = build_forecast_document(original, location=LOCATION, hours=36)
    pdf = render_forecast_pdf(document)
    assert document["fixture"] is True
    assert document["product_title"] == "36-Hour Weather Outlook"
    assert validate_pdf(pdf, document)["pages"] == 2
    assert complete_storage_inventory(env.dsn, env.objects) == before
    subject, text, rich = email_content(document)
    assert "36-Hour Weather Outlook" in subject and "5-Day" not in subject
    ready = Barrier(2)
    smtp = Mock(return_value={"status": "accepted", "phase": "data", "smtp_code": 250})

    def send(_):
        ready.wait(timeout=20)
        return deliver(
            **arguments(
                journal(env),
                issued_id=issued_id,
                attachment=pdf,
                product_version=document["document_policy"],
                subject=subject,
                text=text,
                html=rich,
            ),
            transport=smtp,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert sorted(row["status"] for row in responses) == ["accepted", "duplicate_suppressed"]
    smtp.assert_called_once()
    (attachment,) = smtp.call_args.args[0].iter_attachments()
    assert attachment.get_payload(decode=True) == pdf
    key = Digest(responses[0]["delivery_id"])
    events = journal(env).events(key)
    assert [row["event"] for row in events] == ["intent", "result"]
    assert {row["issued_forecast_id"] for row in events} == {str(issued_id)}
    assert all(row["attachment_digest"] == str(Digest.of_bytes(pdf)) for row in events)
    after = complete_storage_inventory(env.dsn, env.objects)
    with env.factory() as uow:
        manifests = uow.artifacts.find_email_delivery(key)
    assert len(manifests) == 2
    assert all(row.byte_size < 2_000 for row in manifests)
    assert len(after["objects"]) == len(before["objects"]) + 2
    assert_forecasts_unchanged(before, after, env.objects)
    assert env.issuer.read(issued_id) == original

    # Another checkout/image revision must still see the same durable send decision.
    repeated = deliver(
        **arguments(
            journal(env, "b" * 40),
            issued_id=issued_id,
            attachment=pdf,
            product_version=document["document_policy"],
            subject=subject,
            text=text,
            html=rich,
        ),
        transport=smtp,
    )
    assert repeated["status"] == "duplicate_suppressed"
    smtp.assert_called_once()
    assert journal(env).events(key) == events
    assert complete_storage_inventory(env.dsn, env.objects) == after
    assert (
        render_forecast_pdf(
            build_forecast_document(env.issuer.read(issued_id), location=LOCATION, hours=36)
        )
        == pdf
    )
    forbidden.assert_not_called()


def test_saved_36_hour_configured_product_readback_and_rerender_never_write_or_forecast(
    infrastructure: SimpleNamespace,
    final_grid_issuance: IssuedForecastRecord,
    configured_retrieval_storage: None,
    monkeypatch: pytest.MonkeyPatch,
):
    env = infrastructure
    issued_id = final_grid_issuance.issued_forecast_id
    original = env.issuer.read(issued_id)
    before = complete_storage_inventory(env.dsn, env.objects)
    forbidden = forbid_forecast_work(monkeypatch)
    for owner, method in (
        (PostgresUnitOfWork, "commit"),
        (S3ArtifactObjectStore, "put_if_absent"),
        (S3ArtifactObjectStore, "_ensure_bucket"),
    ):
        monkeypatch.setattr(owner, method, forbidden)

    def read_document():
        return saved_document(
            IssuedForecastId(str(issued_id)), ROOT / "configs/locations.json", "grasston"
        )

    first = read_document()  # Normal command defaults to the saved 36-hour product.
    assert first["issued_forecast_id"] == str(issued_id)
    assert first["fixture"] is True
    assert len(first["hours"]) == 36
    assert first["valid_start"] == original["forecast"]["target_reference_time"]
    assert first["valid_end"] == original["forecast"]["hours"][-1]["valid_time"]
    for displayed, native in zip(first["hours"], original["forecast"]["hours"], strict=True):
        assert displayed["valid_time"] == native["valid_time"]
        assert displayed["temperature"] == native["temperature"]["value"]
        qpf = native["surface"]["fields"]["liquid_equivalent_precipitation_amount_1h"]
        assert displayed["qpf"] == qpf["value"]
        assert displayed["qpf_interval"]["start"] == qpf["interval_start"]
        assert displayed["qpf_interval"]["end"] == qpf["interval_end"]
    pdf = render_forecast_pdf(first)
    assert validate_pdf(pdf, first)["pages"] == 2
    assert read_document() == first
    assert render_forecast_pdf(read_document()) == pdf
    with pytest.raises(ForecastCoverageError, match="36 hours"):
        saved_document(
            IssuedForecastId(str(issued_id)),
            ROOT / "configs/locations.json",
            "grasston",
            product="5-day",
        )
    assert env.issuer.read(issued_id) == original
    assert complete_storage_inventory(env.dsn, env.objects) == before
    forbidden.assert_not_called()


@pytest.mark.parametrize("outcome", ["failed", "ambiguous"])
def test_delivery_failure_does_not_rollback_forecast_or_retry(
    infrastructure: SimpleNamespace,
    outcome: str,
):
    env = infrastructure
    before = complete_storage_inventory(env.dsn, env.objects)
    smtp = Mock(return_value={"status": outcome, "phase": "data"})
    kwargs = arguments(journal(env), issued_id=env.issued.issued_forecast_id)
    first = deliver(**kwargs, transport=smtp)
    assert first["status"] == outcome
    after = complete_storage_inventory(env.dsn, env.objects)
    repeated = deliver(**kwargs, transport=smtp)
    assert repeated["previous_outcome"]["status"] == outcome
    smtp.assert_called_once()
    assert_forecasts_unchanged(before, after, env.objects)
    assert complete_storage_inventory(env.dsn, env.objects) == after


def test_existing_36_hour_issuance_cannot_render_five_days_or_modify_storage(
    infrastructure: SimpleNamespace,
):
    env = infrastructure
    before = complete_storage_inventory(env.dsn, env.objects)
    saved = env.issuer.read(env.issued.issued_forecast_id)
    location = {
        "name": "Existing test location",
        "lat": LAT,
        "lon": LON,
        "display_timezone": "America/Chicago",
    }
    with pytest.raises(ForecastCoverageError, match="36 hours"):
        build_forecast_document(saved, location=location)
    assert env.issuer.read(env.issued.issued_forecast_id) == saved
    assert complete_storage_inventory(env.dsn, env.objects) == before
