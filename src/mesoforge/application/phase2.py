"""Fail-closed orchestration boundary for the Phase 2 baseline workflow.

All provider policy and scientific work is performed behind injected ports.  This
module owns ordering, immutable input pinning, and artifact/lifecycle invariants.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.phase2_replay import (
    Phase2PersistedRun,
    Phase2ReplayIdentityError,
    parse_run_spec,
    parse_station_snapshot,
    require_run_spec_artifact,
    require_station_snapshot_artifact,
)
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    RunId,
    validate_code_revision,
)
from mesoforge.common.time import UtcInstant
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.provenance import ActivityManifest
from mesoforge.contracts.runs import RunManifest

_HORIZONS = tuple(range(1, 37))
_MODEL_ORDER = {"HRRR": 0, "NBM": 1, "GFS": 2}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Phase2Request(_FrozenModel):
    run_id: RunId
    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    lockfile_digest: Digest
    target_reference_time: UtcInstant
    forecast_issue_time: UtcInstant
    information_cutoff: UtcInstant
    verification_cutoff: UtcInstant
    target_horizons: tuple[int, ...] = _HORIZONS
    random_seed: int = 0

    @field_validator("code_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_code_revision(value)

    @model_validator(mode="after")
    def _frame(self) -> Phase2Request:
        target = self.target_reference_time
        offset = target.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("target_reference_time must be UTC")
        if any((target.minute, target.second, target.microsecond)):
            raise ValueError("target_reference_time must be a whole UTC hour")
        if self.target_horizons != _HORIZONS:
            raise ValueError("target_horizons must be exactly 1..36")
        if self.forecast_issue_time < target:
            raise ValueError("forecast_issue_time must not precede target_reference_time")
        if self.information_cutoff > self.forecast_issue_time:
            raise ValueError("information_cutoff must not exceed forecast_issue_time")
        if self.verification_cutoff < self.forecast_issue_time:
            raise ValueError("verification_cutoff must not precede forecast_issue_time")
        return self


class DiscoveryResult(_FrozenModel):
    """Opaque deterministic provider selection, interpreted only by acquisition."""

    selection_digest: Digest


class SelectedSourceRoot(_FrozenModel):
    model: Literal["HRRR", "NBM", "GFS"]
    source_lead_hours: int
    field_role: str
    artifact: ArtifactManifest

    @field_validator("source_lead_hours")
    @classmethod
    def _positive_lead(cls, value: int) -> int:
        if value < 0:
            raise ValueError("source_lead_hours must be nonnegative")
        return value

    @field_validator("field_role")
    @classmethod
    def _role(cls, value: str) -> str:
        if not value:
            raise ValueError("field_role must not be empty")
        return value


class Phase2SelectedInputs(_FrozenModel):
    run_spec: ArtifactManifest
    source_roots: tuple[SelectedSourceRoot, ...]
    station_snapshot: ArtifactManifest


class NormalizedGuidanceArtifacts(_FrozenModel):
    artifacts: tuple[ArtifactManifest, ...]

    @model_validator(mode="after")
    def _nonempty(self) -> NormalizedGuidanceArtifacts:
        if not self.artifacts:
            raise ValueError("normalized guidance must not be empty")
        return self


class AlignmentArtifacts(_FrozenModel):
    aligned_guidance: ArtifactManifest


class AvailabilityArtifacts(_FrozenModel):
    cycle_selection: ArtifactManifest
    report: ArtifactManifest


class AtomicForecastArtifacts(_FrozenModel):
    activity: ActivityManifest
    uncorrected_blend: ArtifactManifest
    contribution_manifest: ArtifactManifest


class CorrectedForecastArtifacts(_FrozenModel):
    correction: ArtifactManifest
    baseline: ArtifactManifest


class ObservationArtifacts(_FrozenModel):
    responses: tuple[ArtifactManifest, ...]
    normalized: ArtifactManifest

    @model_validator(mode="after")
    def _responses(self) -> ObservationArtifacts:
        if not self.responses:
            raise ValueError("at least one observation response is required")
        return self


class MatchingArtifacts(_FrozenModel):
    matched_pairs: ArtifactManifest


class VerificationArtifacts(_FrozenModel):
    report: ArtifactManifest


class Phase2Result(_FrozenModel):
    run: RunManifest
    selected_inputs: Phase2SelectedInputs
    normalized: NormalizedGuidanceArtifacts
    alignment: AlignmentArtifacts
    availability: AvailabilityArtifacts
    atomic_forecast: AtomicForecastArtifacts
    corrected_forecast: CorrectedForecastArtifacts
    observations: ObservationArtifacts
    matching: MatchingArtifacts
    verification: VerificationArtifacts


class SourceDiscoveryPort(Protocol):
    def discover(self, request: Phase2Request) -> DiscoveryResult: ...


class SourceAcquisitionPort(Protocol):
    def acquire(
        self, request: Phase2Request, discovery: DiscoveryResult
    ) -> Phase2SelectedInputs: ...


class GuidanceNormalizationPort(Protocol):
    def normalize(
        self,
        request: Phase2Request,
        run: RunManifest,
        inputs: Phase2SelectedInputs,
        *,
        persisted: Phase2PersistedRun,
    ) -> NormalizedGuidanceArtifacts: ...


class StationAlignmentPort(Protocol):
    def align(
        self,
        request: Phase2Request,
        run: RunManifest,
        guidance: NormalizedGuidanceArtifacts,
        *,
        persisted: Phase2PersistedRun,
    ) -> AlignmentArtifacts: ...


class AvailabilityPort(Protocol):
    def evaluate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        *,
        persisted: Phase2PersistedRun,
    ) -> AvailabilityArtifacts: ...


class AtomicForecastPort(Protocol):
    def generate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        availability: AvailabilityArtifacts,
        *,
        persisted: Phase2PersistedRun,
    ) -> AtomicForecastArtifacts: ...


class IdentityCorrectionPort(Protocol):
    def apply(
        self,
        request: Phase2Request,
        run: RunManifest,
        forecast: AtomicForecastArtifacts,
        *,
        persisted: Phase2PersistedRun,
    ) -> CorrectedForecastArtifacts: ...


class ObservationPort(Protocol):
    def acquire_and_normalize(
        self,
        request: Phase2Request,
        run: RunManifest,
        station_snapshot: ArtifactManifest,
        *,
        persisted: Phase2PersistedRun,
        recorded_responses: tuple[ArtifactManifest, ...] | None,
    ) -> ObservationArtifacts: ...


class ObservationMatchingPort(Protocol):
    def match(
        self,
        request: Phase2Request,
        run: RunManifest,
        baseline: ArtifactManifest,
        observations: ObservationArtifacts,
        *,
        persisted: Phase2PersistedRun,
    ) -> MatchingArtifacts: ...


class VerificationPort(Protocol):
    def calculate(
        self,
        request: Phase2Request,
        run: RunManifest,
        pairs: MatchingArtifacts,
        *,
        persisted: Phase2PersistedRun,
    ) -> VerificationArtifacts: ...


class Phase2Coordinator:
    def __init__(
        self,
        *,
        artifact_service: ArtifactService,
        discovery: SourceDiscoveryPort,
        acquisition: SourceAcquisitionPort,
        normalization: GuidanceNormalizationPort,
        alignment: StationAlignmentPort,
        availability: AvailabilityPort,
        forecast: AtomicForecastPort,
        correction: IdentityCorrectionPort,
        observations: ObservationPort,
        matching: ObservationMatchingPort,
        verification: VerificationPort,
    ) -> None:
        self._artifacts = artifact_service
        self._discovery = discovery
        self._acquisition = acquisition
        self._normalization = normalization
        self._alignment = alignment
        self._availability = availability
        self._forecast = forecast
        self._correction = correction
        self._observations = observations
        self._matching = matching
        self._verification = verification

    def run(self, request: Phase2Request) -> Phase2Result:
        discovery = self._discovery.discover(request)
        inputs = self._acquisition.acquire(request, discovery)
        persisted, inputs = self._load_persisted_run(request, inputs)
        return self._run_pinned(request, inputs, persisted, recorded_observations=None)

    def replay(
        self,
        request: Phase2Request,
        pinned_inputs: Phase2SelectedInputs,
        recorded_observations: tuple[ArtifactManifest, ...],
    ) -> Phase2Result:
        """Replay a run exclusively from immutable persisted state.

        Discovery and acquisition are never invoked, and neither the
        caller's ``request`` nor the caller's pinned manifests are
        trusted: the run spec and station snapshot are re-read from the
        repository by artifact ID, their bytes are integrity-verified and
        parsed under strict contracts, and every caller-supplied value is
        then *checked against* that persisted identity (Codex review
        `t_652b155e`). ``recorded_observations`` are the run's own
        registered AviationWeather response artifacts, reused verbatim so
        replay performs no observation network access at all.

        Any conflict -- a different request frame, a different
        configuration identity, a source root the run never acquired, or
        an observation artifact belonging to another run -- raises
        ``Phase2ReplayIdentityError`` before a single stage executes.
        """
        persisted, pinned_inputs = self._load_persisted_run(request, pinned_inputs)
        authoritative_observations = tuple(
            self._artifacts.load_verified_payload(item.artifact_id)[0]
            for item in recorded_observations
        )
        persisted.require_recorded_observations(authoritative_observations)
        return self._run_pinned(
            request,
            pinned_inputs,
            persisted,
            recorded_observations=authoritative_observations,
        )

    def _load_persisted_run(
        self, request: Phase2Request, inputs: Phase2SelectedInputs
    ) -> tuple[Phase2PersistedRun, Phase2SelectedInputs]:
        """Rebuild the run's immutable identity from persisted bytes and
        fail closed on any conflict with the caller's request/roots.

        Both ``run`` and ``replay`` go through this, so a run whose
        persisted spec is incomplete is caught at write time rather than
        only becoming un-replayable later.
        """
        run_spec_manifest, run_spec_payload = self._artifacts.load_verified_payload(
            inputs.run_spec.artifact_id
        )
        require_run_spec_artifact(run_spec_manifest)
        run_spec = parse_run_spec(run_spec_payload)
        if inputs.station_snapshot.artifact_id != run_spec.station_snapshot.artifact_id:
            raise Phase2ReplayIdentityError("the pinned station snapshot is not the run-spec root")
        station_manifest, station_payload = self._artifacts.load_verified_payload(
            run_spec.station_snapshot.artifact_id
        )
        require_station_snapshot_artifact(station_manifest)
        if station_manifest.content_digest != run_spec.station_snapshot.content_digest:
            raise Phase2ReplayIdentityError(
                "the authoritative station snapshot does not match the run-spec digest"
            )
        persisted = Phase2PersistedRun(
            run_spec=run_spec,
            station_snapshot=parse_station_snapshot(station_payload),
            run_spec_artifact=run_spec_manifest,
            station_snapshot_artifact=station_manifest,
        )
        persisted.require_request_identity(
            run_id=request.run_id,
            target_reference_time=request.target_reference_time,
            forecast_issue_time=request.forecast_issue_time,
            information_cutoff=request.information_cutoff,
            verification_cutoff=request.verification_cutoff,
            target_horizons=request.target_horizons,
            random_seed=request.random_seed,
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            lockfile_digest=request.lockfile_digest,
        )
        supplied_roots = tuple(
            (root.model, root.source_lead_hours, root.field_role, root.artifact.artifact_id)
            for root in inputs.source_roots
        )
        persisted.require_source_roots(supplied_roots)
        authoritative_roots: list[SelectedSourceRoot] = []
        for model, lead, role, identity in persisted.expected_source_roots():
            manifest, _payload = self._artifacts.load_verified_payload(identity.artifact_id)
            if manifest.content_digest != identity.content_digest:
                raise Phase2ReplayIdentityError(
                    f"authoritative source artifact {identity.artifact_id!r} conflicts with "
                    "the run-spec digest"
                )
            authoritative_roots.append(
                SelectedSourceRoot(
                    model=model,  # type: ignore[arg-type]
                    source_lead_hours=lead,
                    field_role=role,
                    artifact=manifest,
                )
            )
        authoritative = Phase2SelectedInputs(
            run_spec=run_spec_manifest,
            source_roots=tuple(authoritative_roots),
            station_snapshot=station_manifest,
        )
        # Validate only repository-authoritative manifests before create_run.
        _validate_selected_inputs(authoritative, request)
        return persisted, authoritative

    def _run_pinned(
        self,
        request: Phase2Request,
        inputs: Phase2SelectedInputs,
        persisted: Phase2PersistedRun,
        *,
        recorded_observations: tuple[ArtifactManifest, ...] | None,
    ) -> Phase2Result:
        selected_ids = _validate_selected_inputs(inputs, request)
        run = self._artifacts.create_run(
            run_id=request.run_id,
            forecast_issue_time=request.forecast_issue_time,
            information_cutoff=request.information_cutoff,
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            lockfile_digest=request.lockfile_digest,
            random_seed=request.random_seed,
            selected_input_artifact_ids=selected_ids,
            require_source_inputs=False,
        )
        if run.run_id != request.run_id or run.selected_input_artifact_ids != selected_ids:
            raise ValueError("created run does not preserve the exact pinned input sequence")
        normalized = self._normalization.normalize(request, run, inputs, persisted=persisted)
        _validate_many(normalized.artifacts, request.run_id, "canonical-guidance")
        alignment = self._alignment.align(request, run, normalized, persisted=persisted)
        _derived(alignment.aligned_guidance, "aligned-station-guidance", request.run_id)
        availability = self._availability.evaluate(request, run, alignment, persisted=persisted)
        _derived(availability.cycle_selection, "model-cycle-selection", request.run_id)
        _derived(availability.report, "model-availability-report", request.run_id)
        forecast = self._forecast.generate(
            request, run, alignment, availability, persisted=persisted
        )
        _validate_atomic_forecast(forecast, request.run_id)
        corrected = self._correction.apply(request, run, forecast, persisted=persisted)
        _derived(corrected.correction, "identity-correction", request.run_id)
        _derived(corrected.baseline, "baseline-forecast", request.run_id)
        observations = self._observations.acquire_and_normalize(
            request,
            run,
            inputs.station_snapshot,
            persisted=persisted,
            recorded_responses=recorded_observations,
        )
        for response in observations.responses:
            _source(response, "aviationweather-metar-response")
        if recorded_observations is not None and tuple(
            item.artifact_id for item in observations.responses
        ) != tuple(item.artifact_id for item in recorded_observations):
            raise Phase2ReplayIdentityError(
                "replay observations must reuse exactly the recorded response artifacts"
            )
        _derived(observations.normalized, "normalized-metar-observations", request.run_id)
        matching = self._matching.match(
            request, run, corrected.baseline, observations, persisted=persisted
        )
        _derived(matching.matched_pairs, "matched-pairs", request.run_id)
        verification = self._verification.calculate(request, run, matching, persisted=persisted)
        _derived(verification.report, "verification-report", request.run_id)
        return Phase2Result(
            run=run,
            selected_inputs=inputs,
            normalized=normalized,
            alignment=alignment,
            availability=availability,
            atomic_forecast=forecast,
            corrected_forecast=corrected,
            observations=observations,
            matching=matching,
            verification=verification,
        )


def _validate_selected_inputs(
    inputs: Phase2SelectedInputs, request: Phase2Request
) -> tuple[ArtifactId, ...]:
    _artifact(inputs.run_spec, "phase2-run-spec")
    if inputs.run_spec.artifact_schema_version != "phase2-run-spec.v1":
        raise ValueError("run spec must use exactly phase2-run-spec.v1")
    _artifact(inputs.station_snapshot, "station-catalog-snapshot")
    expected = tuple(
        sorted(
            inputs.source_roots,
            key=lambda root: (_MODEL_ORDER[root.model], root.source_lead_hours, root.field_role),
        )
    )
    if inputs.source_roots != expected:
        raise ValueError("source roots must be ordered by HRRR/NBM/GFS, source lead, field role")
    manifests = (
        (inputs.run_spec,)
        + tuple(root.artifact for root in inputs.source_roots)
        + (inputs.station_snapshot,)
    )
    for root in inputs.source_roots:
        if not root.artifact.artifact_type.startswith(f"{root.model.lower()}-"):
            raise ValueError(f"{root.model} root has a mismatched artifact type")
        if (
            root.artifact.source_registration_digest is None
            or root.artifact.quality_state != "valid"
        ):
            raise ValueError(f"{root.model} root must be a valid source artifact")
    if any(item.run_id is not None for item in manifests):
        raise ValueError("selected inputs must exist before run creation")
    for item in manifests:
        if (
            item.configuration_snapshot_id != request.configuration_snapshot_id
            or item.configuration_digest != request.configuration_digest
            or item.code_revision != request.code_revision
            or item.environment_digest != request.environment_digest
        ):
            raise ValueError("selected inputs must preserve the request execution identity")
    if any(item.availability.available_at > request.information_cutoff for item in manifests):
        raise ValueError("selected inputs must be available by information_cutoff")
    ids = tuple(item.artifact_id for item in manifests)
    if len(ids) != len(set(ids)):
        raise ValueError("selected input roles must reference distinct artifacts")
    return ids


def _artifact(manifest: ArtifactManifest, artifact_type: str) -> None:
    if manifest.artifact_type != artifact_type or manifest.quality_state != "valid":
        raise ValueError(f"role requires valid {artifact_type!r}")


def _source(manifest: ArtifactManifest, artifact_type: str) -> None:
    _artifact(manifest, artifact_type)
    if manifest.source_registration_digest is None:
        raise ValueError(f"{artifact_type!r} must be a source artifact")


def _derived(manifest: ArtifactManifest, artifact_type: str, run_id: RunId) -> None:
    _artifact(manifest, artifact_type)
    if manifest.source_registration_digest is not None or manifest.run_id != run_id:
        raise ValueError(f"{artifact_type!r} must be derived by run {run_id}")


def _validate_many(
    manifests: tuple[ArtifactManifest, ...], run_id: RunId, artifact_type: str
) -> None:
    if not manifests:
        raise ValueError(f"{artifact_type!r} artifacts must not be empty")
    for manifest in manifests:
        _derived(manifest, artifact_type, run_id)


def _validate_atomic_forecast(value: AtomicForecastArtifacts, run_id: RunId) -> None:
    _derived(value.uncorrected_blend, "uncorrected-blend-forecast", run_id)
    _derived(value.contribution_manifest, "blend-contribution-manifest", run_id)
    activity = value.activity
    if activity.status != "succeeded" or activity.run_id != run_id:
        raise ValueError("atomic blend pair requires one succeeded run activity")
    output_ids = {ref.artifact_id for ref in activity.outputs}
    expected = {value.uncorrected_blend.artifact_id, value.contribution_manifest.artifact_id}
    if output_ids != expected:
        raise ValueError("atomic blend activity must expose exactly both forecast outputs")
    if not isinstance(activity.activity_id, ActivityId):
        raise TypeError("atomic blend activity identity must be typed")
