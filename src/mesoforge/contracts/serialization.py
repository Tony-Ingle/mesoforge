"""Deterministic canonical-JSON artifact contract helpers (plan Section
3.5/4.1: small inventories, lineage manifests, coverage reports, and
metric summaries are canonical JSON, RFC 8785/JCS-encoded).
"""

from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Iterable, Iterator
from typing import Any

import jcs
from jcs._jcs import JSONEncoder, convert2Es6Format, encode_basestring

from mesoforge.common.identifiers import Digest

_STREAM_BUFFER_BYTES = 1 << 20


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


def canonical_json_chunks(payload: Any) -> Iterator[str]:
    """Yield the exact JCS text of ``payload`` piecewise, never as one string.

    ``"".join(canonical_json_chunks(x)).encode("utf-8") == canonical_json_bytes(x)``
    for every value the pinned encoder accepts. Large saved issuances cannot afford
    the encoder's one-shot chunk list plus the joined text plus its UTF-8 copy.
    """
    chunks: Iterator[str] = JSONEncoder(sort_keys=True).iterencode(payload, _one_shot=False)
    return chunks


def canonical_json_scalar(value: Any) -> str:
    """The JCS text of one JSON scalar, using the pinned encoder's own primitives.

    Containers and unsupported types are rejected; they belong to the full encoder.
    """
    if isinstance(value, str):
        return str(encode_basestring(value))
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(convert2Es6Format(value))
    raise TypeError(f"Object of type {type(value).__name__} is not a JSON scalar")


def canonical_json_gzip(chunks: Iterable[str]) -> bytes:
    """Gzip the UTF-8 text of JCS chunks without materializing the whole text.

    The result is byte-identical to ``gzip.compress(text, mtime=0)`` at the default
    compression level: that helper is exactly ``zlib.compress(text, 9, wbits=31)``,
    and the deflate stream does not depend on how its input is chunked.
    """
    compressor = zlib.compressobj(level=9, wbits=31)
    output = bytearray()
    buffer = bytearray()
    for chunk in chunks:
        buffer += chunk.encode("utf-8")
        if len(buffer) >= _STREAM_BUFFER_BYTES:
            output += compressor.compress(bytes(buffer))
            buffer.clear()
    output += compressor.compress(bytes(buffer))
    output += compressor.flush()
    return bytes(output)


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
