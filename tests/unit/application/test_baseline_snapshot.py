"""Durable background blends, pinned read-only consumption and process-safe publication."""

from __future__ import annotations

import json
import multiprocessing
import shutil
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from mesoforge.application import baseline_snapshot as baselines
from mesoforge.application import build_baseline as background
from mesoforge.application import prepared_snapshot as prepared
from mesoforge.application import refresh_guidance
from mesoforge.application.forecast_from_baseline import forecast_from_baseline
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.spatial_coverage import CoverageRequiredError
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.forecasting.field_blend import FieldBlendEngine
from tests.support.in_memory_uow import InMemoryObjectStore, InMemoryUnitOfWorkFactory
from tests.unit.application.test_batch_forecast import FIRST, LAST, write_config
from tests.unit.application.test_prepared_snapshot import fixture_steps
from tests.unit.application.test_prepared_temperature import TARGET
from tests.unit.application.test_snapshot_issuance import REQUEST, fixture_forecast

LOCATIONS = [FIRST, LAST]
REFERENCES = [TARGET, TARGET + timedelta(hours=1)]


@pytest.fixture(scope="module")
def baseline_case(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Retain the actual old-path result while the background build invokes it once.

    The existing generated GRIB/prepared fixture supplies real extraction/scientific
    code, not a second forecast implementation. No real provider data is requested.
    """
    root = tmp_path_factory.mktemp("background-baseline")
    guidance, baseline = root / "guidance", root / "baseline"
    expected: dict[tuple[float, float], dict[str, Any]] = {}
    later_expected: dict[tuple[float, float], dict[str, Any]] = {}
    calls: list[datetime] = []
    original_load = prepared.load_preparation

    def load(preparation):
        actual = original_load(preparation)

        def view(reference):
            calls.append(reference)
            old = actual.reference_view(reference)

            def forecast(*, latitude, longitude):
                result = old.forecast(latitude=latitude, longitude=longitude)
                retained = expected if reference == TARGET else later_expected
                retained[(latitude, longitude)] = deepcopy(result)
                return result

            return SimpleNamespace(forecast=forecast)

        return SimpleNamespace(reference_view=view)

    with pytest.MonkeyPatch.context() as patcher:
        forbidden = Mock(side_effect=AssertionError("Background fixture attempted provider access"))
        patcher.setattr("requests.Session", forbidden)
        patcher.setattr("socket.create_connection", forbidden)
        refreshed = refresh_guidance.refresh_guidance(
            write_config(root, LOCATIONS), guidance, steps=fixture_steps()
        )
        assert refreshed["status"] == "published", refreshed
        load_once = Mock(side_effect=load)
        patcher.setattr(background, "load_preparation", load_once)
        result = background.build_baseline(
            guidance, baseline, LOCATIONS + [FIRST], reference_times=REFERENCES
        )
        assert load_once.call_count == 1
        forbidden.assert_not_called()
    assert calls == REFERENCES
    assert set(expected) == {(row["lat"], row["lon"]) for row in LOCATIONS}
    return {
        "guidance": guidance,
        "baseline": baseline,
        "expected": expected,
        "later_expected": later_expected,
        "result": result,
        "prepared": prepared.resolve_latest_complete(guidance),
    }


def forbid_location_calculation(monkeypatch: pytest.MonkeyPatch) -> Mock:
    forbidden = Mock(side_effect=AssertionError("Location job recalculated or acquired guidance"))
    monkeypatch.setattr(FieldBlendEngine, "blend_field", forbidden)
    monkeypatch.setattr(prepared, "load_preparation", forbidden)
    monkeypatch.setattr(background, "load_preparation", forbidden)
    monkeypatch.setattr("requests.Session", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    return forbidden


def test_persisted_canvas_has_exact_lineage_and_replays_every_cell_without_blending(
    baseline_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = forbid_location_calculation(monkeypatch)
    pinned = baselines.load_baseline(baseline_case["baseline"])
    pointer, source, _ = baseline_case["prepared"]
    manifest = pinned.manifest
    assert manifest["schema_version"] == baselines.BASELINE_SCHEMA
    assert manifest["prepared_snapshot"]["snapshot_id"] == source["snapshot_id"]
    assert manifest["prepared_snapshot"]["manifest_sha256"] == pointer["manifest_sha256"]
    assert manifest["field_policies"] == source["field_policies"]
    assert manifest["information_cutoff"]["status"] == "proven"
    assert manifest["coverage"]["reference_times"] == [
        time.isoformat().replace("+00:00", "Z") for time in REFERENCES
    ]
    view = pinned.reference_view(TARGET)
    for location in LOCATIONS:
        coords = (location["lat"], location["lon"])
        expected = baseline_case["expected"][coords]
        actual = view.forecast(latitude=coords[0], longitude=coords[1])
        # Includes native contributors, intervals, masks, provenance and all policies;
        # zero tolerance, no deletion of inconvenient evidence/hash fields.
        assert canonical_json_bytes(actual) == canonical_json_bytes(expected)
        grid = actual["local_grid_baseline"]
        assert len(grid["cells"]) == 49
        assert all(len(cell["hours"]) == 36 for cell in grid["cells"])
        assert canonical_json_bytes(view.forecast(latitude=coords[0], longitude=coords[1])) == (
            canonical_json_bytes(expected)
        )
    forbidden.assert_not_called()


def test_two_locations_share_one_pinned_baseline_and_uncovered_location_is_isolated(
    baseline_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = forbid_location_calculation(monkeypatch)
    not_built = {"lat": 45.85, "lon": -93.05}
    outcome = forecast_from_baseline(
        baseline_case["baseline"], [FIRST, not_built, LAST], reference_time=TARGET
    )
    assert outcome["summary"] == {"ok": 2, "issued": 0, "skipped": 0, "failed": 1}
    assert outcome["results"][1]["error"]["code"] == "coverage_required"
    successful = [row["forecast"] for row in outcome["results"] if row["status"] == "ok"]
    assert {row["baseline_snapshot"]["baseline_snapshot_id"] for row in successful} == {
        outcome["baseline"]["baseline_snapshot_id"]
    }
    assert successful[0]["local_grid"]["geometry"] != successful[1]["local_grid"]["geometry"]
    assert outcome["network_calls"] == 0
    forbidden.assert_not_called()


@pytest.mark.parametrize("clock", ["built_at", "published_at"])
def test_location_cutoff_before_baseline_build_or_publication_cannot_extract_or_issue(
    baseline_case, monkeypatch: pytest.MonkeyPatch, clock: str
) -> None:
    pinned = baselines.load_baseline(baseline_case["baseline"])
    value = pinned.pointer[clock] if clock == "published_at" else pinned.manifest[clock]
    request = datetime.fromisoformat(value) - timedelta(microseconds=1)
    forbidden = Mock(side_effect=AssertionError("Unproven baseline reached extraction or issuance"))
    monkeypatch.setattr(pinned, "reference_view", forbidden)
    monkeypatch.setattr(baselines, "load_baseline", lambda _: pinned)
    issuer = Mock()
    issuer.issue = forbidden
    result = forecast_from_baseline(
        baseline_case["baseline"],
        LOCATIONS,
        reference_time=TARGET,
        request_time=request,
        issue=True,
        issuer=issuer,
        run_lock=forbidden,
    )
    assert result["status"] == "no_current_baseline"
    assert "follows forecast analysis cutoff" in result["reason"]
    assert result["results"] == []
    assert not issuer.mock_calls
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "cutoff",
    [(REQUEST + timedelta(seconds=1)).isoformat(), REQUEST.replace(tzinfo=None).isoformat()],
)
def test_direct_issuance_rejects_future_or_naive_baseline_cutoff_before_storage(cutoff: str):
    objects, uow = InMemoryObjectStore(), InMemoryUnitOfWorkFactory()
    issuer = ForecastIssuanceService(objects, uow, code_identity={}, clock=lambda: REQUEST)
    forecast = fixture_forecast(FIRST["lat"], FIRST["lon"])
    forecast["baseline_snapshot"] = {"forecast_analysis_cutoff": cutoff}
    with pytest.raises(ValueError, match="cutoff"):
        issuer.issue(forecast, batch_run_id=uuid4(), location_index=0)
    assert not objects.objects and not uow.issued_forecasts


def test_reference_and_spatial_coverage_are_explicit_not_on_demand_recomputed(
    baseline_case,
) -> None:
    pinned = baselines.load_baseline(baseline_case["baseline"])
    with pytest.raises(
        (prepared.SnapshotError, ValueError), match="reference|coverage|materialized"
    ):
        pinned.reference_view(TARGET + timedelta(hours=2))
    with pytest.raises(CoverageRequiredError):
        pinned.reference_view(TARGET).forecast(latitude=45.85, longitude=-93.05)


def test_reference_relative_lead_bands_are_materialized_not_sliced_or_relabelled(
    baseline_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = forbid_location_calculation(monkeypatch)
    pinned = baselines.load_baseline(baseline_case["baseline"])
    later = pinned.reference_view(REFERENCES[1]).forecast(
        latitude=FIRST["lat"], longitude=FIRST["lon"]
    )
    expected = baseline_case["later_expected"][(FIRST["lat"], FIRST["lon"])]
    assert canonical_json_bytes(later) == canonical_json_bytes(expected)
    earlier = baseline_case["expected"][(FIRST["lat"], FIRST["lon"])]
    original_19 = earlier["hours"][18]
    later_18 = later["hours"][17]
    assert original_19["valid_time"] == later_18["valid_time"]
    field = "dew_point_temperature_2m"
    assert original_19["surface"]["fields"][field]["weights"] == {"HRRR": 0.6, "GFS": 0.4}
    assert later_18["surface"]["fields"][field]["weights"] == {"HRRR": 0.7, "GFS": 0.3}
    assert later["hours"][0]["sources"][0]["source_lead_hours"] == 2
    forbidden.assert_not_called()


def test_manifest_is_immutable_and_digest_checked_before_reading(baseline_case, tmp_path: Path):
    root = tmp_path / "copy"
    shutil.copytree(baseline_case["baseline"], root)
    pinned = baselines.load_baseline(root)
    manifest_path = pinned.directory / baselines.MANIFEST_FILE
    original = manifest_path.read_bytes()
    with pytest.raises(FileExistsError):
        baselines.write_manifest(pinned.directory, pinned.manifest)
    assert manifest_path.read_bytes() == original
    manifest_path.write_bytes(original + b" ")
    with pytest.raises(prepared.SnapshotError, match="digest|hash|changed"):
        baselines.load_baseline(root)


def test_future_or_unproven_information_cannot_publish_a_baseline(
    baseline_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = baseline_case["baseline"]
    pointer_before = (root / baselines.POINTER_FILE).read_bytes()
    original = background.source_information

    def uncertain(preparation):
        information = deepcopy(original(preparation))
        information["limitations"].append("test source has no retained availability evidence")
        return information

    monkeypatch.setattr(background, "source_information", uncertain)
    with pytest.raises(prepared.SnapshotError, match="information|availability|cutoff|differs"):
        background.build_baseline(
            baseline_case["guidance"], root, LOCATIONS, reference_times=[TARGET]
        )
    assert (root / baselines.POINTER_FILE).read_bytes() == pointer_before


def test_source_publication_after_background_cutoff_is_rejected(baseline_case) -> None:
    root = baseline_case["baseline"]
    before = (root / baselines.POINTER_FILE).read_bytes()
    cutoff = datetime.fromisoformat(baseline_case["prepared"][0]["published_at"]) - timedelta(
        seconds=1
    )
    with pytest.raises(prepared.SnapshotError, match="availability|cutoff"):
        background.build_baseline(
            baseline_case["guidance"],
            root,
            LOCATIONS,
            reference_times=[TARGET],
            analysis_cutoff=cutoff,
        )
    assert (root / baselines.POINTER_FILE).read_bytes() == before


def test_failed_new_state_retains_previous_baseline_then_retry_publishes_without_repinning(
    baseline_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, guidance = tmp_path / "baseline", tmp_path / "guidance"
    shutil.copytree(baseline_case["baseline"], root)
    old = baselines.load_baseline(root)
    old_pointer = (root / baselines.POINTER_FILE).read_bytes()
    # Publish a distinct prepared identity, preserving the generated scientific inputs.
    source = deepcopy(baseline_case["prepared"][1])
    source["snapshot_id"] += "-retry"
    directory = guidance / prepared.SNAPSHOTS_DIRECTORY / source["snapshot_id"]
    directory.mkdir(parents=True)
    _, digest = prepared.write_manifest(directory, source)
    new_source = prepared.publish_latest_complete(
        guidance, source, digest, published_at=datetime.now(UTC)
    )
    source_pointer = (guidance / prepared.POINTER_FILE).read_bytes()
    with monkeypatch.context() as failing:
        failing.setattr(
            background, "load_preparation", Mock(side_effect=RuntimeError("injected blend failure"))
        )
        with pytest.raises(RuntimeError, match="injected"):
            background.build_baseline(guidance, root, LOCATIONS, reference_times=[TARGET])
    assert (root / baselines.POINTER_FILE).read_bytes() == old_pointer
    assert (guidance / prepared.POINTER_FILE).read_bytes() == source_pointer
    assert prepared.resolve_latest_complete(guidance)[0] == new_source

    def replay(*, latitude, longitude):
        return deepcopy(baseline_case["expected"][(latitude, longitude)])

    monkeypatch.setattr(
        background,
        "load_preparation",
        lambda _: SimpleNamespace(reference_view=lambda _: SimpleNamespace(forecast=replay)),
    )
    rebuilt = background.build_baseline(guidance, root, LOCATIONS, reference_times=[TARGET])
    assert rebuilt["status"] == "published"
    current = baselines.load_baseline(root)
    assert current.manifest["prepared_snapshot"]["snapshot_id"] == source["snapshot_id"]
    assert current.manifest["baseline_snapshot_id"] != old.manifest["baseline_snapshot_id"]
    # The object pinned before publication still reads A, not the newly current B.
    assert old.pointer["baseline_snapshot_id"] != current.pointer["baseline_snapshot_id"]
    assert canonical_json_bytes(
        old.reference_view(TARGET).forecast(latitude=FIRST["lat"], longitude=FIRST["lon"])
    ) == canonical_json_bytes(baseline_case["expected"][(FIRST["lat"], FIRST["lon"])])
    assert (guidance / prepared.POINTER_FILE).read_bytes() == source_pointer


@pytest.mark.parametrize("omission", ["hour", "field"])
def test_grid_artifact_tamper_and_incomplete_canvas_are_rejected(
    baseline_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, omission: str
) -> None:
    root = tmp_path / "copy"
    shutil.copytree(baseline_case["baseline"], root)
    pinned = baselines.load_baseline(root)
    domain = pinned.manifest["domains"][0]
    artifact = pinned.directory / domain["artifact"]["file"]
    artifact.write_bytes(artifact.read_bytes() + b"altered")
    with pytest.raises(prepared.SnapshotError, match="digest"):
        pinned.reference_view(TARGET).forecast(
            latitude=domain["latitude"], longitude=domain["longitude"]
        )
    before = (baseline_case["baseline"] / baselines.POINTER_FILE).read_bytes()

    def incomplete(*, latitude, longitude):
        forecast = deepcopy(baseline_case["expected"][(latitude, longitude)])
        hours = forecast["local_grid_baseline"]["cells"][0]["hours"]
        if omission == "hour":
            hours.pop()
        else:
            del hours[0]["surface"]["fields"]["dew_point_temperature_2m"]
        return forecast

    monkeypatch.setattr(
        background,
        "load_preparation",
        lambda _: SimpleNamespace(reference_view=lambda _: SimpleNamespace(forecast=incomplete)),
    )
    with pytest.raises(prepared.SnapshotError, match="incomplete|unordered|field|canvas"):
        background.build_baseline(
            baseline_case["guidance"], baseline_case["baseline"], [FIRST], reference_times=[TARGET]
        )
    assert (baseline_case["baseline"] / baselines.POINTER_FILE).read_bytes() == before


def test_coverage_counts_usable_fallback_as_available_even_with_exclusion_reasons(baseline_case):
    grid = deepcopy(baseline_case["expected"][(FIRST["lat"], FIRST["lon"])]["local_grid_baseline"])
    name = "wind_gust_10m"
    field = grid["cells"][0]["hours"][0]["surface"]["fields"][name]
    assert field["value"] is not None
    field["missing_reasons"] = ["GFS rejected; approved HRRR-only fallback applies"]
    counts = background._availability(grid, TARGET)
    assert counts[name]["available"] == 49 * 36
    assert counts[name]["missing"] == 0


def _publication_manifest(root: Path, name: str, minute: int) -> tuple[dict[str, Any], str]:
    """A publication-only completed manifest, with fixed reference and newer evidence."""
    time = datetime(2026, 9, 18, 12, minute, tzinfo=UTC).isoformat()
    manifest = {
        "schema_version": baselines.BASELINE_SCHEMA,
        "baseline_snapshot_id": name,
        "analysis_cutoff": time,
        "built_at": time,
        "completed_at": time,
        "completeness": {"status": "complete"},
        "artifacts": [],
        "prepared_snapshot": {
            "snapshot_id": f"prepared-{name}",
            "published_at": time,
            "coverage": {"reference_time": "2026-09-18T12:00:00Z"},
        },
        "coverage": {"reference_times": ["2026-09-18T12:00:00Z"]},
    }
    directory = root / baselines.BASELINES_DIRECTORY / name
    directory.mkdir(parents=True)
    _, digest = baselines.write_manifest(directory, manifest)
    return manifest, digest


def _publish_worker(root, manifest, digest, attempted, read, release):
    actual_read = baselines.read_pointer

    def held_read(path):
        result = actual_read(path)
        read.set()
        if release is not None and not release.wait(30):
            raise TimeoutError("Publication barrier not released")
        return result

    attempted.set()
    with patch.object(baselines, "read_pointer", held_read):
        try:
            baselines.publish_latest_baseline(
                root, manifest, digest, published_at=datetime.now(UTC)
            )
        except prepared.SnapshotError as exc:
            result = {"status": "rejected", "reason": str(exc)}
        else:
            result = {"status": "published"}
    (root / f"{manifest['baseline_snapshot_id']}.result.json").write_text(json.dumps(result))


@pytest.mark.parametrize("first_minute,second_minute", [(1, 2), (2, 1)])
def test_separate_process_publishers_serialize_compare_and_publish_at_same_reference(
    tmp_path: Path, first_minute: int, second_minute: int
) -> None:
    initial, initial_digest = _publication_manifest(tmp_path, "initial", 0)
    first_manifest, first_digest = _publication_manifest(tmp_path, "first", first_minute)
    second_manifest, second_digest = _publication_manifest(tmp_path, "second", second_minute)
    baselines.publish_latest_baseline(
        tmp_path, initial, initial_digest, published_at=datetime.now(UTC)
    )
    immutable = {path: path.read_bytes() for path in tmp_path.rglob(baselines.MANIFEST_FILE)}
    context = multiprocessing.get_context("spawn")
    attempted_first, read_first, release = (context.Event() for _ in range(3))
    attempted_second, read_second = context.Event(), context.Event()
    first = context.Process(
        target=_publish_worker,
        args=(tmp_path, first_manifest, first_digest, attempted_first, read_first, release),
    )
    second = context.Process(
        target=_publish_worker,
        args=(tmp_path, second_manifest, second_digest, attempted_second, read_second, None),
    )
    first.start()
    try:
        assert read_first.wait(30)
        second.start()
        assert attempted_second.wait(30)
        read_without_lock = read_second.wait(1)
    finally:
        release.set()
        for child in (first, second):
            if child.pid is not None:
                child.join(30)
                if child.is_alive():
                    child.terminate()
                    child.join(10)
    assert first.exitcode == second.exitcode == 0
    assert not read_without_lock, "Second publisher observed a stale pointer outside the OS lock"
    pointer = baselines.read_pointer(tmp_path)
    assert pointer["baseline_snapshot_id"] == ("second" if second_minute == 2 else "first")
    assert {path: path.read_bytes() for path in immutable} == immutable


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_failed_publication_preserves_current_and_retry_can_publish(
    tmp_path, monkeypatch, operation
):
    original, original_digest = _publication_manifest(tmp_path, "original", 0)
    newer, newer_digest = _publication_manifest(tmp_path, "newer", 1)
    baselines.publish_latest_baseline(
        tmp_path, original, original_digest, published_at=datetime.now(UTC)
    )
    before = (tmp_path / baselines.POINTER_FILE).read_bytes()
    with monkeypatch.context() as patcher:
        patcher.setattr(
            prepared.os, operation, Mock(side_effect=OSError("injected publication failure"))
        )
        with pytest.raises(OSError, match="injected"):
            baselines.publish_latest_baseline(
                tmp_path, newer, newer_digest, published_at=datetime.now(UTC)
            )
    assert (tmp_path / baselines.POINTER_FILE).read_bytes() == before
    assert not list(tmp_path.glob(f".{baselines.POINTER_FILE}.*.tmp"))
    baselines.publish_latest_baseline(tmp_path, newer, newer_digest, published_at=datetime.now(UTC))
    assert baselines.read_pointer(tmp_path)["baseline_snapshot_id"] == "newer"
