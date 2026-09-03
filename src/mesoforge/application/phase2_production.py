"""Production Phase2ProviderSubport/Phase2ScienceSubport implementation
(plan Sections 1-6; Codex review finding 1, CRITICAL).

Composes the pure guidance/observations/forecasting/alignment/
verification domain functions plus the real HRRR/NBM/GFS byte-range
acquisition (``guidance.acquisition_v2``) and production per-cycle
decoding (``guidance.normalization_v2``) with ``ArtifactService``
registration/transformation. This is the concrete, non-test-local
implementation of both Phase 2 application ports: production wiring
and the acceptance proof both use these classes, injecting a real
``RequestsHrrrHttpTransport``-equivalent in production or a
deterministic scripted fixture transport in tests -- never a
parallel test-only port implementation with its own science stages.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pyproj
import xarray as xr

from mesoforge.alignment.station_frame import align_station_to_model
from mesoforge.application.artifacts import (
    ArtifactService,
    InputBinding,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
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
    SelectedSourceRoot,
    VerificationArtifacts,
)
from mesoforge.application.phase2_adapters import (
    Phase2ArtifactOperations,
    Phase2ProductionAdapters,
)
from mesoforge.application.phase2_replay import (
    PersistedLeadAcquisition,
    PersistedModelGroup,
    Phase2PersistedRun,
    Phase2ReplayIdentityError,
)
from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.catalog.domains import BoundingBox
from mesoforge.common.identifiers import ArtifactId, Digest, GridId, RunId, StationId
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.forecasts import validate_baseline_forecast_v2
from mesoforge.contracts.lineage_v2 import VariableLineageManifestV2
from mesoforge.contracts.observations_v2 import NormalizedObservationV2, RawMetarRecordV2
from mesoforge.contracts.runs import RunManifest
from mesoforge.contracts.verification import MatchedPairRowV2, VerificationReportV2
from mesoforge.forecasting.availability import (
    evaluate_pop_availability,
    evaluate_run_state,
    evaluate_scalar_vector_availability,
)
from mesoforge.forecasting.baseline_v2 import assemble_baseline_forecast_v2
from mesoforge.forecasting.contributions import (
    BlendContributionManifest,
    BlendContributionRow,
    ContributorRecord,
    ExcludedContributorRecord,
    IdentityBiasCorrection,
)
from mesoforge.forecasting.gust_blend import (
    SOURCE_GUST_SHORTFALL_FLOOR_TOLERANCE_M_S,
    GustDisqualificationError,
    blend_gust,
    validate_source_gust,
)
from mesoforge.forecasting.pop_blend import pop_passthrough
from mesoforge.forecasting.precipitation_blend import blend_qpf
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar
from mesoforge.forecasting.vector_blend import blend_vector
from mesoforge.guidance.acquisition_v2 import (
    Phase2LeadAcquisition,
    SelectedMessage,
    acquire_gfs_lead,
    acquire_hrrr_phase2_lead,
    acquire_nbm_lead,
)
from mesoforge.guidance.canonical_v2 import (
    SUBSET_POLICY_ID,
    validate_canonical_guidance_lineage_v2,
    validate_canonical_guidance_v2,
)
from mesoforge.guidance.cycle_selection import (
    CandidateCycle,
    generate_candidate_reference_times,
    select_model_cycle,
)
from mesoforge.guidance.interfaces import Clock, HttpResponse, HttpTransport, Sleeper
from mesoforge.guidance.normalization_v2 import (
    normalize_gfs_cycle,
    normalize_hrrr_phase2_cycle,
    normalize_nbm_cycle,
)
from mesoforge.guidance.precipitation import is_bucket_reset_hour
from mesoforge.guidance.sources import gfs as gfs_source
from mesoforge.guidance.sources import hrrr_phase2 as hrrr_phase2_source
from mesoforge.guidance.sources import nbm as nbm_source
from mesoforge.observations.acquisition import RequestRateLimiter, acquire_metar_batch
from mesoforge.observations.normalization_v2 import normalize_metar_record_v2
from mesoforge.observations.sources.aviationweather import parse_raw_metar_response
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.verification.matching import match_baseline_to_observations_v2
from mesoforge.verification.metrics import compute_verification_report_v2
from mesoforge.verification.validation import (
    validate_matched_pairs_v2,
    validate_verification_report_v2,
)

_MODELS = ("HRRR", "NBM", "GFS")
_STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
_HORIZONS = tuple(range(1, 37))
_VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "liquid_equivalent_precipitation_amount_1h",
    "probability_of_precipitation_1h",
)

_JSON = CanonicalJsonSerializer()
_NETCDF = H5NetcdfDatasetSerializer()

# Per-model selector builders, used to record the exact inventory
# selector each variable/lead was chosen with in variable-lineage.v2
# (Codex re-review finding 3).
_SELECTOR_BUILDERS = {
    "HRRR": hrrr_phase2_source.build_field_selector,
    "NBM": nbm_source.build_field_selector,
    "GFS": gfs_source.build_field_selector,
}

# The exact conversion applied to each canonical variable between the
# decoded GRIB value and the canonical value. "identity" means the
# provider unit already is the canonical unit.
_UNIT_CONVERSIONS: dict[str, str] = {
    "air_temperature_2m": "identity:K",
    "dew_point_temperature_2m": "identity:K",
    "eastward_wind_10m": "identity:m/s",
    "northward_wind_10m": "identity:m/s",
    "wind_speed_10m": "identity:m/s",
    "wind_from_direction_10m": "identity:degree",
    "wind_gust_10m": "identity:m/s",
    "liquid_equivalent_precipitation_amount_1h": "identity:kg/m^2",
    "probability_of_precipitation_1h": "percent-to-fraction:divide-by-100",
}

_WIND_VARIABLES = frozenset(
    {
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_speed_10m",
        "wind_from_direction_10m",
    }
)

# The atomic scientific unit a source gust inconsistency invalidates.
# Gust is only meaningful against its own sustained wind, and the gust
# operator's convexity invariant is stated against the *blended* wind,
# so dropping gust alone at a point would leave a blended gust that no
# longer bounds the blended wind it was checked against. U, V and gust
# are therefore rejected together, and nothing else is (Codex review
# t_1564b30c). Order is stable so the persisted provenance is
# deterministic.
_COUPLED_WIND_GUST_VARIABLES = (
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
)


def _wind_rotation_policy(model: str, variable_id: str) -> str | None:
    """The rotation policy actually applied to a wind component.

    HRRR/GFS decode U/V and rotate grid-relative components to
    earth-relative with pyproj; NBM decodes speed/direction and converts
    to earth-relative U/V cornerwise. Non-wind variables have no
    rotation policy at all.
    """
    if variable_id not in _WIND_VARIABLES:
        return None
    if model == "NBM":
        return "speed-direction-to-uv.v1"
    return "grid-to-earth-pyproj.v1"


def _source_grid_profile_id(settings: Any, model: str) -> str:
    """The registered source-grid profile identity for one model.

    NBM pins an explicit approved ``grid_profile`` (Codex re-review
    finding 2); HRRR and GFS pin their native product profiles through
    their own settings identities.
    """
    grid_profile = getattr(settings, "grid_profile", None)
    if grid_profile is not None:
        return str(grid_profile.profile_id)
    product_profile = getattr(settings, "product_profile", None)
    if product_profile is not None:
        return str(product_profile)
    return f"{model.lower()}-{settings.product}-{settings.sector}.v1"


logger = logging.getLogger(__name__)


def _required_source_leads(
    *,
    target_reference_time: datetime,
    candidate_reference_time: datetime,
    max_source_lead_hours: int,
    include_previous_apcp_dependency: bool,
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """Return (target leads, acquisition leads) for one candidate cycle."""
    age_seconds = (target_reference_time - candidate_reference_time).total_seconds()
    if age_seconds < 0 or age_seconds % 3600 != 0:
        return None
    age_hours = int(age_seconds // 3600)
    target_leads = tuple(horizon + age_hours for horizon in _HORIZONS)
    if any(lead < 1 or lead > max_source_lead_hours for lead in target_leads):
        return None
    acquisition_leads = target_leads
    if include_previous_apcp_dependency and not is_bucket_reset_hour(target_leads[0]):
        acquisition_leads = (target_leads[0] - 1, *target_leads)
    return target_leads, acquisition_leads


def _group_normalization_payloads(
    refs: tuple[tuple[str, ArtifactId], ...], raw: tuple[bytes, ...]
) -> dict[str, dict[int, bytes | tuple[bytes, ...]]]:
    """Preserve plural selected records as ordered tuples per field/lead."""
    grouped: dict[str, dict[int, list[bytes]]] = {}
    for (role, _artifact), payload in zip(refs, raw, strict=True):
        lead_text, variable_id, *_record_identity = role.split(":")
        grouped.setdefault(variable_id, {}).setdefault(int(lead_text), []).append(payload)
    return {
        variable_id: {
            lead: payloads[0] if len(payloads) == 1 else tuple(payloads)
            for lead, payloads in by_lead.items()
        }
        for variable_id, by_lead in grouped.items()
    }


def _record_identity(message: SelectedMessage, ordinal: int) -> str:
    """The stable per-message identity fragment shared by the registered
    artifact's ``field_role`` and the persisted run spec.

    Defining it once means acquisition and replay can never drift into
    two different spellings of the same message.
    """
    return f"m{message.row.message_number}-o{message.row.byte_offset}-n{ordinal}"


def _valid_map(value: object) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"expected dict, got {type(value)!r}")


def _late_acquisitions(
    acquisitions: tuple[Phase2LeadAcquisition, ...], information_cutoff: datetime
) -> tuple[datetime, ...]:
    """Every *authoritative provider availability* timestamp in
    ``acquisitions`` that is strictly after ``information_cutoff``
    (Codex re-review finding 4).

    Both the index and the ranged message object count: a forecast may
    only use input bytes that actually existed by the cutoff, and an
    index that only appeared afterwards is just as much a leak of
    future information as a late message.

    The comparison uses ``*_available_at`` -- the provider's own
    ``Last-Modified`` publication assertion -- not ``*_completed_at``,
    which is merely when this process happened to retrieve the bytes.
    Using retrieval time here would reject an already-published
    retrospective cycle purely because the run started later, which is
    not a leak of future information at all.
    """
    late: list[datetime] = []
    for acquisition in acquisitions:
        for available_at in (acquisition.index_available_at, acquisition.grib_available_at):
            if available_at > information_cutoff:
                late.append(available_at)
    return tuple(late)


def _as_json_map(value: object) -> dict[str, Any]:
    """Narrow a transformation output to the canonical-JSON mapping type."""
    _valid_map(value)
    return value  # type: ignore[return-value]


def _validate_pydantic_model(model: type, value: object) -> None:
    # Transformation values have crossed the JSON artifact boundary, where
    # tuples are represented as arrays. Validate through the JSON entrypoint
    # so strict immutable contract fields are reconstructed consistently.
    model.model_validate_json(_JSON.serialize(_as_json_map(value)))  # type: ignore[attr-defined]


def _validate_normalized_observations_v2(value: dict[str, list[object]]) -> None:
    for row in value["rows"]:
        NormalizedObservationV2.model_validate_json(_JSON.serialize(_as_json_map(row)))


def _validate_variable_lineage_v2(value: object) -> None:
    """Validate a serialized ``variable-lineage.v2`` manifest through the
    JSON entrypoint, so tuples that crossed the artifact boundary as
    arrays are reconstructed under the strict contract."""
    VariableLineageManifestV2.model_validate_json(_JSON.serialize(_as_json_map(value)))


class Phase2NoAvailableCycleError(Exception):
    """Raised when discovery cannot find a single approved cycle for
    any of HRRR/NBM/GFS -- Phase 2's ``normalized guidance must not be
    empty`` invariant is enforced by the coordinator itself, so this
    is only raised for a genuinely unrecoverable discovery failure."""


class GuidanceNormalizationProductionError(Exception):
    """Raised when the production science stage cannot normalize an
    acquired guidance cycle, e.g. a required adjacent-lead payload for
    same-bucket precipitation differencing was not acquired."""


def _canonical_validator(lineage_artifact: ArtifactManifest, *, payload: dict[str, Any]) -> Any:
    """Return the canonical-guidance validator bound to this model's own
    lineage manifest (Codex re-review finding 3).

    ``validate_canonical_guidance_v2`` alone can only check that the
    declared ``variable_lineage_manifest_id`` has the right *shape*,
    because it has no artifact repository. Binding the manifest that was
    actually created here closes the content half of the contract in
    production, not merely in tests: a canonical artifact can no longer
    validate while pointing at a raw message, a QPF-only lineage record,
    or a manifest describing a different model/grid/cycle/lead set.
    """
    manifest = VariableLineageManifestV2.model_validate_json(_JSON.serialize(payload))

    def _validate(dataset: xr.Dataset) -> None:
        validate_canonical_guidance_v2(dataset)
        validate_canonical_guidance_lineage_v2(
            dataset,
            lineage_manifest=manifest,
            lineage_artifact_id=str(lineage_artifact.artifact_id),
            lineage_artifact_type=lineage_artifact.artifact_type,
            lineage_schema_version=lineage_artifact.artifact_schema_version,
        )

    return _validate


class Phase2ProductionProvider:
    """Concrete ``Phase2ProviderSubport``: real deterministic cycle
    discovery (attempted-acquisition based, newest candidate first)
    and real HRRR/NBM/GFS byte-range acquisition."""

    def __init__(
        self,
        *,
        configuration: Phase2Configuration,
        hrrr_transport: HttpTransport,
        nbm_transport: HttpTransport,
        gfs_transport: HttpTransport,
        clock: Clock,
        sleeper: Sleeper,
    ) -> None:
        self._configuration = configuration
        self._hrrr_transport = hrrr_transport
        self._nbm_transport = nbm_transport
        self._gfs_transport = gfs_transport
        self._clock = clock
        self._sleeper = sleeper
        self._acquisitions_by_run: dict[str, dict[str, tuple[Phase2LeadAcquisition, ...]]] = {}

    def discover(self, request: Phase2Request) -> DiscoveryResult:
        """Attempt-based discovery: for each model, try candidate
        cycles newest-first; the first candidate whose full required
        lead set acquires successfully and satisfies the model's own
        completion-deadline/max-age policy (checked via
        ``guidance.cycle_selection.select_model_cycle``) is selected.
        A model with no viable candidate is simply absent from the
        run (a partial cycle is never spliced)."""
        selected = self._discover_and_acquire(request)
        self._acquisitions_by_run[str(request.run_id)] = {
            model: acquisitions for model, acquisitions in selected.items()
        }
        payload = {
            "schema_version": "phase2-discovery.v1",
            "available": sorted(selected),
        }
        return DiscoveryResult(selection_digest=Digest.of_bytes(_JSON.serialize(payload)))

    def _discover_and_acquire(
        self, request: Phase2Request
    ) -> dict[str, tuple[Phase2LeadAcquisition, ...]]:
        target = request.target_reference_time
        cutoff = request.information_cutoff
        selected: dict[str, tuple[Phase2LeadAcquisition, ...]] = {}

        # HRRR
        hrrr_settings = self._configuration.hrrr
        hrrr_candidates = generate_candidate_reference_times(
            target_reference_time=target,
            cadence="fixed",
            allowed_hours=hrrr_settings.allowed_cycle_hours,
        )
        hrrr_result = self._try_candidates(
            model="hrrr",
            candidates=hrrr_candidates,
            target=target,
            information_cutoff=cutoff,
            max_age_hours=hrrr_settings.max_age_hours,
            completion_deadline_minutes=hrrr_settings.cycle_completion_deadline_minutes,
            max_source_lead_hours=hrrr_settings.max_source_lead_hours,
            include_previous_apcp_dependency=False,
            acquire_lead=lambda cycle, lead, deadline: acquire_hrrr_phase2_lead(
                hrrr_settings,
                transport=self._hrrr_transport,
                clock=self._clock,
                sleeper=self._sleeper,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
                cycle_deadline=deadline,
            ),
        )
        if hrrr_result is not None:
            selected["HRRR"] = hrrr_result

        # NBM
        nbm_settings = self._configuration.nbm
        nbm_candidates = generate_candidate_reference_times(
            target_reference_time=target, cadence="hourly"
        )
        nbm_result = self._try_candidates(
            model="nbm",
            candidates=nbm_candidates,
            target=target,
            information_cutoff=cutoff,
            max_age_hours=nbm_settings.max_age_hours,
            completion_deadline_minutes=nbm_settings.cycle_completion_deadline_minutes,
            # NBM may be selected up to three hours older than the target
            # reference time, so target horizons 1..36 require source leads
            # through 39.  Limiting this to 36 would silently make every
            # non-current otherwise-valid NBM cycle ineligible.
            max_source_lead_hours=39,
            include_previous_apcp_dependency=False,
            acquire_lead=lambda cycle, lead, deadline: acquire_nbm_lead(
                nbm_settings,
                transport=self._nbm_transport,
                clock=self._clock,
                sleeper=self._sleeper,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
                cycle_deadline=deadline,
            ),
        )
        if nbm_result is not None:
            selected["NBM"] = nbm_result

        # GFS
        gfs_settings = self._configuration.gfs
        gfs_candidates = generate_candidate_reference_times(
            target_reference_time=target,
            cadence="fixed",
            allowed_hours=gfs_settings.allowed_cycle_hours,
        )
        gfs_result = self._try_candidates(
            model="gfs",
            candidates=gfs_candidates,
            target=target,
            information_cutoff=cutoff,
            max_age_hours=gfs_settings.max_age_hours,
            completion_deadline_minutes=gfs_settings.cycle_completion_deadline_minutes,
            max_source_lead_hours=gfs_settings.max_source_lead_hours,
            include_previous_apcp_dependency=True,
            acquire_lead=lambda cycle, lead, deadline: acquire_gfs_lead(
                gfs_settings,
                transport=self._gfs_transport,
                clock=self._clock,
                sleeper=self._sleeper,
                cycle_date=cycle.date(),
                cycle_hour=cycle.hour,
                forecast_hour=lead,
                cycle_deadline=deadline,
            ),
        )
        if gfs_result is not None:
            selected["GFS"] = gfs_result

        return selected

    def _try_candidates(
        self,
        *,
        model: str,
        candidates: tuple[datetime, ...],
        target: datetime,
        information_cutoff: datetime,
        max_age_hours: float,
        completion_deadline_minutes: float,
        max_source_lead_hours: int,
        include_previous_apcp_dependency: bool,
        acquire_lead: Any,
    ) -> tuple[Phase2LeadAcquisition, ...] | None:
        """Try candidate cycles newest-first and return the first whose
        complete required lead set acquires, satisfies the model's own
        max-age/completion-deadline policy, *and* was fully available no
        later than ``information_cutoff``.

        Codex re-review finding 4: the cutoff is an explicit input here
        rather than a downstream-only check. A candidate whose index or
        message bytes only became available after the cutoff is rejected
        at selection time and the walk continues to the next (older)
        candidate, so the run deterministically falls back to a cycle
        the forecast was actually entitled to see instead of failing
        later -- or worse, retaining a late acquisition.
        """
        for candidate in candidates:
            age_hours = (target - candidate).total_seconds() / 3600.0
            if age_hours > max_age_hours or age_hours < 0:
                continue
            if candidate > information_cutoff:
                logger.debug(
                    "candidate cycle %s for %s is after the information cutoff %s",
                    candidate,
                    model,
                    information_cutoff,
                )
                continue
            lead_sets = _required_source_leads(
                target_reference_time=target,
                candidate_reference_time=candidate,
                max_source_lead_hours=max_source_lead_hours,
                include_previous_apcp_dependency=include_previous_apcp_dependency,
            )
            if lead_sets is None:
                continue
            _target_leads, required_leads = lead_sets
            deadline = candidate + timedelta(minutes=completion_deadline_minutes)
            try:
                acquisitions = tuple(
                    acquire_lead(candidate, lead, deadline) for lead in required_leads
                )
            except Exception as exc:  # noqa: BLE001 -- unavailable/incomplete candidate; try next
                logger.debug(
                    "candidate cycle %s for %s failed acquisition: %s", candidate, model, exc
                )
                continue
            late = _late_acquisitions(acquisitions, information_cutoff)
            if late:
                logger.debug(
                    "candidate cycle %s for %s completed after the information cutoff %s "
                    "(latest available_at %s across %d late lead(s)); rejecting the whole "
                    "candidate rather than retaining a late input",
                    candidate,
                    model,
                    information_cutoff,
                    max(late),
                    len(late),
                )
                continue
            # The cycle's completion instant is the provider's own
            # publication assertion for the last object in the required
            # set, not this process's retrieval wall time. Comparing
            # retrieval time to the cycle-completion deadline would
            # reject every legitimately-published retrospective cycle
            # merely because it was fetched later.
            completed_at = max(a.grib_available_at for a in acquisitions)
            selection = select_model_cycle(
                model=model,  # type: ignore[arg-type]
                target_reference_time=target,
                candidates=(
                    CandidateCycle(
                        reference_time=candidate, is_complete=True, completed_at=completed_at
                    ),
                ),
                max_age_hours=max_age_hours,
                completion_deadline_minutes=completion_deadline_minutes,
            )
            if selection.selected is not None:
                return acquisitions
        return None

    def acquire(
        self,
        request: Phase2Request,
        discovery: DiscoveryResult,
        *,
        artifact_service: ArtifactService,
    ) -> Phase2SelectedInputs:
        acquisitions_by_model = self._acquisitions_by_run.get(str(request.run_id))
        if acquisitions_by_model is None:
            raise ValueError(
                f"no discovery acquisition retained for run {request.run_id!r}; discover() "
                "must run before acquire() on the same Phase2ProductionProvider instance"
            )

        # Codex re-review finding 5: the run spec is the durable replay
        # identity. It must carry every input that determines what the
        # run computes -- the verification cutoff, the required source
        # groups, the cycle policy actually applied, the target horizons,
        # and the request/configuration digests -- so a replay is
        # reconstructed from persisted bytes rather than from whatever
        # mutable live configuration happens to be loaded at replay time.
        # Codex re-review finding 5: the station snapshot must carry the
        # exact coordinates, elevation, provider identity, and every
        # other station field consumed downstream (alignment, METAR
        # normalization, matching), not just a list of station IDs.
        station_snapshot = self._register_source(
            artifact_service,
            request,
            locator=f"phase2-station-catalog://{request.run_id}",
            artifact_type="station-catalog-snapshot",
            schema="station-catalog-snapshot.v1",
            payload=_JSON.serialize(self._station_snapshot_payload()),
            # This immutable snapshot describes configuration already fixed
            # for the run at the information cutoff. Persistence may happen
            # later, but its authoritative information availability is the
            # cutoff rather than the wall-clock registration time.
            available_at=request.information_cutoff,
        )

        roots: list[SelectedSourceRoot] = []
        for model in _MODELS:
            acquisitions = acquisitions_by_model.get(model)
            if not acquisitions:
                continue
            for acquisition in acquisitions:
                index_artifact = self._register_source(
                    artifact_service,
                    request,
                    locator=acquisition.resolved_index_url,
                    artifact_type=f"{model.lower()}-grib-index",
                    schema=f"{model.lower()}-index.v1",
                    payload=acquisition.index_payload,
                    media_type="text/plain",
                    available_at=acquisition.index_available_at,
                    source_revision=acquisition.endpoint,
                )
                roots.append(
                    SelectedSourceRoot(
                        model=model,  # type: ignore[arg-type]
                        source_lead_hours=acquisition.forecast_hour,
                        field_role="index",
                        artifact=index_artifact,
                    )
                )
                for ordinal, message in enumerate(acquisition.selected_messages):
                    record_identity = _record_identity(message, ordinal)
                    message_artifact = self._register_source(
                        artifact_service,
                        request,
                        locator=(
                            f"{acquisition.resolved_grib_url}#{message.canonical_variable_id}"
                            f";{record_identity}"
                        ),
                        artifact_type=f"{model.lower()}-grib-selected-grib",
                        schema=f"{model.lower()}-message.v1",
                        payload=message.payload,
                        media_type="application/octet-stream",
                        available_at=acquisition.grib_available_at,
                        source_revision=acquisition.endpoint,
                    )
                    roots.append(
                        SelectedSourceRoot(
                            model=model,  # type: ignore[arg-type]
                            source_lead_hours=acquisition.forecast_hour,
                            field_role=(
                                f"message:{message.canonical_variable_id}:record:{record_identity}"
                            ),
                            artifact=message_artifact,
                        )
                    )

        model_order = {"HRRR": 0, "NBM": 1, "GFS": 2}
        roots.sort(key=lambda r: (model_order[r.model], r.source_lead_hours, r.field_role))
        run_spec_payload = self._run_spec_payload(
            request, discovery, acquisitions_by_model, station_snapshot, tuple(roots)
        )
        run_spec = self._register_source(
            artifact_service,
            request,
            locator=f"phase2-run-spec://{request.run_id}",
            artifact_type="phase2-run-spec",
            schema="phase2-run-spec.v1",
            payload=_JSON.serialize(run_spec_payload),
            # The spec records only configuration and acquisitions whose
            # availability has already been proved at the cutoff. Assigning
            # the cutoff keeps legitimate post-cutoff orchestration compatible
            # with create_run's fail-closed selected-input eligibility check.
            available_at=request.information_cutoff,
        )
        return Phase2SelectedInputs(
            run_spec=run_spec, source_roots=tuple(roots), station_snapshot=station_snapshot
        )

    def _run_spec_payload(
        self,
        request: Phase2Request,
        discovery: DiscoveryResult,
        acquisitions_by_model: dict[str, tuple[Phase2LeadAcquisition, ...]],
        station_snapshot: ArtifactManifest,
        source_roots: tuple[SelectedSourceRoot, ...],
    ) -> dict[str, Any]:
        """Build the complete durable replay run spec (finding 5, and
        Codex review ``t_652b155e``'s replay blocker).

        Everything a replay needs to reconstruct *what this run was
        asked to compute* and *what it actually consumed* is persisted
        here: both cutoffs, the target frame, the required source groups
        with the exact cycle/leads selected and each lead's own
        acquisition evidence (endpoint, resolved index/GRIB URLs,
        completion timestamps, and every selected message's inventory
        row, message number and byte range), the cycle-selection policy
        applied, the target horizons, every request/configuration
        identity digest, and the complete ``Phase2Configuration`` itself.

        Persisting the configuration is what finally severs replay from
        live objects: alignment, availability, blending, observation
        normalization, matching, and verification all read policy back
        out of these bytes. Persisting per-lead message evidence is what
        severs normalization from the provider's process-local
        ``_acquisitions_by_run`` cache.
        """
        cycle_policy = self._configuration.cycle_selection_policy
        required_groups = []
        for model in _MODELS:
            acquisitions = acquisitions_by_model.get(model, ())
            settings = getattr(self._configuration, model.lower())
            reference_time = (
                self._reference_time_of(acquisitions[0]).isoformat() if acquisitions else None
            )
            required_groups.append(
                {
                    "model": model,
                    "selected": bool(acquisitions),
                    "source_cycle_reference_time": reference_time,
                    "source_lead_hours": sorted(a.forecast_hour for a in acquisitions),
                    "endpoints": sorted({a.endpoint for a in acquisitions}),
                    "max_age_hours": settings.max_age_hours,
                    "cycle_completion_deadline_minutes": (
                        settings.cycle_completion_deadline_minutes
                    ),
                    "allowed_cycle_hours": list(getattr(settings, "allowed_cycle_hours", ())),
                    "canonical_variable_ids": sorted(
                        contract.canonical_variable_id for contract in settings.field_contracts
                    ),
                    "source_grid_profile_id": _source_grid_profile_id(settings, model),
                    "leads": [
                        self._lead_payload(acquisition, source_roots, model)
                        for acquisition in sorted(acquisitions, key=lambda a: a.forecast_hour)
                    ],
                }
            )
        return {
            "schema_version": "phase2-run-spec.v1",
            "run_id": str(request.run_id),
            "target_reference_time": request.target_reference_time.isoformat(),
            "forecast_issue_time": request.forecast_issue_time.isoformat(),
            "information_cutoff": request.information_cutoff.isoformat(),
            "verification_cutoff": request.verification_cutoff.isoformat(),
            "target_horizons": list(request.target_horizons),
            "random_seed": request.random_seed,
            "selection_digest": str(discovery.selection_digest),
            "cycle_selection_policy": {
                "schema_version": cycle_policy.schema_version,
                "policy_id": str(cycle_policy.policy_id),
                "target_horizons": list(cycle_policy.target_horizons),
            },
            "required_groups": required_groups,
            "request_digests": {
                "configuration_snapshot_id": str(request.configuration_snapshot_id),
                "configuration_digest": str(request.configuration_digest),
                "code_revision": request.code_revision,
                "environment_digest": str(request.environment_digest),
                "lockfile_digest": str(request.lockfile_digest),
            },
            "configuration_digests": {
                "phase2_configuration": str(
                    Digest.of_bytes(_JSON.serialize(self._configuration.model_dump(mode="json")))
                ),
                "blend_configuration": str(
                    Digest.of_bytes(
                        _JSON.serialize(
                            self._configuration.blend_configuration.model_dump(mode="json")
                        )
                    )
                ),
                "matching_policy": str(self._configuration.matching_policy.digest),
            },
            "configuration": self._configuration.model_dump(mode="json"),
            "station_snapshot": self._artifact_identity(station_snapshot),
        }

    @staticmethod
    def _lead_payload(
        acquisition: Phase2LeadAcquisition,
        source_roots: tuple[SelectedSourceRoot, ...],
        model: str,
    ) -> dict[str, Any]:
        """Persist one acquired lead's complete evidence.

        ``record_identity`` is byte-identical to the fragment
        :meth:`acquire` embeds in each registered message artifact's
        ``field_role``, so a replay can bind persisted message metadata
        to pinned source roots with no live acquisition object.
        """
        by_role = {
            root.field_role: root.artifact
            for root in source_roots
            if root.model == model and root.source_lead_hours == acquisition.forecast_hour
        }
        return {
            "source_lead_hours": acquisition.forecast_hour,
            "endpoint": acquisition.endpoint,
            "resolved_index_url": acquisition.resolved_index_url,
            "resolved_grib_url": acquisition.resolved_grib_url,
            # Local retrieval provenance of this process.
            "index_completed_at": acquisition.index_completed_at.isoformat(),
            "grib_completed_at": acquisition.grib_completed_at.isoformat(),
            # Authoritative provider publication instants (the values the
            # cutoff/deadline policy was actually applied to), plus the
            # verbatim provider assertions they were derived from.
            "index_available_at": acquisition.index_available_at.isoformat(),
            "grib_available_at": acquisition.grib_available_at.isoformat(),
            "index_last_modified": acquisition.index_last_modified,
            "grib_last_modified": acquisition.full_object_last_modified,
            "selected_messages": [
                {
                    "canonical_variable_id": message.canonical_variable_id,
                    "record_identity": _record_identity(message, ordinal),
                    "ordinal": ordinal,
                    "message_number": message.row.message_number,
                    "byte_offset": message.row.byte_offset,
                    "byte_start": message.byte_start,
                    "byte_end": message.byte_end,
                    "inventory_row": message.row.line,
                    "artifact": Phase2ProductionProvider._artifact_identity(
                        by_role[
                            f"message:{message.canonical_variable_id}:record:"
                            f"{_record_identity(message, ordinal)}"
                        ]
                    ),
                }
                for ordinal, message in enumerate(acquisition.selected_messages)
            ],
            "index_artifact": Phase2ProductionProvider._artifact_identity(by_role["index"]),
        }

    @staticmethod
    def _artifact_identity(manifest: ArtifactManifest) -> dict[str, str]:
        return {
            "artifact_id": str(manifest.artifact_id),
            "content_digest": str(manifest.content_digest),
        }

    def _station_snapshot_payload(self) -> dict[str, Any]:
        """Build the complete durable station snapshot (finding 5).

        Every station field the downstream science actually consumes is
        persisted: the exact expected coordinates and elevation used by
        bilinear alignment, the provider ICAO identity used to fetch and
        match METAR reports, the site name/types/priority, and the
        explicitly-unknown exposure/instrument identities. Replay reads
        these bytes instead of re-deriving station geometry from live
        configuration.
        """
        return {
            "schema_version": "station-catalog-snapshot.v1",
            "domain_id": str(self._configuration.domain.domain_id),
            "station_ids": [str(station.station_id) for station in self._configuration.stations],
            "stations": [
                {
                    "station_id": str(station.station_id),
                    "provider_icao_id": station.provider_icao_id,
                    "expected_latitude": station.expected_latitude,
                    "expected_longitude": station.expected_longitude,
                    "expected_elevation_m": station.expected_elevation_m,
                    "site_name": station.site_name,
                    "provider_site_types": list(station.provider_site_types),
                    "provider_priority": station.provider_priority,
                    "exposure_identity": station.exposure_identity,
                    "instrument_identity": station.instrument_identity,
                }
                for station in self._configuration.stations
            ],
            "point_extraction_policy": {
                "schema_version": (self._configuration.point_extraction_policy.schema_version),
                "policy_id": self._configuration.point_extraction_policy.policy_id,
                "allow_extrapolation": (
                    self._configuration.point_extraction_policy.allow_extrapolation
                ),
                "require_four_corners_finite": (
                    self._configuration.point_extraction_policy.require_four_corners_finite
                ),
                "halo_cells": self._configuration.point_extraction_policy.halo_cells,
                "weight_sum_tolerance": (
                    self._configuration.point_extraction_policy.weight_sum_tolerance
                ),
            },
        }

    @staticmethod
    def _reference_time_of(acquisition: Phase2LeadAcquisition) -> datetime:
        return datetime(
            acquisition.cycle_date.year,
            acquisition.cycle_date.month,
            acquisition.cycle_date.day,
            acquisition.cycle_hour,
            tzinfo=UTC,
        )

    def _register_source(
        self,
        artifact_service: ArtifactService,
        request: Phase2Request,
        *,
        locator: str,
        artifact_type: str,
        schema: str,
        payload: bytes,
        media_type: str = "application/json",
        available_at: datetime | None = None,
        source_revision: str = "production.v1",
    ) -> ArtifactManifest:
        now = self._clock.now()
        return artifact_service.register_source(
            SourceRegistrationRequest(
                source_authority="mesoforge.phase2",
                source_locator=locator,
                source_revision=source_revision,
                artifact_type=artifact_type,
                artifact_schema_version=schema,
                media_type=media_type,
                created_at=now,
                availability=Availability(
                    available_at=available_at or now,
                    authority="mesoforge.phase2",
                    method="byte-range-fetch",
                ),
                configuration_snapshot_id=request.configuration_snapshot_id,
                configuration_digest=request.configuration_digest,
                code_revision=request.code_revision,
                environment_digest=request.environment_digest,
            ),
            payload,
        )

    def retained_acquisitions_for(
        self, run_id: RunId
    ) -> dict[str, tuple[Phase2LeadAcquisition, ...]]:
        """Diagnostic view of this instance's retained per-lead
        acquisitions.

        This is deliberately *not* a science input. Codex review
        ``t_652b155e``: normalization and lineage used to read this
        process-local cache, which made replay impossible in a fresh
        process. Every fact they need is now persisted in the run spec
        instead; this accessor exists only so selection/cutoff behavior
        can be observed directly at the discovery boundary.
        """
        return self._acquisitions_by_run.get(str(run_id), {})


class Phase2UnavailableProvider:
    """A provider that refuses discovery/acquisition outright.

    Replay must never touch a source provider. Wiring this in makes that
    structural rather than merely conventional: a coordinator built by
    :func:`build_phase2_replay_adapters` cannot issue a provider request
    even if a future change accidentally called ``run`` instead of
    ``replay``.
    """

    def discover(self, request: Phase2Request) -> DiscoveryResult:
        raise Phase2ReplayIdentityError(
            "replay must not perform source discovery; it reconstructs the run from the "
            "persisted run spec and pinned artifacts only"
        )

    def acquire(
        self,
        request: Phase2Request,
        discovery: DiscoveryResult,
        *,
        artifact_service: ArtifactService,
    ) -> Phase2SelectedInputs:
        raise Phase2ReplayIdentityError(
            "replay must not perform source acquisition; it reuses the run's pinned "
            "source artifacts"
        )


class Phase2ProductionScience:
    """Concrete science stages backing ``Phase2ArtifactOperations``:
    real per-model normalization (``guidance.normalization_v2``),
    station alignment, availability, atomic blend generation, identity
    correction, METAR normalization, matching, and verification --
    composed from the pure domain functions plus the production
    ``ArtifactService``. Never redefines the coordinator's ordering;
    only supplies the eight injected stage callables.

    Codex review ``t_652b155e``: no stage reads a live
    ``Phase2Configuration``, a live station catalog, or the provider's
    process-local acquisition cache. Every policy value, station,
    selected cycle, and per-lead acquisition fact comes from the
    ``Phase2PersistedRun`` the coordinator reconstructed from the run's
    own immutable ``phase2-run-spec.v1``/``station-catalog-snapshot.v1``
    artifacts. A live configuration may still be supplied (production
    wiring does), but it is then only ever *checked against* the
    persisted identity and never read for policy -- so mutating it
    cannot change a replay's result, and swapping it for a different
    configuration fails closed.
    """

    def __init__(
        self,
        *,
        aviationweather_transport: HttpTransport,
        clock: Clock,
        sleeper: Sleeper,
        configuration: Phase2Configuration | None = None,
        aviationweather_rate_limiter: RequestRateLimiter | None = None,
    ) -> None:
        self._configuration = configuration
        self._configuration_digest = (
            Digest.of_bytes(_JSON.serialize(configuration.model_dump(mode="json")))
            if configuration is not None
            else None
        )
        self._aviationweather_transport = aviationweather_transport
        self._clock = clock
        self._sleeper = sleeper
        self._explicit_rate_limiter = aviationweather_rate_limiter
        self._rate_limiters: dict[float, RequestRateLimiter] = {}

    def _require_persisted_identity(self, persisted: Phase2PersistedRun) -> None:
        """Fail closed when a supplied live configuration conflicts with
        the persisted run identity.

        When no live configuration was supplied there is nothing that
        *could* conflict -- the stage reads persisted bytes only.
        """
        if self._configuration_digest is not None:
            persisted.require_configuration_identity(self._configuration_digest)

    def _rate_limiter(self, persisted: Phase2PersistedRun) -> RequestRateLimiter:
        """The AviationWeather rate limiter for this run's persisted
        minimum request interval.

        Limiters are cached per interval so repeated live runs share
        pacing state; replay never reaches this path at all because it
        reuses recorded response artifacts.
        """
        if self._explicit_rate_limiter is not None:
            return self._explicit_rate_limiter
        interval = float(persisted.aviationweather.min_request_interval_seconds)
        limiter = self._rate_limiters.get(interval)
        if limiter is None:
            limiter = RequestRateLimiter(min_interval_seconds=interval)
            self._rate_limiters[interval] = limiter
        return limiter

    def operations(self) -> Phase2ArtifactOperations:
        return Phase2ArtifactOperations(
            normalize=self._normalize,
            align=self._align,
            evaluate=self._evaluate,
            generate=self._generate,
            apply=self._apply,
            acquire_and_normalize=self._acquire_and_normalize,
            match=self._match,
            calculate=self._calculate,
        )

    # ------------------------------------------------------------------
    # normalize
    # ------------------------------------------------------------------

    def _normalize(
        self,
        request: Phase2Request,
        run: RunManifest,
        inputs: Phase2SelectedInputs,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> NormalizedGuidanceArtifacts:
        self._require_persisted_identity(persisted)
        artifacts: list[ArtifactManifest] = []
        for model in _MODELS:
            group = persisted.run_spec.group(model)
            model_roots = [
                r
                for r in inputs.source_roots
                if r.model == model and r.field_role.startswith("message:")
            ]
            if not group.selected:
                if model_roots:
                    raise GuidanceNormalizationProductionError(
                        f"{model} has pinned message roots but the persisted run spec records "
                        "no selected cycle for it"
                    )
                continue
            if not model_roots:
                raise GuidanceNormalizationProductionError(
                    f"{model} was selected by the persisted run spec but no message roots "
                    "were pinned"
                )
            model_roots.sort(key=lambda r: (r.source_lead_hours, r.field_role))
            input_refs = tuple(
                (f"{r.source_lead_hours}:{r.field_role.split(':', 1)[1]}", r.artifact.artifact_id)
                for r in model_roots
            )
            index_refs = tuple(
                (f"index:{r.source_lead_hours}", r.artifact.artifact_id)
                for r in inputs.source_roots
                if r.model == model and r.field_role == "index"
            )
            # Codex review t_652b155e: the selected cycle comes from the
            # persisted run spec, not from the provider's process-local
            # acquisition cache, so a fresh process replays identically.
            reference_time = persisted.reference_time(model)
            cycle_age_hours = int(
                (persisted.run_spec.target_reference_time - reference_time).total_seconds() // 3600
            )
            target_source_leads = tuple(
                horizon + cycle_age_hours for horizon in persisted.target_horizons
            )
            grid_id = GridId(f"phase2-{model.lower()}.v1")
            settings = persisted.source_settings(model)

            # Codex re-review finding 3: every model -- not only GFS --
            # must create and reference a complete, real
            # ``variable-lineage.v2`` manifest. A GFS-QPF-only lineage
            # artifact does not describe the other six variables, and
            # pointing ``variable_lineage_manifest_id`` at a raw message
            # artifact describes nothing at all.
            lineage_artifact, lineage_payload = self._register_variable_lineage_v2(
                request,
                artifact_service,
                model=model,
                grid_id=grid_id,
                reference_time=reference_time,
                source_leads=target_source_leads,
                group=group,
                settings=settings,
                input_refs=input_refs,
                index_refs=index_refs,
                domain_bbox=persisted.configuration.domain.bbox,
                halo_cells=persisted.point_extraction_policy.halo_cells,
            )

            qpf_lineage_artifact: ArtifactManifest | None = None
            if model == "GFS":

                def _lineage_transform(
                    *raw: bytes,
                    refs: tuple[tuple[str, ArtifactId], ...] = input_refs,
                    source_leads: tuple[int, ...] = target_source_leads,
                    cycle_reference_time: datetime = reference_time,
                    gfs_settings: Any = settings,
                    domain_bbox: BoundingBox = persisted.configuration.domain.bbox,
                    halo_cells: int = persisted.point_extraction_policy.halo_cells,
                ) -> dict[str, Any]:
                    field_payloads = _group_normalization_payloads(refs, raw)
                    self._require_contiguous_previous_leads(field_payloads, source_leads)
                    _dataset, lineage = normalize_gfs_cycle(
                        settings=gfs_settings,
                        forecast_reference_time=cycle_reference_time,
                        source_lead_hours=source_leads,
                        field_payloads=field_payloads,
                        grid_id="phase2-gfs.v1",
                        configuration_snapshot_id=str(request.configuration_snapshot_id),
                        variable_lineage_manifest_id=str(refs[0][1]),
                        domain_bbox=domain_bbox,
                        halo_cells=halo_cells,
                    )
                    apcp_parents: dict[int, list[str]] = {}
                    for role, artifact_id in refs:
                        lead_text, variable_id, *_identity = role.split(":")
                        if variable_id == "liquid_equivalent_precipitation_amount_1h":
                            apcp_parents.setdefault(int(lead_text), []).append(str(artifact_id))
                    return {
                        "schema_version": "gfs-qpf-lineage.v1",
                        "records": [
                            {
                                "source_forecast_hour": item.forecast_hour,
                                "parent_artifact_ids": apcp_parents[item.forecast_hour],
                                "previous_parent_artifact_ids": apcp_parents.get(
                                    item.forecast_hour - 1, []
                                ),
                                "dual_parent_equivalent": item.equivalent,
                                "bucket_start_hour": item.result.bucket_start_hour,
                                "is_reset_passthrough": item.result.is_reset_passthrough,
                                "finite_precision_floor_applied": (
                                    item.result.finite_precision_floor_applied
                                ),
                                "one_hour_qpf_kg_m2": item.result.one_hour_qpf_kg_m2,
                                "parent_bucket_value_current": (
                                    item.result.parent_bucket_value_current
                                ),
                                "parent_bucket_value_previous": (
                                    item.result.parent_bucket_value_previous
                                ),
                            }
                            for item in lineage
                        ],
                    }

                lineage_result = artifact_service.execute_raw_transformation(
                    self._transformation(
                        request,
                        activity="record-gfs-qpf-lineage",
                        inputs=input_refs,
                        output_role="qpf-lineage",
                        artifact_type="gfs-qpf-lineage",
                        schema="gfs-qpf-lineage.v1",
                        parameters={"model": model},
                    ),
                    transform=_lineage_transform,
                    serializer=_JSON,
                    input_loader=lambda payload: payload,
                    output_validator=_valid_map,
                )
                qpf_lineage_artifact = lineage_result.output

            canonical_input_refs = (
                input_refs
                + index_refs
                + (("variable-lineage", lineage_artifact.artifact_id),)
                + (
                    (("qpf-lineage", qpf_lineage_artifact.artifact_id),)
                    if qpf_lineage_artifact is not None
                    else ()
                )
            )
            transformation = self._transformation(
                request,
                activity=f"normalize-{model.lower()}",
                inputs=canonical_input_refs,
                output_role="canonical-guidance",
                artifact_type="canonical-guidance",
                schema="canonical-guidance.v2",
                media_type="application/x-netcdf",
                parameters={"model": model},
            )

            def _transform(
                *raw: bytes,
                model: str = model,
                refs: tuple[tuple[str, ArtifactId], ...] = input_refs,
                reference_time: datetime = reference_time,
                source_leads: tuple[int, ...] = target_source_leads,
                lineage_id: str = str(lineage_artifact.artifact_id),
                model_settings: Any = settings,
                # Bounded canonical retention (owner architecture
                # decision). Both come from the run's own persisted
                # evidence -- the run spec's embedded configuration and
                # the station snapshot's point-extraction policy -- so a
                # replay in a fresh process retains byte-identically the
                # same window even if live configuration has moved.
                domain_bbox: BoundingBox = persisted.configuration.domain.bbox,
                halo_cells: int = persisted.point_extraction_policy.halo_cells,
            ) -> xr.Dataset:
                field_payloads = _group_normalization_payloads(refs, raw[: len(refs)])
                grid_id = f"phase2-{model.lower()}.v1"
                cfg_id = str(request.configuration_snapshot_id)
                if model == "HRRR":
                    return normalize_hrrr_phase2_cycle(
                        settings=model_settings,
                        forecast_reference_time=reference_time,
                        source_lead_hours=source_leads,
                        field_payloads=field_payloads,
                        grid_id=grid_id,
                        configuration_snapshot_id=cfg_id,
                        variable_lineage_manifest_id=lineage_id,
                        domain_bbox=domain_bbox,
                        halo_cells=halo_cells,
                    )
                if model == "NBM":
                    return normalize_nbm_cycle(
                        settings=model_settings,
                        forecast_reference_time=reference_time,
                        source_lead_hours=source_leads,
                        field_payloads=field_payloads,
                        grid_id=grid_id,
                        configuration_snapshot_id=cfg_id,
                        variable_lineage_manifest_id=lineage_id,
                        domain_bbox=domain_bbox,
                        halo_cells=halo_cells,
                    )
                self._require_contiguous_previous_leads(field_payloads, source_leads)
                dataset, _lineage = normalize_gfs_cycle(
                    settings=model_settings,
                    forecast_reference_time=reference_time,
                    source_lead_hours=source_leads,
                    field_payloads=field_payloads,
                    grid_id=grid_id,
                    configuration_snapshot_id=cfg_id,
                    variable_lineage_manifest_id=lineage_id,
                    domain_bbox=domain_bbox,
                    halo_cells=halo_cells,
                )
                return dataset

            result = artifact_service.execute_raw_transformation(
                transformation,
                transform=_transform,
                serializer=_NETCDF,
                input_loader=lambda payload: payload,
                output_validator=_canonical_validator(lineage_artifact, payload=lineage_payload),
            )
            artifacts.append(result.output)
        return NormalizedGuidanceArtifacts(artifacts=tuple(artifacts))

    def _register_variable_lineage_v2(
        self,
        request: Phase2Request,
        artifact_service: ArtifactService,
        *,
        model: str,
        grid_id: GridId,
        reference_time: datetime,
        source_leads: tuple[int, ...],
        group: PersistedModelGroup,
        settings: Any,
        input_refs: tuple[tuple[str, ArtifactId], ...],
        index_refs: tuple[tuple[str, ArtifactId], ...],
        domain_bbox: BoundingBox,
        halo_cells: int,
    ) -> tuple[ArtifactManifest, dict[str, Any]]:
        """Create the model's complete ``variable-lineage.v2`` manifest
        (Codex re-review finding 3).

        Every canonical variable at every target source lead gets a real
        entry naming the retained index artifact, every selected message
        artifact that fed it, the exact inventory rows/message
        numbers/byte ranges, the resolved URLs and endpoint, the
        selector actually used, the decode arguments, the unit
        conversion, the wind-rotation policy, and the source/output grid
        identity. This is a derived artifact of the run, so its lineage
        edges reach every selected source root.

        Codex review ``t_652b155e``: all of that evidence is read from
        the persisted run spec's per-lead acquisition records
        (``group``) and the persisted source settings, never from a live
        acquisition object -- so a fresh process rebuilds a
        byte-identical manifest.
        """
        selector_builder = _SELECTOR_BUILDERS[model]
        source_grid_profile_id = group.source_grid_profile_id
        index_by_lead = {
            int(role.split(":", 1)[1]): artifact_id for role, artifact_id in index_refs
        }
        leads_by_hour = group.leads_by_hour
        # role -> artifact for every selected message, keyed by
        # (lead, canonical_variable_id) so plural GFS APCP parents group.
        messages_by_key: dict[tuple[int, str], list[ArtifactId]] = {}
        for role, artifact_id in input_refs:
            lead_text, variable_id, *_identity = role.split(":")
            messages_by_key.setdefault((int(lead_text), variable_id), []).append(artifact_id)

        variable_ids = tuple(group.canonical_variable_ids)
        entries: list[dict[str, Any]] = []
        for lead in source_leads:
            acquisition: PersistedLeadAcquisition | None = leads_by_hour.get(lead)
            index_artifact_id = index_by_lead.get(lead)
            if acquisition is None or index_artifact_id is None:
                raise GuidanceNormalizationProductionError(
                    f"{model} lead {lead!r} has no persisted acquisition/index evidence; a "
                    "complete variable-lineage.v2 manifest cannot be built"
                )
            for variable_id in variable_ids:
                messages = acquisition.messages_for(variable_id)
                artifact_ids = messages_by_key.get((lead, variable_id), [])
                if not messages or len(messages) != len(artifact_ids):
                    raise GuidanceNormalizationProductionError(
                        f"{model} lead {lead!r} variable {variable_id!r} has "
                        f"{len(messages)} persisted message(s) but {len(artifact_ids)} "
                        "registered artifact(s); lineage would be incomplete"
                    )
                entries.append(
                    {
                        "canonical_variable_id": variable_id,
                        "source_lead_hours": lead,
                        "index_artifact_id": str(index_artifact_id),
                        "selected_grib_artifact_ids": [str(a) for a in artifact_ids],
                        "message_numbers": [m.message_number for m in messages],
                        "byte_ranges": [[m.byte_start, m.byte_end] for m in messages],
                        "inventory_rows": [m.inventory_row for m in messages],
                        "source_cycle": reference_time.isoformat(),
                        "endpoint": acquisition.endpoint,
                        "resolved_index_url": acquisition.resolved_index_url,
                        "resolved_grib_url": acquisition.resolved_grib_url,
                        "selector_expression": selector_builder(variable_id, forecast_hour=lead),
                        "decode_backend": "cfgrib",
                        "decode_backend_kwargs": {
                            "indexpath": "",
                            "errors": "raise",
                            "read_keys": list(settings.read_keys),
                        },
                        "unit_conversion": _UNIT_CONVERSIONS[variable_id],
                        "wind_rotation_policy": _wind_rotation_policy(model, variable_id),
                        "source_grid_profile_id": source_grid_profile_id,
                        "source_grid_id": source_grid_profile_id,
                        "output_grid_id": grid_id,
                    }
                )

        payload = {
            "schema_version": "variable-lineage.v2",
            "model": model.lower(),
            "grid_id": grid_id,
            "source_grid_profile_id": source_grid_profile_id,
            "forecast_reference_time": reference_time.isoformat(),
            "configuration_snapshot_id": str(request.configuration_snapshot_id),
            "entries": entries,
            "expected_canonical_variable_ids": list(variable_ids),
            "expected_source_lead_hours": list(source_leads),
            # Bounded canonical retention (owner architecture decision):
            # the policy the canonical artifact was cut under, read from
            # the run's own persisted configuration and station snapshot.
            "canonical_retention_policy": {
                "policy_id": SUBSET_POLICY_ID,
                "halo_cells": halo_cells,
                "bbox_south": domain_bbox.south,
                "bbox_north": domain_bbox.north,
                "bbox_west": domain_bbox.west,
                "bbox_east": domain_bbox.east,
            },
        }
        # Validate before registration so an incomplete manifest can
        # never reach storage or be referenced by canonical guidance.
        VariableLineageManifestV2.model_validate_json(_JSON.serialize(payload))

        result = artifact_service.execute_raw_transformation(
            self._transformation(
                request,
                activity=f"record-variable-lineage-{model.lower()}",
                inputs=input_refs + index_refs,
                output_role="variable-lineage",
                artifact_type="variable-lineage",
                schema="variable-lineage.v2",
                parameters={"model": model},
            ),
            transform=lambda *_raw, payload=payload: payload,
            serializer=_JSON,
            input_loader=lambda payload: payload,
            output_validator=_validate_variable_lineage_v2,
        )
        return result.output, payload

    def _require_contiguous_previous_leads(
        self,
        field_payloads: dict[str, dict[int, bytes | tuple[bytes, ...]]],
        source_leads: tuple[int, ...],
    ) -> None:
        """GFS one-hour QPF requires the same-bucket previous-hour
        value at every grid point; ``normalize_gfs_cycle`` decodes the
        adjacent lead's APCP payload directly from ``field_payloads``.
        This asserts up front that every non-reset lead's immediately
        preceding lead's own APCP payload was actually acquired, so a
        missing dependency fails fast here rather than deep inside
        decoding."""
        qpf_payloads = field_payloads.get("liquid_equivalent_precipitation_amount_1h", {})
        for lead in source_leads:
            if is_bucket_reset_hour(lead):
                continue
            if (lead - 1) not in qpf_payloads:
                raise GuidanceNormalizationProductionError(
                    f"GFS lead {lead!r} requires the previous lead {lead - 1!r}'s APCP payload "
                    "for same-bucket differencing, but it was not acquired"
                )

    # ------------------------------------------------------------------
    # align
    # ------------------------------------------------------------------

    def _align(
        self,
        request: Phase2Request,
        run: RunManifest,
        guidance: NormalizedGuidanceArtifacts,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> AlignmentArtifacts:
        self._require_persisted_identity(persisted)
        transformation = self._transformation(
            request,
            activity="align-stations",
            inputs=tuple(
                (f"model-{i}", item.artifact_id) for i, item in enumerate(guidance.artifacts)
            ),
            output_role="aligned",
            artifact_type="aligned-station-guidance",
            schema="aligned-station-guidance.v1",
        )
        # Stations and horizons come from the run's own persisted
        # station snapshot/run spec, so mutating the live catalog cannot
        # move a replay's extraction points (Codex review t_652b155e).
        stations = persisted.stations
        horizons = persisted.target_horizons
        target_reference_time = persisted.run_spec.target_reference_time

        def _transform(*datasets: xr.Dataset) -> dict[str, Any]:
            models: list[str] = []
            values: dict[str, dict[str, dict[str, float]]] = {}
            lineage: dict[str, dict[str, dict[str, Any]]] = {}
            for dataset, artifact in zip(datasets, guidance.artifacts, strict=True):
                model = str(dataset.attrs["model"]).upper()
                models.append(model)
                cycle_reference_time = np.datetime_as_string(
                    dataset["forecast_reference_time"].values.astype("datetime64[ns]"),
                    unit="s",
                    timezone="UTC",
                )
                crs = _crs_from_dataset(dataset)
                model_values: dict[str, dict[str, float]] = {}
                model_lineage: dict[str, dict[str, Any]] = {}
                for horizon in horizons:
                    target_valid_time = np.datetime64(
                        target_reference_time.replace(tzinfo=None), "ns"
                    ) + np.timedelta64(horizon, "h")
                    matching = np.flatnonzero(
                        dataset["source_valid_time"].values.astype("datetime64[ns]")
                        == target_valid_time
                    )
                    if matching.size != 1:
                        raise ValueError(
                            f"{model} does not provide exactly one source lead for target "
                            f"horizon {horizon}"
                        )
                    source_lead_hours = int(
                        dataset["source_lead_time"].values[matching[0]] / np.timedelta64(1, "h")
                    )
                    model_lineage[str(horizon)] = {
                        "source_cycle_reference_time": cycle_reference_time,
                        "source_forecast_hour": source_lead_hours,
                        "artifact_id": str(artifact.artifact_id),
                    }
                    by_station: dict[str, float] = {}
                    for station in stations:
                        for variable in _VARIABLES:
                            if variable not in dataset:
                                continue
                            aligned = align_station_to_model(
                                dataset,
                                crs=crs,
                                station_latitude=station.expected_latitude,
                                station_longitude=station.expected_longitude,
                                canonical_variable_id=variable,
                                target_horizon_hours=(horizon,),
                                # Target horizons are relative to the product
                                # target reference, not the selected (possibly
                                # older) source cycle reference.
                                target_reference_time=np.datetime64(
                                    target_reference_time.replace(tzinfo=None), "ns"
                                ),
                            )
                            result = aligned.get(horizon)
                            if result is None:
                                continue
                            by_station[f"{station.station_id}|{variable}"] = result.value
                    model_values[str(horizon)] = by_station
                values[model] = model_values
                lineage[model] = model_lineage
            return {
                "schema_version": "aligned-station-guidance.v1",
                "models": [m for m in _MODELS if m in models],
                "shape": [len(models), len(horizons), len(stations)],
                "values": values,
                "lineage": lineage,
            }

        result = artifact_service.execute_transformation(
            transformation,
            transform=_transform,
            serializer=_JSON,
            input_loader=_NETCDF.deserialize,
            input_validator=validate_canonical_guidance_v2,
            output_validator=lambda value: (
                None
                if value["shape"][1:] == [len(horizons), len(stations)]
                else (_ for _ in ()).throw(ValueError("wrong aligned shape"))
            ),
        )
        return AlignmentArtifacts(aligned_guidance=result.output)

    # ------------------------------------------------------------------
    # evaluate
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> AvailabilityArtifacts:
        self._require_persisted_identity(persisted)
        cycle = artifact_service.execute_raw_transformation(
            self._transformation(
                request,
                activity="select-model-cycles",
                inputs=(("aligned", alignment.aligned_guidance.artifact_id),),
                output_role="selection",
                artifact_type="model-cycle-selection",
                schema="model-cycle-selection.v1",
            ),
            transform=lambda aligned: {
                "schema_version": "model-cycle-selection.v1",
                "selected_models": aligned["models"],
                "rejected_models": [m for m in _MODELS if m not in aligned["models"]],
                "cutoff": persisted.run_spec.information_cutoff.isoformat(),
            },
            serializer=_JSON,
            input_loader=_JSON.deserialize,
            output_validator=lambda _: None,
        )
        report = artifact_service.execute_raw_transformation(
            self._transformation(
                request,
                activity="evaluate-availability",
                inputs=(("aligned", alignment.aligned_guidance.artifact_id),),
                output_role="availability",
                artifact_type="model-availability-report",
                schema="model-availability-report.v1",
            ),
            transform=lambda aligned: self._available_payload(aligned, persisted),
            serializer=_JSON,
            input_loader=_JSON.deserialize,
            output_validator=lambda value: (
                None
                if value["run_state"] in {"complete", "degraded", "invalid"}
                else (_ for _ in ()).throw(ValueError("invalid run_state"))
            ),
        )
        return AvailabilityArtifacts(cycle_selection=cycle.output, report=report.output)

    def _screen_model_guidance(
        self,
        *,
        aligned: dict[str, Any],
        model: str,
        stations: Any,
        horizons: Any,
    ) -> tuple[dict[str, Any] | None, dict[tuple[str, int], dict[str, Any]]]:
        """Screen one model's aligned guidance for source-level
        disqualification, at the smallest scientifically valid scope.

        Returns ``(cycle_exclusion, point_exclusions)``:

        * ``cycle_exclusion`` is a whole-cycle rejection record, or
          ``None``. Coverage failures -- a horizon this model has no
          aligned values for, an aligned point missing a required
          field -- and a non-finite/invalid source value stay
          whole-cycle: they are evidence that the cycle's geometry,
          time identity, or quality is not trustworthy as a unit.
        * ``point_exclusions`` maps ``(station_id, horizon)`` to a
          record for each point where this model's *coupled wind/gust
          tuple* is invalid because the source gust falls below its own
          sustained speed by more than the floor tolerance. That is a
          source-product cross-field inconsistency at one point, not
          evidence that the model's temperature, dew point, PoP or QPF
          are corrupt (Codex review ``t_1564b30c``), so U/V and gust
          are rejected together *there* and everything else is retained.

        Nothing is ever clamped or repaired: the source values are
        recorded verbatim on the exclusion record and the point simply
        loses this contributor.
        """
        point_exclusions: dict[tuple[str, int], dict[str, Any]] = {}
        for horizon in horizons:
            by_point = aligned["values"].get(model, {}).get(str(horizon))
            if by_point is None:
                return (
                    {
                        "model": model,
                        "scope": "model-cycle",
                        "reason": "missing_aligned_horizon",
                        "detail": f"no aligned values for horizon {horizon}",
                        "target_horizon": horizon,
                        "station": None,
                    },
                    {},
                )
            for station in stations:
                station_id = str(station.station_id)
                keys = {name: f"{station_id}|{name}" for name in _COUPLED_WIND_GUST_VARIABLES}
                missing = [name for name, key in keys.items() if key not in by_point]
                if missing:
                    return (
                        {
                            "model": model,
                            "scope": "model-cycle",
                            "reason": "missing_aligned_point",
                            "detail": (
                                f"aligned point is missing {sorted(missing)!r} at "
                                f"{station_id} horizon {horizon}"
                            ),
                            "target_horizon": horizon,
                            "station": station_id,
                        },
                        {},
                    )
                # A non-finite or non-numeric aligned value is not a
                # source-product tension between two valid fields; it
                # means normalization's own finiteness contract did not
                # hold for this cycle, so the cycle is rejected as a
                # unit rather than at one point.
                invalid = [
                    name
                    for name, key in keys.items()
                    if not isinstance(by_point[key], (int, float))
                    or isinstance(by_point[key], bool)
                    or not math.isfinite(by_point[key])
                ]
                if invalid:
                    return (
                        {
                            "model": model,
                            "scope": "model-cycle",
                            "reason": "invalid_source_value",
                            "detail": (
                                f"aligned {sorted(invalid)!r} is not a finite number at "
                                f"{station_id} horizon {horizon}"
                            ),
                            "target_horizon": horizon,
                            "station": station_id,
                        },
                        {},
                    )
                gust = by_point[keys["wind_gust_10m"]]
                sustained = math.hypot(
                    by_point[keys["eastward_wind_10m"]],
                    by_point[keys["northward_wind_10m"]],
                )
                try:
                    validate_source_gust(gust_m_s=gust, sustained_speed_m_s=sustained)
                except GustDisqualificationError:
                    point_exclusions[(station_id, horizon)] = {
                        "model": model,
                        "scope": "coupled-wind-gust-point",
                        "reason": "source_gust_inconsistency",
                        "detail": (
                            f"source gust {gust!r} m/s is below sustained speed "
                            f"{sustained!r} m/s beyond the floor tolerance "
                            f"{SOURCE_GUST_SHORTFALL_FLOOR_TOLERANCE_M_S!r} m/s; the coupled "
                            "wind/gust tuple is rejected at this point and the model's "
                            "independent variables are retained"
                        ),
                        "station": station_id,
                        "target_horizon": horizon,
                        "affected_variable_ids": list(_COUPLED_WIND_GUST_VARIABLES),
                        "source_gust_m_s": gust,
                        "source_sustained_speed_m_s": sustained,
                        "shortfall_m_s": sustained - gust,
                        "shortfall_floor_tolerance_m_s": (
                            SOURCE_GUST_SHORTFALL_FLOOR_TOLERANCE_M_S
                        ),
                    }
        return None, point_exclusions

    def _available_payload(
        self, aligned: dict[str, Any], persisted: Phase2PersistedRun
    ) -> dict[str, Any]:
        stations = persisted.stations
        horizons = persisted.target_horizons
        blend = persisted.blend_configuration
        eligible_models: set[str] = set(aligned["models"])
        # Section 4.1/4.3 stays fail-closed, but the *scope* of a
        # rejection is now the smallest scientifically coupled unit
        # (Codex review t_1564b30c). A localized gust-below-sustained
        # inconsistency rejects that model's U/V/gust tuple at the
        # affected station/horizon only; coverage/geometry/quality
        # failures still reject the whole cycle. Either way the cause,
        # the source values, and the tolerance are persisted so an
        # excluded contributor can never vanish from the product
        # without auditable provenance (AGENTS.md: "Model guidance
        # provenance must be retained").
        cycle_exclusions: list[dict[str, Any]] = []
        point_exclusions: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for model in tuple(eligible_models):
            cycle_exclusion, model_points = self._screen_model_guidance(
                aligned=aligned, model=model, stations=stations, horizons=horizons
            )
            if cycle_exclusion is not None:
                eligible_models.remove(model)
                cycle_exclusions.append(cycle_exclusion)
                continue
            for key, record in model_points.items():
                point_exclusions.setdefault(key, []).append(record)
        eligible = frozenset(eligible_models)
        entries = []
        availability_objects = []
        contributing: set[str] = set()
        for variable in _VARIABLES:
            coupled = variable in _COUPLED_WIND_GUST_VARIABLES
            for station in stations:
                station_id = str(station.station_id)
                for horizon in horizons:
                    excluded_here = (
                        [
                            record
                            for record in point_exclusions.get((station_id, horizon), ())
                            if record["model"] in eligible
                        ]
                        if coupled
                        else []
                    )
                    usable = eligible - {record["model"] for record in excluded_here}
                    if variable == "probability_of_precipitation_1h":
                        value = evaluate_pop_availability(
                            location=station_id,
                            target_horizon=horizon,
                            nbm_available="NBM" in usable,
                        )
                    else:
                        table = (
                            blend.qpf_table
                            if variable == "liquid_equivalent_precipitation_amount_1h"
                            else blend.scalar_vector_table
                        )
                        value = evaluate_scalar_vector_availability(
                            table=table,
                            variable_id=variable,
                            location=station_id,
                            target_horizon=horizon,
                            available_models=usable,
                        )
                    availability_objects.append(value)
                    contributing.update(value.available_models)
                    entries.append(
                        {
                            "variable": variable,
                            "station": station_id,
                            "horizon": horizon,
                            "state": value.state,
                            "models": list(value.available_models),
                            "excluded": [
                                {
                                    key: record[key]
                                    for key in (
                                        "model",
                                        "scope",
                                        "reason",
                                        "detail",
                                        "affected_variable_ids",
                                        "source_gust_m_s",
                                        "source_sustained_speed_m_s",
                                        "shortfall_m_s",
                                        "shortfall_floor_tolerance_m_s",
                                    )
                                }
                                for record in sorted(
                                    excluded_here, key=lambda item: _MODELS.index(item["model"])
                                )
                            ],
                            "row_id": (
                                str(value.fallback_row.row_id) if value.fallback_row else None
                            ),
                            "weights": (
                                list(value.fallback_row.weights) if value.fallback_row else None
                            ),
                        }
                    )
        summary = evaluate_run_state(tuple(availability_objects))
        return {
            "schema_version": "model-availability-report.v1",
            "run_state": summary.state,
            # ``models`` is the truthful run-wide contributor label:
            # every model that contributes to at least one output. A
            # model excluded at some points but retained at others is
            # listed here and its per-point exclusions are on the
            # entries themselves.
            "models": [m for m in _MODELS if m in contributing],
            "eligible_models": [m for m in _MODELS if m in eligible],
            "excluded_models": sorted(
                cycle_exclusions,
                key=lambda item: _MODELS.index(item["model"]),
            ),
            "point_exclusions": sorted(
                (
                    record
                    for records in point_exclusions.values()
                    for record in records
                    if record["model"] in eligible
                ),
                key=lambda item: (
                    _MODELS.index(item["model"]),
                    item["station"],
                    item["target_horizon"],
                ),
            ),
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

    def _generate(
        self,
        request: Phase2Request,
        run: RunManifest,
        alignment: AlignmentArtifacts,
        availability: AvailabilityArtifacts,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> AtomicForecastArtifacts:
        self._require_persisted_identity(persisted)
        common_inputs = (
            ("aligned", alignment.aligned_guidance.artifact_id),
            ("availability", availability.report.artifact_id),
        )
        forecast_request = self._transformation(
            request,
            activity="generate-atomic-blend",
            inputs=common_inputs,
            output_role="forecast",
            artifact_type="uncorrected-blend-forecast",
            schema="uncorrected-blend-forecast.v1",
            media_type="application/x-netcdf",
        )
        contribution_request = self._transformation(
            request,
            activity="generate-atomic-blend",
            inputs=common_inputs,
            output_role="contributions",
            artifact_type="blend-contribution-manifest",
            schema="blend-contribution-manifest.v1",
        )
        result = artifact_service.execute_atomic_raw_pair(
            forecast_request,
            contribution_request,
            transform=lambda aligned, available: self._forecast_pair(
                aligned, available, request, persisted
            ),
            serializers=(_NETCDF, _JSON),
            input_loader=_JSON.deserialize,
            output_validators=(
                lambda _: None,
                lambda value: _validate_pydantic_model(BlendContributionManifest, value),
            ),
        )
        return AtomicForecastArtifacts(
            activity=result.activity,
            uncorrected_blend=result.outputs[0],
            contribution_manifest=result.outputs[1],
        )

    def _forecast_pair(
        self,
        aligned: dict[str, Any],
        availability: dict[str, Any],
        request: Phase2Request,
        persisted: Phase2PersistedRun,
    ) -> tuple[xr.Dataset, dict[str, Any]]:
        stations = persisted.station_ids
        horizons = persisted.target_horizons
        target_reference_time = persisted.run_spec.target_reference_time
        values: dict[tuple[str, str, int], float] = {}
        states: dict[tuple[str, str, int], str] = {}
        rows: list[BlendContributionRow] = []
        entries = {
            (entry["variable"], entry["station"], entry["horizon"]): entry
            for entry in availability["entries"]
        }
        for variable in _VARIABLES:
            for station in stations:
                for horizon in horizons:
                    key = (variable, station, horizon)
                    entry = entries[key]
                    states[key] = entry["state"]
                    contributors: list[ContributorRecord] = []
                    # Every model that was eligible for this row but was
                    # rejected here, with the cause and the verbatim
                    # source values. This is the row-level half of the
                    # exclusion provenance; the availability report
                    # aggregates the same records run-wide.
                    excluded = tuple(
                        ExcludedContributorRecord(
                            model=record["model"],
                            reason=record["reason"],
                            scope=record["scope"],
                            detail=record["detail"],
                            affected_variable_ids=tuple(record["affected_variable_ids"]),
                            source_gust_m_s=record["source_gust_m_s"],
                            source_sustained_speed_m_s=record["source_sustained_speed_m_s"],
                            shortfall_m_s=record["shortfall_m_s"],
                            shortfall_floor_tolerance_m_s=record["shortfall_floor_tolerance_m_s"],
                        )
                        for record in entry.get("excluded", ())
                    )
                    if entry["state"] == "unavailable":
                        result = 0.0
                    else:
                        weights = (
                            (0.0, 1.0, 0.0)
                            if variable == "probability_of_precipitation_1h"
                            else tuple(entry["weights"])
                        )
                        scalar = []
                        for model, weight in zip(_MODELS, weights, strict=True):
                            if weight == 0:
                                continue
                            aligned_value = aligned["values"][model][str(horizon)][
                                f"{station}|{variable}"
                            ]
                            scalar.append(
                                Contribution(model=model, value=aligned_value, weight=weight)
                            )
                            contributors.append(
                                # Preserve the selected older cycle and its
                                # source lead, rather than relabeling it as
                                # target cycle/horizon lineage.
                                ContributorRecord(
                                    model=model,  # type: ignore[arg-type]
                                    source_cycle_reference_time=aligned["lineage"][model][
                                        str(horizon)
                                    ]["source_cycle_reference_time"],
                                    source_forecast_hour=aligned["lineage"][model][str(horizon)][
                                        "source_forecast_hour"
                                    ],
                                    artifact_id=aligned["lineage"][model][str(horizon)][
                                        "artifact_id"
                                    ],
                                    aligned_value=aligned_value,
                                    configured_weight=weight,
                                    weighted_contribution=aligned_value * weight,
                                )
                            )
                        if variable in ("eastward_wind_10m", "northward_wind_10m"):
                            vector = blend_vector(
                                eastward_contributions=tuple(
                                    Contribution(
                                        model=c.model,
                                        value=aligned["values"][c.model][str(horizon)][
                                            f"{station}|eastward_wind_10m"
                                        ],
                                        weight=c.configured_weight,
                                    )
                                    for c in contributors
                                ),
                                northward_contributions=tuple(
                                    Contribution(
                                        model=c.model,
                                        value=aligned["values"][c.model][str(horizon)][
                                            f"{station}|northward_wind_10m"
                                        ],
                                        weight=c.configured_weight,
                                    )
                                    for c in contributors
                                ),
                            )
                            result = (
                                vector.eastward_m_s
                                if variable == "eastward_wind_10m"
                                else vector.northward_m_s
                            )
                        elif variable == "wind_gust_10m":
                            eastward = tuple(
                                Contribution(
                                    model=c.model,
                                    value=aligned["values"][c.model][str(horizon)][
                                        f"{station}|eastward_wind_10m"
                                    ],
                                    weight=c.configured_weight,
                                )
                                for c in contributors
                            )
                            northward = tuple(
                                Contribution(
                                    model=c.model,
                                    value=aligned["values"][c.model][str(horizon)][
                                        f"{station}|northward_wind_10m"
                                    ],
                                    weight=c.configured_weight,
                                )
                                for c in contributors
                            )
                            vector = blend_vector(
                                eastward_contributions=eastward,
                                northward_contributions=northward,
                            )
                            scalar = [
                                Contribution(
                                    model=c.model,
                                    value=validate_source_gust(
                                        gust_m_s=c.value,
                                        sustained_speed_m_s=math.hypot(u.value, v.value),
                                    ).validated_gust_m_s,
                                    weight=c.weight,
                                )
                                for c, u, v in zip(scalar, eastward, northward, strict=True)
                            ]
                            gust = blend_gust(
                                contributions=tuple(scalar),
                                blended_sustained_speed_m_s=vector.speed_m_s,
                            )
                            result = gust.blended_gust_m_s
                        elif variable == "liquid_equivalent_precipitation_amount_1h":
                            result = blend_qpf(tuple(scalar))
                        elif variable == "probability_of_precipitation_1h":
                            result = pop_passthrough(scalar[0].value)
                        else:
                            result = blend_scalar(tuple(scalar)).blended_value
                        values[key] = result
                    rows.append(
                        BlendContributionRow(
                            variable_id=variable,
                            location=StationId(station),
                            target_horizon=horizon,
                            target_valid_time=(
                                target_reference_time + timedelta(hours=horizon)
                            ).isoformat(),
                            operator_id=f"phase2.{variable}.v1",
                            availability_state=entry["state"],
                            fallback_row_id=(entry["row_id"] and entry["row_id"]),
                            contributors=tuple(contributors),
                            excluded_contributors=excluded,
                            unrounded_sum=result,
                            serialized_output=result,
                        )
                    )
        dataset = assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64(target_reference_time.replace(tzinfo=None), "ns"),
            forecast_issue_time=np.datetime64(
                persisted.run_spec.forecast_issue_time.replace(tzinfo=None), "ns"
            ),
            uncorrected_blend_artifact_id="pending-atomic-output",
            identity_correction_artifact_id="not-yet-applied",
        )
        dataset.attrs["schema_version"] = "uncorrected-blend-forecast.v1"
        digests = persisted.run_spec.request_identity
        manifest = BlendContributionManifest(
            rows=tuple(rows),
            expected_variable_ids=_VARIABLES,
            expected_locations=tuple(StationId(s) for s in stations),
            expected_target_horizons=horizons,
            configuration_digest=str(digests.configuration_digest),
            code_revision=digests.code_revision,
            environment_digest=str(digests.environment_digest),
            lockfile_digest=str(digests.lockfile_digest),
        )
        return dataset, manifest.model_dump(mode="json")

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    def _apply(
        self,
        request: Phase2Request,
        run: RunManifest,
        forecast: AtomicForecastArtifacts,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> CorrectedForecastArtifacts:
        self._require_persisted_identity(persisted)
        correction = artifact_service.execute_raw_transformation(
            self._transformation(
                request,
                activity="create-identity-correction",
                inputs=(("forecast", forecast.uncorrected_blend.artifact_id),),
                output_role="correction",
                artifact_type="identity-correction",
                schema="identity-bias-correction.v1",
            ),
            transform=lambda _: IdentityBiasCorrection().model_dump(mode="json"),
            serializer=_JSON,
            input_loader=lambda payload: payload,
            output_validator=lambda value: _validate_pydantic_model(IdentityBiasCorrection, value),
        )
        baseline_request = self._transformation(
            request,
            activity="apply-identity-correction",
            inputs=(
                ("forecast", forecast.uncorrected_blend.artifact_id),
                ("correction", correction.output.artifact_id),
            ),
            output_role="baseline",
            artifact_type="baseline-forecast",
            schema="baseline-forecast.v2",
            media_type="application/x-netcdf",
        )

        def make_baseline(bound: Mapping[str, Any]) -> xr.Dataset:
            dataset: xr.Dataset = bound["forecast"].copy(deep=True)
            IdentityBiasCorrection.model_validate(bound["correction"])
            dataset.attrs.update(
                schema_version="baseline-forecast.v2",
                uncorrected_blend_artifact_id=str(forecast.uncorrected_blend.artifact_id),
                identity_correction_artifact_id=str(correction.output.artifact_id),
            )
            return dataset

        baseline = artifact_service.execute_role_bound_transformation(
            baseline_request,
            transform=make_baseline,
            serializer=_NETCDF,
            input_bindings={
                "forecast": InputBinding(_NETCDF.deserialize, lambda _: None),
                "correction": InputBinding(
                    _JSON.deserialize,
                    lambda value: _validate_pydantic_model(IdentityBiasCorrection, value),
                ),
            },
            output_validator=validate_baseline_forecast_v2,
        )
        return CorrectedForecastArtifacts(correction=correction.output, baseline=baseline.output)

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------

    def _acquire_and_normalize(
        self,
        request: Phase2Request,
        run: RunManifest,
        station_snapshot: ArtifactManifest,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
        recorded_responses: tuple[ArtifactManifest, ...] | None,
    ) -> ObservationArtifacts:
        """Normalize the run's METAR observations.

        On a first run ``recorded_responses`` is ``None``, so the
        provider response is fetched once and registered as a source
        artifact. On replay the coordinator supplies exactly the
        artifacts that first run registered, and this stage reuses their
        stored bytes -- ``acquire_metar_batch`` is never called, so
        replay performs zero AviationWeather requests (Codex review
        ``t_652b155e``).
        """
        self._require_persisted_identity(persisted)
        stations = persisted.stations
        query_date = persisted.run_spec.verification_cutoff
        if recorded_responses is None:
            fetched = acquire_metar_batch(
                persisted.aviationweather,
                transport=self._aviationweather_transport,
                clock=self._clock,
                sleeper=self._sleeper,
                station_ids=tuple(s.provider_icao_id for s in stations),
                query_date=query_date,
                rate_limiter=self._rate_limiter(persisted),
            )
            digests = persisted.run_spec.request_identity
            raw = artifact_service.register_source(
                SourceRegistrationRequest(
                    source_authority="aviationweather.gov",
                    source_locator=f"phase2-metar://{persisted.run_spec.run_id}",
                    source_revision="phase2.v1",
                    artifact_type="aviationweather-metar-response",
                    artifact_schema_version="aviationweather-metar-response.v1",
                    media_type="application/json",
                    created_at=self._clock.now(),
                    availability=Availability(
                        available_at=fetched.completed_at,
                        authority="aviationweather.gov",
                        method="get",
                    ),
                    configuration_snapshot_id=digests.configuration_snapshot_id,
                    configuration_digest=digests.configuration_digest,
                    code_revision=digests.code_revision,
                    environment_digest=digests.environment_digest,
                    run_id=persisted.run_spec.run_id,
                ),
                fetched.payload,
            )
            response_payload = fetched.payload
        else:
            if len(recorded_responses) != 1:
                raise ValueError(
                    "Phase 2 records exactly one AviationWeather response per run; replay "
                    f"supplied {len(recorded_responses)}"
                )
            # Repository-authoritative manifest + integrity-verified
            # bytes: replay never trusts the caller's copy.
            raw, response_payload = artifact_service.load_verified_payload(
                recorded_responses[0].artifact_id
            )

        stations_by_icao = {s.provider_icao_id: s for s in stations}
        policy = persisted.observation_normalization_policy
        query_window_start = persisted.run_spec.target_reference_time

        def _normalize(*_raw: Any) -> dict[str, Any]:
            raw_records = parse_raw_metar_response(response_payload)
            rows: list[dict[str, Any]] = []
            for index, record in enumerate(raw_records):
                station = stations_by_icao.get(record.icao_id)
                if station is None:
                    continue
                # AviationWeather answers a *date* query and returns
                # whatever recent records it holds for those stations,
                # including ones just before this run's window (a routine
                # 17:55Z METAR for an 18:00Z reference). Selecting the
                # records that fall in the window is this stage's job;
                # ``normalize_metar_record_v2`` keeps its fail-closed
                # contract and still rejects an out-of-window record if
                # one is ever handed to it. Skipping here is the same
                # treatment already given to a record for a station this
                # run does not carry -- not a relaxation of the window.
                if record.obs_time < query_window_start or record.obs_time > query_date:
                    continue
                station_record = _station_record(station)
                normalized = normalize_metar_record_v2(
                    raw=_as_v2_record(record),
                    station=station_record,
                    station_id=station.station_id,
                    policy=policy,
                    raw_artifact_id=raw.artifact_id,
                    raw_record_index=index,
                    station_snapshot_artifact_id=station_snapshot.artifact_id,
                    ingested_at=self._clock.now(),
                    query_window_start=query_window_start,
                    query_window_end=query_date,
                )
                rows.append(normalized.model_dump(mode="json"))
            return {"rows": rows}

        result = artifact_service.execute_raw_transformation(
            self._transformation(
                request,
                activity="normalize-metar-v2",
                inputs=(("raw", raw.artifact_id), ("stations", station_snapshot.artifact_id)),
                output_role="observations",
                artifact_type="normalized-metar-observations",
                schema="metar-observations.v2",
            ),
            transform=_normalize,
            serializer=_JSON,
            # Raw AviationWeather responses are provider JSON arrays, while
            # the station snapshot is an object; normalization reads the
            # already-verified fetched bytes and does not need either decoded.
            input_loader=lambda payload: payload,
            output_validator=lambda value: _validate_normalized_observations_v2(value),
        )
        return ObservationArtifacts(responses=(raw,), normalized=result.output)

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------

    def _match(
        self,
        request: Phase2Request,
        run: RunManifest,
        baseline: ArtifactManifest,
        observations: ObservationArtifacts,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> MatchingArtifacts:
        self._require_persisted_identity(persisted)
        matched_request = self._transformation(
            request,
            activity="match-phase2-observations",
            inputs=(
                ("baseline", baseline.artifact_id),
                ("observations", observations.normalized.artifact_id),
            ),
            output_role="pairs",
            artifact_type="matched-pairs",
            schema="matched-pairs.v2",
        )
        stations = persisted.station_ids
        horizons = persisted.target_horizons
        matching_policy = persisted.matching_policy
        verification_cutoff = persisted.run_spec.verification_cutoff

        def make_pairs(bound: Mapping[str, Any]) -> dict[str, Any]:
            obs = [
                _model_from_json_value(NormalizedObservationV2, row)
                for row in bound["observations"]["rows"]
            ]
            pair_rows = match_baseline_to_observations_v2(
                baseline=bound["baseline"],
                station_ids=stations,
                target_horizons=horizons,
                observations=obs,
                matching_policy=matching_policy,
                verification_cutoff=verification_cutoff,
                baseline_artifact_id=baseline.artifact_id,
                observations_artifact_id=observations.normalized.artifact_id,
            )
            return {"rows": [row.model_dump(mode="json") for row in pair_rows]}

        result = artifact_service.execute_role_bound_transformation(
            matched_request,
            transform=make_pairs,
            serializer=_JSON,
            input_bindings={
                "baseline": InputBinding(_NETCDF.deserialize, validate_baseline_forecast_v2),
                "observations": InputBinding(_JSON.deserialize, lambda _: None),
            },
            output_validator=lambda value: validate_matched_pairs_v2(
                [_model_from_json_value(MatchedPairRowV2, row) for row in value["rows"]]
            ),
        )
        return MatchingArtifacts(matched_pairs=result.output)

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    def _calculate(
        self,
        request: Phase2Request,
        run: RunManifest,
        pairs: MatchingArtifacts,
        *,
        artifact_service: ArtifactService,
        persisted: Phase2PersistedRun,
    ) -> VerificationArtifacts:
        self._require_persisted_identity(persisted)
        metric_set = persisted.metric_set

        def _calc(value: dict[str, Any]) -> dict[str, Any]:
            rows = [_model_from_json_value(MatchedPairRowV2, row) for row in value["rows"]]
            report = compute_verification_report_v2(
                rows,
                metric_set_id=str(metric_set.metric_set_id),
                baseline_artifact_id=ArtifactId(value["rows"][0]["baseline_artifact_id"]),
                matched_pairs_artifact_id=pairs.matched_pairs.artifact_id,
            )
            return report.model_dump(mode="json")

        result = artifact_service.execute_raw_transformation(
            self._transformation(
                request,
                activity="verify-phase2",
                inputs=(("pairs", pairs.matched_pairs.artifact_id),),
                output_role="report",
                artifact_type="verification-report",
                schema="verification-report.v2",
            ),
            transform=_calc,
            serializer=_JSON,
            input_loader=_JSON.deserialize,
            output_validator=lambda value: validate_verification_report_v2(
                _model_from_json_value(VerificationReportV2, value)
            ),
        )
        return VerificationArtifacts(report=result.output)

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def _transformation(
        self,
        request: Phase2Request,
        *,
        activity: str,
        inputs: tuple[tuple[str, ArtifactId], ...],
        output_role: str,
        artifact_type: str,
        schema: str,
        media_type: str = "application/json",
        parameters: dict[str, object] | None = None,
    ) -> TransformationRequest:
        return TransformationRequest(
            activity_type=activity,
            activity_version="1.0.0",
            inputs=tuple(
                TransformationInputRef(role=role, artifact_id=artifact_id)
                for role, artifact_id in inputs
            ),
            output_role=output_role,
            output_artifact_type=artifact_type,
            output_artifact_schema_version=schema,
            output_media_type=media_type,
            parameters=parameters or {},
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
            run_id=request.run_id,
        )


def _crs_from_dataset(dataset: xr.Dataset) -> pyproj.CRS:
    wkt = dataset.attrs.get("crs_wkt2")
    if wkt is not None:
        return pyproj.CRS.from_wkt(str(wkt))
    return pyproj.CRS.from_epsg(4326)


def _station_record(station: Any) -> Any:
    from mesoforge.catalog.stations import StationRecord

    return StationRecord(
        station_id=station.station_id,
        provider_icao_id=station.provider_icao_id,
        latitude=station.expected_latitude,
        longitude=station.expected_longitude,
        elevation_m=station.expected_elevation_m,
        site_name=station.site_name,
        site_types=station.provider_site_types,
    )


def _as_v2_record(record: Any) -> RawMetarRecordV2:
    return RawMetarRecordV2.model_validate(record.model_dump(mode="python"), strict=True)


def _model_from_json_value(model: type, value: dict[str, object]) -> Any:
    return model.model_validate_json(_JSON.serialize(value))  # type: ignore[attr-defined]


def build_phase2_production_adapters(
    *,
    artifact_service: ArtifactService,
    configuration: Phase2Configuration,
    hrrr_transport: HttpTransport,
    nbm_transport: HttpTransport,
    gfs_transport: HttpTransport,
    aviationweather_transport: HttpTransport,
    clock: Clock,
    sleeper: Sleeper,
    aviationweather_rate_limiter: RequestRateLimiter | None = None,
) -> Phase2ProductionAdapters:
    """Compose the real Phase2ProviderSubport/Phase2ScienceSubport
    implementations (this module) into a production
    ``Phase2ProductionAdapters`` -- the single wiring point a caller
    (script, cron job, service entrypoint) uses to run Phase 2 end to
    end against real transports rather than injected test doubles."""
    provider = Phase2ProductionProvider(
        configuration=configuration,
        hrrr_transport=hrrr_transport,
        nbm_transport=nbm_transport,
        gfs_transport=gfs_transport,
        clock=clock,
        sleeper=sleeper,
    )
    science = Phase2ProductionScience(
        configuration=configuration,
        aviationweather_transport=aviationweather_transport,
        clock=clock,
        sleeper=sleeper,
        aviationweather_rate_limiter=aviationweather_rate_limiter,
    )
    return Phase2ProductionAdapters(
        artifact_service=artifact_service,
        providers=provider,
        science=science.operations(),
    )


class UnavailableHttpTransport:
    """An ``HttpTransport`` whose every request raises.

    Replay wiring injects this so a network call is not merely
    discouraged but impossible: any residual transport use surfaces as a
    loud failure instead of a silent refetch.
    """

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse:
        raise Phase2ReplayIdentityError(
            f"replay must perform no network access, but a GET was attempted for {url!r}"
        )

    def head(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> HttpResponse:
        raise Phase2ReplayIdentityError(
            f"replay must perform no network access, but a HEAD was attempted for {url!r}"
        )


def build_phase2_replay_adapters(
    *,
    artifact_service: ArtifactService,
    clock: Clock,
    sleeper: Sleeper,
) -> Phase2ProductionAdapters:
    """Compose adapters that can only replay from persisted artifacts.

    No ``Phase2Configuration`` is supplied at all: every stage reads the
    run's own persisted run spec and station snapshot. No provider and
    no usable transport is supplied either, so discovery, acquisition,
    and observation fetching are structurally impossible rather than
    merely unused (Codex review ``t_652b155e``). This is the wiring an
    operator uses to reproduce a historical run in a fresh process.
    """
    return Phase2ProductionAdapters(
        artifact_service=artifact_service,
        providers=Phase2UnavailableProvider(),
        science=Phase2ProductionScience(
            aviationweather_transport=UnavailableHttpTransport(),
            clock=clock,
            sleeper=sleeper,
        ).operations(),
    )
