"""Lineage graph construction and cycle detection (plan Section 4.8).

``LineageGraph`` is reconstructible purely from persisted activity
input/output edges. Sorting is by canonical ID so JSON export is
deterministic regardless of edge insertion order.

Every activity/artifact ID field or parameter below uses the typed
``common.identifiers`` classes (``ArtifactId``, ``ActivityId``), not an
unrestricted ``str`` (Codex re-review HIGH finding: the earlier
remediation left ``ActivityEdge``, ``LineageEdgeView``, ``LineageGraph``,
``build_lineage_graph``, and ``detect_cycle`` string-typed, so a runtime
caller supplying a malformed ID was silently accepted). Pydantic
enforces the typed fields at model construction; the two plain
functions (``build_lineage_graph``, ``detect_cycle``) additionally
reconstruct their own ID arguments so a caller that bypasses static type
checking still fails closed with ``InvalidIdentifier``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from mesoforge.common.errors import LineageViolation
from mesoforge.common.identifiers import ActivityId, ArtifactId


class ActivityEdge(BaseModel):
    """One row of the activity_inputs/activity_outputs join: which
    artifact played which role, in which direction, for which activity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity_id: ActivityId
    artifact_id: ArtifactId
    role: str
    direction: Literal["input", "output"]


class LineageEdgeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity_id: ActivityId
    artifact_id: ArtifactId
    role: str
    direction: Literal["input", "output"]


class LineageGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["lineage-graph.v1"] = "lineage-graph.v1"
    root_artifact_id: ArtifactId
    artifact_nodes: tuple[ArtifactId, ...]
    activity_nodes: tuple[ActivityId, ...]
    edges: tuple[LineageEdgeView, ...]


def _edge_sort_key(edge: ActivityEdge) -> tuple[str, str, str, str]:
    return (edge.activity_id, edge.direction, edge.role, edge.artifact_id)


def build_lineage_graph(
    *, root_artifact_id: ArtifactId, edges: tuple[ActivityEdge, ...]
) -> LineageGraph:
    """Build a deterministic LineageGraph from an unordered set of
    persisted activity edges. Node and edge ordering never depends on
    the order ``edges`` was supplied in.

    ``root_artifact_id`` and every edge's IDs are reconstructed through
    their typed classes here, so a runtime caller bypassing static type
    checking (e.g. passing a plain malformed ``str``) fails closed with
    ``InvalidIdentifier`` before a graph is built.
    """
    root_artifact_id = ArtifactId(root_artifact_id)

    artifact_ids: set[ArtifactId] = {root_artifact_id}
    activity_ids: set[ActivityId] = set()
    for edge in edges:
        artifact_ids.add(edge.artifact_id)
        activity_ids.add(edge.activity_id)

    sorted_edges = sorted(edges, key=_edge_sort_key)
    edge_views = tuple(
        LineageEdgeView(
            activity_id=e.activity_id,
            artifact_id=e.artifact_id,
            role=e.role,
            direction=e.direction,
        )
        for e in sorted_edges
    )

    return LineageGraph(
        root_artifact_id=root_artifact_id,
        artifact_nodes=tuple(sorted(artifact_ids)),
        activity_nodes=tuple(sorted(activity_ids)),
        edges=edge_views,
    )


def _build_producer_map(edges: tuple[ActivityEdge, ...]) -> dict[ArtifactId, ActivityId]:
    """artifact_id -> activity_id for each artifact's producing activity
    (output edges only)."""
    producers: dict[ArtifactId, ActivityId] = {}
    for edge in edges:
        if edge.direction == "output":
            producers[edge.artifact_id] = edge.activity_id
    return producers


def _build_consumer_map(edges: tuple[ActivityEdge, ...]) -> dict[ActivityId, list[ArtifactId]]:
    """activity_id -> list of artifact_ids it consumed (input edges only)."""
    consumers: dict[ActivityId, list[ArtifactId]] = {}
    for edge in edges:
        if edge.direction == "input":
            consumers.setdefault(edge.activity_id, []).append(edge.artifact_id)
    return consumers


def detect_cycle(
    *,
    proposed_input_artifact_id: ArtifactId,
    proposed_output_artifact_id: ArtifactId,
    edges: tuple[ActivityEdge, ...],
) -> None:
    """Raise LineageViolation if adding an activity that consumes
    ``proposed_input_artifact_id`` and produces
    ``proposed_output_artifact_id`` would create a cycle: i.e. if
    ``proposed_output_artifact_id`` is already an ancestor of
    ``proposed_input_artifact_id``.

    Both proposed IDs are reconstructed through ``ArtifactId`` here, so a
    runtime caller bypassing static type checking fails closed with
    ``InvalidIdentifier`` before the graph walk runs.
    """
    proposed_input_artifact_id = ArtifactId(proposed_input_artifact_id)
    proposed_output_artifact_id = ArtifactId(proposed_output_artifact_id)

    producers = _build_producer_map(edges)
    consumers = _build_consumer_map(edges)

    # Walk ancestors of proposed_input_artifact_id; if we reach
    # proposed_output_artifact_id, adding this edge would close a cycle.
    visited: set[ArtifactId] = set()
    stack = [proposed_input_artifact_id]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current == proposed_output_artifact_id:
            raise LineageViolation(
                f"adding artifact {proposed_output_artifact_id!r} as an ancestor of "
                f"{proposed_input_artifact_id!r} would create a lineage cycle"
            )
        producing_activity = producers.get(current)
        if producing_activity is None:
            continue
        for ancestor in consumers.get(producing_activity, []):
            if ancestor not in visited:
                stack.append(ancestor)
