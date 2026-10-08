"""Guidance/baseline worker decisions, persistence, failure isolation and shutdown.

Offline: prepared/baseline pointers are small on-disk fakes that satisfy the real
pointer readers; refresh, discovery, build and governance are injected fakes.
"""

from __future__ import annotations

import hashlib
import io
import json
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from mesoforge.application import guidance_worker as worker_module
from mesoforge.application.baseline_snapshot import BASELINE_SCHEMA
from mesoforge.application.baseline_snapshot import POINTER_SCHEMA as BASELINE_POINTER
from mesoforge.application.forecast_schedule import ForecastSchedule
from mesoforge.application.guidance_worker import (
    GuidanceWorker,
    WorkerDeps,
    WorkerSettings,
    backoff_seconds,
    single_writer,
)
from mesoforge.application.prepared_snapshot import POINTER_SCHEMA, SNAPSHOT_SCHEMA, _iso
from mesoforge.application.worker_status import (
    GUIDANCE_HEARTBEAT,
    GUIDANCE_STATUS,
    HEARTBEAT_STALE_SECONDS,
    guidance_health,
    read_json,
    write_json,
)
from mesoforge.common.horizon import FIVE_DAY_HORIZON, LEGACY_HORIZON
from mesoforge.contracts.policy_governance import blend_scope

LOCATIONS = [
    {"id": "minneapolis", "lat": 44.98861, "lon": -93.25553, "display_timezone": "America/Chicago"},
    {"id": "surley", "lat": 44.97304, "lon": -93.20901, "display_timezone": "America/Chicago"},
    {"id": "grasston", "lat": 45.80268, "lon": -93.07952, "display_timezone": "America/Chicago"},
]
# 2026-07-01 09:05Z: next slot 13:00Z (08:00 CDT), so this is not the pre-slot hour.
START = datetime(2026, 7, 1, 9, 5, tzinfo=UTC)
CYCLES = {"HRRR": "2026-07-01T06:00:00+00:00", "GFS": "2026-07-01T00:00:00+00:00"}


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: float) -> None:
        self.value += timedelta(**delta)


def write_config(tmp_path: Path, locations: list[dict] = LOCATIONS) -> Path:
    path = tmp_path / "locations.json"
    path.write_text(json.dumps({"locations": locations}), encoding="utf-8")
    return path


def publish_prepared(
    root: Path,
    reference: datetime,
    *,
    hours: int = 40,
    cycles: dict[str, str] = CYCLES,
    locations: list[dict] = LOCATIONS,
) -> dict[str, Any]:
    guidance = root / "guidance"
    snapshot_id = f"{reference:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    directory = guidance / "snapshots" / snapshot_id
    directory.mkdir(parents=True)
    valid = [_iso(reference + timedelta(hours=hour)) for hour in range(1, hours + 1)]
    manifest = {
        "schema_version": SNAPSHOT_SCHEMA,
        "snapshot_id": snapshot_id,
        "coverage": {
            "reference_time": _iso(reference),
            "first_valid_time": valid[0],
            "last_valid_time": valid[-1],
            "usability_rule": {"required_complete": ["HRRR", "GFS"]},
        },
        "contributors": {
            "HRRR": {"cycle": cycles["HRRR"], "valid_times": valid},
            "GFS": {"cycle": cycles["GFS"], "valid_times": valid},
            "NBM": {"products": {}},
        },
    }
    (directory / "locations.json").write_text(json.dumps({"locations": locations}), "utf-8")
    payload = json.dumps(manifest).encode()
    (directory / "snapshot.json").write_bytes(payload)
    pointer = {
        "schema_version": POINTER_SCHEMA,
        "snapshot_id": snapshot_id,
        "snapshot_directory": f"snapshots/{snapshot_id}",
        "manifest_file": "snapshot.json",
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "reference_time": _iso(reference),
        "first_valid_time": valid[0],
        "last_valid_time": valid[-1],
        "published_at": _iso(reference + timedelta(minutes=20)),
    }
    (guidance / "latest_complete.json").write_text(json.dumps(pointer), "utf-8")
    return pointer


def publish_baseline(
    root: Path,
    prepared_id: str,
    *,
    status: str = "resolved",
    heads: dict[str, Any] | None = None,
    locations: list[dict] = LOCATIONS,
    failed: list[dict] | None = None,
    code_revision: str = "a" * 40,
) -> str:
    baseline = root / "baseline"
    identity = uuid4().hex
    directory = baseline / "baselines" / identity
    directory.mkdir(parents=True)
    manifest = {
        "schema_version": BASELINE_SCHEMA,
        "baseline_snapshot_id": identity,
        "code_revision": code_revision,
        "completeness": {"status": "complete"},
        "prepared_snapshot": {"snapshot_id": prepared_id},
        "blend_governance": {"status": status, "heads": heads or {}},
        "domains": [{"latitude": row["lat"], "longitude": row["lon"]} for row in locations],
        "coverage": {"failed_locations": failed or [], "reference_times": []},
    }
    payload = json.dumps(manifest).encode()
    (directory / "baseline.json").write_bytes(payload)
    pointer = {
        "schema_version": BASELINE_POINTER,
        "baseline_snapshot_id": identity,
        "baseline_directory": f"baselines/{identity}",
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "published_at": _iso(START),
    }
    (baseline / "latest_baseline.json").write_text(json.dumps(pointer), "utf-8")
    return identity


class FakeLearning:
    def __init__(self) -> None:
        self.background_calls: list[Any] = []

    def background(self, pinned: Any, candidates: list[Any], *, analysis_cutoff: datetime) -> dict:
        self.background_calls.append((pinned, candidates, analysis_cutoff))
        return {"overlays": [{"artifact_id": "art_x"}] * len(candidates), "failures": []}


class FakeGovernance:
    def __init__(self) -> None:
        self.heads: dict[str, str] = {}
        self.revoked = False
        self.shadows: list[Any] = []
        self.learning = FakeLearning()

    def snapshot(self, family: str, keys: Any, at: datetime) -> Any:
        scopes = {
            blend_scope(field): SimpleNamespace(head_event_id=head, shadows=())
            for field, head in self.heads.items()
        }
        if self.shadows:
            scopes[blend_scope("candidate_field")] = SimpleNamespace(
                head_event_id=None, shadows=tuple(self.shadows)
            )
        return SimpleNamespace(scopes=scopes)

    def blend_revoked(self, blend: dict) -> bool:
        return self.revoked


