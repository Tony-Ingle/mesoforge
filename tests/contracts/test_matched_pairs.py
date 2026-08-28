"""Contract tests for mesoforge.contracts.verification.MatchedPairRow
(plan Section 3.8, Task 10)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.verification import MatchedPairRow

_ARTIFACT = "art_00000000-0000-0000-0000-000000000000"
_DIGEST = "sha256:" + "a" * 64


def _row(**overrides: object) -> MatchedPairRow:
    values: dict[str, object] = dict(
        station_id="station.kcbg",
        lead_hours=0,
        valid_time=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        baseline_artifact_id=_ARTIFACT,
        observations_artifact_id=_ARTIFACT,
        matching_policy_id="metar-nearest-15m.v1",
        matching_policy_digest=_DIGEST,
        verification_cutoff=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
        row_status="matched_no_fields",
        temperature_status="no_report_within_tolerance",
        eastward_component_status="no_report_within_tolerance",
        northward_component_status="no_report_within_tolerance",
        wind_speed_status="no_report_within_tolerance",
        wind_direction_status="no_report_within_tolerance",
    )
    values.update(overrides)
    return MatchedPairRow(**values)  # type: ignore[arg-type]


class TestMatchedPairRow:
    def test_accepts_consistent_no_fields_row(self) -> None:
        row = _row()
        assert row.row_status == "matched_no_fields"

    def test_accepts_consistent_any_field_row(self) -> None:
        row = _row(row_status="matched_any_field", temperature_status="matched")
        assert row.temperature_status == "matched"

    def test_rejects_row_status_inconsistent_with_field_statuses(self) -> None:
        with pytest.raises(ValidationError, match="inconsistent"):
            _row(row_status="matched_any_field")  # no field is 'matched'

    def test_rejects_negative_lead_hours(self) -> None:
        with pytest.raises(ValidationError):
            _row(lead_hours=-1)

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            _row(unknown_field=True)
