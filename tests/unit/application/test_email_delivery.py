"""SMTP has one durable attempt, bounded transport and no forecast side effects."""

from __future__ import annotations

import json
import smtplib
import socketserver
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock
from uuid import UUID

import pytest

from mesoforge.application.email_delivery import (
    MAX_ATTACHMENT_BYTES,
    SmtpSettings,
    deliver,
    message,
    smtp_send,
)
from mesoforge.common.identifiers import Digest

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
ISSUED = UUID("00000000-0000-4000-8000-000000000123")
PDF = b"%PDF-1.7\nfixture validated elsewhere\n%%EOF\n"
SAMPLE_CREDENTIAL = "test-smtp-private-password"
LOCAL_SMTP = smtplib.SMTP


class Journal:
    """Thread-safe append-only fixture; production uses a persistent storage lock."""

    def __init__(self, fail_event=None):
        self.rows = []
        self.mutex = threading.RLock()
        self.fail_event = fail_event

    @contextmanager
    def lock(self, key):
        with self.mutex:
            yield

    def events(self, key):
        return deepcopy([row for row in self.rows if row["delivery_id"] == str(key)])

    def append(self, event):
        if event["event"] == self.fail_event:
            raise OSError("Fixture storage unavailable")
        self.rows.append(deepcopy(event))
        return {"artifact_id": f"fixture-{len(self.rows)}"}


def settings(**overrides):
    return SmtpSettings(
        **{"host": "smtp.example.test", "port": 587, "sender": "forecast@example.test", **overrides}
    )


def arguments(journal=None, **overrides):
    return {
        "journal": journal or Journal(),
        "issued_id": ISSUED,
        "recipient": "reader@example.test",
        "subject": "MesoForge forecast",
        "text": "Your outlook is attached.",
        "html": "<p>Your outlook is attached.</p>",
        "attachment": PDF,
        "settings": settings(),
        "product_version": "forecast-pdf.v1",
        "clock": lambda: NOW,
        **overrides,
    }


def mail(config=None):
    return message(
        settings=config or settings(),
        recipient="reader@example.test",
        subject="MesoForge — forecast",
        text="Outlook attached.",
        html="<p>Outlook attached.</p>",
        attachment=PDF,
        delivery_id=Digest.of_bytes(b"fixture"),
        timestamp=NOW,
    )


def test_recipient_domain_case_cannot_bypass_idempotency_and_local_part_is_preserved():
    journal = Journal()
    transport = Mock(return_value={"status": "accepted"})
    first = deliver(**arguments(journal), transport=transport)
    repeated = deliver(**arguments(journal, recipient="reader@EXAMPLE.TEST"), transport=transport)
    assert repeated["status"] == "duplicate_suppressed"
    assert repeated["delivery_id"] == first["delivery_id"]
    assert journal.rows[0]["recipient"] == "reader@example.test"
    transport.assert_called_once()
    different_local = deliver(
        **arguments(journal, recipient="Reader@EXAMPLE.TEST"), transport=transport
    )
    assert different_local["recipient"] == "Reader@example.test"
    assert different_local["delivery_id"] != first["delivery_id"]
    assert transport.call_count == 2


def test_mime_has_plain_html_and_exact_pdf_without_secret_metadata():
    config = settings(username="private-user", password=SAMPLE_CREDENTIAL)
    original = mail(config)
    encoded = original.as_bytes()
    parsed = BytesParser(policy=policy.default).parsebytes(encoded)
    assert parsed["From"] == config.sender and parsed["To"] == "reader@example.test"
    assert parsed["Subject"] == "MesoForge — forecast"
    assert parsed.get_body(preferencelist=("plain",)).get_content() == "Outlook attached.\r\n"
    assert "<p>Outlook attached.</p>" in parsed.get_body(preferencelist=("html",)).get_content()
    (attachment,) = parsed.iter_attachments()
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_payload(decode=True) == PDF
    assert attachment.get_filename() == "mesoforge-forecast.pdf"
    assert parsed["Message-ID"] == mail(config)["Message-ID"]
    assert SAMPLE_CREDENTIAL not in repr(config) and "private-user" not in repr(config)
    assert SAMPLE_CREDENTIAL.encode() not in encoded and b"private-user" not in encoded


@pytest.mark.parametrize(
    "overrides",
    [
        {"security": "none"},
        {"security": "local_plaintext"},
        {"port": 0},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 61},
        {"username": "user"},
        {"password": SAMPLE_CREDENTIAL},
        {
            "host": "localhost",
            "security": "local_plaintext",
            "username": "u",
            "password": SAMPLE_CREDENTIAL,
        },
        {"sender": "a@example.test\r\nBcc: b@example.test"},
    ],
)
def test_configuration_rejects_insecure_or_unbounded_transport(overrides):
    with pytest.raises(ValueError):
        settings(**overrides)


