"""Bounded SMTP delivery, independent of forecast generation and AI execution.

A durable intent precedes SMTP. Any intent suppresses automatic retries, including
after a crash or ambiguous DATA result. SMTP acceptance is not inbox confirmation.
"""

from __future__ import annotations

import math
import os
import re
import smtplib
import ssl
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from typing import Any, Protocol

from mesoforge.common.email_address import address as address
from mesoforge.common.identifiers import Digest, IssuedForecastId
from mesoforge.contracts.serialization import canonical_json_bytes

SCHEMA = "mesoforge.email-delivery.v1"
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def delivery_identity(issued_id: IssuedForecastId, recipient: str, product_version: str) -> Digest:
    issued_id = IssuedForecastId(str(issued_id))
    recipient = address(recipient)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", product_version):
        raise ValueError("Invalid delivery product version")
    return Digest.of_bytes(
        canonical_json_bytes(
            {
                "issued_forecast_id": str(issued_id),
                "recipient": recipient,
                "product_version": product_version,
            }
        )
    )


def safe_provider_result(outcome: Any) -> dict[str, Any]:
    """Only bounded transport categories and numeric SMTP codes enter the audit."""
    if not isinstance(outcome, dict) or outcome.get("status") not in (
        "accepted",
        "failed",
        "ambiguous",
    ):
        outcome = {"status": "ambiguous", "phase": "invalid_transport_result"}
    safe: dict[str, Any] = {"status": outcome["status"]}
    phases = {
        "mail",
        "recipient",
        "data",
        "connection_or_auth",
        "transport",
        "invalid_transport_result",
    }
    phase = outcome.get("phase")
    safe["phase"] = phase if isinstance(phase, str) and phase in phases else "transport"
    if type(outcome.get("smtp_code")) is int and 100 <= outcome["smtp_code"] <= 599:
        safe["smtp_code"] = outcome["smtp_code"]
    error_type = outcome.get("error_type")
    if isinstance(error_type, str) and error_type in {
        "SMTPAuthenticationError",
        "SMTPConnectError",
        "SMTPDataError",
        "SMTPHeloError",
        "SMTPResponseException",
        "SMTPSenderRefused",
    }:
        safe["error_type"] = error_type
    return safe


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    sender: str
    security: str = "starttls"
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        address(self.sender)
        if not self.host or any(c.isspace() for c in self.host) or not 1 <= self.port <= 65535:
            raise ValueError("Invalid SMTP host or port")
        if self.security not in {"starttls", "tls", "local_plaintext"}:
            raise ValueError("SMTP security must be starttls, tls or local_plaintext")
        if self.security == "local_plaintext" and self.host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("Plaintext SMTP is restricted to a local test server")
        if bool(self.username) != bool(self.password):
            raise ValueError("SMTP username and password must be provided together")
        if self.security == "local_plaintext" and self.password:
            raise ValueError("Credentials cannot be sent over plaintext SMTP")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60:
            raise ValueError("SMTP timeout must be in (0, 60] seconds")

    @classmethod
    def environment(cls, values: Mapping[str, str] | None = None) -> SmtpSettings:
        env = os.environ if values is None else values
        return cls(
            host=env.get("MESOFORGE_SMTP_HOST", ""),
            port=int(env.get("MESOFORGE_SMTP_PORT", "587")),
            sender=env.get("MESOFORGE_EMAIL_FROM", ""),
            security=env.get("MESOFORGE_SMTP_SECURITY", "starttls"),
            username=env.get("MESOFORGE_SMTP_USERNAME") or None,
            password=env.get("MESOFORGE_SMTP_PASSWORD") or None,
            timeout_seconds=float(env.get("MESOFORGE_SMTP_TIMEOUT_SECONDS", "30")),
        )


class DeliveryJournal(Protocol):
    def lock(self, key: Digest) -> AbstractContextManager[None]: ...
    def events(self, key: Digest) -> list[dict[str, Any]]: ...
    def append(self, event: dict[str, Any]) -> dict[str, Any]: ...


