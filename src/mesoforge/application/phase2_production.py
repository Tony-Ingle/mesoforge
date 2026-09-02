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
from mesoforge.catalog.configuration import Phase2Configuration
from mesoforge.common.identifiers import ArtifactId, Digest, RunId, StationId
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.forecasts import validate_baseline_forecast_v2
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
    IdentityBiasCorrection,
)
from mesoforge.forecasting.gust_blend import (
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
    acquire_gfs_lead,
    acquire_hrrr_phase2_lead,
    acquire_nbm_lead,
)
from mesoforge.guidance.canonical_v2 import validate_canonical_guidance_v2
from mesoforge.guidance.cycle_selection import (
    CandidateCycle,
    generate_candidate_reference_times,
    select_model_cycle,
)
from mesoforge.guidance.interfaces import Clock, HttpTransport, Sleeper
from mesoforge.guidance.normalization_v2 import (
    normalize_gfs_cycle,
    normalize_hrrr_phase2_cycle,
    normalize_nbm_cycle,
)
from mesoforge.guidance.precipitation import is_bucket_reset_hour
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


def _valid_map(value: object) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"expected dict, got {type(value)!r}")


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


class Phase2NoAvailableCycleError(Exception):
    """Raised when discovery cannot find a single approved cycle for
    any of HRRR/NBM/GFS -- Phase 2's ``normalized guidance must not be
    empty`` invariant is enforced by the coordinator itself, so this
    is only raised for a genuinely unrecoverable discovery failure."""