def test_runtime_configuration_is_explicit_and_credentials_stay_private():
    configured = SmtpSettings.environment(
        {
            "MESOFORGE_SMTP_HOST": "smtp.example.test",
            "MESOFORGE_SMTP_PORT": "465",
            "MESOFORGE_EMAIL_FROM": "forecast@example.test",
            "MESOFORGE_SMTP_SECURITY": "tls",
            "MESOFORGE_SMTP_USERNAME": "private-user",
            "MESOFORGE_SMTP_PASSWORD": SAMPLE_CREDENTIAL,
            "MESOFORGE_SMTP_TIMEOUT_SECONDS": "12.5",
        }
    )
    assert configured.port == 465 and configured.timeout_seconds == 12.5
    assert configured.security == "tls" and configured.password == SAMPLE_CREDENTIAL
    assert SAMPLE_CREDENTIAL not in repr(configured)


def client_fixture(monkeypatch, config, **responses):
    client = Mock()
    client.mail.return_value = (250, b"ok")
    client.rcpt.return_value = (250, b"ok")
    client.data.return_value = (250, b"accepted")
    for name, value in responses.items():
        if isinstance(value, BaseException):
            getattr(client, name).side_effect = value
        else:
            getattr(client, name).return_value = value
    constructor = Mock(return_value=client)
    monkeypatch.setattr(smtplib, "SMTP_SSL" if config.security == "tls" else "SMTP", constructor)
    return client, constructor


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_tls_authentication_precedes_envelope_and_data(monkeypatch, security):
    config = settings(security=security, username="private-user", password=SAMPLE_CREDENTIAL)
    client, constructor = client_fixture(monkeypatch, config)
    result = smtp_send(mail(config), config)
    assert result == {"status": "accepted", "phase": "data", "smtp_code": 250}
    constructor.assert_called_once()
    assert constructor.call_args.kwargs["timeout"] == 30
    calls = [row[0] for row in client.mock_calls]
    assert calls == (["ehlo", "starttls", "ehlo"] if security == "starttls" else ["ehlo"]) + [
        "login",
        "mail",
        "rcpt",
        "data",
        "quit",
    ]
    client.login.assert_called_once_with("private-user", SAMPLE_CREDENTIAL)
    context = (
        client.starttls.call_args.kwargs["context"]
        if security == "starttls"
        else constructor.call_args.kwargs["context"]
    )
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "phase, failure, expected",
    [
        ("login", smtplib.SMTPAuthenticationError(535, SAMPLE_CREDENTIAL.encode()), "failed"),
        ("starttls", smtplib.SMTPNotSupportedError(SAMPLE_CREDENTIAL), "failed"),
        ("mail", TimeoutError(SAMPLE_CREDENTIAL), "failed"),
        ("rcpt", (550, SAMPLE_CREDENTIAL.encode()), "failed"),
        ("data", smtplib.SMTPDataError(550, SAMPLE_CREDENTIAL.encode()), "failed"),
        ("data", TimeoutError(SAMPLE_CREDENTIAL), "ambiguous"),
        ("data", smtplib.SMTPServerDisconnected(SAMPLE_CREDENTIAL), "ambiguous"),
    ],
)
def test_failures_are_sanitized_and_transport_never_retries(monkeypatch, phase, failure, expected):
    config = settings(username="private-user", password=SAMPLE_CREDENTIAL)
    client, constructor = client_fixture(monkeypatch, config, **{phase: failure})
    result = smtp_send(mail(config), config)
    assert result["status"] == expected and SAMPLE_CREDENTIAL not in json.dumps(result)
    assert constructor.call_count == 1
    assert client.data.call_count == (1 if phase == "data" else 0)


def test_confirmed_data_acceptance_survives_quit_and_close_failure(monkeypatch):
    config = settings()
    client, _ = client_fixture(
        monkeypatch, config, quit=OSError(SAMPLE_CREDENTIAL), close=OSError(SAMPLE_CREDENTIAL)
    )
    assert smtp_send(mail(config), config)["status"] == "accepted"
    assert client.data.call_count == client.quit.call_count == client.close.call_count == 1


@pytest.mark.parametrize("status", ["accepted", "failed", "ambiguous"])
def test_each_result_is_immutable_and_repeat_does_not_send_again(status):
    journal = Journal()
    transport = Mock(return_value={"status": status, "phase": "data", "smtp_code": 250})
    kwargs = arguments(journal, transport=transport)
    result = deliver(**kwargs)
    assert result["status"] == status and [r["event"] for r in journal.rows] == ["intent", "result"]
    assert journal.rows[0]["attachment_digest"] == str(Digest.of_bytes(PDF))
    assert journal.rows[0]["issued_forecast_id"] == str(ISSUED)
    original = deepcopy(journal.rows)
    repeat = deliver(**kwargs)
    assert repeat["status"] == "duplicate_suppressed"
    assert repeat["previous_outcome"]["status"] == status
    assert journal.rows == original and transport.call_count == 1


