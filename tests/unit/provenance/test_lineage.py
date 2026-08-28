"""Unit tests for mesoforge.provenance.lineage (Task 7, plan Section 4.8,
LineageGraph construction/ordering/cycle detection).

RED: written before src/mesoforge/provenance/lineage.py exists.
"""

from __future__ import annotations

import pytest

from mesoforge.common.errors import InvalidIdentifier, LineageViolation
from mesoforge.provenance.lineage import (
    ActivityEdge,
    build_lineage_graph,
    detect_cycle,
)

_ART_A = "art_00000000-0000-0000-0000-000000000001"
_ART_B = "art_00000000-0000-0000-0000-000000000002"
_ART_C = "art_00000000-0000-0000-0000-000000000003"
_ACT_1 = "act_00000000-0000-0000-0000-000000000001"
_ACT_2 = "act_00000000-0000-0000-0000-000000000002"


class TestBuildLineageGraph:
    def test_root_artifact_with_no_producer(self) -> None:
        graph = build_lineage_graph(root_artifact_id=_ART_A, edges=())
        assert graph.root_artifact_id == _ART_A
        assert graph.schema_version == "lineage-graph.v1"

    def test_single_activity_chain(self) -> None:
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
        )
        graph = build_lineage_graph(root_artifact_id=_ART_B, edges=edges)
        assert _ART_A in graph.artifact_nodes
        assert _ART_B in graph.artifact_nodes
        assert _ACT_1 in graph.activity_nodes

    def test_artifact_nodes_are_canonically_sorted(self) -> None:
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_C, role="primary", direction="input"),
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
        )
        graph = build_lineage_graph(root_artifact_id=_ART_B, edges=edges)
        assert graph.artifact_nodes == tuple(sorted(graph.artifact_nodes))

    def test_activity_nodes_are_canonically_sorted(self) -> None:
        edges = (
            ActivityEdge(activity_id=_ACT_2, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="input"),
        )
        graph = build_lineage_graph(root_artifact_id=_ART_A, edges=edges)
        assert graph.activity_nodes == tuple(sorted(graph.activity_nodes))

    def test_export_json_is_deterministic(self) -> None:
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
        )
        graph_one = build_lineage_graph(root_artifact_id=_ART_B, edges=edges)
        graph_two = build_lineage_graph(root_artifact_id=_ART_B, edges=tuple(reversed(edges)))
        assert graph_one.model_dump_json() == graph_two.model_dump_json()


class TestDetectCycle:
    def test_no_cycle_in_linear_chain(self) -> None:
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
        )
        detect_cycle(
            proposed_input_artifact_id=_ART_A, proposed_output_artifact_id=_ART_B, edges=edges
        )

    def test_direct_cycle_is_rejected(self) -> None:
        # A produces B; proposing B -> A would create a 2-cycle.
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
        )
        with pytest.raises(LineageViolation):
            detect_cycle(
                proposed_input_artifact_id=_ART_B, proposed_output_artifact_id=_ART_A, edges=edges
            )

    def test_indirect_cycle_is_rejected(self) -> None:
        # A -> (act1) -> B -> (act2) -> C; proposing C -> A would create a cycle.
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
            ActivityEdge(activity_id=_ACT_2, artifact_id=_ART_B, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_2, artifact_id=_ART_C, role="primary", direction="output"
            ),
        )
        with pytest.raises(LineageViolation):
            detect_cycle(
                proposed_input_artifact_id=_ART_C, proposed_output_artifact_id=_ART_A, edges=edges
            )


class TestLineageGraphModel:
    def test_frozen(self) -> None:
        graph = build_lineage_graph(root_artifact_id=_ART_A, edges=())
        with pytest.raises(Exception):  # noqa: B017 - pydantic frozen model error
            graph.root_artifact_id = _ART_B  # type: ignore[misc]


class TestActivityEdgeRejectsMalformedRuntimeValues:
    """Direct model-construction probes reproducing the Codex-review
    finding: ``ActivityEdge`` must reject malformed activity/artifact
    IDs, not silently accept an unrestricted str."""

    def test_rejects_malformed_activity_id(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            ActivityEdge(
                activity_id="bad-activity", artifact_id=_ART_A, role="primary", direction="input"
            )

    def test_rejects_malformed_artifact_id(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            ActivityEdge(
                activity_id=_ACT_1, artifact_id="bad-artifact", role="primary", direction="input"
            )


class TestBuildLineageGraphRejectsMalformedRuntimeValues:
    """Direct function-call probes reproducing the Codex-review finding:
    ``build_lineage_graph(root_artifact_id="not-an-artifact-id", ...)``
    must fail closed with ``InvalidIdentifier``, not silently succeed."""

    def test_rejects_malformed_root_artifact_id(self) -> None:
        with pytest.raises(InvalidIdentifier):
            build_lineage_graph(root_artifact_id="not-an-artifact-id", edges=())

    def test_reproduction_malformed_root_and_edge_fails_closed(self) -> None:
        """Exact reproduction from the Codex review: a malformed root
        artifact ID together with an ``ActivityEdge`` built from
        malformed activity/artifact IDs must both be rejected."""
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            build_lineage_graph(
                root_artifact_id="not-an-artifact-id",
                edges=(
                    ActivityEdge(
                        activity_id="bad-activity",
                        artifact_id="bad-artifact",
                        role="primary",
                        direction="input",
                    ),
                ),
            )

    def test_valid_round_trip_still_succeeds(self) -> None:
        edges = (
            ActivityEdge(activity_id=_ACT_1, artifact_id=_ART_A, role="primary", direction="input"),
            ActivityEdge(
                activity_id=_ACT_1, artifact_id=_ART_B, role="primary", direction="output"
            ),
        )
        graph = build_lineage_graph(root_artifact_id=_ART_B, edges=edges)
        assert graph.root_artifact_id == _ART_B
        assert set(graph.artifact_nodes) == {_ART_A, _ART_B}
        assert graph.activity_nodes == (_ACT_1,)


class TestDetectCycleRejectsMalformedRuntimeValues:
    def test_rejects_malformed_proposed_input_artifact_id(self) -> None:
        with pytest.raises(InvalidIdentifier):
            detect_cycle(
                proposed_input_artifact_id="not-an-artifact-id",
                proposed_output_artifact_id=_ART_A,
                edges=(),
            )

    def test_rejects_malformed_proposed_output_artifact_id(self) -> None:
        with pytest.raises(InvalidIdentifier):
            detect_cycle(
                proposed_input_artifact_id=_ART_A,
                proposed_output_artifact_id="not-an-artifact-id",
                edges=(),
            )
