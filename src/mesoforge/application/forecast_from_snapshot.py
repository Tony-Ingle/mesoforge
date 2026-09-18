"""Fast forecasts from the published latest-complete prepared snapshot; no provider access.

The request picks its own reference hour (the current UTC hour), checks that the
snapshot's absolute valid times cover hours 1..36 after it, builds the current
MesoForge baseline grid with the existing field policies and returns the 36-hour
point forecast. Nothing here discovers, downloads or prepares guidance, and an
insufficient snapshot is reported as ``no_current_snapshot`` rather than shortened.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.batch_forecast import (
    _coordinates,
    create_issuer,
    load_locations,
    location_display_timezone,
)
from mesoforge.application.hourly_report import build_hourly_report, render_hourly_report
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.point_forecast import ReferenceCoverageError
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    coverage_for,
    derive_reference_time,
    load_preparation,
    resolve_latest_complete,
    verify_prepared_run,
)
from mesoforge.application.spatial_coverage import (
    CoverageRequiredError,
    UnsupportedCoordinateError,
    validate_coordinate,
)
from mesoforge.application.weather_transitions import validate_display_timezone

_ROOT = Path(__file__).resolve().parents[3]
PROVENANCE_RULE = (
    "Every input was available before the snapshot's decision cutoff and before this "
    "issuance; contributing cycles need not match the reference hour."
)


def forecast_from_snapshot(
    root: Path,
    locations: list[Any],
    *,
    reference_time: datetime | None = None,
    request_time: datetime | None = None,
    display_timezone: str = "UTC",
    issue: bool = False,
    issuer: ForecastIssuanceService | None = None,
    reissue: bool = False,
) -> dict[str, Any]:
    """Serve every location from one snapshot load; failures stay per location."""
    validate_display_timezone(display_timezone)
    requested_at = request_time or datetime.now(UTC)
    if requested_at.tzinfo is None:
        raise ValueError("Request time must include a timezone")
    requested_at = requested_at.astimezone(UTC)
    derived = derive_reference_time(requested_at)
    reference = derived if reference_time is None else reference_time.astimezone(UTC)
    if reference.minute or reference.second or reference.microsecond:
        raise ValueError("Reference time must be an exact UTC hour")
    if issue and reference > derived:
        raise ValueError("Issuance cannot use a reference hour after the request hour")
    timings: dict[str, float] = {}
    started = time.perf_counter()
    result: dict[str, Any] = {
        "request_time": requested_at.isoformat().replace("+00:00", "Z"),
        "reference_time": reference.isoformat().replace("+00:00", "Z"),
        "reference_time_source": "request_hour" if reference_time is None else "explicit",
        "network_calls": 0,
        "provenance_rule": PROVENANCE_RULE,
    }
    try:
        pointer, manifest, directory = resolve_latest_complete(root)
        preparation = verify_prepared_run(manifest)
    except (SnapshotError, OSError, KeyError, ValueError) as exc:
        result.update(status="no_current_snapshot", reason=str(exc), results=[])
        return result
    timings["resolve_and_verify_seconds"] = time.perf_counter() - started
    coverage = coverage_for(manifest, reference)
    result.update(
        snapshot={
            "snapshot_id": manifest["snapshot_id"],
            "directory": str(directory),
            "published_at": pointer["published_at"],
            "manifest_sha256": pointer["manifest_sha256"],
            "prepared_reference_time": manifest["coverage"]["reference_time"],
            "decision_time": manifest["coverage"]["decision_time"],
            "first_valid_time": manifest["coverage"]["first_valid_time"],
            "last_valid_time": manifest["coverage"]["last_valid_time"],
            "contributor_cycles": {
                model: row.get("cycle")
                for model, row in manifest["contributors"].items()
                if "cycle" in row
            },
            "nbm_product_cycles": {
                name: row.get("cycle")
                for name, row in manifest["contributors"]["NBM"]["products"].items()
            },
            "field_policies": manifest["field_policies"],
        },
        coverage=coverage,
    )
    if not coverage["usable"]:
        result.update(status="no_current_snapshot", reason=coverage["reason"], results=[])
        result["timings"] = timings
        return result
    clock = time.perf_counter()
    prepared = load_preparation(preparation)
    timings["guidance_load_seconds"] = time.perf_counter() - clock
    try:
        view = prepared.reference_view(reference)
    except ReferenceCoverageError as exc:
        result.update(status="no_current_snapshot", reason=str(exc), results=[])
        result["timings"] = timings
        return result
    if issue and issuer is None:
        issuer = create_issuer()
    batch_run_id = uuid4()
    rows: list[dict[str, Any]] = []
    for index, location in enumerate(locations):
        row: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
            validate_coordinate(latitude, longitude)
            zone = location_display_timezone(location) or display_timezone
        except ValueError as exc:
            row.update(status="error", error={"code": "invalid_location", "message": str(exc)})
            rows.append(row)
            continue
        row["display_timezone"] = zone
        if issue:
            # The decision-window guard runs before the expensive grid build.
            assert issuer is not None
            existing = issuer.find_versions(
                latitude=latitude, longitude=longitude, target_reference_time=reference
            )
            if existing and not reissue:
                row.update(
                    status="skipped_already_issued",
                    skipped={
                        "reason": "A version exists for this coordinate and reference hour; "
                        "pass --reissue to add one",
                        "existing_issued_forecast_ids": [
                            str(record.issued_forecast_id) for record in existing
                        ],
                    },
                )
                rows.append(row)
                continue
        try:
            clock = time.perf_counter()
            forecast = view.forecast(latitude=latitude, longitude=longitude)
            row["local_grid_build_seconds"] = time.perf_counter() - clock
            forecast["prepared_snapshot"] = {
                "snapshot_id": manifest["snapshot_id"],
                "published_at": pointer["published_at"],
                "manifest_sha256": pointer["manifest_sha256"],
                "prepared_reference_time": manifest["coverage"]["reference_time"],
                "decision_time": manifest["coverage"]["decision_time"],
                "request_time": result["request_time"],
                "reference_time": result["reference_time"],
                "reference_time_source": result["reference_time_source"],
                "coverage": {
                    "first_valid_time": manifest["coverage"]["first_valid_time"],
                    "last_valid_time": manifest["coverage"]["last_valid_time"],
                    "prepared_hours": manifest["coverage"]["prepared_hours"],
                    "policy": manifest["coverage"]["policy"]["id"],
                    "nbm_active_products": coverage["nbm_active_products"],
                },
                "contributor_cycles": result["snapshot"]["contributor_cycles"],
                "nbm_product_cycles": result["snapshot"]["nbm_product_cycles"],
                "field_policies": manifest["field_policies"],
                "provenance_rule": PROVENANCE_RULE,
            }
            clock = time.perf_counter()
            forecast["hourly_report"] = build_hourly_report(forecast, display_timezone=zone)
            row["hourly_report_seconds"] = time.perf_counter() - clock
        except UnsupportedCoordinateError as exc:
            row.update(
                status="error", error={"code": "unsupported_coordinate", "message": str(exc)}
            )
        except CoverageRequiredError as exc:
            row.update(
                status="error",
                error={
                    "code": "coverage_required",
                    "message": (
                        f"{exc}; the snapshot covers only the refreshed collection's footprint"
                    ),
                },
            )
        except Exception as exc:
            row.update(
                status="error",
                error={"code": "forecast_failed", "message": f"{type(exc).__name__}: {exc}"},
            )
        else:
            row["forecast"] = forecast
            row["status"] = "ok"
            if issue:
                assert issuer is not None
                try:
                    clock = time.perf_counter()
                    issued = issuer.issue(forecast, batch_run_id=batch_run_id, location_index=index)
                    row["issuance_seconds"] = time.perf_counter() - clock
                except Exception:
                    row.update(
                        status="error",
                        error={
                            "code": "issuance_failed",
                            "message": "Could not persist this forecast; nothing was issued.",
                        },
                    )
                else:
                    row["issued"] = issued.model_dump(mode="json")
        rows.append(row)
    timings["total_seconds"] = time.perf_counter() - started
    result.update(
        status="ok",
        batch_run_id=str(batch_run_id) if issue else None,
        results=rows,
        timings=timings,
        summary={
            "ok": sum(row["status"] == "ok" for row in rows),
            "issued": sum("issued" in row for row in rows),
            "skipped": sum(row["status"] == "skipped_already_issued" for row in rows),
            "failed": sum(row["status"] == "error" for row in rows),
        },
    )
    return result


def _public(result: dict[str, Any]) -> dict[str, Any]:
    """The CLI summary omits the multi-megabyte forecast payloads."""
    rows = []
    for row in result.get("results", []):
        public = {key: value for key, value in row.items() if key != "forecast"}
        forecast = row.get("forecast")
        if forecast is not None:
            public["hours"] = len(forecast["hours"])
            public["first_valid_time"] = forecast["hours"][0]["valid_time"]
            public["last_valid_time"] = forecast["hours"][-1]["valid_time"]
            public["prepared_window"] = forecast.get("prepared_window")
        rows.append(public)
    return {**result, "results": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Guidance root with latest_complete"
    )
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--name", help="Optional display label for --lat/--lon")
    parser.add_argument("--config", type=Path, help="Locations JSON instead of --lat/--lon")
    parser.add_argument("--display-timezone", default="UTC")
    parser.add_argument(
        "--reference-time",
        type=datetime.fromisoformat,
        help="Explicit UTC reference hour (default: the current UTC hour)",
    )
    parser.add_argument("--issue", action="store_true", help="Also save immutable issuances")
    parser.add_argument("--reissue", action="store_true", help="Issue even if a version exists")
    parser.add_argument(
        "--output-dir", type=Path, help="Retain result.json and reports outside Git"
    )
    args = parser.parse_args(argv)
    if (args.config is None) == (args.lat is None or args.lon is None):
        parser.error("Provide either --config or both --lat and --lon")
    locations = (
        load_locations(args.config)
        if args.config is not None
        else [
            {
                "lat": args.lat,
                "lon": args.lon,
                **({"name": args.name} if args.name else {}),
                "display_timezone": args.display_timezone,
            }
        ]
    )
    if args.output_dir is not None and args.output_dir.resolve().is_relative_to(_ROOT):
        parser.error("--output-dir must lie outside the repository")
    try:
        result = forecast_from_snapshot(
            args.root,
            locations,
            reference_time=args.reference_time,
            display_timezone=args.display_timezone,
            issue=args.issue,
            reissue=args.reissue,
        )
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "forecast_failed", "message": str(exc)}}), file=sys.stderr
        )
        return 2
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "result.json").write_text(
            json.dumps(result, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8"
        )
        lines = ["# Forecast from prepared snapshot", ""]
        for row in result.get("results", []):
            if row.get("status") in ("ok", "skipped_already_issued") and "forecast" in row:
                lines += [f"## Location {row['index']}: {json.dumps(row['location'])}", ""]
                if "issued" in row:
                    lines += [f"Issued forecast: {row['issued']['issued_forecast_id']}", ""]
                lines += [render_hourly_report(row["forecast"]["hourly_report"]), ""]
        (args.output_dir / "hourly-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(_public(result), indent=2, default=str))
    if result["status"] == "no_current_snapshot":
        return 3
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