def test_durable_intent_survives_result_storage_failure_without_automatic_resend():
    journal = Journal(fail_event="result")
    transport = Mock(return_value={"status": "accepted", "phase": "data", "smtp_code": 250})
    kwargs = arguments(journal, transport=transport)
    with pytest.raises(OSError):
        deliver(**kwargs)
    assert len(journal.rows) == 1 and journal.rows[0]["event"] == "intent"
    journal.fail_event = None
    repeat = deliver(**kwargs)
    assert repeat["previous_outcome"] == {"status": "ambiguous"}
    assert transport.call_count == 1 and len(journal.rows) == 1


def test_intent_storage_failure_prevents_smtp():
    transport = Mock()
    with pytest.raises(OSError):
        deliver(**arguments(Journal(fail_event="intent"), transport=transport))
    transport.assert_not_called()


def test_concurrent_same_delivery_has_only_one_smtp_attempt():
    journal = Journal()
    transport = Mock(return_value={"status": "accepted", "phase": "data", "smtp_code": 250})
    kwargs = arguments(journal, transport=transport)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: deliver(**kwargs), range(4)))
    assert sorted(r["status"] for r in results) == [
        "accepted",
        "duplicate_suppressed",
        "duplicate_suppressed",
        "duplicate_suppressed",
    ]
    assert transport.call_count == 1 and len(journal.rows) == 2


@pytest.mark.parametrize("response", [None, [], {"status": []}, {"status": "unexpected"}])
def test_malformed_transport_result_is_ambiguous_and_cannot_trigger_retry(response):
    journal = Journal()
    transport = Mock(return_value=response)
    kwargs = arguments(journal, transport=transport)
    assert deliver(**kwargs)["status"] == "ambiguous"
    assert deliver(**kwargs)["status"] == "duplicate_suppressed"
    assert transport.call_count == 1


def test_adapter_text_and_secret_values_are_never_retained(caplog):
    journal = Journal()
    result = deliver(
        **arguments(
            journal,
            settings=settings(username="user", password=SAMPLE_CREDENTIAL),
            transport=lambda *_: {
                "status": "failed",
                "phase": SAMPLE_CREDENTIAL,
                "smtp_code": SAMPLE_CREDENTIAL,
                "error_type": SAMPLE_CREDENTIAL,
                "message": SAMPLE_CREDENTIAL,
            },
        )
    )
    assert result["provider_result"] == {"status": "failed", "phase": "transport"}
    assert SAMPLE_CREDENTIAL not in json.dumps(journal.rows) + json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"attachment": b"not pdf"},
        {"attachment": b"%PDF-" + b"x" * MAX_ATTACHMENT_BYTES},
        {"recipient": "a@example.test\r\nBcc: b@example.test"},
        {"subject": "bad\nsubject"},
        {"clock": lambda: NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_message_is_rejected_before_any_intent_or_transport(overrides):
    journal = Journal()
    transport = Mock()
    with pytest.raises(ValueError):
        deliver(**arguments(journal, transport=transport, **overrides))
    assert journal.rows == []
    transport.assert_not_called()


def test_real_smtplib_sends_one_mime_message_to_bounded_loopback_fixture(monkeypatch):
    # Restore stdlib only for this uncredentialed loopback server; ordinary tests
    # keep the global constructor guard against operator SMTP configuration.
    monkeypatch.setattr(smtplib, "SMTP", LOCAL_SMTP)
    received = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            self.wfile.write(b"220 fixture ready\r\n")
            while line := self.rfile.readline(10000):
                command = line.split(b" ", 1)[0].strip().upper()
                if command == b"DATA":
                    self.wfile.write(b"354 send message\r\n")
                    chunks = []
                    while (chunk := self.rfile.readline(10000)) != b".\r\n":
                        if not chunk:
                            return
                        chunks.append(chunk)
                    received.append(b"".join(chunks))
                    self.wfile.write(b"250 queued\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 fixture\r\n")

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            config = settings(
                host="127.0.0.1",
                port=server.server_address[1],
                security="local_plaintext",
                timeout_seconds=2,
            )
            kwargs = arguments(settings=config)
            assert deliver(**kwargs)["status"] == "accepted"
            assert deliver(**kwargs)["status"] == "duplicate_suppressed"
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert len(received) == 1
    parsed = BytesParser(policy=policy.default).parsebytes(received[0])
    (attachment,) = parsed.iter_attachments()
    assert attachment.get_payload(decode=True) == PDF
