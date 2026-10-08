"""Offline daily orchestration proofs; no Docker, providers, paid AI or SMTP."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "mesoforge_daily_cycle", ROOT / "deploy/hosted/daily_cycle.py"
)
assert SPEC is not None and SPEC.loader is not None
daily = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = daily
SPEC.loader.exec_module(daily)


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= 30
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


def configuration(tmp_path: Path) -> dict[str, Any]:
    value = {
        "project": "mesoforge-dedicated-offline-test",
        "env_file": str(tmp_path / "deployment.env"),
        "compose_files": [str(tmp_path / "compose.yaml")],
        "state_root": str(tmp_path / "daily-state"),
        "runtime_root": "/var/lib/mesoforge/runtime/minneapolis-v1",
        "recipients": ["customer@example.test"],
        "approved_template": "mesoforge-120-hour-presentation.v1",
    }
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return daily.load_config(path)


class FakeCycle(daily.DailyCycle):
    def __init__(self, config: dict[str, Any], clock: Clock, *, free: int = 40 * 1024**3) -> None:
        super().__init__(config, clock=clock, sleep=clock.sleep, disk_free=lambda _: free)
        self.test_clock = clock
        self.calls: list[tuple[str, str, str, list[str]]] = []
        self.hook: Any = None
        self.guidance_started: datetime | None = None
        self.heavy_checks = 0

    def no_heavy_worker(self) -> None:
        self.heavy_checks += 1

    def baseline(self) -> dict[str, Any]:
        morning = self.slot.replace(hour=self.slot.hour - 1, minute=0)
        return {
            "baseline_snapshot_id": "baseline-for-this-local-day",
            "contributor_state_id": "prepared-for-this-local-day",
            "forecast_horizon_hours": 120,
            "prepared_reference_time": daily.iso(morning),
            "published_at": daily.iso(self.test_clock()),
            "reference_times": {
                "first": daily.iso(morning),
                "last": daily.iso(morning + timedelta(hours=6)),
            },
        }

    def command(self, phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        self.no_heavy_worker()
        self.calls.append((phase, role, module, args))
        if self.hook is not None:
            override = self.hook(phase)
            if override is not None:
                return override
        if phase == "guidance":
            self.guidance_started = self.test_clock()
            self.test_clock.value += timedelta(minutes=50)
            return {"log": "private-guidance.log"}
        if phase == "guidance-status":
            assert self.guidance_started is not None
            return {
                "polls": {
                    "last": {"started_at": daily.iso(self.guidance_started), "categories": []}
                },
                "readiness": {"ready": True, "baseline": self.baseline()},
            }
        if phase == "readiness":
            return {"ready": True, "baseline": self.baseline()}
        if phase == "forecast":
            assert self.test_clock() >= self.slot
            self.test_clock.value += timedelta(minutes=10)
            return {
                "status": "completed",
                "baseline": "baseline-for-this-local-day",
                "results": [
                    {
                        "location": {"id": "minneapolis"},
                        "status": "issued",
                        "issued_forecast_id": "existing-authoritative-issuance",
                        "ai_desk": {"completion_reason": "provider_failure", "accepted_edits": 0},
                    }
                ],
            }
        if phase == "pdf":
            return {"pages": 2, "bytes": 12000, "digest": "sha256:" + "a" * 64}
        if phase.startswith("email-"):
            assert args[args.index("--not-before") + 1] == daily.iso(self.delivery)
            self.test_clock.value = max(self.test_clock(), self.delivery)
            return {"status": "accepted", "delivery_id": "existing-authoritative-delivery"}
        raise AssertionError(f"Unexpected boundary: {phase}")


@pytest.fixture(autouse=True)
def isolated_disk_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESOFORGE_GUIDANCE_MIN_FREE_GB", raising=False)
    monkeypatch.delenv("MESOFORGE_GUIDANCE_WARN_FREE_GB", raising=False)


@pytest.mark.parametrize(
    ("started", "slot", "delivery"),
    [
        ("2026-01-15T12:05:00+00:00", "2026-01-15T13:15:00Z", "2026-01-15T14:00:00Z"),
        ("2026-07-15T11:05:00+00:00", "2026-07-15T12:15:00Z", "2026-07-15T13:00:00Z"),
        ("2026-03-08T11:05:00+00:00", "2026-03-08T12:15:00Z", "2026-03-08T13:00:00Z"),
        ("2026-11-01T12:05:00+00:00", "2026-11-01T13:15:00Z", "2026-11-01T14:00:00Z"),
    ],
)
def test_one_serial_cycle_per_chicago_day_across_cst_cdt(
    tmp_path: Path, started: str, slot: str, delivery: str
) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime.fromisoformat(started))
    cycle = FakeCycle(config, clock)
    result = cycle.execute()
    assert daily.iso(cycle.slot) == slot
    assert daily.iso(cycle.delivery) == delivery
    assert result["status"] == "completed"
    assert [call[0] for call in cycle.calls] == [
        "guidance",
        "guidance-status",
        "readiness",
        "forecast",
        "pdf",
        "email-0",
    ]
    assert cycle.heavy_checks == len(cycle.calls)
    forecast_args = next(call[3] for call in cycle.calls if call[0] == "forecast")
    assert (
        forecast_args[forecast_args.index("--expected-baseline-id") + 1]
        == "baseline-for-this-local-day"
    )
    assert "--scheduled" in forecast_args and "--reference-time" not in forecast_args
    guidance_args = cycle.calls[0][3]
    assert guidance_args[0] == "once" and "--no-hourly-probe" not in guidance_args
    assert "--forecast-horizon-hours" in guidance_args and "120" in guidance_args
    assert (
        result["phases"]["forecast"]["result"]["results"][0]["ai_desk"]["completion_reason"]
        == "provider_failure"
    )
    # A legitimate AI fallback is still an issuance, and delivery proceeds.
    assert result["phases"]["email-0"]["result"]["status"] == "accepted"
    original = cycle.receipt.read_bytes()
    repeated = FakeCycle(config, clock)
    assert repeated.execute()["repeat"] == "completed_day_reused"
    assert repeated.calls == []
    assert repeated.receipt.read_bytes() == original
    # The next local day gets another cycle; no fixed date is supplied by the operator.
    clock.value = datetime.fromisoformat(started) + timedelta(days=1)
    tomorrow = FakeCycle(config, clock)
    assert tomorrow.execute()["status"] == "completed"
    assert tomorrow.directory != cycle.directory
    assert sum(call[0] == "forecast" for call in tomorrow.calls) == 1


def test_same_minneapolis_only_selection_reaches_every_role(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    cycle.execute()
    selection = json.loads((cycle.directory / "locations.json").read_text("utf-8"))
    assert selection == {
        "locations": [
            {
                "id": "minneapolis",
                "name": "Minneapolis",
                "lat": 44.98861,
                "lon": -93.25553,
                "display_timezone": "America/Chicago",
            }
        ]
    }
    override = json.loads(cycle.override.read_text("utf-8"))
    for role in ("guidance-worker", "forecast-worker", "delivery"):
        row = override["services"][role]
        assert row["restart"] == "no"
        assert row["environment"]["MESOFORGE_FORECAST_HORIZON_HOURS"] == "120"
        assert row["environment"]["MESOFORGE_FORECAST_TIMES"] == "07:15"
        assert row["volumes"] == [
            f"{cycle.directory / 'locations.json'}:/run/mesoforge/locations.json:ro"
        ]
    for _, _, _, args in cycle.calls:
        if "--config" in args:
            assert args[args.index("--config") + 1] == "/run/mesoforge/locations.json"


@pytest.mark.parametrize(
    "problem",
    [
        "not_ready",
        "wrong_horizon",
        "old_reference",
        "future_publication",
        "categories",
        "stale_poll",
    ],
)
def test_guidance_failure_or_unproven_freshness_never_starts_forecast(
    tmp_path: Path, problem: str
) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def hook(phase: str) -> dict[str, Any] | None:
        if phase != "guidance-status":
            return None
        baseline = cycle.baseline()
        ready = problem != "not_ready"
        if problem == "wrong_horizon":
            baseline["forecast_horizon_hours"] = 36
        if problem == "old_reference":
            baseline["prepared_reference_time"] = "2026-07-14T11:00:00Z"
        if problem == "future_publication":
            baseline["published_at"] = "2026-07-16T11:00:00Z"
        assert cycle.guidance_started is not None
        started = cycle.guidance_started - timedelta(minutes=1 if problem == "stale_poll" else 0)
        return {
            "polls": {
                "last": {
                    "started_at": daily.iso(started),
                    "categories": ["provider_incomplete"] if problem == "categories" else [],
                }
            },
            "readiness": {"ready": ready, "baseline": baseline},
        }

    cycle.hook = hook
    with pytest.raises(daily.DailyError):
        cycle.execute()
    assert [call[0] for call in cycle.calls] == ["guidance", "guidance-status"]
    assert json.loads(cycle.receipt.read_bytes())["status"] == "failed"
    assert "forecast" not in cycle.record["phases"]


def test_changed_baseline_after_wait_is_not_substituted(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    cycle.hook = lambda phase: (
        {"ready": True, "baseline": {**cycle.baseline(), "baseline_snapshot_id": "new-arrival"}}
        if phase == "readiness"
        else None
    )
    with pytest.raises(daily.DailyError, match="baseline_changed_after_daily_pin"):
        cycle.execute()
    assert "forecast" not in [call[0] for call in cycle.calls]


def test_guidance_error_preserves_previous_receipt_and_blocks_new_forecast(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    yesterday = Path(config["state_root"]) / "2026-07-14" / "result.json"
    yesterday.parent.mkdir(parents=True)
    yesterday.write_text('{"status":"completed","baseline":"previous-good"}', encoding="utf-8")
    original = yesterday.read_bytes()
    cycle = FakeCycle(config, Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def fail(phase: str) -> None:
        if phase == "guidance":
            raise daily.DailyError("guidance_exit_1")

    cycle.hook = fail
    with pytest.raises(daily.DailyError, match="guidance_exit_1"):
        cycle.execute()
    assert [call[0] for call in cycle.calls] == ["guidance"]
    assert yesterday.read_bytes() == original


def test_missing_future_reference_view_is_rejected_before_forecast(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def hook(phase: str) -> dict[str, Any] | None:
        if phase != "guidance-status":
            return None
        assert cycle.guidance_started is not None
        return {
            "polls": {"last": {"started_at": daily.iso(cycle.guidance_started), "categories": []}},
            "readiness": {
                "ready": True,
                "baseline": {
                    **cycle.baseline(),
                    "reference_times": {
                        "first": "2026-07-15T11:00:00Z",
                        "last": "2026-07-15T11:00:00Z",
                    },
                },
            },
        }

    cycle.hook = hook
    with pytest.raises(daily.DailyError, match="morning_forecast_view_not_covered"):
        cycle.execute()
    assert "forecast" not in [call[0] for call in cycle.calls]


def test_low_disk_refuses_without_provider_cleanup_or_forecast(tmp_path: Path) -> None:
    cycle = FakeCycle(
        configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)), free=19 * 1024**3
    )
    with pytest.raises(daily.DailyError, match="disk_refused"):
        cycle.execute()
    assert cycle.calls == []
    assert cycle.record["disk_after"]["heavy_work_admitted"] is False


def test_late_start_does_not_backdate_or_run_any_worker(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 13, tzinfo=UTC)))
    with pytest.raises(daily.DailyError, match="forecast_window_closed"):
        cycle.execute()
    assert cycle.calls == []


def test_delivery_retry_reuses_saved_forecast_and_duplicate_suppression(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    cycle = FakeCycle(config, clock)

    def fail_email(phase: str) -> None:
        if phase == "email-0":
            raise daily.DailyError("email-0_exit_1; immutable_issuance_remains_valid")

    cycle.hook = fail_email
    with pytest.raises(daily.DailyError, match="email-0_exit_1"):
        cycle.execute()
    assert cycle.record["phases"]["forecast"]["status"] == "completed"
    assert cycle.record["phases"]["pdf"]["status"] == "completed"
    clock.value = datetime(2026, 7, 15, 17, tzinfo=UTC)
    retry = FakeCycle(config, clock)
    retry.hook = lambda phase: (
        {"status": "duplicate_suppressed", "previous_outcome": {"status": "accepted"}}
        if phase == "email-0"
        else None
    )
    assert retry.execute()["status"] == "completed"
    assert [call[0] for call in retry.calls] == ["email-0"]


def test_interrupted_forecast_requires_inspection_not_another_ai_job(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    cycle = FakeCycle(config, clock)

    def interrupt(phase: str) -> None:
        if phase == "forecast":
            raise KeyboardInterrupt

    cycle.hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        cycle.execute()
    assert json.loads(cycle.receipt.read_bytes())["phases"]["forecast"]["status"] == "started"
    retry = FakeCycle(config, clock)
    with pytest.raises(daily.DailyError, match="forecast_outcome_requires_operator_inspection"):
        retry.execute()
    assert "forecast" not in [call[0] for call in retry.calls]
    assert "email-0" not in [call[0] for call in retry.calls]


def test_status_is_read_only_even_without_a_state_directory(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    before = sorted(tmp_path.rglob("*"))
    assert cycle.status()["receipt"] is None
    assert sorted(tmp_path.rglob("*")) == before
    assert not cycle.state_root.exists()
    assert cycle.calls == []


def test_running_heavy_role_is_rejected_without_stopping_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def docker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(args, 0, stdout="running-worker\n", stderr="")

    monkeypatch.setattr(daily.subprocess, "run", docker)
    cycle = daily.DailyCycle(
        configuration(tmp_path), clock=lambda: datetime(2026, 7, 15, 11, 5, tzinfo=UTC)
    )
    with pytest.raises(daily.DailyError, match="do_not_stop_another_workload"):
        cycle.no_heavy_worker()
    assert len(calls) == 1 and calls[0][:3] == [daily.DOCKER, "ps", "-q"]
    assert "stop" not in calls[0]


def test_last_json_uses_last_document_when_pretty_json_openers_repeat() -> None:
    assert daily.last_json('{\n  "old": true\n}\n{\n  "status": "completed"\n}\n') == {
        "status": "completed"
    }


@pytest.mark.parametrize("status", ["failed", "ambiguous"])
def test_email_failure_result_cannot_mark_the_daily_cycle_successful(
    tmp_path: Path, status: str
) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    cycle.hook = lambda phase: (
        {"status": "duplicate_suppressed", "previous_outcome": {"status": status}}
        if phase == "email-0"
        else None
    )
    with pytest.raises(daily.DailyError):
        cycle.execute()
    assert cycle.record["phases"]["forecast"]["status"] == "completed"


def test_forecast_error_row_cannot_be_promoted_to_delivery_by_an_id(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    cycle.hook = lambda phase: (
        {
            "status": "completed",
            "baseline": "baseline-for-this-local-day",
            "results": [
                {
                    "location": {"id": "minneapolis"},
                    "status": "failed",
                    "issued_forecast_id": "not-proof-of-success",
                }
            ],
        }
        if phase == "forecast"
        else None
    )
    with pytest.raises(daily.DailyError):
        cycle.execute()
    assert "pdf" not in [call[0] for call in cycle.calls]


def test_naive_runtime_clock_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(daily.DailyError, match="aware_runtime_clock_required"):
        FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 6, 5)))


def test_manual_clock_wait_is_finite_when_wall_clock_stalls(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, 14, tzinfo=UTC))
    cycle = FakeCycle(configuration(tmp_path), clock)
    waits: list[float] = []
    cycle.sleep = waits.append  # A stuck clock never advances.
    with pytest.raises(daily.DailyError, match="forecast_slot_wait_clock_failed"):
        cycle.wait_for_slot()
    assert len(waits) == 5
    assert cycle.calls == []


def test_corrupt_day_receipt_never_becomes_a_new_paid_attempt(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    cycle.directory.mkdir(parents=True)
    cycle.receipt.write_text("incomplete", encoding="utf-8")
    with pytest.raises(daily.DailyError, match="daily_receipt_unreadable"):
        cycle.execute()
    assert cycle.calls == []


def test_manual_run_before_morning_refuses_before_guidance(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 10, tzinfo=UTC)))
    with pytest.raises(daily.DailyError, match="guidance_not_due"):
        cycle.execute()
    assert cycle.calls == []


def test_candidate_warning_does_not_block_current_baseline(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def warning(phase: str) -> dict[str, Any] | None:
        if phase == "guidance-status":
            return {
                "polls": {
                    "last": {
                        "started_at": daily.iso(cycle.guidance_started),
                        "categories": ["candidate_background_failed"],
                    }
                },
                "readiness": {"ready": True, "baseline": cycle.baseline()},
            }
        return None

    cycle.hook = warning
    assert cycle.execute()["status"] == "completed"


@pytest.mark.parametrize("bad_path", ["runner", "runner_install", "root", "runtime_escape"])
def test_configuration_refuses_runner_state_and_runtime_path_escape(
    tmp_path: Path, bad_path: str
) -> None:
    config = configuration(tmp_path)
    if bad_path == "runner":
        config["state_root"] = str(tmp_path / "_work" / "repo" / "daily-state")
    elif bad_path == "runner_install":
        config["state_root"] = str(tmp_path / "actions-runner-mesoforge" / "daily-state")
    elif bad_path == "root":
        config["state_root"] = str(Path(tmp_path.anchor))
    else:
        config["runtime_root"] = "/var/lib/mesoforge/runtime/../../runner"
    path = tmp_path / "bad-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(daily.DailyError):
        daily.load_config(path)
