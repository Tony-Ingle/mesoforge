"""Guidance/baseline worker: a bounded polling loop around existing background workflows.

Each poll decides only WHEN to run the existing ``refresh_guidance`` and
``build_baseline`` workflows; it contains no provider logic and no forecast science.
Decisions use facts those workflows already publish:

* refresh when no prepared state exists, when the current reference hour or the next
  ``coverage_margin_hours`` are no longer usable (``coverage_for``), when configured
  coordinates lie outside the prepared footprint, in the hour before a scheduled
  issuance slot, or when an hourly discovery probe (the existing ``select_model_set``)
  finds a newer cycle of a required contributor. Otherwise nothing is downloaded;
* build when the latest baseline is missing, pins an older prepared snapshot, lacks
  resolved blend governance, was revoked, pins different governed blend heads, or
  lacks a configured coordinate that the prepared snapshot covers. A build-input
  fingerprint is attempted once, so a deterministic failure cannot loop.

Existing publication rules keep the previous ``latest_complete``/``latest_baseline``
on any failure. Refresh attempts are bounded per UTC hour, back off exponentially and
survive restarts through the status file; an interrupted phase counts as a failure.
One process may own a runtime root. The worker never migrates schema, never
promotes, activates or rolls back policy and never issues forecasts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from mesoforge.application.baseline_readiness import baseline_readiness
from mesoforge.application.baseline_snapshot import current_manifest, load_baseline
from mesoforge.application.baseline_snapshot import read_pointer as read_baseline_pointer
from mesoforge.application.batch_forecast import (
    _coordinates,
    load_locations,
    location_display_timezone,
)
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.forecast_schedule import ForecastSchedule
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    coverage_for,
    derive_reference_time,
    resolve_latest_complete,
)
from mesoforge.application.runtime_log import event, redact
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.application.worker_status import (
    GUIDANCE_HEARTBEAT,
    GUIDANCE_STATUS,
    HEARTBEAT_SECONDS,
    POLL_BOUND_SECONDS,
    guidance_health,
    iso,
    parse_instant,
    phase_bound,
    read_json,
    status_directory,
    write_json,
)
from mesoforge.contracts.policy_governance import BLEND_POLICY, parse_scope
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.guidance.coverage import MAXIMUM_PREPARED_HOURS

_ROOT = Path(__file__).resolve().parents[3]
ROLE = "guidance-worker"
STATUS_SCHEMA = "mesoforge.guidance-worker-status.v1"
DEFAULT_CONFIG = _ROOT / "configs/locations.json"
# Only an unexpected poll error slows the loop; every external dependency keeps the
# normal cadence because refresh needs no database and recovery must be noticed.
BLOCKING_CATEGORIES = frozenset({"internal_error"})
POLL_BACKOFF_CAP_SECONDS = 900
# An interrupted build (watchdog, OOM, forced stop) may be retried this many times.
MAX_BUILD_INTERRUPTIONS = 2
_KEEP_HOURS = 48
_KEEP_FINGERPRINTS = 64
_KEEP_INTERRUPTIONS = 20


@dataclass(frozen=True)
class WorkerSettings:
    root: Path
    config: Path = DEFAULT_CONFIG
    interval_seconds: int = 300
    refresh: str = "auto"
    coverage_margin_hours: int = 2
    latest_start_minute: int = 40
    max_refresh_attempts_per_hour: int = 2
    backoff_base_seconds: int = 300
    backoff_cap_seconds: int = 3600
    min_free_bytes: int = 6 * 1024**3
    coverage_hours: int = MAXIMUM_PREPARED_HOURS
    # False refreshes only for coverage, footprint and pre-slot reasons (less bandwidth).
    hourly_probe: bool = True
    schedule: ForecastSchedule = field(default_factory=ForecastSchedule)

    def __post_init__(self) -> None:
        if self.refresh not in {"auto", "off"}:
            raise ValueError("refresh must be 'auto' or 'off'")
        if self.interval_seconds < 10:
            raise ValueError("Poll interval must be at least 10 seconds")
        if not 0 <= self.coverage_margin_hours <= 6:
            raise ValueError("Coverage margin must be 0..6 hours")
        if not 1 <= self.latest_start_minute <= 59:
            raise ValueError("Latest refresh start minute must be 1..59")
        if self.max_refresh_attempts_per_hour < 1:
            raise ValueError("At least one refresh attempt per hour is required")
        if self.backoff_cap_seconds < max(self.interval_seconds, 3600):
            raise ValueError("Backoff cap must be at least one hour and the poll interval")
        if self.backoff_base_seconds < 1 or self.min_free_bytes < 0:
            raise ValueError("Backoff base and free-space floor must be non-negative")

    def public(self) -> dict[str, Any]:
        values = asdict(self)
        values["root"], values["config"] = str(self.root), str(self.config)
        values["schedule"] = asdict(self.schedule)
        return values


@dataclass
class WorkerDeps:
    """Injectable effects; defaults call the existing workflows and services."""

    clock: Callable[[], datetime]
    schema: Callable[[], dict[str, Any]]
    governance: Callable[[], Any]
    discover: Callable[[Path], dict[str, Any]]
    refresh: Callable[[Path, Path, Path | None], dict[str, Any]]
    build: Callable[..., dict[str, Any]]
    disk_free: Callable[[Path], int]
    read_revision: Callable[[], str]
    monotonic: Callable[[], float] = time.perf_counter
    readiness: Callable[..., dict[str, Any]] = baseline_readiness


def backoff_seconds(failures: int, *, base: int, interval: int, cap: int) -> int:
    """Never sooner than the normal cadence, doubling per consecutive failure, capped."""
    if failures <= 0:
        return interval
    return int(min(cap, max(interval, base * 2 ** (failures - 1))))


def _hour_key(value: datetime) -> str:
    return iso(derive_reference_time(value))


def _error(exc: BaseException) -> str:
    return str(redact(f"{type(exc).__name__}: {exc}"))[:2000]


def _reuse_probe(
    source: Path, fallback: Callable[[Path], dict[str, Any]], clock: Callable[[], datetime]
) -> Callable[[Path], dict[str, Any]]:
    """Discovery step that reuses this hour's probe evidence instead of rediscovering.

    ``select_model_set`` retains relative evidence only, and preparation revalidates
    the selection (decision time, expiry and every inventory) before acquisition.
    """

    def discover(directory: Path) -> dict[str, Any]:
        try:
            report = json.loads((source / "selection.json").read_text(encoding="utf-8"))
            target = datetime.fromisoformat(report["target_reference_time"])
        except (OSError, ValueError, KeyError):
            return fallback(directory)
        if report.get("status") != "selected" or derive_reference_time(clock()) != target:
            return fallback(directory)
        shutil.copytree(source, directory)
        loaded: dict[str, Any] = json.loads((directory / "selection.json").read_text("utf-8"))
        return loaded

    return discover


def default_deps(settings: WorkerSettings) -> WorkerDeps:
    def clock() -> datetime:
        return datetime.now(UTC)

    def schema() -> dict[str, Any]:
        from mesoforge.storage.postgres.database import resolve_database_dsn
        from mesoforge.storage.postgres.schema import schema_status

        return schema_status(resolve_database_dsn("MESOFORGE_DATABASE_DSN"))

    def governance() -> Any:
        from mesoforge.application.governance import configured_governance

        return configured_governance()

    def discover(directory: Path) -> dict[str, Any]:
        from mesoforge.application.refresh_guidance import default_steps

        return default_steps(coverage_hours=settings.coverage_hours).discover(directory)

    def refresh(config: Path, guidance_root: Path, probe: Path | None) -> dict[str, Any]:
        from mesoforge.application.refresh_guidance import default_steps, refresh_guidance

        steps = default_steps(coverage_hours=settings.coverage_hours)
        if probe is not None:
            steps = replace(steps, discover=_reuse_probe(probe, steps.discover, clock))
        return refresh_guidance(
            config, guidance_root, coverage_hours=settings.coverage_hours, steps=steps
        )

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from mesoforge.application.build_baseline import build_baseline

        return build_baseline(*args, **kwargs)

    return WorkerDeps(
        clock=clock,
        schema=schema,
        governance=governance,
        discover=discover,
        refresh=refresh,
        build=build,
        disk_free=lambda path: shutil.disk_usage(path).free,
        read_revision=lambda: current_code_revision(_ROOT),
    )


def new_state() -> dict[str, Any]:
    return {
        "schema_version": STATUS_SCHEMA,
        "role": ROLE,
        "state": "starting",
        "in_flight": None,
        "interrupted": [],
        "polls": {"count": 0, "consecutive_failures": 0, "last": None},
        "refresh": {
            "attempts": {},
            "succeeded_hours": [],
            "probed_hours": [],
            "consecutive_failures": 0,
            "next_allowed_at": None,
            "last_attempt": None,
            "last_success_at": None,
            "last_probe": None,
            "last_successful_probe": None,
        },
        "build": {
            "fingerprints": {},
            "consecutive_failures": 0,
            "next_allowed_at": None,
            "last_attempt": None,
            "last_published": None,
        },
        "background": None,
        "governance": None,
        "guidance": None,
        "readiness": None,
        "next_slot": None,
        "disk": None,
        "next_poll_at": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    """Persisted counters and fingerprints, merged over defaults (never raises)."""
    state = new_state()
    saved = read_json(path)
    if saved is None or saved.get("schema_version") != STATUS_SCHEMA:
        return state
    for key, value in saved.items():
        if isinstance(state.get(key), dict) and isinstance(value, dict):
            state[key] = {**state[key], **value}
        else:
            state[key] = value
    return state


def _trim_hours(values: list[str]) -> list[str]:
    return sorted(set(values))[-_KEEP_HOURS:]


def _valid_locations(config: Path) -> tuple[list[Any], list[dict[str, Any]]]:
    valid, invalid = [], []
    for index, location in enumerate(load_locations(config)):
        try:
            validate_coordinate(*_coordinates(location))
            location_display_timezone(location)
        except ValueError as exc:
            invalid.append({"index": index, "reason": str(exc)})
        else:
            valid.append(location)
    return valid, invalid


def _prepared_coordinates(directory: Path) -> set[tuple[float, float]]:
    try:
        rows = json.loads((directory / "locations.json").read_text(encoding="utf-8"))
        return {_coordinates(row) for row in rows["locations"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


@contextmanager
def single_writer(root: Path) -> Iterator[bool]:
    """Hold an exclusive, non-blocking lock on the runtime root for this process."""
    directory = status_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "guidance-worker.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class GuidanceWorker:
    def __init__(
        self,
        settings: WorkerSettings,
        deps: WorkerDeps,
        *,
        stream: TextIO | None = None,
    ) -> None:
        root = settings.root.resolve()
        if root.is_relative_to(_ROOT):
            raise ValueError("The runtime root must remain outside the repository")
        self.settings = replace(settings, root=root)
        self.deps = deps
        self.stream = stream
        self.status_path = status_directory(root) / GUIDANCE_STATUS
        self.heartbeat_path = status_directory(root) / GUIDANCE_HEARTBEAT
        self.guidance_root = root / "guidance"
        self.baseline_root = root / "baseline"
        self.probe_root = root / "discovery" / "worker"
        self.state = load_state(self.status_path)
        self.stop = threading.Event()
        self.in_flight: dict[str, Any] | None = None
        self.poll_started_at: str | None = None
        self.code_revision: str | None = None
        self.heartbeat_seconds: float = HEARTBEAT_SECONDS
        self._signals = 0

    # ------------------------------------------------------------------ plumbing
    def log(self, name: str, **fields: Any) -> dict[str, Any]:
        return event(ROLE, name, stream=self.stream, clock=self.deps.clock, **fields)

    def save(self) -> None:
        self.state["updated_at"] = iso(self.deps.clock())
        write_json(self.status_path, self.state)

    def beat(self) -> None:
        """Best effort: a reader holding the file open must not stop the worker."""
        try:
            self._beat()
        except OSError:
            pass

    def _beat(self) -> None:
        write_json(
            self.heartbeat_path,
            {
                "heartbeat_at": iso(self.deps.clock()),
                "pid": os.getpid(),
                "state": self.state.get("state"),
                "in_flight": self.in_flight,
                "poll_started_at": self.poll_started_at,
                "next_poll_at": self.state.get("next_poll_at"),
            },
        )

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        self.in_flight = {"phase": name, "started_at": iso(self.deps.clock())}
        self.state.update(in_flight=self.in_flight, state="busy")
        self.save()
        self.beat()
        try:
            yield
        except Exception:
            self._finish_phase()
            raise
        except BaseException:
            # SystemExit/KeyboardInterrupt abandon the phase: keep it recorded as in
            # flight so the next start counts it as interrupted.
            raise
        else:
            self._finish_phase()

    def _finish_phase(self) -> None:
        self.in_flight = None
        self.state["in_flight"] = None
        self.save()
        self.beat()

    def recover_interrupted(self) -> dict[str, Any] | None:
        """An unfinished phase from a previous process counts as that phase's failure."""
        interrupted = self.state.get("in_flight")
        if not isinstance(interrupted, dict):
            return None
        now = self.deps.clock()
        record = {**interrupted, "detected_at": iso(now), "outcome": "interrupted"}
        self.state["interrupted"] = [*self.state["interrupted"], record][-_KEEP_INTERRUPTIONS:]
        self.state["in_flight"] = None
        phase = interrupted.get("phase")
        if phase in {"refresh", "build"}:
            info = self.state[phase]
            info["consecutive_failures"] = int(info["consecutive_failures"]) + 1
            info["next_allowed_at"] = iso(now + timedelta(seconds=self._backoff(info)))
            info["last_attempt"] = {"status": "interrupted", **record}
        for attempt in self.state["build"]["fingerprints"].values():
            if attempt.get("outcome") == "in_flight":
                attempt.update(
                    outcome="interrupted",
                    interruptions=int(attempt.get("interruptions", 0)) + 1,
                )
        self.log("phase_interrupted", **record)
        return record

    def _backoff(self, info: dict[str, Any]) -> int:
        s = self.settings
        return backoff_seconds(
            int(info["consecutive_failures"]),
            base=s.backoff_base_seconds,
            interval=s.interval_seconds,
            cap=s.backoff_cap_seconds,
        )

    # ------------------------------------------------------------------ poll
    def poll_once(self) -> dict[str, Any]:
        d = self.deps
        started, timer = d.clock(), d.monotonic()
        polls = self.state["polls"]
        polls["count"] = int(polls["count"]) + 1
        outcome: dict[str, Any] = {"poll": polls["count"], "started_at": iso(started)}
        self.poll_started_at = outcome["started_at"]
        categories: list[str] = []
        outcome["categories"] = categories
        self.log("poll_started", poll=polls["count"])
        try:
            self._poll(outcome, categories)
        except Exception as exc:
            categories.append("internal_error")
            outcome["error"] = _error(exc)
            self.log("poll_crashed", error=outcome["error"])
        outcome["seconds"] = round(d.monotonic() - timer, 3)
        outcome["finished_at"] = iso(d.clock())
        self.poll_started_at = None
        blocking = sorted(set(categories) & BLOCKING_CATEGORIES)
        polls["consecutive_failures"] = int(polls["consecutive_failures"]) + 1 if blocking else 0
        polls["last"] = outcome
        self.save()
        self.log(
            "poll_finished",
            poll=polls["count"],
            categories=categories,
            refresh=(outcome.get("refresh") or {}).get("decision"),
            build=(outcome.get("build") or {}).get("decision"),
            ready=(self.state.get("readiness") or {}).get("ready"),
            seconds=outcome["seconds"],
        )
        return outcome

    def _poll(self, outcome: dict[str, Any], categories: list[str]) -> None:
        d = self.deps
        self._clear_probes()
        locations, invalid = _valid_locations(self.settings.config)
        outcome["locations"] = {"valid": len(locations), "invalid": invalid}
        database = True
        with self.phase("schema"):
            try:
                schema = d.schema()
            except Exception as exc:
                database = False
                categories.append("database_unavailable")
                outcome["schema"] = {"status": "database_unavailable", "error": _error(exc)}
            else:
                outcome["schema"] = schema
                if not schema.get("at_head"):
                    categories.append("schema_not_at_head")
                    self.log("schema_not_at_head", **schema)
                    return
        try:
            free: int | None = d.disk_free(self.settings.root)
        except OSError:
            free = None
        self.state["disk"] = {"free_bytes": free, "min_free_bytes": self.settings.min_free_bytes}
        outcome["refresh"] = self._refresh_step(locations, free, categories)
        if self._stopping(outcome):
            return
        governance, snapshot = None, None
        if database:
            with self.phase("governance"):
                try:
                    governance = d.governance()
                    snapshot = governance.snapshot(BLEND_POLICY, None, d.clock())
                except Exception as exc:
                    categories.append("governance_unavailable")
                    outcome["governance"] = {"status": "unavailable", "error": _error(exc)}
                    governance = None
            if governance is not None and snapshot is not None:
                outcome["governance"] = {
                    "status": "resolved",
                    "blend_heads": {
                        parse_scope(key)["field"]: str(scope.head_event_id)
                        for key, scope in snapshot.scopes.items()
                        if scope.head_event_id is not None
                    },
                    "registered_blend_candidates": sum(
                        len(scope.shadows) for scope in snapshot.scopes.values()
                    ),
                }
            self.state["governance"] = outcome["governance"]
        if governance is None or snapshot is None:
            outcome["build"] = {
                "decision": "deferred",
                "blocked_by": "database_unavailable" if not database else "governance_unavailable",
            }
        else:
            outcome["build"] = self._build_step(governance, snapshot, locations, categories)
            if self._stopping(outcome):
                return
            candidates = [row for scope in snapshot.scopes.values() for row in scope.shadows]
            outcome["background"] = self._background_step(governance, candidates, categories)
            self.state["background"] = outcome["background"]
        if self._stopping(outcome):
            return
        with self.phase("readiness"):
            self._summarize(locations, governance)

    def _stopping(self, outcome: dict[str, Any]) -> bool:
        """After a stop request the poll ends at the next phase boundary."""
        if self.stop.is_set():
            outcome["stopped_early"] = "stop_requested"
            return True
        return False

    # ------------------------------------------------------------------ refresh
    def _prepared(self) -> tuple[dict[str, Any], dict[str, Any], Path] | None:
        try:
            return resolve_latest_complete(self.guidance_root)
        except (SnapshotError, OSError, ValueError, KeyError):
            return None

    def _usable_ahead(self, manifest: dict[str, Any], current: datetime) -> int:
        """-1 when the current hour is unusable, else consecutive usable later hours."""
        try:
            if not coverage_for(manifest, current)["usable"]:
                return -1
            ahead = 0
            for hour in range(1, 7):
                if not coverage_for(manifest, current + timedelta(hours=hour))["usable"]:
                    break
                ahead = hour
            return ahead
        except (KeyError, ValueError, TypeError):
            return -1

    def refresh_reasons(self, locations: list[Any], now: datetime) -> list[str]:
        prepared = self._prepared()
        if prepared is None:
            return ["no_prepared_state"]
        _, manifest, directory = prepared
        current = derive_reference_time(now)
        reasons = []
        ahead = self._usable_ahead(manifest, current)
        if ahead < 0:
            reasons.append("current_hour_not_covered")
        elif ahead < self.settings.coverage_margin_hours:
            reasons.append("coverage_expiring")
        covered = _prepared_coordinates(directory)
        if any(_coordinates(location) not in covered for location in locations):
            reasons.append("configured_locations_changed")
        slot_hour = derive_reference_time(self.settings.schedule.next_run(now))
        prepared_reference = datetime.fromisoformat(manifest["coverage"]["reference_time"])
        if slot_hour - current == timedelta(hours=1) and prepared_reference < current:
            reasons.append("pre_slot_refresh")
        return reasons

    def refresh_gate(self, now: datetime, free: int | None) -> str | None:
        s, info = self.settings, self.state["refresh"]
        hour = _hour_key(now)
        allowed = parse_instant(info.get("next_allowed_at"))
        if s.refresh == "off":
            return "refresh_disabled"
        if free is not None and free < s.min_free_bytes:
            return "disk_low"
        if hour in info["succeeded_hours"]:
            return "refreshed_this_hour"
        if int(info["attempts"].get(hour, 0)) >= s.max_refresh_attempts_per_hour:
            return "hour_attempt_budget_exhausted"
        if allowed is not None and now < allowed:
            return "backoff"
        if now.minute >= s.latest_start_minute:
            return "too_late_in_hour"
        return None

    def _refresh_step(
        self, locations: list[Any], free: int | None, categories: list[str]
    ) -> dict[str, Any]:
        now = self.deps.clock()
        reasons = self.refresh_reasons(locations, now)
        blocked = self.refresh_gate(now, free)
        if blocked is not None:
            if blocked == "disk_low" and reasons:
                categories.append("disk_low")
            return {
                "decision": "deferred" if reasons else "none",
                "reasons": reasons,
                "blocked_by": blocked,
            }
        probe_selection = None
        if not reasons and not self.settings.hourly_probe:
            return {"decision": "none", "reasons": [], "probe": "disabled"}
        if not reasons:
            info = self.state["refresh"]
            hour = _hour_key(now)
            if hour in info["probed_hours"]:
                return {"decision": "none", "reasons": [], "probe": "already_probed_this_hour"}
            # Recorded before probing: a probe the watchdog ends is not repeated.
            info["probed_hours"] = _trim_hours([*info["probed_hours"], hour])
            self.save()
            probe = self._probe()
            if probe["status"] != "selected":
                categories.append(
                    "provider_unavailable" if probe["status"] == "failed" else "provider_incomplete"
                )
                return {"decision": "none", "reasons": [], "probe": probe}
            reasons = [f"newer_required_cycle:{model}" for model in probe["newer_required_cycles"]]
            if not reasons:
                return {"decision": "no_material_change", "reasons": [], "probe": probe}
            probe_selection = Path(probe.pop("selection_directory"))
        return self._run_refresh(reasons, locations, probe_selection, categories)

    def _probe(self) -> dict[str, Any]:
        d = self.deps
        now = d.clock()
        directory = self.probe_root / f"{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        timer = d.monotonic()
        summary: dict[str, Any] = {"at": iso(now)}
        with self.phase("probe"):
            try:
                selection = d.discover(directory / "selection")
            except Exception as exc:
                summary.update(status="failed", error=_error(exc))
                selection = None
        summary["seconds"] = round(d.monotonic() - timer, 3)
        if selection is not None:
            summary.update(
                status=selection.get("status"),
                target_reference_time=selection.get("target_reference_time"),
                selected_cycles=selection.get("selected_cycles", {}),
                shadow_discovery_shortfalls=sorted(
                    selection.get("shadow_discovery_shortfalls") or {}
                ),
                reason=selection.get("reason"),
            )
        if summary["status"] == "selected":
            prepared = self._prepared()
            manifest = prepared[1] if prepared is not None else {}
            required = (
                manifest.get("coverage", {})
                .get("usability_rule", {})
                .get("required_complete", ["HRRR", "GFS"])
            )
            newer = []
            for model in required:
                selected = summary["selected_cycles"].get(model)
                held = manifest.get("contributors", {}).get(model, {}).get("cycle")
                if selected and (
                    held is None or datetime.fromisoformat(selected) > datetime.fromisoformat(held)
                ):
                    newer.append(model)
            summary["newer_required_cycles"] = newer
            summary["selection_directory"] = str(directory / "selection")
        self.state["refresh"]["last_probe"] = {
            key: value for key, value in summary.items() if key != "selection_directory"
        }
        if summary["status"] == "selected":
            self.state["refresh"]["last_successful_probe"] = self.state["refresh"]["last_probe"]
        self.log("probe_finished", **self.state["refresh"]["last_probe"])
        return summary

    def _clear_probes(self) -> None:
        """The worker's own probe scratch only; a refresh keeps its own copy of evidence.

        Manual ``discover --keep`` evidence lives beside it and is never removed here.
        """
        if self.probe_root.is_dir():
            for child in self.probe_root.iterdir():
                shutil.rmtree(child, ignore_errors=True)

    def _run_refresh(
        self,
        reasons: list[str],
        locations: list[Any],
        probe_selection: Path | None,
        categories: list[str],
    ) -> dict[str, Any]:
        d, info = self.deps, self.state["refresh"]
        now = d.clock()
        hour = _hour_key(now)
        info["attempts"] = dict(
            sorted({**info["attempts"], hour: int(info["attempts"].get(hour, 0)) + 1}.items())[
                -_KEEP_HOURS:
            ]
        )
        config = status_directory(self.settings.root) / "refresh-locations.json"
        write_json(config, {"locations": locations})
        self.log("refresh_started", reasons=reasons, reused_probe=probe_selection is not None)
        timer = d.monotonic()
        with self.phase("refresh"):
            try:
                result = d.refresh(config, self.guidance_root, probe_selection)
            except Exception as exc:
                result = {"status": "failed", "error": _error(exc), "steps": []}
        seconds = round(d.monotonic() - timer, 3)
        steps = [
            {"step": row.get("step"), "status": row.get("status"), "seconds": row.get("seconds")}
            for row in result.get("steps", [])
        ]
        if result.get("status") == "published":
            selection = result.get("selection") or {}
            summary: dict[str, Any] = {
                "status": "published",
                "at": iso(d.clock()),
                "snapshot_id": result.get("snapshot_id"),
                "reference_time": (result.get("latest_complete") or {}).get("reference_time"),
                "selected_cycles": selection.get("selected_cycles"),
                "shadow_discovery_shortfalls": sorted(
                    selection.get("shadow_discovery_shortfalls") or {}
                ),
                "downloaded_bytes": result.get("downloaded_bytes"),
                "seconds": seconds,
                "steps": steps,
            }
            info.update(consecutive_failures=0, next_allowed_at=None, last_success_at=summary["at"])
            info["succeeded_hours"] = _trim_hours([*info["succeeded_hours"], hour])
            self.log("refresh_published", **{k: v for k, v in summary.items() if k != "steps"})
        else:
            failed = next((row["step"] for row in steps if row["status"] == "error"), None)
            if failed is None and steps and steps[-1]["step"] == "discover":
                # Discovery returned no complete model set, so the refresh stopped there.
                failed = "discover"
            category = "provider_unavailable" if failed == "discover" else "guidance_refresh_failed"
            categories.append(category)
            info["consecutive_failures"] = int(info["consecutive_failures"]) + 1
            delay = self._backoff(info)
            info["next_allowed_at"] = iso(d.clock() + timedelta(seconds=delay))
            summary = {
                "status": "failed",
                "at": iso(d.clock()),
                "category": category,
                "failed_step": failed,
                "error": str(redact(str(result.get("error", "refresh did not publish"))))[:2000],
                "snapshot_id": result.get("snapshot_id"),
                "latest_complete_unchanged": result.get("latest_complete_unchanged"),
                "retry_after_seconds": delay,
                "seconds": seconds,
                "steps": steps,
            }
            self.log("refresh_failed", **{k: v for k, v in summary.items() if k != "steps"})
        info["last_attempt"] = summary
        return {"decision": "refresh", "reasons": reasons, **summary}

    # ------------------------------------------------------------------ build
    def build_inputs(
        self, snapshot: Any, locations: list[Any]
    ) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, str], str] | None:
        prepared = self._prepared()
        if prepared is None:
            return None
        pointer, manifest, directory = prepared
        heads = {
            parse_scope(key)["field"]: str(scope.head_event_id)
            for key, scope in snapshot.scopes.items()
            if scope.head_event_id is not None
        }
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "prepared_snapshot_id": pointer["snapshot_id"],
                    "prepared_manifest_sha256": pointer["manifest_sha256"],
                    "coordinates": sorted(_coordinates(row) for row in locations),
                    "code_revision": self.code_revision,
                    "blend_heads": heads,
                }
            )
        ).hexdigest()
        return pointer, manifest, directory, heads, digest

    def build_reasons(
        self,
        governance: Any,
        prepared_pointer: dict[str, Any],
        prepared_directory: Path,
        heads: dict[str, str],
        locations: list[Any],
    ) -> list[str]:
        try:
            baseline = current_manifest(self.baseline_root)
        except (SnapshotError, OSError, ValueError, KeyError):
            return ["baseline_unreadable"]
        if baseline is None:
            return ["no_baseline"]
        reasons = []
        if baseline["prepared_snapshot"]["snapshot_id"] != prepared_pointer["snapshot_id"]:
            reasons.append("new_prepared_snapshot")
        blend = baseline.get("blend_governance")
        if not isinstance(blend, dict) or blend.get("status") != "resolved":
            reasons.append("baseline_governance_unproven")
        else:
            if governance.blend_revoked(blend):
                reasons.append("blend_revoked")
            pinned = {
                target: str(row["head_event_id"])
                for target, row in blend.get("heads", {}).items()
                if row.get("head_event_id")
            }
            if pinned != heads:
                reasons.append("blend_policy_changed")
        prepared = _prepared_coordinates(prepared_directory)
        domains = {(row["latitude"], row["longitude"]) for row in baseline.get("domains", [])}
        failed = {
            (row["latitude"], row["longitude"])
            for row in baseline.get("coverage", {}).get("failed_locations", [])
            if "latitude" in row and "longitude" in row
        }
        for location in locations:
            coordinate = _coordinates(location)
            if coordinate in prepared and coordinate not in domains | failed:
                reasons.append("configured_location_missing")
                break
        return reasons

    def _build_step(
        self, governance: Any, snapshot: Any, locations: list[Any], categories: list[str]
    ) -> dict[str, Any]:
        d, info = self.deps, self.state["build"]
        inputs = self.build_inputs(snapshot, locations)
        if inputs is None:
            return {"decision": "deferred", "blocked_by": "no_prepared_state"}
        if not locations:
            return {"decision": "deferred", "blocked_by": "no_valid_configured_location"}
        pointer, _, directory, heads, fingerprint = inputs
        with self.phase("governance"):
            reasons = self.build_reasons(governance, pointer, directory, heads, locations)
        if not reasons:
            return {"decision": "current", "fingerprint": fingerprint}
        previous = info["fingerprints"].get(fingerprint)
        retry = (
            previous is None
            or previous.get("outcome") == "retryable_failure"
            or (
                previous.get("outcome") == "interrupted"
                and int(previous.get("interruptions", 0)) < MAX_BUILD_INTERRUPTIONS
            )
        )
        if not retry and previous is not None:
            return {
                "decision": "skipped",
                "reasons": reasons,
                "blocked_by": "fingerprint_already_attempted",
                "fingerprint": fingerprint,
                "previous": previous,
            }
        allowed = parse_instant(info.get("next_allowed_at"))
        if allowed is not None and d.clock() < allowed:
            return {"decision": "deferred", "reasons": reasons, "blocked_by": "backoff"}
        self.log("build_started", reasons=reasons, prepared_snapshot_id=pointer["snapshot_id"])
        timer = d.monotonic()
        outcome: dict[str, Any]
        interruptions = int((previous or {}).get("interruptions", 0))
        # Marked before the phase starts, so a killed build is known at the next start.
        info["fingerprints"] = dict(
            [
                *[(k, v) for k, v in info["fingerprints"].items() if k != fingerprint],
                (
                    fingerprint,
                    {"outcome": "in_flight", "interruptions": interruptions, "at": iso(d.clock())},
                ),
            ][-_KEEP_FINGERPRINTS:]
        )
        with self.phase("build"):
            try:
                built = d.build(
                    self.guidance_root,
                    self.baseline_root,
                    locations,
                    prepared_pointer=pointer,
                    governance=governance,
                )
            except (SnapshotError, ValueError, KeyError) as exc:
                outcome = {
                    "outcome": "failed",
                    "category": "baseline_build_failed",
                    "error": _error(exc),
                }
            except Exception as exc:
                outcome = {
                    "outcome": "retryable_failure",
                    "category": "baseline_build_failed",
                    "error": _error(exc),
                }
            else:
                failed = built["manifest"]["coverage"].get("failed_locations", [])
                outcome = {
                    "outcome": "published_with_failures" if failed else "published",
                    "baseline_snapshot_id": built["baseline_snapshot_id"],
                    "reference_times": built["manifest"]["coverage"]["reference_times"],
                    "failed_locations": len(failed),
                    "artifact_bytes": built.get("artifact_bytes"),
                    "timings": built.get("timings"),
                }
        outcome.update(
            at=iso(d.clock()),
            seconds=round(d.monotonic() - timer, 3),
            prepared_snapshot_id=pointer["snapshot_id"],
        )
        info["fingerprints"] = dict(
            [
                *[(k, v) for k, v in info["fingerprints"].items() if k != fingerprint],
                (
                    fingerprint,
                    {
                        **{
                            k: v
                            for k, v in outcome.items()
                            if k not in {"timings", "reference_times"}
                        },
                        "interruptions": interruptions,
                    },
                ),
            ][-_KEEP_FINGERPRINTS:]
        )
        if outcome["outcome"].startswith("published"):
            info.update(consecutive_failures=0, next_allowed_at=None)
            info["last_published"] = {
                k: v for k, v in outcome.items() if k not in {"timings", "reference_times"}
            }
            self.log("baseline_published", **{k: v for k, v in outcome.items() if k != "timings"})
        else:
            categories.append(outcome["category"])
            info["consecutive_failures"] = int(info["consecutive_failures"]) + 1
            if outcome["outcome"] == "retryable_failure":
                info["next_allowed_at"] = iso(d.clock() + timedelta(seconds=self._backoff(info)))
            self.log("baseline_build_failed", **outcome)
        info["last_attempt"] = outcome
        return {"decision": "build", "reasons": reasons, "fingerprint": fingerprint, **outcome}

    # ------------------------------------------------------------------ background
    def _background_step(
        self, governance: Any, candidates: list[Any], categories: list[str]
    ) -> dict[str, Any]:
        """Registered blend candidates shadow the current baseline; find() makes it idempotent."""
        if not candidates:
            return {"status": "no_registered_candidates"}
        try:
            pointer = read_baseline_pointer(self.baseline_root)
        except (SnapshotError, OSError, ValueError):
            pointer = None
        if pointer is None:
            return {"status": "no_baseline", "registered_candidates": len(candidates)}
        with self.phase("background"):
            try:
                pinned = load_baseline(self.baseline_root, pointer=pointer)
                report = governance.learning.background(
                    pinned, candidates, analysis_cutoff=self.deps.clock()
                )
            except Exception as exc:
                categories.append("candidate_background_failed")
                return {"status": "failed", "error": _error(exc)}
        failures = report.get("failures", [])
        if failures:
            categories.append("candidate_background_failed")
        return {
            "status": "partial" if failures else "ready",
            "baseline_snapshot_id": pointer["baseline_snapshot_id"],
            "registered_candidates": len(candidates),
            "overlays": len(report.get("overlays", [])),
            "built": len(report.get("measurements", [])),
            "failures": [str(redact(str(row)))[:500] for row in failures],
        }

    # ------------------------------------------------------------------ summary
    def _summarize(self, locations: list[Any], governance: Any | None) -> None:
        now = self.deps.clock()
        prepared = self._prepared()
        if prepared is not None:
            pointer, manifest, _ = prepared
            current = derive_reference_time(now)
            self.state["guidance"] = {
                "contributor_state_id": pointer["snapshot_id"],
                "reference_time": pointer["reference_time"],
                "published_at": pointer["published_at"],
                "contributor_cycles": {
                    model: row.get("cycle")
                    for model, row in manifest.get("contributors", {}).items()
                    if "cycle" in row
                },
                "covers_current_hour": self._usable_ahead(manifest, current) >= 0,
                "usable_hours_ahead": max(self._usable_ahead(manifest, current), 0),
            }
        else:
            self.state["guidance"] = None
        readiness = self.deps.readiness(
            self.baseline_root, locations, now=now, governance=governance
        )
        readiness.pop("pointer", None)
        self.state["readiness"] = readiness
        slot = self.settings.schedule.next_run(now)
        slot_reference = derive_reference_time(slot)
        views = (readiness.get("baseline") or {}).get("reference_times") or {}
        covered = False
        if views.get("first") and views.get("last"):
            covered = (
                datetime.fromisoformat(views["first"])
                <= slot_reference
                <= datetime.fromisoformat(views["last"])
            )
        self.state["next_slot"] = {
            **self.settings.schedule.describe(now),
            "reference_time": iso(slot_reference),
            "covered_by_latest_baseline": covered,
        }

    # ------------------------------------------------------------------ loop
    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self._signals += 1
        if self._signals > 1:
            # A second signal abandons the in-flight phase; the next start records it.
            raise SystemExit(128 + signum)
        self.log("stop_requested", signal=signal.Signals(signum).name, in_flight=self.in_flight)
        self.stop.set()

    def _watchdog(self, done: threading.Event, exit_process: Callable[[int], None]) -> None:
        """Heartbeat while busy; exit when a phase exceeds its bound so restart applies."""
        while not done.wait(self.heartbeat_seconds):
            try:
                self.beat()
            except OSError:
                pass
            now = self.deps.clock()
            poll_started = parse_instant(self.poll_started_at)
            if (
                poll_started is not None
                and (now - poll_started).total_seconds() > POLL_BOUND_SECONDS
            ):
                self.log("watchdog_exit", poll_started_at=self.poll_started_at)
                exit_process(70)
                return
            current = self.in_flight
            if current is None:
                continue
            started = parse_instant(current.get("started_at"))
            if started is None:
                continue
            age = (now - started).total_seconds()
            if age > phase_bound(current.get("phase")):
                self.log("watchdog_exit", in_flight=current, age_seconds=age)
                exit_process(70)
                return

    def run(
        self,
        *,
        once: bool = False,
        install_signals: bool = True,
        exit_process: Callable[[int], None] = os._exit,
    ) -> int:
        with single_writer(self.settings.root) as owned:
            if not owned:
                self.log("worker_busy", root=str(self.settings.root))
                return 2
            try:
                self.code_revision = self.deps.read_revision()
            except Exception as exc:
                self.log("code_revision_unavailable", error=_error(exc))
                return 2
            self.recover_interrupted()
            self.state.update(
                state="starting",
                pid=os.getpid(),
                hostname=socket.gethostname(),
                started_at=iso(self.deps.clock()),
                code_revision=self.code_revision,
                settings=self.settings.public(),
            )
            self.save()
            self.beat()
            self.log(
                "worker_started",
                once=once,
                code_revision=self.code_revision,
                settings=self.settings.public(),
            )
            if install_signals and threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGTERM, self._handle_signal)
                signal.signal(signal.SIGINT, self._handle_signal)
                # Windows consoles deliver a targeted stop as CTRL_BREAK (SIGBREAK).
                console_break = getattr(signal, "SIGBREAK", None)
                if console_break is not None:
                    signal.signal(console_break, self._handle_signal)
            done = threading.Event()
            watchdog = None
            if not once:
                watchdog = threading.Thread(
                    target=self._watchdog, args=(done, exit_process), daemon=True
                )
                watchdog.start()
            status = 0
            try:
                while not self.stop.is_set():
                    outcome = self.poll_once()
                    if once:
                        failures = set(outcome["categories"]) - {"provider_incomplete"}
                        status = 1 if failures else 0
                        break
                    delay = backoff_seconds(
                        int(self.state["polls"]["consecutive_failures"]),
                        base=self.settings.backoff_base_seconds,
                        interval=self.settings.interval_seconds,
                        cap=max(self.settings.interval_seconds, POLL_BACKOFF_CAP_SECONDS),
                    )
                    self.state.update(
                        state="sleeping",
                        next_poll_at=iso(self.deps.clock() + timedelta(seconds=delay)),
                    )
                    self.save()
                    self._sleep(delay)
            finally:
                done.set()
                if watchdog is not None:
                    watchdog.join(timeout=5)
                self.state.update(state="stopped", stopped_at=iso(self.deps.clock()))
                self.save()
                self.beat()
                self.log("worker_stopped", polls=self.state["polls"]["count"])
            return status

    def _sleep(self, seconds: float) -> None:
        deadline = self.deps.monotonic() + seconds
        while not self.stop.is_set():
            remaining = deadline - self.deps.monotonic()
            if remaining <= 0:
                return
            self.stop.wait(min(remaining, self.heartbeat_seconds))
            self.beat()


