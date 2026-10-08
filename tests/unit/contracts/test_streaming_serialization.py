"""Digest streaming preserves the pinned JCS contract without whole-document buffers."""

from __future__ import annotations

import hashlib
import math
import tracemalloc
from typing import Any

import jcs
import pytest

from mesoforge.common.identifiers import Digest
from mesoforge.contracts import serialization


def expected_digest(payload: dict[str, Any]) -> Digest:
    return Digest.of_bytes(jcs.canonicalize(payload))


@pytest.mark.parametrize(
    "payload",
    [
        # UTF-16 ordering puts the surrogate-pair key before the private-use key;
        # sorting Unicode code points instead would silently change saved identity.
        {"\ue000": "private", "\U0001f600": "astral", "\r": "control", "ö": "accent"},
        {"text": 'Grasston — 雪 ☀ / \\ "\b\f\n\r\t\x00\x1f'},
        {
            "numbers": [
                -0.0,
                0,
                1.0,
                333333333.33333329,
                1e30,
                1e-27,
                1e-7,
                1e-6,
                1e20,
                1e21,
                5e-324,
                1.7976931348623157e308,
                9007199254740991,
            ]
        },
        {"nested": [{"values": [True, False, None, {}, []]}], "empty": {}},
    ],
)
def test_streaming_digest_matches_pinned_jcs_bytes(payload: dict[str, Any]) -> None:
    assert serialization.canonical_json_digest(payload) == expected_digest(payload)


def test_shared_subtrees_remain_values_and_digest_does_not_materialize_bytes(monkeypatch) -> None:
    native = {"value": 1.0, "metadata": {"units": "mm", "label": "☔"}}
    payload = {"point": native, "grid": [native, {"nested": native}]}
    expected = expected_digest(payload)

    def forbidden(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("Digest must stream, not allocate the complete canonical payload")

    monkeypatch.setattr(serialization, "canonical_json_bytes", forbidden)
    monkeypatch.setattr(jcs, "canonicalize", forbidden)
    assert serialization.canonical_json_digest(payload) == expected
    assert payload["point"] is payload["grid"][0]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_streaming_digest_rejects_nonfinite_values_like_jcs(value: float) -> None:
    payload = {"nested": [{"invalid": value}]}
    with pytest.raises(ValueError):
        jcs.canonicalize(payload)
    with pytest.raises(ValueError):
        serialization.canonical_json_digest(payload)


def test_streaming_digest_rejects_cycles_but_not_shared_subtrees() -> None:
    payload: dict[str, Any] = {"cycle": []}
    payload["cycle"].append(payload)
    with pytest.raises(ValueError, match="Circular reference"):
        jcs.canonicalize(payload)
    with pytest.raises(ValueError, match="Circular reference"):
        serialization.canonical_json_digest(payload)


def test_streaming_digest_has_bounded_auxiliary_memory_for_repeated_payload() -> None:
    # About 1.6 MiB serialized, with bounded nesting/chunk size. Construct the
    # document and reference digest before tracing auxiliary encoder allocations.
    row = {"value": 274.125, "evidence": "native provenance " * 60, "valid": True}
    payload = {"hours": [row] * 1600}
    canonical = jcs.canonicalize(payload)
    expected = Digest("sha256:" + hashlib.sha256(canonical).hexdigest())
    serialized_size = len(canonical)
    del canonical
    require_standalone_trace = not tracemalloc.is_tracing()
    if not require_standalone_trace:
        pytest.skip("An outer memory profiler owns tracemalloc; do not reset its state")
    tracemalloc.start()
    try:
        assert serialization.canonical_json_digest(payload) == expected
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Catch a whole Unicode/bytes payload or a list of every encoder fragment.
    # Allow ample interpreter variation without tying the check to exact bytes.
    assert peak < serialized_size // 4
