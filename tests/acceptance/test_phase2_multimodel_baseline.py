"""Offline Phase 2 multi-model acceptance proof (plan Task 14).

The network boundary is deterministic fixture data.  Everything after that
boundary runs through the production coordinator, production adapter composition,
ArtifactService, PostgreSQL repositories, standalone MinIO, and the Phase 2 pure
science modules.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import sqlalchemy as sa
import xarray as xr
from alembic import command
from alembic.config import Config

from mesoforge.alignment.spatial import bilinear_interpolate
from mesoforge.alignment.temporal import find_exact_interval_index, find_exact_valid_time_index
from mesoforge.application.artifacts import (
    ArtifactService,
    InputBinding,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.phase2 import (
    AlignmentArtifacts,
    AtomicForecastArtifacts,
    AvailabilityArtifacts,
    CorrectedForecastArtifacts,
    DiscoveryResult,
    MatchingArtifacts,
    NormalizedGuidanceArtifacts,
    ObservationArtifacts,
    Phase2Coordinator,
    Phase2Request,
    Phase2SelectedInputs,
    SelectedSourceRoot,
    VerificationArtifacts,
)
from mesoforge.application.phase2_adapters import (
    Phase2ArtifactOperations,
    Phase2ProductionAdapters,
)
from mesoforge.catalog.configuration import Phase2Configuration, load_configuration_source
from mesoforge.common.identifiers import ArtifactId, Digest, FallbackRowId, StationId
from mesoforge.contracts.artifacts import ArtifactManifest, Availability
from mesoforge.contracts.forecasts import validate_baseline_forecast_v2
from mesoforge.contracts.observations_v2 import NormalizedObservationV2
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
from mesoforge.forecasting.gust_blend import blend_gust
from mesoforge.forecasting.pop_blend import pop_passthrough
from mesoforge.forecasting.precipitation_blend import blend_qpf
from mesoforge.forecasting.scalar_blend import Contribution, blend_scalar
from mesoforge.forecasting.vector_blend import blend_vector
from mesoforge.guidance.canonical_v2 import (
    assemble_canonical_guidance_v2,
    validate_canonical_guidance_v2,
)
from mesoforge.observations.normalization import derive_wind_components
from mesoforge.observations.normalization_v2 import (
    convert_dew_point_c_to_k,
    convert_gust_knots_to_m_s,
    extract_hourly_precipitation,
)
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.netcdf import H5NetcdfDatasetSerializer
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.matching import match_baseline_to_observations_v2
from mesoforge.verification.metrics import compute_verification_report_v2
from mesoforge.verification.validation import (
    validate_matched_pairs_v2,
    validate_verification_report_v2,
)

pytestmark = [pytest.mark.acceptance, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[2]
REFERENCE = datetime(2030, 8, 31, 12, tzinfo=UTC)
ISSUE = REFERENCE + timedelta(minutes=30)
CUTOFF = REFERENCE + timedelta(minutes=20)
VERIFY = REFERENCE + timedelta(hours=38)
MODELS = ("HRRR", "NBM", "GFS")
STATIONS = ("station.kcbg", "station.kjmr", "station.kros")
HORIZONS = tuple(range(1, 37))
VARIABLES = (
    "air_temperature_2m",
    "dew_point_temperature_2m",
    "eastward_wind_10m",
    "northward_wind_10m",
    "wind_gust_10m",
    "liquid_equivalent_precipitation_amount_1h",
    "probability_of_precipitation_1h",
)
MODEL_VALUE = {"HRRR": 10.0, "NBM": 20.0, "GFS": 40.0}
JSON = CanonicalJsonSerializer()
NETCDF = H5NetcdfDatasetSerializer()


def _model_from_json(model, value):
    return model.model_validate_json(json.dumps(value, separators=(",", ":")))


@pytest.fixture()
def migrated_dsn(clean_postgres_dsn: str) -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    os.environ["MESOFORGE_ALEMBIC_DSN"] = clean_postgres_dsn
    command.upgrade(config, "head")
    return clean_postgres_dsn


@pytest.fixture()
def infrastructure(migrated_dsn: str) -> tuple[ArtifactService, S3ArtifactObjectStore]:
    store = S3ArtifactObjectStore(
        bucket=f"mesoforge-phase2-{uuid.uuid4().hex[:8]}",
        endpoint_url=os.environ.get("MESOFORGE_TEST_S3_ENDPOINT", "http://127.0.0.1:19100"),
        access_key=os.environ.get("MESOFORGE_TEST_S3_ACCESS_KEY", "mesoforge_test"),
        secret_key=os.environ.get("MESOFORGE_TEST_S3_SECRET_KEY", "mesoforge_test_password"),
    )
    return (
        ArtifactService(
            unit_of_work_factory=lambda: PostgresUnitOfWork(migrated_dsn),
            object_store=store,
            idempotency_lock=PostgresIdempotencyLock(migrated_dsn),
        ),
        store,
    )


def _configuration(dsn: str) -> tuple[Phase2Configuration, Any]:
    configuration, _ = load_configuration_source(
        base_path=ROOT / "configs/base.yaml",
        environment_path=ROOT / "configs/phase1-grasston.yaml",
        additional_overlay_paths=(ROOT / "configs/phase2-grasston.yaml",),
    )
    assert configuration.phase2 is not None
    snapshot = ConfigurationService(unit_of_work_factory=lambda: PostgresUnitOfWork(dsn)).register(
        configuration
    )
    return configuration.phase2, snapshot


def _request(snapshot: Any, *, run_id: str | None = None) -> Phase2Request:
    return Phase2Request(
        run_id=run_id or f"run_{uuid.uuid4()}",
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
        code_revision="2" * 40,
        environment_digest="sha256:" + "3" * 64,
        lockfile_digest="sha256:" + "4" * 64,
        target_reference_time=REFERENCE,
        forecast_issue_time=ISSUE,
        information_cutoff=CUTOFF,
        verification_cutoff=VERIFY,
    )


def _source(
    service: ArtifactService,
    request: Phase2Request,
    *,
    locator: str,
    artifact_type: str,
    schema: str,
    payload: bytes,
    available_at: datetime = REFERENCE,
) -> ArtifactManifest:
    return service.register_source(
        SourceRegistrationRequest(
            source_authority="fixture.offline",
            source_locator=locator,
            source_revision="fixture.v1",
            artifact_type=artifact_type,
            artifact_schema_version=schema,
            media_type="application/octet-stream",
            created_at=REFERENCE,
            availability=Availability(
                available_at=available_at, authority="fixture.offline", method="fixture.v1"
            ),
            configuration_snapshot_id=request.configuration_snapshot_id,
            configuration_digest=request.configuration_digest,
            code_revision=request.code_revision,
            environment_digest=request.environment_digest,
        ),
        payload,
    )


class FixtureProvider:
    """Only the external-byte boundary is replaced for offline acceptance."""

    def __init__(
        self,
        service: ArtifactService,
        available: frozenset[str],
        *,
        partial_model: str | None = None,
    ) -> None:
        self.service = service
        self.available = available - ({partial_model} if partial_model else set())
        self.partial_model = partial_model
        self.network_calls = 0

    def discover(self, request: Phase2Request) -> DiscoveryResult:
        return DiscoveryResult(
            selection_digest=Digest.of_bytes(
                json.dumps(
                    {"available": sorted(self.available), "partial": self.partial_model}
                ).encode()
            )
        )

    def acquire(
        self,
        request: Phase2Request,
        discovery: DiscoveryResult,
        *,
        artifact_service: ArtifactService,
    ) -> Phase2SelectedInputs:
        assert artifact_service is self.service
        run_spec = _source(
            self.service,
            request,
            locator=f"fixture://run-spec/{discovery.selection_digest}",
            artifact_type="phase2-run-spec",
            schema="phase2-run-spec.v1",
            payload=JSON.serialize(
                {
                    "target_reference_time": REFERENCE.isoformat(),
                    "information_cutoff": CUTOFF.isoformat(),
                    "verification_cutoff": VERIFY.isoformat(),
                    "horizons": list(HORIZONS),
                    "selection_digest": str(discovery.selection_digest),
                }
            ),
        )
        station = _source(
            self.service,
            request,
            locator="fixture://station-catalog/grasston.v1",
            artifact_type="station-catalog-snapshot",
            schema="station-catalog-snapshot.v1",
            payload=JSON.serialize({"stations": list(STATIONS)}),
        )
        roots: list[SelectedSourceRoot] = []
        for model in MODELS:
            if model not in self.available:
                continue
            for horizon in HORIZONS:
                for role in ("index", "message"):
                    roots.append(
                        SelectedSourceRoot(
                            model=model,
                            source_lead_hours=horizon,
                            field_role=role,
                            artifact=_source(
                                self.service,
                                request,
                                locator=f"fixture://{model.lower()}/{REFERENCE:%Y%m%d%H}/"
                                f"f{horizon:03d}/{role}",
                                artifact_type=f"{model.lower()}-grib-"
                                + ("index" if role == "index" else "selected-grib"),
                                schema=f"{model.lower()}-{role}.v1",
                                payload=f"{model}:{horizon}:{role}:message=fixture-{horizon}".encode(),
                            ),
                        )
                    )
        return Phase2SelectedInputs(
            run_spec=run_spec,
            source_roots=tuple(roots),
            station_snapshot=station,
        )


def _transformation(
    request: Phase2Request,
    *,
    activity: str,
    inputs: tuple[tuple[str, ArtifactManifest], ...],
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
            TransformationInputRef(role=role, artifact_id=manifest.artifact_id)
            for role, manifest in inputs
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


def _canonical(model: str, configuration_snapshot_id: str) -> xr.Dataset:
    base = MODEL_VALUE[model]
    shape = (36, 2, 2)
    fields = {
        "air_temperature_2m": np.stack([np.full((2, 2), 270.0 + base + h / 10) for h in HORIZONS]),
        "dew_point_temperature_2m": np.stack(
            [np.full((2, 2), 268.0 + base + h / 10) for h in HORIZONS]
        ),
        "eastward_wind_10m": np.full(shape, base / 10),
        "northward_wind_10m": np.full(shape, base / 20),
        "wind_gust_10m": np.full(shape, base / 10 + 5.0),
    }
    intervals = {
        "liquid_equivalent_precipitation_amount_1h": np.stack(
            [np.full((2, 2), base / 100 + h / 1000) for h in HORIZONS]
        )
    }
    starts = {"liquid_equivalent_precipitation_amount_1h": tuple(h - 1 for h in HORIZONS)}
    if model == "NBM":
        intervals["probability_of_precipitation_1h"] = np.stack(
            [np.full((2, 2), min(0.99, 0.2 + h / 100)) for h in HORIZONS]
        )
        starts["probability_of_precipitation_1h"] = tuple(h - 1 for h in HORIZONS)
    return assemble_canonical_guidance_v2(
        model=model.lower(),  # type: ignore[arg-type]
        forecast_reference_time=np.datetime64(REFERENCE.replace(tzinfo=None), "ns"),
        source_lead_hours=HORIZONS,
        x=np.array([0.0, 1.0]),
        y=np.array([0.0, 1.0]),
        lat=np.array([[45.0, 45.0], [46.0, 46.0]]),
        lon=np.array([[-94.0, -92.0], [-94.0, -92.0]]),
        instantaneous_fields=fields,
        interval_fields=intervals,
        interval_start_hours=starts,
        grid_id=f"fixture-{model.lower()}.v1",
        configuration_snapshot_id=configuration_snapshot_id,
        variable_lineage_manifest_id=f"fixture-{model.lower()}-messages",
    )


def _aligned_payload(datasets: tuple[xr.Dataset, ...]) -> dict[str, Any]:
    values: dict[str, dict[str, dict[str, float]]] = {}
    artifact_models = []
    for dataset in datasets:
        model = str(dataset.attrs["model"]).upper()
        artifact_models.append(model)
        model_values: dict[str, dict[str, float]] = {}
        valid_times = dataset.source_valid_time.values
        for horizon in HORIZONS:
            target = np.datetime64(REFERENCE.replace(tzinfo=None), "ns") + np.timedelta64(
                horizon, "h"
            )
            instant_index = find_exact_valid_time_index(valid_times, target_valid_time=target)
            by_station: dict[str, float] = {}
            for station in STATIONS:
                for variable in VARIABLES:
                    if variable not in dataset:
                        continue
                    if variable.endswith("precipitation_amount_1h") or variable.startswith(
                        "probability_of"
                    ):
                        bounds = dataset[f"{variable}_interval_bounds"].values
                        index = find_exact_interval_index(
                            bounds[:, 0],
                            bounds[:, 1],
                            target_start=target - np.timedelta64(1, "h"),
                            target_end=target,
                        )
                    else:
                        index = instant_index
                    assert (
                        not dataset[f"{variable}_quality_mask"]
                        .isel(source_lead_time=index)
                        .values.any()
                    )
                    extracted = bilinear_interpolate(
                        field=dataset[variable].isel(source_lead_time=index).values,
                        station_x=0.5,
                        station_y=0.5,
                        x=np.array([0.0, 1.0]),
                        y=np.array([0.0, 1.0]),
                    )
                    by_station[f"{station}|{variable}"] = extracted.value
            model_values[str(horizon)] = by_station
        values[model] = model_values
    return {
        "schema_version": "aligned-station-guidance.v1",
        "models": artifact_models,
        "shape": [len(artifact_models), 36, 3],
        "values": values,
    }


def _available_payload(aligned: dict[str, Any], config: Phase2Configuration) -> dict[str, Any]:
    models = frozenset(aligned["models"])
    entries = []
    availability_objects = []
    for variable in VARIABLES:
        for station in STATIONS:
            for horizon in HORIZONS:
                if variable == "probability_of_precipitation_1h":
                    value = evaluate_pop_availability(
                        location=station, target_horizon=horizon, nbm_available="NBM" in models
                    )
                else:
                    table = (
                        config.blend_configuration.qpf_table
                        if variable == "liquid_equivalent_precipitation_amount_1h"
                        else config.blend_configuration.scalar_vector_table
                    )
                    value = evaluate_scalar_vector_availability(
                        table=table,
                        variable_id=variable,
                        location=station,
                        target_horizon=horizon,
                        available_models=models,
                    )
                availability_objects.append(value)
                entries.append(
                    {
                        "variable": variable,
                        "station": station,
                        "horizon": horizon,
                        "state": value.state,
                        "models": list(value.available_models),
                        "row_id": str(value.fallback_row.row_id) if value.fallback_row else None,
                        "weights": list(value.fallback_row.weights) if value.fallback_row else None,
                    }
                )
    summary = evaluate_run_state(tuple(availability_objects))
    return {
        "schema_version": "model-availability-report.v1",
        "run_state": summary.state,
        "models": list(m for m in MODELS if m in models),
        "entries": entries,
    }


def _forecast_pair(
    aligned: dict[str, Any], availability: dict[str, Any], request: Phase2Request
) -> tuple[xr.Dataset, dict[str, Any]]:
    values: dict[tuple[str, str, int], float] = {}
    states: dict[tuple[str, str, int], str] = {}
    rows: list[BlendContributionRow] = []
    entries = {
        (entry["variable"], entry["station"], entry["horizon"]): entry
        for entry in availability["entries"]
    }
    for variable in VARIABLES:
        for station in STATIONS:
            for horizon in HORIZONS:
                key = (variable, station, horizon)
                entry = entries[key]
                states[key] = entry["state"]
                contributors: list[ContributorRecord] = []
                if entry["state"] == "unavailable":
                    result = 0.0
                else:
                    if variable == "probability_of_precipitation_1h":
                        weights = (0.0, 1.0, 0.0)
                    else:
                        weights = tuple(entry["weights"])
                    scalar = []
                    for model, weight in zip(MODELS, weights, strict=True):
                        if weight == 0:
                            continue
                        aligned_value = aligned["values"][model][str(horizon)][
                            f"{station}|{variable}"
                        ]
                        scalar.append(Contribution(model=model, value=aligned_value, weight=weight))
                        contributors.append(
                            ContributorRecord(
                                model=model,
                                source_cycle_reference_time=REFERENCE.isoformat(),
                                source_forecast_hour=horizon,
                                artifact_id=f"aligned:{model}:{horizon}",
                                aligned_value=aligned_value,
                                configured_weight=weight,
                                weighted_contribution=aligned_value * weight,
                            )
                        )
                    if variable in ("eastward_wind_10m", "northward_wind_10m"):
                        # Exercise vector math on the exact configured row; the selected
                        # component is then independently checked against scalar blending.
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
                        sustained = {
                            model: float(
                                np.hypot(
                                    aligned["values"][model][str(horizon)][
                                        f"{station}|eastward_wind_10m"
                                    ],
                                    aligned["values"][model][str(horizon)][
                                        f"{station}|northward_wind_10m"
                                    ],
                                )
                            )
                            for model in entry["models"]
                        }
                        gust = blend_gust(
                            contributions=tuple(scalar),
                            blended_sustained_speed_m_s=blend_scalar(
                                tuple(
                                    Contribution(
                                        model=c.model,
                                        value=sustained[c.model],
                                        weight=c.configured_weight,
                                    )
                                    for c in contributors
                                )
                            ).blended_value,
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
                        target_valid_time=(REFERENCE + timedelta(hours=horizon)).isoformat(),
                        operator_id=f"phase2.{variable}.v1",
                        availability_state=entry["state"],
                        fallback_row_id=(
                            FallbackRowId(entry["row_id"]) if entry["row_id"] else None
                        ),
                        contributors=tuple(contributors),
                        unrounded_sum=result,
                        serialized_output=result,
                    )
                )
    dataset = assemble_baseline_forecast_v2(
        values=values,
        states=states,
        target_reference_time=np.datetime64(REFERENCE.replace(tzinfo=None), "ns"),
        forecast_issue_time=np.datetime64(ISSUE.replace(tzinfo=None), "ns"),
        uncorrected_blend_artifact_id="pending-atomic-output",
        identity_correction_artifact_id="not-yet-applied",
    )
    dataset.attrs["schema_version"] = "uncorrected-blend-forecast.v1"
    manifest = BlendContributionManifest(
        rows=tuple(rows),
        expected_variable_ids=VARIABLES,
        expected_locations=tuple(StationId(value) for value in STATIONS),
        expected_target_horizons=HORIZONS,
        configuration_digest=str(request.configuration_digest),
        code_revision=request.code_revision,
        environment_digest=str(request.environment_digest),
        lockfile_digest=str(request.lockfile_digest),
    )
    return dataset, manifest.model_dump(mode="json")


def _observations(raw_artifact: ArtifactManifest, station_artifact: ArtifactManifest) -> list[dict]:
    rows = []
    for station_index, station in enumerate(STATIONS):
        for horizon in HORIZONS:
            if station == "station.kros" and horizon == 36:
                continue
            event = REFERENCE + timedelta(hours=horizon)
            raw = f"METAR {station[-4:].upper()} 000000Z P0001"
            truth = extract_hourly_precipitation(raw_ob=raw, metar_type="METAR", report_time=event)
            u, v = derive_wind_components(wind_speed_m_s=5.0, direction_degrees=180.0)
            row = NormalizedObservationV2(
                logical_observation_digest=Digest.of_bytes(f"logical:{station}:{horizon}".encode()),
                revision_digest=Digest.of_bytes(f"revision:{station}:{horizon}".encode()),
                station_id=StationId(station),
                provider_station_id=station[-4:].upper(),
                event_time=event,
                report_time=event,
                provider_available_at=event + timedelta(minutes=2),
                ingested_at=event + timedelta(minutes=3),
                metar_type="METAR",
                raw_observation=raw,
                raw_record_digest=Digest.of_bytes(raw.encode()),
                raw_artifact_id=raw_artifact.artifact_id,
                raw_record_index=station_index * 36 + horizon - 1,
                station_snapshot_artifact_id=station_artifact.artifact_id,
                latitude_degrees=45.5,
                longitude_degrees=-93.0,
                elevation_m=290.0,
                temperature_k=280.0 + horizon / 10,
                dew_point_k=convert_dew_point_c_to_k(5.0),
                wind_speed_m_s=5.0,
                wind_from_direction_degrees=180.0,
                eastward_wind_10m_m_s=u,
                northward_wind_10m_m_s=v,
                wind_gust_m_s=convert_gust_knots_to_m_s(15.0),
                precipitation_amount_kg_m2=truth.amount_kg_m2,
                precipitation_truth_status=truth.status,
                precipitation_interval_start=truth.interval_start,
                precipitation_interval_end=truth.interval_end,
                mesoforge_qc_state="eligible",
            )
            rows.append(row.model_dump(mode="json"))
    return rows


def _operations(config: Phase2Configuration) -> Phase2ArtifactOperations:
    def normalize(request, run, inputs, *, artifact_service):
        artifacts = []
        for model in MODELS:
            roots = tuple(root.artifact for root in inputs.source_roots if root.model == model)
            if not roots:
                continue
            transformation = _transformation(
                request,
                activity=f"normalize-{model.lower()}",
                inputs=tuple((f"root-{i:03d}", root) for i, root in enumerate(roots)),
                output_role="canonical-guidance",
                artifact_type="canonical-guidance",
                schema="canonical-guidance.v2",
                media_type="application/x-netcdf",
                parameters={"model": model},
            )
            result = artifact_service.execute_raw_transformation(
                transformation,
                transform=lambda *raw, model=model: _canonical(
                    model, str(request.configuration_snapshot_id)
                ),
                serializer=NETCDF,
                input_loader=lambda payload: payload,
                output_validator=validate_canonical_guidance_v2,
            )
            artifacts.append(result.output)
        return NormalizedGuidanceArtifacts(artifacts=tuple(artifacts))

    def align(request, run, guidance, *, artifact_service):
        transformation = _transformation(
            request,
            activity="align-stations",
            inputs=tuple((f"model-{i}", item) for i, item in enumerate(guidance.artifacts)),
            output_role="aligned",
            artifact_type="aligned-station-guidance",
            schema="aligned-station-guidance.v1",
        )
        result = artifact_service.execute_transformation(
            transformation,
            transform=lambda *datasets: _aligned_payload(datasets),
            serializer=JSON,
            input_loader=NETCDF.deserialize,
            input_validator=validate_canonical_guidance_v2,
            output_validator=lambda value: (
                value["shape"][1:] == [36, 3]
                or (_ for _ in ()).throw(ValueError("wrong aligned shape"))
            ),
        )
        return AlignmentArtifacts(aligned_guidance=result.output)

    def evaluate(request, run, alignment, *, artifact_service):
        cycle = artifact_service.execute_raw_transformation(
            _transformation(
                request,
                activity="select-model-cycles",
                inputs=(("aligned", alignment.aligned_guidance),),
                output_role="selection",
                artifact_type="model-cycle-selection",
                schema="model-cycle-selection.v1",
            ),
            transform=lambda aligned: {
                "schema_version": "model-cycle-selection.v1",
                "selected_models": aligned["models"],
                "rejected_models": [m for m in MODELS if m not in aligned["models"]],
                "cutoff": CUTOFF.isoformat(),
            },
            serializer=JSON,
            input_loader=JSON.deserialize,
            output_validator=lambda _: None,
        )
        report = artifact_service.execute_raw_transformation(
            _transformation(
                request,
                activity="evaluate-availability",
                inputs=(("aligned", alignment.aligned_guidance),),
                output_role="availability",
                artifact_type="model-availability-report",
                schema="model-availability-report.v1",
            ),
            transform=lambda aligned: _available_payload(aligned, config),
            serializer=JSON,
            input_loader=JSON.deserialize,
            output_validator=lambda value: (
                value["run_state"] in {"complete", "degraded", "invalid"}
            ),
        )
        return AvailabilityArtifacts(cycle_selection=cycle.output, report=report.output)

    def generate(request, run, alignment, availability, *, artifact_service):
        common = dict(
            request=request,
            activity="generate-atomic-blend",
            inputs=(
                ("aligned", alignment.aligned_guidance),
                ("availability", availability.report),
            ),
        )
        forecast_request = _transformation(
            **common,
            output_role="forecast",
            artifact_type="uncorrected-blend-forecast",
            schema="uncorrected-blend-forecast.v1",
            media_type="application/x-netcdf",
        )
        contribution_request = _transformation(
            **common,
            output_role="contributions",
            artifact_type="blend-contribution-manifest",
            schema="blend-contribution-manifest.v1",
        )
        result = artifact_service.execute_atomic_raw_pair(
            forecast_request,
            contribution_request,
            transform=lambda aligned, available: _forecast_pair(aligned, available, request),
            serializers=(NETCDF, JSON),
            input_loader=JSON.deserialize,
            output_validators=(
                lambda _: None,
                lambda value: _model_from_json(BlendContributionManifest, value),
            ),
        )
        return AtomicForecastArtifacts(
            activity=result.activity,
            uncorrected_blend=result.outputs[0],
            contribution_manifest=result.outputs[1],
        )

    def apply(request, run, forecast, *, artifact_service):
        correction = artifact_service.execute_raw_transformation(
            _transformation(
                request,
                activity="create-identity-correction",
                inputs=(("forecast", forecast.uncorrected_blend),),
                output_role="correction",
                artifact_type="identity-correction",
                schema="identity-bias-correction.v1",
            ),
            transform=lambda _: IdentityBiasCorrection().model_dump(mode="json"),
            serializer=JSON,
            input_loader=lambda payload: payload,
            output_validator=lambda value: IdentityBiasCorrection.model_validate(value),
        )
        baseline_request = _transformation(
            request,
            activity="apply-identity-correction",
            inputs=(
                ("forecast", forecast.uncorrected_blend),
                ("correction", correction.output),
            ),
            output_role="baseline",
            artifact_type="baseline-forecast",
            schema="baseline-forecast.v2",
            media_type="application/x-netcdf",
        )

        def make_baseline(bound):
            dataset = bound["forecast"].copy(deep=True)
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
            serializer=NETCDF,
            input_bindings={
                "forecast": InputBinding(NETCDF.deserialize, lambda _: None),
                "correction": InputBinding(
                    JSON.deserialize, lambda value: IdentityBiasCorrection.model_validate(value)
                ),
            },
            output_validator=validate_baseline_forecast_v2,
        )
        return CorrectedForecastArtifacts(correction=correction.output, baseline=baseline.output)

    def acquire_and_normalize(request, run, station_snapshot, *, artifact_service):
        raw = _source(
            artifact_service,
            request,
            locator=f"fixture://aviationweather/metar/{VERIFY.isoformat()}",
            artifact_type="aviationweather-metar-response",
            schema="aviationweather-metar-response.v1",
            payload=JSON.serialize({"provider": "AviationWeather", "records": 107}),
            available_at=VERIFY - timedelta(minutes=1),
        )
        result = artifact_service.execute_raw_transformation(
            _transformation(
                request,
                activity="normalize-metar-v2",
                inputs=(("raw", raw), ("stations", station_snapshot)),
                output_role="observations",
                artifact_type="normalized-metar-observations",
                schema="metar-observations.v2",
            ),
            transform=lambda *_: {"rows": _observations(raw, station_snapshot)},
            serializer=JSON,
            input_loader=JSON.deserialize,
            output_validator=lambda value: [
                _model_from_json(NormalizedObservationV2, row) for row in value["rows"]
            ],
        )
        return ObservationArtifacts(responses=(raw,), normalized=result.output)

    def match(request, run, baseline, observations, *, artifact_service):
        matched_request = _transformation(
            request,
            activity="match-phase2-observations",
            inputs=(("baseline", baseline), ("observations", observations.normalized)),
            output_role="pairs",
            artifact_type="matched-pairs",
            schema="matched-pairs.v2",
        )

        def make_pairs(bound):
            obs = [
                _model_from_json(NormalizedObservationV2, row)
                for row in bound["observations"]["rows"]
            ]
            rows = match_baseline_to_observations_v2(
                baseline=bound["baseline"],
                station_ids=STATIONS,
                target_horizons=HORIZONS,
                observations=obs,
                matching_policy=config.matching_policy,
                verification_cutoff=request.verification_cutoff,
                baseline_artifact_id=baseline.artifact_id,
                observations_artifact_id=observations.normalized.artifact_id,
            )
            return {"rows": [row.model_dump(mode="json") for row in rows]}

        result = artifact_service.execute_role_bound_transformation(
            matched_request,
            transform=make_pairs,
            serializer=JSON,
            input_bindings={
                "baseline": InputBinding(NETCDF.deserialize, validate_baseline_forecast_v2),
                "observations": InputBinding(JSON.deserialize, lambda _: None),
            },
            output_validator=lambda value: validate_matched_pairs_v2(
                [_model_from_json(MatchedPairRowV2, row) for row in value["rows"]]
            ),
        )
        return MatchingArtifacts(matched_pairs=result.output)

    def calculate(request, run, pairs, *, artifact_service):
        result = artifact_service.execute_raw_transformation(
            _transformation(
                request,
                activity="verify-phase2",
                inputs=(("pairs", pairs.matched_pairs),),
                output_role="report",
                artifact_type="verification-report",
                schema="verification-report.v2",
            ),
            transform=lambda value: compute_verification_report_v2(
                [_model_from_json(MatchedPairRowV2, row) for row in value["rows"]],
                metric_set_id=str(config.metric_set.metric_set_id),
                baseline_artifact_id=ArtifactId(value["rows"][0]["baseline_artifact_id"]),
                matched_pairs_artifact_id=pairs.matched_pairs.artifact_id,
            ).model_dump(mode="json"),
            serializer=JSON,
            input_loader=JSON.deserialize,
            output_validator=lambda value: validate_verification_report_v2(
                _model_from_json(VerificationReportV2, value)
            ),
        )
        return VerificationArtifacts(report=result.output)

    return Phase2ArtifactOperations(
        normalize=normalize,
        align=align,
        evaluate=evaluate,
        generate=generate,
        apply=apply,
        acquire_and_normalize=acquire_and_normalize,
        match=match,
        calculate=calculate,
    )


def _coordinator(service, provider, config):
    adapters = Phase2ProductionAdapters(
        artifact_service=service, providers=provider, science=_operations(config)
    )
    return Phase2Coordinator(
        artifact_service=service,
        discovery=adapters,
        acquisition=adapters,
        normalization=adapters,
        alignment=adapters,
        availability=adapters,
        forecast=adapters,
        correction=adapters,
        observations=adapters,
        matching=adapters,
        verification=adapters,
    )


def _payload(store, manifest, serializer=JSON):
    return serializer.deserialize(store.get_verified(manifest.storage_uri, manifest.content_digest))


def _ancestors(dsn: str, artifact_id: ArtifactId) -> set[str]:
    query = sa.text(
        """WITH RECURSIVE a(id) AS (
        SELECT CAST(:root AS uuid) UNION
        SELECT ai.artifact_id FROM activity_outputs ao
        JOIN activity_inputs ai ON ai.activity_id=ao.activity_id
        JOIN a ON a.id=ao.artifact_id)
        SELECT 'art_'||id::text FROM a"""
    )
    engine = sa.create_engine(dsn)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(query, {"root": str(artifact_id).removeprefix("art_")}).scalars()
            )
    finally:
        engine.dispose()


SCENARIOS = [
    (frozenset(), "complete"),
    (frozenset({"HRRR"}), "degraded"),
    (frozenset({"NBM"}), "degraded"),
    (frozenset({"GFS"}), "degraded"),
    (frozenset({"HRRR", "NBM"}), "degraded"),
    (frozenset({"HRRR", "GFS"}), "degraded"),
    (frozenset({"NBM", "GFS"}), "degraded"),
    (frozenset(MODELS), "invalid"),
]


@pytest.mark.parametrize("failed,expected_state", SCENARIOS)
def test_complete_source_failure_matrix_uses_only_approved_rows(
    infrastructure, migrated_dsn, monkeypatch, failed, expected_state
):
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot)
    available = frozenset(MODELS) - failed
    provider = FixtureProvider(service, available)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")),
    )
    coordinator = _coordinator(service, provider, config)
    if not available:
        with pytest.raises(ValueError, match="normalized guidance must not be empty"):
            coordinator.run(request)
        return
    result = coordinator.run(request)
    report = _payload(store, result.availability.report)
    assert report["run_state"] == expected_state
    assert report["models"] == [model for model in MODELS if model in available]
    entries = report["entries"]
    for entry in entries:
        if entry["variable"] == "probability_of_precipitation_1h":
            assert entry["state"] == ("complete" if "NBM" in available else "unavailable")
            assert entry["weights"] is None
            continue
        table = (
            config.blend_configuration.qpf_table
            if entry["variable"] == "liquid_equivalent_precipitation_amount_1h"
            else config.blend_configuration.scalar_vector_table
        )
        configured = table.row_for(
            available_models=tuple(model for model in MODELS if model in available),
            horizon=entry["horizon"],
        )
        # Independent literal equality: this fails if preferred weights are
        # silently renormalized instead of selecting the configured fallback row.
        assert tuple(entry["weights"]) == configured.weights
        assert entry["row_id"] == str(configured.row_id)
    baseline = _payload(store, result.corrected_forecast.baseline, NETCDF)
    validate_baseline_forecast_v2(baseline)
    if "NBM" not in available:
        assert np.isnan(baseline.probability_of_precipitation_1h.values).all()
        assert np.all(baseline.probability_of_precipitation_1h_state.values == 2)
        # A nonzero deterministic QPF proves PoP was not synthesized from QPF.
        assert np.all(baseline.liquid_equivalent_precipitation_amount_1h.values > 0)


def test_complete_cycle_proves_configuration_lineage_replay_and_concurrency(
    infrastructure, migrated_dsn
):
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    request = _request(snapshot, run_id="run_00000000-0000-0000-0000-000000000014")
    provider = FixtureProvider(service, frozenset(MODELS))
    coordinator = _coordinator(service, provider, config)
    first = coordinator.run(request)

    assert config.hrrr.allowed_cycle_hours == (0, 6, 12, 18)
    assert config.hrrr.max_age_hours == 6.0
    assert config.nbm.max_age_hours == 3.0
    assert config.gfs.max_age_hours == 12.0
    assert config.cycle_selection_policy.target_horizons == HORIZONS
    assert not any(
        (
            config.rrfs_enabled,
            config.ai_adjustment_enabled,
            config.learned_weights_enabled,
            config.publication_enabled,
        )
    )
    assert len(first.selected_inputs.source_roots) == 3 * 36 * 2
    assert all(
        root.artifact.availability.available_at <= CUTOFF
        for root in first.selected_inputs.source_roots
    )
    assert tuple(root.model for root in first.selected_inputs.source_roots[:72]) == ("HRRR",) * 72
    assert len(first.normalized.artifacts) == 3
    aligned = _payload(store, first.alignment.aligned_guidance)
    assert aligned["models"] == list(MODELS) and aligned["shape"] == [3, 36, 3]

    contributions = _model_from_json(
        BlendContributionManifest,
        _payload(store, first.atomic_forecast.contribution_manifest),
    )
    assert len(contributions.rows) == 7 * 3 * 36
    for row in contributions.rows:
        assert sum(item.weighted_contribution for item in row.contributors) == pytest.approx(
            row.unrounded_sum, abs=1e-12
        )
    pairs = [
        _model_from_json(MatchedPairRowV2, row)
        for row in _payload(store, first.matching.matched_pairs)["rows"]
    ]
    assert len(pairs) == 108
    missing = next(
        row
        for row in pairs
        if str(row.station_id) == "station.kros" and row.target_horizon_hours == 36
    )
    assert missing.temperature_status == "no_report_within_tolerance"
    report = _model_from_json(VerificationReportV2, _payload(store, first.verification.report))
    validate_verification_report_v2(report)
    assert report.rows

    lineage = _ancestors(migrated_dsn, first.verification.report.artifact_id)
    required = {
        str(first.selected_inputs.station_snapshot.artifact_id),
        str(first.observations.responses[0].artifact_id),
        *(str(root.artifact.artifact_id) for root in first.selected_inputs.source_roots),
    }
    assert required <= lineage
    assert all(
        "message=fixture-"
        in store.get_verified(root.artifact.storage_uri, root.artifact.content_digest).decode()
        for root in first.selected_inputs.source_roots
        if root.field_role == "message"
    )

    physical = coordinator.replay(request, first.selected_inputs)
    assert physical.verification.report.artifact_id == first.verification.report.artifact_id
    assert (
        physical.atomic_forecast.activity.activity_id == first.atomic_forecast.activity.activity_id
    )
    assert _payload(store, physical.verification.report) == _payload(
        store, first.verification.report
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = tuple(
            pool.map(lambda _: coordinator.replay(request, first.selected_inputs), range(2))
        )
    assert {item.verification.report.artifact_id for item in concurrent} == {
        first.verification.report.artifact_id
    }
    assert {item.atomic_forecast.activity.activity_id for item in concurrent} == {
        first.atomic_forecast.activity.activity_id
    }
    assert provider.network_calls == 0
    forbidden = ("mesoforge.rrfs", "mesoforge.ai", "mesoforge.bias", "mesoforge.publication")
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in sys.modules
        for prefix in forbidden
    )


def test_partial_cycle_rejects_whole_model_without_splicing(infrastructure, migrated_dsn):
    service, store = infrastructure
    config, snapshot = _configuration(migrated_dsn)
    provider = FixtureProvider(service, frozenset(MODELS), partial_model="HRRR")
    result = _coordinator(service, provider, config).run(_request(snapshot))
    assert all(root.model != "HRRR" for root in result.selected_inputs.source_roots)
    aligned = _payload(store, result.alignment.aligned_guidance)
    assert aligned["models"] == ["NBM", "GFS"]
    report = _payload(store, result.availability.report)
    assert report["run_state"] == "degraded"
    assert all("HRRR" not in entry["models"] for entry in report["entries"])
