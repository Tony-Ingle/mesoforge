"""Deterministic Parquet table assembly for the ``matched-pairs.v1``
artifact (plan Section 1.3 design choice 5 / Section 3.8, Task 12).

Mirrors ``observations.tables.build_observations_table``'s pattern:
exact ``(station_id, lead_hours)`` sort order and a fixed schema/column
order.
"""

from __future__ import annotations

import pyarrow as pa

from mesoforge.contracts.verification import MatchedPairRow

_PARQUET_SCHEMA = pa.schema(
    [
        ("schema_version", pa.string()),
        ("station_id", pa.string()),
        ("lead_hours", pa.int64()),
        ("valid_time", pa.timestamp("us", tz="UTC")),
        ("baseline_artifact_id", pa.string()),
        ("observations_artifact_id", pa.string()),
        ("matching_policy_id", pa.string()),
        ("matching_policy_digest", pa.string()),
        ("verification_cutoff", pa.timestamp("us", tz="UTC")),
        ("selected_logical_observation_digest", pa.string()),
        ("selected_revision_digest", pa.string()),
        ("selected_event_time", pa.timestamp("us", tz="UTC")),
        ("selected_provider_available_at", pa.timestamp("us", tz="UTC")),
        ("delta_seconds", pa.float64()),
        ("forecast_temperature_k", pa.float64()),
        ("forecast_eastward_wind_m_s", pa.float64()),
        ("forecast_northward_wind_m_s", pa.float64()),
        ("forecast_wind_speed_m_s", pa.float64()),
        ("forecast_wind_from_direction_degrees", pa.float64()),
        ("observed_temperature_k", pa.float64()),
        ("observed_eastward_wind_m_s", pa.float64()),
        ("observed_northward_wind_m_s", pa.float64()),
        ("observed_wind_speed_m_s", pa.float64()),
        ("observed_wind_from_direction_degrees", pa.float64()),
        ("row_status", pa.string()),
        ("temperature_status", pa.string()),
        ("eastward_component_status", pa.string()),
        ("northward_component_status", pa.string()),
        ("wind_speed_status", pa.string()),
        ("wind_direction_status", pa.string()),
    ]
)


def sort_matched_pairs(rows: list[MatchedPairRow]) -> list[MatchedPairRow]:
    """Deterministic sort by ``(station_id, lead_hours)``."""
    return sorted(rows, key=lambda row: (str(row.station_id), row.lead_hours))


def build_matched_pairs_table(rows: list[MatchedPairRow]) -> pa.Table:
    ordered = sort_matched_pairs(rows)
    columns: dict[str, list[object]] = {field.name: [] for field in _PARQUET_SCHEMA}
    for row in ordered:
        payload = row.model_dump(mode="python")
        for field in _PARQUET_SCHEMA:
            value = payload[field.name]
            if field.name.endswith("_id") or field.name.endswith("_digest"):
                value = str(value) if value is not None else None
            columns[field.name].append(value)

    arrays = [pa.array(columns[field.name], type=field.type) for field in _PARQUET_SCHEMA]
    return pa.Table.from_arrays(arrays, schema=_PARQUET_SCHEMA)
