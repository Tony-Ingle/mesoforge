"""Deterministic canonical-JSON artifact contract helpers (plan Section
3.5/4.1: small inventories, lineage manifests, coverage reports, and
metric summaries are canonical JSON, RFC 8785/JCS-encoded).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import jcs
from jcs._jcs import JSONEncoder

from mesoforge.common.identifiers import Digest


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize ``payload`` to deterministic RFC 8785/JCS canonical
    JSON bytes. Key order, whitespace, and number formatting are fully
    determined by the JCS algorithm -- two logically equal payloads
    always produce byte-identical output."""
    return bytes(jcs.canonicalize(payload))


def canonical_json_digest(payload: dict[str, Any]) -> Digest:
    """Hash the same JCS bytes without materializing the whole serialized value.

    The pinned JCS encoder's canonicalize() joins this exact iterator into one
    string and then one bytes object. Rich saved grids can exhaust memory at
    that join even though their retained representation and decoded state fit.
    Keep its UTF-16 key ordering and ECMAScript number formatting unchanged.
    """
    digest = hashlib.sha256()
    buffer = bytearray()
    for chunk in JSONEncoder(sort_keys=True).iterencode(payload, _one_shot=False):
        buffer.extend(chunk.encode("utf-8"))
        if len(buffer) >= 65536:
            digest.update(buffer)
            buffer.clear()
    digest.update(buffer)
    return Digest(f"sha256:{digest.hexdigest()}")


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
