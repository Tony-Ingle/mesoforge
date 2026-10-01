"""Structured operational events for hosted worker roles: one JSON object per line.

Events go to stderr so a command's machine-readable result can own stdout. Public
display values pass through ``redact``. Persisted control state and programmatic
records use ``redact_diagnostics`` so a short credential coinciding with a schema
name or timestamp cannot corrupt recovery identities or clocks.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TextIO

# Values of these variables are never written anywhere, whatever field carries them.
# Access-key identifiers are not secrets and are deliberately absent: a short reused
# name would otherwise redact unrelated paths and module names.
SECRET_VARIABLES = (
    "OPENAI_API_KEY",
    "MESOFORGE_S3_SECRET_KEY",
    "MESOFORGE_DATABASE_DSN",
    "MESOFORGE_ALEMBIC_DSN",
    "MESOFORGE_TEST_DATABASE_DSN",
    "MESOFORGE_PG_PASSWORD",
    "MESOFORGE_PG_WORKER_PASSWORD",
    "MESOFORGE_MINIO_ROOT_PASSWORD",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
REDACTED = "[redacted]"
_DSN_PASSWORD = re.compile(r"(?P<head>[a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", re.IGNORECASE)
_TOKENS = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})")
# A provider exception may include a presigned URL rather than an environment
# credential. Keep its useful host/path, never the authorization query values.
_URL_CREDENTIAL = re.compile(
    r"(?P<head>[?&](?:X-Amz-[A-Za-z-]+|X-Goog-[A-Za-z-]+|AWSAccessKeyId|"
    r"Signature|GoogleAccessId|sig|access_token|api_key|token)=)[^\s&#\"'<>]*",
    re.IGNORECASE,
)


def _secret_values() -> list[str]:
    values = []
    for name in SECRET_VARIABLES:
        value = os.environ.get(name, "")
        if value:
            values.append(value)
        match = re.match(r"[a-z][a-z0-9+.-]*://[^:/@\s]*:([^@\s]+)@", value, re.IGNORECASE)
        if match and match.group(1):
            values.append(match.group(1))
    return sorted(set(values), key=len, reverse=True)


def redact(value: Any) -> Any:
    """A copy of ``value`` with credentials removed from every string it contains."""
    secrets = _secret_values()

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, REDACTED)
            item = _DSN_PASSWORD.sub(lambda m: f"{m.group('head')}{REDACTED}@", item)
            item = _URL_CREDENTIAL.sub(lambda m: f"{m.group('head')}{REDACTED}", item)
            return _TOKENS.sub(REDACTED, item)
        if isinstance(item, dict):
            return {key: clean(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(entry) for entry in item]
        return item

    return clean(value)


def redact_diagnostics(value: Any) -> Any:
    """Clean free-form diagnostics while preserving trusted machine control fields.

    Known schemas, timestamps, outcome enums and digest keys are application facts,
    not credential-bearing text. Diagnostic subtrees may contain arbitrary service
    exceptions and are fully redacted, including their nested display metadata.
    Public logs and CLI output still use ``redact`` on the entire record.
    """
    diagnostic_keys = {"error", "errors", "reason", "reasons", "message", "failures", "invalid"}
    if isinstance(value, dict):
        return {
            key: redact(item) if key in diagnostic_keys else redact_diagnostics(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_diagnostics(item) for item in value]
    return value


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def event(
    role: str,
    name: str,
    *,
    stream: TextIO | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    **fields: Any,
) -> dict[str, Any]:
    """Emit and return one redacted structured event."""
    record: dict[str, Any] = redact({"ts": iso(clock()), "role": role, "event": name, **fields})
    print(json.dumps(record, default=str, allow_nan=False), file=stream or sys.stderr, flush=True)
    return record
