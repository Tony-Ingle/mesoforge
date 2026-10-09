"""Offline daily orchestration proofs; no Docker, providers, paid AI or SMTP."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from contextlib import contextmanager
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
        "backup_root": str(tmp_path / "backups"),
        "runtime_root": "/var/lib/mesoforge/runtime/minneapolis-v1",
        "approved_template": "mesoforge-120-hour-presentation.v1",
    }
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return daily.load_config(path)


class FakeCycle(daily.DailyCycle):
    def __init__(
        self,
        config: dict[str, Any],
        clock: Clock,
        *,
        free: int = 40 * 1024**3,
        location_id: str = "minneapolis",
    ) -> None:
        super().__init__(
            config,
            clock=clock,
            sleep=clock.sleep,
            disk_free=lambda _: free,
            location_id=location_id,
        )
        self.test_clock = clock
        self.calls: list[tuple[str, str, str, list[str]]] = []
        self.hook: Any = None
        self.guidance_started: datetime | None = None
        self.heavy_checks = 0

    def no_heavy_worker(self) -> None:
        self.heavy_checks += 1

    def preflight(self) -> dict[str, Any]:
        return {"status": "ready", "backup_kind": "same_host_recovery_copy"}

    def backup(self) -> dict[str, Any]:
        return self.command("backup", "backup", "offline.backup", [])

    def retention(self, backup: dict[str, Any]) -> dict[str, Any]:
        assert backup["status"] == "verified"
        return self.command("retention", "admin", "offline.retention", [])

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
                        "location": {"id": self.location_id},
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
        if phase == "backup":
            return {"status": "verified", "backup_id": "verified-offline-backup"}
        if phase == "retention":
            return {"status": "completed", "deleted": []}
        raise AssertionError(f"Unexpected boundary: {phase}")


@pytest.fixture(autouse=True)
def isolated_disk_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESOFORGE_GUIDANCE_MIN_FREE_GB", raising=False)
    monkeypatch.delenv("MESOFORGE_GUIDANCE_WARN_FREE_GB", raising=False)
    registry = daily.load_locations(ROOT / "configs/locations.json")
    locations = [
        {**row, "email_recipients": ["customer@example.test"]}
        if row.get("id") == "minneapolis"
        else row
        for row in registry
    ]
    monkeypatch.setattr(daily, "load_locations", lambda _: locations)


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
        "backup",
        "retention",
    ]
    assert cycle.heavy_checks == len(cycle.calls)
    forecast_args = next(call[3] for call in cycle.calls if call[0] == "forecast")
    assert (
        forecast_args[forecast_args.index("--expected-baseline-id") + 1]
        == "baseline-for-this-local-day"
    )
    assert "--scheduled" not in forecast_args and "--reference-time" not in forecast_args
    guidance_args = cycle.calls[0][3]
    assert guidance_args[0] == "once" and "--no-hourly-probe" in guidance_args
    assert guidance_args[guidance_args.index("--guidance-config") + 1] == (
        "/run/mesoforge/guidance-locations.json"
    )
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
                "email_recipients": ["customer@example.test"],
            }
        ]
    }
    override = json.loads(cycle.override.read_text("utf-8"))
    for role in ("guidance-worker", "forecast-worker", "delivery"):
        row = override["services"][role]
        assert row["restart"] == "no"
        assert row["environment"]["MESOFORGE_FORECAST_HORIZON_HOURS"] == "120"
        assert row["environment"]["MESOFORGE_FORECAST_TIMES"] == "07:15"
        assert row["volumes"][:1] == [
            f"{cycle.directory / 'locations.json'}:/run/mesoforge/locations.json:ro"
        ]


def test_locations_have_independent_receipts_and_delivery_but_shared_runtime(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    minneapolis = FakeCycle(config, clock)
    minneapolis.execute()
    clock.value = datetime(2026, 7, 15, 13, 15, tzinfo=UTC)
    grasston = FakeCycle(config, clock, location_id="grasston")
    result = grasston.execute()
    assert result["status"] == "completed"
    assert result["location_id"] == "grasston"
    assert result["workflow_slot"] == "daily"
    assert result["delivery"]["status"] == "no_recipients"
    assert "forecast" in [call[0] for call in grasston.calls]
    assert not any(call[0].startswith("email-") for call in grasston.calls)
    assert grasston.receipt != minneapolis.receipt
    assert grasston.state_root == minneapolis.state_root
    assert grasston.runtime == minneapolis.runtime
    assert grasston.directory.parts[-3:] == ("2026-07-15", "grasston", "daily")
    assert daily.iso(grasston.slot) == "2026-07-15T13:15:00Z"
    assert daily.iso(grasston.delivery) == "2026-07-15T14:30:00Z"
    selected = json.loads((grasston.directory / "locations.json").read_text())
    assert [row["id"] for row in selected["locations"]] == ["grasston"]
    for cycle in (minneapolis, grasston):
        shared = json.loads((cycle.directory / "guidance-locations.json").read_text())
        assert [row["id"] for row in shared["locations"]] == ["minneapolis", "grasston"]
        args = next(row[3] for row in cycle.calls if row[0] == "guidance")
        assert "--guidance-config" in args and "--no-hourly-probe" in args
    repeated = FakeCycle(config, clock, location_id="grasston")
    assert repeated.execute()["repeat"] == "completed_day_reused"
    assert repeated.calls == []


def test_legacy_same_day_receipt_stops_before_repeating_paid_work(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    legacy = cycle.state_root / cycle.day.isoformat() / "result.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"status": "completed"}))
    with pytest.raises(daily.DailyError, match="legacy_daily_receipt_requires_inspection"):
        cycle.execute()
    assert cycle.calls == []
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


def test_late_start_issues_with_actual_current_reference_and_no_fixed_slot(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 13, tzinfo=UTC)))
    assert cycle.execute()["status"] == "completed"
    args = next(call[3] for call in cycle.calls if call[0] == "forecast")
    assert "--scheduled" not in args and "--reference-time" not in args
    assert cycle.record["phases"]["forecast"]["started_at"] == "2026-07-15T14:00:00Z"
    assert cycle.record["analysis_window"]["waited_seconds"] == 600


def test_delivery_retry_reuses_saved_forecast_and_duplicate_suppression(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    cycle = FakeCycle(config, clock)

    def fail_email(phase: str) -> None:
        if phase == "email-0":
            raise daily.DailyError("email-0_exit_1; immutable_issuance_remains_valid")

    cycle.hook = fail_email
    with pytest.raises(daily.DailyError, match="delivery_failed"):
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
    assert [call[0] for call in retry.calls] == ["email-0", "backup", "retention"]
    assert retry.record["maintenance"]["fingerprint"] != cycle.record["maintenance"]["fingerprint"]


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


def set_recipients(monkeypatch: pytest.MonkeyPatch, values: list[str]) -> None:
    locations = daily.load_locations(ROOT / "configs/locations.json")
    monkeypatch.setattr(
        daily,
        "load_locations",
        lambda _: [
            {**row, "email_recipients": values} if row["id"] == "minneapolis" else row
            for row in locations
        ],
    )


def test_empty_recipients_still_issue_and_back_up_without_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_recipients(monkeypatch, [])
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    result = cycle.execute()
    assert result["status"] == "completed"
    assert result["delivery"] == {"status": "no_recipients", "recipient_count": 0, "errors": {}}
    assert "forecast" in [row[0] for row in cycle.calls]
    assert not any(row[0].startswith("email-") for row in cycle.calls)
    assert [row[0] for row in cycle.calls][-2:] == ["backup", "retention"]


def test_recipient_failure_isolated_and_valid_issuance_backed_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_recipients(monkeypatch, ["first@example.test", "second@example.test", "first@example.test"])
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def fail_first(phase: str) -> None:
        if phase == "email-0":
            raise daily.DailyError("smtp_rejected")

    cycle.hook = fail_first
    with pytest.raises(daily.DailyError, match="delivery_failed"):
        cycle.execute()
    assert [row[0] for row in cycle.calls][-4:] == ["email-0", "email-1", "backup", "retention"]
    assert cycle.record["phases"]["email-1"]["result"]["status"] == "accepted"
    assert cycle.record["delivery"]["errors"] == {"first@example.test": "smtp_rejected"}
    assert cycle.record["maintenance"]["backup"]["status"] == "verified"
    assert cycle.record["phases"]["forecast"]["status"] == "completed"


def test_unchanged_delivery_failure_reuses_verified_backup(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    cycle = FakeCycle(config, clock)
    cycle.hook = lambda phase: (
        {"status": "duplicate_suppressed", "previous_outcome": {"status": "ambiguous"}}
        if phase == "email-0"
        else None
    )
    with pytest.raises(daily.DailyError, match="delivery_failed"):
        cycle.execute()
    first_fingerprint = cycle.record["maintenance"]["fingerprint"]
    retry = FakeCycle(config, clock)
    retry.hook = cycle.hook
    with pytest.raises(daily.DailyError, match="delivery_failed"):
        retry.execute()
    assert retry.record["maintenance"]["fingerprint"] == first_fingerprint
    assert [row[0] for row in retry.calls] == ["email-0"]


@pytest.mark.parametrize("failure", ["exception", "unverified"])
def test_backup_failure_preserves_forecast_and_blocks_retention(
    tmp_path: Path, failure: str
) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def fail_backup(phase: str) -> dict[str, Any] | None:
        if phase == "backup":
            if failure == "exception":
                raise daily.DailyError("backup_failed")
            return {"status": "incomplete"}
        return None

    cycle.hook = fail_backup
    with pytest.raises(daily.DailyError, match="backup"):
        cycle.execute()
    assert cycle.record["phases"]["forecast"]["status"] == "completed"
    assert cycle.record["phases"]["email-0"]["result"]["status"] == "accepted"
    assert "retention" not in [row[0] for row in cycle.calls]


def test_missing_backup_storage_fails_before_guidance_or_ai(tmp_path: Path) -> None:
    cycle = FakeCycle(configuration(tmp_path), Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))

    def fail_preflight() -> dict[str, Any]:
        raise daily.DailyError("backup_storage_unavailable")

    cycle.preflight = fail_preflight
    with pytest.raises(daily.DailyError, match="backup_storage_unavailable"):
        cycle.execute()
    assert cycle.calls == []


def test_retry_reports_current_success_and_preserves_previous_failure_trace(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    failed = FakeCycle(config, clock)
    failure = {"phase": "backup-estimate", "exit_code": 1, "result": {"status": "failed"}}

    def failed_preflight() -> dict[str, Any]:
        failed.record["worker_failure"] = failure
        raise daily.DailyError("temporary_preflight_failure")

    failed.preflight = failed_preflight
    with pytest.raises(daily.DailyError, match="temporary_preflight_failure"):
        failed.execute()
    assert failed.record["status"] == "failed"
    retry = FakeCycle(config, clock)

    def recovered_preflight() -> dict[str, Any]:
        # Durable status must describe this attempt even while it is still running.
        saved = json.loads(retry.receipt.read_bytes())
        assert saved["status"] == "started" and saved["phase"] == "preflight"
        assert "reason" not in saved and "worker_failure" not in saved
        return {"status": "ready"}

    retry.preflight = recovered_preflight
    result = retry.execute()
    assert result["status"] == "completed"
    assert "reason" not in result and "worker_failure" not in result
    assert result["previous_failures"] == [
        {
            "retried_at": "2026-07-15T11:05:00Z",
            "phase": "preflight",
            "reason": "temporary_preflight_failure",
            "worker_failure": failure,
        }
    ]


def test_recipient_changes_require_review_of_same_day_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    first = FakeCycle(config, clock)
    first.execute()
    original = first.receipt.read_bytes()
    set_recipients(monkeypatch, ["new-recipient@example.test"])
    retry = FakeCycle(config, clock)
    with pytest.raises(daily.DailyError, match="daily_configuration_changed"):
        retry.execute()
    assert retry.calls == []
    assert retry.receipt.read_bytes() == original


@pytest.mark.parametrize("key", ["state_root", "backup_root"])
def test_private_roots_cannot_target_runner_workspaces(tmp_path: Path, key: str) -> None:
    config = configuration(tmp_path)
    config[key] = str(tmp_path / "actions-runner-mesoforge" / "state")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(config), "utf-8")
    with pytest.raises(daily.DailyError, match="runner_workspaces"):
        daily.load_config(path)


def test_global_recipient_fallback_is_rejected(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    config["recipients"] = ["global@example.test"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(config), "utf-8")
    with pytest.raises(daily.DailyError, match="global_recipients_not_supported"):
        daily.load_config(path)


def test_execution_does_not_roll_an_in_progress_job_into_another_local_day(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    cycle = FakeCycle(configuration(tmp_path), clock)
    clock.value += timedelta(days=1)
    with pytest.raises(daily.DailyError, match="local_day_changed"):
        cycle.execute()
    assert cycle.calls == []


@pytest.mark.parametrize(
    "counts", [None, [], {"unknown": 4}, {"hrrr": 0}, {"gfs": -1}, {"ifs": True}, {"rap": 1.5}]
)
def test_bad_retention_configuration_fails_before_work(tmp_path: Path, counts: object) -> None:
    config = configuration(tmp_path)
    config["retention_cycles"] = counts
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(config), "utf-8")
    with pytest.raises(daily.DailyError, match="retention_cycles_requires"):
        daily.load_config(path)


@pytest.mark.parametrize("legacy_host_python", [False, True])
def test_actual_backup_preflight_uses_local_storage_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_host_python: bool
) -> None:
    if legacy_host_python:
        monkeypatch.delattr(Path, "is_junction")
    config = configuration(tmp_path)
    cycle = daily.DailyCycle(
        config,
        clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)),
        disk_free=lambda _: 40 * 1024**3,
    )
    calls: list[tuple[str, str, str, list[str]]] = []

    def estimate(phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        calls.append((phase, role, module, args))
        return {"status": "estimated", "backup_bytes": 5 * 1024**3}

    monkeypatch.setattr(cycle, "command", estimate)
    assert not Path(config["backup_root"]).exists()
    result = cycle.preflight()
    assert result["recovery"] == "same_host" and result["off_host"] is False
    assert result["estimate"]["backup_bytes"] == 5 * 1024**3
    assert calls == [
        (
            "backup-estimate",
            "guidance-worker",
            "mesoforge.application.local_backup",
            ["estimate", "--runtime-root", "/var/lib/mesoforge/runtime"],
        )
    ]
    assert Path(config["backup_root"]).is_dir()
    cycle.disk_free = lambda _: 19 * 1024**3
    with pytest.raises(daily.DailyError, match="backup_disk_reserve"):
        cycle.preflight()


def test_actual_backup_preflight_refuses_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    root = Path(config["backup_root"])
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root)
    cycle = daily.DailyCycle(
        config,
        clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)),
        disk_free=lambda _: 40 * 1024**3,
    )
    with pytest.raises(daily.DailyError, match="must_not_follow_links"):
        cycle.preflight()
    assert not root.exists()


def test_actual_retention_passes_verified_receipt_and_cycle_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    config["retention_cycles"] = {"hrrr": 5, "ecmwf-ens": 2}
    cycle = daily.DailyCycle(config, clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)))
    cycle.prepare()
    source = Path(config["backup_root"]) / "complete-recovery-set"
    source.mkdir(parents=True)
    receipt = {"status": "verified", "host_directory": str(source), "manifest_sha256": "a" * 64}
    calls: list[tuple[str, str, str, list[str]]] = []

    def command(phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        calls.append((phase, role, module, args))
        return {"status": "complete", "deleted_bytes": 0}

    monkeypatch.setattr(cycle, "command", command)
    assert cycle.retention(receipt)["status"] == "complete"
    assert len(calls) == 2
    phase, role, module, args = calls[0]
    assert (phase, role, module) == (
        "retention-apply",
        "guidance-worker",
        "mesoforge.application.guidance_retention",
    )
    assert args == [
        "--runtime-root",
        cycle.runtime,
        "--apply",
        "--backup-receipt",
        "/run/mesoforge/backup-receipt.json",
        "--keep-ecmwf-ens",
        "2",
        "--keep-hrrr",
        "5",
    ]
    retained = json.loads((cycle.directory / "verified-backup.json").read_bytes())
    assert retained == receipt
    override = json.loads((cycle.directory / "retention.override.json").read_bytes())
    assert override["services"]["guidance-worker"]["volumes"] == [
        f"{cycle.directory / 'verified-backup.json'}:/run/mesoforge/backup-receipt.json:ro"
    ]
    assert cycle.compose[-2:] == ["-f", str(cycle.directory / "retention.override.json")]
    assert calls[1] == (
        "backup-prune",
        "admin",
        "mesoforge.application.local_backup",
        [
            "prune",
            "--root",
            "/recovery",
            "--keep",
            "2",
            "--current-backup",
            f"/recovery/{source.name}",
            "--apply",
        ],
    )


def test_actual_retention_rejects_backup_outside_private_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path), clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    )
    monkeypatch.setattr(cycle, "command", lambda *_: pytest.fail("must not start cleanup"))
    with pytest.raises(daily.DailyError, match="backup_path_outside_configured_root"):
        cycle.retention({"status": "verified", "host_directory": str(tmp_path / "other")})


def test_backup_validator_uses_host_identity_but_runtime_archive_keeps_worker_identity(
    tmp_path: Path,
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path), clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    )
    cycle.prepare()
    override = json.loads(cycle.override.read_bytes())
    admin = override["services"]["admin"]
    import os

    assert admin["user"] == (
        f"{getattr(os, 'getuid', lambda: 1000)()}:{getattr(os, 'getgid', lambda: 1000)()}"
    )
    assert admin["volumes"] == [f"{cycle.config['backup_root']}:/recovery"]
    shell = (ROOT / "deploy/hosted/backup.sh").read_text("utf-8")
    assert "--user 10001:10001 --entrypoint tar admin" in shell


def test_actual_backup_creation_validation_and_resume_without_recopy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    cycle = daily.DailyCycle(
        config,
        clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)),
        disk_free=lambda _: 40 * 1024**3,
    )
    cycle.prepare()
    cycle.record = {"maintenance": {"fingerprint": "a" * 64}, "phases": {}}
    cycle.save()
    commands: list[str] = []
    subprocesses: list[list[str]] = []
    held = False
    validation_fails = True
    source = Path(config["backup_root"]) / "mesoforge-fixed-complete"

    @contextmanager
    def lock():
        nonlocal held
        assert not held
        held = True
        try:
            yield
        finally:
            held = False

    def command(phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        commands.append(phase)
        if phase == "backup-estimate":
            assert not held and role == "guidance-worker"
            return {"status": "estimated", "backup_bytes": 1024}
        if phase == "retention-dry-run":
            assert not held
            assert "--dry-run" in args and "--apply" not in args
            return {"plan_sha256": "b" * 64}
        assert phase == "backup-validation" and role == "admin"
        assert held  # Validation finishes before allowing workers to mutate state.
        assert args == ["validate", "--source", f"/recovery/{source.name}"]
        if validation_fails:
            raise daily.DailyError("validation_temporarily_failed")
        return {"status": "verified", "backup_id": "verified-test-recovery"}

    class CompletedProcess:
        returncode = 0

        def poll(self) -> int:
            return 0

    def create(args: list[str], **kwargs: Any) -> CompletedProcess:
        assert held
        assert args == ["/bin/sh", str(ROOT / "deploy/hosted/backup.sh"), config["backup_root"]]
        subprocesses.append(args)
        env = kwargs["env"]
        assert env["COMPOSE_PROJECT_NAME"] == config["project"]
        assert env["COMPOSE_ENV_FILES"] == config["env_file"]
        assert json.loads(Path(env["MESOFORGE_BACKUP_RETENTION_PLAN"]).read_bytes()) == {
            "plan_sha256": "b" * 64
        }
        assert Path(env["MESOFORGE_BACKUP_DAILY_RECEIPTS"]).is_file()
        source.mkdir()
        kwargs["stdout"].write(f"Backup complete: {source}\n")
        return CompletedProcess()

    monkeypatch.setattr(cycle, "backup_lock", lock)
    monkeypatch.setattr(cycle, "command", command)
    monkeypatch.setattr(cycle, "no_heavy_worker", lambda: None)
    monkeypatch.setattr(daily.subprocess, "Popen", create)
    with pytest.raises(daily.DailyError, match="validation_temporarily_failed"):
        cycle.backup()
    assert not held
    validation_fails = False
    result = cycle.backup()
    assert result["status"] == "verified" and Path(result["host_directory"]) == source
    assert len(subprocesses) == 1  # Retry rereads the completed immutable recovery set.
    assert commands == ["backup-estimate", "retention-dry-run", "backup-validation"] * 2
    assert not held


def test_backup_lock_failure_never_enters_work_and_closes_owned_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path), clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    )
    cycle.prepare()
    calls: list[list[str]] = []

    class Holder:
        stdin = io.StringIO()
        stdout = io.StringIO('{"status":"busy"}\n')
        waits: list[int] = []

        def wait(self, *, timeout: int) -> int:
            self.waits.append(timeout)
            return 3

    holder = Holder()

    def create(args: list[str], **kwargs: Any) -> Holder:
        calls.append(args)
        return holder

    monkeypatch.setattr(daily.subprocess, "Popen", create)
    monkeypatch.setattr(daily.select, "select", lambda streams, *_: (streams, [], []))
    with pytest.raises(daily.DailyError, match="backup_runtime_lock_unavailable"):
        with cycle.backup_lock():
            pytest.fail("Busy lock must prevent backup work")
    assert holder.stdin.closed and holder.stdout.closed
    assert holder.waits == [30]
    assert len(calls) == 1
    assert calls[0][-6:] == [
        "-T",
        "admin",
        "mesoforge.application.local_backup",
        "hold-lock",
        "--runtime-root",
        cycle.runtime,
    ]


def test_backup_lock_timeout_stops_only_its_owned_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path), clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    )
    cycle.prepare()
    launches: list[list[str]] = []
    stops: list[list[str]] = []

    class Holder:
        stdin = io.StringIO()
        stdout = io.StringIO()
        waits = 0

        def wait(self, *, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("owned lock holder", timeout)
            return 0

    holder = Holder()

    def create(args: list[str], **kwargs: Any) -> Holder:
        launches.append(args)
        return holder

    def stop(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stops.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(daily.subprocess, "Popen", create)
    monkeypatch.setattr(daily.subprocess, "run", stop)
    monkeypatch.setattr(daily.select, "select", lambda *_: ([], [], []))
    with pytest.raises(daily.DailyError, match="backup_runtime_lock_unavailable"):
        with cycle.backup_lock():
            pytest.fail("Timed out lock must prevent backup work")
    name = launches[0][launches[0].index("--name") + 1]
    assert stops == [[daily.DOCKER, "stop", "--time", "10", name]]
    assert holder.stdin.closed and holder.stdout.closed and holder.waits == 2


@pytest.mark.parametrize(
    ("start", "ready", "waited"),
    [
        ("2026-07-15T12:15:00.125+00:00", "2026-07-15T12:15:00.125000Z", 0),
        ("2026-07-15T12:20:00+00:00", "2026-07-15T12:20:00Z", 0),
        ("2026-07-15T12:35:00+00:00", "2026-07-15T13:00:00Z", 1500),
    ],
)
def test_analysis_headroom_is_bounded_operator_timing_not_reference_override(
    tmp_path: Path, start: str, ready: str, waited: int
) -> None:
    clock = Clock(datetime.fromisoformat(start))
    cycle = FakeCycle(configuration(tmp_path), clock)
    result = cycle.wait_for_analysis_window()
    assert result["ready_at"] == ready
    assert result["waited_seconds"] == waited
    assert result["remaining_seconds"] >= 40 * 60
    assert len(clock.sleeps) <= 121
    assert cycle.calls == []


def test_analysis_headroom_rechecks_readiness_after_wait(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 7, 15, 13, tzinfo=UTC))
    cycle = FakeCycle(configuration(tmp_path), clock)

    def expired(phase: str) -> dict[str, Any] | None:
        if phase == "readiness":
            assert clock() == datetime(2026, 7, 15, 14, tzinfo=UTC)
            return {"ready": False, "reason": "coverage_expired"}
        return None

    cycle.hook = expired
    with pytest.raises(daily.DailyError, match="fresh_120h_baseline_not_ready"):
        cycle.execute()
    assert "forecast" not in [call[0] for call in cycle.calls]


def test_slow_readiness_cannot_consume_headroom_then_start_paid_forecast(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    clock = Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    cycle = FakeCycle(config, clock)

    def slow_readiness(phase: str) -> dict[str, Any] | None:
        if phase == "readiness":
            clock.value += timedelta(minutes=30)
            return {"ready": True, "baseline": cycle.baseline()}
        return None

    cycle.hook = slow_readiness
    with pytest.raises(daily.DailyError, match="forecast_headroom_consumed_before_launch"):
        cycle.execute()
    assert "forecast" not in [call[0] for call in cycle.calls]
    assert cycle.record["phases"]["forecast"]["status"] == "failed"
    retry = FakeCycle(config, clock)
    with pytest.raises(daily.DailyError, match="forecast_outcome_requires_operator_inspection"):
        retry.execute()
    assert "forecast" not in [call[0] for call in retry.calls]


def test_analysis_headroom_stalled_clock_never_waits_indefinitely(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, 35, tzinfo=UTC))
    cycle = FakeCycle(configuration(tmp_path), clock)
    sleeps: list[float] = []
    cycle.sleep = sleeps.append
    with pytest.raises(daily.DailyError, match="analysis_window_wait_clock_failed"):
        cycle.wait_for_analysis_window()
    assert len(sleeps) == 121
    assert cycle.calls == []


@pytest.mark.parametrize("minutes", [0, 60, True, "40", 1.5, None])
def test_analysis_headroom_configuration_is_explicit_and_bounded(
    tmp_path: Path, minutes: object
) -> None:
    config = configuration(tmp_path)
    config["forecast_min_remaining_minutes"] = minutes
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(config), "utf-8")
    with pytest.raises(daily.DailyError, match="forecast_min_remaining_minutes"):
        daily.load_config(path)


def test_projected_backup_overflow_stops_cycle_before_guidance_or_paid_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path),
        clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)),
        disk_free=lambda _: 40 * 1024**3,
    )
    calls: list[str] = []

    def command(phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        calls.append(phase)
        assert phase == "backup-estimate"
        return {"status": "estimated", "backup_bytes": 21 * 1024**3}

    monkeypatch.setattr(cycle, "command", command)
    with pytest.raises(daily.DailyError, match="backup_would_cross_disk_reserve"):
        cycle.execute()
    assert calls == ["backup-estimate"]
    assert cycle.record["phases"] == {}
    assert cycle.record["status"] == "failed"


@pytest.mark.parametrize(
    "estimate",
    [
        {"status": "unavailable"},
        {"status": "estimated", "backup_bytes": None},
        {"status": "estimated", "backup_bytes": -1},
        {"status": "estimated", "backup_bytes": True},
        {"status": "estimated", "backup_bytes": 1.5},
    ],
)
def test_unproven_backup_size_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, estimate: dict[str, Any]
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path),
        clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)),
        disk_free=lambda _: 40 * 1024**3,
    )
    monkeypatch.setattr(cycle, "command", lambda *_: estimate)
    with pytest.raises(daily.DailyError, match="backup_size_unproven"):
        cycle.preflight()


def test_heavy_role_admission_is_host_wide_without_touching_any_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def docker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="another-project-worker\n", stderr="")

    monkeypatch.setattr(daily.subprocess, "run", docker)
    cycle = daily.DailyCycle(
        configuration(tmp_path), clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC))
    )
    with pytest.raises(daily.DailyError, match="existing_guidance-worker_running"):
        cycle.no_heavy_worker()
    assert len(commands) == 1
    assert commands[0] == [
        daily.DOCKER,
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.service=guidance-worker",
    ]


def test_backup_stops_owned_copy_when_reserve_is_consumed_mid_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = daily.DailyCycle(
        configuration(tmp_path),
        clock=Clock(datetime(2026, 7, 15, 11, 5, tzinfo=UTC)),
        disk_free=lambda _: 40 * 1024**3,
    )
    cycle.prepare()
    cycle.record = {"maintenance": {"fingerprint": "a" * 64}, "phases": {}}
    held = False
    terminated = False

    @contextmanager
    def lock():
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def command(phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        assert phase in {"backup-estimate", "retention-dry-run"}
        if phase == "backup-estimate":
            return {"status": "estimated", "backup_bytes": 1024}
        return {"plan_sha256": "b" * 64}

    class Copy:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            nonlocal terminated
            terminated = True

        def wait(self, *, timeout: int) -> int:
            assert timeout == 120 and terminated
            return 1

    def create(args: list[str], **kwargs: Any) -> Copy:
        assert held
        cycle.disk_free = lambda _: 19 * 1024**3
        return Copy()

    monkeypatch.setattr(cycle, "backup_lock", lock)
    monkeypatch.setattr(cycle, "command", command)
    monkeypatch.setattr(cycle, "no_heavy_worker", lambda: None)
    monkeypatch.setattr(cycle, "sample_resources", lambda: None)
    monkeypatch.setattr(daily.subprocess, "Popen", create)
    with pytest.raises(
        daily.DailyError, match="backup_disk_reserve_reached; inspect_incomplete_set"
    ):
        cycle.backup()
    assert terminated and not held
