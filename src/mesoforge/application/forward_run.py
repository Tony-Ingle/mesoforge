"""Compatibility/development lifecycle: verify, prepare and blend inline.

Normal configured-location jobs consume a background-built baseline through
``forecast_from_baseline``; this retained path supports explicit replay/comparison.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from mesoforge.application.batch_forecast import (
    _coordinates,
    create_issuer,
    load_locations,
    location_display_timezone,
)
from mesoforge.application.current_model_set import select_model_set
from mesoforge.application.forward_verification import verify_previous
from mesoforge.application.hourly_report import build_hourly_report, render_hourly_report
from mesoforge.application.issuance import (
    FORWARD_RUN_LOCK,
    ForecastIssuanceService,
    acquire_issuance_run_lock,
)
from mesoforge.application.prepared_temperature import BoundedHttpTransport
from mesoforge.application.selected_forecast import run_selected_batch
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.guidance.runtime import SystemClock, SystemSleeper
from mesoforge.storage.postgres.idempotency_lock import AdvisoryLockBusy

_ROOT = Path(__file__).resolve().parents[3]
OVERLAP_PROTECTION = {
    "mechanism": "postgresql_session_advisory_lock",
    "lock_key": str(FORWARD_RUN_LOCK),
    "behavior": (
        "A concurrent forward run fails immediately with forward_run_overlap before creating "
        "its directory, verifying or issuing; it never waits."
    ),
}
DECISION_WINDOW_GUARD = (
    "skip_issuance_when_a_version_exists_for_the_same_coordinate_and_target_reference_time"
)


def _discover(directory: Path) -> dict[str, Any]:
    configuration, _ = load_configuration_source(
        base_path=_ROOT / "configs/base.yaml",
        additional_overlay_paths=(
            _ROOT / "configs/phase1-grasston.yaml",
            _ROOT / "configs/phase2-grasston.yaml",
        ),
    )
    assert configuration.phase2 is not None
    transport = BoundedHttpTransport()
    try:
        return select_model_set(
            directory,
            configuration=configuration.phase2,
            transport=transport,
            clock=SystemClock(),
            sleeper=SystemSleeper(),
            surface_fields=True,
            qpf_fields=True,
        )
    finally:
        transport.close()


def _save(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, allow_nan=False)
        stream.write("\n")


def _acquire_run_lock() -> AbstractContextManager[None]:
    return acquire_issuance_run_lock()


def _report_builder(
    zones: dict[tuple[float, float], str], default: str
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def build(forecast: dict[str, Any]) -> dict[str, Any]:
        key = (float(forecast["latitude"]), float(forecast["longitude"]))
        return build_hourly_report(forecast, display_timezone=zones.get(key, default))

    return build


def run_forward(
    config_path: Path,
    output_directory: Path,
    *,
    display_timezone: str = "UTC",
    issuer: ForecastIssuanceService | None = None,
    reissue: bool = False,
    run_lock: Callable[[], AbstractContextManager[None]] | None = None,
) -> dict[str, Any]:
    """Share one current selection/preparation after independent prior-hour verification.

    Each invocation owns a new external directory. Verification outcomes remain
    separate from newly issued forecasts, whose future hours are not yet verified.
    The display timezone never affects coordinate selection or scientific values.
    Safe to repeat from an external scheduler: an overlapping run raises
    ``AdvisoryLockBusy`` before any work, and a repeated run skips coordinates that
    already hold a version for the discovered target reference time unless
    ``reissue`` is set.
    """
    ZoneInfo(display_timezone)  # Reject a bad presentation option before acquiring anything.
    locations = load_locations(config_path)
    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(_ROOT):
        raise ValueError("Retain forward-run data outside the repository")
    if output_directory.exists():
        raise FileExistsError(f"{output_directory} already exists; each run owns a new directory")
    with (run_lock or _acquire_run_lock)():
        issuer = issuer if issuer is not None else create_issuer()
        return _run_locked(
            locations,
            output_directory,
            display_timezone=display_timezone,
            issuer=issuer,
            reissue=reissue,
        )


def _run_locked(
    locations: list[Any],
    output_directory: Path,
    *,
    display_timezone: str,
    issuer: ForecastIssuanceService,
    reissue: bool,
) -> dict[str, Any]:
    output_directory.mkdir(parents=True, exist_ok=False)
    # All stages consume this same snapshot even if the user's config changes mid-run.
    snapshot = output_directory / "locations.json"
    _save(snapshot, {"locations": locations})
    evaluated_at = datetime.now(UTC)
    report: dict[str, Any] = {
        "forward_run_id": str(uuid4()),
        "started_at": evaluated_at.isoformat(),
        "directory": str(output_directory),
        "display_timezone": display_timezone,
        "reissue": reissue,
        "overlap_protection": OVERLAP_PROTECTION,
        "decision_window_guard": DECISION_WINDOW_GUARD,
        "results": [],
    }
    zones: dict[tuple[float, float], str] = {}
    coordinates: dict[int, tuple[float, float]] = {}
    for index, location in enumerate(locations):
        row: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
            validate_coordinate(latitude, longitude)
            zone = location_display_timezone(location) or display_timezone
        except ValueError as exc:
            row.update(status="error", error={"code": "invalid_location", "message": str(exc)})
        else:
            coordinates[index] = (latitude, longitude)
            zones[(latitude, longitude)] = zone
            row["display_timezone"] = zone
            try:
                row["verification"] = verify_previous(latitude, longitude, now=evaluated_at)
            except Exception:
                # Verification trouble must not prevent a fresh numerical issuance.
                row["verification"] = {
                    "status": "error",
                    "error": {
                        "code": "verification_failed",
                        "message": "Could not verify previous hours; new issuance will still run.",
                    },
                }
            row["status"] = "pending_issuance"
        report["results"].append(row)
    _save(output_directory / "previous-verification.json", {"results": report["results"]})

    if coordinates:
        try:
            # Discover after verification so observation work cannot expire the selection.
            selection = _discover(output_directory / "selection")
            report["selection"] = selection
            if selection["status"] != "selected":
                raise ValueError(selection.get("reason", "No complete current model set available"))
            target = datetime.fromisoformat(selection["target_reference_time"])
            pending = []
            for row in report["results"]:
                if row["status"] != "pending_issuance":
                    continue
                latitude, longitude = coordinates[row["index"]]
                existing = issuer.find_versions(
                    latitude=latitude, longitude=longitude, target_reference_time=target
                )
                if existing and not reissue:
                    row.update(
                        status="skipped_already_issued",
                        skipped={
                            "reason": (
                                "An issued forecast already exists for this coordinate and "
                                "target reference time; pass --reissue to add a version."
                            ),
                            "target_reference_time": selection["target_reference_time"],
                            "existing_issued_forecast_ids": [
                                str(record.issued_forecast_id) for record in existing
                            ],
                        },
                    )
                else:
                    pending.append(row)
            report["issuance_indexes"] = [row["index"] for row in pending]
            if pending:
                # Only coordinates still needing this decision window reach preparation.
                issuance_snapshot = output_directory / "issuance-locations.json"
                _save(issuance_snapshot, {"locations": [row["location"] for row in pending]})
                batch = run_selected_batch(
                    issuance_snapshot,
                    output_directory / "selection/selection.json",
                    output_directory / "prepared",
                    issuer=issuer,
                    include_pop=True,
                    forecast_report_builder=_report_builder(zones, display_timezone),
                )
                report.update({key: value for key, value in batch.items() if key != "results"})
                for row, issued in zip(pending, batch["results"], strict=True):
                    row.update({**issued, "index": row["index"]})
        except Exception as exc:
            report["issuance_error"] = {
                "code": "current_issuance_failed",
                "message": str(exc),
            }
            for row in report["results"]:
                if row["status"] == "pending_issuance":
                    row.update(status="error", error=report["issuance_error"])

    report["summary"] = {
        "issued": sum(row["status"] == "ok" for row in report["results"]),
        "skipped": sum(row["status"] == "skipped_already_issued" for row in report["results"]),
        "failed": sum(row["status"] == "error" for row in report["results"]),
        "verification": [
            {"index": row["index"], **row["verification"]}
            for row in report["results"]
            if "verification" in row
        ],
    }
    report["completed_at"] = datetime.now(UTC).isoformat()
    _save(output_directory / "result.json", report)
    lines = ["# Forward forecast run", "", f"Run: {report['forward_run_id']}", ""]
    for row in report["results"]:
        location = row["location"]
        # JSON quoting keeps optional labels display-only in this local Markdown report.
        lines.extend(
            [f"## Location {row['index']}: {json.dumps(location, ensure_ascii=False)}", ""]
        )
        if row["status"] == "ok":
            lines.extend(
                [
                    f"Issued forecast: {row['issued']['issued_forecast_id']}",
                    "",
                    "Previous forecasts: " + row.get("verification", {}).get("status", "not_run"),
                    "",
                    render_hourly_report(row["forecast"]["hourly_report"]),
                ]
            )
        elif row["status"] == "skipped_already_issued":
            lines.extend(
                [
                    "Issuance skipped: existing version(s) "
                    + ", ".join(row["skipped"]["existing_issued_forecast_ids"])
                    + f" already cover target {row['skipped']['target_reference_time']}",
                    "",
                    "Previous forecasts: " + row.get("verification", {}).get("status", "not_run"),
                    "",
                ]
            )
        else:
            lines.extend(["Forecast failed: " + row["error"]["message"], ""])
    with (output_directory / "hourly-report.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Coordinates and optional names")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory outside Git")
    parser.add_argument(
        "--display-timezone",
        default="UTC",
        help="Default report presentation zone, e.g. America/Chicago; locations may override",
    )
    parser.add_argument(
        "--reissue",
        action="store_true",
        help="Issue a new version even where one exists for the discovered target reference time",
    )
    args = parser.parse_args(argv)
    try:
        result = run_forward(
            args.config,
            args.output_dir,
            display_timezone=args.display_timezone,
            reissue=args.reissue,
        )
    except AdvisoryLockBusy:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "forward_run_overlap",
                        "message": (
                            "Another forward run holds the forward-run lock; nothing was "
                            "verified or issued. Retry after it completes."
                        ),
                    }
                }
            ),
            file=sys.stderr,
        )
        return 3
    except Exception:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "forward_run_failed",
                        "message": "Forward run could not start; check configuration and storage.",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