def discover_once(
    settings: WorkerSettings, deps: WorkerDeps, *, keep: bool = False
) -> dict[str, Any]:
    """Read-only provider availability probe (metadata only; no model data is acquired)."""
    root = settings.root.resolve()
    directory = root / "discovery" / f"manual-{deps.clock():%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    # Never under discovery/worker, which the running worker clears every poll.
    directory.mkdir(parents=True, exist_ok=False)
    timer = deps.monotonic()
    try:
        selection = deps.discover(directory / "selection")
        result = {
            "status": selection.get("status"),
            "target_reference_time": selection.get("target_reference_time"),
            "selected_cycles": selection.get("selected_cycles", {}),
            "shadow_discovery_shortfalls": sorted(
                selection.get("shadow_discovery_shortfalls") or {}
            ),
            "downloaded_metadata_bytes": selection.get("downloaded_metadata_bytes"),
            "reason": selection.get("reason"),
        }
    except Exception as exc:
        result = {"status": "failed", "error": _error(exc)}
    result["seconds"] = round(deps.monotonic() - timer, 3)
    if keep:
        result["evidence"] = str(directory)
    else:
        shutil.rmtree(directory, ignore_errors=True)
    return result


def _default_root() -> Path | None:
    value = os.environ.get("MESOFORGE_PROSPECTIVE_ROOT")
    return Path(value) if value else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "once", "status", "health", "discover"))
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument(
        "--refresh",
        choices=("auto", "off"),
        default="auto",
        help="'off' builds from the current latest_complete only (recovery; no downloads)",
    )
    parser.add_argument("--coverage-margin-hours", type=int, default=2)
    parser.add_argument("--latest-start-minute", type=int, default=40)
    parser.add_argument("--max-refresh-attempts-per-hour", type=int, default=2)
    parser.add_argument("--backoff-base-seconds", type=int, default=300)
    parser.add_argument("--backoff-cap-seconds", type=int, default=3600)
    parser.add_argument("--min-free-gb", type=float, default=6.0)
    parser.add_argument(
        "--no-hourly-probe",
        action="store_true",
        help="Refresh only for coverage, footprint and pre-slot reasons (saves bandwidth)",
    )
    parser.add_argument("--keep", action="store_true", help="discover: retain probe evidence")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.root is None:
        print(
            json.dumps({"error": "--root or MESOFORGE_PROSPECTIVE_ROOT is required"}),
            file=sys.stderr,
        )
        return 2
    if args.command == "health":
        health = guidance_health(args.root)
        print(json.dumps(health, indent=2))
        return 0 if health["healthy"] else 1
    if args.command == "status":
        status = read_json(status_directory(args.root) / GUIDANCE_STATUS)
        print(json.dumps(status or {"state": "no_status"}, indent=2, default=str))
        return 0 if status is not None else 1
    try:
        settings = WorkerSettings(
            root=args.root,
            config=args.config,
            interval_seconds=args.interval_seconds,
            refresh=args.refresh,
            coverage_margin_hours=args.coverage_margin_hours,
            latest_start_minute=args.latest_start_minute,
            max_refresh_attempts_per_hour=args.max_refresh_attempts_per_hour,
            backoff_base_seconds=args.backoff_base_seconds,
            backoff_cap_seconds=args.backoff_cap_seconds,
            min_free_bytes=int(args.min_free_gb * 1024**3),
            hourly_probe=not args.no_hourly_probe,
            schedule=ForecastSchedule.from_environment(),
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    deps = default_deps(settings)
    if args.command == "discover":
        result = discover_once(settings, deps, keep=args.keep)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] == "selected" else 1
    try:
        worker = GuidanceWorker(settings, deps)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return worker.run(once=args.command == "once")


if __name__ == "__main__":
    raise SystemExit(main())
