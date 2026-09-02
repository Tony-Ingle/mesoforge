"""Offline contract probes for the immutable Phase 2 replay roots
(Codex review ``t_652b155e``, MEDIUM replay blocker).

The integration-backed acceptance proof exercises replay end to end
against real PostgreSQL/MinIO. These tests exercise the persisted
contracts themselves -- the run spec, the station snapshot, and the
fail-closed binding checks -- offline and directly, so a regression in
replay identity is caught by the unit suite too.

Payloads are produced by the *production* writers
(``Phase2ProductionProvider._run_spec_payload`` /
``_station_snapshot_payload``) rather than restated here, so these tests
prove the real writer and the real loader agree.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application.phase2 import DiscoveryResult, Phase2Request, SelectedSourceRoot
from mesoforge.application.phase2_production import (
    Phase2ProductionProvider,
    Phase2UnavailableProvider,
    UnavailableHttpTransport,
)
from mesoforge.application.phase2_replay import (
    Phase2PersistedRun,
    Phase2ReplayContractError,
    Phase2ReplayIdentityError,
    parse_run_spec,
    parse_station_snapshot,
    require_run_spec_artifact,
    require_station_snapshot_artifact,
)
from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.artifacts import ArtifactManifest, Availability, SourceIdentity
from mesoforge.guidance.acquisition_v2 import Phase2LeadAcquisition, SelectedMessage
from mesoforge.guidance.index_parsing import IndexRow
from mesoforge.storage.json import CanonicalJsonSerializer

ROOT = Path(__file__).resolve().parents[3]
JSON = CanonicalJsonSerializer()

REFERENCE = datetime(2030, 8, 31, 12, tzinfo=UTC)
ISSUE = REFERENCE + timedelta(minutes=30)
CUTOFF = REFERENCE + timedelta(minutes=20)
VERIFY = REFERENCE + timedelta(hours=38)
HORIZONS = tuple(range(1, 37))
RUN_ID = "run_00000000-0000-0000-0000-0000000000a1"
SNAPSHOT_ID = "cfg_sha256_" + "1" * 64
CONFIG_DIGEST = Digest("sha256:" + "1" * 64)
ENV_DIGEST = Digest("sha256:" + "3" * 64)
LOCK_DIGEST = Digest("sha256:" + "4" * 64)
CODE_REVISION = "2" * 40

# The fixture-grid overlay is the approved acceptance profile; using the
# same composition here keeps the offline probes bound to real approved
# configuration rather than a hand-written stub.
_OVERLAYS = (
    ROOT / "configs/phase2-grasston.yaml",
    ROOT / "tests/fixtures/phase2-fixture-grid-overlay.yaml",
)


def _configuration() -> Phase2Configuration:
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=_OVERLAYS,
    )
    assert configuration.phase2 is not None
    return configuration.phase2


def _request(**changes: Any) -> Phase2Request:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "configuration_snapshot_id": SNAPSHOT_ID,
        "configuration_digest": CONFIG_DIGEST,
        "code_revision": CODE_REVISION,
        "environment_digest": ENV_DIGEST,
        "lockfile_digest": LOCK_DIGEST,
        "target_reference_time": REFERENCE,
        "forecast_issue_time": ISSUE,
        "information_cutoff": CUTOFF,
        "verification_cutoff": VERIFY,
    }
    values.update(changes)
    return Phase2Request.model_validate(values, strict=True)


def _message(variable_id: str, *, number: int, offset: int) -> SelectedMessage:
    return SelectedMessage(
        canonical_variable_id=variable_id,
        row=IndexRow(
            message_number=number,
            byte_offset=offset,
            line=f"{number}:{offset}:d=2030083112:{variable_id}:1 hour fcst:",
        ),
        byte_start=offset,
        byte_end=offset + 128,
        payload=b"GRIB" + bytes(124),
    )


def _acquisition(model: str, lead: int, variables: tuple[str, ...]) -> Phase2LeadAcquisition:
    return Phase2LeadAcquisition(
        model=model.lower(),
        cycle_date=date(2030, 8, 31),
        cycle_hour=12,
        forecast_hour=lead,
        endpoint="aws",
        resolved_grib_url=f"https://example/{model.lower()}/f{lead:03d}.grib2",
        resolved_index_url=f"https://example/{model.lower()}/f{lead:03d}.grib2.idx",
        index_payload=b"1:0:d=2030083112:TMP:2 m above ground:1 hour fcst:\n",
        index_attempts=(),
        index_completed_at=CUTOFF - timedelta(minutes=5),
        selected_messages=tuple(
            _message(variable_id, number=index + 1, offset=(index + 1) * 1000)
            for index, variable_id in enumerate(variables)
        ),
        grib_attempts=(),
        grib_completed_at=CUTOFF - timedelta(minutes=4),
        full_object_etag=None,
        full_object_last_modified=None,
        full_object_content_length=4096,
    )


def _provider(configuration: Phase2Configuration) -> Phase2ProductionProvider:
    """A provider used only as the real payload *writer*.

    No transport is ever exercised: the tests call the private payload
    builders directly with synthesized acquisitions, which is exactly
    what production would hand them.
    """
    transport = UnavailableHttpTransport()
    return Phase2ProductionProvider(
        configuration=configuration,
        hrrr_transport=transport,
        nbm_transport=transport,
        gfs_transport=transport,
        clock=_FixedClock(CUTOFF),
        sleeper=_NoSleep(),
    )


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _NoSleep:
    def sleep(self, seconds: float) -> None:
        return None


def _acquisitions(
    configuration: Phase2Configuration,
) -> dict[str, tuple[Phase2LeadAcquisition, ...]]:
    result: dict[str, tuple[Phase2LeadAcquisition, ...]] = {}
    for model in ("HRRR", "NBM", "GFS"):
        settings = getattr(configuration, model.lower())
        variables = tuple(
            sorted(contract.canonical_variable_id for contract in settings.field_contracts)
        )
        leads = (0, 1) if model == "GFS" else (1,)
        result[model] = tuple(_acquisition(model, lead, variables) for lead in leads)
    return result


def _payloads() -> tuple[Phase2Configuration, dict[str, Any], dict[str, Any]]:
    configuration = _configuration()
    provider = _provider(configuration)
    acquisitions = _acquisitions(configuration)
    stations = provider._station_snapshot_payload()  # noqa: SLF001 -- production writer
    station_manifest = _manifest(
        "station-catalog-snapshot", "station-catalog-snapshot.v1", JSON.serialize(stations)
    )
    roots = _source_roots(acquisitions)
    run_spec = provider._run_spec_payload(  # noqa: SLF001 -- exercising the production writer
        _request(),
        DiscoveryResult(selection_digest=Digest("sha256:" + "9" * 64)),
        acquisitions,
        station_manifest,
        roots,
    )
    return configuration, run_spec, stations


def _source_roots(
    acquisitions: dict[str, tuple[Phase2LeadAcquisition, ...]],
) -> tuple[SelectedSourceRoot, ...]:
    roots: list[SelectedSourceRoot] = []
    for model in ("HRRR", "NBM", "GFS"):
        for acquisition in acquisitions.get(model, ()):
            roots.append(
                SelectedSourceRoot(
                    model=model,
                    source_lead_hours=acquisition.forecast_hour,
                    field_role="index",
                    artifact=_manifest(
                        f"{model.lower()}-grib-index",
                        f"{model.lower()}-index.v1",
                        acquisition.index_payload,
                    ),
                )
            )
            for ordinal, message in enumerate(acquisition.selected_messages):
                identity = f"m{message.row.message_number}-o{message.row.byte_offset}-n{ordinal}"
                roots.append(
                    SelectedSourceRoot(
                        model=model,
                        source_lead_hours=acquisition.forecast_hour,
                        field_role=(f"message:{message.canonical_variable_id}:record:{identity}"),
                        artifact=_manifest(
                            f"{model.lower()}-grib-selected-grib",
                            f"{model.lower()}-message.v1",
                            message.payload,
                        ),
                    )
                )
    order = {"HRRR": 0, "NBM": 1, "GFS": 2}
    return tuple(
        sorted(
            roots,
            key=lambda root: (order[root.model], root.source_lead_hours, root.field_role),
        )
    )


def _manifest(artifact_type: str, schema: str, payload: bytes) -> ArtifactManifest:
    return ArtifactManifest(
        artifact_id=ArtifactId.generate(),
        artifact_type=artifact_type,
        artifact_schema_version=schema,
        media_type="application/json",
        byte_size=len(payload),
        content_digest=Digest.of_bytes(payload),
        storage_uri=f"s3://bucket/{Digest.of_bytes(payload)}",
        created_at=CUTOFF,
        registered_at=CUTOFF,
        availability=Availability(
            available_at=CUTOFF, authority="mesoforge.phase2", method="byte-range-fetch"
        ),
        configuration_snapshot_id=SNAPSHOT_ID,
        configuration_digest=CONFIG_DIGEST,
        code_revision=CODE_REVISION,
        environment_digest=ENV_DIGEST,
        quality_state="valid",
        source_registration_digest=Digest.of_bytes(b"registration:" + payload[:32]),
        source_identity=SourceIdentity(
            authority="mesoforge.phase2", locator="phase2-run-spec://x", revision="production.v1"
        ),
    )


def _persisted(run_spec: dict[str, Any], stations: dict[str, Any]) -> Phase2PersistedRun:
    run_spec_bytes = JSON.serialize(run_spec)
    stations_bytes = JSON.serialize(stations)
    station_manifest = _manifest(
        "station-catalog-snapshot", "station-catalog-snapshot.v1", stations_bytes
    ).model_copy(update={"artifact_id": ArtifactId(run_spec["station_snapshot"]["artifact_id"])})
    return Phase2PersistedRun(
        run_spec=parse_run_spec(run_spec_bytes),
        station_snapshot=parse_station_snapshot(stations_bytes),
        run_spec_artifact=_manifest("phase2-run-spec", "phase2-run-spec.v1", run_spec_bytes),
        station_snapshot_artifact=station_manifest,
    )


def _observation_manifest(**changes: Any) -> ArtifactManifest:
    payload = b'[{"icaoId":"KCBG"}]'
    values: dict[str, Any] = {
        "artifact_id": ArtifactId.generate(),
        "artifact_type": "aviationweather-metar-response",
        "artifact_schema_version": "aviationweather-metar-response.v1",
        "media_type": "application/json",
        "byte_size": len(payload),
        "content_digest": Digest.of_bytes(payload),
        "storage_uri": "s3://bucket/metar",
        "created_at": VERIFY,
        "registered_at": VERIFY,
        "availability": Availability(
            available_at=VERIFY, authority="aviationweather.gov", method="get"
        ),
        "configuration_snapshot_id": SNAPSHOT_ID,
        "configuration_digest": CONFIG_DIGEST,
        "code_revision": CODE_REVISION,
        "environment_digest": ENV_DIGEST,
        "run_id": RUN_ID,
        "quality_state": "valid",
        "source_registration_digest": Digest.of_bytes(b"obs-registration"),
        "source_identity": SourceIdentity(
            authority="aviationweather.gov",
            locator=f"phase2-metar://{RUN_ID}",
            revision="phase2.v1",
        ),
    }
    values.update(changes)
    return ArtifactManifest.model_validate(values, strict=True)


# ----------------------------------------------------------------------
# Round-trip: the production writer and the replay loader agree
# ----------------------------------------------------------------------


def test_production_run_spec_round_trips_through_the_replay_contract() -> None:
    configuration, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)

    # The whole configuration survives, so no stage needs a live one.
    assert persisted.configuration == configuration
    assert persisted.blend_configuration == configuration.blend_configuration
    assert persisted.matching_policy == configuration.matching_policy
    assert persisted.metric_set == configuration.metric_set
    assert persisted.observation_normalization_policy == (
        configuration.observation_normalization_policy
    )
    assert persisted.aviationweather == configuration.aviationweather
    assert persisted.stations == configuration.stations
    assert persisted.point_extraction_policy == configuration.point_extraction_policy
    assert persisted.target_horizons == HORIZONS
    assert persisted.reference_time("HRRR") == REFERENCE


def test_persisted_leads_carry_every_message_evidence_normalization_needs() -> None:
    _configuration_unused, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    group = persisted.run_spec.group("GFS")
    assert group.selected
    assert tuple(group.source_lead_hours) == (0, 1)
    lead = group.lead(1)
    assert lead.endpoint == "aws"
    assert lead.resolved_index_url.endswith(".idx")
    messages = lead.messages_for("air_temperature_2m")
    assert len(messages) == 1
    assert messages[0].byte_end > messages[0].byte_start
    assert messages[0].inventory_row
    # The persisted record identity is exactly the registered artifact's
    # own field role, so replay can bind them without a live acquisition.
    assert messages[0].field_role.startswith("message:air_temperature_2m:record:m")


def test_unselected_model_persists_no_cycle_and_no_leads() -> None:
    configuration = _configuration()
    provider = _provider(configuration)
    acquisitions = _acquisitions(configuration)
    del acquisitions["NBM"]
    stations = provider._station_snapshot_payload()  # noqa: SLF001
    run_spec = provider._run_spec_payload(  # noqa: SLF001
        _request(),
        DiscoveryResult(selection_digest=Digest("sha256:" + "9" * 64)),
        acquisitions,
        _manifest(
            "station-catalog-snapshot",
            "station-catalog-snapshot.v1",
            JSON.serialize(stations),
        ),
        _source_roots(acquisitions),
    )
    persisted = _persisted(run_spec, stations)
    assert persisted.run_spec.selected_models == ("HRRR", "GFS")
    nbm = persisted.run_spec.group("NBM")
    assert nbm.selected is False
    assert nbm.leads == ()
    assert nbm.source_cycle_reference_time is None


def test_a_run_with_no_selected_model_is_a_valid_persisted_fact() -> None:
    """Every candidate cycle being unavailable is a real outcome, so the
    run spec must record it faithfully.

    The invariant that stops such a run ("normalized guidance must not be
    empty") belongs to the coordinator and fires identically for the
    original run and for a replay; the run spec must not disagree with
    what actually happened.
    """
    configuration = _configuration()
    provider = _provider(configuration)
    stations = provider._station_snapshot_payload()  # noqa: SLF001
    run_spec = provider._run_spec_payload(  # noqa: SLF001
        _request(),
        DiscoveryResult(selection_digest=Digest("sha256:" + "9" * 64)),
        {},
        _manifest(
            "station-catalog-snapshot",
            "station-catalog-snapshot.v1",
            JSON.serialize(stations),
        ),
        (),
    )
    persisted = _persisted(run_spec, stations)
    assert persisted.run_spec.selected_models == ()
    for model in ("HRRR", "NBM", "GFS"):
        group = persisted.run_spec.group(model)
        assert group.selected is False
        assert group.leads == ()
    # Nothing was acquired, so no source root may be claimed either.
    persisted.require_source_roots(())


# ----------------------------------------------------------------------
# Missing / incomplete persisted replay metadata fails closed
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda spec: spec.pop("configuration"), id="missing-configuration"),
        pytest.param(lambda spec: spec.pop("required_groups"), id="missing-groups"),
        pytest.param(lambda spec: spec.pop("request_digests"), id="missing-request-digests"),
        pytest.param(
            lambda spec: spec["required_groups"][0].pop("leads"), id="missing-lead-evidence"
        ),
        pytest.param(
            lambda spec: spec["required_groups"][0]["leads"][0].pop("selected_messages"),
            id="missing-message-evidence",
        ),
        pytest.param(
            lambda spec: spec["required_groups"][0]["leads"][0]["selected_messages"][0].pop(
                "inventory_row"
            ),
            id="missing-inventory-row",
        ),
        pytest.param(lambda spec: spec["required_groups"].pop(1), id="incomplete-model-coverage"),
    ],
)
def test_incomplete_persisted_run_spec_fails_closed(mutation: Any) -> None:
    _configuration_unused, run_spec, _stations = _payloads()
    broken = copy.deepcopy(run_spec)
    mutation(broken)
    with pytest.raises(Phase2ReplayContractError, match="missing or incomplete"):
        parse_run_spec(JSON.serialize(broken))


def test_run_spec_with_tampered_configuration_is_rejected() -> None:
    """A run spec whose embedded configuration no longer reproduces its
    own recorded digests is refused: the two halves can never disagree."""
    _configuration_unused, run_spec, _stations = _payloads()
    tampered = copy.deepcopy(run_spec)
    tampered["configuration"]["matching_policy"]["tolerance_minutes"] = 1.0
    with pytest.raises(Phase2ReplayContractError, match="missing or incomplete"):
        parse_run_spec(JSON.serialize(tampered))


def test_run_spec_group_policy_must_match_the_embedded_configuration() -> None:
    _configuration_unused, run_spec, _stations = _payloads()
    tampered = copy.deepcopy(run_spec)
    tampered["required_groups"][0]["max_age_hours"] = 99.0
    with pytest.raises(Phase2ReplayContractError, match="missing or incomplete"):
        parse_run_spec(JSON.serialize(tampered))


def test_station_snapshot_must_agree_with_the_run_spec_configuration() -> None:
    _configuration_unused, run_spec, stations = _payloads()
    tampered = copy.deepcopy(stations)
    tampered["stations"][0]["expected_latitude"] += 1.0
    with pytest.raises(ValueError, match="run-spec identity"):
        _persisted(run_spec, tampered)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda snap: snap.pop("stations"), id="missing-stations"),
        pytest.param(lambda snap: snap.pop("point_extraction_policy"), id="missing-policy"),
        pytest.param(lambda snap: snap["stations"][0].pop("provider_icao_id"), id="missing-icao"),
    ],
)
def test_incomplete_station_snapshot_fails_closed(mutation: Any) -> None:
    _configuration_unused, _run_spec, stations = _payloads()
    broken = copy.deepcopy(stations)
    mutation(broken)
    with pytest.raises(Phase2ReplayContractError, match="missing or incomplete"):
        parse_station_snapshot(JSON.serialize(broken))


# ----------------------------------------------------------------------
# Fail-closed binding
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"run_id": "run_00000000-0000-0000-0000-0000000000b2"}, "run_id"),
        ({"random_seed": 11}, "random_seed"),
        ({"verification_cutoff": VERIFY + timedelta(hours=2)}, "verification_cutoff"),
        ({"forecast_issue_time": ISSUE + timedelta(minutes=1)}, "forecast_issue_time"),
        ({"code_revision": "b" * 40}, "code_revision"),
        ({"lockfile_digest": Digest("sha256:" + "7" * 64)}, "lockfile_digest"),
        ({"environment_digest": Digest("sha256:" + "8" * 64)}, "environment_digest"),
    ],
)
def test_request_conflicting_with_the_run_spec_is_rejected(
    changes: dict[str, Any], expected: str
) -> None:
    _configuration_unused, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    conflicting = _request(**changes)
    with pytest.raises(Phase2ReplayIdentityError, match=expected):
        persisted.require_request_identity(
            run_id=str(conflicting.run_id),
            target_reference_time=conflicting.target_reference_time,
            forecast_issue_time=conflicting.forecast_issue_time,
            information_cutoff=conflicting.information_cutoff,
            verification_cutoff=conflicting.verification_cutoff,
            target_horizons=conflicting.target_horizons,
            random_seed=conflicting.random_seed,
            configuration_snapshot_id=str(conflicting.configuration_snapshot_id),
            configuration_digest=str(conflicting.configuration_digest),
            code_revision=conflicting.code_revision,
            environment_digest=str(conflicting.environment_digest),
            lockfile_digest=str(conflicting.lockfile_digest),
        )


def test_matching_request_is_accepted() -> None:
    _configuration_unused, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    request = _request()
    persisted.require_request_identity(
        run_id=str(request.run_id),
        target_reference_time=request.target_reference_time,
        forecast_issue_time=request.forecast_issue_time,
        information_cutoff=request.information_cutoff,
        verification_cutoff=request.verification_cutoff,
        target_horizons=request.target_horizons,
        random_seed=request.random_seed,
        configuration_snapshot_id=str(request.configuration_snapshot_id),
        configuration_digest=str(request.configuration_digest),
        code_revision=request.code_revision,
        environment_digest=str(request.environment_digest),
        lockfile_digest=str(request.lockfile_digest),
    )


def test_live_configuration_conflicting_with_the_persisted_one_is_rejected() -> None:
    configuration, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    # The exact live configuration is accepted...
    persisted.require_configuration_identity(
        Digest.of_bytes(JSON.serialize(configuration.model_dump(mode="json")))
    )
    # ...and any mutation of it is not.
    mutated = configuration.model_copy(
        update={
            "matching_policy": configuration.matching_policy.model_copy(
                update={"tolerance_minutes": 1.0}
            )
        }
    )
    with pytest.raises(Phase2ReplayIdentityError, match="conflicts with the persisted run"):
        persisted.require_configuration_identity(
            Digest.of_bytes(JSON.serialize(mutated.model_dump(mode="json")))
        )


def _expected_roots(persisted: Phase2PersistedRun) -> list[tuple[str, int, str, ArtifactId]]:
    return [
        (model, lead, role, identity.artifact_id)
        for model, lead, role, identity in persisted.expected_source_roots()
    ]


def test_pinned_source_roots_must_match_the_persisted_run_spec_exactly() -> None:
    _configuration_unused, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    roots = _expected_roots(persisted)
    persisted.require_source_roots(tuple(roots))

    with pytest.raises(Phase2ReplayIdentityError, match="do not match"):
        persisted.require_source_roots(tuple(roots[1:]))

    extra = (*roots, ("HRRR", 99, "index", ArtifactId.generate()))
    with pytest.raises(Phase2ReplayIdentityError, match="do not match"):
        persisted.require_source_roots(extra)

    with pytest.raises(Phase2ReplayIdentityError, match="duplicates"):
        persisted.require_source_roots((*roots, roots[0]))

    substituted = [*roots]
    model, lead, role, _artifact_id = substituted[0]
    substituted[0] = (model, lead, role, ArtifactId.generate())
    with pytest.raises(Phase2ReplayIdentityError, match="do not match"):
        persisted.require_source_roots(tuple(substituted))


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"artifact_type": "station-catalog-snapshot"}, "artifact type"),
        ({"artifact_schema_version": "aviationweather-metar-response.v0"}, "schema"),
        ({"quality_state": "partial"}, "not valid"),
        ({"configuration_snapshot_id": "cfg_sha256_" + "2" * 64}, "configuration snapshot"),
        ({"configuration_digest": Digest("sha256:" + "6" * 64)}, "configuration digest"),
        ({"code_revision": "c" * 40}, "code revision"),
        ({"environment_digest": Digest("sha256:" + "5" * 64)}, "environment digest"),
        ({"run_id": "run_00000000-0000-0000-0000-0000000000ff"}, "not bound to run"),
        (
            {
                "availability": Availability(
                    available_at=VERIFY + timedelta(hours=1),
                    authority="aviationweather.gov",
                    method="get",
                )
            },
            "available after",
        ),
        (
            {
                "source_identity": SourceIdentity(
                    authority="example.invalid", locator="metar", revision="phase2.v1"
                )
            },
            "was not issued by",
        ),
        (
            {
                "source_identity": SourceIdentity(
                    authority="aviationweather.gov",
                    locator="phase2-metar://run_00000000-0000-0000-0000-0000000000ff",
                    revision="phase2.v1",
                )
            },
            "locator",
        ),
    ],
)
def test_recorded_observations_that_do_not_belong_to_the_run_are_rejected(
    changes: dict[str, Any], expected: str
) -> None:
    _configuration_unused, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    persisted.require_recorded_observations((_observation_manifest(),))
    with pytest.raises(Phase2ReplayIdentityError, match=expected):
        persisted.require_recorded_observations((_observation_manifest(**changes),))


def test_replay_requires_at_least_one_recorded_observation() -> None:
    _configuration_unused, run_spec, stations = _payloads()
    persisted = _persisted(run_spec, stations)
    with pytest.raises(Phase2ReplayIdentityError, match="at least one recorded observation"):
        persisted.require_recorded_observations(())


def test_derived_or_mistyped_root_manifests_are_rejected() -> None:
    _configuration_unused, run_spec, stations = _payloads()
    run_spec_bytes = JSON.serialize(run_spec)
    valid = _manifest("phase2-run-spec", "phase2-run-spec.v1", run_spec_bytes)
    require_run_spec_artifact(valid)

    with pytest.raises(Phase2ReplayContractError, match="expected artifact type"):
        require_run_spec_artifact(valid.model_copy(update={"artifact_type": "canonical-guidance"}))
    with pytest.raises(Phase2ReplayContractError, match="expected schema"):
        require_run_spec_artifact(
            valid.model_copy(update={"artifact_schema_version": "phase2-run-spec.v0"})
        )
    with pytest.raises(Phase2ReplayContractError, match="must be valid"):
        require_run_spec_artifact(valid.model_copy(update={"quality_state": "invalid"}))
    with pytest.raises(Phase2ReplayContractError, match="must be a source artifact"):
        require_run_spec_artifact(
            valid.model_copy(update={"source_registration_digest": None, "source_identity": None})
        )

    stations_manifest = _manifest(
        "station-catalog-snapshot", "station-catalog-snapshot.v1", JSON.serialize(stations)
    )
    require_station_snapshot_artifact(stations_manifest)
    with pytest.raises(Phase2ReplayContractError, match="expected artifact type"):
        require_station_snapshot_artifact(valid)


# ----------------------------------------------------------------------
# Replay wiring is structurally offline
# ----------------------------------------------------------------------


def test_replay_provider_refuses_discovery_and_acquisition() -> None:
    provider = Phase2UnavailableProvider()
    request = _request()
    with pytest.raises(Phase2ReplayIdentityError, match="must not perform source discovery"):
        provider.discover(request)
    with pytest.raises(Phase2ReplayIdentityError, match="must not perform source acquisition"):
        provider.acquire(
            request,
            DiscoveryResult(selection_digest=Digest("sha256:" + "9" * 64)),
            artifact_service=None,  # type: ignore[arg-type]
        )


def test_replay_transport_refuses_every_request() -> None:
    transport = UnavailableHttpTransport()
    with pytest.raises(Phase2ReplayIdentityError, match="no network access"):
        transport.get("https://example/metar")
    with pytest.raises(Phase2ReplayIdentityError, match="no network access"):
        transport.head("https://example/metar")
