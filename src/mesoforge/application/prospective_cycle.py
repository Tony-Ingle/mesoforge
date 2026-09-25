"""Run one configured-location cycle now; an external scheduler owns when to invoke it.

Composition only: guidance and numerical baseline publish independently, then the
existing issuance boundary pins one baseline, verifies prior hours and delivers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.baseline_snapshot import load_baseline
from mesoforge.application.batch_forecast import (
    _coordinates,
    create_issuer,
    load_locations,
    location_display_timezone,
)
from mesoforge.application.build_baseline import build_baseline
from mesoforge.application.forecast_from_baseline import forecast_from_baseline
from mesoforge.application.forecast_from_snapshot import _public
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_snapshot import derive_reference_time
from mesoforge.application.refresh_guidance import refresh_guidance
from mesoforge.application.spatial_coverage import validate_coordinate

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = _ROOT / "configs/locations.json"


def _now(clock: Callable[[], datetime] | None) -> datetime:
    value = datetime.now(UTC) if clock is None else clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("The runtime clock must be timezone-aware")
    return value.astimezone(UTC)


def _repeat_pointer(
    root: Path,
    locations: list[Any],
    reference: datetime,
    issuer: ForecastIssuanceService,
) -> dict[str, Any] | None:
    """An optimization only; the existing locked issuance check remains authoritative."""
    try:
        for location in locations:
            lat, lon = _coordinates(location)
            if not issuer.find_versions(
                latitude=lat, longitude=lon, target_reference_time=reference
            ):
                return None
        pinned = load_baseline(root)
        pinned.reference_view(reference)
        covered = {
            (row["latitude"], row["longitude"])
            for row in pinned.manifest["domains"]
            if datetime.fromisoformat(row["reference_time"]) == reference
        }
        if all(_coordinates(location) in covered for location in locations):
            return dict(pinned.pointer)
    except Exception:
        # A lookup failure is retried/reported by the normal per-location boundary.
        # It never establishes permission to bypass that boundary's duplicate guard.
        return None
    return None


def run_prospective_cycle(
    config_path: Path,
    root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    replay_reference_time: datetime | None = None,
    issuer: ForecastIssuanceService | None = None,
) -> dict[str, Any]:
    """One noninteractive operator cycle; clock injection is for deterministic tests.

    The location analysis clock is sampled AFTER background publication. Its floored
    UTC hour is the normal reference, just as in forecast_from_baseline. The start
    clock is operational metadata, not a false cutoff for later acquired guidance.
    """
    started = _now(clock)
    reference = derive_reference_time(started)
    if replay_reference_time is not None:
        if replay_reference_time.tzinfo is None or replay_reference_time.utcoffset() is None:
            raise ValueError("Replay reference must be timezone-aware")
        reference = replay_reference_time.astimezone(UTC)
        if reference != derive_reference_time(reference) or reference > started:
            raise ValueError("Replay reference must be an exact hour, not in the future")
    root = root.resolve()
    if root.is_relative_to(_ROOT):
        raise ValueError("Prospective artifacts must remain outside the repository")
    locations = load_locations(config_path)
    if not locations:
        raise ValueError("Configure at least one location")
    valid, failures = [], []
    for index, location in enumerate(locations):
        try:
            validate_coordinate(*_coordinates(location))
            location_display_timezone(location)
        except ValueError as exc:
            failures.append(
                {
                    "index": index,
                    "location": location,
                    "status": "error",
                    "error": {"code": "invalid_location", "message": str(exc)},
                }
            )
        else:
            valid.append(location)
    directory = root / "runs" / f"{started:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "started_at": started.isoformat(),
        "config": str(config_path.resolve()),
        "directory": str(directory),
        "reference_time": reference.isoformat(),
        "reference_time_source": "runtime_UTC_hour"
        if replay_reference_time is None
        else "explicit_replay",
        "background": {},
        "results": failures,
    }
    timer = time.perf_counter()
    phase = "storage_preflight"
    try:
        if not valid:
            raise ValueError("No valid configured locations")
        issuer = issuer or create_issuer()
        pointer = _repeat_pointer(root / "baseline", valid, reference, issuer)
        if pointer is None:
            # Refresh's collection validation is intentionally strict. Isolate bad
            # configured rows here without redefining the shared locations format.
            background_config = directory / "locations.json"
            background_config.write_text(json.dumps({"locations": valid}), encoding="utf-8")
            phase = "guidance_refresh"
            refresh = refresh_guidance(background_config, root / "guidance")
            result["background"]["refresh"] = refresh
            if refresh["status"] != "published":
                raise RuntimeError(refresh.get("error", "Guidance refresh did not publish"))
            phase = "baseline_build"
            build = build_baseline(
                root / "guidance",
                root / "baseline",
                valid,
                prepared_pointer=refresh["latest_complete"],
            )
            result["background"]["build"] = {k: v for k, v in build.items() if k != "manifest"}
            pointer = build["pointer"]
        else:
            result["background"] = {
                "status": "reused_for_already_issued_window",
                "reason": "Every valid location has a version; recheck under issuance lock.",
            }
        phase = "configured_forecast"
        requested = _now(clock)
        if requested < started:
            raise ValueError("Runtime clock moved backward during this cycle")
        # Crossing an hour during a repeat check cannot reuse that earlier window.
        # The pinned baseline must actually cover the new request hour; never relabel it.
        forecast = forecast_from_baseline(
            root / "baseline",
            locations,
            request_time=requested,
            reference_time=replay_reference_time,
            baseline_pointer=pointer,
            issue=True,
            issuer=issuer,
        )
        result.update(_public(forecast))
        result["status"] = (
            "completed"
            if forecast.get("status") == "ok" and not forecast["summary"]["failed"]
            else "partial"
            if forecast.get("status") == "ok"
            else "failed"
        )
    except Exception as exc:
        result.update(status="failed", failed_phase=phase, reason=f"{type(exc).__name__}: {exc}")
    if not result.get("results") or result["status"] == "failed":
        existing = {row["index"]: row for row in result["results"]}
        result["results"] = [
            existing.get(
                index,
                {
                    "index": index,
                    "location": location,
                    "status": "not_run",
                    "reason": result.get("reason", "No current baseline"),
                },
            )
            for index, location in enumerate(locations)
        ]
    for row in result["results"]:
        row["extraction_outcome"] = (
            "completed"
            if "baseline_extraction_seconds" in row
            else "not_needed_already_issued"
            if row["status"] == "skipped_already_issued"
            else "not_completed"
        )
        row["issuance_outcome"] = (
            "issued"
            if "issued" in row
            else "reused"
            if row["status"] == "skipped_already_issued"
            else "not_issued"
        )
        row.setdefault("previous_verification", {"status": "not_run"})
    result["operator_seconds"] = time.perf_counter() - timer
    (directory / "result.json").write_text(
        json.dumps(result, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def render_summary(result: dict[str, Any]) -> str:
    lines = [
        "Prospective cycle",
        f"Started: {result['started_at']}",
        f"Reference: {result['reference_time']}",
    ]
    baseline = result.get("baseline", {})
    lines += [
        f"Contributor snapshot: {baseline.get('prepared_snapshot_id', 'not available')}",
        f"Baseline: {baseline.get('baseline_snapshot_id', 'not available')}",
    ]
    for row in result["results"]:
        location = row["location"]
        lines.append(
            str(location.get("name", location)) if isinstance(location, dict) else str(location)
        )
        verification = row["previous_verification"]
        for field in ("temperature", "qpf"):
            value = verification.get(field, verification)
            lines.append(f"  {field} verification: {value.get('status', 'not_run')}")
        ids = row.get("issued", {}).get("issued_forecast_id") or row.get("skipped", {}).get(
            "existing_issued_forecast_ids", ""
        )
        lines.append(f"  extraction: {row['extraction_outcome']}")
        lines.append(f"  issuance: {row['issuance_outcome']} {ids}")
        if row.get("error") or row.get("reason"):
            lines.append(f"  reason: {row.get('error', row.get('reason'))}")
    lines.append(f"Cycle result: {result['status']}")
    if result.get("reason"):
        lines.append(f"Reason: {result['reason']}")
    lines.append(f"Report: {result['directory']}/result.json")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    data_home = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or Path.home() / ".local/share"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.environ.get("MESOFORGE_PROSPECTIVE_ROOT", str(data_home / "MesoForge/prospective"))
        ),
    )
    parser.add_argument(
        "--replay-reference-time",
        type=datetime.fromisoformat,
        help="Explicit covered hour for testing/replay/debugging only; normal runs need no date",
    )
    args = parser.parse_args(argv)
    try:
        result = run_prospective_cycle(
            args.config, args.root, replay_reference_time=args.replay_reference_time
        )
    except Exception as exc:
        print(f"Prospective cycle failed: {exc}", file=sys.stderr)
        return 2
    print(render_summary(result))
    return 0 if result["status"] == "completed" else 1 if result["status"] == "partial" else 2


if __name__ == "__main__":
    raise SystemExit(main())
