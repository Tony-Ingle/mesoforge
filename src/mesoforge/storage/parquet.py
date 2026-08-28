"""Deterministic Parquet artifact serializer (plan Section 3.7/4.4).

Wraps ``pyarrow.parquet`` with a fixed, minimal write configuration so
identical table content always produces byte-identical Parquet bytes:
no dictionary encoding (which can otherwise vary by write-time
statistics), a pinned format version, and no embedded wall-clock
"created_by"/statistics variance beyond what pyarrow itself pins per
version. Column order is exactly the caller-supplied
``pyarrow.Table``'s schema order; callers own deterministic row sort
order (e.g. Section 3.7's ``(station_id, event_time,
provider_available_at, revision_digest)``) before calling
``serialize``.
"""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq


class ParquetTableSerializer:
    """Concrete ``storage.interfaces.DatasetSerializer``-shaped adapter
    for ``pyarrow.Table`` payloads (observation/matched-pairs artifacts)."""

    def serialize(self, dataset: pa.Table) -> bytes:
        buffer = io.BytesIO()
        pq.write_table(
            dataset,
            buffer,
            version="2.6",
            use_dictionary=False,
            write_statistics=False,
            compression="snappy",
        )
        return buffer.getvalue()

    def deserialize(self, payload: bytes) -> pa.Table:
        buffer = io.BytesIO(payload)
        return pq.read_table(buffer)
