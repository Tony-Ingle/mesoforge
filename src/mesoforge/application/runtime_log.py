"""Structured operational events for hosted worker roles: one JSON object per line.

Events go to stderr so a command's machine-readable result can own stdout. Every
value passes through ``redact`` first: the values of known credential variables, DSN
passwords and bearer/API-key shaped tokens never reach a log or status file.
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
    "MESOFORGE_MINIO_ROOT_PASSWORD",
)
REDACTED = "[redacted]"
# Shorter values are not matched verbatim (DSN passwords and token shapes still are).
MINIMUM_SECRET_LENGTH = 12
_DSN_PASSWORD = re.compile(r"(?P<head>[a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", re.IGNORECASE)
_TOKENS = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})")


def _secret_values() -> list[str]:
    values = []
    for name in SECRET_VARIABLES:
        value = os.environ.get(name, "")
        if len(value) >= MINIMUM_SECRET_LENGTH:
            values.append(value)
        match = re.match(r"[a-z][a-z0-9+.-]*://[^:/@\s]*:([^@\s]+)@", value, re.IGNORECASE)
        if match and len(match.group(1)) >= MINIMUM_SECRET_LENGTH:
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
            return _TOKENS.sub(REDACTED, item)
        if isinstance(item, dict):
            return {key: clean(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(entry) for entry in item]
        return item

    return clean(value)


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