def message(
    *,
    settings: SmtpSettings,
    recipient: str,
    subject: str,
    text: str,
    html: str,
    attachment: bytes,
    delivery_id: Digest,
    timestamp: datetime,
) -> EmailMessage:
    recipient = address(recipient)
    if not subject or len(subject) > 200 or "\r" in subject or "\n" in subject:
        raise ValueError("Invalid email subject")
    if not attachment.startswith(b"%PDF-") or not 0 < len(attachment) <= MAX_ATTACHMENT_BYTES:
        raise ValueError("Expected an email-sized validated PDF")
    if timestamp.tzinfo is None:
        raise ValueError("Delivery timestamp must be timezone-aware")
    mail = EmailMessage(policy=SMTP)
    mail["From"], mail["To"], mail["Subject"] = settings.sender, recipient, subject
    mail["Date"] = format_datetime(timestamp)
    mail["Message-ID"] = f"<{str(delivery_id).split(':')[-1]}@mesoforge.local>"
    mail.set_content(text)
    mail.add_alternative(html, subtype="html")
    mail.add_attachment(
        attachment, maintype="application", subtype="pdf", filename="mesoforge-forecast.pdf"
    )
    return mail


def smtp_send(mail: EmailMessage, settings: SmtpSettings) -> dict[str, Any]:
    """No retry. Sanitize failures: server text and credentials never become audit data."""
    client: smtplib.SMTP | None = None
    data_started = False
    try:
        if settings.security == "tls":
            client = smtplib.SMTP_SSL(
                settings.host,
                settings.port,
                timeout=settings.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)
        client.ehlo()
        if settings.security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if settings.username and settings.password:
            client.login(settings.username, settings.password)
        code, _ = client.mail(str(mail["From"]))
        if code != 250:
            return {"status": "failed", "phase": "mail", "smtp_code": code}
        code, _ = client.rcpt(str(mail["To"]))
        if code not in {250, 251}:
            return {"status": "failed", "phase": "recipient", "smtp_code": code}
        data_started = True
        code, _ = client.data(mail.as_bytes())
        return {
            "status": "accepted" if code == 250 else "failed",
            "phase": "data",
            "smtp_code": code,
        }
    except smtplib.SMTPResponseException as exc:
        return {
            "status": "failed",
            "phase": "data" if data_started else "connection_or_auth",
            "smtp_code": int(exc.smtp_code),
            "error_type": type(exc).__name__,
        }
    except (OSError, smtplib.SMTPException):
        return {
            "status": "ambiguous" if data_started else "failed",
            "phase": "data" if data_started else "connection_or_auth",
        }
    finally:
        if client is not None:
            # DATA's confirmed250 remains accepted if shutdown fails; never resend.
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                try:
                    client.close()
                except (OSError, smtplib.SMTPException):
                    pass


def deliver(
    *,
    journal: DeliveryJournal,
    issued_id: IssuedForecastId,
    recipient: str,
    subject: str,
    text: str,
    html: str,
    attachment: bytes,
    settings: SmtpSettings,
    product_version: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    transport: Callable[[EmailMessage, SmtpSettings], dict[str, Any]] = smtp_send,
) -> dict[str, Any]:
    """At most one automatic SMTP attempt per issuance/recipient/product version.

    An existing intent without a result is ambiguous, even if the process crashed
    before connecting. Safety takes precedence over claiming exactly-once delivery.
    """
    issued_id = IssuedForecastId(str(issued_id))
    recipient = address(recipient)
    key = delivery_identity(issued_id, recipient, product_version)
    timestamp = clock()
    mail = message(
        settings=settings,
        recipient=recipient,
        subject=subject,
        text=text,
        html=html,
        attachment=attachment,
        delivery_id=key,
        timestamp=timestamp,
    )
    with journal.lock(key):
        previous = journal.events(key)
        if previous:
            outcomes = [event for event in previous if event["event"] == "result"]
            return {
                "status": "duplicate_suppressed",
                "delivery_id": str(key),
                "previous_outcome": outcomes[-1]["provider_result"]
                if outcomes
                else {"status": "ambiguous"},
            }
        common = {
            "schema_version": SCHEMA,
            "delivery_id": str(key),
            "issued_forecast_id": str(issued_id),
            "recipient": recipient,
            "subject": subject,
            "attachment_digest": str(Digest.of_bytes(attachment)),
            "attachment_bytes": len(attachment),
            "product_version": product_version,
            "provider": "smtp",
        }
        journal.append({**common, "event": "intent", "created_at": timestamp.isoformat()})
        try:
            outcome = transport(mail, settings)
        except Exception:
            # Unknown injected/adapter failure may follow DATA; never automatically retry.
            outcome = {"status": "ambiguous", "phase": "transport"}
        safe = safe_provider_result(outcome)
        result = {
            **common,
            "event": "result",
            "created_at": clock().isoformat(),
            "provider_result": safe,
        }
        # If this save fails the durable intent still blocks another automatic send.
        reference = journal.append(result)
        return {**result, "status": safe["status"], "audit_reference": reference}
