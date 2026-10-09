"""Baseline-only evidence factorization must be lossless, owned and checksummed."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from mesoforge.application.baseline_codec import CompactCodec
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.serialization import canonical_json_bytes, canonical_json_digest


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


@pytest.mark.parametrize("child_first", [False, True])
def test_decoded_sources_and_metadata_are_owned_with_parent_child_aliases(source, child_first):
    descriptor, evidence, _ = source
    tables = {
        "source_documents": [descriptor],
        "metadata": [{"source": {"$source": [0, "/events~1~0/0"]}, "values": [1, None]}],
    }
    reader = CompactCodec.from_tables(tables)
    pointers = {"parent": {"$source": [0, ""]}, "child": {"$source": [0, "/events~1~0/0"]}}
    if child_first:
        pointers = dict(reversed(list(pointers.items())))
    encoded = {**pointers, "first": {"$metadata": 0}, "second": {"$metadata": 0}}
    before = deepcopy(encoded)
    first = reader.decode(encoded)
    second = reader.decode(encoded)
    assert first == second
    assert first["parent"]["events/~"][0] is first["child"]
    assert first["child"] is first["first"]["source"]
    assert first["first"] is first["second"]
    first["child"]["cycle"] = "changed in first read only"
    first["first"]["values"].append(3)
    assert second["child"] == evidence
    assert second["first"]["values"] == [1, None]
    assert reader.decode(encoded) == second
    assert reader.export_tables() == tables
    assert encoded == before


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


def test_owned_five_day_canvas_transfer_is_lossless_and_keeps_replay_tables_isolated(source):
    descriptor, evidence, _ = source
    writer = CompactCodec([descriptor])
    grid = {
        "cells": [
            {
                "index": cell,
                "hours": [
                    {
                        "lead": lead,
                        "temperature": 273.15 + cell + lead / 10,
                        "qpf": 0.0 if lead % 2 else None,
                        "provenance": deepcopy(evidence),
                        "policy": {"identity": "unchanged", "meaning": "exact policy " * 30},
                    }
                    for lead in range(1, 121)
                ],
            }
            for cell in range(49)
        ]
    }
    encoded = writer.encode(grid)
    tables = writer.export_tables()
    reader = CompactCodec.from_tables(deepcopy(tables), consume=True)
    transferred = deepcopy(encoded)
    cells = transferred["cells"]
    result = reader.decode(transferred, consume=True)
    assert result is transferred and result["cells"] is cells
    assert result == grid
    assert canonical_json_digest(result) == canonical_json_digest(grid)
    result["cells"][0]["hours"][0]["provenance"]["cycle"] = "changed in this read only"
    result["cells"][0]["hours"][0]["policy"]["identity"] = "changed in this read only"
    assert reader.export_tables() == tables
    assert reader.decode(deepcopy(encoded), consume=True) == grid
    assert reader.decode(encoded) == grid


def test_owned_decode_keeps_metadata_cycle_rejection():
    reader = CompactCodec.from_tables(
        {"source_documents": [], "metadata": [{"nested": {"$metadata": 0}}]}, consume=True
    )
    with pytest.raises(ValueError, match="cyclic"):
        reader.decode({"cells": [{"provenance": {"$metadata": 0}}]}, consume=True)


def _live_grid(evidence):
    """Shared decoded blocks, non-canonical key order, 1.0 floats and tuples."""
    shared_provenance = deepcopy(evidence)
    shared_policy = {
        "weight": 1.0,
        "name": "unchanged_policy",
        "reason": "No invented weights. " * 20,
        "rules": [{"id": index, "text": "exact rule text " * 30} for index in range(2)],
    }
    small_threshold = {"unit": "mm", "value": 2.0}
    hours = [
        {
            "zeta": index,
            "alpha": -0.0 if index else 1e21,
            "tags": ("a", "b"),
            "qpf": {
                "value": float(index),
                "unit": "kg/m2",
                "provenance": shared_provenance,
                "policy": shared_policy,
                "threshold": small_threshold,
            },
            "\U0001f600": {"\ue000": 2**53 - 1, "ö": None},
        }
        for index in range(5)
    ]
    return {"version": "v2", "cells": [{"hours": hours}, {"hours": list(reversed(hours))}]}


def test_canonical_stream_equals_whole_normalized_encoding_and_factors_live_blocks_once(
    source, monkeypatch
):
    from mesoforge.application import baseline_codec

    descriptor, evidence, _ = source
    grid = _live_grid(evidence)
    original = deepcopy(grid)
    normalized = json.loads(canonical_json_bytes(grid))
    reference = CompactCodec([descriptor])
    expected = canonical_json_bytes(reference.encode(normalized))
    canonicalized = []
    real = baseline_codec.canonical_json_bytes
    monkeypatch.setattr(
        baseline_codec,
        "canonical_json_bytes",
        lambda value: canonicalized.append(value) or real(value),
    )
    codec = CompactCodec([descriptor])
    text = "".join(codec.encode_canonical(grid)).encode("utf-8")
    assert text == expected
    assert codec.export_tables() == reference.export_tables()
    assert codec.statistics["metadata_blocks"] == reference.statistics["metadata_blocks"]
    assert codec.statistics["source_references"] == reference.statistics["source_references"]
    # Three distinct live metadata objects are normalized once each, not per cell/hour.
    assert len(canonicalized) == 3
    assert grid == original
    assert isinstance(grid["cells"][0]["hours"][0]["qpf"]["policy"]["weight"], float)
    assert isinstance(grid["cells"][0]["hours"][0]["tags"], tuple)
    decoded = CompactCodec.from_tables(codec.export_tables()).decode(json.loads(text))
    assert decoded == normalized
    assert canonical_json_digest(decoded) == canonical_json_digest(grid)
    live = codec.export_tables(copy=False)
    assert live["metadata"] is codec._metadata and live["source_documents"] is codec._descriptors
    assert codec.export_tables()["metadata"] is not codec._metadata


@pytest.mark.parametrize(
    "grid,error",
    [
        ({"grid": {"$source": []}}, ValueError),
        ({"grid": {"policy": {"$metadata": 0}}}, ValueError),
        ({"grid": {1: "integer key"}}, TypeError),
        ({"grid": {"when": object()}}, TypeError),
    ],
)
def test_canonical_stream_rejects_reserved_keys_and_non_json_members(grid, error):
    codec = CompactCodec([])
    with pytest.raises(error):
        "".join(codec.encode_canonical(grid))
    with pytest.raises(ValueError, match="JSON object"):
        codec.encode_canonical(["not", "an", "object"])
