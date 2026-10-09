"""Self-contained, lossless storage for long issuances; no forecast calculations."""

from __future__ import annotations

import gzip
import io
import json
import zlib
from collections.abc import Iterator
from typing import Any

from mesoforge.application.baseline_codec import CompactCodec
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import (
    canonical_json_chunks,
    canonical_json_digest,
    canonical_json_gzip,
    canonical_json_scalar,
)

ENCODING = "mesoforge.issued-storage.v1"
# The envelope members in the pinned canonical (UTF-16 code unit) order. The payload
# walk fills the metadata tables, which the canonical order places after it.
_ENVELOPE_KEYS = ("forecast_payload_digest", "payload", "schema", "tables")


def encode_issuance(saved: dict[str, Any]) -> tuple[bytes, Digest]:
    """Reference repeated metadata locally, then compress; never reference disk paths.

    The returned logical digest identifies the exact decoded issuance. The object
    store separately checksums the resulting compressed bytes.

    Stable table numbering and numeric representation must not depend on the
    caller's insertion order or whether JSON readback represented 1.0 as 1, so the
    encoding is that of the canonical normalized issuance. A saved 120-hour grid
    cannot afford that normalized copy, its complete canonical text and a third
    encoded graph side by side: the logical digest streams over the saved values,
    the codec normalizes and factors one metadata subtree at a time while writing
    the payload in canonical order, and the envelope is compressed as it is written.
    """
    logical_digest = canonical_json_digest(saved)
    codec = CompactCodec([])
    return canonical_json_gzip(_envelope_chunks(codec, saved, logical_digest)), logical_digest


def _envelope_chunks(
    codec: CompactCodec, saved: dict[str, Any], logical_digest: Digest
) -> Iterator[str]:
    yield f'{{"forecast_payload_digest":{canonical_json_scalar(str(logical_digest))},"payload":'
    yield from codec.encode_canonical(saved)
    yield f',"schema":{canonical_json_scalar(ENCODING)},"tables":'
    yield from canonical_json_chunks(codec.export_tables(copy=False))
    yield "}"


def decode_issuance(payload: bytes, *, expected_logical_digest: Digest) -> dict[str, Any]:
    """Decode only the known self-contained envelope and verify its logical identity.

    The compressed bytes expand and parse as one stream, and the envelope's own
    containers become the decoded issuance in place: neither the expanded bytes nor
    a second complete object graph is held beside the result.
    """
    try:
        with (
            gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed,
            io.TextIOWrapper(compressed, encoding="utf-8") as text,
        ):
            envelope = json.load(text)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != set(_ENVELOPE_KEYS)
            or envelope["schema"] != ENCODING
            or envelope["forecast_payload_digest"] != str(expected_logical_digest)
            or envelope["tables"]["source_documents"] != []
        ):
            raise ValueError("Unknown encoding, digest mismatch, or external source references")
        codec = CompactCodec.from_tables(envelope["tables"], consume=True)
        saved = codec.decode(envelope["payload"], consume=True)
        del envelope, codec  # Decoded state owns its values; release the table expansion.
        if canonical_json_digest(saved) != expected_logical_digest:
            raise ValueError("Decoded issuance checksum mismatch")
        return saved
    except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error) as exc:
        raise IntegrityError("Invalid compact issued forecast or logical checksum") from exc