class Harness:
    """A worker over fakes whose effects are recorded."""

    def __init__(self, tmp_path: Path, *, clock: datetime = START, **settings: Any) -> None:
        self.root = tmp_path / "runtime"
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = Clock(clock)
        self.governance = FakeGovernance()
        self.discovered = {"status": "selected", "selected_cycles": dict(CYCLES)}
        self.discover_error: Exception | None = None
        self.refresh_result: dict[str, Any] | None = None
        self.refresh_calls: list[tuple[Path, Path | None]] = []
        self.build_calls: list[dict[str, Any]] = []
        self.build_error: Exception | None = None
        self.schema_result: dict[str, Any] | Exception = {
            "current": "h",
            "head": "h",
            "at_head": True,
        }
        self.free = 100 * 1024**3
        self.stream = io.StringIO()
        self.settings = WorkerSettings(root=self.root, config=write_config(self.root), **settings)
        self.deps = WorkerDeps(
            clock=self.clock,
            schema=self.schema,
            governance=lambda: self.governance,
            discover=self.discover,
            refresh=self.refresh,
            build=self.build,
            disk_free=lambda _path: self.free,
            read_revision=lambda: "a" * 40,
            monotonic=lambda: self.clock().timestamp(),
            readiness=lambda root, locations, **kwargs: {
                "ready": True,
                "reasons": [],
                "baseline": None,
            },
        )

    def schema(self) -> dict[str, Any]:
        if isinstance(self.schema_result, Exception):
            raise self.schema_result
        return self.schema_result

    def discover(self, directory: Path) -> dict[str, Any]:
        if self.discover_error is not None:
            raise self.discover_error
        directory.mkdir(parents=True)
        report = {**self.discovered, "target_reference_time": _iso(self.hour())}
        (directory / "selection.json").write_text(json.dumps(report), "utf-8")
        return report

    def hour(self) -> datetime:
        return self.clock().replace(minute=0, second=0, microsecond=0)

    def refresh(self, config: Path, guidance_root: Path, probe: Path | None) -> dict[str, Any]:
        self.refresh_calls.append((config, probe))
        assert json.loads(config.read_text("utf-8"))["locations"] == LOCATIONS
        if self.refresh_result is not None:
            return self.refresh_result
        cycles = self.discovered.get("selected_cycles") or CYCLES
        pointer = publish_prepared(self.root, self.hour(), cycles=cycles)
        return {
            "status": "published",
            "snapshot_id": pointer["snapshot_id"],
            "latest_complete": pointer,
            "selection": {"selected_cycles": cycles, "shadow_discovery_shortfalls": {"IFS": {}}},
            "downloaded_bytes": 1234,
            "steps": [{"step": "discover", "status": "ok", "seconds": 1.0}],
        }

    def build(
        self, guidance_root: Path, baseline_root: Path, locations: list[Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.build_calls.append(kwargs)
        if self.build_error is not None:
            raise self.build_error
        prepared = kwargs["prepared_pointer"]["snapshot_id"]
        heads = {
            field: {"head_event_id": head, "scope_seq": 1}
            for field, head in self.governance.heads.items()
        }
        identity = publish_baseline(self.root, prepared, heads=heads)
        return {
            "status": "published",
            "baseline_snapshot_id": identity,
            "manifest": {"coverage": {"failed_locations": [], "reference_times": []}},
            "timings": {"total_seconds": 1.0},
            "artifact_bytes": 10,
        }

    def worker(self) -> GuidanceWorker:
        worker = GuidanceWorker(self.settings, self.deps, stream=self.stream)
        worker.code_revision = "a" * 40
        return worker

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


def test_backoff_never_retries_sooner_than_the_interval_and_is_capped() -> None:
    values = [backoff_seconds(n, base=300, interval=300, cap=3600) for n in range(7)]
    assert values == [300, 300, 600, 1200, 2400, 3600, 3600]
    assert backoff_seconds(1, base=30, interval=300, cap=3600) == 300


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        WorkerSettings(root=Path("x"), refresh="sometimes")
    with pytest.raises(ValueError):
        WorkerSettings(root=Path("x"), backoff_cap_seconds=600)
    with pytest.raises(ValueError):
        WorkerSettings(root=Path("x"), interval_seconds=1)


def test_hosted_horizon_is_explicit_and_legacy_prepared_state_cannot_satisfy_it(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MESOFORGE_FORECAST_HORIZON_HOURS", raising=False)
    assert worker_module.build_parser().parse_args(["once"]).forecast_horizon_hours == 36
    monkeypatch.setenv("MESOFORGE_FORECAST_HORIZON_HOURS", "120")
    assert worker_module.build_parser().parse_args(["once"]).forecast_horizon_hours == 120
    legacy = WorkerSettings(root=tmp_path)
    assert legacy.forecast_horizon == LEGACY_HORIZON and legacy.coverage_hours == 42
    settings = WorkerSettings(root=tmp_path, forecast_horizon=FIVE_DAY_HORIZON)
    assert settings.coverage_hours == 126
    with pytest.raises(ValueError, match="Prepared coverage"):
        WorkerSettings(root=tmp_path, forecast_horizon=FIVE_DAY_HORIZON, coverage_hours=42)
    harness = Harness(tmp_path, forecast_horizon=FIVE_DAY_HORIZON)
    pointer = publish_prepared(harness.root, harness.hour())
    publish_baseline(harness.root, pointer["snapshot_id"])
    worker = harness.worker()
    assert "current_hour_not_covered" in worker.refresh_reasons(LOCATIONS, harness.clock())
    assert worker.build_inputs(SimpleNamespace(scopes={}), LOCATIONS) is None


@pytest.mark.parametrize("horizon", [LEGACY_HORIZON, FIVE_DAY_HORIZON])
def test_default_dependencies_pass_one_explicit_horizon_to_existing_boundaries(
    tmp_path, monkeypatch, horizon
):
    from unittest.mock import Mock

    from mesoforge.application import refresh_guidance as refresh_module

    discover = Mock(return_value={"status": "selected"})
    steps = SimpleNamespace(discover=discover)
    default = Mock(return_value=steps)
    refresh = Mock(return_value={"status": "published"})
    monkeypatch.setattr(refresh_module, "default_steps", default)
    monkeypatch.setattr(refresh_module, "refresh_guidance", refresh)
    settings = WorkerSettings(root=tmp_path, forecast_horizon=horizon)
    deps = worker_module.default_deps(settings)
    deps.discover(tmp_path / "probe")
    deps.refresh(tmp_path / "locations.json", tmp_path / "guidance", None)
    options = {"forecast_horizon": horizon} if horizon != LEGACY_HORIZON else {}
    assert default.call_count == 2
    default.assert_called_with(coverage_hours=horizon.duration_hours + 6, **options)
    refresh.assert_called_once_with(
        tmp_path / "locations.json",
        tmp_path / "guidance",
        coverage_hours=horizon.duration_hours + 6,
        steps=steps,
        **options,
    )


def test_first_poll_refreshes_and_builds_then_nothing_repeats_in_the_same_hour(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()
    first = worker.poll_once()
    assert first["refresh"]["decision"] == "refresh"
    assert first["refresh"]["reasons"] == ["no_prepared_state"]
    assert first["refresh"]["shadow_discovery_shortfalls"] == ["IFS"]
    assert first["build"]["decision"] == "build"
    assert first["build"]["reasons"] == ["no_baseline"]
    assert first["build"]["outcome"] == "published"
    assert first["categories"] == []
    assert harness.build_calls[0]["governance"] is harness.governance
    harness.clock.advance(minutes=5)
    second = worker.poll_once()
    assert second["refresh"] == {
        "decision": "none",
        "reasons": [],
        "blocked_by": "refreshed_this_hour",
    }
    assert second["build"]["decision"] == "current"
    assert len(harness.refresh_calls) == 1 and len(harness.build_calls) == 1
    saved = read_json(harness.root / "status" / GUIDANCE_STATUS)
    assert saved is not None and saved["polls"]["count"] == 2
    assert saved["refresh"]["succeeded_hours"] == [_iso(harness.hour())]


def test_no_material_change_downloads_nothing_and_probes_once_per_hour(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    worker = harness.worker()
    outcome = worker.poll_once()
    assert outcome["refresh"]["decision"] == "no_material_change"
    assert worker.state["refresh"]["last_successful_probe"]["status"] == "selected"
    assert outcome["refresh"]["probe"]["newer_required_cycles"] == []
    assert outcome["build"]["decision"] == "current"
    assert harness.refresh_calls == [] and harness.build_calls == []
    harness.clock.advance(minutes=10)
    again = worker.poll_once()
    assert again["refresh"]["probe"] == "already_probed_this_hour"
    # Probe scratch never accumulates.
    assert not any((harness.root / "discovery" / "worker").iterdir())


def test_hourly_probe_can_be_disabled_to_save_bandwidth(tmp_path: Path) -> None:
    harness = Harness(tmp_path, hourly_probe=False)
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    harness.deps.discover = lambda directory: pytest.fail("probe must not run")
    outcome = harness.worker().poll_once()
    assert outcome["refresh"] == {"decision": "none", "reasons": [], "probe": "disabled"}


def test_newer_required_cycle_refreshes_with_the_probe_selection_then_rebuilds(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    harness.discovered = {
        "status": "selected",
        "selected_cycles": {**CYCLES, "GFS": "2026-07-01T06:00:00+00:00"},
    }
    outcome = harness.worker().poll_once()
    assert outcome["refresh"]["reasons"] == ["newer_required_cycle:GFS"]
    assert outcome["refresh"]["status"] == "published"
    assert harness.refresh_calls[0][1] is not None  # probe evidence reused, not rediscovered
    assert outcome["build"]["reasons"] == ["new_prepared_snapshot"]


@pytest.mark.parametrize(
    "native,selected,held,expected",
    [
        (True, {"NBM": "2026-07-01T07:00:00Z"}, None, ["newer_participating_cycle:NBM"]),
        (
            True,
            {"GFS": "2026-07-01T06:00:00Z"},
            "2026-07-01T00:00:00Z",
            ["newer_participating_cycle:GFS"],
        ),
        (True, {"RAP": "2026-07-01T06:00:00Z"}, "2026-07-01T06:00:00Z", []),
        (True, {}, "2026-07-01T06:00:00Z", []),
        (False, {"NBM": "2026-07-01T07:00:00Z"}, None, []),
    ],
)
def test_native_probe_refreshes_for_eligible_participating_cycles_without_required_set(
    tmp_path: Path, monkeypatch, native: bool, selected: dict, held: str | None, expected: list
) -> None:
    horizon = FIVE_DAY_HORIZON if native else LEGACY_HORIZON
    harness = Harness(tmp_path, forecast_horizon=horizon)
    harness.discovered["selected_cycles"] = selected
    worker = harness.worker()
    manifest = {
        "forecast_horizon": horizon.payload(),
        "coverage": {"usability_rule": {"required_complete": [] if native else ["HRRR", "GFS"]}},
        "contributors": {model: {"cycle": held} for model in ("HRRR", "RAP", "GFS", "IFS", "NBM")},
    }
    monkeypatch.setattr(worker, "_prepared", lambda: ({}, manifest, tmp_path))
    # The existing coverage and spatial contracts already proved this state
    # usable. Discovery must independently recognize changed native inputs.
    monkeypatch.setattr(worker, "refresh_reasons", lambda locations, now: [])
    calls = []

    def refresh(reasons, locations, selection, categories):
        calls.append(selection)
        return {"reasons": reasons}

    monkeypatch.setattr(worker, "_run_refresh", refresh)
    result = worker._refresh_step(LOCATIONS, harness.free, [])
    assert result["reasons"] == expected
    assert len(calls) == bool(expected)
    if calls:
        assert (calls[0] / "selection.json").is_file()
    else:
        assert result["decision"] == "no_material_change"


def test_reused_probe_is_copied_only_within_its_hour(tmp_path: Path) -> None:
    source = tmp_path / "probe"
    source.mkdir()
    report = {"status": "selected", "target_reference_time": "2026-07-01T09:00:00+00:00"}
    (source / "selection.json").write_text(json.dumps(report), "utf-8")
    fallback_calls: list[Path] = []

    def fallback(directory: Path) -> dict:
        fallback_calls.append(directory)
        return {"status": "fallback"}

    same = worker_module._reuse_probe(source, fallback, lambda: START)
    assert same(tmp_path / "a")["status"] == "selected"
    assert (tmp_path / "a" / "selection.json").is_file() and not fallback_calls
    later = worker_module._reuse_probe(source, fallback, lambda: START + timedelta(hours=1))
    assert later(tmp_path / "b") == {"status": "fallback"}


@pytest.mark.parametrize(
    ("clock", "reference", "hours", "expected"),
    [
        (START, START.replace(minute=0) - timedelta(hours=3), 38, ["current_hour_not_covered"]),
        (START, START.replace(minute=0) - timedelta(hours=1), 38, ["coverage_expiring"]),
        # 12:10Z is the hour before the 13:00Z slot: refresh for fresh slot guidance.
        (
            START.replace(hour=12, minute=10),
            START.replace(hour=11, minute=0),
            42,
            ["pre_slot_refresh"],
        ),
    ],
)
def test_refresh_reasons_come_from_coverage_and_the_schedule(
    tmp_path: Path, clock: datetime, reference: datetime, hours: int, expected: list[str]
) -> None:
    harness = Harness(tmp_path, clock=clock)
    publish_prepared(harness.root, reference, hours=hours)
    assert harness.worker().refresh_reasons(LOCATIONS, clock) == expected


def test_configured_location_outside_the_prepared_footprint_requires_refresh(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1), locations=LOCATIONS[:2])
    assert harness.worker().refresh_reasons(LOCATIONS, START) == ["configured_locations_changed"]


def test_partial_provider_probe_and_provider_outage_are_categorized(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    harness.discover_error = ConnectionError("HTTP 503 from provider index")
    outcome = harness.worker().poll_once()
    assert "provider_unavailable" in outcome["categories"]
    assert outcome["refresh"]["probe"]["status"] == "failed"
    assert harness.refresh_calls == []
    assert outcome["build"]["decision"] == "current"  # the valid baseline stays served


def test_failed_refresh_keeps_latest_backs_off_and_caps_attempts_per_hour(tmp_path: Path) -> None:
    harness = Harness(tmp_path, latest_start_minute=59)
    harness.refresh_result = {
        "status": "failed",
        "error": "SelectedObjectError: 503",
        "latest_complete_unchanged": True,
        "steps": [
            {"step": "discover", "status": "ok"},
            {"step": "prepare_selected_with_pop", "status": "error"},
        ],
    }
    worker = harness.worker()
    first = worker.poll_once()
    assert first["refresh"]["category"] == "guidance_refresh_failed"
    assert first["refresh"]["failed_step"] == "prepare_selected_with_pop"
    assert first["refresh"]["retry_after_seconds"] == 300
    assert "guidance_refresh_failed" in first["categories"]
    harness.clock.advance(minutes=1)
    assert worker.poll_once()["refresh"]["blocked_by"] == "backoff"
    harness.clock.advance(minutes=5)
    second = worker.poll_once()
    assert second["refresh"]["retry_after_seconds"] == 600
    harness.clock.advance(minutes=11)
    assert worker.poll_once()["refresh"]["blocked_by"] == "hour_attempt_budget_exhausted"
    assert len(harness.refresh_calls) == 2
    harness.refresh_result["steps"] = [{"step": "discover", "status": "error"}]
    harness.clock.value = START + timedelta(hours=1, minutes=30)
    third = worker.poll_once()
    assert third["refresh"]["category"] == "provider_unavailable"
    # Discovery that returns no complete model set stops the refresh right after it.
    harness.refresh_result["steps"] = [{"step": "discover", "status": "ok"}]
    harness.clock.value = START + timedelta(hours=3, minutes=5)
    worker.state["refresh"]["next_allowed_at"] = None
    fourth = worker.poll_once()
    assert fourth["refresh"]["category"] == "provider_unavailable"
    assert fourth["refresh"]["failed_step"] == "discover"


def test_disk_floor_late_hour_and_disabled_refresh_gate_downloads(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.free = 1
    outcome = harness.worker().poll_once()
    assert outcome["refresh"]["blocked_by"] == "disk_low"
    assert "disk_low" in outcome["categories"]
    late = Harness(tmp_path / "late", clock=START.replace(minute=45))
    assert late.worker().poll_once()["refresh"]["blocked_by"] == "too_late_in_hour"
    off = Harness(tmp_path / "off", refresh="off")
    assert off.worker().poll_once()["refresh"]["blocked_by"] == "refresh_disabled"
    assert harness.refresh_calls == late.refresh_calls == off.refresh_calls == []


def test_build_only_mode_builds_from_retained_guidance_without_downloads(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=5), hours=36)
    outcome = harness.worker().poll_once()
    assert outcome["refresh"]["blocked_by"] == "refresh_disabled"
    assert outcome["build"]["outcome"] == "published"
    assert harness.refresh_calls == []


def test_deterministic_build_failure_is_attempted_once_per_fingerprint(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    from mesoforge.application.prepared_snapshot import SnapshotError

    harness.build_error = SnapshotError("No configured domain could be built")
    previous = publish_baseline(harness.root, "older-prepared-snapshot")
    pointer = (harness.root / "baseline" / "latest_baseline.json").read_bytes()
    worker = harness.worker()
    first = worker.poll_once()
    assert first["build"]["outcome"] == "failed"
    # The failed build never touched the published pointer.
    assert (harness.root / "baseline" / "latest_baseline.json").read_bytes() == pointer
    assert worker.state["build"]["last_published"] is None and previous
    assert "baseline_build_failed" in first["categories"]
    harness.clock.advance(minutes=5)
    second = worker.poll_once()
    assert second["build"]["blocked_by"] == "fingerprint_already_attempted"
    assert len(harness.build_calls) == 1
    # New code or new inputs form a new fingerprint and are attempted again.
    worker.code_revision = "b" * 40
    harness.build_error = None
    assert worker.poll_once()["build"]["outcome"] == "published"


def test_retryable_build_failure_backs_off_then_retries(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    harness.build_error = ConnectionError("object store temporarily unavailable")
    worker = harness.worker()
    assert worker.poll_once()["build"]["outcome"] == "retryable_failure"
    harness.clock.advance(minutes=1)
    assert worker.poll_once()["build"]["blocked_by"] == "backoff"
    harness.build_error = None
    harness.clock.advance(minutes=5)
    assert worker.poll_once()["build"]["outcome"] == "published"


def test_revoked_or_changed_governed_blend_rebuilds_the_same_guidance(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    field = "dew_point_temperature_2m"
    harness.governance.heads = {field: "gev_1"}
    publish_baseline(
        harness.root,
        pointer["snapshot_id"],
        heads={field: {"head_event_id": "gev_1", "scope_seq": 3}},
    )
    worker = harness.worker()
    assert worker.poll_once()["build"]["decision"] == "current"
    harness.governance.revoked = True
    harness.governance.heads = {field: "gev_2"}
    outcome = worker.poll_once()
    assert outcome["build"]["reasons"] == ["blend_revoked", "blend_policy_changed"]
    assert harness.build_calls[0]["prepared_pointer"]["snapshot_id"] == pointer["snapshot_id"]


def test_ungoverned_baseline_and_missing_location_trigger_rebuild(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(
        harness.root, pointer["snapshot_id"], status="not_configured", locations=LOCATIONS[:2]
    )
    outcome = harness.worker().poll_once()
    assert outcome["build"]["reasons"] == [
        "baseline_governance_unproven",
        "configured_location_missing",
    ]


def test_location_that_failed_in_the_build_is_reported_not_rebuilt(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    failed = [{"latitude": LOCATIONS[2]["lat"], "longitude": LOCATIONS[2]["lon"]}]
    publish_baseline(harness.root, pointer["snapshot_id"], locations=LOCATIONS[:2], failed=failed)
    assert harness.worker().poll_once()["build"]["decision"] == "current"


def test_worker_reconnects_after_a_database_outage(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    harness.schema_result = ConnectionError("connection refused")
    worker = harness.worker()
    assert worker.poll_once()["categories"] == ["database_unavailable"]
    # The poll cadence is unchanged: refresh needs no database and recovery is prompt.
    assert worker.state["polls"]["consecutive_failures"] == 0
    harness.schema_result = {"current": "h", "head": "h", "at_head": True}
    harness.clock.advance(minutes=5)
    recovered = worker.poll_once()
    assert recovered["categories"] == [] and recovered["build"]["outcome"] == "published"
    assert worker.state["polls"]["consecutive_failures"] == 0
    assert worker.state["build"]["last_published"]["outcome"] == "published"
    assert worker.state["governance"]["status"] == "resolved"


def test_schema_not_at_head_does_no_work_and_database_outage_still_refreshes(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.schema_result = {"current": "0004", "head": "0005", "at_head": False}
    outcome = harness.worker().poll_once()
    assert outcome["categories"] == ["schema_not_at_head"]
    assert "refresh" not in outcome and harness.refresh_calls == []
    outage = Harness(tmp_path / "outage")
    outage.schema_result = ConnectionError("connection refused")
    result = outage.worker().poll_once()
    assert result["categories"] == ["database_unavailable"]
    assert result["refresh"]["status"] == "published"  # guidance needs no database
    assert result["build"] == {"decision": "deferred", "blocked_by": "database_unavailable"}


def test_governance_outage_defers_build_and_background(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))

    def unavailable() -> Any:
        raise RuntimeError("governance_read_failed")

    harness.deps.governance = unavailable
    outcome = harness.worker().poll_once()
    assert outcome["categories"] == ["governance_unavailable"]
    assert outcome["build"]["blocked_by"] == "governance_unavailable"
    assert harness.build_calls == []


def test_registered_candidates_shadow_the_current_baseline_every_poll(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    harness.governance.shadows = [SimpleNamespace(policy_artifact_id="art_candidate")]
    loaded: list[Any] = []

    def load(root: Path, *, pointer: dict) -> Any:
        loaded.append(pointer["baseline_snapshot_id"])
        return SimpleNamespace(pointer=pointer)

    original = worker_module.load_baseline
    worker_module.load_baseline = load  # type: ignore[assignment]
    try:
        outcome = harness.worker().poll_once()
    finally:
        worker_module.load_baseline = original  # type: ignore[assignment]
    assert outcome["background"]["overlays"] == 1
    assert len(harness.governance.learning.background_calls) == 1
    assert loaded


def test_interrupted_phase_counts_as_failure_after_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    status = harness.root / "status" / GUIDANCE_STATUS
    state = worker_module.new_state()
    state["in_flight"] = {"phase": "refresh", "started_at": _iso(START - timedelta(minutes=3))}
    state["refresh"]["attempts"] = {_iso(harness.hour()): 1}
    write_json(status, state)
    worker = harness.worker()
    record = worker.recover_interrupted()
    assert record is not None and record["phase"] == "refresh"
    assert worker.state["refresh"]["consecutive_failures"] == 1
    assert worker.state["in_flight"] is None
    outcome = worker.poll_once()
    assert outcome["refresh"]["blocked_by"] == "backoff"
    assert harness.refresh_calls == []


def test_state_survives_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.worker().run(once=True, install_signals=False) == 0
    restarted = harness.worker()
    assert restarted.state["refresh"]["succeeded_hours"] == [_iso(harness.hour())]
    assert restarted.state["build"]["fingerprints"]
    harness.clock.advance(minutes=5)
    assert restarted.run(once=True, install_signals=False) == 0
    assert len(harness.refresh_calls) == 1 and len(harness.build_calls) == 1
    saved = read_json(harness.root / "status" / GUIDANCE_STATUS)
    assert saved is not None and saved["state"] == "stopped"


def test_single_writer_refuses_a_second_worker_on_the_same_root(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with single_writer(harness.root) as owned:
        assert owned
        assert harness.worker().run(once=True, install_signals=False) == 2
    assert any(row["event"] == "worker_busy" for row in harness.events())


def test_single_writer_lock_excludes_an_independent_process_and_releases(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from mesoforge.application.guidance_worker import single_writer\n"
        "with single_writer(Path(sys.argv[1])) as owned:\n"
        "    print('owned' if owned else 'busy')\n"
    )

    def independent_attempt() -> str:
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    with single_writer(root) as owned:
        assert owned
        assert independent_attempt() == "busy"
    assert independent_attempt() == "owned"


def test_signal_stops_the_loop_after_the_current_poll(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    worker = harness.worker()
    worker.heartbeat_seconds = 0.01
    worker.deps.monotonic = __import__("time").monotonic
    polls: list[int] = []
    original = worker.poll_once

    def poll() -> dict:
        result = original()
        polls.append(result["poll"])
        worker._handle_signal(signal.SIGTERM, None)
        return result

    worker.poll_once = poll  # type: ignore[method-assign]
    assert worker.run(install_signals=False, exit_process=lambda code: None) == 0
    assert polls == [1]
    names = [row["event"] for row in harness.events()]
    assert "stop_requested" in names and names[-1] == "worker_stopped"
    with pytest.raises(SystemExit):
        worker._handle_signal(signal.SIGTERM, None)


@pytest.mark.parametrize("once", [True, False])
def test_long_poll_keeps_heartbeat_fresh_in_once_and_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, once: bool
) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()
    worker.heartbeat_seconds = 0.01
    advanced, beat_seen, watchdog_finished = threading.Event(), threading.Event(), threading.Event()
    health: list[dict[str, Any]] = []
    original_beat, original_watchdog = worker.beat, worker._watchdog
    original_refresh, original_poll = harness.deps.refresh, worker.poll_once

    def beat() -> None:
        before = harness.clock()
        original_beat()
        if advanced.is_set() and before == harness.clock():
            beat_seen.set()

    def watchdog(done: threading.Event, exit_process: Any) -> None:
        try:
            original_watchdog(done, exit_process)
        finally:
            watchdog_finished.set()

    def refresh(config: Path, root: Path, probe: Path | None) -> dict[str, Any]:
        harness.clock.advance(seconds=HEARTBEAT_STALE_SECONDS + 1)
        advanced.set()
        assert beat_seen.wait(timeout=5), "Busy worker did not refresh its heartbeat"
        health.append(guidance_health(harness.root, now=harness.clock()))
        return original_refresh(config, root, probe)

    def poll() -> dict[str, Any]:
        outcome = original_poll()
        if not once:
            worker.stop.set()  # Stop the normal loop after the same one-poll exercise.
        return outcome

    monkeypatch.setattr(worker, "beat", beat)
    monkeypatch.setattr(worker, "_watchdog", watchdog)
    monkeypatch.setattr(worker, "poll_once", poll)
    harness.deps.refresh = refresh
    exits: list[int] = []
    assert worker.run(once=once, install_signals=False, exit_process=exits.append) == 0
    assert len(health) == 1 and health[0]["healthy"] is True
    assert health[0]["heartbeat_age_seconds"] == 0
    assert health[0]["in_flight"]["phase"] == "refresh"
    assert health[0]["in_flight"]["age_seconds"] == HEARTBEAT_STALE_SECONDS + 1
    assert worker.state["polls"]["count"] == 1 and watchdog_finished.is_set()
    assert not exits
    assert guidance_health(harness.root, now=harness.clock())["reason"] == "worker_stopped"


@pytest.mark.parametrize("bound", ["phase", "poll"])
def test_once_enforces_existing_watchdog_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()
    worker.heartbeat_seconds = 0.01
    expired = threading.Event()
    exits: list[int] = []

    def exit_process(code: int) -> None:
        exits.append(code)  # Production uses os._exit; never terminate the test process.
        expired.set()

    def blocked_poll(outcome: dict[str, Any], categories: list[str]) -> None:
        if bound == "phase":
            with worker.phase("refresh"):
                harness.clock.advance(seconds=worker_module.phase_bound("refresh") + 1)
                assert expired.wait(timeout=5), "Single-poll phase bound was not enforced"
        else:
            harness.clock.advance(seconds=worker_module.POLL_BOUND_SECONDS + 1)
            assert expired.wait(timeout=5), "Single-poll total bound was not enforced"

    monkeypatch.setattr(worker, "_poll", blocked_poll)
    worker.run(once=True, install_signals=False, exit_process=exit_process)
    assert exits == [70]
    assert any(row["event"] == "watchdog_exit" for row in harness.events())


def test_watchdog_exits_when_a_phase_exceeds_its_bound(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()
    worker.heartbeat_seconds = 0.01
    worker.in_flight = {"phase": "probe", "started_at": _iso(START - timedelta(hours=2))}
    exits: list[int] = []
    done = threading.Event()
    thread = threading.Thread(target=worker._watchdog, args=(done, exits.append))
    thread.start()
    thread.join(timeout=5)
    done.set()
    assert exits == [70]
    # A poll stuck outside any bounded phase is also not progressing.
    worker.in_flight = None
    worker.poll_started_at = _iso(START - timedelta(hours=5))
    exits.clear()
    done = threading.Event()
    thread = threading.Thread(target=worker._watchdog, args=(done, exits.append))
    thread.start()
    thread.join(timeout=5)
    done.set()
    assert exits == [70]


def test_health_is_process_health_not_readiness(tmp_path: Path) -> None:
    root = tmp_path
    now = START
    assert guidance_health(root, now=now)["reason"] == "no_status"
    write_json(root / "status" / GUIDANCE_STATUS, {"state": "sleeping"})
    write_json(
        root / "status" / GUIDANCE_HEARTBEAT,
        {"heartbeat_at": _iso(now - timedelta(seconds=20)), "in_flight": None},
    )
    assert guidance_health(root, now=now)["healthy"] is True
    assert guidance_health(root, now=now + timedelta(minutes=10))["reason"] == "heartbeat_stale"
    write_json(
        root / "status" / GUIDANCE_HEARTBEAT,
        {
            "heartbeat_at": _iso(now),
            "in_flight": {"phase": "refresh", "started_at": _iso(now - timedelta(hours=2))},
        },
    )
    assert guidance_health(root, now=now)["reason"] == "phase_exceeded_bound"
    write_json(
        root / "status" / GUIDANCE_HEARTBEAT,
        {
            "heartbeat_at": _iso(now),
            "state": "sleeping",
            "in_flight": None,
            "next_poll_at": _iso(now - timedelta(minutes=30)),
        },
    )
    assert guidance_health(root, now=now)["reason"] == "poll_overdue"
    write_json(
        root / "status" / GUIDANCE_HEARTBEAT,
        {
            "heartbeat_at": _iso(now),
            "state": "busy",
            "in_flight": None,
            "poll_started_at": _iso(now - timedelta(hours=5)),
        },
    )
    assert guidance_health(root, now=now)["reason"] == "poll_exceeded_bound"
    write_json(root / "status" / GUIDANCE_STATUS, {"state": "stopped"})
    assert guidance_health(root, now=now)["reason"] == "worker_stopped"


def test_worker_rejects_a_runtime_root_inside_the_repository(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    inside = WorkerSettings(root=worker_module._ROOT / "runtime", config=harness.settings.config)
    with pytest.raises(ValueError):
        GuidanceWorker(inside, harness.deps)


def test_status_contains_readiness_next_slot_and_guidance(tmp_path: Path) -> None:
    harness = Harness(tmp_path, schedule=ForecastSchedule())
    harness.worker().poll_once()
    saved = read_json(harness.root / "status" / GUIDANCE_STATUS)
    assert saved is not None
    assert saved["guidance"]["covers_current_hour"] is True
    assert saved["next_slot"]["next_run"] == "2026-07-01T13:00:00Z"
    assert saved["readiness"]["ready"] is True
    assert saved["disk"]["free_bytes"] == harness.free


def test_stop_request_ends_the_poll_at_the_next_phase_boundary(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()
    original = harness.deps.refresh

    def refresh_then_stop(config: Path, root: Path, probe: Path | None) -> dict:
        result = original(config, root, probe)
        worker.stop.set()  # SIGTERM arrives during the refresh phase
        return result

    harness.deps.refresh = refresh_then_stop
    outcome = worker.poll_once()
    assert outcome["refresh"]["status"] == "published"
    assert outcome["stopped_early"] == "stop_requested"
    assert "build" not in outcome and harness.build_calls == []


def test_killed_build_is_retried_a_bounded_number_of_times(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))

    def killed(*args: Any, **kwargs: Any) -> dict:
        harness.build_calls.append(kwargs)
        raise SystemExit(137)  # stands in for SIGKILL/OOM: no normal bookkeeping runs

    harness.deps.build = killed
    for attempt in range(1, 4):
        restarted = harness.worker()
        restarted.recover_interrupted()
        restarted.state["build"]["next_allowed_at"] = None  # skip the backoff wait
        try:
            outcome = restarted.poll_once()
        except SystemExit:
            outcome = None
        if attempt <= worker_module.MAX_BUILD_INTERRUPTIONS:
            assert outcome is None and len(harness.build_calls) == attempt
        else:
            assert outcome is not None
            assert outcome["build"]["blocked_by"] == "fingerprint_already_attempted"
    assert len(harness.build_calls) == worker_module.MAX_BUILD_INTERRUPTIONS
    records = [
        row
        for row in read_json(harness.root / "status" / GUIDANCE_STATUS)["build"][
            "fingerprints"
        ].values()
    ]
    assert records[-1]["outcome"] == "interrupted"


def test_second_signal_leaves_the_phase_recorded_for_the_next_start(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()
    with pytest.raises(SystemExit):
        with worker.phase("refresh"):
            raise SystemExit(143)
    saved = read_json(harness.root / "status" / GUIDANCE_STATUS)
    assert saved is not None and saved["in_flight"]["phase"] == "refresh"
    assert harness.worker().recover_interrupted() is not None


def test_manual_discovery_evidence_survives_worker_polls(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    kept = worker_module.discover_once(harness.settings, harness.deps, keep=True)
    evidence = Path(kept["evidence"])
    assert evidence.is_dir()
    harness.worker().poll_once()
    assert evidence.is_dir()
    scratch = harness.root / "discovery" / "worker"
    assert not scratch.exists() or not any(scratch.iterdir())


def test_probe_hour_is_recorded_before_probing(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    seen: list[list[str]] = []
    original = harness.discover

    def recording(directory: Path) -> dict:
        saved = read_json(harness.root / "status" / GUIDANCE_STATUS)
        seen.append(saved["refresh"]["probed_hours"] if saved else [])
        return original(directory)

    harness.deps.discover = recording
    harness.worker().poll_once()
    assert seen == [[_iso(harness.hour())]]


def test_only_internal_errors_slow_the_poll_cadence(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    harness.schema_result = ConnectionError("database down")
    worker = harness.worker()
    worker.poll_once()
    assert worker.state["polls"]["consecutive_failures"] == 0
    harness.schema_result = {"current": "0004", "head": "0005", "at_head": False}
    worker.poll_once()
    assert worker.state["polls"]["consecutive_failures"] == 0
    harness.deps.disk_free = lambda path: (_ for _ in ()).throw(RuntimeError("bug"))
    harness.schema_result = {"current": "h", "head": "h", "at_head": True}
    outcome = worker.poll_once()
    assert "internal_error" in outcome["categories"]
    assert worker.state["polls"]["consecutive_failures"] == 1


def test_discover_command_retains_nothing_by_default(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    result = worker_module.discover_once(harness.settings, harness.deps)
    assert result["status"] == "selected"
    assert not any((harness.root / "discovery").iterdir())


def test_new_code_revision_rebuilds_retained_guidance(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"], code_revision="b" * 40)
    outcome = harness.worker().poll_once()
    assert outcome["build"]["reasons"] == ["code_revision_changed"]
    assert outcome["build"]["outcome"] == "published"
    assert len(harness.build_calls) == 1 and not harness.refresh_calls


def test_missing_publication_can_be_rebuilt_after_the_same_inputs_previously_succeeded(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    worker = harness.worker()
    assert worker.poll_once()["build"]["outcome"] == "published"
    (harness.root / "baseline" / "latest_baseline.json").unlink()
    recovered = harness.worker().poll_once()
    assert recovered["build"]["reasons"] == ["no_baseline"]
    assert recovered["build"]["outcome"] == "published"
    assert len(harness.build_calls) == 2


def test_orphaned_build_marker_recovers_after_phase_exit_crash(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    worker = harness.worker()
    inputs = worker.build_inputs(harness.governance.snapshot("", None, START), LOCATIONS)
    assert inputs is not None
    fingerprint = inputs[-1]
    worker.state["build"]["fingerprints"][fingerprint] = {
        "outcome": "in_flight",
        "interruptions": 0,
        "at": _iso(START),
    }
    # phase() finished and saved, but the process died before build result accounting.
    worker.state["in_flight"] = None
    worker.save()
    restarted = harness.worker()
    record = restarted.recover_interrupted()
    assert record is not None and record["reason"] == "unfinished_build_bookkeeping"
    assert restarted.state["build"]["fingerprints"][fingerprint]["interruptions"] == 1
    assert restarted.poll_once()["build"]["blocked_by"] == "backoff"
    harness.clock.advance(minutes=5)
    assert restarted.poll_once()["build"]["outcome"] == "published"


def test_retry_state_is_reloaded_after_obtaining_the_singleton_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.refresh_result = {"status": "failed", "steps": []}
    stale_instance = harness.worker()
    assert harness.worker().run(once=True, install_signals=False) == 1
    assert stale_instance.run(once=True, install_signals=False) == 0
    assert stale_instance.state["polls"]["last"]["refresh"]["blocked_by"] == "backoff"
    assert len(harness.refresh_calls) == 1


@pytest.mark.parametrize("stop", [False, True])
def test_probe_rechecks_refresh_admission_at_its_phase_boundary(tmp_path: Path, stop: bool) -> None:
    harness = Harness(tmp_path)
    pointer = publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    publish_baseline(harness.root, pointer["snapshot_id"])
    harness.discovered["selected_cycles"]["HRRR"] = "2026-07-01T07:00:00+00:00"
    worker = harness.worker()
    original = harness.discover

    def delayed(directory: Path) -> dict:
        result = original(directory)
        if stop:
            worker.stop.set()
        else:
            harness.clock.advance(minutes=36)
        return result

    harness.deps.discover = delayed
    outcome = worker.poll_once()
    expected = "stop_requested" if stop else "too_late_in_hour"
    assert outcome["refresh"]["blocked_by"] == expected
    assert harness.refresh_calls == []


def test_stop_during_schema_check_does_not_start_refresh(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    worker = harness.worker()

    def stop_schema() -> dict:
        worker.stop.set()
        return harness.schema()

    harness.deps.schema = stop_schema
    assert worker.poll_once()["stopped_early"] == "stop_requested"
    assert not harness.refresh_calls and not harness.build_calls


def test_stop_during_governance_check_does_not_start_build(tmp_path: Path) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    worker = harness.worker()

    def stop_governance() -> Any:
        worker.stop.set()
        return harness.governance

    harness.deps.governance = stop_governance
    assert worker.poll_once()["stopped_early"] == "stop_requested"
    assert not harness.build_calls


def test_disk_floor_blocks_build_and_candidate_background_without_spending_attempt(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, refresh="off")
    publish_prepared(harness.root, harness.hour() - timedelta(hours=1))
    harness.governance.shadows = [SimpleNamespace(policy_artifact_id="candidate")]
    harness.free = 1
    worker = harness.worker()
    outcome = worker.poll_once()
    assert outcome["build"]["blocked_by"] == "disk_low"
    assert outcome["background"]["blocked_by"] == "disk_low"
    assert not harness.build_calls and not harness.governance.learning.background_calls
    assert worker.state["build"]["fingerprints"] == {}
    harness.free = 100 * 1024**3
    harness.governance.shadows = []
    assert worker.poll_once()["build"]["outcome"] == "published"


def test_refresh_can_consume_the_build_reserve(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    original = harness.refresh

    def consuming(*args: Any) -> dict:
        result = original(*args)
        harness.free = 1
        return result

    harness.deps.refresh = consuming
    outcome = harness.worker().poll_once()
    assert outcome["refresh"]["status"] == "published"
    assert outcome["build"]["blocked_by"] == "disk_low"
    assert not harness.build_calls


def test_unknown_disk_capacity_does_not_allow_large_writes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def unavailable(path: Path) -> int:
        raise OSError("filesystem unavailable")

    harness.deps.disk_free = unavailable
    assert harness.worker().poll_once()["refresh"]["blocked_by"] == "disk_unavailable"
    assert not harness.refresh_calls


def test_persisted_worker_state_redacts_downstream_readiness_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    test_credential = "sk-regression-not-real-0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", test_credential)
    harness.deps.readiness = lambda *a, **k: {
        "ready": False,
        "reasons": [f"downstream error {test_credential}"],
        "baseline": None,
    }
    worker = harness.worker()
    worker.poll_once()
    assert test_credential in worker.state["readiness"]["reasons"][0]
    assert test_credential not in worker.status_path.read_text("utf-8")


@pytest.mark.parametrize("test_credential", ["2026", "mesoforge"])
def test_short_credential_overlap_preserves_durable_retry_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_credential: str
) -> None:
    monkeypatch.setenv("MESOFORGE_PG_WORKER_PASSWORD", test_credential)
    harness = Harness(tmp_path)
    harness.refresh_result = {
        "status": "failed",
        "error": f"provider said {test_credential}",
        "steps": [],
    }
    assert harness.worker().run(once=True, install_signals=False) == 1
    saved = read_json(harness.root / "status" / GUIDANCE_STATUS)
    assert saved is not None
    assert saved["schema_version"] == worker_module.STATUS_SCHEMA
    assert worker_module.parse_instant(saved["refresh"]["next_allowed_at"]) is not None
    assert saved["refresh"]["attempts"] == {_iso(harness.hour()): 1}
    assert test_credential not in saved["refresh"]["last_attempt"]["error"]
    restarted = harness.worker()
    assert restarted.run(once=True, install_signals=False) == 0
    assert restarted.state["polls"]["last"]["refresh"]["blocked_by"] == "backoff"
    assert len(harness.refresh_calls) == 1
