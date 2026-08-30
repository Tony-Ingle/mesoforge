"""Unit tests for mesoforge.contracts.serialization and
mesoforge.storage.json (plan Task 3, Section 3.5/4.4)."""

from __future__ import annotations

import pytest

from mesoforge.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_digest,
    parse_canonical_json,
)
from mesoforge.storage.json import CanonicalJsonSerializer


class TestCanonicalJsonBytes:
    def test_key_order_does_not_affect_output(self) -> None:
        a = canonical_json_bytes({"b": 1, "a": 2})
        b = canonical_json_bytes({"a": 2, "b": 1})
        assert a == b

    def test_nested_key_order_does_not_affect_output(self) -> None:
        a = canonical_json_bytes({"outer": {"z": 1, "y": 2}})
        b = canonical_json_bytes({"outer": {"y": 2, "z": 1}})
        assert a == b

    def test_different_content_produces_different_bytes(self) -> None:
        a = canonical_json_bytes({"value": 1})
        b = canonical_json_bytes({"value": 2})
        assert a != b

    def test_digest_is_deterministic(self) -> None:
        payload = {"station": "KCBG", "temp_k": 273.15}
        assert canonical_json_digest(payload) == canonical_json_digest(dict(payload))


class TestParseCanonicalJson:
    def test_round_trips_a_dict(self) -> None:
        payload = {"a": 1, "b": [1, 2, 3], "c": {"d": "e"}}
        encoded = canonical_json_bytes(payload)
        assert parse_canonical_json(encoded) == payload

    def test_rejects_non_object_top_level(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            parse_canonical_json(b"[1, 2, 3]")

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(ValueError):
            parse_canonical_json(b"{not valid json")


class TestCanonicalJsonSerializer:
    def test_round_trip(self) -> None:
        serializer = CanonicalJsonSerializer()
        payload = {"artifact_type": "hrrr-acquisition-manifest", "leads": [0, 1, 2]}
        serialized = serializer.serialize(payload)
        assert serializer.deserialize(serialized) == payload

    def test_serialize_is_deterministic_across_key_order(self) -> None:
        serializer = CanonicalJsonSerializer()
        first = serializer.serialize({"b": 1, "a": 2})
        second = serializer.serialize({"a": 2, "b": 1})
        assert first == second
