"""Immutable persisted Phase 2 replay contracts (Codex review
`t_652b155e`, MEDIUM replay blocker).

The previous remediation *wrote* a run spec and a station snapshot but
nothing ever read them back: replay accepted the caller's request and
pinned manifests verbatim, production normalization required the
provider's process-local ``_acquisitions_by_run`` cache, every
downstream stage closed over a mutable live ``Phase2Configuration``, and
observations were re-fetched from AviationWeather.

This module supplies the missing half: strict, frozen contracts for the
two immutable roots a Phase 2 run persists, plus the derived accessors
the science stages need. Everything a replay recomputes is reconstructed
from these bytes:

* the replay request frame (both cutoffs, target frame, horizons, seed)
  and its identity digests;
* the exact selected model cycle, per-lead acquisition metadata, and
  per-message inventory evidence (endpoint, resolved URLs, message
  numbers, byte ranges, inventory rows) -- this is what removes
  normalization's dependency on the provider's in-process acquisition
  cache;
* the complete ``Phase2Configuration`` that produced the run, so
  alignment, availability, blending, observation normalization,
  matching, and verification read persisted policy rather than whatever
  live object happens to be wired up at replay time;
* the station catalog snapshot (coordinates, elevation, provider
  identity, priority) and the point-extraction policy.

Fail-closed philosophy: a caller may supply a request and pinned
manifests, but they are only ever *checked against* these bytes -- never
trusted. ``Phase2PersistedRun`` therefore exposes ``require_*`` methods
that raise :class:`Phase2ReplayIdentityError` on any conflict, and the
coordinator calls them before a single stage runs.

Layering note: this module is pure contract/policy reconstruction. It
imports only ``catalog``/``contracts``/``common`` types (no storage, no
science, no adapters), so ``application.phase2`` can depend on it
without weakening the "coordinator is pure composition" import contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mesoforge.catalog.configuration import (
    MatchingPolicy,
    MetricSet,
    ObservationNormalizationPolicy,
    Phase2BlendConfiguration,
    Phase2Configuration,
    PointExtractionPolicy,
)
from mesoforge.catalog.sources import AviationWeatherSettings
from mesoforge.catalog.stations import StationDefinition
from mesoforge.common.identifiers import (
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    ModelCycleSelectionPolicyId,
    RunId,
    StationId,
    VariableId,
    validate_code_revision,
)
from mesoforge.common.time import UtcInstant
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.serialization import canonical_json_digest

RUN_SPEC_ARTIFACT_TYPE = "phase2-run-spec"
RUN_SPEC_SCHEMA_VERSION = "phase2-run-spec.v1"
STATION_SNAPSHOT_ARTIFACT_TYPE = "station-catalog-snapshot"
STATION_SNAPSHOT_SCHEMA_VERSION = "station-catalog-snapshot.v1"
OBSERVATION_RESPONSE_ARTIFACT_TYPE = "aviationweather-metar-response"
OBSERVATION_RESPONSE_SCHEMA_VERSION = "aviationweather-metar-response.v1"
OBSERVATION_RESPONSE_AUTHORITY = "aviationweather.gov"

MODELS: tuple[str, ...] = ("HRRR", "NBM", "GFS")


class Phase2ReplayIdentityError(Exception):
    """A caller-supplied request, pinned root, live configuration, or
    recorded observation artifact conflicts with the persisted run
    identity. Replay fails closed rather than silently recomputing a
    different run."""


class Phase2ReplayContractError(Exception):
    """A persisted replay root is missing, malformed, or incomplete."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PersistedArtifactIdentity(_FrozenModel):
    """Exact immutable identity of an input registered before the run spec."""

    artifact_id: ArtifactId
    content_digest: Digest


class PersistedSelectedMessage(_FrozenModel):
    """One retained GRIB message's exact acquisition evidence.

    ``record_identity`` is the identity fragment the acquisition stage
    embedded in the registered artifact's ``field_role``
    (``message:<variable>:record:<record_identity>``), so a replay can
    bind each persisted message to its pinned source root without any
    live acquisition object.
    """

    canonical_variable_id: VariableId
    record_identity: str
    ordinal: int
    message_number: int
    byte_offset: int
    byte_start: int
    byte_end: int
    inventory_row: str
    artifact: PersistedArtifactIdentity

    @model_validator(mode="after")
    def _check_ranges(self) -> PersistedSelectedMessage:
        if self.ordinal < 0:
            raise ValueError("ordinal must be nonnegative")
        if self.byte_end <= self.byte_start:
            raise ValueError("byte_end must exceed byte_start")
        if not self.inventory_row:
            raise ValueError("inventory_row must not be empty")
        if not self.canonical_variable_id:
            raise ValueError("canonical_variable_id must not be empty")
        return self

    @property
    def field_role(self) -> str:
        return f"message:{self.canonical_variable_id}:record:{self.record_identity}"


