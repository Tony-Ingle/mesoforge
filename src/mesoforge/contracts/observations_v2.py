"""Phase 2 METAR record/schema extension (plan Section 6.1, Task 11):
``metar-observations.v2`` adds dew point, explicit wind gust, and raw
routine hourly liquid precipitation (``Prrrr``) truth. Distinct,
additive models -- ``RawMetarRecord``/``NormalizedObservation`` (v1)
remain unchanged.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from mesoforge.common.identifiers import ArtifactId, Digest, StationId
from mesoforge.common.time import UtcInstant
from mesoforge.contracts.observations import MesoForgeQcState, RawMetarRecord


class RawMetarRecordV2(RawMetarRecord):
    """Section 6.1: adds ``dewp`` (C) and ``wgst`` (knots) to the
    retained provider fields. ``raw_ob`` (inherited) remains the exact
    routine METAR text used for ``Prrrr`` group parsing."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dewp: float | None = None
    wgst: float | None = None

    @model_validator(mode="after")
    def _check_finite_v2_fields(self) -> RawMetarRecordV2:
        for name, value in (("dewp", self.dewp), ("wgst", self.wgst)):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
        return self


class NormalizedObservationV2(BaseModel):
    """Section 3.7/6.1: ``metar-observations.v2`` Parquet row schema.
    Adds dew point, gust, and hourly precipitation truth to the v1
    schema fields. Precipitation truth uses an explicit tri-state:
    ``precipitation_amount_kg_m2`` is ``None`` with
    ``precipitation_truth_status='missing'`` when no ``Prrrr`` group is
    present (missing is never zero, per Section 6.1)."""

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
    dew_point_k: float | None = None
    wind_speed_m_s: float | None = None
    wind_from_direction_degrees: float | None = None
    eastward_wind_10m_m_s: float | None = None
    northward_wind_10m_m_s: float | None = None
    wind_gust_m_s: float | None = None
    precipitation_amount_kg_m2: float | None = None
    precipitation_truth_status: Literal["reported", "missing", "malformed"] = "missing"
    precipitation_interval_start: UtcInstant | None = None
    precipitation_interval_end: UtcInstant | None = None
    provider_qc_field: float | None = None
    mesoforge_qc_state: MesoForgeQcState
    quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_quality_flags_sorted(self) -> NormalizedObservationV2:
        if tuple(sorted(self.quality_flags)) != self.quality_flags:
            raise ValueError("quality_flags must be deterministically sorted")
        return self

    @model_validator(mode="after")
    def _check_finite_numbers(self) -> NormalizedObservationV2:
        for name, value in (
            ("temperature_k", self.temperature_k),
            ("dew_point_k", self.dew_point_k),
            ("wind_speed_m_s", self.wind_speed_m_s),
            ("wind_from_direction_degrees", self.wind_from_direction_degrees),
            ("eastward_wind_10m_m_s", self.eastward_wind_10m_m_s),
            ("northward_wind_10m_m_s", self.northward_wind_10m_m_s),
            ("wind_gust_m_s", self.wind_gust_m_s),
            ("precipitation_amount_kg_m2", self.precipitation_amount_kg_m2),
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite when present, got {value!r}")
        return self

    @model_validator(mode="after")
    def _check_precipitation_status_consistency(self) -> NormalizedObservationV2:
        if self.precipitation_truth_status == "reported":
            if self.precipitation_amount_kg_m2 is None:
                raise ValueError(
                    "precipitation_truth_status='reported' requires a non-null "
                    "precipitation_amount_kg_m2"
                )
        else:
            if self.precipitation_amount_kg_m2 is not None:
                raise ValueError(
                    f"precipitation_truth_status={self.precipitation_truth_status!r} requires "
                    "precipitation_amount_kg_m2 to be null (missing is never zero)"
                )
        return self
