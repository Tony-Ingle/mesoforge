"""Unit tests for mesoforge.storage.parquet (plan Task 3, Section 3.7)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from mesoforge.storage.parquet import ParquetTableSerializer


def _table() -> pa.Table:
    return pa.table(
        {
            "station_id": ["station.kcbg", "station.kjmr"],
            "event_time": pa.array([1000, 2000], type=pa.int64()),
            "temperature_k": pa.array([273.15, 280.0], type=pa.float64()),
        }
    )


class TestParquetTableSerializer:
    def test_round_trip_preserves_content(self) -> None:
        serializer = ParquetTableSerializer()
        table = _table()
        serialized = serializer.serialize(table)
        deserialized = serializer.deserialize(serialized)
        assert deserialized.equals(table)

    def test_serialize_is_deterministic(self) -> None:
        serializer = ParquetTableSerializer()
        table = _table()
        first = serializer.serialize(table)
        second = serializer.serialize(table)
        assert first == second

    def test_preserves_column_order(self) -> None:
        serializer = ParquetTableSerializer()
        table = _table()
        deserialized = serializer.deserialize(serializer.serialize(table))
        assert deserialized.schema.names == table.schema.names

    def test_deserialize_rejects_malformed_bytes(self) -> None:
        serializer = ParquetTableSerializer()
        with pytest.raises(Exception):  # noqa: B017 -- pyarrow raises its own error types
            serializer.deserialize(b"not a parquet file")
