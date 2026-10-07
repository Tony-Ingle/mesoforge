"""Real test PostgreSQL/S3 audit; fake SMTP only, no operational five-day claim.

The existing issuance fixture is genuinely 36 hours and must fail the five-day
render gate. Transport tests use a synthetic PDF attachment independently of the
production command's scientific coverage and operator-review gates.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mesoforge.application.delivery_artifacts import ArtifactDeliveryJournal
from mesoforge.application.email_delivery import deliver
from mesoforge.application.forecast_delivery import validate_pdf
from mesoforge.common.identifiers import Digest
from mesoforge.presentation.forecast_document import (
    ForecastCoverageError,
    build_forecast_document,
)
from mesoforge.presentation.forecast_pdf import render_forecast_pdf
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


def journal(env, revision="a" * 40):
    return ArtifactDeliveryJournal(env.artifacts, env.factory, env.verifier.configuration, revision)


def test_concurrent_fake_delivery_persists_one_intent_result_and_repeat_is_read_only(
    infrastructure: SimpleNamespace,
):
    env = infrastructure
    original = env.issuer.read(env.issued.issued_forecast_id)
    before = complete_storage_inventory(env.dsn, env.objects)
    document = build_forecast_document(five_day_saved(), location=LOCATION)
    pdf = render_forecast_pdf(document)
    assert document["fixture"] is True
    assert validate_pdf(pdf, document)["pages"] == 2
    ready = Barrier(2)
    smtp = Mock(return_value={"status": "accepted", "phase": "data", "smtp_code": 250})

    def send(_):
        ready.wait(timeout=20)
        return deliver(
            **arguments(journal(env), issued_id=env.issued.issued_forecast_id, attachment=pdf),
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
    assert {row["issued_forecast_id"] for row in events} == {str(env.issued.issued_forecast_id)}
    assert all(row["attachment_digest"] == str(Digest.of_bytes(pdf)) for row in events)
    after = complete_storage_inventory(env.dsn, env.objects)
    with env.factory() as uow:
        manifests = uow.artifacts.find_email_delivery(key)
    assert len(manifests) == 2
    assert all(row.byte_size < 2_000 for row in manifests)
    assert len(after["objects"]) == len(before["objects"]) + 2
    assert_forecasts_unchanged(before, after, env.objects)
    assert env.issuer.read(env.issued.issued_forecast_id) == original

    # Another checkout/image revision must still see the same durable send decision.
    repeated = deliver(
        **arguments(
            journal(env, "b" * 40), issued_id=env.issued.issued_forecast_id, attachment=pdf
        ),
        transport=smtp,
    )
    assert repeated["status"] == "duplicate_suppressed"
    smtp.assert_called_once()
    assert journal(env).events(key) == events
    assert complete_storage_inventory(env.dsn, env.objects) == after
    assert render_forecast_pdf(document) == pdf


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
