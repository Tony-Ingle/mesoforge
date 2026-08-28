from datetime import UTC, datetime

import pyarrow as pa

from mesoforge.common.identifiers import ArtifactId, Digest, MatchingPolicyId, StationId
from mesoforge.contracts.verification import MatchedPairRow
from mesoforge.verification.tables import build_matched_pairs_table


def _row(station: str, lead: int) -> MatchedPairRow:
    return MatchedPairRow(
        station_id=StationId(station),
        lead_hours=lead,
        valid_time=datetime(2026, 8, 28, 18 + lead, tzinfo=UTC),
        baseline_artifact_id=ArtifactId("art_00000000-0000-0000-0000-000000000001"),
        observations_artifact_id=ArtifactId("art_00000000-0000-0000-0000-000000000002"),
        matching_policy_id=MatchingPolicyId("metar-nearest-15m.v1"),
        matching_policy_digest=Digest("sha256:" + "a" * 64),
        verification_cutoff=datetime(2026, 8, 29, 1, tzinfo=UTC),
        row_status="matched_no_fields",
        temperature_status="no_report_within_tolerance",
        eastward_component_status="no_report_within_tolerance",
        northward_component_status="no_report_within_tolerance",
        wind_speed_status="no_report_within_tolerance",
        wind_direction_status="no_report_within_tolerance",
    )


def test_table_has_fixed_schema_and_deterministic_station_lead_order() -> None:
    table = build_matched_pairs_table(
        [_row("station.kros", 1), _row("station.kcbg", 1), _row("station.kcbg", 0)]
    )
    assert isinstance(table, pa.Table)
    assert table.column_names[0:4] == ["schema_version", "station_id", "lead_hours", "valid_time"]
    assert list(
        zip(table["station_id"].to_pylist(), table["lead_hours"].to_pylist(), strict=True)
    ) == [
        ("station.kcbg", 0),
        ("station.kcbg", 1),
        ("station.kros", 1),
    ]
    assert table["valid_time"].type == pa.timestamp("us", tz="UTC")


def test_equal_rows_produce_equal_tables() -> None:
    rows = [_row("station.kcbg", 0)]
    assert build_matched_pairs_table(rows).equals(build_matched_pairs_table(list(rows)))
