"""Canonical-JSON ``DatasetSerializer``-shaped adapter (plan Section 4.4).

Implements ``storage.interfaces.DatasetSerializer`` structurally
(``serialize``/``deserialize``) for ``dict[str, Any]`` payloads rather
than ``xarray.Dataset`` -- used as the ``serializer`` argument to
``ArtifactService.execute_role_bound_transformation`` for any Phase 1
activity whose *output* is a canonical-JSON artifact (acquisition
manifest, variable lineage manifest, point-extraction report,
verification report).
"""

from __future__ import annotations

from typing import Any

from mesoforge.contracts.serialization import canonical_json_bytes, parse_canonical_json


class CanonicalJsonSerializer:
    """Concrete ``storage.interfaces.DatasetSerializer`` for canonical
    JSON dict payloads."""

    def serialize(self, dataset: dict[str, Any]) -> bytes:
        return canonical_json_bytes(dataset)

    def deserialize(self, payload: bytes) -> dict[str, Any]:
        return parse_canonical_json(payload)
