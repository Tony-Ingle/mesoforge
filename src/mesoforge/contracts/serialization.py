"""Deterministic canonical-JSON artifact contract helpers (plan Section
3.5/4.1: small inventories, lineage manifests, coverage reports, and
metric summaries are canonical JSON, RFC 8785/JCS-encoded).
"""

from __future__ import annotations

import json
from typing import Any

import jcs

from mesoforge.common.identifiers import Digest


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize ``payload`` to deterministic RFC 8785/JCS canonical
    JSON bytes. Key order, whitespace, and number formatting are fully
    determined by the JCS algorithm -- two logically equal payloads
    always produce byte-identical output."""
    return bytes(jcs.canonicalize(payload))


def canonical_json_digest(payload: dict[str, Any]) -> Digest:
    return Digest.of_bytes(canonical_json_bytes(payload))


def parse_canonical_json(data: bytes) -> dict[str, Any]:
    """Parse canonical JSON bytes back into a plain ``dict``. Rejects
    non-object top-level payloads: every Phase 1 canonical-JSON artifact
    (acquisition manifest, lineage manifest, extraction report,
    verification report) is a JSON object, never a bare array or
    scalar."""
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(
            f"canonical JSON artifact payload must decode to a JSON object, got {type(value)!r}"
        )
    return value
