"""Digest streaming preserves the pinned JCS contract without whole-document buffers."""

from __future__ import annotations

import gzip
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


CHUNK_PAYLOADS = [
    {"\ue000": "private", "\U0001f600": "astral", "\r": "control", "ö": "accent"},
    {"text": 'Grasston — 雪 ☀ / \\ "\b\f\n\r\t\x00\x1f', "empty": {}, "none": []},
    {"numbers": [-0.0, 0, 1.0, 333333333.33333329, 1e30, 1e-27, 1e-7, 1e21, 5e-324, 2**53 - 1]},
    {"nested": [{"values": [True, False, None, {}, []]}], "tuple": (1, "two", 3.0)},
]


@pytest.mark.parametrize("payload", CHUNK_PAYLOADS)
def test_chunks_and_gzip_match_the_pinned_one_shot_bytes(payload: dict[str, Any]) -> None:
    canonical = jcs.canonicalize(payload)
    assert "".join(serialization.canonical_json_chunks(payload)).encode("utf-8") == canonical
    assert serialization.canonical_json_gzip(
        serialization.canonical_json_chunks(payload)
    ) == gzip.compress(canonical, mtime=0)


@pytest.mark.parametrize(
    "value", ["", '☔ "quoted"\n', None, True, False, 0, -0.0, 1.0, 1e21, 1e-7, 2**63, 0.1]
)
def test_scalar_text_matches_the_pinned_encoder(value: Any) -> None:
    assert serialization.canonical_json_scalar(value) == jcs.canonicalize(value).decode("utf-8")


@pytest.mark.parametrize("value", [{}, [], (), object(), b"bytes", math.nan])
def test_scalar_text_rejects_containers_and_unsupported_values(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        serialization.canonical_json_scalar(value)


def test_gzip_stream_crosses_its_buffer_boundary_without_whole_text(monkeypatch) -> None:
    row = {"value": 274.125, "evidence": "native provenance ☔ " * 60, "valid": True}
    payload = {"hours": [row] * 4000}
    canonical = jcs.canonicalize(payload)
    assert len(canonical) > 3 * serialization._STREAM_BUFFER_BYTES
    expected = gzip.compress(canonical, mtime=0)
    del canonical

    def forbidden(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("Streaming gzip must not allocate the complete text")

    monkeypatch.setattr(serialization, "canonical_json_bytes", forbidden)
    monkeypatch.setattr(jcs, "canonicalize", forbidden)
    monkeypatch.setattr(gzip, "compress", forbidden)
    assert (
        serialization.canonical_json_gzip(serialization.canonical_json_chunks(payload)) == expected
    )
