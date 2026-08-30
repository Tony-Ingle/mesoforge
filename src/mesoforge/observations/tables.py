"""Deterministic Parquet table assembly for normalized METAR
observations (plan Section 3.7, Task 9): exact sort order
``(station_id, event_time, provider_available_at, revision_digest)``
and exact-duplicate-revision deduplication (by digest)."""

from __future__ import annotations

import pyarrow as pa

from mesoforge.contracts.observations import NormalizedObservation

_PARQUET_SCHEMA = pa.schema(
    [
        ("logical_observation_digest", pa.string()),
        ("revision_digest", pa.string()),
        ("station_id", pa.string()),
        ("provider_station_id", pa.string()),
        ("event_time", pa.timestamp("us", tz="UTC")),
        ("report_time", pa.timestamp("us", tz="UTC")),
        ("provider_available_at", pa.timestamp("us", tz="UTC")),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
        ("metar_type", pa.string()),
        ("raw_observation", pa.string()),
        ("raw_record_digest", pa.string()),
        ("raw_artifact_id", pa.string()),
        ("raw_record_index", pa.int64()),
        ("station_snapshot_artifact_id", pa.string()),
        ("latitude_degrees", pa.float64()),
        ("longitude_degrees", pa.float64()),
        ("elevation_m", pa.float64()),
        ("temperature_k", pa.float64()),
        ("wind_speed_m_s", pa.float64()),
        ("wind_from_direction_degrees", pa.float64()),
        ("eastward_wind_10m_m_s", pa.float64()),
        ("northward_wind_10m_m_s", pa.float64()),
        ("provider_qc_field", pa.float64()),
        ("mesoforge_qc_state", pa.string()),
        ("quality_flags", pa.list_(pa.string())),
    ]
)


def deduplicate_exact_revisions(
    observations: list[NormalizedObservation],
) -> list[NormalizedObservation]:
    """Exact-duplicate revisions (same ``revision_digest``) deduplicate;
    a genuinely corrected report has a different ``revision_digest`` and
    is retained as a distinct append-only row (plan Section 3.1/3.7)."""
    seen: dict[str, NormalizedObservation] = {}
    for observation in observations:
        seen.setdefault(str(observation.revision_digest), observation)
    return list(seen.values())


def sort_observations(
    observations: list[NormalizedObservation],
) -> list[NormalizedObservation]:
    """Deterministic sort by ``(station_id, event_time,
    provider_available_at, revision_digest)``."""
    return sorted(
        observations,
        key=lambda o: (
            str(o.station_id),
            o.event_time,
            o.provider_available_at,
            str(o.revision_digest),
        ),
    )


def build_observations_table(observations: list[NormalizedObservation]) -> pa.Table:
    """Assemble the ``metar-observations.v1`` Parquet table: dedup ->
    sort -> exact schema/column order."""
    deduplicated = deduplicate_exact_revisions(observations)
    ordered = sort_observations(deduplicated)

    columns: dict[str, list[object]] = {field.name: [] for field in _PARQUET_SCHEMA}
    for observation in ordered:
        payload = observation.model_dump(mode="python")
        for field in _PARQUET_SCHEMA:
            value = payload[field.name]
            if field.name.endswith("_id") or field.name.endswith("_digest"):
                value = str(value) if value is not None else None
            columns[field.name].append(value)

    arrays = [pa.array(columns[field.name], type=field.type) for field in _PARQUET_SCHEMA]
    return pa.Table.from_arrays(arrays, schema=_PARQUET_SCHEMA)
