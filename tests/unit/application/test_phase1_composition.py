"""Task 12 tests for the strict Phase 1 lifecycle coordinator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.application.phase1 import (
    ForecastArtifacts,
    HrrrLeadArtifacts,
    ObservationArtifacts,
    Phase1Coordinator,
    Phase1Request,
    StationCatalogArtifacts,
    VerificationArtifacts,
)
from mesoforge.contracts.artifacts import ArtifactManifest, Availability, SourceIdentity
from mesoforge.contracts.runs import RunManifest

CFG_ID = "cfg_sha256_" + "a" * 64
DIGEST = "sha256:" + "a" * 64
ENV = "sha256:" + "b" * 64
LOCK = "sha256:" + "c" * 64
RUN_ID = "run_00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 8, 28, 18, tzinfo=UTC)


def _request(**updates: object) -> Phase1Request:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "configuration_snapshot_id": CFG_ID,
        "configuration_digest": DIGEST,
        "code_revision": "d" * 40,
        "environment_digest": ENV,
        "lockfile_digest": LOCK,
        "cycle": NOW,
        "forecast_issue_time": NOW,
        "information_cutoff": NOW,
        "verification_cutoff": datetime(2026, 8, 29, 1, tzinfo=UTC),
    }
    values.update(updates)
    return Phase1Request(**values)


def _artifact(number: int, artifact_type: str, *, source: bool, run: bool = False):
    return ArtifactManifest(
        artifact_id=f"art_{number:08d}-0000-0000-0000-000000000000",
        artifact_type=artifact_type,
        artifact_schema_version=f"{artifact_type}.v1",
        media_type="application/octet-stream",
        byte_size=1,
        content_digest=DIGEST,
        storage_uri=f"s3://test/{number}",
        created_at=NOW,
        registered_at=NOW,
        availability=Availability(available_at=NOW, authority="synthetic", method="fixture.v1"),
        run_id=RUN_ID if run else None,
        configuration_snapshot_id=CFG_ID,
        configuration_digest=DIGEST,
        code_revision="d" * 40,
        environment_digest=ENV,
        quality_state="valid",
        source_registration_digest=DIGEST if source else None,
        source_identity=(
            SourceIdentity(authority="synthetic", locator=f"fixture://{number}", revision="v1")
            if source
            else None
        ),
    )


class _Clock:
    def now(self):
        return NOW


class _Sleeper:
    def sleep(self, seconds: float) -> None:
        del seconds


class _Service:
    def __init__(self) -> None:
        self.calls = 0
        self.run_manifest = None

    def create_run(self, **values):
        self.calls += 1
        if self.run_manifest is None:
            self.run_manifest = RunManifest(
                run_id=values["run_id"],
                forecast_issue_time=values["forecast_issue_time"],
                information_cutoff=values["information_cutoff"],
                configuration_snapshot_id=values["configuration_snapshot_id"],
                configuration_digest=values["configuration_digest"],
                code_revision=values["code_revision"],
                environment_digest=values["environment_digest"],
                lockfile_digest=values["lockfile_digest"],
                random_seed=values["random_seed"],
                selected_input_artifact_ids=values["selected_input_artifact_ids"],
                created_at=NOW,
            )
        return self.run_manifest


class _Sources:
    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.station = StationCatalogArtifacts(
            response=_artifact(1, "aviationweather-station-response", source=True),
            snapshot=_artifact(2, "station-catalog-snapshot", source=False),
        )
        self.hrrr = tuple(
            HrrrLeadArtifacts(
                lead_hours=lead,
                index=_artifact(10 + lead * 2, "hrrr-grib-index", source=True),
                selected_grib=_artifact(11 + lead * 2, "hrrr-selected-grib", source=True),
            )
            for lead in range(7)
        )

    def _called(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(name)

    def register_station_catalog(self, *args, **kwargs):
        self._called("station")
        return self.station

    def register_hrrr_sources(self, *args, **kwargs):
        self._called("hrrr")
        return self.hrrr

    def register_metar_observations(self, *args, **kwargs):
        self._called("metar")
        return ObservationArtifacts(
            responses=(_artifact(40, "aviationweather-metar-response", source=True),),
            normalized=_artifact(41, "normalized-metar-observations", source=False, run=True),
        )


class _Transforms:
    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[str] = []

    def _called(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(name)

    def build_forecast(self, *args, **kwargs):
        self._called("forecast")
        return ForecastArtifacts(
            acquisition_manifest=_artifact(30, "hrrr-acquisition-manifest", source=False, run=True),
            variable_lineage=_artifact(31, "variable-lineage-manifest", source=False, run=True),
            canonical_guidance=_artifact(32, "canonical-guidance", source=False, run=True),
            extraction_report=_artifact(33, "point-extraction-report", source=False, run=True),
            baseline=_artifact(34, "baseline-forecast", source=False, run=True),
        )

    def verify(self, *args, **kwargs):
        self._called("verify")
        return VerificationArtifacts(
            matched_pairs=_artifact(42, "matched-pairs", source=False, run=True),
            report=_artifact(43, "verification-report", source=False, run=True),
        )


def _coordinator(sources=None, transforms=None, service=None):
    return Phase1Coordinator(
        artifact_service=service or _Service(),
        sources=sources or _Sources(),
        transformations=transforms or _Transforms(),
        clock=_Clock(),
        sleeper=_Sleeper(),
    )


def test_request_is_strict_and_checks_temporal_lifecycle() -> None:
    with pytest.raises(ValidationError):
        _request(random_seed="0")
    with pytest.raises(ValidationError, match="information_cutoff"):
        _request(information_cutoff=datetime(2026, 8, 28, 19, tzinfo=UTC))


def test_lifecycle_pins_fourteen_hrrr_roots_then_station_snapshot() -> None:
    service = _Service()
    sources = _Sources()
    transforms = _Transforms()
    result = _coordinator(sources, transforms, service).run(_request())
    assert sources.calls == ["station", "hrrr", "metar"]
    assert transforms.calls == ["forecast", "verify"]
    assert len(result.run.selected_input_artifact_ids) == 15
    assert result.run.selected_input_artifact_ids[-1] == sources.station.snapshot.artifact_id


@pytest.mark.parametrize("boundary", ["station", "hrrr", "forecast", "metar", "verify"])
def test_failure_never_advances_to_later_boundary(boundary: str) -> None:
    sources = _Sources(boundary if boundary in {"station", "hrrr", "metar"} else None)
    transforms = _Transforms(boundary if boundary in {"forecast", "verify"} else None)
    with pytest.raises(RuntimeError, match=boundary):
        _coordinator(sources, transforms).run(_request())
    order = sources.calls + transforms.calls
    assert "verify" not in order if boundary != "verify" else True


def test_wrong_role_artifact_fails_before_run_creation() -> None:
    service = _Service()
    sources = _Sources()
    sources.hrrr = (
        sources.hrrr[0].model_copy(update={"index": _artifact(90, "wrong", source=True)}),
        *sources.hrrr[1:],
    )
    with pytest.raises(ValueError, match="hrrr-grib-index"):
        _coordinator(sources=sources, service=service).run(_request())
    assert service.calls == 0


def test_repeat_reuses_service_run_and_returns_equal_artifact_ids() -> None:
    service = _Service()
    coordinator = _coordinator(service=service)
    first = coordinator.run(_request())
    second = coordinator.run(_request())
    assert first.run == second.run
    assert first.verification.report.artifact_id == second.verification.report.artifact_id
