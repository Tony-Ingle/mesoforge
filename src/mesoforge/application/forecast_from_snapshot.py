"""Development/replay forecasts from a prepared snapshot; no provider access.

The request picks its own reference hour (the current UTC hour), checks that the
snapshot's absolute valid times cover hours 1..36 after it, builds the current
MesoForge baseline grid with the existing field policies and returns the 36-hour
point forecast. Nothing here discovers, downloads or prepares guidance, and an
insufficient snapshot is reported as ``no_current_snapshot`` rather than shortened.
Normal configured-location operation uses ``forecast_from_baseline`` instead.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from functools import partial
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
from mesoforge.application.issuance import (
    FORWARD_RUN_LOCK,
    ForecastIssuanceService,
    acquire_issuance_run_lock,
)
from mesoforge.application.point_forecast import ReferenceCoverageError
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    check_information_cutoff,
    coverage_for,
    derive_reference_time,
    load_preparation,
    resolve_latest_complete,
    source_information,
    verify_prepared_run,
)
from mesoforge.application.spatial_coverage import (
    CoverageRequiredError,
    UnsupportedCoordinateError,
    validate_coordinate,
)
from mesoforge.application.weather_transitions import validate_display_timezone
from mesoforge.contracts.policy_governance import GovernanceBlockedError

_ROOT = Path(__file__).resolve().parents[3]
PROVENANCE_RULE = (
    "Source discovery cutoffs are independent; attachments do not inherit the original "
    "model decision cutoff. Issuance requires retained availability and acquisition "
    "evidence at or before the forecast analysis cutoff; source cycles may differ."
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
    run_lock: Callable[[], AbstractContextManager[None]] | None = None,
) -> dict[str, Any]:
    """Serve every location from one snapshot load; failures stay per location."""
    validate_display_timezone(display_timezone)
    requested_at = request_time or datetime.now(UTC)
    if requested_at.tzinfo is None:
        raise ValueError("Request time must include a timezone")
    requested_at = requested_at.astimezone(UTC)
    derived = derive_reference_time(requested_at)
    if reference_time is not None and reference_time.tzinfo is None:
        raise ValueError("Reference time must include a timezone")
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
        information = source_information(preparation)
        if "source_information" in manifest and manifest["source_information"] != information:
            raise SnapshotError("Retained source information differs from the snapshot manifest")
    except (SnapshotError, OSError, KeyError, ValueError) as exc:
        result.update(status="no_current_snapshot", reason=str(exc), results=[])
        return result
    timings["resolve_and_verify_seconds"] = time.perf_counter() - started
    cutoff_problems = check_information_cutoff(
        information,
        analysis_cutoff=requested_at,
        published_at=pointer["published_at"],
        completed_at=manifest.get("completed_at"),
    )
    result["information_cutoff"] = {
        "forecast_analysis_cutoff": result["request_time"],
        "status": "proven" if not cutoff_problems else "unproven",
        "limitations": cutoff_problems,
        "source_information": information,
    }
    if issue and cutoff_problems:
        result.update(
            status="no_current_snapshot",
            reason="Input availability at the forecast analysis cutoff cannot be proven",
            results=[],
        )
        return result
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
    lineage = {
        "prepared_snapshot": {
            "snapshot_id": manifest["snapshot_id"],
            "issuance_mode": ("explicit_reissue" if reissue else "primary") if issue else None,
            "published_at": pointer["published_at"],
            "manifest_sha256": pointer["manifest_sha256"],
            "prepared_reference_time": manifest["coverage"]["reference_time"],
            "decision_time": manifest["coverage"]["decision_time"],
            "request_time": result["request_time"],
            "forecast_analysis_cutoff": result["request_time"],
            "information_cutoff": result["information_cutoff"],
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
    }
    result.update(
        _deliver_locations(
            view,
            locations,
            reference_time=reference,
            display_timezone=display_timezone,
            issue=issue,
            issuer=issuer,
            reissue=reissue,
            run_lock=run_lock,
            lineage=lineage,
            build_timing_key="local_grid_build_seconds",
        )
    )
    timings["total_seconds"] = time.perf_counter() - started
    result["timings"] = timings
    return result


def _deliver_locations(
    view: Any,
    locations: list[Any],
    *,
    reference_time: datetime,
    display_timezone: str,
    issue: bool,
    issuer: ForecastIssuanceService | None,
    reissue: bool,
    run_lock: Callable[[], AbstractContextManager[None]] | None,
    lineage: dict[str, dict[str, Any]],
    build_timing_key: str,
    stage_processor: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]
    | None = None,
    stage_binding: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deliver pinned forecasts with one shared location-isolation/issuance boundary."""
    result: dict[str, Any] = {}
    if issue and issuer is None:
        issuer = create_issuer()
    batch_run_id = uuid4()
    rows: list[dict[str, Any]] = []
    # The storage lock serializes the decision-window lookup and, separately, the
    # recheck-and-publish step. The bounded learning/AI stage (minutes per location)
    # runs between them without holding a database session lock. A concurrent
    # process that published meanwhile is detected by the locked recheck.
    lock_factory = (run_lock or partial(acquire_issuance_run_lock, wait=True)) if issue else None

    def locked() -> AbstractContextManager[None]:
        return lock_factory() if lock_factory is not None else nullcontext()

    if issue:
        result["issuance_guard"] = {
            "mechanism": "postgresql_session_advisory_lock",
            "lock_key": str(FORWARD_RUN_LOCK),
            "behavior": "locked_lookup_unlocked_build_locked_recheck_and_publish",
            "explicit_reissue": reissue,
        }

    def skipped_row(row: dict[str, Any], existing: Sequence[Any]) -> None:
        row.pop("forecast", None)
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
            try:
                with locked():
                    existing = issuer.find_versions(
                        latitude=latitude,
                        longitude=longitude,
                        target_reference_time=reference_time,
                    )
            except Exception:
                row.update(
                    status="error",
                    error={
                        "code": "issuance_lookup_failed",
                        "message": "Could not check existing issuances for this location; "
                        "nothing was issued.",
                    },
                )
                rows.append(row)
                continue
            if existing and not reissue:
                skipped_row(row, existing)
                rows.append(row)
                continue
        try:
            clock = time.perf_counter()
            forecast = view.forecast(latitude=latitude, longitude=longitude)
            row[build_timing_key] = time.perf_counter() - clock
            forecast.update({key: dict(value) for key, value in lineage.items()})
            if stage_processor is not None:
                try:
                    forecast, row["learning"] = stage_processor(forecast)
                except GovernanceBlockedError:
                    # Governed state failed or was revoked: nothing issues for this row.
                    raise
                except Exception as exc:
                    # The complete pinned numerical baseline survives learning failure.
                    row["learning"] = {"status": "fallback", "reason": str(exc)}
                    forecast = {**forecast, "learning_failure": row["learning"]}
            clock = time.perf_counter()
            forecast["hourly_report"] = build_hourly_report(forecast, display_timezone=zone)
            row["hourly_report_seconds"] = time.perf_counter() - clock
        except GovernanceBlockedError as exc:
            row.pop("learning", None)
            row.update(status="error", error={"code": exc.code, "message": str(exc)})
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
                issued = None
                try:
                    with locked():
                        recheck = (
                            []
                            if reissue
                            else issuer.find_versions(
                                latitude=latitude,
                                longitude=longitude,
                                target_reference_time=reference_time,
                            )
                        )
                        if not recheck:
                            clock = time.perf_counter()
                            issued = issuer.issue(
                                forecast, batch_run_id=batch_run_id, location_index=index
                            )
                            row["issuance_seconds"] = time.perf_counter() - clock
                except GovernanceBlockedError as exc:
                    row.update(status="error", error={"code": exc.code, "message": str(exc)})
                except Exception:
                    row.update(
                        status="error",
                        error={
                            "code": "issuance_failed",
                            "message": "Could not persist this forecast; nothing was issued.",
                        },
                    )
                else:
                    if issued is None:
                        # Another process published this decision window while this
                        # one built; its immutable version stands.
                        skipped_row(row, recheck)
                        rows.append(row)
                        continue
                    row["issued"] = issued.model_dump(mode="json")
                    if stage_binding is not None and "learning" in row:
                        try:
                            row["learning"]["binding"] = stage_binding(
                                row["issued"], row["learning"]
                            )
                        except Exception as exc:
                            row["learning"]["binding_failure"] = str(exc)
        rows.append(row)
    result.update(
        status="ok",
        batch_run_id=str(batch_run_id) if issue else None,
        results=rows,
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


def _write_outputs(result: dict[str, Any], output_dir: Path, *, title: str) -> None:
    """Retain the complete result and readable report for either pinned input path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8"
    )
    lines = [f"# {title}", ""]
    for row in result.get("results", []):
        if row.get("status") in ("ok", "skipped_already_issued") and "forecast" in row:
            lines += [f"## Location {row['index']}: {json.dumps(row['location'])}", ""]
            if "issued" in row:
                lines += [f"Issued forecast: {row['issued']['issued_forecast_id']}", ""]
            lines += [render_hourly_report(row["forecast"]["hourly_report"]), ""]
    (output_dir / "hourly-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        _write_outputs(result, args.output_dir, title="Forecast from prepared snapshot")
    print(json.dumps(_public(result), indent=2, default=str))
    if result["status"] == "no_current_snapshot":
        return 3
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
