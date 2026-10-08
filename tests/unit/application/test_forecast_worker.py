"""Forecast/issuance worker orchestration with injected clocks and fake services."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mesoforge.application import forecast_worker as module
from mesoforge.application.forecast_worker import (
    EXIT_FAILED,
    EXIT_NOT_READY,
    EXIT_OK,
    EXIT_PARTIAL,
    ForecastDeps,
    ForecastSettings,
    location_outcome,
    run_forecast,
)
from mesoforge.application.worker_lock import single_writer, worker_lock_root
from mesoforge.application.worker_status import FORECAST_STATUS, read_json
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy

LOCATIONS = [
    {"id": "minneapolis", "name": "Minneapolis", "lat": 44.98861, "lon": -93.25553},
    {"id": "surley", "name": "Surley", "lat": 44.97304, "lon": -93.20901},
    {"id": "grasston", "name": "Grasston", "lat": 45.80268, "lon": -93.07952},
]
SLOT = datetime(2026, 1, 15, 14, tzinfo=UTC)  # 08:00 CST
POINTER = {"baseline_snapshot_id": "b1", "published_at": "2026-01-15T13:20:00Z"}


def ready(**overrides: Any) -> dict[str, Any]:
    return {
        "ready": True,
        "reasons": [],
        "reference_time": "2026-01-15T14:00:00Z",
        "uncovered_locations": [],
        "baseline": {
            "baseline_snapshot_id": "b1",
            "contributor_state_id": "p1",
            "published_at": POINTER["published_at"],
            "age_seconds": 2400.0,
            "contributor_cycles": {"HRRR": "2026-01-15T06:00:00+00:00"},
        },
        **overrides,
    }


def desk(reason: str, *, calls: int = 1, edits: int = 0) -> dict[str, Any]:
    return {
        "ai": {
            "completion_reason": reason,
            "usage": {"provider_calls": calls},
            "accepted_recipes": [{}] * edits,
        }
    }


class Harness:
    def __init__(
        self, tmp_path: Path, *, now: datetime = SLOT + timedelta(seconds=5), **settings: Any
    ) -> None:
        self.now = now
        self.root = tmp_path / "runtime"
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.config = tmp_path / "locations.json"
        self.config.write_text(json.dumps({"locations": LOCATIONS}), "utf-8")
        self.stream = io.StringIO()
        self.calls: list[str] = []
        self.readiness_results: list[dict[str, Any]] = [ready()]
        self.readiness_calls: list[dict[str, Any]] = []
        self.forecast_calls: list[dict[str, Any]] = []
        self.sleeps: list[float] = []
        self.schema_result: dict[str, Any] | Exception = {
            "current": "h",
            "head": "h",
            "at_head": True,
        }
        self.lock_error: Exception | None = None
        self.preflight_error: Exception | None = None
        self.governance_error: Exception | None = None
        self.forecast_result: dict[str, Any] = {
            "status": "ok",
            "request_time": "2026-01-15T14:00:05Z",
            "reference_time": "2026-01-15T14:00:00Z",
            "baseline": {"baseline_snapshot_id": "b1", "prepared_snapshot_id": "p1"},
            "governance": {"status": "resolved"},
            "summary": {"ok": 3, "issued": 3, "skipped": 0, "failed": 0},
            "results": [
                {
                    "index": i,
                    "location": row,
                    "status": "ok",
                    "issued": {"issued_forecast_id": f"id-{i}"},
                    "learning": desk("provider_rate_limited", calls=1),
                    "previous_verification": {
                        "temperature": {"status": "verified"},
                        "qpf": {"status": "nothing_to_verify"},
                    },
                }
                for i, row in enumerate(LOCATIONS)
            ],
        }
        self.learning = SimpleNamespace(
            overlays_for=self._overlays,
            background=lambda *a, **k: pytest.fail("issuance must never build overlays"),
        )
        self.overlay_calls: list[Any] = []
        self.candidates: list[Any] = []
        self.free = 100 * 1024**3
        self.settings = ForecastSettings(root=self.root, config=self.config, **settings)

    def _overlays(self, pinned: Any, candidates: list[Any], *, analysis_cutoff: datetime) -> dict:
        self.overlay_calls.append((pinned, candidates, analysis_cutoff))
        return {"overlays": [{"artifact_id": "art_o"}], "missing": [], "failures": []}

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def schema(self) -> dict[str, Any]:
        self.calls.append("schema")
        if isinstance(self.schema_result, Exception):
            raise self.schema_result
        return self.schema_result

    @contextmanager
    def lock(self) -> Any:
        self.calls.append("lock")
        if self.lock_error is not None:
            raise self.lock_error
        yield
        self.calls.append("unlock")

    def preflight(self) -> None:
        self.calls.append("preflight")
        if self.preflight_error is not None:
            raise self.preflight_error

    def governance(self, learning: Any) -> Any:
        if self.governance_error is not None:
            raise self.governance_error
        return SimpleNamespace(
            status=lambda: {"scopes": []}, blend_candidates=lambda at: self.candidates
        )

    def readiness(self, root: Path, locations: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.readiness_calls.append(kwargs)
        result = self.readiness_results[
            min(len(self.readiness_calls), len(self.readiness_results)) - 1
        ]
        return result

    def forecast(self, root: Path, locations: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.forecast_calls.append(kwargs)
        return self.forecast_result

    def deps(self) -> ForecastDeps:
        return ForecastDeps(
            clock=self.clock,
            sleep=self.sleep,
            schema=self.schema,
            run_lock=self.lock,
            storage_preflight=self.preflight,
            issuer=lambda: "issuer",
            learning=lambda: self.learning,
            governance=self.governance,
            forecast=self.forecast,
            read_revision=lambda: "c" * 40,
            monotonic=lambda: self.now.timestamp(),
            readiness=self.readiness,
            pointer=lambda root: dict(POINTER),
            disk_free=lambda path: self.free,
        )

    def run(self) -> tuple[int, dict[str, Any]]:
        return run_forecast(self.settings, self.deps(), stream=self.stream)

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


def test_scheduled_run_outside_a_slot_is_a_quiet_not_due(tmp_path: Path) -> None:
    harness = Harness(tmp_path, now=SLOT + timedelta(hours=2), scheduled=True)
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_OK, "not_due")
    assert record["schedule"]["next_run"] == "2026-01-16T02:00:00Z"
    assert harness.calls == [] and not (harness.root / "runs").exists()


def test_run_pins_the_ready_pointer_and_issues_every_location(tmp_path: Path) -> None:
    harness = Harness(tmp_path, scheduled=True)
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_OK, "completed")
    assert harness.calls == ["schema", "lock", "preflight", "unlock"]
    call = harness.forecast_calls[0]
    assert call["baseline_pointer"] == POINTER
    assert call["issue"] is True and call["verify_prior"] is True
    assert call["request_time"] == harness.readiness_calls[0]["now"]  # one clock sample
    assert harness.readiness_calls[0]["pointer"] == POINTER
    assert harness.readiness_calls[0]["expected_code_revision"] == "c" * 40
    assert [row["issued_forecast_id"] for row in record["results"]] == ["id-0", "id-1", "id-2"]
    assert record["results"][0]["ai_desk"] == {
        "completion_reason": "provider_rate_limited",
        "provider_failure_code": None,
        "provider_calls": 1,
        "tool_calls": None,
        "input_tokens": None,
        "output_tokens": None,
        "desk_seconds": None,
        "accepted_edits": 0,
        "issued_stage": "deterministic_corrected",
        "discarded_edits": 0,
    }
    events = [row["event"] for row in harness.events()]
    assert events.count("location_result") == 3 and events[-1] == "run_finished"
    saved = json.loads(Path(record["record"]).read_text("utf-8"))
    assert saved["status"] == "completed" and saved["baseline"] == "b1"
    latest = read_json(harness.root / "status" / FORECAST_STATUS)
    assert latest is not None and latest["results"][2]["issued_forecast_id"] == "id-2"


def test_guidance_and_forecast_share_the_existing_runtime_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with single_writer(harness.root) as owned:
        assert owned
        code, record = harness.run()
    assert (code, record["status"]) == (EXIT_NOT_READY, "guidance_busy")
    assert not harness.forecast_calls and not harness.overlay_calls


def test_forecast_respects_deployment_lock_from_a_different_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESOFORGE_WORKER_LOCK_ROOT", str(tmp_path / "host-runtime"))
    harness = Harness(tmp_path / "forecast")
    with single_writer(worker_lock_root(tmp_path / "other-guidance")) as owned:
        assert owned
        code, record = harness.run()
    assert (code, record["category"]) == (EXIT_NOT_READY, "guidance_busy")
    assert not harness.forecast_calls


def test_forecast_holds_process_lock_through_issuance_then_releases(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    deps = harness.deps()
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from mesoforge.application.worker_lock import single_writer\n"
        "with single_writer(Path(sys.argv[1])) as owned:\n"
        "    print('owned' if owned else 'busy')\n"
    )

    def forecast(*args: Any, **kwargs: Any) -> dict:
        child = subprocess.run(
            [sys.executable, "-c", script, str(harness.root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert child.stdout.strip() == "busy"
        assert harness.calls[-1] != "unlock"  # DB run lock is held too.
        return harness.forecast(*args, **kwargs)

    deps.forecast = forecast
    code, record = run_forecast(harness.settings, deps, stream=harness.stream)
    assert (code, record["status"]) == (EXIT_OK, "completed")
    assert record["runtime_lock"] == "held_through_issuance"
    with single_writer(harness.root) as owned:
        assert owned


def test_readiness_wait_does_not_prevent_guidance_from_publishing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, scheduled=True)
    harness.readiness_results = [ready(ready=False, reasons=["pending"]), ready()]
    deps = harness.deps()

    def readiness(*args: Any, **kwargs: Any) -> dict:
        with single_writer(harness.root) as owned:
            assert owned
        return harness.readiness(*args, **kwargs)

    deps.readiness = readiness
    assert run_forecast(harness.settings, deps, stream=harness.stream)[0] == EXIT_OK
    assert harness.sleeps == [60]


@pytest.mark.parametrize("failure", [RuntimeError, SystemExit])
def test_forecast_exception_always_releases_runtime_lock(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    harness = Harness(tmp_path)
    deps = harness.deps()

    def forecast(*args: Any, **kwargs: Any) -> dict:
        with single_writer(harness.root) as owned:
            assert not owned
        raise failure("interrupted forecast")

    deps.forecast = forecast
    if failure is SystemExit:
        with pytest.raises(SystemExit):
            run_forecast(harness.settings, deps, stream=harness.stream)
    else:
        code, record = run_forecast(harness.settings, deps, stream=harness.stream)
        assert (code, record["category"]) == (EXIT_FAILED, "forecast_failed")
    with single_writer(harness.root) as owned:
        assert owned


@pytest.mark.parametrize("expected", ["b1", "another-baseline"])
def test_exact_guidance_handoff_rejects_another_ready_baseline(
    tmp_path: Path, expected: str
) -> None:
    harness = Harness(tmp_path, expected_baseline_id=expected)
    code, record = harness.run()
    assert record["expected_baseline_id"] == expected
    if expected == "b1":
        assert code == EXIT_OK
        assert harness.forecast_calls[0]["baseline_pointer"] == POINTER
    else:
        assert (code, record["category"]) == (EXIT_NOT_READY, "unexpected_baseline")
        assert not harness.forecast_calls and not harness.overlay_calls


def test_no_ready_baseline_fails_clearly_without_refreshing(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.readiness_results = [ready(ready=False, reasons=["reference_hour_not_covered"])]
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_NOT_READY, "baseline_not_ready")
    assert record["reason"] == "reference_hour_not_covered"
    assert harness.forecast_calls == [] and harness.sleeps == []


def test_disk_floor_stops_issuance_before_overlay_lookup_and_paid_desk(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.free = harness.settings.min_free_bytes - 1
    harness.candidates = [SimpleNamespace(policy_artifact_id="candidate")]
    code, record = harness.run()
    assert (code, record["category"]) == (EXIT_FAILED, "disk_low")
    assert record["disk"]["free_bytes"] == harness.free
    assert not harness.forecast_calls and not harness.overlay_calls
    harness.free = harness.settings.min_free_bytes
    harness.candidates = []
    assert harness.run()[0] == EXIT_OK


def test_disk_capacity_error_is_explicit_and_prevents_issuance(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    deps = harness.deps()

    def unavailable(path: Path) -> int:
        raise OSError("filesystem unavailable")

    deps.disk_free = unavailable
    code, record = run_forecast(harness.settings, deps, stream=harness.stream)
    assert (code, record["category"]) == (EXIT_FAILED, "disk_unavailable")
    assert not harness.forecast_calls


def test_returned_and_persisted_run_details_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    test_credential = "sk-regression-not-real-0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", test_credential)
    harness.readiness_results = [
        ready(ready=False, reasons=[f"service returned {test_credential}"])
    ]
    _, record = harness.run()
    assert test_credential not in json.dumps(record)
    assert test_credential not in Path(record["record"]).read_text("utf-8")
    assert test_credential not in (harness.root / "status" / FORECAST_STATUS).read_text("utf-8")


def test_readiness_cli_redacts_downstream_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(tmp_path)
    test_credential = "sk-regression-not-real-0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", test_credential)
    monkeypatch.setattr(module, "default_deps", harness.deps)
    monkeypatch.setattr(module, "current_code_revision", lambda root: "c" * 40)
    monkeypatch.setattr(
        module, "baseline_readiness", lambda *a, **k: ready(ready=False, reasons=[test_credential])
    )
    code = module.main(["readiness", "--root", str(harness.root), "--config", str(harness.config)])
    assert code == EXIT_NOT_READY
    assert test_credential not in capsys.readouterr().out


@pytest.mark.parametrize("test_credential", ["2026", "mesoforge"])
def test_short_credential_overlap_cannot_break_successful_run_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_credential: str
) -> None:
    monkeypatch.setenv("MESOFORGE_PG_WORKER_PASSWORD", test_credential)
    harness = Harness(tmp_path)
    harness.forecast_result["results"][0]["reason"] = f"downstream credential {test_credential}"
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_OK, "completed")
    assert record["schema_version"] == module.RUN_SCHEMA
    assert datetime.fromisoformat(record["started_at"]) == harness.now
    assert Path(record["record"]).is_file()
    assert test_credential not in record["results"][0]["reason"]
    saved = json.loads(Path(record["record"]).read_text("utf-8"))
    assert test_credential not in saved["results"][0]["reason"]


def test_scheduled_run_waits_for_readiness_inside_the_slot_window(tmp_path: Path) -> None:
    harness = Harness(tmp_path, scheduled=True)
    harness.readiness_results = [
        ready(ready=False, reasons=["no_published_baseline"]),
        ready(ready=False, reasons=["reference_hour_not_covered"]),
        ready(),
    ]
    code, record = harness.run()
    assert code == EXIT_OK and record["readiness_attempts"] == 3
    assert harness.sleeps == [60, 60]
    # Every sample is still the slot's UTC hour, so the reference never changes.
    assert all(call["now"].hour == SLOT.hour for call in harness.readiness_calls)


def test_scheduled_wait_stops_when_the_window_closes(tmp_path: Path) -> None:
    harness = Harness(tmp_path, now=SLOT + timedelta(minutes=58), scheduled=True)
    harness.readiness_results = [ready(ready=False, reasons=["reference_hour_not_covered"])]
    code, record = harness.run()
    assert code == EXIT_NOT_READY
    assert record["reason"] == "reference_hour_not_covered; slot_window_closed"
    assert harness.sleeps == [60, 60] and harness.now == SLOT + timedelta(hours=1)
    assert harness.forecast_calls == []


def test_overlapping_manual_trigger_exits_quietly(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.lock_error = AdvisoryLockBusy("busy")
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_OK, "already_running")
    assert harness.forecast_calls == [] and not (harness.root / "runs").exists()


@pytest.mark.parametrize(
    ("change", "category"),
    [
        ("schema_behind", "schema_not_at_head"),
        ("database_down", "database_unavailable"),
        ("lock_down", "database_unavailable"),
        ("storage_down", "storage_unavailable"),
        ("governance_down", "governance_unavailable"),
    ],
)
def test_infrastructure_failures_exit_2_with_a_category(
    tmp_path: Path, change: str, category: str
) -> None:
    harness = Harness(tmp_path)
    if change == "schema_behind":
        harness.schema_result = {"current": "0004", "head": "0005", "at_head": False}
    elif change == "database_down":
        harness.schema_result = ConnectionError("connection refused")
    elif change == "lock_down":
        harness.lock_error = ConnectionError("connection reset")
    elif change == "storage_down":
        harness.preflight_error = ConnectionError("S3 endpoint timed out")
    else:
        harness.governance_error = RuntimeError("governance_read_failed")
    code, record = harness.run()
    assert (code, record["category"]) == (EXIT_FAILED, category)
    assert harness.forecast_calls == []
    assert Path(record["record"]).is_file()


def test_one_location_failure_is_partial_and_others_issue(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.forecast_result["summary"] = {"ok": 2, "issued": 2, "skipped": 0, "failed": 1}
    harness.forecast_result["results"][1] = {
        "index": 1,
        "location": LOCATIONS[1],
        "status": "error",
        "error": {"code": "coverage_required", "message": "no domain"},
    }
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_PARTIAL, "partial")
    assert record["results"][0]["issued_forecast_id"] == "id-0"
    assert "coverage_required" in record["results"][1]["reason"]


def test_duplicate_trigger_reuses_existing_issuances(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.forecast_result["summary"] = {"ok": 0, "issued": 0, "skipped": 3, "failed": 0}
    for row in harness.forecast_result["results"]:
        row.pop("issued")
        row.pop("learning")
        row["status"] = "skipped_already_issued"
        row["skipped"] = {"existing_issued_forecast_ids": [f"old-{row['index']}"]}
    code, record = harness.run()
    assert (code, record["status"]) == (EXIT_OK, "completed")
    assert record["results"][0]["existing_issued_forecast_ids"] == ["old-0"]


def test_governance_refusal_from_issuance_exits_2(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.forecast_result = {
        "status": "baseline_governance_revoked",
        "reason": "rolled back",
        "results": [],
        "summary": {"failed": 3},
    }
    code, record = harness.run()
    assert (code, record["category"]) == (EXIT_FAILED, "baseline_governance_revoked")


def test_candidate_overlays_are_looked_up_never_built(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.candidates = [SimpleNamespace(policy_artifact_id="art_candidate")]
    loaded: list[Any] = []
    original = module.load_baseline
    module.load_baseline = lambda root, pointer: loaded.append(pointer) or "pinned"  # type: ignore
    try:
        code, record = harness.run()
    finally:
        module.load_baseline = original  # type: ignore[assignment]
    assert code == EXIT_OK
    assert harness.forecast_calls[0]["learning_overlays"] == [{"artifact_id": "art_o"}]
    assert loaded == [POINTER] and record["candidate_overlays"]["found"] == 1


def test_explicit_reference_and_skip_verification_are_passed_through(tmp_path: Path) -> None:
    reference = datetime(2026, 1, 15, 12, tzinfo=UTC)
    harness = Harness(tmp_path, reference_time=reference, verify_prior=False)
    code, record = harness.run()
    assert code == EXIT_OK and record["reference_time_source"] == "explicit_replay"
    assert harness.forecast_calls[0]["reference_time"] == reference
    assert harness.forecast_calls[0]["verify_prior"] is False
    assert harness.readiness_calls[0]["reference_time"] == reference


def test_settings_reject_ambiguous_or_invalid_references(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ForecastSettings(root=tmp_path, scheduled=True, reference_time=SLOT)
    with pytest.raises(ValueError):
        ForecastSettings(root=tmp_path, reference_time=SLOT + timedelta(minutes=5))
    with pytest.raises(ValueError):
        ForecastSettings(root=tmp_path, reference_time=datetime(2026, 1, 15, 14))


def test_invalid_configuration_and_code_revision_fail_before_any_service(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.config.write_text(json.dumps({"locations": [{"lat": 999, "lon": 0}]}), "utf-8")
    code, record = harness.run()
    assert (code, record["category"]) == (EXIT_FAILED, "invalid_configuration")
    assert harness.calls == []
    other = Harness(tmp_path / "other")
    deps = other.deps()

    def unavailable() -> str:
        raise FileNotFoundError("git")

    deps.read_revision = unavailable
    code, record = run_forecast(other.settings, deps, stream=io.StringIO())
    assert (code, record["category"]) == (EXIT_FAILED, "code_revision_unavailable")


def test_location_outcome_reports_ai_edits_and_redacts_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-000000")
    row = {
        "index": 0,
        "location": LOCATIONS[0],
        "status": "ok",
        "issued": {"issued_forecast_id": "x"},
        "learning": desk("completed", calls=4, edits=2),
        "reason": "provider said sk-test-not-a-real-key-000000 was rejected",
    }
    row["learning"].update(status="applied", applied_delta_k=-0.4)
    row["learning"]["ai"].update(
        usage={"provider_calls": 4, "tool_calls": 2, "input_tokens": 900, "output_tokens": 80},
        timings={"total_seconds": 12.5},
    )
    row["baseline_extraction_seconds"] = 0.123456
    row["learning"]["operational_stage"] = {"transformation_type": "ai_adjusted"}
    outcome = location_outcome(row)
    assert outcome["ai_desk"]["issued_stage"] == "ai_adjusted"
    assert outcome["ai_desk"]["accepted_edits"] == 2
    assert outcome["ai_desk"]["tool_calls"] == 2 and outcome["ai_desk"]["desk_seconds"] == 12.5
    assert outcome["correction"] == {"status": "applied", "applied_delta_k": -0.4}
    assert outcome["baseline_extraction_seconds"] == 0.123
    assert "sk-test" not in outcome["reason"]


def test_scheduled_trigger_waits_for_a_busy_run_lock_inside_its_window(tmp_path: Path) -> None:
    harness = Harness(tmp_path, scheduled=True)
    attempts = {"count": 0}

    @contextmanager
    def busy_twice() -> Any:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise AdvisoryLockBusy("busy")
        yield

    deps = harness.deps()
    deps.run_lock = busy_twice
    code, record = run_forecast(harness.settings, deps, stream=harness.stream)
    assert (code, record["status"]) == (EXIT_OK, "completed")
    assert record["run_lock_waits"] == 2 and harness.sleeps == [60, 60]


def test_scheduled_trigger_reports_a_slot_lost_to_a_busy_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path, now=SLOT + timedelta(minutes=58), scheduled=True)
    harness.lock_error = AdvisoryLockBusy("busy")
    code, record = harness.run()
    assert (code, record["category"]) == (EXIT_FAILED, "run_lock_busy_until_window_closed")
    assert Path(record["record"]).is_file()


def test_window_close_keeps_the_real_readiness_reasons(tmp_path: Path) -> None:
    harness = Harness(tmp_path, now=SLOT + timedelta(minutes=58), scheduled=True)
    harness.readiness_results = [ready(ready=False, reasons=["reference_hour_not_covered"])]
    code, record = harness.run()
    assert code == EXIT_NOT_READY
    assert record["readiness"]["reasons"] == ["reference_hour_not_covered", "slot_window_closed"]
    assert record["readiness"]["baseline"]["baseline_snapshot_id"] == "b1"


def test_missing_pointer_is_not_re_read_by_readiness(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    deps = harness.deps()
    deps.pointer = lambda root: None
    code, record = run_forecast(harness.settings, deps, stream=harness.stream)
    assert (code, record["reason"]) == (EXIT_NOT_READY, "no_published_baseline")
    assert harness.readiness_calls == []


def test_future_replay_hour_is_rejected_before_any_service(tmp_path: Path) -> None:
    harness = Harness(tmp_path, reference_time=SLOT + timedelta(hours=5))
    code, record = harness.run()
    assert (code, record["category"]) == (EXIT_FAILED, "invalid_configuration")
    assert harness.calls == [] and Path(record["record"]).is_file()


def test_unexpected_readiness_error_is_recorded(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    deps = harness.deps()

    def broken(*args: Any, **kwargs: Any) -> dict:
        raise KeyError("published_at")

    deps.readiness = broken
    code, record = run_forecast(harness.settings, deps, stream=harness.stream)
    assert code == EXIT_NOT_READY and record["reason"].startswith("readiness_error")
    assert Path(record["record"]).is_file()


def test_invalid_schedule_environment_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESOFORGE_FORECAST_TIMES", "8:00,20:00")
    assert module.main(["next-run"]) == EXIT_FAILED


def test_issued_stage_reflects_an_ai_stage_without_accepted_edits() -> None:
    row = {
        "index": 0,
        "location": LOCATIONS[0],
        "status": "ok",
        "issued": {"issued_forecast_id": "x"},
        "learning": {
            **desk("no_edit", calls=5, edits=0),
            "operational_stage": {"transformation_type": "ai_adjusted"},
        },
    }
    assert location_outcome(row)["ai_desk"]["issued_stage"] == "ai_adjusted"


def test_desk_configuration_check_is_network_free_and_names_only_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert module.desk_configuration()["status"] == "unconfigured"
    monkeypatch.setenv("MESOFORGE_AI_PROVIDER", "openai")
    monkeypatch.setenv("MESOFORGE_AI_MODEL", "gpt-6-sol")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-000000")
    assert module.desk_configuration() == {
        "status": "configured",
        "provider": "openai",
        "model": "gpt-6-sol",
    }
    monkeypatch.setenv("MESOFORGE_AI_MAX_PROVIDER_CALLS", "")
    result = module.desk_configuration()
    assert result == {
        "status": "configuration_invalid",
        "setting": "MESOFORGE_AI_MAX_PROVIDER_CALLS",
    }
    assert "sk-test" not in json.dumps(result)
