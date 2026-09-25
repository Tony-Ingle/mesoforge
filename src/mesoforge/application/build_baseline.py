"""Build and publish MesoForge numerical baselines from pinned prepared guidance.

On-demand background work only: no discovery, provider access, or issuance. Exact
configured domains and usable reference-hour views preserve the current science.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.baseline_codec import CompactCodec
from mesoforge.application.baseline_snapshot import (
    BASELINE_SCHEMA,
    BASELINES_DIRECTORY,
    publish_latest_baseline,
    write_artifact,
    write_manifest,
)
from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    check_information_cutoff,
    coverage_for,
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
from mesoforge.forecasting.coherence import (
    collect_baseline_coherence,
    framework_metadata,
)
from mesoforge.forecasting.field_blend import FIELD_REGISTRY

_ROOT = Path(__file__).resolve().parents[3]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _references(manifest: dict[str, Any]) -> list[datetime]:
    first = datetime.fromisoformat(manifest["coverage"]["reference_time"])
    last = datetime.fromisoformat(manifest["coverage"]["last_valid_time"]) - timedelta(hours=36)
    references = []
    while first <= last:
        if coverage_for(manifest, first)["usable"]:
            references.append(first)
        first += timedelta(hours=1)
    return references


def _availability(grid: dict[str, Any], reference: datetime) -> dict[str, Any]:
    """Validate structural time coverage; missing weather is retained, not repaired."""
    expected = [_iso(reference + timedelta(hours=hour)) for hour in range(1, 37)]
    counts: dict[str, dict[str, int]] = {}
    geometry = grid["geometry"]
    if len(grid["cells"]) != len(geometry["x_m"]) * len(geometry["y_m"]):
        raise SnapshotError("Calculated baseline has incomplete spatial cells")
    expected_fields = set(grid["cells"][0]["hours"][0].get("surface", {}).get("fields", {}))
    required = {
        "air_temperature_2m",
        "dew_point_temperature_2m",
        "relative_humidity_2m",
        "eastward_wind_10m",
        "northward_wind_10m",
        "wind_speed_10m",
        "wind_from_direction_10m",
        "wind_gust_10m",
    }
    if not required <= expected_fields:
        raise SnapshotError("Calculated baseline lacks the current surface canvas")
    for cell in grid["cells"]:
        if [hour["valid_time"] for hour in cell["hours"]] != expected:
            raise SnapshotError("Calculated baseline has incomplete or unordered valid times")
        for hour in cell["hours"]:
            fields = hour.get("surface", {}).get("fields", {})
            if set(fields) != expected_fields:
                raise SnapshotError("Calculated baseline has inconsistent field coverage")
            for field, value in fields.items():
                row = counts.setdefault(field, {"available": 0, "missing": 0})
                # A valid fallback can retain exclusion reasons. They describe its
                # evidence, not absence of the delivered numerical value.
                missing = value.get("value") is None or value.get("status") == "unavailable"
                row["missing" if missing else "available"] += 1
    return counts


def _source_documents(information: dict[str, Any]) -> list[dict[str, str]]:
    documents = {}
    for source in information["sources"]:
        for row in [source, *source.get("region_manifests", [])]:
            documents[row["manifest_file"]] = {
                "path": row["manifest_file"],
                "sha256": row["manifest_sha256"],
            }
    return list(documents.values())


def build_baseline(
    guidance_root: Path,
    baseline_root: Path,
    locations: list[Any],
    *,
    prepared_pointer: dict[str, Any] | None = None,
    analysis_cutoff: datetime | None = None,
    reference_times: list[datetime] | None = None,
) -> dict[str, Any]:
    """Pin contributors once; materialize all requested domains before publication.

    Default views cover every usable prepared reference hour. This matters because
    current lead-band weights depend on that reference; slicing a single blended
    view would silently change the forecast. Explicit views are for bounded replay.
    """
    if baseline_root.resolve().is_relative_to(_ROOT):
        raise ValueError("Baseline artifacts must remain outside the repository")
    started = time.perf_counter()
    built = datetime.now(UTC)
    cutoff = analysis_cutoff or built
    if cutoff.tzinfo is None or cutoff.utcoffset() is None or cutoff > built:
        raise ValueError("Background analysis cutoff must be timezone-aware and not in the future")
    pointer, prepared_manifest, prepared_directory = (
        resolve_latest_complete(guidance_root)
        if prepared_pointer is None
        else resolve_latest_complete(guidance_root, pointer=prepared_pointer)
    )
    preparation = verify_prepared_run(prepared_manifest)
    information = source_information(preparation)
    if prepared_manifest.get("source_information", information) != information:
        raise SnapshotError("Retained source information differs from the prepared snapshot")
    problems = check_information_cutoff(
        information,
        analysis_cutoff=cutoff,
        published_at=pointer["published_at"],
        completed_at=prepared_manifest.get("completed_at"),
    )
    if problems:
        raise SnapshotError(
            "Background input availability cannot be proven: " + "; ".join(problems)
        )
    references = sorted(
        set(reference_times if reference_times is not None else _references(prepared_manifest))
    )
    if not references:
        raise SnapshotError("Prepared state has no complete 36-hour reference view")
    for reference in references:
        coverage = coverage_for(prepared_manifest, reference)
        if not coverage["usable"]:
            raise SnapshotError(coverage["reason"])
    coordinates: list[tuple[float, float]] = []
    failures = []
    for index, location in enumerate(locations):
        try:
            coordinate = _coordinates(location)
            validate_coordinate(*coordinate)
        except ValueError as exc:
            failures.append({"index": index, "location": location, "reason": str(exc)})
        else:
            if coordinate not in coordinates:
                coordinates.append(coordinate)
    if not coordinates:
        raise SnapshotError("No valid configured coordinates for a baseline build")
    timings = {"contributor_validation_seconds": time.perf_counter() - started}
    clock = time.perf_counter()
    prepared = load_preparation(preparation)
    timings["contributor_load_seconds"] = time.perf_counter() - clock
    clock = time.perf_counter()
    codec = CompactCodec(_source_documents(information))
    timings["evidence_index_seconds"] = time.perf_counter() - clock
    identity = f"{built:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
    directory = baseline_root / BASELINES_DIRECTORY / identity
    directory.mkdir(parents=True, exist_ok=False)
    domains: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    build_seconds = persist_seconds = coherence_seconds = blend_seconds = 0.0
    for reference in references:
        view = prepared.reference_view(reference)
        for latitude, longitude in coordinates:
            clock = time.perf_counter()
            try:
                with collect_baseline_coherence() as coherence:
                    forecast = view.forecast(latitude=latitude, longitude=longitude)
            except (CoverageRequiredError, UnsupportedCoordinateError) as exc:
                failures.append(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "reference_time": _iso(reference),
                        "reason": str(exc),
                    }
                )
                continue
            build_seconds += time.perf_counter() - clock
            clock = time.perf_counter()
            grid = forecast["local_grid_baseline"]
            availability = _availability(grid, reference)
            # Missing peripheral coverage remains ordinary missingness. Every
            # actually calculated cell/hour must complete current required rules;
            # cached/replayed payloads cannot masquerade as a new coherent build.
            coherence.validate(
                sum(len(cell["hours"]) for cell in grid["cells"] if cell["status"] == "calculated")
            )
            coherence_seconds += coherence.coherence_seconds
            blend_seconds += coherence.blend_seconds
            artifact = write_artifact(
                directory, f"domain-{len(domains):04d}.json.gz", codec.encode(grid)
            )
            artifacts.append(artifact)
            domains.append(
                {
                    "latitude": latitude,
                    "longitude": longitude,
                    "reference_time": _iso(reference),
                    "first_valid_time": _iso(reference + timedelta(hours=1)),
                    "last_valid_time": _iso(reference + timedelta(hours=36)),
                    "geometry": grid["geometry"],
                    "grid_version": grid["version"],
                    "grid_policy": grid["policy"],
                    "grid_sha256": forecast["local_grid"]["sha256"],
                    "field_availability": availability,
                    "coherence": coherence.report(),
                    "transformation": grid["transformation"],
                    "artifact": artifact,
                }
            )
            persist_seconds += time.perf_counter() - clock
            del forecast, grid
    if not domains:
        raise SnapshotError("No configured domain could be built; previous baseline retained")
    clock = time.perf_counter()
    metadata = write_artifact(directory, "metadata.json.gz", codec.export_tables())
    proof = write_artifact(directory, "information.json.gz", information)
    artifacts.extend([metadata, proof])
    # Recheck identities after calculation before sealing the immutable manifest.
    if (
        verify_prepared_run(prepared_manifest) != preparation
        or source_information(preparation) != information
    ):
        raise SnapshotError("Prepared source evidence changed during background build")
    package = Path(__file__).resolve().parents[1]
    producer = {
        name: hashlib.sha256((package / name).read_bytes()).hexdigest()
        for name in (
            "application/build_baseline.py",
            "application/baseline_snapshot.py",
            "application/baseline_codec.py",
        )
    }
    manifest = {
        "schema_version": BASELINE_SCHEMA,
        "baseline_snapshot_id": identity,
        "kind": "mesoforge_numerical_baseline",
        "built_at": _iso(built),
        "analysis_cutoff": _iso(cutoff),
        "completed_at": _iso(datetime.now(UTC)),
        "prepared_snapshot": {
            "snapshot_id": prepared_manifest["snapshot_id"],
            "directory": str(prepared_directory),
            "manifest_sha256": pointer["manifest_sha256"],
            "published_at": pointer["published_at"],
            "completed_at": prepared_manifest.get("completed_at"),
            "coverage": prepared_manifest["coverage"],
            "contributor_cycles": {
                model: row.get("cycle")
                for model, row in prepared_manifest["contributors"].items()
                if "cycle" in row
            },
            "nbm_product_cycles": {
                name: row.get("cycle")
                for name, row in prepared_manifest["contributors"]["NBM"]["products"].items()
            },
            "field_policies": prepared_manifest["field_policies"],
        },
        "coverage": {
            "representation": "exact_configured_domains_and_reference_views",
            "reference_times": sorted({row["reference_time"] for row in domains}),
            "uncovered_behavior": "background_rebuild_required_no_request_blending",
            "failed_locations": failures,
        },
        "domains": domains,
        "field_policies": prepared_manifest["field_policies"],
        "field_registry": {name: asdict(row) for name, row in FIELD_REGISTRY.items()},
        "coherence_and_derivation": {
            **framework_metadata(),
            "status": "passed",
            "scope": "current_approved_rules_only",
            "execution_reports": "domains[].coherence",
            "field_evidence": "cells[].hours[].surface.fields/source_validation",
        },
        "producer_source_sha256": producer,
        "information_cutoff": {
            "status": "proven",
            "analysis_cutoff": _iso(cutoff),
            "source_information_file": proof,
            "rule": information["rule"],
        },
        "metadata_file": metadata,
        "artifacts": artifacts,
        "completeness": {
            "status": "complete",
            "meaning": "All declared grids and 36-hour views saved; "
            "field missingness remains explicit",
        },
    }
    _, digest = write_manifest(directory, manifest)
    persist_seconds += time.perf_counter() - clock
    timings.update(
        baseline_build_seconds=build_seconds,
        field_blend_kernel_seconds=blend_seconds,
        coherence_seconds=coherence_seconds,
        serialization_persistence_seconds=persist_seconds,
    )
    clock = time.perf_counter()
    published = publish_latest_baseline(
        baseline_root, manifest, digest, published_at=datetime.now(UTC)
    )
    timings["publication_seconds"] = time.perf_counter() - clock
    timings["total_seconds"] = time.perf_counter() - started
    return {
        "status": "published",
        "baseline_snapshot_id": identity,
        "directory": str(directory),
        "pointer": published,
        "manifest": manifest,
        "timings": timings,
        "artifact_bytes": sum(row["bytes"] for row in artifacts),
        "network_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guidance-root", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Configured lat/lon collection")
    parser.add_argument(
        "--reference-time",
        type=datetime.fromisoformat,
        action="append",
        help="Limit materialization to these exact reference hours (default: all covered hours)",
    )
    args = parser.parse_args(argv)
    try:
        result = build_baseline(
            args.guidance_root,
            args.baseline_root,
            load_locations(args.config),
            reference_times=args.reference_time,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "previous_baseline": "retained",
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({key: value for key, value in result.items() if key != "manifest"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
