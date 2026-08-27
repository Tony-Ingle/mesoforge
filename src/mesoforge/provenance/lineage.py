"""Lineage graph construction and cycle detection (plan Section 4.8).

``LineageGraph`` is reconstructible purely from persisted activity
input/output edges. Sorting is by canonical ID so JSON export is
deterministic regardless of edge insertion order.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from mesoforge.common.errors import LineageViolation


class ActivityEdge(BaseModel):
    """One row of the activity_inputs/activity_outputs join: which
    artifact played which role, in which direction, for which activity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity_id: str
    artifact_id: str
    role: str
    direction: Literal["input", "output"]


class LineageEdgeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    activity_id: str
    artifact_id: str
    role: str
    direction: Literal["input", "output"]


class LineageGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "lineage-graph.v1"
    root_artifact_id: str
    artifact_nodes: tuple[str, ...]
    activity_nodes: tuple[str, ...]
    edges: tuple[LineageEdgeView, ...]


def _edge_sort_key(edge: ActivityEdge) -> tuple[str, str, str, str]:
    return (edge.activity_id, edge.direction, edge.role, edge.artifact_id)


def build_lineage_graph(*, root_artifact_id: str, edges: tuple[ActivityEdge, ...]) -> LineageGraph:
    """Build a deterministic LineageGraph from an unordered set of
    persisted activity edges. Node and edge ordering never depends on
    the order ``edges`` was supplied in."""
    artifact_ids = {root_artifact_id}
    activity_ids: set[str] = set()
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


def _build_producer_map(edges: tuple[ActivityEdge, ...]) -> dict[str, str]:
    """artifact_id -> activity_id for each artifact's producing activity
    (output edges only)."""
    producers: dict[str, str] = {}
    for edge in edges:
        if edge.direction == "output":
            producers[edge.artifact_id] = edge.activity_id
    return producers


def _build_consumer_map(edges: tuple[ActivityEdge, ...]) -> dict[str, list[str]]:
    """activity_id -> list of artifact_ids it consumed (input edges only)."""
    consumers: dict[str, list[str]] = {}
    for edge in edges:
        if edge.direction == "input":
            consumers.setdefault(edge.activity_id, []).append(edge.artifact_id)
    return consumers


def detect_cycle(
    *,
    proposed_input_artifact_id: str,
    proposed_output_artifact_id: str,
    edges: tuple[ActivityEdge, ...],
) -> None:
    """Raise LineageViolation if adding an activity that consumes
    ``proposed_input_artifact_id`` and produces
    ``proposed_output_artifact_id`` would create a cycle: i.e. if
    ``proposed_output_artifact_id`` is already an ancestor of
    ``proposed_input_artifact_id``."""
    producers = _build_producer_map(edges)
    consumers = _build_consumer_map(edges)

    # Walk ancestors of proposed_input_artifact_id; if we reach
    # proposed_output_artifact_id, adding this edge would close a cycle.
    visited: set[str] = set()
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
