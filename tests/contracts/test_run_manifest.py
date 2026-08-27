"""Contract tests for RunManifest (Task 7, plan Section 4.8).

RED: written before src/mesoforge/contracts/runs.py exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.runs import RunManifest

_VALID_SHA = "a" * 40


def _base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        run_id="run_" + "0" * 8 + "-0000-0000-0000-000000000000",
        forecast_issue_time=datetime(2026, 1, 1, tzinfo=UTC),
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        configuration_snapshot_id="cfg_sha256_" + "a" * 64,
        configuration_digest="sha256:" + "a" * 64,
        code_revision=_VALID_SHA,
        environment_digest="sha256:" + "b" * 64,
        lockfile_digest="sha256:" + "c" * 64,
        random_seed=42,
        selected_input_artifact_ids=(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    kwargs.update(overrides)
    return kwargs


class TestRunManifest:
    def test_valid_manifest_constructs(self) -> None:
        manifest = RunManifest(**_base_kwargs())
        assert manifest.schema_version == "run-manifest.v1"

    def test_rejects_short_code_revision(self) -> None:
        with pytest.raises(ValidationError):
            RunManifest(**_base_kwargs(code_revision="abc123"))

    def test_rejects_uppercase_code_revision(self) -> None:
        with pytest.raises(ValidationError):
            RunManifest(**_base_kwargs(code_revision="A" * 40))

    def test_preserves_selected_input_order(self) -> None:
        ids = (
            "art_00000000-0000-0000-0000-000000000001",
            "art_00000000-0000-0000-0000-000000000002",
        )
        manifest = RunManifest(**_base_kwargs(selected_input_artifact_ids=ids))
        assert manifest.selected_input_artifact_ids == ids

    def test_frozen(self) -> None:
        manifest = RunManifest(**_base_kwargs())
        with pytest.raises(ValidationError):
            manifest.random_seed = 1  # type: ignore[misc]

    def test_forecast_issue_time_distinct_field_from_information_cutoff(self) -> None:
        # Regression guard: these must be independently settable, never aliased.
        issue = datetime(2026, 1, 1, tzinfo=UTC)
        cutoff = datetime(2026, 1, 1, 6, tzinfo=UTC)
        manifest = RunManifest(**_base_kwargs(forecast_issue_time=issue, information_cutoff=cutoff))
        assert manifest.forecast_issue_time == issue
        assert manifest.information_cutoff == cutoff
        assert manifest.forecast_issue_time != manifest.information_cutoff