class GuidanceNormalizationProductionError(Exception):
    """Raised when the production science stage cannot normalize an
    acquired guidance cycle, e.g. a required adjacent-lead payload for
    same-bucket precipitation differencing was not acquired."""


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
        max_age_hours: float,
        completion_deadline_minutes: float,
        max_source_lead_hours: int,
        include_previous_apcp_dependency: bool,
        acquire_lead: Any,
    ) -> tuple[Phase2LeadAcquisition, ...] | None:
        for candidate in candidates:
            age_hours = (target - candidate).total_seconds() / 3600.0
            if age_hours > max_age_hours or age_hours < 0:
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
            completed_at = max(a.grib_completed_at for a in acquisitions)
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

        run_spec_payload = {
            "schema_version": "phase2-run-spec.v1",
            "run_id": str(request.run_id),
            "target_reference_time": request.target_reference_time.isoformat(),
            "selection_digest": str(discovery.selection_digest),
        }
        run_spec = self._register_source(
            artifact_service,
            request,
            locator=f"phase2-run-spec://{request.run_id}",
            artifact_type="phase2-run-spec",
            schema="phase2-run-spec.v1",
            payload=_JSON.serialize(run_spec_payload),
        )

        station_payload = {
            "schema_version": "station-catalog-snapshot.v1",
            "stations": list(_STATIONS),
        }
        station_snapshot = self._register_source(
            artifact_service,
            request,
            locator="phase2-station-catalog://grasston",
            artifact_type="station-catalog-snapshot",
            schema="station-catalog-snapshot.v1",
            payload=_JSON.serialize(station_payload),
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
                    available_at=acquisition.index_completed_at,
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
                    record_identity = (
                        f"m{message.row.message_number}-o{message.row.byte_offset}-n{ordinal}"
                    )
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
                        available_at=acquisition.grib_completed_at,
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
        return Phase2SelectedInputs(
            run_spec=run_spec, source_roots=tuple(roots), station_snapshot=station_snapshot
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

    def acquisitions_for(self, run_id: RunId) -> dict[str, tuple[Phase2LeadAcquisition, ...]]:
        """Expose retained per-lead acquisitions for the science
        stages (grid/lineage identity, GFS previous-bucket chaining)."""
        return self._acquisitions_by_run.get(str(run_id), {})


class Phase2ProductionScience:
    """Concrete science stages backing ``Phase2ArtifactOperations``:
    real per-model normalization (``guidance.normalization_v2``),
    station alignment, availability, atomic blend generation, identity
    correction, METAR acquisition/normalization, matching, and
    verification -- composed from the pure domain functions plus the
    production ``ArtifactService``. Never redefines the coordinator's
    ordering; only supplies the eight injected stage callables."""

    def __init__(
        self,
        *,
        configuration: Phase2Configuration,
        provider: Phase2ProductionProvider,
        aviationweather_transport: HttpTransport,
        clock: Clock,
        sleeper: Sleeper,
        aviationweather_rate_limiter: RequestRateLimiter | None = None,
    ) -> None:
        self._configuration = configuration
        self._provider = provider
        self._aviationweather_transport = aviationweather_transport
        self._clock = clock
        self._sleeper = sleeper
        self._aviationweather_rate_limiter = (
            aviationweather_rate_limiter
            if aviationweather_rate_limiter is not None
            else RequestRateLimiter(
                min_interval_seconds=configuration.aviationweather.min_request_interval_seconds
            )
        )

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
    ) -> NormalizedGuidanceArtifacts:
        acquisitions_by_model = self._provider.acquisitions_for(request.run_id)
        artifacts: list[ArtifactManifest] = []
        for model in _MODELS:
            model_roots = [
                r
                for r in inputs.source_roots
                if r.model == model and r.field_role.startswith("message:")
            ]
            if not model_roots:
                continue
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
            reference_time = self._model_reference_time(acquisitions_by_model, model)
            cycle_age_hours = int(
                (request.target_reference_time - reference_time).total_seconds() // 3600
            )
            target_source_leads = tuple(horizon + cycle_age_hours for horizon in _HORIZONS)
            lineage_artifact: ArtifactManifest | None = None
            if model == "GFS":

                def _lineage_transform(
                    *raw: bytes,
                    refs: tuple[tuple[str, ArtifactId], ...] = input_refs,
                    source_leads: tuple[int, ...] = target_source_leads,
                    cycle_reference_time: datetime = reference_time,
                ) -> dict[str, Any]:
                    field_payloads = _group_normalization_payloads(refs, raw)
                    self._require_contiguous_previous_leads(field_payloads, source_leads)
                    _dataset, lineage = normalize_gfs_cycle(
                        settings=self._configuration.gfs,
                        forecast_reference_time=cycle_reference_time,
                        source_lead_hours=source_leads,
                        field_payloads=field_payloads,
                        grid_id="phase2-gfs.v1",
                        configuration_snapshot_id=str(request.configuration_snapshot_id),
                        variable_lineage_manifest_id=str(refs[0][1]),
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
                lineage_artifact = lineage_result.output

            canonical_input_refs = (
                input_refs
                + index_refs
                + (
                    (("qpf-lineage", lineage_artifact.artifact_id),)
                    if lineage_artifact is not None
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
                lineage_id: str = (
                    str(lineage_artifact.artifact_id)
                    if lineage_artifact is not None
                    else str(input_refs[0][1])
                ),
            ) -> xr.Dataset:
                field_payloads = _group_normalization_payloads(refs, raw[: len(refs)])
                grid_id = f"phase2-{model.lower()}.v1"
                cfg_id = str(request.configuration_snapshot_id)
                if model == "HRRR":
                    return normalize_hrrr_phase2_cycle(
                        settings=self._configuration.hrrr,
                        forecast_reference_time=reference_time,
                        source_lead_hours=source_leads,
                        field_payloads=field_payloads,
                        grid_id=grid_id,
                        configuration_snapshot_id=cfg_id,
                        variable_lineage_manifest_id=lineage_id,
                    )
                if model == "NBM":
                    return normalize_nbm_cycle(
                        settings=self._configuration.nbm,
                        forecast_reference_time=reference_time,
                        source_lead_hours=source_leads,
                        field_payloads=field_payloads,
                        grid_id=grid_id,
                        configuration_snapshot_id=cfg_id,
                        variable_lineage_manifest_id=lineage_id,
                    )
                self._require_contiguous_previous_leads(field_payloads, source_leads)
                dataset, _lineage = normalize_gfs_cycle(
                    settings=self._configuration.gfs,
                    forecast_reference_time=reference_time,
                    source_lead_hours=source_leads,
                    field_payloads=field_payloads,
                    grid_id=grid_id,
                    configuration_snapshot_id=cfg_id,
                    variable_lineage_manifest_id=lineage_id,
                )
                return dataset

            result = artifact_service.execute_raw_transformation(
                transformation,
                transform=_transform,
                serializer=_NETCDF,
                input_loader=lambda payload: payload,
                output_validator=validate_canonical_guidance_v2,
            )
            artifacts.append(result.output)
        return NormalizedGuidanceArtifacts(artifacts=tuple(artifacts))

    def _model_reference_time(
        self,
        acquisitions_by_model: dict[str, tuple[Phase2LeadAcquisition, ...]],
        model: str,
    ) -> datetime:
        acquisitions = acquisitions_by_model.get(model.upper(), ())
        if not acquisitions:
            raise ValueError(f"no retained acquisition metadata for model {model!r}")
        first = acquisitions[0]
        return datetime(
            first.cycle_date.year,
            first.cycle_date.month,
            first.cycle_date.day,
            first.cycle_hour,
            tzinfo=UTC,
        )

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
    ) -> AlignmentArtifacts:
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
        stations = self._configuration.stations

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
                for horizon in _HORIZONS:
                    target_valid_time = np.datetime64(
                        request.target_reference_time.replace(tzinfo=None), "ns"
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
                                    request.target_reference_time.replace(tzinfo=None), "ns"
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
                "shape": [len(models), len(_HORIZONS), len(stations)],
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
                if value["shape"][1:] == [len(_HORIZONS), len(self._configuration.stations)]
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
    ) -> AvailabilityArtifacts:
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
                "cutoff": request.information_cutoff.isoformat(),
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
            transform=lambda aligned: self._available_payload(aligned),
            serializer=_JSON,
            input_loader=_JSON.deserialize,
            output_validator=lambda value: (
                None
                if value["run_state"] in {"complete", "degraded", "invalid"}
                else (_ for _ in ()).throw(ValueError("invalid run_state"))
            ),
        )
        return AvailabilityArtifacts(cycle_selection=cycle.output, report=report.output)

    def _available_payload(self, aligned: dict[str, Any]) -> dict[str, Any]:
        approved_models: set[str] = set(aligned["models"])
        for model in tuple(approved_models):
            try:
                for horizon in _HORIZONS:
                    for station in self._configuration.stations:
                        station_id = str(station.station_id)
                        by_point = aligned["values"][model][str(horizon)]
                        validate_source_gust(
                            gust_m_s=by_point[f"{station_id}|wind_gust_10m"],
                            sustained_speed_m_s=math.hypot(
                                by_point[f"{station_id}|eastward_wind_10m"],
                                by_point[f"{station_id}|northward_wind_10m"],
                            ),
                        )
            except (KeyError, ValueError, GustDisqualificationError):
                approved_models.remove(model)
        models = frozenset(approved_models)
        stations = self._configuration.stations
        entries = []
        availability_objects = []
        for variable in _VARIABLES:
            for station in stations:
                for horizon in _HORIZONS:
                    if variable == "probability_of_precipitation_1h":
                        value = evaluate_pop_availability(
                            location=str(station.station_id),
                            target_horizon=horizon,
                            nbm_available="NBM" in models,
                        )
                    else:
                        table = (
                            self._configuration.blend_configuration.qpf_table
                            if variable == "liquid_equivalent_precipitation_amount_1h"
                            else self._configuration.blend_configuration.scalar_vector_table
                        )
                        value = evaluate_scalar_vector_availability(
                            table=table,
                            variable_id=variable,
                            location=str(station.station_id),
                            target_horizon=horizon,
                            available_models=models,
                        )
                    availability_objects.append(value)
                    entries.append(
                        {
                            "variable": variable,
                            "station": str(station.station_id),
                            "horizon": horizon,
                            "state": value.state,
                            "models": list(value.available_models),
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
            "models": [m for m in _MODELS if m in models],
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
    ) -> AtomicForecastArtifacts:
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
            transform=lambda aligned, available: self._forecast_pair(aligned, available, request),
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
        self, aligned: dict[str, Any], availability: dict[str, Any], request: Phase2Request
    ) -> tuple[xr.Dataset, dict[str, Any]]:
        stations = tuple(str(s.station_id) for s in self._configuration.stations)
        values: dict[tuple[str, str, int], float] = {}
        states: dict[tuple[str, str, int], str] = {}
        rows: list[BlendContributionRow] = []
        entries = {
            (entry["variable"], entry["station"], entry["horizon"]): entry
            for entry in availability["entries"]
        }
        for variable in _VARIABLES:
            for station in stations:
                for horizon in _HORIZONS:
                    key = (variable, station, horizon)
                    entry = entries[key]
                    states[key] = entry["state"]
                    contributors: list[ContributorRecord] = []
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
                                request.target_reference_time + timedelta(hours=horizon)
                            ).isoformat(),
                            operator_id=f"phase2.{variable}.v1",
                            availability_state=entry["state"],
                            fallback_row_id=(entry["row_id"] and entry["row_id"]),
                            contributors=tuple(contributors),
                            unrounded_sum=result,
                            serialized_output=result,
                        )
                    )
        dataset = assemble_baseline_forecast_v2(
            values=values,
            states=states,
            target_reference_time=np.datetime64(
                request.target_reference_time.replace(tzinfo=None), "ns"
            ),
            forecast_issue_time=np.datetime64(
                request.forecast_issue_time.replace(tzinfo=None), "ns"
            ),
            uncorrected_blend_artifact_id="pending-atomic-output",
            identity_correction_artifact_id="not-yet-applied",
        )
        dataset.attrs["schema_version"] = "uncorrected-blend-forecast.v1"
        manifest = BlendContributionManifest(
            rows=tuple(rows),
            expected_variable_ids=_VARIABLES,
            expected_locations=tuple(StationId(s) for s in stations),
            expected_target_horizons=_HORIZONS,
            configuration_digest=str(request.configuration_digest),
            code_revision=request.code_revision,
            environment_digest=str(request.environment_digest),
            lockfile_digest=str(request.lockfile_digest),
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
    ) -> CorrectedForecastArtifacts:
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
    ) -> ObservationArtifacts:
        settings = self._configuration.aviationweather
        station_ids = tuple(s.provider_icao_id for s in self._configuration.stations)
        query_date = request.verification_cutoff
        fetched = acquire_metar_batch(
            settings,
            transport=self._aviationweather_transport,
            clock=self._clock,
            sleeper=self._sleeper,
            station_ids=station_ids,
            query_date=query_date,
            rate_limiter=self._aviationweather_rate_limiter,
        )
        raw = artifact_service.register_source(
            SourceRegistrationRequest(
                source_authority="aviationweather.gov",
                source_locator="metar",
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
                configuration_snapshot_id=request.configuration_snapshot_id,
                configuration_digest=request.configuration_digest,
                code_revision=request.code_revision,
                environment_digest=request.environment_digest,
            ),
            fetched.payload,
        )

        stations_by_icao = {s.provider_icao_id: s for s in self._configuration.stations}

        def _normalize(*_raw: Any) -> dict[str, Any]:
            raw_records = parse_raw_metar_response(fetched.payload)
            rows: list[dict[str, Any]] = []
            for index, record in enumerate(raw_records):
                station = stations_by_icao.get(record.icao_id)
                if station is None:
                    continue
                station_record = _station_record(station)
                normalized = normalize_metar_record_v2(
                    raw=_as_v2_record(record),
                    station=station_record,
                    station_id=station.station_id,
                    policy=self._configuration.observation_normalization_policy,
                    raw_artifact_id=raw.artifact_id,
                    raw_record_index=index,
                    station_snapshot_artifact_id=station_snapshot.artifact_id,
                    ingested_at=self._clock.now(),
                    query_window_start=request.target_reference_time,
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
    ) -> MatchingArtifacts:
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
        stations = tuple(str(s.station_id) for s in self._configuration.stations)
        matching_policy = self._configuration.matching_policy

        def make_pairs(bound: Mapping[str, Any]) -> dict[str, Any]:
            obs = [
                _model_from_json_value(NormalizedObservationV2, row)
                for row in bound["observations"]["rows"]
            ]
            pair_rows = match_baseline_to_observations_v2(
                baseline=bound["baseline"],
                station_ids=stations,
                target_horizons=_HORIZONS,
                observations=obs,
                matching_policy=matching_policy,
                verification_cutoff=request.verification_cutoff,
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
    ) -> VerificationArtifacts:
        metric_set = self._configuration.metric_set

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
        provider=provider,
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
