"""Unit tests for mesoforge.observations.tables (plan Section 3.7,
Task 9): exact-duplicate dedup, deterministic sort, Parquet round
trip."""

from __future__ import annotations

from datetime import UTC, datetime

from mesoforge.contracts.observations import NormalizedObservation
from mesoforge.observations.tables import build_observations_table, deduplicate_exact_revisions
from mesoforge.storage.parquet import ParquetTableSerializer


def _observation(
    *,
    station_id: str = "station.kcbg",
    event_time: datetime = datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
    provider_available_at: datetime = datetime(2026, 8, 28, 18, 1, tzinfo=UTC),
    revision_digest: str = "sha256:" + "a" * 64,
    temperature_k: float | None = 288.15,
) -> NormalizedObservation:
    return NormalizedObservation(
        logical_observation_digest="sha256:" + "b" * 64,
        revision_digest=revision_digest,  # type: ignore[arg-type]
        station_id=station_id,  # type: ignore[arg-type]
        provider_station_id="KCBG",
        event_time=event_time,
        report_time=event_time,
        provider_available_at=provider_available_at,
        ingested_at=event_time,
        metar_type="METAR",
        raw_observation="KCBG 281800Z 27010KT 10SM CLR 15/10 A3000",
        raw_record_digest="sha256:" + "c" * 64,  # type: ignore[arg-type]
        raw_artifact_id="art_" + "0" * 8 + "-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        raw_record_index=0,
        station_snapshot_artifact_id="art_" + "1" * 8 + "-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        latitude_degrees=45.557,
        longitude_degrees=-93.264,
        elevation_m=285.0,
        temperature_k=temperature_k,
        mesoforge_qc_state="partial",
    )


class TestDeduplicateExactRevisions:
    def test_deduplicates_identical_revision_digest(self) -> None:
        a = _observation()
        b = _observation()  # identical revision_digest
        result = deduplicate_exact_revisions([a, b])
        assert len(result) == 1

    def test_retains_distinct_revision_as_correction(self) -> None:
        a = _observation(revision_digest="sha256:" + "a" * 64, temperature_k=288.15)
        b = _observation(revision_digest="sha256:" + "d" * 64, temperature_k=289.0)
        result = deduplicate_exact_revisions([a, b])
        assert len(result) == 2


class TestBuildObservationsTable:
    def test_sorts_by_station_then_event_time(self) -> None:
        early = _observation(
            station_id="station.kcbg",
            event_time=datetime(2026, 8, 28, 18, 0, tzinfo=UTC),
            revision_digest="sha256:" + "a" * 64,
        )
        late = _observation(
            station_id="station.kcbg",
            event_time=datetime(2026, 8, 28, 19, 0, tzinfo=UTC),
            revision_digest="sha256:" + "e" * 64,
        )
        other_station = _observation(
            station_id="station.kjmr",
            event_time=datetime(2026, 8, 28, 17, 0, tzinfo=UTC),
            revision_digest="sha256:" + "f" * 64,
        )
        table = build_observations_table([late, other_station, early])
        stations = table.column("station_id").to_pylist()
        assert stations == ["station.kcbg", "station.kcbg", "station.kjmr"]

    def test_parquet_round_trip_preserves_content(self) -> None:
        observation = _observation()
        table = build_observations_table([observation])
        serializer = ParquetTableSerializer()
        serialized = serializer.serialize(table)
        deserialized = serializer.deserialize(serialized)
        assert deserialized.num_rows == 1
        assert deserialized.column("station_id").to_pylist() == ["station.kcbg"]

    def test_nullable_fields_round_trip_as_null(self) -> None:
        observation = _observation(temperature_k=None)
        table = build_observations_table([observation])
        assert table.column("temperature_k").to_pylist() == [None]

    def test_table_dedups_and_sorts_together(self) -> None:
        a = _observation(revision_digest="sha256:" + "a" * 64)
        duplicate = _observation(revision_digest="sha256:" + "a" * 64)
        table = build_observations_table([a, duplicate])
        assert table.num_rows == 1
