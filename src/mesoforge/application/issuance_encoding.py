"""Self-contained, lossless storage for long issuances; no forecast calculations."""

from __future__ import annotations

import gzip
import zlib
from typing import Any

from mesoforge.application.baseline_codec import CompactCodec
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_digest,
    parse_canonical_json,
)

ENCODING = "mesoforge.issued-storage.v1"


def encode_issuance(saved: dict[str, Any]) -> tuple[bytes, Digest]:
    """Reference repeated metadata locally, then compress; never reference disk paths.

    The returned logical digest identifies the exact decoded issuance. The object
    store separately checksums the resulting compressed bytes.
    """
    logical_bytes = canonical_json_bytes(saved)
    logical_digest = Digest.of_bytes(logical_bytes)
    # Stable table numbering and numeric representation must not depend on the
    # caller's insertion order or whether JSON readback represented 1.0 as 1.
    normalized = parse_canonical_json(logical_bytes)
    del logical_bytes
    codec = CompactCodec([])
    payload = codec.encode(normalized)
    envelope = {
        "schema": ENCODING,
        "forecast_payload_digest": str(logical_digest),
        "payload": payload,
        "tables": codec.export_tables(),
    }
    return gzip.compress(canonical_json_bytes(envelope), mtime=0), logical_digest


def decode_issuance(payload: bytes, *, expected_logical_digest: Digest) -> dict[str, Any]:
    """Decode only the known self-contained envelope and verify its logical identity."""
    try:
        envelope = parse_canonical_json(gzip.decompress(payload))
        if (
            set(envelope) != {"schema", "forecast_payload_digest", "payload", "tables"}
            or envelope["schema"] != ENCODING
            or envelope["forecast_payload_digest"] != str(expected_logical_digest)
            or envelope["tables"]["source_documents"] != []
        ):
            raise ValueError("Unknown encoding, digest mismatch, or external source references")
        saved = CompactCodec.from_tables(envelope["tables"]).decode(envelope["payload"])
        if canonical_json_digest(saved) != expected_logical_digest:
            raise ValueError("Decoded issuance checksum mismatch")
        return saved
    except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error) as exc:
        raise IntegrityError("Invalid compact issued forecast or logical checksum") from exc
