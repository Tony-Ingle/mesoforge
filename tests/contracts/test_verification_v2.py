from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.contracts.verification import MatchedPairRowV2, MetricRowV2


def _row(**updates: object) -> MatchedPairRowV2:
    values: dict[str, object] = {
        "station_id": "station.kcbg",
        "target_horizon_hours": 1,
        "valid_time": datetime(2026, 1, 1, 1, tzinfo=UTC),
        "precipitation_interval_start": datetime(2026, 1, 1, 0, tzinfo=UTC),
        "precipitation_interval_end": datetime(2026, 1, 1, 1, tzinfo=UTC),
        "baseline_artifact_id": "art_00000000-0000-0000-0000-000000000001",
        "observations_artifact_id": "art_00000000-0000-0000-0000-000000000002",
        "matching_policy_id": "metar-nearest-15m.v1",
        "matching_policy_digest": "sha256:" + "3" * 64,
        "verification_cutoff": datetime(2026, 1, 2, tzinfo=UTC),
        "availability_state": "complete",
        "row_status": "matched_no_fields",
        **{
            f"{name}_status": "no_report_within_tolerance"
            for name in (
                "temperature",
                "dew_point",
                "eastward_component",
                "northward_component",
                "wind_speed",
                "wind_direction",
                "gust",
                "qpf",
                "pop",
            )
        },
    }
    values.update(updates)
    return MatchedPairRowV2.model_validate(values)


def test_v2_contract_is_strict_frozen_and_additive() -> None:
    row = _row()
    assert row.schema_version == "matched-pairs.v2"
    with pytest.raises(ValidationError):
        _row(target_horizon_hours="1")
    with pytest.raises(ValidationError):
        row.row_status = "matched_any_field"  # type: ignore[misc]


def test_v2_contract_rejects_nonfinite_or_value_status_mismatch() -> None:
    with pytest.raises(ValidationError, match="finite"):
        _row(forecast_temperature_k=float("nan"))
    with pytest.raises(ValidationError, match="matched status"):
        _row(temperature_status="matched", row_status="matched_any_field")


def test_v2_contract_requires_horizons_one_through_36() -> None:
    with pytest.raises(ValidationError):
        _row(target_horizon_hours=0)
    with pytest.raises(ValidationError):
        _row(target_horizon_hours=37)


def test_metric_contract_rejects_nonfinite_nested_json_details() -> None:
    with pytest.raises(ValidationError, match="finite"):
        MetricRowV2(
            metric_name="example",
            unit_id="1",
            stratum_kind="overall",
            value=0.0,
            sample_count=1,
            missing_counts={},
            details={"nested": [float("inf")]},
        )