class PersistedLeadAcquisition(_FrozenModel):
    """Everything one acquired source lead contributed to the run."""

    source_lead_hours: int
    endpoint: str
    resolved_index_url: str
    resolved_grib_url: str
    index_completed_at: UtcInstant
    grib_completed_at: UtcInstant
    selected_messages: tuple[PersistedSelectedMessage, ...]
    index_artifact: PersistedArtifactIdentity

    @model_validator(mode="after")
    def _check_messages(self) -> PersistedLeadAcquisition:
        if self.source_lead_hours < 0:
            raise ValueError("source_lead_hours must be nonnegative")
        if not self.selected_messages:
            raise ValueError("an acquired lead must retain at least one selected message")
        roles = tuple(message.field_role for message in self.selected_messages)
        if len(set(roles)) != len(roles):
            raise ValueError("selected message record identities must be distinct within a lead")
        return self

    def messages_for(
        self, canonical_variable_id: VariableId
    ) -> tuple[PersistedSelectedMessage, ...]:
        return tuple(
            message
            for message in self.selected_messages
            if message.canonical_variable_id == canonical_variable_id
        )


class PersistedModelGroup(_FrozenModel):
    """One model's persisted selection outcome and cycle policy."""

    model: Literal["HRRR", "NBM", "GFS"]
    selected: bool
    source_cycle_reference_time: UtcInstant | None
    source_lead_hours: tuple[int, ...]
    endpoints: tuple[str, ...]
    max_age_hours: float
    cycle_completion_deadline_minutes: float
    allowed_cycle_hours: tuple[int, ...]
    canonical_variable_ids: tuple[VariableId, ...]
    source_grid_profile_id: GridId
    leads: tuple[PersistedLeadAcquisition, ...]

    @model_validator(mode="after")
    def _check_group(self) -> PersistedModelGroup:
        if self.selected:
            if self.source_cycle_reference_time is None:
                raise ValueError(f"{self.model} was selected but has no source cycle")
            if not self.leads:
                raise ValueError(f"{self.model} was selected but retained no lead acquisitions")
        else:
            if self.source_cycle_reference_time is not None or self.leads:
                raise ValueError(f"{self.model} was not selected but retained acquisitions")
        lead_hours = tuple(lead.source_lead_hours for lead in self.leads)
        if len(set(lead_hours)) != len(lead_hours):
            raise ValueError(f"{self.model} retained duplicate source leads")
        if tuple(sorted(lead_hours)) != tuple(self.source_lead_hours):
            raise ValueError(
                f"{self.model} source_lead_hours must list exactly the retained lead hours"
            )
        if tuple(sorted({lead.endpoint for lead in self.leads})) != tuple(self.endpoints):
            raise ValueError(f"{self.model} endpoints must list exactly the retained endpoints")
        if not self.canonical_variable_ids:
            raise ValueError(f"{self.model} must declare its canonical variables")
        return self

    def lead(self, source_lead_hours: int) -> PersistedLeadAcquisition:
        for item in self.leads:
            if item.source_lead_hours == source_lead_hours:
                return item
        raise Phase2ReplayContractError(
            f"{self.model} lead {source_lead_hours!r} has no persisted acquisition evidence"
        )

    @property
    def leads_by_hour(self) -> dict[int, PersistedLeadAcquisition]:
        return {lead.source_lead_hours: lead for lead in self.leads}


class PersistedRequestIdentity(_FrozenModel):
    """The run's request-identity digests, exactly as persisted.

    The persisted JSON key is ``request_digests`` (unchanged since the
    previous remediation); the model field deliberately avoids a
    ``_digests`` suffix because it is an aggregate, not a digest value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    configuration_snapshot_id: ConfigurationSnapshotId
    configuration_digest: Digest
    code_revision: str
    environment_digest: Digest
    lockfile_digest: Digest

    @field_validator("code_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_code_revision(value)


class PersistedConfigurationIdentity(_FrozenModel):
    """Canonical digests of the configuration objects that produced the
    run. Persisted under the ``configuration_digests`` JSON key."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    phase2_configuration: Digest
    blend_configuration: Digest
    matching_policy: Digest


