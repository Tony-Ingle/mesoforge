"""Lossless metadata references for the baseline artifact, not a new forecast schema.

Numerical grid values retain their existing representation. Large repeated metadata
uses one baseline-owned table; exact source-owned objects instead reference a
checksummed prepared document and JSON pointer. The enclosing baseline manifest
checksums the compact grid and tables together. Nothing here calculates a field.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_chunks,
    canonical_json_scalar,
)

_MINIMUM_BYTES = 256
_REFERENCE_KEYS = frozenset({"$metadata", "$source"})
_METADATA_KEYS = frozenset(
    {
        "provenance",
        "acquisition",
        "source_metadata",
        "normalization",
        "source_inputs",
        "source_status",
        "selection_evidence",
        "current_model_set",
        "contributor_configuration",
        "configuration",
        "code_identity",
        "prepared_snapshot",
        "source_information",
        "source",
        "native_intervals",
        "available_native_intervals",
        "native_events",
        "event",
        "threshold",
        "spatial_support",
        "policy",
        "definition",
        "method",
        "products",
        "rules",
        "derivation",
        "field_policies",
        "spatial_extraction",
        "source_validation",
        "transformation",
        "grib_keys",
        "grib_fields",
        "grib_threshold",
        "diagnostic_reference",
        "censoring",
        "member_population",
        "event_definition",
        "comparisons",
        "missing_reasons",
    }
)


def _bytes(value: Any) -> bytes:
    # This is only an in-memory exact-subtree lookup key. The artifact itself uses
    # the repository's canonical serialization and manifest digests.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _canonical_key_order(item: tuple[str, Any]) -> bytes:
    # The pinned JCS encoder orders object members by UTF-16 code units.
    return item[0].encode("utf-16_be")


def _pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("Baseline source reference has an invalid JSON pointer")
    for raw in pointer[1:].split("/"):
        if "~" in raw.replace("~0", "").replace("~1", ""):
            raise ValueError("Baseline source reference has an invalid JSON pointer escape")
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isascii() or not token.isdecimal() or str(int(token)) != token:
                raise ValueError("Baseline source reference has an invalid array index")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("Baseline source reference traverses a scalar")
    return value


class CompactCodec:
    """One shared metadata table for all domains/reference windows of a baseline.

    Readback verifies every source document, including documents not selected by a
    particular location. Public decode results never alias retained source/table
    objects. The caller owns immutable artifact publication and digest validation.
    """

    def __init__(self, source_documents: list[dict[str, Any]]) -> None:
        self._initialize(source_documents)
        self._source_index: dict[str, tuple[int, str]] = {}
        for index, value in enumerate(self._documents):
            self._index_source(value, index, "")

    def _initialize(self, source_documents: list[dict[str, Any]]) -> None:
        if not isinstance(source_documents, list):
            raise ValueError("Baseline source documents must be a list")
        self._descriptors: list[dict[str, str]] = []
        self._documents: list[Any] = []
        self._metadata: list[Any] = []
        self._metadata_index: dict[str, int] = {}
        self._source_index = {}
        self._source_bytes = 0
        self._source_references = 0
        self._metadata_references = 0
        for descriptor in source_documents:
            if not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256"}:
                raise ValueError("Baseline source documents require path and sha256")
            path, expected = descriptor["path"], descriptor["sha256"]
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError("Baseline source document paths must be absolute")
            if not isinstance(expected, str):
                raise ValueError("Baseline source document digest must be a string")
            expected = str(
                Digest(expected if expected.startswith("sha256:") else f"sha256:{expected}")
            )
            payload = Path(path).read_bytes()
            if str(Digest.of_bytes(payload)) != expected:
                raise ValueError(f"Baseline source document digest differs: {path}")
            self._documents.append(json.loads(payload))
            self._descriptors.append({"path": path, "sha256": expected})
            self._source_bytes += len(payload)

    def _index_source(self, value: Any, index: int, pointer: str) -> None:
        if not isinstance(value, (dict, list)):
            return
        payload = _bytes(value)
        if len(payload) >= _MINIMUM_BYTES:
            self._source_index.setdefault(hashlib.sha256(payload).hexdigest(), (index, pointer))
        entries = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in entries:
            token = str(key).replace("~", "~0").replace("/", "~1")
            self._index_source(child, index, f"{pointer}/{token}")

    def encode(self, grid: dict[str, Any]) -> dict[str, Any]:
        """Factor repeated metadata without altering values, intervals or policies."""
        if not isinstance(grid, dict):
            raise ValueError("Baseline grid must be a JSON object")
        result: dict[str, Any] = self._visit(grid, "")
        return result

    def _visit(self, value: Any, key: str) -> Any:
        """Factor one value under its parent ``key``; the unchanged ``encode`` rule."""
        if not isinstance(value, (dict, list)):
            return value
        if isinstance(value, dict) and _REFERENCE_KEYS.intersection(value):
            raise ValueError("Unencoded baseline grid contains reserved reference keys")
        factor = key in _METADATA_KEYS
        if factor:
            raw = _bytes(value)
            factor = len(raw) >= _MINIMUM_BYTES
            source = self._source_index.get(hashlib.sha256(raw).hexdigest()) if factor else None
            if source is not None:
                self._source_references += 1
                return {"$source": list(source)}
        encoded = (
            {child_key: self._visit(child, child_key) for child_key, child in value.items()}
            if isinstance(value, dict)
            else [self._visit(child, key) for child in value]
        )
        if factor:
            digest = hashlib.sha256(_bytes(encoded)).hexdigest()
            if digest not in self._metadata_index:
                self._metadata_index[digest] = len(self._metadata)
                self._metadata.append(encoded)
            self._metadata_references += 1
            return {"$metadata": self._metadata_index[digest]}
        return encoded

    def encode_canonical(self, value: dict[str, Any]) -> Iterator[str]:
        """Stream the canonical text of ``encode(normalized)`` from a live object graph.

        ``normalized`` means ``json.loads(canonical_json_bytes(value))``: members in
        canonical order, integral floats read back as ints, tuples as lists. A saved
        120-hour issuance cannot afford that complete copy, its canonical text and a
        third encoded graph at once. Here every metadata-keyed subtree is normalized
        on its own and factored through the unchanged ``encode`` rule, while the
        remaining structure is written in canonical order directly from the caller's
        objects, whose scalars serialize identically either way. A metadata object
        shared by many cells/hours (one decoded block) is factored once. The result
        text equals ``canonical_json_bytes(encode(normalized))``; the caller's values
        are never modified and the metadata table grows exactly as with ``encode``.
        """
        if not isinstance(value, dict):
            raise ValueError("Baseline grid must be a JSON object")
        memo: dict[int, tuple[Any, Any]] = {}
        return self._stream(value, "", memo)

    def _stream(self, value: Any, key: str, memo: dict[int, tuple[Any, Any]]) -> Iterator[str]:
        if isinstance(value, dict):
            if key in _METADATA_KEYS:
                yield from canonical_json_chunks(self._factor_live(value, key, memo))
                return
            if _REFERENCE_KEYS.intersection(value):
                raise ValueError("Unencoded baseline grid contains reserved reference keys")
            if not value:
                yield "{}"
                return
            if any(not isinstance(child_key, str) for child_key in value):
                raise TypeError("Canonical baseline encoding requires string keys")
            separator = "{"
            for child_key, child in sorted(value.items(), key=_canonical_key_order):
                yield f"{separator}{canonical_json_scalar(child_key)}:"
                separator = ","
                yield from self._stream(child, child_key, memo)
            yield "}"
        elif isinstance(value, (list, tuple)):
            if key in _METADATA_KEYS:
                yield from canonical_json_chunks(self._factor_live(value, key, memo))
                return
            if not value:
                yield "[]"
                return
            separator = "["
            for child in value:
                yield separator
                separator = ","
                yield from self._stream(child, key, memo)
            yield "]"
        else:
            yield canonical_json_scalar(value)

    def _factor_live(self, value: Any, key: str, memo: dict[int, tuple[Any, Any]]) -> Any:
        """Normalize one live metadata-keyed container and factor it exactly once.

        The memo keeps each live object referenced, so its identity stays unique for
        the whole stream; only caller-owned objects are memoized, never the transient
        normalized copies.
        """
        cached = memo.get(id(value))
        if cached is not None and cached[0] is value:
            encoded = cached[1]
            if isinstance(encoded, dict) and "$metadata" in encoded:
                self._metadata_references += 1
            elif isinstance(encoded, dict) and "$source" in encoded:
                self._source_references += 1
            return encoded
        if isinstance(value, dict) and _REFERENCE_KEYS.intersection(value):
            raise ValueError("Unencoded baseline grid contains reserved reference keys")
        normalized = json.loads(canonical_json_bytes(value))
        encoded = self._visit(normalized, key)
        memo[id(value)] = (value, encoded)
        return encoded

    def export_tables(self, *, copy: bool = True) -> dict[str, Any]:
        """Save once after encoding every reference view in this baseline.

        ``copy=False`` exposes the live tables to a caller that owns this codec and
        only serializes them immediately; it must neither mutate nor retain them.
        """
        if not copy:
            return {"source_documents": self._descriptors, "metadata": self._metadata}
        return {
            "source_documents": deepcopy(self._descriptors),
            "metadata": deepcopy(self._metadata),
        }

    @classmethod
    def from_tables(cls, tables: dict[str, Any], *, consume: bool = False) -> CompactCodec:
        """Verify retained sources without rebuilding the encoding index.

        ``consume=True`` transfers freshly loaded tables to this codec. The caller
        must relinquish them; the default keeps independent public ownership.
        """
        if (
            not isinstance(tables, dict)
            or set(tables) != {"source_documents", "metadata"}
            or not isinstance(tables["metadata"], list)
        ):
            raise ValueError("Invalid baseline metadata tables")
        codec = cls.__new__(cls)
        codec._initialize(tables["source_documents"])
        codec._metadata = tables["metadata"] if consume else deepcopy(tables["metadata"])
        return codec

    def decode(self, encoded: dict[str, Any], *, consume: bool = False) -> dict[str, Any]:
        """Reconstruct an owned grid; never calculate a forecast.

        ``consume=True`` transfers a freshly loaded encoded grid, resolving its
        containers in place instead of holding two full numerical grids at once.
        Retained metadata/source tables are always isolated from the result.
        """
        cache: dict[int, Any] = {}
        resolving: set[int] = set()
        source_memo: dict[int, Any] = {}

        def visit(value: Any, *, owned: bool = False) -> Any:
            if isinstance(value, list):
                if owned:
                    for index, child in enumerate(value):
                        value[index] = visit(child, owned=True)
                    return value
                return [visit(child) for child in value]
            if not isinstance(value, dict):
                return value
            if "$source" in value:
                reference = value["$source"]
                if (
                    set(value) != {"$source"}
                    or not isinstance(reference, list)
                    or len(reference) != 2
                    or type(reference[0]) is not int
                    or not 0 <= reference[0] < len(self._documents)
                    or not isinstance(reference[1], str)
                ):
                    raise ValueError("Invalid baseline source reference")
                try:
                    # Source pointers alone return retained objects. Copy those
                    # once per decode, preserving parent/child pointer aliasing.
                    return deepcopy(
                        _pointer(self._documents[reference[0]], reference[1]), source_memo
                    )
                except (IndexError, KeyError, TypeError) as exc:
                    raise ValueError("Unresolvable baseline source reference") from exc
            if "$metadata" in value:
                index = value["$metadata"]
                if (
                    set(value) != {"$metadata"}
                    or type(index) is not int
                    or not 0 <= index < len(self._metadata)
                    or index in resolving
                ):
                    raise ValueError("Invalid or cyclic baseline metadata reference")
                if index not in cache:
                    resolving.add(index)
                    cache[index] = visit(self._metadata[index])
                    resolving.remove(index)
                return cache[index]
            if owned:
                for key, child in value.items():
                    value[key] = visit(child, owned=True)
                return value
            return {key: visit(child) for key, child in value.items()}

        if not isinstance(encoded, dict):
            raise ValueError("Compact baseline grid must be a JSON object")
        result = visit(encoded, owned=consume)
        if not isinstance(result, dict):
            raise ValueError("Decoded baseline grid must be a JSON object")
        # All other dicts/lists, including cached metadata, were constructed by
        # visit for this decode. A final deepcopy would duplicate the entire
        # owned grid while both graphs are live, without adding isolation.
        return result

    @property
    def statistics(self) -> dict[str, int]:
        return {
            "source_documents": len(self._documents),
            "source_document_bytes": self._source_bytes,
            "source_references": self._source_references,
            "metadata_references": self._metadata_references,
            "metadata_blocks": len(self._metadata),
            "metadata_table_json_bytes": len(_bytes(self._metadata)),
        }
