"""Prepared snapshots, the latest-complete pointer and the network-free forecast path.

Every prepared input below is a generated fixture in the real preparation format;
nothing is downloaded and no provider availability is claimed.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import numpy as np
import pytest

from mesoforge.application import forecast_from_snapshot as fast
from mesoforge.application import local_surface_grid, refresh_guidance
from mesoforge.application import prepared_snapshot as snapshots
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import ReferenceCoverageError
from mesoforge.application.prepared_shadow import normalize_shadow_temperature
from mesoforge.application.prepared_temperature import (
    _write_prepared_file,
    prepare_temperature_guidance,
)
from mesoforge.common.errors import InvalidIdentifier
from mesoforge.common.identifiers import Digest, PreparedSnapshotId
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.guidance.coverage import COVERAGE_POLICY, REQUIRED_HOURS
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.support.phase1_fixture_transports import FixedClock
from tests.unit.application.test_batch_forecast import FIRST, LAST, write_config
from tests.unit.application.test_prepared_qpf import QpfFixtureTransport
from tests.unit.application.test_prepared_shadow import frame, geographic_frame
from tests.unit.application.test_prepared_temperature import (
    GFS_CYCLE,
    TARGET,
    FixtureClock,
    FixtureSleeper,
    phase2_configuration,
)

PREPARED_HOURS = 42
DECISION = TARGET + timedelta(minutes=12)


def test_snapshot_id_is_checked_before_path_or_manifest_io(tmp_path):
    for unsafe in ("../outside", "a/b", "a\\b", "/absolute", "C:outside"):
        with pytest.raises(InvalidIdentifier):
            snapshots.snapshot_directory(tmp_path, unsafe)
        with pytest.raises(InvalidIdentifier):
            snapshots.build_snapshot_manifest(
                snapshot_id=unsafe,
                root=tmp_path,
                preparation_path=tmp_path / "absent-preparation.json",
                selection_path=tmp_path / "absent-selection.json",
                steps=[],
                downloaded_bytes=0,
                clock=DECISION,
                completed_at=DECISION,
            )
    assert list(tmp_path.iterdir()) == []
    assert snapshots.snapshot_directory(tmp_path, PreparedSnapshotId("historical")) == (
        tmp_path / "snapshots" / "historical"
    )


def test_snapshot_directory_rejects_symlink_escape(tmp_path):
    root = tmp_path / "guidance"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "snapshots").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires OS permission")
    with pytest.raises(snapshots.SnapshotError, match="escapes"):
        snapshots.snapshot_directory(root, PreparedSnapshotId("valid-label"))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """The snapshot path must never open a provider connection."""
    forbidden = Mock(side_effect=AssertionError("Snapshot path attempted network access"))
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    return forbidden


@pytest.fixture(autouse=True)
def small_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The smallest nested geometry: each column costs about a second even on fixtures."""
    monkeypatch.setattr(
        local_surface_grid,
        "SurfaceGridGeometry",
        partial(local_surface_grid.SurfaceGridGeometry, context_half_width_cells=2),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def prepared_window_fixture(directory: Path, *, hours: int = PREPARED_HOURS) -> Path:
    """A finished selected preparation (control + RAP/IFS shadows) for 1..hours."""
    horizons = tuple(range(1, hours + 1))
    control = directory / "prepared" / "control"
    manifest = prepare_temperature_guidance(
        control,
        configuration=phase2_configuration(),
        target_reference_time=TARGET,
        hrrr_cycle=TARGET,
        gfs_cycle=GFS_CYCLE,
        target_horizon_hours=horizons,
        transport=QpfFixtureTransport(horizons),
        clock=FixtureClock(),
        sleeper=FixtureSleeper(),
        surface_fields=True,
        qpf_fields=True,
    )
    configuration = phase2_configuration()
    selection: dict[str, Any] = {
        "status": "selected",
        "decision_time": _iso(DECISION),
        "target_reference_time": _iso(TARGET),
        "first_valid_time": _iso(TARGET + timedelta(hours=1)),
        "last_valid_time": _iso(TARGET + timedelta(hours=hours)),
        "horizon_hours": list(horizons),
        "selected_cycles": {
            "HRRR": _iso(TARGET),
            "GFS": _iso(GFS_CYCLE),
            "RAP": _iso(TARGET),
            "IFS": _iso(TARGET),
        },
        "surface_fields": True,
        "qpf_fields": True,
        "source_configuration": configuration.model_dump(mode="json"),
        "coverage": {
            "policy": COVERAGE_POLICY,
            "requested_hours": hours,
            "prepared_hours": hours,
            "models": {},
        },
        "models": {},
        "fixture_notice": "Generated test evidence; no real provider discovery is claimed.",
    }
    for model, leads in (
        ("HRRR", range(1, hours + 1)),
        ("GFS", range(7, hours + 7)),
        ("RAP", range(1, hours + 1)),
        ("IFS", range(3, hours + 1, 3)),
    ):
        cycle = datetime.fromisoformat(selection["selected_cycles"][model])
        selection["models"][model] = {
            "status": "metadata_complete",
            "selected_cycle": _iso(cycle),
            "valid_times": [_iso(cycle + timedelta(hours=lead)) for lead in leads],
            "candidates": [
                {
                    "status": "metadata_complete",
                    "probes": [{"source_lead_hours": lead} for lead in leads],
                }
            ],
        }
    manifest["surface_blend_configuration"] = configuration.blend_configuration.model_dump(
        mode="json"
    )
    evidence = {
        "selection_sha256": str(Digest.of_bytes(json.dumps(selection).encode())),
        "selection": selection,
        "object_validation": [{"matched": True, "fixture_notice": "Generated test evidence"}],
    }
    manifest["current_model_set"] = evidence
    (control / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    shadows, shadow_reports = {}, {}
    for model, make_frame, leads in (
        ("RAP", frame, range(1, hours + 1)),
        ("IFS", geographic_frame, range(3, hours + 1, 3)),
    ):
        time = np.datetime64(TARGET.replace(tzinfo=None), "ns")
        decoded = {
            lead: make_frame(lead).assign_coords(
                time=time,
                step=np.timedelta64(lead, "h"),
                valid_time=time + np.timedelta64(lead, "h"),
            )
            for lead in leads
        }
        extra: dict[str, dict[int, Any]] = {}
        for variable, value, unit in (
            ("dew_point_temperature_2m", 260.0, "K"),
            ("eastward_wind_10m", 3.0, "m/s"),
            ("northward_wind_10m", 4.0, "m/s"),
            ("wind_gust_10m", 8.0, "m/s"),
        ):
            if model == "IFS" and variable == "wind_gust_10m":
                continue
            extra[variable] = {}
            for lead, temperature in decoded.items():
                field = temperature.copy(
                    data=np.full(
                        temperature.shape,
                        value + lead if variable == "dew_point_temperature_2m" else value,
                    )
                )
                field.attrs.update(GRIB_units=unit, GRIB_uvRelativeToGrid=0)
                extra[variable][lead] = field
        dataset = normalize_shadow_temperature(
            decoded, model=model, cycle=TARGET, target=TARGET, decoded_surface=extra
        )
        dataset.attrs["data_kind"] = "synthetic_demonstration"
        shadow = directory / "prepared" / model
        shadow.mkdir(parents=True)
        prepared_file = _write_prepared_file(shadow, model, dataset)
        # Explicit synthetic timing evidence for issuance-cutoff tests. These arrays
        # are generated above; no real provider availability is asserted.
        (shadow / "manifest.json").write_text(
            json.dumps(
                {
                    "data_kind": "synthetic_demonstration",
                    "prepared_files": {model: prepared_file},
                    "inputs": [
                        {
                            "model": model,
                            "cycle": _iso(TARGET),
                            "source_lead_hours": lead,
                            "grib_available_at": _iso(TARGET),
                            "index_available_at": _iso(TARGET),
                            "grib_retrieved_at": _iso(FixtureClock().now()),
                            "index_retrieved_at": _iso(FixtureClock().now()),
                        }
                        for lead in leads
                    ],
                }
            ),
            encoding="utf-8",
        )
        shadows[model] = str(shadow)
        shadow_reports[model] = {"selected_cycle": _iso(TARGET), "supported_hours": list(leads)}
    selection_dir = directory / "selection"
    selection_dir.mkdir(parents=True, exist_ok=True)
    (selection_dir / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
    preparation = {
        "directory": str(control),
        "shadow_directories": shadows,
        "current_model_set": evidence,
        "shadows": shadow_reports,
        "downloaded_bytes": 0,
        "retained_raw_bytes": sum(row["raw_bytes"] for row in manifest["inputs"]),
        "fixture_notice": "Offline prepared generated fixture; no real model downloads.",
    }
    (directory / "prepared" / "preparation.json").write_text(
        json.dumps(preparation, indent=1), encoding="utf-8"
    )
    return directory / "prepared" / "preparation.json"


def _passthrough(prepared: Path, out: Path) -> dict[str, Any]:
    """Attachment fake: copy the preparation forward, attaching nothing."""
    out.mkdir(parents=True)
    payload = json.loads((prepared / "preparation.json").read_text(encoding="utf-8"))
    (out / "preparation.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return payload


def fixture_steps(
    *, hours: int = PREPARED_HOURS, fail: str | None = None
) -> refresh_guidance.RefreshSteps:
    def discover(directory: Path) -> dict[str, Any]:
        if fail == "discover":
            raise RuntimeError("provider unreachable (fixture)")
        directory.mkdir(parents=True)
        return {
            "status": "selected",
            "selected_cycles": {},
            "horizon_hours": list(range(1, hours + 1)),
        }

    def prepare(locations: list[Any], selection: Path, out: Path) -> dict[str, Any]:
        if fail == "prepare":
            raise RuntimeError("acquisition failed (fixture)")
        path = prepared_window_fixture(out.parent, hours=hours)
        return json.loads(path.read_text(encoding="utf-8"))

    def failing(prepared: Path, out: Path) -> dict[str, Any]:
        raise RuntimeError("attachment failed (fixture)")

    return refresh_guidance.RefreshSteps(
        discover=discover,
        prepare=prepare,
        attach_ptype=failing if fail == "ptype" else _passthrough,
        attach_cloud=_passthrough,
        attach_thunder=_passthrough,
        attach_visibility=failing if fail == "visibility" else _passthrough,
    )


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """One real fixture refresh shared by the read-only tests below (they restore any edits)."""
    base = tmp_path_factory.mktemp("snapshot")
    root = base / "guidance"
    result = refresh_guidance.refresh_guidance(
        write_config(base, [FIRST, LAST]), root, steps=fixture_steps()
    )
    assert result["status"] == "published", result
    return root, result


def test_request_reference_time_is_the_current_utc_hour_never_the_next() -> None:
    assert snapshots.derive_reference_time(datetime(2026, 9, 17, 22, 37, tzinfo=UTC)) == datetime(
        2026, 9, 17, 22, tzinfo=UTC
    )
    assert snapshots.derive_reference_time(datetime(2026, 9, 17, 23, 5, tzinfo=UTC)) == datetime(
        2026, 9, 17, 23, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="timezone"):
        snapshots.derive_reference_time(datetime(2026, 9, 17, 23, 5))


def test_refresh_publishes_a_validated_snapshot_with_source_roles(published) -> None:
    root, result = published
    pointer = snapshots.read_pointer(root)
    assert pointer is not None and pointer["snapshot_id"] == result["snapshot_id"]
    _, manifest, directory = snapshots.resolve_latest_complete(root)
    assert manifest["schema_version"] == snapshots.SNAPSHOT_SCHEMA
    assert manifest["kind"] == "prepared_contributor_snapshot"
    coverage = manifest["coverage"]
    assert coverage["policy"]["id"] == "mesoforge-prepared-coverage-policy.v1"
    assert coverage["prepared_hours"] == PREPARED_HOURS
    assert coverage["first_valid_time"] == _iso(TARGET + timedelta(hours=1))
    assert coverage["last_valid_time"] == _iso(TARGET + timedelta(hours=PREPARED_HOURS))
    assert len(coverage["supported_valid_times"]) == PREPARED_HOURS
    contributors = manifest["contributors"]
    assert {m: c["kind"] for m, c in contributors.items()} == {
        "HRRR": "native_deterministic",
        "GFS": "native_deterministic",
        "RAP": "native_deterministic",
        "IFS": "native_deterministic",
        "NBM": "blended_meta_model",
    }
    assert contributors["HRRR"]["usage"] == "active_current_policy"
    assert contributors["RAP"]["usage"] == "shadow_evidence"
    assert contributors["IFS"]["valid_times"] == [
        _iso(TARGET + timedelta(hours=h)) for h in range(3, PREPARED_HOURS + 1, 3)
    ]
    assert manifest["completeness"]["required_deterministic"] is True
    # Nothing NBM-based was attached: the active products are explicitly unavailable,
    # and that never blocks publication of the deterministic snapshot.
    assert manifest["completeness"]["nbm_active_products"] == {
        "probability_of_precipitation_1h": "unavailable",
        "cloud_area_fraction": "unavailable",
        "probability_of_thunder_1h": "unavailable",
    }
    assert set(manifest["field_policies"]) == {
        "air_temperature_2m",
        "surface_scalar_vector",
        "liquid_equivalent_precipitation_amount_1h",
        "probability_of_precipitation_1h",
        "cloud_area_fraction",
        "probability_of_thunder_1h",
        "precipitation_type",
    }
    assert manifest["field_policies"]["air_temperature_2m"]["policy"] == "temperature_control_v1/1"
    run = manifest["prepared_run"]
    assert Path(run["preparation_file"]).is_file() and len(run["preparation_sha256"]) == 64
    assert manifest["validation"]["point_columns"][0]["hours"] == 36
    assert (directory / "refresh-log.json").is_file()
    assert [row["step"] for row in result["steps"]] == [
        "discover",
        "prepare_selected_with_pop",
        "attach_precipitation_type",
        "attach_cloud",
        "attach_thunder",
        "attach_visibility_evidence",
        "validate",
        "publish_latest_complete",
    ]


def test_absolute_coverage_serves_later_reference_hours_until_the_exact_boundary(
    published,
) -> None:
    root, _ = published
    _, manifest, _ = snapshots.resolve_latest_complete(root)
    grace = PREPARED_HOURS - REQUIRED_HOURS
    for offset in range(grace + 1):
        coverage = snapshots.coverage_for(manifest, TARGET + timedelta(hours=offset))
        assert coverage["usable"] is True, offset
        assert coverage["reference_offset_hours"] == offset
        assert coverage["required_complete"] == {"HRRR": True, "GFS": True}
        assert (
            coverage["nbm_active_products"]["probability_of_precipitation_1h"]["covered_hours"] == 0
        )
    boundary = snapshots.coverage_for(manifest, TARGET + timedelta(hours=grace + 1))
    assert boundary["usable"] is False
    assert boundary["missing"]["HRRR"] == [_iso(TARGET + timedelta(hours=PREPARED_HOURS + 1))]
    assert "first missing" in boundary["reason"]
    earlier = snapshots.coverage_for(manifest, TARGET - timedelta(hours=1))
    assert earlier["usable"] is False and "precedes" in earlier["reason"]
    with pytest.raises(ValueError, match="exact UTC hour"):
        snapshots.coverage_for(manifest, TARGET + timedelta(minutes=30))


def test_pointer_is_replaced_atomically_and_never_moves_backwards(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    manifest = {
        "schema_version": snapshots.SNAPSHOT_SCHEMA,
        "snapshot_id": "a",
        "completeness": {"required_deterministic": True},
        "coverage": {
            "reference_time": "2026-09-17T22:00:00Z",
            "first_valid_time": "2026-09-17T23:00:00Z",
            "last_valid_time": "2026-09-19T16:00:00Z",
        },
    }
    published_at = datetime(2026, 9, 17, 22, 15, tzinfo=UTC)
    pointer = snapshots.publish_latest_complete(root, manifest, "0" * 64, published_at=published_at)
    assert pointer["previous_snapshot_id"] is None
    assert {p.name for p in root.iterdir()} == {snapshots.POINTER_FILE, ".latest_complete.lock"}
    newer = deepcopy(manifest)
    newer.update(snapshot_id="b")
    newer["coverage"]["reference_time"] = "2026-09-17T23:00:00Z"
    second = snapshots.publish_latest_complete(root, newer, "1" * 64, published_at=published_at)
    assert second["previous_snapshot_id"] == "a"
    assert snapshots.read_pointer(root)["snapshot_id"] == "b"
    with pytest.raises(snapshots.SnapshotError, match="newer"):
        snapshots.publish_latest_complete(root, manifest, "0" * 64, published_at=published_at)
    incomplete = deepcopy(newer)
    incomplete["completeness"]["required_deterministic"] = False
    with pytest.raises(snapshots.SnapshotError, match="incomplete"):
        snapshots.publish_latest_complete(root, incomplete, "2" * 64, published_at=published_at)
    assert snapshots.read_pointer(root)["snapshot_id"] == "b"


def test_manifest_digest_and_retained_artifacts_are_validated_before_use(published) -> None:
    root, result = published
    manifest_path = Path(result["manifest_file"])
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(original.replace(b"prepared_contributor_snapshot", b"x" * 29))
    with pytest.raises(snapshots.SnapshotError, match="digest"):
        snapshots.resolve_latest_complete(root)
    manifest_path.write_bytes(original)
    _, manifest, _ = snapshots.resolve_latest_complete(root)
    preparation = Path(manifest["prepared_run"]["preparation_file"])
    payload = preparation.read_bytes()
    preparation.write_bytes(payload + b"\n")
    with pytest.raises(snapshots.SnapshotError, match="preparation.json differs"):
        snapshots.verify_prepared_run(manifest)
    preparation.write_bytes(payload)
    assert snapshots.verify_prepared_run(manifest)["directory"]


@pytest.mark.parametrize("failure", ["discover", "prepare", "ptype"])
def test_failed_refresh_leaves_latest_complete_unchanged(published, tmp_path, failure) -> None:
    root, first = published
    before = snapshots.read_pointer(root)
    result = refresh_guidance.refresh_guidance(
        write_config(tmp_path, [FIRST]), root, steps=fixture_steps(fail=failure)
    )
    assert result["status"] == "failed"
    assert result["latest_complete_unchanged"] is True
    assert snapshots.read_pointer(root) == before
    assert before["snapshot_id"] == first["snapshot_id"]
    failed_directory = Path(result["directory"])
    assert (failed_directory / "failure.json").is_file()
    assert not (failed_directory / snapshots.MANIFEST_FILE).exists()
    # The earlier good snapshot is untouched and still replayable.
    _, manifest, _ = snapshots.resolve_latest_complete(root)
    assert manifest["snapshot_id"] == first["snapshot_id"]


def test_optional_visibility_failure_does_not_block_publication(tmp_path: Path) -> None:
    root = tmp_path / "guidance"
    result = refresh_guidance.refresh_guidance(
        write_config(tmp_path, [FIRST]), root, steps=fixture_steps(fail="visibility")
    )
    assert result["status"] == "published"
    step = next(row for row in result["steps"] if row["step"] == "attach_visibility_evidence")
    assert step["status"] == "error"
    assert snapshots.read_pointer(root)["snapshot_id"] == result["snapshot_id"]


def test_refresh_retains_supported_columns_around_an_out_of_domain_location(tmp_path: Path) -> None:
    outside = {"lat": 48.8566, "lon": 2.3522, "name": "Outside HRRR native domain"}
    root = tmp_path / "guidance"
    result = refresh_guidance.refresh_guidance(
        write_config(tmp_path, [FIRST, outside, LAST]), root, steps=fixture_steps()
    )
    assert result["status"] == "published", result
    _, manifest, _ = snapshots.resolve_latest_complete(root)
    validation = manifest["validation"]
    assert [row["location"] for row in validation["point_columns"]] == [FIRST, LAST]
    assert all(row["hours"] == 36 for row in validation["point_columns"])
    assert len(validation["failed_locations"]) == 1
    failure = validation["failed_locations"][0]
    assert failure["index"] == 1 and failure["location"] == outside
    assert failure["code"] == "unsupported_coordinate"
    assert "native model domain" in failure["reason"]

    before = snapshots.read_pointer(root)
    failed = refresh_guidance.refresh_guidance(
        write_config(tmp_path, [outside]), root, steps=fixture_steps()
    )
    assert failed["status"] == "failed"
    assert "No configured location has usable prepared coverage" in failed["error"]
    assert snapshots.read_pointer(root) == before


def test_coordinate_isolation_does_not_hide_scientific_validation_errors(
    published, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    root, _ = published
    _, manifest, _ = snapshots.resolve_latest_complete(root)
    preparation = snapshots.verify_prepared_run(manifest)
    actual = snapshots.load_preparation(preparation)

    def view(reference):
        selected = actual.reference_view(reference)

        def column(*, latitude, longitude):
            if latitude == LAST["lat"]:
                raise ValueError("Retained contributor identity mismatch")
            return selected.point_column(latitude=latitude, longitude=longitude)

        return SimpleNamespace(point_column=column)

    monkeypatch.setattr(
        refresh_guidance,
        "load_preparation",
        lambda _: SimpleNamespace(
            reference_view=view, prepared_valid_times=actual.prepared_valid_times
        ),
    )
    with pytest.raises(ValueError, match="contributor identity mismatch"):
        refresh_guidance._validate(preparation, [FIRST, LAST])


def test_fast_path_serves_36_hours_from_the_snapshot_without_network(published) -> None:
    root, result = published
    outcome = fast.forecast_from_snapshot(
        root,
        [FIRST],
        request_time=TARGET + timedelta(minutes=37),
        display_timezone="America/Chicago",
    )
    assert outcome["status"] == "ok"
    assert outcome["reference_time"] == _iso(TARGET)
    assert outcome["reference_time_source"] == "request_hour"
    assert outcome["network_calls"] == 0
    assert outcome["snapshot"]["snapshot_id"] == result["snapshot_id"]
    assert outcome["coverage"]["usable"] is True
    row = outcome["results"][0]
    assert row["status"] == "ok"
    forecast = row["forecast"]
    assert [hour["horizon_hours"] for hour in forecast["hours"]] == list(range(1, 37))
    assert forecast["hours"][0]["valid_time"] == _iso(TARGET + timedelta(hours=1))
    assert forecast["hours"][-1]["valid_time"] == _iso(TARGET + timedelta(hours=36))
    assert forecast["target_reference_time"] == _iso(TARGET)
    assert forecast["prepared_window"] == {
        "prepared_reference_time": _iso(TARGET),
        "prepared_horizon_hours": list(range(1, PREPARED_HOURS + 1)),
        "reference_offset_hours": 0,
    }
    assert forecast["prepared_snapshot"]["snapshot_id"] == result["snapshot_id"]
    assert forecast["prepared_snapshot"]["contributor_cycles"]["GFS"] == _iso(GFS_CYCLE)
    assert forecast["hourly_report"]["display_timezone"] == "America/Chicago"
    assert forecast["hourly_report"]["hours"][0]["bias_correction"]["status"] == "not_implemented"
    assert "local_grid_baseline" in forecast
    assert set(outcome["timings"]) >= {"guidance_load_seconds", "total_seconds"}
    assert row["local_grid_build_seconds"] > 0


def test_same_snapshot_serves_a_later_reference_hour_and_refuses_past_the_boundary(
    published,
) -> None:
    root, result = published
    grace = PREPARED_HOURS - REQUIRED_HOURS
    later = TARGET + timedelta(hours=grace)
    outcome = fast.forecast_from_snapshot(root, [FIRST], request_time=later + timedelta(minutes=5))
    assert outcome["status"] == "ok"
    assert outcome["snapshot"]["snapshot_id"] == result["snapshot_id"]
    forecast = outcome["results"][0]["forecast"]
    assert forecast["target_reference_time"] == _iso(later)
    assert forecast["hours"][0]["valid_time"] == _iso(later + timedelta(hours=1))
    assert forecast["hours"][-1]["valid_time"] == _iso(TARGET + timedelta(hours=PREPARED_HOURS))
    assert forecast["prepared_window"]["reference_offset_hours"] == grace
    # Source cycles and leads are what was prepared; only the reference moved.
    first_hour = forecast["hours"][0]
    hrrr = next(source for source in first_hour["sources"] if source["model"] == "HRRR")
    assert hrrr["cycle"] == _iso(TARGET) and hrrr["source_lead_hours"] == grace + 1
    assert forecast["current_model_set"]["selection"]["target_reference_time"] == _iso(TARGET)
    expired = fast.forecast_from_snapshot(
        root, [FIRST], request_time=later + timedelta(hours=1, minutes=5)
    )
    assert expired["status"] == "no_current_snapshot"
    assert expired["results"] == []
    assert "first missing" in expired["reason"]
    assert expired["coverage"]["usable"] is False


def test_multiple_coordinates_share_one_snapshot_and_failures_stay_isolated(published) -> None:
    root, result = published
    outside = {"lat": 40.0, "lon": -80.0, "name": "outside prepared footprint"}
    outcome = fast.forecast_from_snapshot(root, [FIRST, outside, LAST], request_time=DECISION)
    assert outcome["status"] == "ok"
    statuses = [row["status"] for row in outcome["results"]]
    assert statuses == ["ok", "error", "ok"]
    assert outcome["results"][1]["error"]["code"] in {"coverage_required", "unsupported_coordinate"}
    ids = {
        row["forecast"]["prepared_snapshot"]["snapshot_id"]
        for row in outcome["results"]
        if "forecast" in row
    }
    assert ids == {result["snapshot_id"]}
    assert outcome["summary"] == {"ok": 2, "issued": 0, "skipped": 0, "failed": 1}
    assert outcome["timings"]["guidance_load_seconds"] > 0  # loaded once for all locations


def test_issuance_from_snapshot_records_request_and_snapshot_provenance(published) -> None:
    root, result = published
    # Historical numerical reference, new issuance after the actual fixture publication.
    request = datetime.fromisoformat(snapshots.read_pointer(root)["published_at"]) + timedelta(
        seconds=1
    )
    clock = FixedClock(request)
    issuer = ForecastIssuanceService(
        InMemoryObjectStore(),
        InMemoryUnitOfWorkFactory(),
        code_identity={"test": "snapshot-issuance"},
        clock=clock.now,
    )
    outcome = fast.forecast_from_snapshot(
        root,
        [FIRST],
        request_time=request,
        reference_time=TARGET,
        issue=True,
        issuer=issuer,
        run_lock=nullcontext,
    )
    row = outcome["results"][0]
    assert row["status"] == "ok" and "issued" in row
    issued = issuer.read(UUID(row["issued"]["issued_forecast_id"]))
    assert issued["issued_at"] == _iso(request)
    assert issued["target_reference_time"] == _iso(TARGET)
    provenance = issued["forecast"]["prepared_snapshot"]
    assert provenance["snapshot_id"] == result["snapshot_id"]
    assert provenance["published_at"] == snapshots.read_pointer(root)["published_at"]
    assert provenance["request_time"] == _iso(request)
    assert provenance["forecast_analysis_cutoff"] == _iso(request)
    assert provenance["information_cutoff"]["status"] == "proven"
    assert provenance["decision_time"] == _iso(DECISION)
    assert provenance["contributor_cycles"] == {
        "HRRR": _iso(TARGET),
        "GFS": _iso(GFS_CYCLE),
        "RAP": _iso(TARGET),
        "IFS": _iso(TARGET),
    }
    assert provenance["field_policies"]["precipitation_type"]["policy"].startswith("temporary")
    assert [hour["horizon_hours"] for hour in issued["forecast"]["hours"]] == list(range(1, 37))
    repeated = fast.forecast_from_snapshot(
        root,
        [FIRST],
        request_time=request,
        reference_time=TARGET,
        issue=True,
        issuer=issuer,
        run_lock=nullcontext,
    )
    assert repeated["results"][0]["status"] == "skipped_already_issued"
    reissued = fast.forecast_from_snapshot(
        root,
        [FIRST],
        request_time=request,
        reference_time=TARGET,
        issue=True,
        issuer=issuer,
        run_lock=nullcontext,
        reissue=True,
    )
    assert "issued" in reissued["results"][0]
    with pytest.raises(ValueError, match="after the request hour"):
        fast.forecast_from_snapshot(
            root,
            [FIRST],
            request_time=DECISION,
            reference_time=TARGET + timedelta(hours=1),
            issue=True,
            issuer=issuer,
        )


def test_no_snapshot_is_reported_not_prepared(tmp_path: Path) -> None:
    outcome = fast.forecast_from_snapshot(tmp_path / "empty", [FIRST], request_time=DECISION)
    assert outcome["status"] == "no_current_snapshot"
    assert "No latest-complete" in outcome["reason"]


def test_reference_view_relabels_hours_without_touching_source_leads(tmp_path: Path) -> None:
    preparation = json.loads(prepared_window_fixture(tmp_path).read_text(encoding="utf-8"))
    prepared = snapshots.load_preparation(preparation)
    assert prepared.horizon_hours == tuple(range(1, PREPARED_HOURS + 1))
    view = prepared.reference_view(TARGET + timedelta(hours=2))
    assert view.horizon_hours == tuple(range(1, 37))
    assert prepared.horizon_hours == tuple(range(1, PREPARED_HOURS + 1))  # original untouched
    column = view.point_column(latitude=FIRST["lat"], longitude=FIRST["lon"])
    assert column["hours"][0]["valid_time"] == _iso(TARGET + timedelta(hours=3))
    gfs = next(source for source in column["hours"][0]["sources"] if source["model"] == "GFS")
    assert gfs["cycle"] == _iso(GFS_CYCLE) and gfs["source_lead_hours"] == 9
    with pytest.raises(ReferenceCoverageError, match="lacks"):
        prepared.reference_view(TARGET + timedelta(hours=PREPARED_HOURS - 36 + 1))
    with pytest.raises(ReferenceCoverageError, match="precedes"):
        prepared.reference_view(TARGET - timedelta(hours=1))
    with pytest.raises(ValueError, match="exact UTC hour"):
        prepared.reference_view(TARGET + timedelta(minutes=1))


def test_historical_36_hour_preparation_still_loads_and_serves_its_own_hour(tmp_path: Path) -> None:
    preparation = json.loads(
        prepared_window_fixture(tmp_path, hours=36).read_text(encoding="utf-8")
    )
    prepared = snapshots.load_preparation(preparation)
    assert prepared.horizon_hours == tuple(range(1, 37))
    view = prepared.reference_view(TARGET)
    assert view.point_column(latitude=FIRST["lat"], longitude=FIRST["lon"])["hours"][-1][
        "valid_time"
    ] == _iso(TARGET + timedelta(hours=36))
    with pytest.raises(ReferenceCoverageError):
        prepared.reference_view(TARGET + timedelta(hours=1))


def test_snapshot_column_builds_one_transformer_per_native_crs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real loader format carries four distinct CRSs; cost must not scale with hours."""
    import pyproj

    from mesoforge.alignment import spatial
    from mesoforge.application import spatial_coverage

    preparation = json.loads(prepared_window_fixture(tmp_path).read_text(encoding="utf-8"))
    prepared = snapshots.load_preparation(preparation)
    view = prepared.reference_view(TARGET)
    constructions: list[object] = []
    real_from_crs = pyproj.Transformer.from_crs

    def counting(*args: object, **kwargs: object) -> object:
        constructions.append(args[1] if len(args) > 1 else kwargs.get("crs_to"))
        return real_from_crs(*args, **kwargs)

    monkeypatch.setattr(pyproj.Transformer, "from_crs", staticmethod(counting))
    spatial._WGS84_TO_NATIVE.clear()
    memoized = view.point_column(latitude=FIRST["lat"], longitude=FIRST["lon"])
    cached_count = len(constructions)

    def uncached(crs: object) -> object:
        return pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    monkeypatch.setattr(spatial, "wgs84_to_native_transformer", uncached)
    monkeypatch.setattr(spatial_coverage, "wgs84_to_native_transformer", uncached)
    constructions.clear()
    original = view.point_column(latitude=FIRST["lat"], longitude=FIRST["lon"])

    assert canonical_json_bytes(memoized) == canonical_json_bytes(original)
    assert cached_count <= 4  # HRRR and RAP Lambert, GFS and IFS geographic
    assert len(constructions) > 36 * 4  # the per-hour, per-model construction it replaces
