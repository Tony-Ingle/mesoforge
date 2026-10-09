"""Background guidance refresh: discover, prepare, validate and publish one snapshot.

This is the slow path. It runs on its own clock, reuses the existing discovery,
acquisition and preparation functions, and ends by atomically publishing a small
``latest_complete`` pointer. Forecast requests never call it; a failed refresh leaves
the previous good snapshot published and retains its own failure evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.current_model_set import select_model_set
from mesoforge.application.point_forecast import _iso as _iso64
from mesoforge.application.prepared_cloud import prepare_cloud_run
from mesoforge.application.prepared_precipitation_type import prepare_type_run
from mesoforge.application.prepared_probability_sources import prepare_native_probability_sources
from mesoforge.application.prepared_snapshot import (
    SnapshotError,
    build_snapshot_manifest,
    load_preparation,
    publish_latest_complete,
    read_pointer,
    snapshot_directory,
    write_manifest,
)
from mesoforge.application.prepared_temperature import BoundedHttpTransport
from mesoforge.application.prepared_thunder import prepare_thunder_run
from mesoforge.application.prepared_visibility import prepare_visibility_run
from mesoforge.application.selected_forecast import prepare_selected
from mesoforge.application.spatial_coverage import (
    CoverageRequiredError,
    UnsupportedCoordinateError,
    validate_coordinate,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.horizon import LEGACY_HORIZON, ForecastHorizon, horizon_for
from mesoforge.common.identifiers import PreparedSnapshotId
from mesoforge.guidance.coverage import MAXIMUM_PREPARED_HOURS
from mesoforge.guidance.runtime import SystemClock, SystemSleeper

_ROOT = Path(__file__).resolve().parents[3]


class RefreshError(RuntimeError):
    """The refresh could not produce a complete validated snapshot."""


@dataclass(frozen=True)
class RefreshSteps:
    """The provider-facing steps, injectable so the flow can be tested offline."""

    discover: Callable[[Path], dict[str, Any]]
    prepare: Callable[[list[Any], Path, Path], dict[str, Any]]
    attach_ptype: Callable[[Path, Path], dict[str, Any]]
    attach_cloud: Callable[[Path, Path], dict[str, Any]]
    attach_thunder: Callable[[Path, Path], dict[str, Any]]
    attach_visibility: Callable[[Path, Path], dict[str, Any]] | None = None
    attach_probabilities: Callable[[Path, Path], dict[str, Any]] | None = None


def default_steps(
    *, coverage_hours: int, forecast_horizon: ForecastHorizon = LEGACY_HORIZON
) -> RefreshSteps:
    def discover(directory: Path) -> dict[str, Any]:
        configuration, _ = load_configuration_source(
            base_path=_ROOT / "configs/base.yaml",
            additional_overlay_paths=(
                _ROOT / "configs/phase1-grasston.yaml",
                _ROOT / "configs/phase2-grasston.yaml",
            ),
        )
        assert configuration.phase2 is not None
        transport = BoundedHttpTransport()
        horizon_options: dict[str, Any] = (
            {"forecast_horizon": forecast_horizon} if forecast_horizon.duration_hours == 120 else {}
        )
        try:
            return select_model_set(
                directory,
                configuration=configuration.phase2,
                transport=transport,
                clock=SystemClock(),
                sleeper=SystemSleeper(),
                surface_fields=True,
                qpf_fields=True,
                coverage_hours=coverage_hours,
                require_complete_shadows=False,
                **horizon_options,
            )
        finally:
            transport.close()

    return RefreshSteps(
        discover=discover,
        prepare=lambda locations, selection, out: prepare_selected(
            locations, selection, out, include_pop=True, require_complete_shadows=False
        ),
        attach_ptype=prepare_type_run,
        attach_cloud=prepare_cloud_run,
        attach_thunder=prepare_thunder_run,
        attach_visibility=prepare_visibility_run,
        attach_probabilities=(
            prepare_native_probability_sources if forecast_horizon.duration_hours == 120 else None
        ),
    )


class _Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []

    def run(self, name: str, function: Callable[[], Any], *, optional: bool = False) -> Any:
        started = time.perf_counter()
        entry: dict[str, Any] = {"step": name, "started_at": datetime.now(UTC).isoformat()}
        self.entries.append(entry)
        try:
            result = function()
        except Exception as exc:
            entry.update(
                status="error",
                seconds=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
            self.save()
            if optional:
                return None
            raise
        entry.update(status="ok", seconds=time.perf_counter() - started)
        self.save()
        return result

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.entries, indent=2, default=str) + "\n", encoding="utf-8"
        )

    def public(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "traceback"} for row in self.entries]


def refresh_guidance(
    config_path: Path,
    root: Path,
    *,
    coverage_hours: int = MAXIMUM_PREPARED_HOURS,
    steps: RefreshSteps | None = None,
    include_visibility: bool = True,
    forecast_horizon: ForecastHorizon = LEGACY_HORIZON,
) -> dict[str, Any]:
    """Prepare everything the current policies need for the collection, then publish.

    Coverage is the collection's footprint: an ad-hoc request later must lie inside it.
    Publication happens only after the finished preparation loads offline and covers the
    reference window for the active contributors.
    """
    locations = load_locations(config_path)
    for location in locations:
        validate_coordinate(*_coordinates(location))
    root = root.resolve()
    if root.is_relative_to(_ROOT):
        raise ValueError("Keep the guidance root outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    snapshot_id = PreparedSnapshotId(f"{started:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    directory = snapshot_directory(root, snapshot_id)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "locations.json").write_text(
        json.dumps({"locations": locations}, indent=2) + "\n", encoding="utf-8"
    )
    log = _Log(directory / "refresh-log.json")
    steps = steps or default_steps(
        coverage_hours=coverage_hours,
        **(
            {"forecast_horizon": forecast_horizon} if forecast_horizon.duration_hours == 120 else {}
        ),
    )
    before = read_pointer(root)
    result: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "directory": str(directory),
        "started_at": started.isoformat(),
        "previous_latest_complete": before,
    }
    try:
        selection = log.run("discover", lambda: steps.discover(directory / "selection"))
        if selection["status"] != "selected":
            raise RefreshError(selection.get("reason") or "No complete current model set")
        selection_path = directory / "selection" / "selection.json"
        result["selection"] = {
            key: selection.get(key)
            for key in (
                "decision_time",
                "target_reference_time",
                "selected_cycles",
                "horizon_hours",
                "coverage",
                "shadow_discovery_shortfalls",
            )
        }
        preparation = log.run(
            "prepare_selected_with_pop",
            lambda: steps.prepare(locations, selection_path, directory / "prepared"),
        )
        current = directory / "prepared"
        downloaded = int(preparation.get("downloaded_bytes", 0))
        attachments = (
            ("attach_precipitation_type", steps.attach_ptype, "ptype"),
            ("attach_cloud", steps.attach_cloud, "cloud"),
            ("attach_thunder", steps.attach_thunder, "thunder"),
        )
        for name, function, target in attachments:
            if target == "cloud" and horizon_for(selection).duration_hours == 120:
                continue
            attached = log.run(name, partial(function, current, directory / target))
            current = directory / target
            downloaded += _attachment_bytes(attached, target)
        visibility = steps.attach_visibility
        if include_visibility and visibility is not None:
            attached = log.run(
                "attach_visibility_evidence",
                partial(visibility, current, directory / "visibility"),
                optional=True,
            )
            if attached is not None:
                current = directory / "visibility"
                downloaded += _attachment_bytes(attached, "visibility")
        probabilities = steps.attach_probabilities
        if horizon_for(selection).duration_hours == 120 and probabilities is not None:
            attached = log.run(
                "attach_native_probability_evidence",
                partial(probabilities, current, directory / "probabilities"),
                optional=True,
            )
            if attached is not None:
                current = directory / "probabilities"
                downloaded += int(attached["probability_source_run"]["downloaded_bytes"])
        preparation_path = current / "preparation.json"
        final = json.loads(preparation_path.read_text(encoding="utf-8"))
        validation = log.run("validate", lambda: _validate(final, locations))
        completed = datetime.now(UTC)
        manifest = build_snapshot_manifest(
            snapshot_id=snapshot_id,
            root=root,
            preparation_path=preparation_path,
            selection_path=selection_path,
            steps=log.public(),
            downloaded_bytes=downloaded,
            clock=started,
            completed_at=completed,
        )
        manifest["validation"] = validation
        path, digest = write_manifest(directory, manifest)
        pointer = log.run(
            "publish_latest_complete",
            lambda: publish_latest_complete(root, manifest, digest, published_at=datetime.now(UTC)),
        )
        result.update(
            status="published",
            manifest_file=str(path),
            manifest_sha256=digest,
            latest_complete=pointer,
            coverage=manifest["coverage"],
            completeness=manifest["completeness"],
            nbm=manifest["contributors"]["NBM"]["products"]["probability_of_precipitation_1h"].get(
                "selection"
            ),
            downloaded_bytes=downloaded,
            steps=log.public(),
            seconds=(datetime.now(UTC) - started).total_seconds(),
        )
    except Exception as exc:
        failure = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "steps": log.public(),
            "latest_complete_unchanged": read_pointer(root) == before,
            "latest_complete": read_pointer(root),
            "seconds": (datetime.now(UTC) - started).total_seconds(),
        }
        (directory / "failure.json").write_text(
            json.dumps(failure, indent=2, default=str) + "\n", encoding="utf-8"
        )
        result.update(failure)
    (directory / "result.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return result


def _attachment_bytes(attached: dict[str, Any], target: str) -> int:
    key = {"ptype": "ptype_guidance", "cloud": "cloud_guidance", "thunder": "thunder_guidance"}.get(
        target, f"{target}_guidance"
    )
    return int((attached.get(key) or {}).get("downloaded_bytes", 0))


def _validate(preparation: dict[str, Any], locations: list[Any]) -> dict[str, Any]:
    """Load the finished preparation offline and prove it serves its own reference hour."""
    started = time.perf_counter()
    prepared = load_preparation(preparation)
    loaded = time.perf_counter() - started
    selection = preparation["current_model_set"]["selection"]
    reference = datetime.fromisoformat(selection["target_reference_time"])
    view = prepared.reference_view(reference)  # Raises when the active window is incomplete.
    held = {
        model: [_iso64(value) for value in values]
        for model, values in prepared.prepared_valid_times().items()
    }
    models = (
        tuple(selection["selected_cycles"])
        if horizon_for(selection).duration_hours == 120
        else ("HRRR", "GFS")
    )
    if horizon_for(selection).duration_hours == 120 and set(held) != set(models):
        raise SnapshotError("Prepared native sources differ from selected available sources")
    for model in models:
        if held[model] != list(selection["models"][model]["valid_times"]):
            raise SnapshotError(f"{model}: prepared valid times differ from the selection window")
    columns = []
    failures = []
    for index, location in enumerate(locations):
        latitude, longitude = _coordinates(location)
        clock = time.perf_counter()
        try:
            column = view.point_column(latitude=latitude, longitude=longitude)
        except (CoverageRequiredError, UnsupportedCoordinateError) as exc:
            failures.append(
                {
                    "index": index,
                    "location": location,
                    "code": "unsupported_coordinate"
                    if isinstance(exc, UnsupportedCoordinateError)
                    else "coverage_required",
                    "reason": str(exc),
                }
            )
            continue
        columns.append(
            {
                "location": location,
                "hours": len(column["hours"]),
                "seconds": time.perf_counter() - clock,
                "first_valid_time": column["hours"][0]["valid_time"],
                "last_valid_time": column["hours"][-1]["valid_time"],
            }
        )
        if len(column["hours"]) != horizon_for(selection).duration_hours:
            raise SnapshotError("Validation column does not hold the declared forecast horizon")
    if not columns:
        raise SnapshotError("No configured location has usable prepared coverage")
    return {
        "guidance_load_seconds": loaded,
        "reference_time": selection["target_reference_time"],
        "point_columns": columns,
        "failed_locations": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Coordinates defining coverage")
    parser.add_argument("--root", type=Path, required=True, help="Guidance root outside Git")
    parser.add_argument("--forecast-hours", type=int, choices=(36, 120), default=36)
    parser.add_argument(
        "--coverage-hours",
        type=int,
        default=None,
        help="Prepared hours: forecast duration through duration+6; default duration+6",
    )
    parser.add_argument(
        "--no-visibility", action="store_true", help="Skip the evidence-only visibility step"
    )
    args = parser.parse_args(argv)
    try:
        result = refresh_guidance(
            args.config,
            args.root,
            coverage_hours=args.coverage_hours or args.forecast_hours + 6,
            forecast_horizon=ForecastHorizon(args.forecast_hours),
            include_visibility=not args.no_visibility,
        )
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "refresh_failed", "message": str(exc)}}), file=sys.stderr
        )
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "published" else 2


if __name__ == "__main__":
    raise SystemExit(main())