class PersistedCycleSelectionPolicy(_FrozenModel):
    schema_version: str
    policy_id: ModelCycleSelectionPolicyId
    target_horizons: tuple[int, ...]


class Phase2RunSpecV1(_FrozenModel):
    """``phase2-run-spec.v1`` -- the durable, self-sufficient replay root.

    Reconstructing this model from the registered artifact's bytes is the
    only way a replay learns what the run was asked to compute and what
    it actually consumed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    schema_version: Literal["phase2-run-spec.v1"] = "phase2-run-spec.v1"
    run_id: RunId
    target_reference_time: UtcInstant
    forecast_issue_time: UtcInstant
    information_cutoff: UtcInstant
    verification_cutoff: UtcInstant
    target_horizons: tuple[int, ...]
    random_seed: int
    selection_digest: Digest
    cycle_selection_policy: PersistedCycleSelectionPolicy
    required_groups: tuple[PersistedModelGroup, ...]
    request_identity: PersistedRequestIdentity = Field(alias="request_digests")
    configuration_identity: PersistedConfigurationIdentity = Field(alias="configuration_digests")
    configuration: Phase2Configuration
    station_snapshot: PersistedArtifactIdentity

    @model_validator(mode="after")
    def _check_groups(self) -> Phase2RunSpecV1:
        models = tuple(group.model for group in self.required_groups)
        if models != MODELS:
            raise ValueError(
                f"required_groups must describe exactly {MODELS!r} in order, got {models!r}"
            )
        if not self.target_horizons:
            raise ValueError("target_horizons must not be empty")
        if tuple(self.cycle_selection_policy.target_horizons) != tuple(self.target_horizons):
            raise ValueError(
                "cycle_selection_policy.target_horizons must equal the run's target horizons"
            )
        # A run in which no model was selected is a legitimate persisted
        # fact (every candidate cycle was unavailable or past the
        # cutoff). Rejecting it here would make the run spec disagree
        # with what actually happened; the "normalized guidance must not
        # be empty" invariant that stops such a run is the coordinator's,
        # and it still fires -- for the original run and for a replay
        # alike.
        return self

    @model_validator(mode="after")
    def _check_embedded_configuration_matches_its_digests(self) -> Phase2RunSpecV1:
        """The embedded configuration bytes must reproduce exactly the
        digests the run recorded.

        Without this, a tampered run spec could persist policy that never
        produced the run. With it, the run spec is internally
        self-verifying: replay can safely treat the embedded
        configuration as *the* configuration.
        """
        digests = self.configuration_identity
        actual_configuration = canonical_json_digest(self.configuration.model_dump(mode="json"))
        if str(actual_configuration) != str(digests.phase2_configuration):
            raise ValueError(
                "embedded configuration does not reproduce configuration_digests."
                f"phase2_configuration ({actual_configuration!s} != "
                f"{digests.phase2_configuration!s})"
            )
        actual_blend = canonical_json_digest(
            self.configuration.blend_configuration.model_dump(mode="json")
        )
        if str(actual_blend) != str(digests.blend_configuration):
            raise ValueError(
                "embedded blend configuration does not reproduce configuration_digests."
                "blend_configuration"
            )
        if str(self.configuration.matching_policy.digest) != str(digests.matching_policy):
            raise ValueError(
                "embedded matching policy does not reproduce configuration_digests.matching_policy"
            )
        return self

    @model_validator(mode="after")
    def _check_groups_match_the_embedded_configuration(self) -> Phase2RunSpecV1:
        """Each persisted group's cycle policy and canonical variables
        must equal the embedded configuration's own settings, so the two
        halves of the run spec can never disagree about what was
        selected."""
        policy = self.configuration.cycle_selection_policy
        if tuple(policy.target_horizons) != tuple(self.target_horizons):
            raise ValueError(
                "embedded configuration's cycle policy horizons must equal the run's horizons"
            )
        if str(policy.policy_id) != self.cycle_selection_policy.policy_id:
            raise ValueError("persisted cycle policy id must equal the embedded configuration's")
        for group in self.required_groups:
            settings = getattr(self.configuration, group.model.lower())
            if group.max_age_hours != settings.max_age_hours:
                raise ValueError(f"{group.model} max_age_hours disagrees with the configuration")
            if (
                group.cycle_completion_deadline_minutes
                != settings.cycle_completion_deadline_minutes
            ):
                raise ValueError(
                    f"{group.model} cycle_completion_deadline_minutes disagrees with the "
                    "configuration"
                )
            if tuple(group.allowed_cycle_hours) != tuple(
                getattr(settings, "allowed_cycle_hours", ())
            ):
                raise ValueError(
                    f"{group.model} allowed_cycle_hours disagrees with the configuration"
                )
            expected_variables = tuple(
                sorted(contract.canonical_variable_id for contract in settings.field_contracts)
            )
            if tuple(group.canonical_variable_ids) != expected_variables:
                raise ValueError(
                    f"{group.model} canonical_variable_ids disagree with the configuration"
                )
        return self

    def group(self, model: str) -> PersistedModelGroup:
        for item in self.required_groups:
            if item.model == model:
                return item
        raise Phase2ReplayContractError(f"no persisted group for model {model!r}")

    @property
    def selected_models(self) -> tuple[str, ...]:
        return tuple(group.model for group in self.required_groups if group.selected)


class PersistedStation(_FrozenModel):
    station_id: StationId
    provider_icao_id: str
    expected_latitude: float
    expected_longitude: float
    expected_elevation_m: float
    site_name: str
    provider_site_types: tuple[str, ...]
    provider_priority: int
    exposure_identity: Literal["unknown"] = "unknown"
    instrument_identity: Literal["unknown"] = "unknown"

    def as_definition(self) -> StationDefinition:
        """Reconstruct the exact ``StationDefinition`` the run used.

        Alignment, METAR normalization, and matching all consume station
        definitions; rebuilding them here means those stages never touch
        a live catalog object.
        """
        return StationDefinition.model_validate(
            {
                "station_id": self.station_id,
                "provider_icao_id": self.provider_icao_id,
                "expected_latitude": self.expected_latitude,
                "expected_longitude": self.expected_longitude,
                "expected_elevation_m": self.expected_elevation_m,
                "site_name": self.site_name,
                "provider_site_types": self.provider_site_types,
                "provider_priority": self.provider_priority,
                "exposure_identity": self.exposure_identity,
                "instrument_identity": self.instrument_identity,
            },
            strict=True,
        )


class Phase2StationSnapshotV1(_FrozenModel):
    """``station-catalog-snapshot.v1`` -- the run's immutable stations."""

    schema_version: Literal["station-catalog-snapshot.v1"] = "station-catalog-snapshot.v1"
    domain_id: str
    station_ids: tuple[StationId, ...]
    stations: tuple[PersistedStation, ...]
    point_extraction_policy: PointExtractionPolicy

    @model_validator(mode="after")
    def _check_stations(self) -> Phase2StationSnapshotV1:
        if not self.stations:
            raise ValueError("a station snapshot must describe at least one station")
        ids = tuple(station.station_id for station in self.stations)
        if ids != tuple(self.station_ids):
            raise ValueError("station_ids must list exactly the snapshot's stations in order")
        if len(set(ids)) != len(ids):
            raise ValueError("station ids must be distinct")
        return self

    @property
    def definitions(self) -> tuple[StationDefinition, ...]:
        return tuple(station.as_definition() for station in self.stations)


