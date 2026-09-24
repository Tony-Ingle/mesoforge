"""Baseline-only evidence factorization must be lossless, owned and checksummed."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from mesoforge.application.baseline_codec import CompactCodec
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import canonical_json_bytes


@pytest.fixture()
def source(tmp_path):
    evidence = {
        "cycle": "2026-09-18T00:00:00Z",
        "message": "Native evidence retained exactly. " * 20,
        "source_url": "https://example.test/model.grib2",
    }
    path = tmp_path / "manifest.json"
    payload = json.dumps({"events/~": [evidence]}).encode()
    path.write_bytes(payload)
    return {"path": str(path), "sha256": str(Digest.of_bytes(payload))}, evidence, path


def test_multiple_grids_share_metadata_preserve_exact_science_and_are_owned(source):
    descriptor, evidence, _ = source
    codec = CompactCodec([descriptor])
    native = {
        "value": 0.0,
        "unit": "kg/m2",
        "interval_start": "2026-09-18T01:00:00Z",
        "interval_end": "2026-09-18T02:00:00Z",
        "provenance": deepcopy(evidence),
        "policy": {"name": "unchanged_policy", "reason": "No invented weights. " * 20},
    }
    grid = {"cells": [{"hours": [{"qpf": native, "missing_qpf": None}]}]}
    original = deepcopy(grid)
    first = codec.encode(grid)
    count = codec.statistics["metadata_blocks"]
    second = codec.encode(grid)
    assert codec.statistics["metadata_blocks"] == count
    assert codec.statistics["source_references"] == 2
    assert codec.statistics["metadata_references"] == 2
    tables = codec.export_tables()
    assert evidence not in tables["metadata"]
    reader = CompactCodec.from_tables(tables)
    assert canonical_json_bytes(reader.decode(first)) == canonical_json_bytes(original)
    assert reader.decode(second) == original
    assert grid == original
    decoded = reader.decode(first)
    decoded["cells"][0]["hours"][0]["qpf"]["provenance"]["cycle"] = "changed"
    decoded["cells"][0]["hours"][0]["qpf"]["policy"]["name"] = "changed"
    tables["metadata"].clear()
    assert reader.decode(first) == original
    assert codec.decode(first) == original


def test_source_checksums_are_verified_even_without_a_reference(source):
    descriptor, _, path = source
    codec = CompactCodec([descriptor])
    tables = codec.export_tables()
    path.write_text("{}")
    with pytest.raises(ValueError, match="digest differs"):
        CompactCodec.from_tables(tables)


@pytest.mark.parametrize(
    "value",
    [
        {"$metadata": -1},
        {"$metadata": True},
        {"$metadata": 0, "other": "not a reference"},
        {"$source": [-1, ""]},
        {"$source": [True, ""]},
        {"$source": [0, "not-a-pointer"]},
        {"$source": [0, "/missing"]},
        {"$source": [0, "/events~1~0/01"]},
        {"$source": [0, "/events~2/0"]},
    ],
)
def test_malformed_references_fail_explicitly(source, value):
    descriptor, _, _ = source
    reader = CompactCodec([descriptor])
    with pytest.raises(ValueError):
        reader.decode({"grid": value})


def test_cyclic_metadata_and_unencoded_reserved_keys_are_rejected():
    reader = CompactCodec.from_tables(
        {"source_documents": [], "metadata": [{"nested": {"$metadata": 0}}]}
    )
    with pytest.raises(ValueError, match="cyclic"):
        reader.decode({"grid": {"$metadata": 0}})
    with pytest.raises(ValueError, match="reserved"):
        CompactCodec([]).encode({"grid": {"$source": []}})
