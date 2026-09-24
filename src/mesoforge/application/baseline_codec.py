"""Lossless metadata references for the baseline artifact, not a new forecast schema.

Numerical grid values retain their existing representation. Large repeated metadata
uses one baseline-owned table; exact source-owned objects instead reference a
checksummed prepared document and JSON pointer. The enclosing baseline manifest
checksums the compact grid and tables together. Nothing here calculates a field.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from mesoforge.common.identifiers import Digest

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

        def visit(value: Any, key: str = "") -> Any:
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
                {child_key: visit(child, child_key) for child_key, child in value.items()}
                if isinstance(value, dict)
                else [visit(child, key) for child in value]
            )
            if factor:
                digest = hashlib.sha256(_bytes(encoded)).hexdigest()
                if digest not in self._metadata_index:
                    self._metadata_index[digest] = len(self._metadata)
                    self._metadata.append(encoded)
                self._metadata_references += 1
                return {"$metadata": self._metadata_index[digest]}
            return encoded

        result: dict[str, Any] = visit(grid)
        return result

    def export_tables(self) -> dict[str, Any]:
        """Save once after encoding every reference view in this baseline."""
        return {
            "source_documents": deepcopy(self._descriptors),
            "metadata": deepcopy(self._metadata),
        }

    @classmethod
    def from_tables(cls, tables: dict[str, Any]) -> CompactCodec:
        """Verify retained source documents without rebuilding the encoding index."""
        if (
            not isinstance(tables, dict)
            or set(tables) != {"source_documents", "metadata"}
            or not isinstance(tables["metadata"], list)
        ):
            raise ValueError("Invalid baseline metadata tables")
        codec = cls.__new__(cls)
        codec._initialize(tables["source_documents"])
        codec._metadata = deepcopy(tables["metadata"])
        return codec

    def decode(self, encoded: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct an independently owned grid; never calculate a forecast."""
        cache: dict[int, Any] = {}
        resolving: set[int] = set()

        def visit(value: Any) -> Any:
            if isinstance(value, list):
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
                    return _pointer(self._documents[reference[0]], reference[1])
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
            return {key: visit(child) for key, child in value.items()}

        if not isinstance(encoded, dict):
            raise ValueError("Compact baseline grid must be a JSON object")
        result = visit(encoded)
        if not isinstance(result, dict):
            raise ValueError("Decoded baseline grid must be a JSON object")
        return deepcopy(result)

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
