"""Observation record/Parquet schema contracts (plan Section 2.4/3.7,
Task 8/9).

``RawMetarRecord`` is the strict, provider-record-shaped model that
preserves required fields and rejects malformed types/times (Task 8);
``NormalizedObservation`` is the normalized Phase 1 record with the
Section 3.7 Parquet schema and QC state (Task 9).
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import ArtifactId, Digest, StationId
from mesoforge.common.time import UtcInstant

MesoForgeQcState = Literal["eligible", "partial", "rejected"]


class RawMetarRecord(BaseModel):
    """Section 2.4: required/preserved METAR JSON fields. Rejects an
    unknown station ID (checked by the caller against the station
    catalog), malformed timestamp, non-UTC time, nonfinite numeric
    value, or schema type mismatch. Unknown extra JSON fields are
    preserved in the raw artifact bytes but not modeled here."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    icao_id: str
    obs_time: UtcInstant
    report_time: UtcInstant
    receipt_time: UtcInstant
    temp: float | None = None
    wdir: float | str | None = None
    wspd: float | None = None
    qc_field: float | None = None
    metar_type: str
    raw_ob: str
    lat: float
    lon: float
    elev: float

    @model_validator(mode="after")
    def _check_finite_numbers(self) -> RawMetarRecord:
        errors: list[str] = []
        for name, value in (
            ("temp", self.temp),
            ("wspd", self.wspd),
            ("qc_field", self.qc_field),
            ("lat", self.lat),
            ("lon", self.lon),
            ("elev", self.elev),
        ):
            if value is not None and not math.isfinite(value):
                errors.append(f"{name} must be finite, got {value!r}")
        if isinstance(self.wdir, float) and not math.isfinite(self.wdir):
            errors.append(f"wdir must be finite when numeric, got {self.wdir!r}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @model_validator(mode="after")
    def _check_wdir_shape(self) -> RawMetarRecord:
        if isinstance(self.wdir, str) and self.wdir != "VRB":
            raise ValueError(f"wdir string value must be 'VRB', got {self.wdir!r}")
        return self


class NormalizedObservation(BaseModel):
    """Section 3.7: ``metar-observations.v1`` Parquet row schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    logical_observation_digest: Digest
    revision_digest: Digest
    station_id: StationId
    provider_station_id: str
    event_time: UtcInstant
    report_time: UtcInstant
    provider_available_at: UtcInstant
    ingested_at: UtcInstant
    metar_type: str
    raw_observation: str
    raw_record_digest: Digest
    raw_artifact_id: ArtifactId
    raw_record_index: int
    station_snapshot_artifact_id: ArtifactId
    latitude_degrees: float
    longitude_degrees: float
    elevation_m: float
    temperature_k: float | None = None
    wind_speed_m_s: float | None = None
    wind_from_direction_degrees: float | None = None
    eastward_wind_10m_m_s: float | None = None
    northward_wind_10m_m_s: float | None = None
    provider_qc_field: float | None = None
    mesoforge_qc_state: MesoForgeQcState
    quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_quality_flags_sorted(self) -> NormalizedObservation:
        if tuple(sorted(self.quality_flags)) != self.quality_flags:
            raise ValueError("quality_flags must be deterministically sorted")
        return self

    @model_validator(mode="after")
    def _check_finite_numbers(self) -> NormalizedObservation:
        for name, value in (
            ("temperature_k", self.temperature_k),
            ("wind_speed_m_s", self.wind_speed_m_s),
            ("wind_from_direction_degrees", self.wind_from_direction_degrees),
            ("eastward_wind_10m_m_s", self.eastward_wind_10m_m_s),
            ("northward_wind_10m_m_s", self.northward_wind_10m_m_s),
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite when present, got {value!r}")
        return self
