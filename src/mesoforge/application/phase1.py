"""Strict orchestration boundary for the Phase 1 operational slice.

Provider I/O and scientific transformations are injected ports. This
module owns only ordering, lifecycle invariants, artifact-role checks,
and construction of the run manifest.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mesoforge.application.artifacts import ArtifactService
from mesoforge.common.identifiers import (
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    validate_code_revision,
)
from mesoforge.common.time import UtcInstant
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.runs import RunManifest
from mesoforge.guidance.interfaces import Clock, Sleeper

_LEADS = tuple(range(7))


class Phase1Request(BaseModel):
    """Complete, replay-identifying input to one Phase 1 lifecycle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    run_id: RunId
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    lockfile_digest: Digest
    cycle: UtcInstant
    forecast_issue_time: UtcInstant
    information_cutoff: UtcInstant
    verification_cutoff: UtcInstant
    random_seed: int = 0

    @field_validator("code_revision")
    @classmethod
    def _validate_revision(cls, value: str) -> str:
        return validate_code_revision(value)

    @model_validator(mode="after")
    def _validate_times(self) -> Phase1Request:
        if any((self.cycle.minute, self.cycle.second, self.cycle.microsecond)):
            raise ValueError("cycle must identify an exact hourly HRRR cycle")
        if self.information_cutoff > self.forecast_issue_time:
            raise ValueError("information_cutoff must not exceed forecast_issue_time")
        if self.forecast_issue_time < self.cycle:
            raise ValueError("forecast_issue_time must not precede cycle")
        if self.verification_cutoff < self.forecast_issue_time:
            raise ValueError("verification_cutoff must not precede forecast_issue_time")
        return self


class HrrrLeadArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    lead_hours: int
    index: ArtifactManifest
    selected_grib: ArtifactManifest


class StationCatalogArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    response: ArtifactManifest
    snapshot: ArtifactManifest


class ForecastArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    acquisition_manifest: ArtifactManifest
    variable_lineage: ArtifactManifest
    canonical_guidance: ArtifactManifest
    extraction_report: ArtifactManifest
    baseline: ArtifactManifest


class ObservationArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    responses: tuple[ArtifactManifest, ...]
    normalized: ArtifactManifest

    @model_validator(mode="after")
    def _require_response(self) -> ObservationArtifacts:
        if not self.responses:
            raise ValueError("at least one METAR response artifact is required")
        return self


class VerificationArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    matched_pairs: ArtifactManifest
    report: ArtifactManifest


