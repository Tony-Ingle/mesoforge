"""Status files and process health for hosted worker roles (standard library only).

Health answers one question: is the worker process alive and progressing? It is
independent of provider availability and of baseline readiness, which are reported
separately. Importing this module must stay cheap, because a container healthcheck
runs it every interval; it never imports guidance, storage or scientific code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

STATUS_DIRECTORY = "status"
GUIDANCE_STATUS = "guidance-worker.json"
GUIDANCE_HEARTBEAT = "guidance-worker.heartbeat.json"
FORECAST_STATUS = "forecast-worker.json"
# Upper bounds for one in-flight phase; beyond them the worker is not progressing.
PHASE_BOUNDS_SECONDS = {
    "schema": 300,
    "governance": 600,
    # Discovery can legitimately run until its reference hour expires.
    "probe": 3600,
    "refresh": 5400,
    "build": 3600,
    "background": 3600,
    "readiness": 600,
}
DEFAULT_PHASE_BOUND_SECONDS = 3600
# One whole poll (probe, refresh, build, background) must finish within this bound.
POLL_BOUND_SECONDS = 4 * 3600
HEARTBEAT_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 180
# A sleeping worker must start its next poll this long after it was due at the latest.
POLL_OVERDUE_SECONDS = 600


def status_directory(root: Path) -> Path:
    return root / STATUS_DIRECTORY


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def read_json(path: Path) -> dict[str, Any] | None:
    """A status document, or None when absent or unreadable (never raises)."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomic replacement, so readers never observe a partial status document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open.
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def phase_bound(phase: str | None) -> int:
    return PHASE_BOUNDS_SECONDS.get(phase or "", DEFAULT_PHASE_BOUND_SECONDS)


def guidance_health(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Process health of the guidance worker from its heartbeat and status files."""
    directory = status_directory(root)
    status = read_json(directory / GUIDANCE_STATUS)
    heartbeat = read_json(directory / GUIDANCE_HEARTBEAT)
    # Read first: a concurrent heartbeat must not appear to come from the future
    # merely because it was written after this healthcheck began reading files.
    now = (now or datetime.now(UTC)).astimezone(UTC)
    result: dict[str, Any] = {"role": "guidance-worker", "checked_at": iso(now), "healthy": False}
    if status is None or heartbeat is None:
        result["reason"] = "no_status"
        return result
    result["state"] = status.get("state")
    if not isinstance(status.get("state"), str):
        result["reason"] = "status_unreadable"
        return result
    if status.get("state") in {"stopped", "failed"}:
        result["reason"] = f"worker_{status.get('state')}"
        return result
    beat = parse_instant(heartbeat.get("heartbeat_at"))
    if beat is None:
        result["reason"] = "heartbeat_unreadable"
        return result
    result["heartbeat_age_seconds"] = round((now - beat).total_seconds(), 1)
    if beat > now:
        result["reason"] = "heartbeat_in_future"
        return result
    if (now - beat).total_seconds() > HEARTBEAT_STALE_SECONDS:
        result["reason"] = "heartbeat_stale"
        return result
    poll_started = parse_instant(heartbeat.get("poll_started_at"))
    if heartbeat.get("poll_started_at") is not None and (
        poll_started is None or poll_started > now
    ):
        result["reason"] = "poll_time_unreadable"
        return result
    if poll_started is not None and (now - poll_started).total_seconds() > POLL_BOUND_SECONDS:
        result["reason"] = "poll_exceeded_bound"
        return result
    due = parse_instant(heartbeat.get("next_poll_at"))
    if heartbeat.get("next_poll_at") is not None and due is None:
        result["reason"] = "next_poll_time_unreadable"
        return result
    if (
        heartbeat.get("state") == "sleeping"
        and due is not None
        and (now - due).total_seconds() > POLL_OVERDUE_SECONDS
    ):
        result["reason"] = "poll_overdue"
        return result
    in_flight = heartbeat.get("in_flight")
    if in_flight is not None and (
        not isinstance(in_flight, dict) or not isinstance(in_flight.get("phase"), str)
    ):
        result["reason"] = "phase_unreadable"
        return result
    if isinstance(in_flight, dict):
        started = parse_instant(in_flight.get("started_at"))
        bound = phase_bound(in_flight.get("phase"))
        age = (now - started).total_seconds() if started else None
        result["in_flight"] = {**in_flight, "age_seconds": age, "bound_seconds": bound}
        if age is None or age < 0 or age > bound:
            result["reason"] = "phase_exceeded_bound"
            return result
    result.update(healthy=True, reason="progressing")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("guidance-health",))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ["MESOFORGE_PROSPECTIVE_ROOT"])
        if os.environ.get("MESOFORGE_PROSPECTIVE_ROOT")
        else None,
    )
    args = parser.parse_args(argv)
    if args.root is None:
        parser.error("--root or MESOFORGE_PROSPECTIVE_ROOT is required")
    result = guidance_health(args.root)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
