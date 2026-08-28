"""Contract tests for mesoforge.contracts.verification.MetricRow and
VerificationReport (plan Section 3.9, Task 11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.verification import MetricRow, MissingCounts, VerificationReport

_ARTIFACT = "art_00000000-0000-0000-0000-000000000000"
_DIGEST = "sha256:" + "a" * 64


def _row(**overrides: object) -> MetricRow:
    values: dict[str, object] = dict(
        metric_set_id="phase1-temperature-wind.v1",
        metric_name="temperature_bias",
        unit_id="K",
        stratum_kind="overall",
        value=0.5,
        sample_count=1,
        missing_counts=MissingCounts(),
        baseline_artifact_id=_ARTIFACT,
        matched_pairs_artifact_id=_ARTIFACT,
        matching_policy_digest=_DIGEST,
        matching_policy_id="metar-nearest-15m.v1",
        verification_cutoff=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
    )
    values.update(overrides)
    return MetricRow(**values)  # type: ignore[arg-type]


class TestMetricRow:
    def test_accepts_valid_row(self) -> None:
        row = _row()
        assert row.value == pytest.approx(0.5)

    def test_rejects_nonnull_value_with_zero_samples(self) -> None:
        with pytest.raises(ValidationError, match="null"):
            _row(value=0.5, sample_count=0)

    def test_rejects_null_value_with_positive_samples(self) -> None:
        with pytest.raises(ValidationError, match="non-null"):
            _row(value=None, sample_count=1)

    def test_accepts_null_value_with_zero_samples(self) -> None:
        row = _row(value=None, sample_count=0)
        assert row.value is None

    def test_rejects_overall_stratum_with_lead_hours(self) -> None:
        with pytest.raises(ValidationError):
            _row(stratum_kind="overall", stratum_lead_hours=0)

    def test_rejects_by_lead_stratum_missing_lead_hours(self) -> None:
        with pytest.raises(ValidationError):
            _row(stratum_kind="by_lead")

    def test_rejects_by_station_stratum_missing_station_id(self) -> None:
        with pytest.raises(ValidationError):
            _row(stratum_kind="by_station")

    def test_accepts_by_lead_stratum_with_lead_hours(self) -> None:
        row = _row(stratum_kind="by_lead", stratum_lead_hours=3)
        assert row.stratum_lead_hours == 3


class TestMissingCounts:
    def test_total_sums_all_reasons(self) -> None:
        counts = MissingCounts(no_report_within_tolerance=2, station_metadata_conflict=1)
        assert counts.total == 3

    def test_rejects_negative_count(self) -> None:
        with pytest.raises(ValidationError):
            MissingCounts(no_report_within_tolerance=-1)


class TestVerificationReport:
    def test_accepts_rows_tuple(self) -> None:
        report = VerificationReport(rows=(_row(),))
        assert len(report.rows) == 1
        assert report.schema_version == "verification-report.v1"
