"""Compact immutable delivery events reject unexpected content before persistence."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from mesoforge.application.delivery_artifacts import ArtifactDeliveryJournal, validate_audit
from mesoforge.application.email_delivery import deliver
from mesoforge.common.identifiers import Digest
from tests.unit.application.test_artifact_service import service_and_uow as service_and_uow
from tests.unit.application.test_email_delivery import ISSUED, Journal, arguments


def example_events():
    journal = Journal()
    deliver(**arguments(journal), transport=lambda *_: {"status": "accepted", "smtp_code": 250})
    return journal.rows


@pytest.fixture
def journal(service_and_uow, monkeypatch):
    artifacts, factory, objects = service_and_uow
    with factory() as uow:
        repository = type(uow.artifacts)

    def find(self, identity):
        return tuple(
            row
            for row in self._store.values()
            if row.artifact_type == "email-delivery"
            and row.attributes.get("delivery_id") == identity
        )

    monkeypatch.setattr(repository, "find_email_delivery", find, raising=False)
    snapshot = SimpleNamespace(
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest="sha256:" + "a" * 64,
    )
    return ArtifactDeliveryJournal(artifacts, factory, lambda: snapshot, "a" * 40)


def test_compact_audit_immutable_issue_identity_and_repeat_across_code_revision(journal):
    response = deliver(**arguments(journal), transport=lambda *_: {"status": "accepted"})
    key = Digest(response["delivery_id"])
    events = journal.events(key)
    assert [row["event"] for row in events] == ["intent", "result"]
    assert {row["issued_forecast_id"] for row in events} == {str(ISSUED)}
    with journal.factory() as uow:
        manifests = uow.artifacts.find_email_delivery(key)
    assert len(manifests) == 2
    assert all(row.byte_size < 2_000 for row in manifests)
    assert {row.code_revision for row in manifests} == {"a" * 40}
    other_revision = ArtifactDeliveryJournal(
        journal.artifacts, journal.factory, journal.configuration, "b" * 40
    )

    def forbidden(*_):
        raise AssertionError("A repeated event must not reach SMTP")

    repeated = deliver(**arguments(other_revision), transport=forbidden)
    assert repeated["status"] == "duplicate_suppressed"
    assert journal.events(key) == events
    # Caller mutation after append cannot change retained audit bytes.
    events[0]["subject"] = "modified copy"
    assert journal.events(key)[0]["subject"] == "MesoForge forecast"


@pytest.mark.parametrize(
    "updates",
    [
        {"password": "must-not-persist"},
        {"schema_version": "other.v1"},
        {"issued_forecast_id": "not-an-issued-uuid"},
        {"delivery_id": str(Digest.of_bytes(b"wrong"))},
        {"attachment_digest": "invalid"},
        {"attachment_bytes": True},
        {"attachment_bytes": 0},
        {"attachment_bytes": 8 * 1024 * 1024 + 1},
        {"provider": "unknown"},
        {"created_at": "2026-10-07T12:00:00"},
        {"subject": "Subject\r\nBcc: other@example.test"},
        {"recipient": "many@example.test,other@example.test"},
        {"product_version": "not bounded\n"},
        {"provider_result": {"status": "accepted", "phase": "data", "password": "secret"}},
        {"provider_result": {"status": "accepted", "phase": "raw-server-secret"}},
        {"provider_result": {"status": "accepted", "phase": "data", "smtp_code": "250"}},
        {"provider_result": {"status": "invented", "phase": "data"}},
    ],
)
def test_invalid_audit_never_touches_configuration_or_storage(journal, updates, monkeypatch):
    event = {**example_events()[1], **updates}

    def forbidden():
        raise AssertionError("Invalid delivery data reached persistence")

    monkeypatch.setattr(journal, "configuration", forbidden)
    with pytest.raises(ValueError):
        journal.append(event)


def test_immutable_event_cannot_be_replaced_with_different_content(journal):
    event = example_events()[0]
    original = journal.append(event)
    assert journal.append(deepcopy(event)) == original
    changed = {**event, "subject": "different attachment description"}
    with pytest.raises(ValueError, match="already has different content"):
        journal.append(changed)
    assert journal.events(Digest(event["delivery_id"])) == [event]


def test_result_without_intent_or_conflicting_pair_fails_closed(journal):
    intent, result = example_events()
    journal.append({**result, "subject": "conflicts with intent"})
    with pytest.raises(ValueError, match="sequence"):
        journal.events(Digest(intent["delivery_id"]))
    journal.append(intent)
    with pytest.raises(ValueError, match="sequence"):
        journal.events(Digest(intent["delivery_id"]))


def test_manifest_attributes_cannot_relabel_verified_body(journal):
    intent = example_events()[0]
    reference = journal.append(intent)
    with journal.factory() as uow:
        repository = uow.artifacts
        manifest = repository._store[reference["artifact_id"]]
        repository._store[reference["artifact_id"]] = manifest.model_copy(
            update={"attributes": {**manifest.attributes, "issued_forecast_id": "different"}}
        )
        uow.commit()
    with pytest.raises(ValueError, match="identity mismatch"):
        journal.events(Digest(intent["delivery_id"]))


def test_intent_has_no_result_and_result_has_no_freeform_server_response():
    intent, result = example_events()
    validate_audit(intent)
    validate_audit(result)
    with pytest.raises(ValueError, match="Unexpected"):
        validate_audit({**intent, "provider_result": result["provider_result"]})