class Phase2PersistedRun(_FrozenModel):
    """The validated pair of persisted roots, plus every derived policy
    accessor the science stages need.

    Instances are created by the coordinator from repository-authoritative
    manifests and object-store-verified bytes. Once constructed, no stage
    needs a live configuration, a live station catalog, or a provider's
    in-process acquisition cache.
    """

    run_spec: Phase2RunSpecV1
    station_snapshot: Phase2StationSnapshotV1
    run_spec_artifact: ArtifactManifest
    station_snapshot_artifact: ArtifactManifest

    @model_validator(mode="after")
    def _check_roots_agree(self) -> Phase2PersistedRun:
        station_identity = self.run_spec.station_snapshot
        if (
            self.station_snapshot_artifact.artifact_id != station_identity.artifact_id
            or self.station_snapshot_artifact.content_digest != station_identity.content_digest
        ):
            raise ValueError("station snapshot manifest does not match the run-spec identity")
        configured = self.run_spec.configuration.stations
        persisted = self.station_snapshot.definitions
        if configured != persisted:
            raise ValueError(
                "the station snapshot's stations must be byte-identical to the run spec's "
                "persisted configuration stations"
            )
        if self.station_snapshot.domain_id != str(self.run_spec.configuration.domain.domain_id):
            raise ValueError("the station snapshot's domain must match the persisted domain")
        if (
            self.station_snapshot.point_extraction_policy
            != self.run_spec.configuration.point_extraction_policy
        ):
            raise ValueError(
                "the station snapshot's point-extraction policy must match the persisted policy"
            )
        return self

    # ------------------------------------------------------------------
    # Derived, persisted-only policy accessors
    # ------------------------------------------------------------------

    @property
    def configuration(self) -> Phase2Configuration:
        return self.run_spec.configuration

    @property
    def stations(self) -> tuple[StationDefinition, ...]:
        return self.station_snapshot.definitions

    @property
    def station_ids(self) -> tuple[str, ...]:
        return tuple(str(station.station_id) for station in self.stations)

    @property
    def target_horizons(self) -> tuple[int, ...]:
        return tuple(self.run_spec.target_horizons)

    @property
    def blend_configuration(self) -> Phase2BlendConfiguration:
        return self.run_spec.configuration.blend_configuration

    @property
    def matching_policy(self) -> MatchingPolicy:
        return self.run_spec.configuration.matching_policy

    @property
    def metric_set(self) -> MetricSet:
        return self.run_spec.configuration.metric_set

    @property
    def observation_normalization_policy(self) -> ObservationNormalizationPolicy:
        return self.run_spec.configuration.observation_normalization_policy

    @property
    def point_extraction_policy(self) -> PointExtractionPolicy:
        return self.station_snapshot.point_extraction_policy

    @property
    def aviationweather(self) -> AviationWeatherSettings:
        return self.run_spec.configuration.aviationweather

    def source_settings(self, model: str) -> Any:
        """The persisted per-model source settings (HRRR/NBM/GFS)."""
        attribute = model.lower()
        if attribute not in {"hrrr", "nbm", "gfs"}:
            raise Phase2ReplayContractError(f"unknown model {model!r}")
        return getattr(self.run_spec.configuration, attribute)

    def reference_time(self, model: str) -> datetime:
        group = self.run_spec.group(model)
        if group.source_cycle_reference_time is None:
            raise Phase2ReplayContractError(f"{model} has no persisted source cycle reference time")
        return group.source_cycle_reference_time

    # ------------------------------------------------------------------
    # Fail-closed binding checks
    # ------------------------------------------------------------------

    def require_request_identity(
        self,
        *,
        run_id: RunId,
        target_reference_time: datetime,
        forecast_issue_time: datetime,
        information_cutoff: datetime,
        verification_cutoff: datetime,
        target_horizons: tuple[int, ...],
        random_seed: int,
        configuration_snapshot_id: ConfigurationSnapshotId,
        configuration_digest: Digest,
        code_revision: str,
        environment_digest: Digest,
        lockfile_digest: Digest,
    ) -> None:
        """Reject a caller-supplied request that conflicts with the run
        spec on any identity-bearing field.

        Every identifier/digest parameter is re-constructed against its
        typed class immediately, so a malformed value from an untyped
        caller fails closed before any comparison is attempted -- and a
        merely *different* value fails closed on the comparison itself.

        This is also what makes a *live configuration* conflict fail
        closed: a caller who loads a different configuration must register
        a different snapshot and therefore present different
        ``configuration_snapshot_id``/``configuration_digest`` values,
        which are rejected here before any stage runs.
        """
        run_id = RunId(run_id)
        configuration_snapshot_id = ConfigurationSnapshotId(configuration_snapshot_id)
        configuration_digest = Digest(configuration_digest)
        code_revision = validate_code_revision(code_revision)
        environment_digest = Digest(environment_digest)
        lockfile_digest = Digest(lockfile_digest)
        spec = self.run_spec
        digests = spec.request_identity
        mismatches: list[str] = []
        for label, supplied, persisted in (
            ("run_id", str(run_id), str(spec.run_id)),
            (
                "target_reference_time",
                target_reference_time.isoformat(),
                spec.target_reference_time.isoformat(),
            ),
            (
                "forecast_issue_time",
                forecast_issue_time.isoformat(),
                spec.forecast_issue_time.isoformat(),
            ),
            (
                "information_cutoff",
                information_cutoff.isoformat(),
                spec.information_cutoff.isoformat(),
            ),
            (
                "verification_cutoff",
                verification_cutoff.isoformat(),
                spec.verification_cutoff.isoformat(),
            ),
            ("target_horizons", repr(tuple(target_horizons)), repr(tuple(spec.target_horizons))),
            ("random_seed", repr(random_seed), repr(spec.random_seed)),
            (
                "configuration_snapshot_id",
                str(configuration_snapshot_id),
                str(digests.configuration_snapshot_id),
            ),
            ("configuration_digest", str(configuration_digest), str(digests.configuration_digest)),
            ("code_revision", code_revision, digests.code_revision),
            ("environment_digest", str(environment_digest), str(digests.environment_digest)),
            ("lockfile_digest", str(lockfile_digest), str(digests.lockfile_digest)),
        ):
            if supplied != persisted:
                mismatches.append(f"{label}: supplied {supplied!r} != persisted {persisted!r}")
        if mismatches:
            raise Phase2ReplayIdentityError(
                "the supplied replay request conflicts with the persisted phase2-run-spec.v1: "
                + "; ".join(mismatches)
            )

    def require_configuration_identity(self, configuration_digest: Digest) -> None:
        """Reject a live configuration whose canonical digest differs
        from the one the run persisted."""
        persisted = self.run_spec.configuration_identity.phase2_configuration
        if str(configuration_digest) != str(persisted):
            raise Phase2ReplayIdentityError(
                f"live Phase 2 configuration digest {str(configuration_digest)!r} conflicts with "
                f"the persisted run configuration digest {str(persisted)!r}"
            )

    def expected_source_roots(
        self,
    ) -> tuple[tuple[str, int, str, PersistedArtifactIdentity], ...]:
        roots: list[tuple[str, int, str, PersistedArtifactIdentity]] = []
        for group in self.run_spec.required_groups:
            for lead in group.leads:
                roots.append((group.model, lead.source_lead_hours, "index", lead.index_artifact))
                roots.extend(
                    (group.model, lead.source_lead_hours, message.field_role, message.artifact)
                    for message in lead.selected_messages
                )
        return tuple(sorted(roots, key=lambda root: (MODELS.index(root[0]), root[1], root[2])))

    def require_source_roots(self, roots: tuple[tuple[str, int, str, ArtifactId], ...]) -> None:
        """Reject pinned source roots that are not exactly the roots the
        run spec says were acquired.

        ``roots`` is ``(model, source_lead_hours, field_role, artifact_id)``
        for every pinned root, in the coordinator's canonical order. Both
        directions are checked: a missing root and an extra root are each
        a hard failure, so a replay can neither drop evidence nor smuggle
        in an artifact the run never consumed.
        """
        supplied = {(model, lead, role, artifact) for model, lead, role, artifact in roots}
        if len(supplied) != len(roots):
            raise Phase2ReplayIdentityError("pinned source roots contain duplicates")
        expected = {
            (model, lead, role, identity.artifact_id)
            for model, lead, role, identity in self.expected_source_roots()
        }
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        if missing or extra:
            raise Phase2ReplayIdentityError(
                "pinned source roots do not match the persisted run spec; "
                f"missing={missing[:8]!r} (of {len(missing)}) "
                f"extra={extra[:8]!r} (of {len(extra)})"
            )

    def require_recorded_observations(self, manifests: tuple[ArtifactManifest, ...]) -> None:
        """Reject recorded observation source artifacts that were not
        registered under this run's own identity.

        Replay reuses these bytes instead of re-fetching AviationWeather,
        so they must be genuine source artifacts of the same
        configuration snapshot/code revision, available by the run's
        verification cutoff.
        """
        if not manifests:
            raise Phase2ReplayIdentityError(
                "replay requires at least one recorded observation response artifact"
            )
        digests = self.run_spec.request_identity
        for manifest in manifests:
            if manifest.artifact_type != OBSERVATION_RESPONSE_ARTIFACT_TYPE:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has artifact type "
                    f"{manifest.artifact_type!r}, expected "
                    f"{OBSERVATION_RESPONSE_ARTIFACT_TYPE!r}"
                )
            if manifest.artifact_schema_version != OBSERVATION_RESPONSE_SCHEMA_VERSION:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has schema "
                    f"{manifest.artifact_schema_version!r}"
                )
            if manifest.source_registration_digest is None or manifest.source_identity is None:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} is not a source artifact"
                )
            if manifest.source_identity.authority != OBSERVATION_RESPONSE_AUTHORITY:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} was not issued by "
                    f"{OBSERVATION_RESPONSE_AUTHORITY!r}"
                )
            expected_locator = f"phase2-metar://{self.run_spec.run_id}"
            if manifest.source_identity.locator != expected_locator:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has locator "
                    f"{manifest.source_identity.locator!r}, expected {expected_locator!r}"
                )
            if manifest.source_identity.revision != "phase2.v1":
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has a conflicting revision"
                )
            if manifest.quality_state != "valid":
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} is not valid"
                )
            if manifest.availability.authority != OBSERVATION_RESPONSE_AUTHORITY:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has a conflicting "
                    "availability authority"
                )
            if str(manifest.configuration_snapshot_id) != str(digests.configuration_snapshot_id):
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} belongs to configuration "
                    f"snapshot {manifest.configuration_snapshot_id!r}, not the run's"
                )
            if str(manifest.configuration_digest) != str(digests.configuration_digest):
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has a conflicting "
                    "configuration digest"
                )
            if manifest.code_revision != digests.code_revision:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has a conflicting code revision"
                )
            if str(manifest.environment_digest) != str(digests.environment_digest):
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} has a conflicting "
                    "environment digest"
                )
            if manifest.run_id != self.run_spec.run_id:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} is not bound to run "
                    f"{self.run_spec.run_id!r}"
                )
            if manifest.availability.available_at > self.run_spec.verification_cutoff:
                raise Phase2ReplayIdentityError(
                    f"recorded observation {manifest.artifact_id!r} became available after the "
                    "run's verification cutoff"
                )


