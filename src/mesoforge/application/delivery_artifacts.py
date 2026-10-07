"""Email audit on the existing PostgreSQL/content-addressed artifact infrastructure."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from mesoforge.application.artifacts import ArtifactService, TransformationRequest
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.email_delivery import (
    MAX_ATTACHMENT_BYTES,
    SCHEMA,
    address,
    delivery_identity,
    safe_provider_result,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.identifiers import Digest, IssuedForecastId, validate_code_revision
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

_COMMON = {
    "schema_version",
    "delivery_id",
    "issued_forecast_id",
    "recipient",
    "subject",
    "attachment_digest",
    "attachment_bytes",
    "product_version",
    "provider",
}
_ATTRIBUTES = ("delivery_id", "event", "issued_forecast_id")


def validate_audit(event: Any) -> None:
    """Reject unbounded/unknown fields before retention; no credentials or SMTP prose."""
    if not isinstance(event, dict) or event.get("event") not in ("intent", "result"):
        raise ValueError("Unsupported delivery audit event")
    expected = _COMMON | {"event", "created_at"}
    if event["event"] == "result":
        expected |= {"provider_result"}
    if set(event) != expected:
        raise ValueError("Unexpected delivery audit fields")
    strings = expected - {"attachment_bytes", "provider_result"}
    if any(not isinstance(event[name], str) or len(event[name]) > 254 for name in strings):
        raise ValueError("Invalid delivery audit text")
    if event["schema_version"] != SCHEMA or event["provider"] != "smtp":
        raise ValueError("Unsupported delivery audit contract")
    issued_id = IssuedForecastId(event["issued_forecast_id"])
    if address(event["recipient"]) != event["recipient"]:
        raise ValueError("Email audit recipient must use its canonical DNS domain")
    if Digest(event["delivery_id"]) != delivery_identity(
        issued_id, address(event["recipient"]), event["product_version"]
    ):
        raise ValueError("Email audit identity mismatch")
    Digest(event["attachment_digest"])
    if (
        type(event["attachment_bytes"]) is not int
        or not 0 < event["attachment_bytes"] <= MAX_ATTACHMENT_BYTES
    ):
        raise ValueError("Invalid delivery attachment size")
    subject = event["subject"]
    if not subject or len(subject) > 200 or "\r" in subject or "\n" in subject:
        raise ValueError("Invalid delivery subject")
    timestamp = datetime.fromisoformat(event["created_at"])
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Delivery audit timestamp must be timezone-aware")
    if event["event"] == "result":
        result = event["provider_result"]
        if not isinstance(result, dict) or result != safe_provider_result(result):
            raise ValueError("Unbounded or unsupported SMTP audit result")


class ArtifactDeliveryJournal:
    """Compact intent/result artifacts, never a mutation of the issued forecast."""

    def __init__(
        self,
        artifacts: ArtifactService,
        factory: Callable[[], Any],
        configuration: Callable[[], Any],
        code_revision: str,
    ) -> None:
        self.artifacts = artifacts
        self.factory = factory
        self.configuration = configuration
        self.code_revision = validate_code_revision(code_revision)

    def lock(self, key: Digest) -> AbstractContextManager[None]:
        key = Digest(key)
        return self.artifacts.acquire_identity(Digest.of_bytes(f"email:{key}".encode()))

    def events(self, key: Digest) -> list[dict[str, Any]]:
        key = Digest(key)
        with self.factory() as uow:
            manifests = uow.artifacts.find_email_delivery(key)
        result = []
        for manifest in manifests:
            _, raw = self.artifacts.load_verified_payload(manifest.artifact_id)
            body = CanonicalJsonSerializer().deserialize(raw)
            validate_audit(body)
            if (
                body["delivery_id"] != str(key)
                or manifest.artifact_type != "email-delivery"
                or manifest.artifact_schema_version != SCHEMA
                or manifest.media_type != "application/json"
                or manifest.attributes != {name: body[name] for name in _ATTRIBUTES}
            ):
                raise ValueError("Email audit identity mismatch")
            result.append(body)
        # A crash after intent is valid and suppresses a retry. Anything else
        # inconsistent fails closed instead of authorizing another SMTP transaction.
        if result:
            if [event["event"] for event in result] not in (["intent"], ["intent", "result"]):
                raise ValueError("Ambiguous email delivery audit sequence")
            if len(result) == 2 and (
                any(result[0][name] != result[1][name] for name in _COMMON)
                or datetime.fromisoformat(result[1]["created_at"])
                < datetime.fromisoformat(result[0]["created_at"])
            ):
                raise ValueError("Conflicting email delivery audit evidence")
        return result

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        validate_audit(event)
        raw = canonical_json_bytes(event)
        event = CanonicalJsonSerializer().deserialize(raw)
        configuration = self.configuration()
        request = TransformationRequest(
            activity_type="retain-email-delivery",
            activity_version="v1",
            inputs=(),
            output_role="delivery-audit",
            output_artifact_type="email-delivery",
            output_artifact_schema_version=SCHEMA,
            output_media_type="application/json",
            parameters={"delivery_id": str(Digest(event["delivery_id"])), "event": event["event"]},
            configuration_snapshot_id=configuration.configuration_snapshot_id,
            configuration_digest=configuration.configuration_digest,
            code_revision=self.code_revision,
            environment_digest=Digest.of_bytes(canonical_json_bytes({"schema": SCHEMA})),
            attributes={name: event[name] for name in _ATTRIBUTES},
        )

        def transform(*parents: bytes) -> dict[str, Any]:
            return event

        saved = self.artifacts.execute_raw_transformation(
            request,
            transform,
            CanonicalJsonSerializer(),
            input_loader=bytes,
            output_validator=validate_audit,
        )
        _, retained = self.artifacts.load_verified_payload(saved.output.artifact_id)
        if retained != raw:
            raise ValueError("An immutable delivery event already has different content")
        return {
            "artifact_id": str(saved.output.artifact_id),
            "content_digest": str(saved.output.content_digest),
            "byte_size": saved.output.byte_size,
        }


def configured_journal() -> ArtifactDeliveryJournal:
    root = Path(__file__).resolve().parents[3]
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    factory = cast(Any, lambda: PostgresUnitOfWork(dsn))
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    artifacts = ArtifactService(
        unit_of_work_factory=factory,
        object_store=cast(Any, objects),
        idempotency_lock=PostgresIdempotencyLock(dsn),
    )

    def configuration() -> Any:
        config, _ = load_configuration_source(base_path=root / "configs/base.yaml")
        return ConfigurationService(factory).register(config)

    return ArtifactDeliveryJournal(artifacts, factory, configuration, current_code_revision(root))
