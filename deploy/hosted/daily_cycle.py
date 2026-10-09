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
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

# Host orchestration needs only these stdlib modules, not a second scientific venv.
ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker") or "/usr/bin/docker"
DAILY_LOCATIONS = ("minneapolis", "grasston")
sys.path.insert(0, str(ROOT / "src"))
from mesoforge.application.disk_admission import DiskPolicy  # noqa: E402
from mesoforge.application.forecast_schedule import ForecastSchedule  # noqa: E402
from mesoforge.application.location_config import (  # noqa: E402
    load_locations,
    location_email_recipients,
)
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
        "backup_root",
        "runtime_root",
        "approved_template",
    }
    if not isinstance(config, dict) or not required <= config.keys():
        raise DailyError("daily_configuration_incomplete")
    for key in ("env_file", "state_root", "backup_root"):
        if not Path(config[key]).is_absolute():
            raise DailyError(f"{key}_must_be_absolute")
    for key in ("state_root", "backup_root"):
        directory = Path(config[key]).resolve()
        if (
            directory.is_relative_to(ROOT)
            or ROOT.is_relative_to(directory)
            or directory == Path.home().resolve()
            or any(part == "_work" or part.startswith("actions-runner") for part in directory.parts)
        ):
            raise DailyError(f"{key}_must_be_outside_checkout_and_runner_workspaces")
    state, backup = Path(config["state_root"]).resolve(), Path(config["backup_root"]).resolve()
    if state.is_relative_to(backup) or backup.is_relative_to(state):
        raise DailyError("backup_and_daily_state_roots_must_be_separate")
    runtime = PurePosixPath(config["runtime_root"])
    if not runtime.is_relative_to("/var/lib/mesoforge/runtime") or ".." in runtime.parts:
        raise DailyError("runtime_must_be_inside_existing_runtime_volume")
    if not config["compose_files"] or any(
        not Path(p).is_absolute() for p in config["compose_files"]
    ):
        raise DailyError("explicit_absolute_compose_files_required")
    if "recipients" in config:
        raise DailyError("global_recipients_not_supported; use_location_email_recipients")
    counts = config.get("retention_cycles", {})
    if (
        not isinstance(counts, dict)
        or set(counts) - {"hrrr", "rap", "gfs", "ifs", "nbm", "gefs", "ecmwf-ens"}
        or any(type(value) is not int or value < 1 for value in counts.values())
    ):
        raise DailyError("retention_cycles_requires_known_models_and_positive_integer_counts")
    config.setdefault("forecast_time", "07:15")
    config.setdefault("delivery_time", "08:00")
    config.setdefault("forecast_min_remaining_minutes", 40)
    remaining = config["forecast_min_remaining_minutes"]
    if type(remaining) is not int or not 1 <= remaining <= 59:
        raise DailyError("forecast_min_remaining_minutes_must_be_integer_1_to_59")
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
        location_id: str = "minneapolis",
    ) -> None:
        if location_id not in DAILY_LOCATIONS:
            raise DailyError("unsupported_daily_location")
        self.config, self.clock, self.sleep, self.disk_free = config, clock, sleep, disk_free
        self.location_id = location_id
        self.workflow_slot = "daily"
        self.forecast_time = config["forecast_time"] if location_id == "minneapolis" else "08:15"
        self.delivery_time = config["delivery_time"] if location_id == "minneapolis" else "09:30"
        self.state_root = Path(config["state_root"])
        self.schedule = ForecastSchedule("America/Chicago", (self.forecast_time,))
        now = clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DailyError("aware_runtime_clock_required")
        self.day = now.astimezone(self.schedule.zone).date()
        self.slot = self.schedule.slots_on(self.day)[0]
        self.delivery = ForecastSchedule("America/Chicago", (self.delivery_time,)).slots_on(
            self.day
        )[0]
        self.directory = self.state_root / self.day.isoformat() / location_id / self.workflow_slot
        self.receipt = self.directory / "result.json"
        self.record: dict[str, Any] = {}
        self.location: dict[str, Any] = {}
        self.recipients: list[str] = []
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
            "location_id": self.location_id,
            "workflow_slot": self.workflow_slot,
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
        locations = [
            row
            for row in load_locations(ROOT / "configs/locations.json")
            if isinstance(row, dict) and row.get("id") == self.location_id
        ]
        if len(locations) != 1:
            raise DailyError("maintained_location_registry_mismatch")
        self.recipients = location_email_recipients(locations[0])
        self.location = {**locations[0], "email_recipients": self.recipients}
        selection = self.directory / "locations.json"
        write_json(selection, {"locations": [self.location]})
        # The host receipt directory stays private. This non-secret file is bound
        # directly into uid10001 containers and must be readable even with umask077.
        selection.chmod(0o644)
        guidance_selection = self.directory / "guidance-locations.json"
        shared_locations = [
            row
            for row in load_locations(ROOT / "configs/locations.json")
            if isinstance(row, dict) and row.get("id") in DAILY_LOCATIONS
        ]
        if {row["id"] for row in shared_locations} != set(DAILY_LOCATIONS):
            raise DailyError("shared_guidance_registry_incomplete")
        write_json(guidance_selection, {"locations": shared_locations})
        guidance_selection.chmod(0o644)
        # One shared prepared footprint, but only this location's baseline/issuance.
        services: dict[str, Any] = {}
        for role in ("guidance-worker", "forecast-worker", "delivery"):
            services[role] = {
                "restart": "no",
                "environment": {
                    "MESOFORGE_PROSPECTIVE_ROOT": self.runtime,
                    "MESOFORGE_FORECAST_TIMEZONE": "America/Chicago",
                    "MESOFORGE_FORECAST_TIMES": self.forecast_time,
                    "MESOFORGE_FORECAST_HORIZON_HOURS": "120",
                },
                "volumes": [f"{selection}:/run/mesoforge/locations.json:ro"],
            }
        services["guidance-worker"]["volumes"].append(
            f"{guidance_selection}:/run/mesoforge/guidance-locations.json:ro"
        )
        host_user = getattr(os, "getuid", lambda: 1000)()
        host_group = getattr(os, "getgid", lambda: 1000)()
        services["admin"] = {
            "user": f"{host_user}:{host_group}",
            "environment": {"HOME": "/tmp"},  # noqa: S108 - container-local, no host temp files
            "volumes": [f"{self.config['backup_root']}:/recovery"],
        }
        write_json(self.override, {"services": services})
        self.compose.extend(["-f", str(self.override)])

    def no_heavy_worker(self) -> None:
        # Capacity is host-wide: another proof/deployment project may use a different
        # runtime volume and therefore a different lock. Refuse, never stop it.
        for role in ("guidance-worker", "forecast-worker"):
            result = subprocess.run(  # noqa: S603
                [
                    DOCKER,
                    "ps",
                    "-q",
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

    def require_current_day(self) -> None:
        if self.clock().astimezone(self.schedule.zone).date() != self.day:
            raise DailyError("local_day_changed; inspect_daily_state_without_backdating")

    def wait_for_slot(self) -> None:
        # Finite even for a manual invocation after a backward wall-clock change.
        checks = max(0, int((self.slot - self.clock()).total_seconds()) // 30) + 3
        for _ in range(checks):
            if self.clock() >= self.slot:
                self.require_current_day()
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

    def wait_for_analysis_window(self) -> dict[str, Any]:
        """Leave measured processing headroom before the first hour can expire.

        This is operator timing, never a forecast/reference override. A late run
        waits at most for the next UTC hour, then normal readiness and the worker's
        actual-clock reference selection remain authoritative.
        """
        self.require_current_day()
        started = self.clock().astimezone(UTC)
        minimum = self.config["forecast_min_remaining_minutes"] * 60
        boundary = started.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if (boundary - started).total_seconds() < minimum:
            # Finite even if the wall clock stalls or moves backward. No provider
            # work occurs during this bounded readiness wait.
            for _ in range(121):
                now = self.clock().astimezone(UTC)
                if now >= boundary:
                    break
                self.sleep(min(30, (boundary - now).total_seconds()))
            else:
                raise DailyError("analysis_window_wait_clock_failed; no_paid_work_started")
        self.require_current_day()
        ready = self.clock().astimezone(UTC)
        next_boundary = ready.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        remaining = (next_boundary - ready).total_seconds()
        if remaining < minimum:
            raise DailyError("analysis_window_headroom_unavailable; no_paid_work_started")
        return {
            "minimum_remaining_minutes": self.config["forecast_min_remaining_minutes"],
            "started_at": iso(started),
            "ready_at": iso(ready),
            "waited_seconds": (ready - started).total_seconds(),
            "remaining_seconds": remaining,
        }

    def guidance(self) -> dict[str, Any]:
        self.require_current_day()
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
            [
                "once",
                *self.common,
                "--forecast-horizon-hours",
                "120",
                "--guidance-config",
                "/run/mesoforge/guidance-locations.json",
                "--no-hourly-probe",
            ],
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
        forecast_hour = max(self.slot, self.clock()).replace(minute=0, second=0, microsecond=0)
        if first is None or last is None or not first <= forecast_hour <= last:
            raise DailyError("morning_forecast_view_not_covered")
        return {"baseline": baseline, "poll": poll, "disk": disk}

    def forecast(self, baseline_id: str) -> dict[str, Any]:
        self.require_current_day()
        now = self.clock().astimezone(UTC)
        boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if (boundary - now).total_seconds() < self.config["forecast_min_remaining_minutes"] * 60:
            raise DailyError("forecast_headroom_consumed_before_launch; no_paid_work_started")
        result = self.command(
            "forecast",
            "forecast-worker",
            "mesoforge.application.forecast_worker",
            ["run", *self.common, "--expected-baseline-id", baseline_id, "--json"],
        )
        if result.get("status") != "completed" or result.get("baseline") != baseline_id:
            raise DailyError("forecast_not_completed_on_pinned_baseline")
        rows = result.get("results", [])
        if len(rows) != 1 or rows[0].get("location", {}).get("id") != self.location_id:
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

    def _backup_root(self) -> Path:
        root = Path(self.config["backup_root"])
        # Host orchestration supports Debian's Python 3.11; junction inspection
        # exists only on 3.12+. Linux symlinks remain explicitly rejected.
        if any(
            p.is_symlink() or getattr(p, "is_junction", lambda: False)()
            for p in (root, *root.parents)
        ):
            raise DailyError("backup_root_must_not_follow_links")
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        return root

    def latest_verified_backup(self) -> dict[str, Any] | None:
        """The newest verified same-host backup, read only; it is never touched here."""
        root = Path(self.config["backup_root"])
        latest: dict[str, Any] | None = None
        for path in sorted(root.glob("*/local-backup-receipt.json")) if root.is_dir() else []:
            receipt = read_json(path)
            if not isinstance(receipt, dict) or receipt.get("status") != "verified":
                continue
            verified_at = parse_instant(receipt.get("verified_at"))
            if verified_at is None or (latest is not None and verified_at <= latest["_at"]):
                continue
            latest = {
                "host_directory": str(path.parent),
                "backup_id": receipt.get("backup_id"),
                "verified_at": receipt.get("verified_at"),
                "bytes": receipt.get("bytes"),
                "_at": verified_at,
            }
        if latest is not None:
            del latest["_at"]
        return latest

    def preflight(self) -> dict[str, Any]:
        """Verify local recovery storage before acquisition or paid forecast work.

        The storage reserve is the only capacity gate for heavy work. A replacement
        full backup is not sized here: it is attempted after a successful day when
        space allows, and the latest verified same-host backup stays the recovery
        checkpoint until then.
        """
        root = self._backup_root()
        report = DiskPolicy.from_environment().report(self.disk_free(root))
        if not report["heavy_work_admitted"]:
            raise DailyError("backup_disk_reserve_reached")
        return {
            "recovery": "same_host",
            "off_host": False,
            "disk": report,
            "recovery_checkpoint": self.latest_verified_backup(),
            "replacement_backup": "after_delivery_if_space_allows",
        }

    def backup_capacity(self) -> dict[str, Any]:
        """Whether a replacement full backup fits now without crossing the reserve."""
        root = self._backup_root()
        report = DiskPolicy.from_environment().report(self.disk_free(root))
        estimate = self.command(
            "backup-estimate",
            "guidance-worker",
            "mesoforge.application.local_backup",
            ["estimate", "--runtime-root", "/var/lib/mesoforge/runtime"],
        )
        required = estimate.get("backup_bytes")
        if estimate.get("status") != "estimated" or type(required) is not int or required < 0:
            raise DailyError("backup_size_unproven")
        free = self.disk_free(root)
        fits = bool(report["heavy_work_admitted"]) and free - required >= report["min_free_bytes"]
        return {
            "fits": fits,
            "disk": report,
            "estimate": estimate,
            "free_bytes": free,
            "required_bytes": required,
        }

    def backup(self) -> dict[str, Any]:
        """Use the committed full recovery procedure, then validate every saved byte.

        Without room for a replacement above the reserve, the backup is skipped and
        the previous verified backup remains the recovery checkpoint; the issued
        forecast and its delivery evidence are not invalidated by that.
        """
        capacity = self.backup_capacity()
        if not capacity["fits"]:
            return {
                "status": "backup_skipped_low_space",
                **capacity,
                "retained_backup": self.latest_verified_backup(),
            }
        self.no_heavy_worker()
        fingerprint = self.record["maintenance"]["fingerprint"]
        plan = self.command(
            "retention-dry-run",
            "guidance-worker",
            "mesoforge.application.guidance_retention",
            ["--runtime-root", self.runtime, "--dry-run", *self.retention_args()],
        )
        plan_file = self.directory / f"retention-{fingerprint}.json"
        write_json(plan_file, plan)
        receipts = self.directory / "backup-receipts.json"
        write_json(
            receipts,
            {
                path.relative_to(self.state_root).as_posix(): read_json(path)
                for path in sorted(self.state_root.rglob("result.json"))
            },
        )
        deployment = self.directory / "backup-deployment.json"
        write_json(
            deployment,
            {
                "project": self.config["project"],
                "runtime_root": self.runtime,
                "runtime_archive_root": "/var/lib/mesoforge/runtime",
                "location": self.location,
                "forecast_time": self.forecast_time,
                "delivery_time": self.delivery_time,
                "approved_template": self.config["approved_template"],
                "retention_cycles": self.config.get("retention_cycles", {}),
            },
        )
        env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": self.config["project"],
            "COMPOSE_FILE": os.pathsep.join([*self.config["compose_files"], str(self.override)]),
            "COMPOSE_ENV_FILES": self.config["env_file"],
            "MESOFORGE_BACKUP_RETENTION_PLAN": str(plan_file),
            "MESOFORGE_BACKUP_DAILY_RECEIPTS": str(receipts),
            "MESOFORGE_BACKUP_DEPLOYMENT": str(deployment),
        }
        log = self.directory / f"backup-{fingerprint}.log"
        with self.backup_lock():
            if not log.exists():
                with log.open("x", encoding="utf-8") as output:
                    process = subprocess.Popen(  # noqa: S603
                        [
                            "/bin/sh",
                            str(ROOT / "deploy/hosted/backup.sh"),
                            self.config["backup_root"],
                        ],
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    try:
                        deadline = time.monotonic() + 3600
                        while process.poll() is None:
                            self.sample_resources()
                            if not DiskPolicy.from_environment().report(
                                self.disk_free(Path(self.config["backup_root"]))
                            )["heavy_work_admitted"]:
                                raise DailyError(
                                    "backup_disk_reserve_reached; inspect_incomplete_set"
                                )
                            if time.monotonic() >= deadline:
                                raise DailyError("backup_timeout; inspect_incomplete_set")
                            try:
                                process.wait(timeout=30)
                            except subprocess.TimeoutExpired:
                                pass
                    except BaseException:
                        process.terminate()
                        process.wait(timeout=120)
                        raise
                if process.returncode:
                    raise DailyError("backup_creation_failed; no_retention")
            completed = [
                line.removeprefix("Backup complete: ")
                for line in log.read_text("utf-8").splitlines()
                if line.startswith("Backup complete: ")
            ]
            if len(completed) != 1:
                raise DailyError("backup_completion_unproven; no_retention")
            source = Path(completed[0]).resolve()
            if source.parent != Path(self.config["backup_root"]).resolve() or not source.is_dir():
                raise DailyError("backup_path_outside_configured_root")
            result = self.command(
                "backup-validation",
                "admin",
                "mesoforge.application.local_backup",
                ["validate", "--source", f"/recovery/{source.name}"],
            )
            result["host_directory"] = str(source)
            return result

    @contextmanager
    def backup_lock(self) -> Iterator[None]:
        """Hold the workers' actual volume lock across the host recovery procedure.

        Closing the pipe (also on host-process death) releases the one-shot holder.
        Only this helper container is owned here; protected host services are untouched.
        """
        name = f"mesoforge-backup-lock-{os.getpid()}"
        with (self.directory / "backup-lock.log").open("a", encoding="utf-8") as errors:
            process = subprocess.Popen(  # noqa: S603
                [
                    *self.compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "--name",
                    name,
                    "--user",
                    "10001:10001",
                    "-T",
                    "admin",
                    "mesoforge.application.local_backup",
                    "hold-lock",
                    "--runtime-root",
                    self.runtime,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
            )
            try:
                assert process.stdout is not None
                ready, _, _ = select.select([process.stdout], [], [], 30)
                if not ready or json.loads(process.stdout.readline()).get("status") != "locked":
                    raise DailyError("backup_runtime_lock_unavailable")
                yield
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    subprocess.run(  # noqa: S603
                        [DOCKER, "stop", "--time", "10", name],
                        capture_output=True,
                        check=False,
                        timeout=30,
                    )
                    process.wait(timeout=30)
                if process.stdout is not None:
                    process.stdout.close()

    def retention_args(self) -> list[str]:
        args = []
        for model, count in sorted(self.config.get("retention_cycles", {}).items()):
            args.extend([f"--keep-{model}", str(count)])
        return args

    def retention(self, backup: dict[str, Any]) -> dict[str, Any]:
        """Cleanup may run only after a verified backup returned by the prior phase."""
        source = Path(backup["host_directory"])
        if source.parent.resolve() != Path(self.config["backup_root"]).resolve():
            raise DailyError("backup_path_outside_configured_root")
        receipt = self.directory / "verified-backup.json"
        write_json(receipt, backup)
        receipt.chmod(0o644)
        override = self.directory / "retention.override.json"
        write_json(
            override,
            {
                "services": {
                    "guidance-worker": {
                        "volumes": [f"{receipt}:/run/mesoforge/backup-receipt.json:ro"]
                    }
                }
            },
        )
        self.compose.extend(["-f", str(override)])
        result = self.command(
            "retention-apply",
            "guidance-worker",
            "mesoforge.application.guidance_retention",
            [
                "--runtime-root",
                self.runtime,
                "--apply",
                "--backup-receipt",
                "/run/mesoforge/backup-receipt.json",
                *self.retention_args(),
            ],
        )
        result["backup_prune"] = self.command(
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
        return result

    def maintenance(self) -> None:
        # A later accepted delivery adds audit evidence and requires a new backup.
        # Identical retries reuse completed maintenance, without duplicating history.
        outcomes = {
            name: {key: row[key] for key in ("status", "result", "error") if key in row}
            for name, row in self.record["phases"].items()
            if name in {"forecast", "pdf"} or name.startswith("email-")
        }
        fingerprint = hashlib.sha256(json.dumps(outcomes, sort_keys=True).encode()).hexdigest()
        self.record["maintenance"] = {"fingerprint": fingerprint}

        def verified_or_skipped_backup() -> dict[str, Any]:
            result = self.backup()
            if result.get("status") not in {"verified", "backup_skipped_low_space"}:
                raise DailyError("backup_not_verified; retention_skipped")
            return result

        backup = self.phase(f"backup-{fingerprint}", verified_or_skipped_backup)
        self.record["maintenance"]["backup"] = backup
        if backup.get("status") == "verified":
            retention = self.phase(f"retention-{fingerprint}", partial(self.retention, backup))
        else:
            # Cleanup needs a newly verified backup; the previous checkpoint stays as is.
            retention = {
                "status": "retention_skipped_no_new_verified_backup",
                "backup_status": backup.get("status"),
                "retained_backup": backup.get("retained_backup"),
            }
        self.record["maintenance"]["retention"] = retention
        self.save()

    def execute(self) -> dict[str, Any]:
        # Atomic receipts plus a persistent host lock complement, never replace, the
        # database issuance/advisory locks and immutable SMTP delivery intent.
        self.state_root.mkdir(parents=True, exist_ok=True)
        with single_writer(self.state_root) as acquired:
            if not acquired:
                raise DailyError("daily_cycle_already_running")
            self.prepare()
            identity = hashlib.sha256(
                json.dumps(
                    {"config": self.config, "location": self.location}, sort_keys=True
                ).encode()
            ).hexdigest()
            retained = read_json(self.receipt)
            legacy = self.state_root / self.day.isoformat() / "result.json"
            if retained is None and self.location_id == "minneapolis" and legacy.exists():
                raise DailyError("legacy_daily_receipt_requires_inspection; no_repeat_paid_work")
            if self.receipt.exists() and retained is None:
                raise DailyError("daily_receipt_unreadable; do_not_repeat_paid_work")
            self.record = retained or {
                "local_date": self.day.isoformat(),
                "location_id": self.location_id,
                "workflow_slot": self.workflow_slot,
                "config_digest": identity,
                "started_at": iso(self.clock()),
                "location": self.location,
                "phases": {},
                "status": "started",
            }
            if self.record.get("config_digest") != identity:
                raise DailyError("daily_configuration_changed; inspect_existing_receipt")
            if self.record["status"] == "completed":
                return {**self.record, "repeat": "completed_day_reused"}
            if self.record["status"] == "failed":
                previous = {
                    "retried_at": iso(self.clock()),
                    "phase": self.record.get("phase"),
                    "reason": self.record.get("reason"),
                }
                if "worker_failure" in self.record:
                    previous["worker_failure"] = self.record["worker_failure"]
                self.record.setdefault("previous_failures", []).append(previous)
            self.record.update(status="started", phase="preflight")
            for stale in ("reason", "worker_failure", "finished_at"):
                self.record.pop(stale, None)
            self.save()
            try:
                self.record["preflight"] = self.preflight()
                if "forecast" not in self.record["phases"]:
                    self.require_current_day()
                guidance = self.phase("guidance", self.guidance)
                baseline_id = guidance["baseline"]["baseline_snapshot_id"]
                if (self.record["phases"].get("forecast") or {}).get("status") != "completed":
                    self.record["phase"] = "wait_for_forecast_slot"
                    self.save()
                    self.wait_for_slot()
                    self.record["phase"] = "wait_for_analysis_window"
                    self.save()
                    self.record["analysis_window"] = self.wait_for_analysis_window()
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
                pdf = f"{self.runtime}/delivery/{self.location_id}-{self.day.isoformat()}.pdf"
                args = [
                    "--issued-id",
                    forecast["issued_forecast_id"],
                    "--location",
                    self.location_id,
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
                delivery_errors = {}
                for index, recipient in enumerate(self.recipients):
                    phase = f"email-{index}"
                    try:
                        self.phase(phase, partial(self.email, phase, args, recipient))
                    except Exception as exc:
                        # One recipient cannot prevent other configured recipients or
                        # backup of the valid issuance and delivery audit evidence.
                        delivery_errors[recipient] = (
                            str(exc) if isinstance(exc, DailyError) else type(exc).__name__
                        )
                self.record["delivery"] = {
                    "status": "partial"
                    if delivery_errors
                    else "completed"
                    if self.recipients
                    else "no_recipients",
                    "recipient_count": len(self.recipients),
                    "errors": delivery_errors,
                }
                self.maintenance()
                if delivery_errors:
                    raise DailyError("delivery_failed; valid_forecast_and_backup_preserved")
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
    parser.add_argument("--location", choices=DAILY_LOCATIONS, default="minneapolis")
    parser.add_argument("operation", choices=("run", "status"))
    args = parser.parse_args(argv)
    try:
        cycle = DailyCycle(load_config(args.config), location_id=args.location)
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
