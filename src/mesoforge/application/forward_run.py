"""One on-demand run: verify saved hours, then prepare and issue current forecasts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from mesoforge.application.batch_forecast import _coordinates, create_issuer, load_locations
from mesoforge.application.current_model_set import select_model_set
from mesoforge.application.forward_verification import verify_previous
from mesoforge.application.hourly_report import build_hourly_report, render_hourly_report
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_temperature import BoundedHttpTransport
from mesoforge.application.selected_forecast import run_selected_batch
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.guidance.runtime import SystemClock, SystemSleeper

_ROOT = Path(__file__).resolve().parents[3]


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


def run_forward(
    config_path: Path,
    output_directory: Path,
    *,
    display_timezone: str = "UTC",
    issuer: ForecastIssuanceService | None = None,
) -> dict[str, Any]:
    """Share one current selection/preparation after independent prior-hour verification.

    Each invocation owns a new external directory. Verification outcomes remain
    separate from newly issued forecasts, whose future hours are not yet verified.
    The display timezone never affects coordinate selection or scientific values.
    """
    ZoneInfo(display_timezone)  # Reject a bad presentation option before acquiring anything.
    locations = load_locations(config_path)
    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(_ROOT):
        raise ValueError("Retain forward-run data outside the repository")
    issuer = issuer if issuer is not None else create_issuer()
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
        "results": [],
    }
    supported = 0
    for index, location in enumerate(locations):
        row: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
            validate_coordinate(latitude, longitude)
        except ValueError as exc:
            row.update(status="error", error={"code": "invalid_location", "message": str(exc)})
        else:
            supported += 1
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

    if supported:
        try:
            # Discover after verification so observation work cannot expire the selection.
            selection = _discover(output_directory / "selection")
            report["selection"] = selection
            if selection["status"] != "selected":
                raise ValueError(selection.get("reason", "No complete current model set available"))
            batch = run_selected_batch(
                snapshot,
                output_directory / "selection/selection.json",
                output_directory / "prepared",
                issuer=issuer,
                forecast_report_builder=partial(
                    build_hourly_report, display_timezone=display_timezone
                ),
            )
            report.update({key: value for key, value in batch.items() if key != "results"})
            for row, issued in zip(report["results"], batch["results"], strict=True):
                if row["status"] == "pending_issuance":
                    row.update(issued)
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
        help="Optional report presentation zone, e.g. America/Chicago",
    )
    args = parser.parse_args(argv)
    try:
        result = run_forward(args.config, args.output_dir, display_timezone=args.display_timezone)
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