class Phase1Result(BaseModel):
    """All durable outputs required to replay and audit the run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    run: RunManifest
    station_catalog: StationCatalogArtifacts
    hrrr: tuple[HrrrLeadArtifacts, ...]
    forecast: ForecastArtifacts
    observations: ObservationArtifacts
    verification: VerificationArtifacts


class Phase1SourcePort(Protocol):
    """Provider acquisition plus exact-byte source registration."""

    def register_station_catalog(
        self,
        request: Phase1Request,
        *,
        artifact_service: ArtifactService,
        clock: Clock,
        sleeper: Sleeper,
    ) -> StationCatalogArtifacts: ...

    def register_hrrr_sources(
        self,
        request: Phase1Request,
        *,
        artifact_service: ArtifactService,
        clock: Clock,
        sleeper: Sleeper,
    ) -> tuple[HrrrLeadArtifacts, ...]: ...

    def register_metar_observations(
        self,
        request: Phase1Request,
        station_catalog: StationCatalogArtifacts,
        *,
        artifact_service: ArtifactService,
        clock: Clock,
        sleeper: Sleeper,
    ) -> ObservationArtifacts: ...


class Phase1TransformationPort(Protocol):
    """Artifact-backed pure transformations; formulas live behind this port."""

    def build_forecast(
        self,
        request: Phase1Request,
        run: RunManifest,
        station_catalog: StationCatalogArtifacts,
        hrrr: tuple[HrrrLeadArtifacts, ...],
        *,
        artifact_service: ArtifactService,
    ) -> ForecastArtifacts: ...

    def verify(
        self,
        request: Phase1Request,
        run: RunManifest,
        forecast: ForecastArtifacts,
        observations: ObservationArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> VerificationArtifacts: ...


class Phase1Coordinator:
    """Fail-closed lifecycle coordinator from plan Section 4.3."""

    def __init__(
        self,
        *,
        artifact_service: ArtifactService,
        sources: Phase1SourcePort,
        transformations: Phase1TransformationPort,
        clock: Clock,
        sleeper: Sleeper,
    ) -> None:
        self._artifact_service = artifact_service
        self._sources = sources
        self._transformations = transformations
        self._clock = clock
        self._sleeper = sleeper

    def run(self, request: Phase1Request) -> Phase1Result:
        station_catalog = self._sources.register_station_catalog(
            request,
            artifact_service=self._artifact_service,
            clock=self._clock,
            sleeper=self._sleeper,
        )
        _require_artifact(station_catalog.response, "aviationweather-station-response", True)
        _require_artifact(station_catalog.snapshot, "station-catalog-snapshot", False)
        _require_pre_run(station_catalog.response)
        _require_pre_run(station_catalog.snapshot)

        hrrr = self._sources.register_hrrr_sources(
            request,
            artifact_service=self._artifact_service,
            clock=self._clock,
            sleeper=self._sleeper,
        )
        _validate_hrrr_roots(hrrr)
        selected = tuple(
            artifact.artifact_id for lead in hrrr for artifact in (lead.index, lead.selected_grib)
        ) + (station_catalog.snapshot.artifact_id,)
        run = self._artifact_service.create_run(
            run_id=request.run_id,
            forecast_issue_time=request.forecast_issue_time,
            information_cutoff=request.information_cutoff,
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            lockfile_digest=request.lockfile_digest,
            random_seed=request.random_seed,
            selected_input_artifact_ids=selected,
            require_source_inputs=False,
        )

        forecast = self._transformations.build_forecast(
            request,
            run,
            station_catalog,
            hrrr,
            artifact_service=self._artifact_service,
        )
        _validate_forecast(forecast, request.run_id)
        observations = self._sources.register_metar_observations(
            request,
            station_catalog,
            artifact_service=self._artifact_service,
            clock=self._clock,
            sleeper=self._sleeper,
        )
        for response in observations.responses:
            _require_artifact(response, "aviationweather-metar-response", True)
        _require_artifact(observations.normalized, "normalized-metar-observations", False)
        verification = self._transformations.verify(
            request,
            run,
            forecast,
            observations,
            artifact_service=self._artifact_service,
        )
        _require_artifact(verification.matched_pairs, "matched-pairs", False)
        _require_artifact(verification.report, "verification-report", False)
        _require_run(verification.matched_pairs, request.run_id)
        _require_run(verification.report, request.run_id)
        return Phase1Result(
            run=run,
            station_catalog=station_catalog,
            hrrr=hrrr,
            forecast=forecast,
            observations=observations,
            verification=verification,
        )


def _require_artifact(manifest: ArtifactManifest, artifact_type: str, source: bool) -> None:
    if manifest.artifact_type != artifact_type:
        raise ValueError(f"role requires {artifact_type!r}, got {manifest.artifact_type!r}")
    if manifest.quality_state != "valid":
        raise ValueError(f"artifact {manifest.artifact_id} is not valid")
    if (manifest.source_registration_digest is not None) is not source:
        kind = "source" if source else "derived"
        raise ValueError(f"artifact {manifest.artifact_id} must be a {kind} artifact")


def _require_pre_run(manifest: ArtifactManifest) -> None:
    if manifest.run_id is not None:
        raise ValueError(f"pre-run artifact {manifest.artifact_id} must have run_id=None")


def _require_run(manifest: ArtifactManifest, run_id: RunId) -> None:
    if manifest.run_id != run_id:
        raise ValueError(f"artifact {manifest.artifact_id} is not registered to run {run_id}")


def _validate_hrrr_roots(hrrr: tuple[HrrrLeadArtifacts, ...]) -> None:
    if tuple(lead.lead_hours for lead in hrrr) != _LEADS:
        raise ValueError(f"HRRR roots must be in exact lead order {_LEADS!r}")
    ids: set[ArtifactId] = set()
    for lead in hrrr:
        _require_artifact(lead.index, "hrrr-grib-index", True)
        _require_artifact(lead.selected_grib, "hrrr-selected-grib", True)
        _require_pre_run(lead.index)
        _require_pre_run(lead.selected_grib)
        ids.update((lead.index.artifact_id, lead.selected_grib.artifact_id))
    if len(ids) != 14:
        raise ValueError("the fourteen HRRR roles must reference distinct artifacts")


def _validate_forecast(forecast: ForecastArtifacts, run_id: RunId) -> None:
    roles = (
        (forecast.acquisition_manifest, "hrrr-acquisition-manifest"),
        (forecast.variable_lineage, "variable-lineage-manifest"),
        (forecast.canonical_guidance, "canonical-guidance"),
        (forecast.extraction_report, "point-extraction-report"),
        (forecast.baseline, "baseline-forecast"),
    )
    for artifact, artifact_type in roles:
        _require_artifact(artifact, artifact_type, False)
        _require_run(artifact, run_id)
