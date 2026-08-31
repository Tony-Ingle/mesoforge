"""Production composition adapter for the Phase 2 application ports.

Exact-byte provider orchestration remains an injected subport until a complete
provider implementation can satisfy the approved contracts.  This adapter adds no
selectors or alternate science; it only supplies the production ``ArtifactService``
to the injected artifact-backed operations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.phase2 import (
    AlignmentArtifacts,
    AtomicForecastArtifacts,
    AvailabilityArtifacts,
    CorrectedForecastArtifacts,
    DiscoveryResult,
    MatchingArtifacts,
    NormalizedGuidanceArtifacts,
    ObservationArtifacts,
    Phase2Request,
    Phase2SelectedInputs,
    VerificationArtifacts,
)
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.runs import RunManifest


class Phase2ProviderSubport(Protocol):
    def discover(self, request: Phase2Request) -> DiscoveryResult: ...

    def acquire(
        self,
        request: Phase2Request,
        discovery: DiscoveryResult,
        *,
        artifact_service: ArtifactService,
    ) -> Phase2SelectedInputs: ...


class Phase2ScienceSubport(Protocol):
    def normalize(
        self,
        request: Phase2Request,
        run: RunManifest,
        inputs: Phase2SelectedInputs,
        *,
        artifact_service: ArtifactService,
    ) -> NormalizedGuidanceArtifacts: ...
    def align(
        self,
        request: Phase2Request,
        run: RunManifest,
        guidance: NormalizedGuidanceArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> AlignmentArtifacts: ...
    def evaluate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> AvailabilityArtifacts: ...
    def generate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        availability: AvailabilityArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> AtomicForecastArtifacts: ...
    def apply(
        self,
        request: Phase2Request,
        run: RunManifest,
        forecast: AtomicForecastArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> CorrectedForecastArtifacts: ...
    def acquire_and_normalize(
        self,
        request: Phase2Request,
        run: RunManifest,
        station_snapshot: ArtifactManifest,
        *,
        artifact_service: ArtifactService,
    ) -> ObservationArtifacts: ...
    def match(
        self,
        request: Phase2Request,
        run: RunManifest,
        baseline: ArtifactManifest,
        observations: ObservationArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> MatchingArtifacts: ...
    def calculate(
        self,
        request: Phase2Request,
        run: RunManifest,
        pairs: MatchingArtifacts,
        *,
        artifact_service: ArtifactService,
    ) -> VerificationArtifacts: ...


@dataclass(frozen=True, slots=True)
class Phase2ArtifactOperations:
    """Named production composition for the artifact-backed science stages.

    The callables have the same contracts as :class:`Phase2ScienceSubport` and
    receive the production ``ArtifactService`` explicitly.  Keeping the stages
    named prevents an acceptance harness from replacing the application workflow
    with a second coordinator while still allowing deterministic provider fixtures
    to supply bytes at the network boundary.
    """

    normalize: Callable[..., NormalizedGuidanceArtifacts]
    align: Callable[..., AlignmentArtifacts]
    evaluate: Callable[..., AvailabilityArtifacts]
    generate: Callable[..., AtomicForecastArtifacts]
    apply: Callable[..., CorrectedForecastArtifacts]
    acquire_and_normalize: Callable[..., ObservationArtifacts]
    match: Callable[..., MatchingArtifacts]
    calculate: Callable[..., VerificationArtifacts]


class Phase2ProductionAdapters:
    """Concrete application adapter backed only by approved injected subports."""

    def __init__(
        self,
        *,
        artifact_service: ArtifactService,
        providers: Phase2ProviderSubport,
        science: Phase2ScienceSubport | Phase2ArtifactOperations,
    ) -> None:
        self._artifacts = artifact_service
        self._providers = providers
        self._science = science

    def discover(self, request: Phase2Request) -> DiscoveryResult:
        return self._providers.discover(request)

    def acquire(self, request: Phase2Request, discovery: DiscoveryResult) -> Phase2SelectedInputs:
        return self._providers.acquire(request, discovery, artifact_service=self._artifacts)

    def normalize(
        self, request: Phase2Request, run: RunManifest, inputs: Phase2SelectedInputs
    ) -> NormalizedGuidanceArtifacts:
        return self._science.normalize(request, run, inputs, artifact_service=self._artifacts)

    def align(
        self, request: Phase2Request, run: RunManifest, guidance: NormalizedGuidanceArtifacts
    ) -> AlignmentArtifacts:
        return self._science.align(request, run, guidance, artifact_service=self._artifacts)

    def evaluate(
        self, request: Phase2Request, run: RunManifest, alignment: AlignmentArtifacts
    ) -> AvailabilityArtifacts:
        return self._science.evaluate(request, run, alignment, artifact_service=self._artifacts)

    def generate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        availability: AvailabilityArtifacts,
    ) -> AtomicForecastArtifacts:
        return self._science.generate(
            request, run, alignment, availability, artifact_service=self._artifacts
        )

    def apply(
        self, request: Phase2Request, run: RunManifest, forecast: AtomicForecastArtifacts
    ) -> CorrectedForecastArtifacts:
        return self._science.apply(request, run, forecast, artifact_service=self._artifacts)

    def acquire_and_normalize(
        self, request: Phase2Request, run: RunManifest, station_snapshot: ArtifactManifest
    ) -> ObservationArtifacts:
        return self._science.acquire_and_normalize(
            request, run, station_snapshot, artifact_service=self._artifacts
        )

    def match(
        self,
        request: Phase2Request,
        run: RunManifest,
        baseline: ArtifactManifest,
        observations: ObservationArtifacts,
    ) -> MatchingArtifacts:
        return self._science.match(
            request, run, baseline, observations, artifact_service=self._artifacts
        )

    def calculate(
        self, request: Phase2Request, run: RunManifest, pairs: MatchingArtifacts
    ) -> VerificationArtifacts:
        return self._science.calculate(request, run, pairs, artifact_service=self._artifacts)
