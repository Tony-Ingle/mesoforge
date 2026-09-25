"""Configured-location forecasts from one pinned, already blended baseline snapshot.

This path reads the saved MesoForge numerical forecast; it does not load native
guidance, blend fields, discover model providers or prepare model data. Before
issuance, independent prior-temperature/QPF verification may acquire observations.
An uncovered reference
or coordinate requires a new background baseline build, not on-request blending.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.forecast_from_snapshot import _deliver_locations, _public, _write_outputs
from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_snapshot import SnapshotError, derive_reference_time
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.application.weather_transitions import validate_display_timezone

_ROOT = Path(__file__).resolve().parents[3]


def forecast_from_baseline(
    root: Path,
    locations: list[Any],
    *,
    baseline_pointer: dict[str, Any] | None = None,
    reference_time: datetime | None = None,
    request_time: datetime | None = None,
    display_timezone: str = "UTC",
    issue: bool = False,
    issuer: ForecastIssuanceService | None = None,
    reissue: bool = False,
    run_lock: Callable[[], AbstractContextManager[None]] | None = None,
    verify_prior: bool = True,
    verification_runner: Callable[[float, float], dict[str, Any]] | None = None,
    qpf_lookback_hours: int = 72,
    qpf_max_opportunities: int = 36,
) -> dict[str, Any]:
    """Pin one complete baseline once, then isolate each location's delivery."""
    from mesoforge.application.baseline_snapshot import load_baseline

    validate_display_timezone(display_timezone)
    requested_at = request_time or datetime.now(UTC)
    if requested_at.tzinfo is None or requested_at.utcoffset() is None:
        raise ValueError("Request time must include a timezone")
    requested_at = requested_at.astimezone(UTC)
    derived = derive_reference_time(requested_at)
    if reference_time is not None and (
        reference_time.tzinfo is None or reference_time.utcoffset() is None
    ):
        raise ValueError("Reference time must include a timezone")
    reference = derived if reference_time is None else reference_time.astimezone(UTC)
    if reference.minute or reference.second or reference.microsecond:
        raise ValueError("Reference time must be an exact UTC hour")
    if issue and reference > derived:
        raise ValueError("Issuance cannot use a reference hour after the request hour")
    started = time.perf_counter()
    result: dict[str, Any] = {
        "request_time": requested_at.isoformat().replace("+00:00", "Z"),
        "reference_time": reference.isoformat().replace("+00:00", "Z"),
        "reference_time_source": "request_hour" if reference_time is None else "explicit",
        "network_calls": 0,
        "network_calls_scope": "model_guidance_only; observation attempts reported separately",
        "numerical_blend_execution": "background_baseline_only",
    }
    timings: dict[str, float] = {}
    try:
        pinned = (
            load_baseline(root)
            if baseline_pointer is None
            else load_baseline(root, pointer=baseline_pointer)
        )
        pointer, manifest = pinned.pointer, pinned.manifest
        for label, value in (
            ("baseline analysis cutoff", manifest["analysis_cutoff"]),
            ("baseline build", manifest["built_at"]),
            ("baseline completion", manifest["completed_at"]),
            ("baseline publication", pointer["published_at"]),
        ):
            instant = datetime.fromisoformat(value)
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise SnapshotError(f"{label} lacks a timezone")
            if instant > requested_at:
                raise SnapshotError(f"{label} follows forecast analysis cutoff")
        if manifest["information_cutoff"]["status"] != "proven":
            raise SnapshotError("Baseline input availability is not proven")
        view = pinned.reference_view(reference)
    except (SnapshotError, OSError, KeyError, ValueError) as exc:
        result.update(status="no_current_baseline", reason=str(exc), results=[])
        result["timings"] = {"total_seconds": time.perf_counter() - started}
        return result
    timings["baseline_load_and_verify_seconds"] = time.perf_counter() - started
    prepared = manifest["prepared_snapshot"]
    baseline_lineage = {
        "baseline_snapshot_id": manifest["baseline_snapshot_id"],
        "schema_version": manifest["schema_version"],
        "directory": str(pinned.directory),
        "manifest_sha256": pointer["manifest_sha256"],
        "prepared_snapshot_id": prepared["snapshot_id"],
        "background_analysis_cutoff": manifest["analysis_cutoff"],
        "built_at": manifest["built_at"],
        "completed_at": manifest["completed_at"],
        "published_at": pointer["published_at"],
        "forecast_analysis_cutoff": result["request_time"],
        "reference_time": result["reference_time"],
        "reference_time_source": result["reference_time_source"],
        "field_policies": manifest["field_policies"],
        "information_cutoff": manifest["information_cutoff"],
        "issuance_mode": ("explicit_reissue" if reissue else "primary") if issue else None,
    }
    prepared_lineage = {
        **prepared,
        "request_time": result["request_time"],
        "forecast_analysis_cutoff": result["request_time"],
        "reference_time": result["reference_time"],
        "reference_time_source": result["reference_time_source"],
        "information_evidence": {
            "baseline_snapshot_id": manifest["baseline_snapshot_id"],
            **manifest["information_cutoff"],
        },
        "issuance_mode": baseline_lineage["issuance_mode"],
    }
    result["baseline"] = baseline_lineage
    verification: dict[int, dict[str, Any]] = {}
    if issue and verify_prior:
        from mesoforge.application.forward_verification import verify_previous_fields

        for index, location in enumerate(locations):
            try:
                lat, lon = _coordinates(location)
                validate_coordinate(lat, lon)
                verification[index] = (
                    verification_runner(lat, lon)
                    if verification_runner is not None
                    else verify_previous_fields(
                        lat,
                        lon,
                        now=requested_at,
                        qpf_lookback_hours=qpf_lookback_hours,
                        qpf_max_opportunities=qpf_max_opportunities,
                    )
                )
            except Exception as exc:
                verification[index] = {"status": "error", "retryable": True, "reason": str(exc)}
        # Observation work is outside the issuance lock and cannot gate delivery.
        # One immutable baseline stays pinned throughout all of these attempts.
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
            lineage={"baseline_snapshot": baseline_lineage, "prepared_snapshot": prepared_lineage},
            build_timing_key="baseline_extraction_seconds",
        )
    )
    for row in result["results"]:
        if row["index"] in verification:
            row["previous_verification"] = verification[row["index"]]
    timings["total_seconds"] = time.perf_counter() - started
    result["timings"] = timings
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="Baseline root with latest_baseline"
    )
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--name", help="Optional display label for --lat/--lon")
    parser.add_argument("--config", type=Path, help="Locations JSON instead of --lat/--lon")
    parser.add_argument("--display-timezone", default="UTC")
    parser.add_argument(
        "--reference-time",
        type=datetime.fromisoformat,
        help="Explicit covered UTC reference hour (default: the current UTC hour)",
    )
    parser.add_argument("--issue", action="store_true", help="Also save immutable issuances")
    parser.add_argument("--reissue", action="store_true", help="Issue even if a version exists")
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="Explicit issuance-only replay; no prior observation work",
    )
    parser.add_argument(
        "--qpf-lookback-hours",
        type=int,
        default=72,
        help="Bound automatic prior QPF valid-hour lookup (default 72)",
    )
    parser.add_argument(
        "--qpf-max-opportunities",
        type=int,
        default=36,
        help="Per-location unresolved QPF stage/hour work cap",
    )
    parser.add_argument(
        "--output-dir", type=Path, help="Retain result.json and reports outside Git"
    )
    args = parser.parse_args(argv)
    if (args.config is None) == (args.lat is None or args.lon is None):
        parser.error("Provide either --config or both --lat and --lon")
    locations = (
        load_locations(args.config)
        if args.config is not None
        else [{"lat": args.lat, "lon": args.lon, **({"name": args.name} if args.name else {})}]
    )
    if args.output_dir is not None and args.output_dir.resolve().is_relative_to(_ROOT):
        parser.error("--output-dir must lie outside the repository")
    try:
        result = forecast_from_baseline(
            args.root,
            locations,
            reference_time=args.reference_time,
            display_timezone=args.display_timezone,
            issue=args.issue,
            reissue=args.reissue,
            verify_prior=not args.skip_verification,
            qpf_lookback_hours=args.qpf_lookback_hours,
            qpf_max_opportunities=args.qpf_max_opportunities,
        )
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "forecast_failed", "message": str(exc)}}), file=sys.stderr
        )
        return 2
    if args.output_dir is not None:
        _write_outputs(result, args.output_dir, title="Forecast from MesoForge baseline")
    print(json.dumps(_public(result), indent=2, default=str))
    if result["status"] == "no_current_baseline":
        return 3
    return 1 if result["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
