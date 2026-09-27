"""Forecast/issuance worker: configured locations from one pinned, published baseline.

A production entry point composed from existing boundaries only:

    configured locations -> schedule gate (``--scheduled``) -> one run at a time
    -> schema at head -> storage and governance readable -> pin ONE ready baseline
    -> lookup-only candidate overlays -> ``forecast_from_baseline`` (correction, AI
    desk, validation, presentation, issuance, prior verification per location)

It never refreshes guidance, builds a baseline, blends fields, migrates schema or
changes governance. A missing or unready baseline fails clearly (exit 3); with
``--scheduled`` readiness is rechecked until the slot window closes, because the
existing locked issuance lookup turns every repeated trigger into a skip, never a
second issuance. One location's failure never stops the next.

Exit codes: 0 completed (issued or already issued) or not due; 1 partial;
2 infrastructure, configuration or governance failure; 3 no ready baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from mesoforge.application.baseline_readiness import (
    baseline_readiness,
    mark_governance_unavailable,
)
from mesoforge.application.baseline_snapshot import load_baseline
from mesoforge.application.baseline_snapshot import read_pointer as read_baseline_pointer
from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.forecast_schedule import ForecastSchedule
from mesoforge.application.prepared_snapshot import SnapshotError, derive_reference_time
from mesoforge.application.runtime_log import event, redact
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.application.worker_status import (
    FORECAST_STATUS,
    iso,
    status_directory,
    write_json,
)
from mesoforge.common.identifiers import Digest
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy

_ROOT = Path(__file__).resolve().parents[3]
ROLE = "forecast-worker"
RUN_SCHEMA = "mesoforge.forecast-worker-run.v1"
DEFAULT_CONFIG = _ROOT / "configs/locations.json"
# Distinct from the per-location issuance lock, which delivery takes on its own
# connections; sharing that key would self-deadlock.
FORECAST_RUN_LOCK = Digest.of_bytes(b"mesoforge.forecast-worker-run.v1")
EXIT_OK, EXIT_PARTIAL, EXIT_FAILED, EXIT_NOT_READY = 0, 1, 2, 3


@dataclass(frozen=True)
class ForecastSettings:
    root: Path
    config: Path = DEFAULT_CONFIG
    scheduled: bool = False
    reference_time: datetime | None = None
    verify_prior: bool = True
    readiness_poll_seconds: int = 60
    schedule: ForecastSchedule = field(default_factory=ForecastSchedule)

    def __post_init__(self) -> None:
        if self.scheduled and self.reference_time is not None:
            raise ValueError("A scheduled run uses the slot's hour; do not pass a reference")
        if self.reference_time is not None:
            value = self.reference_time
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Reference time must include a timezone")
            if derive_reference_time(value) != value.astimezone(UTC):
                raise ValueError("Reference time must be an exact UTC hour")
        if self.readiness_poll_seconds < 5:
            raise ValueError("Readiness poll interval must be at least 5 seconds")


@dataclass
class ForecastDeps:
    """Injectable effects; defaults use the existing configured services."""

    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    schema: Callable[[], dict[str, Any]]
    run_lock: Callable[[], AbstractContextManager[None]]
    storage_preflight: Callable[[], None]
    issuer: Callable[[], Any]
    learning: Callable[[], Any]
    governance: Callable[[Any], Any]
    forecast: Callable[..., dict[str, Any]]
    read_revision: Callable[[], str]
    monotonic: Callable[[], float] = time.perf_counter
    readiness: Callable[..., dict[str, Any]] = baseline_readiness
    pointer: Callable[[Path], dict[str, Any] | None] = read_baseline_pointer


def object_store(*, ensure_bucket: bool) -> Any:
    """The configured S3-compatible store from the established four variables."""
    from mesoforge.storage.s3 import S3ArtifactObjectStore

    names = (
        "MESOFORGE_S3_BUCKET",
        "MESOFORGE_S3_ENDPOINT",
        "MESOFORGE_S3_ACCESS_KEY",
        "MESOFORGE_S3_SECRET_KEY",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Object storage settings are missing: {', '.join(missing)}")
    return S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=ensure_bucket,
    )


def default_deps() -> ForecastDeps:
    def schema() -> dict[str, Any]:
        from mesoforge.storage.postgres.database import resolve_database_dsn
        from mesoforge.storage.postgres.schema import schema_status

        return schema_status(resolve_database_dsn("MESOFORGE_DATABASE_DSN"))

    def run_lock() -> AbstractContextManager[None]:
        from mesoforge.storage.postgres.database import resolve_database_dsn
        from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock

        lock = PostgresIdempotencyLock(resolve_database_dsn("MESOFORGE_DATABASE_DSN"))
        return lock.try_acquire(FORECAST_RUN_LOCK)

    def issuer() -> Any:
        from mesoforge.application.batch_forecast import create_issuer

        return create_issuer(ensure_bucket=False)

    def learning() -> Any:
        from mesoforge.application.learning import configured_learning

        return configured_learning()

    def governance(learning_service: Any) -> Any:
        from mesoforge.application.governance import GovernanceService

        return GovernanceService(learning_service)

    def forecast(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from mesoforge.application.forecast_from_baseline import forecast_from_baseline

        return forecast_from_baseline(*args, **kwargs)

    return ForecastDeps(
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        schema=schema,
        run_lock=run_lock,
        storage_preflight=lambda: object_store(ensure_bucket=False).check_bucket(),
        issuer=issuer,
        learning=learning,
        governance=governance,
        forecast=forecast,
        read_revision=lambda: current_code_revision(_ROOT),
    )


def desk_configuration() -> dict[str, Any]:
    """Network-free check of the runtime desk settings; never reports a value.

    The desk is attempted regardless. This only makes a missing credential or an
    invalid (for example blank) setting visible before any location runs.
    """
    from mesoforge.application.forecast_desk_provider import (
        config_from_environment,
        provider_from_environment,
    )
    from mesoforge.contracts.forecast_desk import DeskConfigurationError

    try:
        provider = provider_from_environment()
        config_from_environment()
    except DeskConfigurationError as exc:
        return {"status": "configuration_invalid", "setting": exc.setting}
    return {
        "status": "configured" if provider.provider_name != "unconfigured" else "unconfigured",
        "provider": provider.provider_name,
        "model": provider.model_name,
    }


def _error(exc: BaseException) -> str:
    return str(redact(f"{type(exc).__name__}: {exc}"))[:2000]


def _label(location: Any) -> dict[str, Any]:
    if isinstance(location, dict):
        return {key: location.get(key) for key in ("id", "name", "lat", "lon") if key in location}
    return {"value": str(location)}


def location_outcome(row: dict[str, Any]) -> dict[str, Any]:
    """Operator facts for one configured location, without forecast payloads."""
    desk = (row.get("learning") or {}).get("ai")
    verification = row.get("previous_verification") or {"status": "not_run"}
    outcome: dict[str, Any] = {
        "index": row.get("index"),
        "location": _label(row.get("location")),
        "status": row.get("status"),
        "issued_forecast_id": (row.get("issued") or {}).get("issued_forecast_id"),
        "existing_issued_forecast_ids": (row.get("skipped") or {}).get(
            "existing_issued_forecast_ids"
        ),
        "verification": {
            field: (verification.get(field) or verification).get("status", "not_run")
            for field in ("temperature", "qpf")
        },
    }
    learning = row.get("learning") or {}
    if learning.get("status") is not None:
        outcome["correction"] = {
            "status": learning.get("status"),
            "applied_delta_k": learning.get("applied_delta_k"),
        }
    if desk is not None:
        accepted = desk.get("accepted_recipes", [])
        usage = desk.get("usage") or {}
        outcome["ai_desk"] = {
            "completion_reason": desk.get("completion_reason"),
            "provider_failure_code": desk.get("provider_failure_code"),
            "provider_calls": usage.get("provider_calls"),
            "tool_calls": usage.get("tool_calls"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "desk_seconds": (desk.get("timings") or {}).get("total_seconds"),
            "accepted_edits": len(accepted),
            "issued_stage": (learning.get("operational_stage") or {}).get(
                "transformation_type", "deterministic_corrected"
            ),
            "discarded_edits": len(desk.get("discarded_recipes", [])),
        }
    reason = row.get("error") or row.get("reason")
    if reason is not None:
        outcome["reason"] = str(redact(str(reason)))[:1000]
    for key, value in row.items():
        if key.endswith("_seconds") and isinstance(value, int | float):
            outcome[key] = round(value, 3)
    return outcome


class ForecastRun:
    def __init__(
        self, settings: ForecastSettings, deps: ForecastDeps, *, stream: TextIO | None = None
    ) -> None:
        root = settings.root.resolve()
        if root.is_relative_to(_ROOT):
            raise ValueError("The runtime root must remain outside the repository")
        self.settings, self.deps, self.stream = settings, deps, stream
        self.root = root
        self.baseline_root = root / "baseline"

    def log(self, name: str, **fields: Any) -> dict[str, Any]:
        return event(ROLE, name, stream=self.stream, clock=self.deps.clock, **fields)

    def execute(self) -> tuple[int, dict[str, Any]]:
        d, s = self.deps, self.settings
        started, timer = d.clock(), d.monotonic()
        record: dict[str, Any] = {
            "schema_version": RUN_SCHEMA,
            "role": ROLE,
            "started_at": iso(started),
            "trigger": "scheduled" if s.scheduled else "manual",
            "reference_time_source": "explicit_replay" if s.reference_time else "request_hour",
        }

        def finish(code: int, status: str, **fields: Any) -> tuple[int, dict[str, Any]]:
            record.update(status=status, exit_code=code, **fields)
            record["finished_at"] = iso(d.clock())
            record["seconds"] = round(d.monotonic() - timer, 3)
            self.log(
                "run_finished",
                status=status,
                exit_code=code,
                **{k: record[k] for k in ("category", "reason", "summary") if k in record},
            )
            return code, record

        try:
            record["code_revision"] = d.read_revision()
        except Exception as exc:
            return finish(
                EXIT_FAILED, "failed", category="code_revision_unavailable", reason=_error(exc)
            )
        try:
            locations = load_locations(s.config)
        except (OSError, ValueError) as exc:
            return finish(
                EXIT_FAILED, "failed", category="invalid_configuration", reason=_error(exc)
            )
        valid = 0
        for location in locations:
            try:
                validate_coordinate(*_coordinates(location))
            except ValueError:
                continue
            valid += 1
        record["locations"] = {"configured": len(locations), "valid": valid}
        try:
            record["ai_desk_configuration"] = desk_configuration()
        except Exception as exc:
            record["ai_desk_configuration"] = {"status": "unknown", "error": _error(exc)}
        if not valid:
            return finish(
                EXIT_FAILED,
                "failed",
                category="invalid_configuration",
                reason="No valid configured location",
            )
        if s.reference_time is not None and s.reference_time.astimezone(
            UTC
        ) > derive_reference_time(started):
            return finish(
                EXIT_FAILED,
                "failed",
                category="invalid_configuration",
                reason="Reference time is after the current UTC hour",
            )
        slot = None
        if s.scheduled:
            slot = s.schedule.due_slot(started)
            record["schedule"] = s.schedule.describe(started)
            if slot is None:
                return finish(EXIT_OK, "not_due")
        self.log(
            "run_started",
            trigger=record["trigger"],
            slot=iso(slot) if slot else None,
            locations=record["locations"],
        )
        try:
            schema = d.schema()
        except Exception as exc:
            return finish(
                EXIT_FAILED, "failed", category="database_unavailable", reason=_error(exc)
            )
        record["schema"] = schema
        if not schema.get("at_head"):
            return finish(
                EXIT_FAILED,
                "failed",
                category="schema_not_at_head",
                reason=f"database {schema.get('current')} != code {schema.get('head')}",
            )
        deadline = s.schedule.window_end(slot) if slot is not None else None
        with ExitStack() as stack:
            waited = 0
            while True:
                try:
                    stack.enter_context(d.run_lock())
                    break
                except AdvisoryLockBusy:
                    remaining = (deadline - d.clock()).total_seconds() if deadline else 0.0
                    if remaining <= 0:
                        if deadline is None:
                            return finish(
                                EXIT_OK,
                                "already_running",
                                reason="Another forecast run holds the run lock",
                            )
                        # The slot was not run by this trigger; surface it, never hide it.
                        return finish(
                            EXIT_FAILED,
                            "failed",
                            category="run_lock_busy_until_window_closed",
                            reason="Another forecast run held the run lock for the whole slot",
                        )
                    waited += 1
                    self.log("run_lock_wait", attempt=waited)
                    d.sleep(min(s.readiness_poll_seconds, remaining))
                except Exception as exc:
                    return finish(
                        EXIT_FAILED, "failed", category="database_unavailable", reason=_error(exc)
                    )
            record["run_lock_waits"] = waited
            return self._locked(record, locations, slot, finish)

    def _locked(
        self,
        record: dict[str, Any],
        locations: list[Any],
        slot: datetime | None,
        finish: Callable[..., tuple[int, dict[str, Any]]],
    ) -> tuple[int, dict[str, Any]]:
        d, s = self.deps, self.settings
        try:
            d.storage_preflight()
        except Exception as exc:
            return finish(EXIT_FAILED, "failed", category="storage_unavailable", reason=_error(exc))
        try:
            issuer = d.issuer()
            learning = d.learning()
            governance = d.governance(learning)
            governance.status()
        except Exception as exc:
            return finish(
                EXIT_FAILED, "failed", category="governance_unavailable", reason=_error(exc)
            )
        deadline = s.schedule.window_end(slot) if slot is not None else None
        attempts = 0
        readiness: dict[str, Any] = {"ready": False, "reasons": [], "baseline": None}
        while True:
            attempts += 1
            pointer: dict[str, Any] | None = None
            unreadable = None
            try:
                pointer = d.pointer(self.baseline_root)
            except (SnapshotError, OSError, ValueError) as exc:
                unreadable = exc
            requested = d.clock()
            if deadline is not None and requested >= deadline:
                readiness = {
                    **readiness,
                    "ready": False,
                    "reasons": [*readiness.get("reasons", []), "slot_window_closed"],
                }
                break
            if pointer is None:
                readiness = {
                    "ready": False,
                    "baseline": None,
                    "reference_time": None,
                    "reasons": [
                        f"baseline_unreadable: {_error(unreadable)}"
                        if unreadable is not None
                        else "no_published_baseline"
                    ],
                }
            else:
                try:
                    readiness = d.readiness(
                        self.baseline_root,
                        locations,
                        now=requested,
                        reference_time=s.reference_time,
                        pointer=pointer,
                        governance=governance,
                    )
                except Exception as exc:
                    readiness = {
                        "ready": False,
                        "baseline": None,
                        "reasons": [f"readiness_error: {_error(exc)}"],
                    }
            if readiness["ready"] or deadline is None:
                break
            wait = min(s.readiness_poll_seconds, (deadline - d.clock()).total_seconds())
            if wait <= 0:
                break
            self.log(
                "readiness_wait",
                attempt=attempts,
                reasons=readiness["reasons"],
                retry_in_seconds=round(wait, 1),
            )
            d.sleep(wait)
        record["readiness"] = {k: v for k, v in readiness.items() if k != "pointer"}
        record["readiness_attempts"] = attempts
        if not readiness["ready"] or pointer is None:
            return finish(
                EXIT_NOT_READY,
                "baseline_not_ready",
                category="baseline_not_ready",
                reason="; ".join(readiness["reasons"]),
            )
        self.log(
            "baseline_pinned",
            baseline=readiness["baseline"],
            reference_time=readiness["reference_time"],
            uncovered_locations=readiness["uncovered_locations"],
        )
        overlays: dict[str, Any] = {"overlays": [], "missing": [], "failures": []}
        try:
            candidates = governance.blend_candidates(requested)
            if candidates:
                pinned = load_baseline(self.baseline_root, pointer=pointer)
                overlays = learning.overlays_for(pinned, candidates, analysis_cutoff=requested)
        except Exception as exc:
            overlays["failures"].append({"phase": "overlay_lookup", "reason": _error(exc)})
        record["candidate_overlays"] = {
            "found": len(overlays["overlays"]),
            "missing": overlays["missing"],
            "failures": overlays["failures"],
        }
        try:
            result = d.forecast(
                self.baseline_root,
                locations,
                baseline_pointer=pointer,
                request_time=requested,
                reference_time=s.reference_time,
                issue=True,
                issuer=issuer,
                verify_prior=s.verify_prior,
                learning_service=learning,
                learning_overlays=overlays["overlays"],
                governance=governance,
            )
        except Exception as exc:
            return finish(EXIT_FAILED, "failed", category="forecast_failed", reason=_error(exc))
        rows = [location_outcome(row) for row in result.get("results", [])]
        for row in rows:
            self.log("location_result", **row)
        record.update(
            request_time=result.get("request_time"),
            reference_time=result.get("reference_time"),
            baseline=result.get("baseline", {}).get("baseline_snapshot_id"),
            contributor_state=result.get("baseline", {}).get("prepared_snapshot_id"),
            governance=result.get("governance"),
            results=rows,
            summary=result.get("summary"),
            timings=result.get("timings"),
        )
        status = result.get("status")
        if status == "no_current_baseline":
            return finish(
                EXIT_NOT_READY,
                "baseline_not_ready",
                category="baseline_not_ready",
                reason=result.get("reason"),
            )
        if status != "ok":
            return finish(EXIT_FAILED, "failed", category=str(status), reason=result.get("reason"))
        summary = result.get("summary") or {}
        if summary.get("failed"):
            return finish(EXIT_PARTIAL, "partial", category="location_failures")
        return finish(EXIT_OK, "completed")


def persist(root: Path, record: dict[str, Any]) -> Path:
    """Retain the public run record and the compact latest-run status."""
    started = datetime.fromisoformat(record["started_at"])
    directory = root / "runs" / f"forecast-{started:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "result.json", record)
    write_json(
        status_directory(root) / FORECAST_STATUS,
        {
            **{k: v for k, v in record.items() if k not in {"results", "readiness", "timings"}},
            "record": str(directory / "result.json"),
            "results": [
                {
                    k: v
                    for k, v in row.items()
                    if k
                    in {
                        "location",
                        "status",
                        "issued_forecast_id",
                        "existing_issued_forecast_ids",
                        "ai_desk",
                        "reason",
                    }
                }
                for row in record.get("results", [])
            ],
        },
    )
    return directory


def run_forecast(
    settings: ForecastSettings, deps: ForecastDeps, *, stream: TextIO | None = None
) -> tuple[int, dict[str, Any]]:
    run = ForecastRun(settings, deps, stream=stream)
    code, record = run.execute()
    if record["status"] not in {"not_due", "already_running"}:
        record["record"] = str(persist(run.root, record) / "result.json")
    return code, record


def render(record: dict[str, Any]) -> str:
    lines = [
        f"Forecast worker: {record['status']} (exit {record['exit_code']})",
        f"Trigger: {record['trigger']}; started {record['started_at']}",
    ]
    if record.get("reason"):
        lines.append(f"Reason: {record['reason']}")
    readiness = record.get("readiness") or {}
    baseline = readiness.get("baseline") or {}
    if baseline:
        lines += [
            f"Baseline: {baseline.get('baseline_snapshot_id')} "
            f"(published {baseline.get('published_at')}, "
            f"age {baseline.get('age_seconds', 0):.0f}s)",
            f"Contributor state: {baseline.get('contributor_state_id')}",
            f"Contributors: {json.dumps(baseline.get('contributor_cycles', {}))}",
            f"Reference: {readiness.get('reference_time')}",
        ]
    for row in record.get("results", []):
        label = row["location"].get("name") or row["location"].get("id") or row["index"]
        issued = row.get("issued_forecast_id") or row.get("existing_issued_forecast_ids") or ""
        desk = row.get("ai_desk") or {}
        lines.append(
            f"  {label}: {row['status']} {issued}"
            + (
                f"; AI desk {desk.get('completion_reason')}, edits {desk.get('accepted_edits')}"
                if desk
                else ""
            )
            + (f"; {row['reason']}" if row.get("reason") else "")
        )
    if record.get("schedule"):
        lines.append(f"Next scheduled run: {record['schedule'].get('next_run_local')}")
    if record.get("record"):
        lines.append(f"Record: {record['record']}")
    return "\n".join(lines)


def _default_root() -> Path | None:
    value = os.environ.get("MESOFORGE_PROSPECTIVE_ROOT")
    return Path(value) if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "readiness", "next-run"))
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--scheduled",
        action="store_true",
        help="Only act inside a configured local-time slot window",
    )
    group.add_argument(
        "--reference-time",
        type=datetime.fromisoformat,
        help="Explicit covered UTC hour for replay/recovery only",
    )
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="Issuance-only recovery run; no prior observation work",
    )
    parser.add_argument("--json", action="store_true", help="Print the machine-readable record")
    args = parser.parse_args(argv)
    try:
        schedule = ForecastSchedule.from_environment()
    except (ValueError, KeyError) as exc:
        error = {"code": "invalid_configuration", "message": _error(exc)}
        print(json.dumps({"error": error}), file=sys.stderr)
        return EXIT_FAILED
    now = datetime.now(UTC)
    if args.command == "next-run":
        print(json.dumps(schedule.describe(now), indent=2))
        return EXIT_OK
    if args.root is None:
        parser.error("--root or MESOFORGE_PROSPECTIVE_ROOT is required")
    if args.command == "readiness":
        governance, failure = None, None
        try:
            deps = default_deps()
            governance = deps.governance(deps.learning())
        except Exception as exc:
            failure = exc
        try:
            report = baseline_readiness(
                args.root.resolve() / "baseline",
                load_locations(args.config),
                now=now,
                reference_time=args.reference_time,
                governance=governance,
            )
        except (OSError, ValueError) as exc:
            error = {"code": "invalid_request", "message": _error(exc)}
            print(json.dumps({"error": error}), file=sys.stderr)
            return EXIT_FAILED
        if failure is not None:
            mark_governance_unavailable(report, failure)
        report.pop("pointer", None)
        report["next_run"] = schedule.describe(now)
        print(json.dumps(report, indent=2, default=str))
        return EXIT_OK if report["ready"] else EXIT_NOT_READY
    try:
        settings = ForecastSettings(
            root=args.root,
            config=args.config,
            scheduled=args.scheduled,
            reference_time=args.reference_time,
            verify_prior=not args.skip_verification,
            schedule=schedule,
        )
        code, record = run_forecast(settings, default_deps())
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "forecast_worker_failed", "message": _error(exc)}}),
            file=sys.stderr,
        )
        return EXIT_FAILED
    print(json.dumps(record, indent=2, default=str) if args.json else render(record))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
