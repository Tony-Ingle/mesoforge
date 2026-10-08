#!/usr/bin/env python3
"""Serial, opt-in daily operator composition; no meteorology or credentials here.

Run on the Linux Docker host. Durable day receipts live outside the checkout. The
existing workers, issuance locks and delivery journal remain authoritative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

# Host orchestration needs only these stdlib modules, not a second scientific venv.
ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker") or "/usr/bin/docker"
sys.path.insert(0, str(ROOT / "src"))
from mesoforge.application.disk_admission import DiskPolicy  # noqa: E402
from mesoforge.application.forecast_schedule import ForecastSchedule  # noqa: E402
from mesoforge.application.worker_lock import single_writer  # noqa: E402
from mesoforge.application.worker_status import (  # noqa: E402
    iso,
    parse_instant,
    read_json,
    write_json,
)


class DailyError(RuntimeError):
    """A bounded operator failure with a non-secret reason."""


def last_json(text: str) -> dict[str, Any]:
    """Read the last complete JSON document after structured worker log lines."""
    decoder = json.JSONDecoder()
    result: dict[str, Any] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("{"):
            try:
                value, _ = decoder.raw_decode(text[offset:])
            except ValueError:
                pass
            else:
                if isinstance(value, dict):
                    result = value
        offset += len(line)
    if result is None:
        raise DailyError("worker_result_unreadable; inspect restricted phase log")
    return result


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text("utf-8"))
    required = {
        "project",
        "env_file",
        "compose_files",
        "state_root",
        "runtime_root",
        "recipients",
        "approved_template",
    }
    if not isinstance(config, dict) or not required <= config.keys():
        raise DailyError("daily_configuration_incomplete")
    for key in ("env_file", "state_root"):
        if not Path(config[key]).is_absolute():
            raise DailyError(f"{key}_must_be_absolute")
    state = Path(config["state_root"]).resolve()
    if (
        state.is_relative_to(ROOT)
        or ROOT.is_relative_to(state)
        or state == Path.home().resolve()
        or any(part == "_work" or part.startswith("actions-runner") for part in state.parts)
    ):
        raise DailyError("daily_state_must_be_outside_checkout_and_runner_workspaces")
    runtime = PurePosixPath(config["runtime_root"])
    if not runtime.is_relative_to("/var/lib/mesoforge/runtime") or ".." in runtime.parts:
        raise DailyError("runtime_must_be_inside_existing_runtime_volume")
    if not config["compose_files"] or any(
        not Path(p).is_absolute() for p in config["compose_files"]
    ):
        raise DailyError("explicit_absolute_compose_files_required")
    recipients = config["recipients"]
    if (
        not isinstance(recipients, list)
        or not recipients
        or any(
            not isinstance(value, str) or "@" not in value or any(c in value for c in "\r\n")
            for value in recipients
        )
        or len(set(recipients)) != len(recipients)
    ):
        raise DailyError("unique_configured_recipients_required")
    config.setdefault("forecast_time", "07:15")
    config.setdefault("delivery_time", "08:00")
    schedule = ForecastSchedule("America/Chicago", (config["forecast_time"],))
    delivery = ForecastSchedule("America/Chicago", (config["delivery_time"],))
    if not "06:05" < schedule.times[0] < delivery.times[0]:
        raise DailyError("require_guidance_then_forecast_then_delivery")
    return config


class DailyCycle:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
    ) -> None:
        self.config, self.clock, self.sleep, self.disk_free = config, clock, sleep, disk_free
        self.state_root = Path(config["state_root"])
        self.schedule = ForecastSchedule("America/Chicago", (config["forecast_time"],))
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DailyError("aware_runtime_clock_required")
        self.day = now.astimezone(self.schedule.zone).date()
        self.slot = self.schedule.slots_on(self.day)[0]
        self.delivery = ForecastSchedule("America/Chicago", (config["delivery_time"],)).slots_on(
            self.day
        )[0]
        self.directory = self.state_root / self.day.isoformat()
        self.receipt = self.directory / "result.json"
        self.record: dict[str, Any] = {}
        self.compose = [
            DOCKER,
            "compose",
            "--project-name",
            config["project"],
            "--env-file",
            config["env_file"],
        ]
        for path in config["compose_files"]:
            self.compose.extend(["-f", path])
        self.runtime = config["runtime_root"]
        self.common = ["--root", self.runtime, "--config", "/run/mesoforge/locations.json"]
        self.override = self.directory / "daily.override.json"

    def status(self) -> dict[str, Any]:
        # Read-only: no mkdir, container, provider or receipt mutation.
        existing = self.state_root if self.state_root.exists() else self.state_root.parent
        return {
            "local_date": self.day.isoformat(),
            "forecast_slot": iso(self.slot),
            "delivery_not_before": iso(self.delivery),
            "disk": DiskPolicy.from_environment().report(self.disk_free(existing)),
            "receipt": read_json(self.receipt),
        }

    def save(self) -> None:
        write_json(self.receipt, self.record)

    def prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_root, 0o700)
        os.chmod(self.directory, 0o700)
        registry = json.loads((ROOT / "configs/locations.json").read_text("utf-8"))
        locations = [row for row in registry["locations"] if row["id"] == "minneapolis"]
        if len(locations) != 1 or (locations[0]["lat"], locations[0]["lon"]) != (
            44.98861,
            -93.25553,
        ):
            raise DailyError("maintained_minneapolis_registry_mismatch")
        selection = self.directory / "locations.json"
        write_json(selection, {"locations": locations})
        # The host receipt directory stays private. This non-secret file is bound
        # directly into uid10001 containers and must be readable even with umask077.
        selection.chmod(0o644)
        # Same selection/root/horizon for both roles, independent secret mounts intact.
        services = {}
        for role in ("guidance-worker", "forecast-worker", "delivery"):
            services[role] = {
                "restart": "no",
                "environment": {
                    "MESOFORGE_PROSPECTIVE_ROOT": self.runtime,
                    "MESOFORGE_FORECAST_TIMEZONE": "America/Chicago",
                    "MESOFORGE_FORECAST_TIMES": self.config["forecast_time"],
                    "MESOFORGE_FORECAST_HORIZON_HOURS": "120",
                },
                "volumes": [f"{selection}:/run/mesoforge/locations.json:ro"],
            }
        write_json(self.override, {"services": services})
        self.compose.extend(["-f", str(self.override)])

    def no_heavy_worker(self) -> None:
        # Also catch a manually started worker in another runtime root of this live project.
        for role in ("guidance-worker", "forecast-worker"):
            result = subprocess.run(  # noqa: S603
                [
                    DOCKER,
                    "ps",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={self.config['project']}",
                    "--filter",
                    f"label=com.docker.compose.service={role}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            if result.stdout.strip():
                raise DailyError(f"existing_{role}_running; do_not_stop_another_workload")

    def sample_resources(self) -> None:
        """Bounded host samples during owned container work; no monitoring service."""
        peak = self.record.setdefault("resource_samples", {})
        free = self.disk_free(self.state_root)
        peak["minimum_free_bytes"] = min(free, peak.get("minimum_free_bytes", free))
        memory = Path("/proc/meminfo")
        if memory.exists():
            values = {
                line.split(":")[0]: int(line.split()[1]) * 1024
                for line in memory.read_text().splitlines()
            }
            used = values["MemTotal"] - values["MemAvailable"]
            swap = values["SwapTotal"] - values["SwapFree"]
            peak["host_used_bytes"] = max(used, peak.get("host_used_bytes", 0))
            peak["host_swap_bytes"] = max(swap, peak.get("host_swap_bytes", 0))
            load = float(Path("/proc/loadavg").read_text().split()[0])
            peak["load_1m"] = max(load, peak.get("load_1m", 0))
        peak["container_peak"] = "not_collected"
        peak["sampled_at"] = iso(self.clock())
        self.save()

    def command(self, phase: str, role: str, module: str, args: list[str]) -> dict[str, Any]:
        self.no_heavy_worker()
        name = f"mesoforge-daily-{self.day:%Y%m%d}-{os.getpid()}-{phase}"
        command = [
            *self.compose,
            "run",
            "--rm",
            "--no-deps",
            "--name",
            name,
            "-T",
            role,
            module,
            *args,
        ]
        log = self.directory / f"{phase}-{time.time_ns()}.log"
        # Workers already redact diagnostics; logs remain private and are not uploaded to GHA.
        with log.open("x", encoding="utf-8") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, text=True)  # noqa: S603
            try:
                deadline = time.monotonic() + (3 * 3600 if phase == "guidance" else 3600)
                while True:
                    self.sample_resources()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DailyError(f"{phase}_timeout")
                    try:
                        code = process.wait(timeout=min(30, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except BaseException:
                # Stop only this invocation's named container. Guidance handles SIGTERM at
                # a safe phase boundary; never issue compose down or remove live volumes.
                subprocess.run(  # noqa: S603
                    [DOCKER, "stop", "--time", "1200", name],
                    check=False,
                    timeout=1230,
                    capture_output=True,
                )  # noqa: S603
                process.wait(timeout=30)
                raise
        if code:
            try:
                diagnostic = last_json(log.read_text("utf-8"))
            except DailyError:
                diagnostic = {"reason": "No structured result; inspect restricted phase log"}
            self.record["worker_failure"] = {
                "phase": phase,
                "exit_code": code,
                "result": diagnostic,
            }
            self.save()
            raise DailyError(f"{phase}_exit_{code}; inspect {log.name}")
        value = last_json(log.read_text("utf-8")) if phase != "guidance" else {"log": str(log)}
        return value

    def phase(self, name: str, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        previous = self.record["phases"].get(name)
        if previous and previous["status"] == "completed":
            return dict(previous["result"])
        if previous and (previous["status"] == "started" or name == "forecast"):
            raise DailyError(f"{name}_outcome_requires_operator_inspection; no_automatic_repeat")
        row: dict[str, Any] = {"status": "started", "started_at": iso(self.clock())}
        self.record["phases"][name] = row
        self.record["phase"] = name
        self.save()  # Intent is durable before any paid or expensive work.
        try:
            result = operation()
        except Exception as exc:
            row.update(
                status="failed",
                error=str(exc) if isinstance(exc, DailyError) else type(exc).__name__,
            )
            raise
        else:
            row.update(status="completed", result=result)
            return result
        finally:
            row["finished_at"] = iso(self.clock())
            row["seconds"] = (
                self.clock() - datetime.fromisoformat(row["started_at"])
            ).total_seconds()
            self.save()

    def require_slot(self) -> None:
        if self.clock() >= self.schedule.window_end(self.slot):
            raise DailyError("forecast_window_closed; skip_today_without_backdating")

    def wait_for_slot(self) -> None:
        # Finite even for a manual invocation after a backward wall-clock change.
        checks = max(0, int((self.slot - self.clock()).total_seconds()) // 30) + 3
        for _ in range(checks):
            if self.clock() >= self.slot:
                self.require_slot()
                return
            self.sleep(min(30, (self.slot - self.clock()).total_seconds()))
        raise DailyError("forecast_slot_wait_clock_failed; no_paid_work_started")

    def fresh(self, report: dict[str, Any]) -> dict[str, Any]:
        baseline = report.get("baseline") or {}
        morning = ForecastSchedule("America/Chicago", ("06:00",)).slots_on(self.day)[0]
        if not report.get("ready") or baseline.get("forecast_horizon_hours") != 120:
            raise DailyError("fresh_120h_baseline_not_ready")
        for key in ("prepared_reference_time", "published_at"):
            instant = parse_instant(baseline.get(key))
            if instant is None or not morning <= instant <= self.clock():
                raise DailyError(
                    f"baseline_{key}_not_from_this_morning; skip_without_stale_fallback"
                )
        return baseline

    def guidance(self) -> dict[str, Any]:
        self.require_slot()
        morning_start = ForecastSchedule("America/Chicago", ("06:05",)).slots_on(self.day)[0]
        if self.clock() < morning_start:
            raise DailyError("guidance_not_due; no_overnight_acquisition")
        disk = DiskPolicy.from_environment().report(self.disk_free(self.state_root))
        if not disk["heavy_work_admitted"]:
            raise DailyError("disk_refused; no_automatic_scientific_deletion")
        started = self.clock()
        self.command(
            "guidance",
            "guidance-worker",
            "mesoforge.application.guidance_worker",
            ["once", *self.common, "--forecast-horizon-hours", "120"],
        )
        state = self.command(
            "guidance-status",
            "guidance-worker",
            "mesoforge.application.guidance_worker",
            ["status", *self.common],
        )
        poll = (state.get("polls") or {}).get("last") or {}
        poll_start = parse_instant(poll.get("started_at"))
        if (
            poll_start is None
            or poll_start < started
            or set(poll.get("categories") or []) - {"candidate_background_failed"}
            or poll.get("stopped_early")
        ):
            raise DailyError("guidance_not_successful; inspect_worker_status")
        baseline = self.fresh(state.get("readiness") or {})
        views = baseline.get("reference_times") or {}
        first, last = parse_instant(views.get("first")), parse_instant(views.get("last"))
        forecast_hour = self.slot.replace(minute=0, second=0, microsecond=0)
        if first is None or last is None or not first <= forecast_hour <= last:
            raise DailyError("morning_forecast_view_not_covered")
        return {"baseline": baseline, "poll": poll, "disk": disk}

    def forecast(self, baseline_id: str) -> dict[str, Any]:
        self.require_slot()
        result = self.command(
            "forecast",
            "forecast-worker",
            "mesoforge.application.forecast_worker",
            ["run", *self.common, "--scheduled", "--expected-baseline-id", baseline_id, "--json"],
        )
        if result.get("status") != "completed" or result.get("baseline") != baseline_id:
            raise DailyError("forecast_not_completed_on_pinned_baseline")
        rows = result.get("results", [])
        if len(rows) != 1 or rows[0].get("location", {}).get("id") != "minneapolis":
            raise DailyError("forecast_location_mismatch")
        row = rows[0]
        if row.get("status") not in {"issued", "skipped_already_issued"}:
            raise DailyError("primary_issuance_status_not_proven")
        identity = row.get("issued_forecast_id")
        existing = row.get("existing_issued_forecast_ids") or []
        if identity is None and len(existing) == 1:
            identity = existing[0]
        if not identity:
            raise DailyError("single_primary_issuance_not_proven")
        return {**result, "issued_forecast_id": identity}

    def email(self, phase: str, args: list[str], recipient: str) -> dict[str, Any]:
        result = self.command(
            phase,
            "delivery",
            "mesoforge.application.forecast_delivery",
            [
                "send",
                *args,
                "--recipient",
                recipient,
                "--approved-template",
                self.config["approved_template"],
                "--not-before",
                iso(self.delivery),
            ],
        )
        outcome = result.get("status")
        if outcome == "duplicate_suppressed":
            outcome = (result.get("previous_outcome") or {}).get("status")
        if outcome != "accepted":
            raise DailyError("email_not_accepted; issuance_preserved; inspect_delivery_audit")
        return result

    def execute(self) -> dict[str, Any]:
        # Atomic receipts plus a persistent host lock complement, never replace, the
        # database issuance/advisory locks and immutable SMTP delivery intent.
        self.state_root.mkdir(parents=True, exist_ok=True)
        with single_writer(self.state_root) as acquired:
            if not acquired:
                raise DailyError("daily_cycle_already_running")
            self.prepare()
            identity = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
            retained = read_json(self.receipt)
            if self.receipt.exists() and retained is None:
                raise DailyError("daily_receipt_unreadable; do_not_repeat_paid_work")
            self.record = retained or {
                "local_date": self.day.isoformat(),
                "config_digest": identity,
                "started_at": iso(self.clock()),
                "phases": {},
                "status": "started",
            }
            if self.record.get("config_digest") != identity:
                raise DailyError("daily_configuration_changed; inspect_existing_receipt")
            if self.record["status"] == "completed":
                return {**self.record, "repeat": "completed_day_reused"}
            try:
                if "forecast" not in self.record["phases"]:
                    self.require_slot()
                guidance = self.phase("guidance", self.guidance)
                baseline_id = guidance["baseline"]["baseline_snapshot_id"]
                if (self.record["phases"].get("forecast") or {}).get("status") != "completed":
                    self.record["phase"] = "wait_for_forecast_slot"
                    self.save()
                    self.wait_for_slot()
                    self.record["phase"] = "readiness"
                    self.save()
                    readiness = self.command(
                        "readiness",
                        "forecast-worker",
                        "mesoforge.application.forecast_worker",
                        ["readiness", *self.common],
                    )
                    self.record["handoff_readiness"] = readiness
                    if self.fresh(readiness)["baseline_snapshot_id"] != baseline_id:
                        raise DailyError("baseline_changed_after_daily_pin")
                forecast = self.phase("forecast", lambda: self.forecast(baseline_id))
                pdf = f"{self.runtime}/delivery/daily-{self.day.isoformat()}.pdf"
                args = [
                    "--issued-id",
                    forecast["issued_forecast_id"],
                    "--location",
                    "minneapolis",
                    "--config",
                    "/run/mesoforge/locations.json",
                    "--pdf",
                    pdf,
                    "--product",
                    "120-hour",
                ]
                self.phase(
                    "pdf",
                    lambda: self.command(
                        "pdf",
                        "delivery",
                        "mesoforge.application.forecast_delivery",
                        ["render", *args],
                    ),
                )
                for index, recipient in enumerate(self.config["recipients"]):
                    phase = f"email-{index}"
                    self.phase(phase, partial(self.email, phase, args, recipient))
                self.record.update(status="completed", finished_at=iso(self.clock()))
            except Exception as exc:
                self.record.update(
                    status="failed",
                    reason=str(exc) if isinstance(exc, DailyError) else type(exc).__name__,
                )
                raise
            finally:
                self.record["disk_after"] = DiskPolicy.from_environment().report(
                    self.disk_free(self.state_root)
                )
                self.save()
            return self.record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("operation", choices=("run", "status"))
    args = parser.parse_args(argv)
    try:
        cycle = DailyCycle(load_config(args.config))
        if args.operation == "run":
            # No daemon/scheduler is installed. GHA has its own explicit enable gate.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            result = cycle.execute()
        else:
            result = cycle.status()
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": str(exc) if isinstance(exc, DailyError) else type(exc).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