def require_run_spec_artifact(manifest: ArtifactManifest) -> None:
    """Fail closed unless ``manifest`` really is a valid run-spec root."""
    _require_root(manifest, RUN_SPEC_ARTIFACT_TYPE, RUN_SPEC_SCHEMA_VERSION)


def require_station_snapshot_artifact(manifest: ArtifactManifest) -> None:
    """Fail closed unless ``manifest`` really is a valid station root."""
    _require_root(manifest, STATION_SNAPSHOT_ARTIFACT_TYPE, STATION_SNAPSHOT_SCHEMA_VERSION)


def _require_root(manifest: ArtifactManifest, artifact_type: str, schema_version: str) -> None:
    if manifest.artifact_type != artifact_type:
        raise Phase2ReplayContractError(
            f"expected artifact type {artifact_type!r}, got {manifest.artifact_type!r}"
        )
    if manifest.artifact_schema_version != schema_version:
        raise Phase2ReplayContractError(
            f"expected schema {schema_version!r}, got {manifest.artifact_schema_version!r}"
        )
    if manifest.quality_state != "valid":
        raise Phase2ReplayContractError(f"{artifact_type!r} root must be valid")
    if manifest.source_registration_digest is None:
        raise Phase2ReplayContractError(f"{artifact_type!r} root must be a source artifact")


def parse_run_spec(payload: bytes) -> Phase2RunSpecV1:
    """Reconstruct the run spec from persisted bytes, failing closed on
    anything malformed or incomplete."""
    try:
        return Phase2RunSpecV1.model_validate_json(payload)
    except Exception as exc:  # noqa: BLE001 -- normalized to a replay contract failure
        raise Phase2ReplayContractError(
            f"persisted {RUN_SPEC_SCHEMA_VERSION} is missing or incomplete: {exc}"
        ) from exc


def parse_station_snapshot(payload: bytes) -> Phase2StationSnapshotV1:
    """Reconstruct the station snapshot from persisted bytes."""
    try:
        return Phase2StationSnapshotV1.model_validate_json(payload)
    except Exception as exc:  # noqa: BLE001 -- normalized to a replay contract failure
        raise Phase2ReplayContractError(
            f"persisted {STATION_SNAPSHOT_SCHEMA_VERSION} is missing or incomplete: {exc}"
        ) from exc
