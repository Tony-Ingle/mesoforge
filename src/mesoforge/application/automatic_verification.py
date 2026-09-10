"""Acquire a bounded observation snapshot for saved past hours, then verify on demand."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.issuance import select_issued_forecast_hours
from mesoforge.application.issued_temperature_verification import configured_service
from mesoforge.application.prepared_observations import (
    acquire_for_valid_times,
    load_observation_configuration,
    prepare_bundle,
    retained_station_ids,
)
from mesoforge.application.spatial_coverage import (
    UnsupportedCoordinateError,
    validate_coordinate,
)
from mesoforge.catalog.configuration import compute_configuration_digest
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import Digest
from mesoforge.common.time import IntervalClosure, IntervalDefinition
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.verification.issued_temperature import forecast_eligibility_reasons

_JSON = CanonicalJsonSerializer()
_MARGIN = timedelta(minutes=15)


def derive_request(selection: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Preflight forecast facts only; the existing verifier decides full match eligibility."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("The evaluation time must include a timezone")
    hours, valid_times = [], set()
    for row in selection["results"]:
        forecast, issued = row["hour"], row["issued"]
        reasons = forecast_eligibility_reasons(forecast, issued["issued_at"], cutoff=now)
        status = "ineligible" if reasons else "ready"
        if reasons == ["forecast_valid_time_after_verification_cutoff"]:
            status = "deferred"
        valid = datetime.fromisoformat(forecast["valid_time"])
        if not reasons and valid + _MARGIN > now:
            status = "deferred"
            reasons = ["observation_matching_window_not_complete"]
        if status == "ready":
            valid_times.add(valid.astimezone(UTC))
        hours.append(
            {
                "issued_forecast_id": issued["issued_forecast_id"],
                "valid_time": forecast["valid_time"],
                "status": status,
                "reasons": reasons,
            }
        )
    times = sorted(valid_times)
    if times and times[-1] - times[0] > timedelta(hours=6):
        raise ValueError(
            "Eligible valid times span more than the existing six-hour acquisition bound"
        )
    return {
        "hours": hours,
        "ready_valid_times": [t.isoformat() for t in times],
        "query_window_start": (times[0] - _MARGIN).isoformat() if times else None,
        "query_window_end": (times[-1] + _MARGIN).isoformat() if times else None,
    }


def _find_retained(
    dsn: str,
    artifacts: ArtifactService,
    *,
    latitude: float,
    longitude: float,
    configuration_digest: Digest,
    station_ids: tuple[str, ...],
    request: dict[str, Any],
) -> tuple[ArtifactManifest, dict[str, Any]] | None:
    """Discover existing real inputs through their source manifests and transformation edges."""
    start = datetime.fromisoformat(request["query_window_start"])
    end = datetime.fromisoformat(request["query_window_end"])
    with PostgresUnitOfWork(dsn) as uow:
        sources = uow.artifacts.find_real_metar_sources(
            latitude=latitude,
            longitude=longitude,
            configuration_digest=configuration_digest,
        )
        for raw in sources:
            source: dict[str, Any] = raw.attributes or {}
            if (
                raw.quality_state == "invalid"
                or not set(station_ids) <= set(source["station_ids"])
                or datetime.fromisoformat(source["query_window_start"]) > start
                or datetime.fromisoformat(source["query_window_end"]) < end
            ):
                continue
            activities = sorted(
                uow.activities.consumers_of(raw.artifact_id),
                key=lambda a: (a.started_at, a.activity_id),
            )
            for activity in activities:
                if (
                    activity.activity_type != "prepare-retained-metar"
                    or activity.status != "succeeded"
                ):
                    continue
                for output in activity.outputs:
                    manifest, payload = artifacts.load_verified_payload(output.artifact_id)
                    if (
                        manifest.artifact_type != "normalized-metar-observations"
                        or manifest.artifact_schema_version != "metar-observations.v2"
                        or manifest.quality_state == "invalid"
                        or manifest.configuration_digest != configuration_digest
                    ):
                        continue
                    normalized = _JSON.deserialize(payload)
                    _, raw_bytes = artifacts.load_verified_payload(raw.artifact_id)
                    if (
                        normalized["source_provenance"] != source
                        or str(Digest.of_bytes(raw_bytes)) != source["raw_digest"]
                        or len(raw_bytes) != source["raw_bytes"]
                        or any(
                            row["raw_artifact_id"] != str(raw.artifact_id)
                            for row in normalized["rows"]
                        )
                    ):
                        raise IntegrityError("Retained METAR preparation provenance mismatch")
                    return manifest, source
    return None


def _raw_root() -> Path:
    configured = os.environ.get("MESOFORGE_OBSERVATIONS_DIR")
    if configured:
        return Path(configured)
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or Path.home() / ".local/share"
    )
    return base / "MesoForge/observations"


def run_window(
    *,
    latitude: float,
    longitude: float,
    start_valid_time: datetime,
    end_valid_time: datetime,
) -> dict[str, Any]:
    selection = select_issued_forecast_hours(
        latitude=latitude,
        longitude=longitude,
        start_valid_time=start_valid_time,
        end_valid_time=end_valid_time,
    )
    request = derive_request(selection, now=datetime.now(UTC))
    result: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "start_valid_time": selection["start_valid_time"],
        "end_valid_time": selection["end_valid_time"],
        "preflight": request,
        "downloaded_bytes": 0,
        "observation_source": None,
        "verification": None,
    }
    if not request["ready_valid_times"]:
        return {
            **result,
            "status": "nothing_to_verify",
            "reason": "No saved forecast hours are ready for observation matching.",
        }
    configuration = load_observation_configuration()
    stations = retained_station_ids(configuration, latitude, longitude)
    request["station_ids"] = list(stations)
    digest = compute_configuration_digest(configuration)
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    service = configured_service()
    # Coordinate-scoped lock rechecks retained inputs after waiting, including overlapping windows.
    lock_key = Digest.of_bytes(
        _JSON.serialize(
            {
                "operation": "automatic-metar-preparation",
                "latitude": latitude,
                "longitude": longitude,
            }
        )
    )
    with PostgresIdempotencyLock(dsn).acquire(lock_key):
        retained = _find_retained(
            dsn,
            service._artifacts,
            latitude=latitude,
            longitude=longitude,
            configuration_digest=digest,
            station_ids=stations,
            request=request,
        )
        if retained is not None:
            manifest, source = retained
            identifier = str(manifest.artifact_id)
            result["observations_reused"] = True
        else:
            raw_dir = _raw_root() / f"automatic-{uuid4()}"
            source = acquire_for_valid_times(
                raw_dir,
                latitude=latitude,
                longitude=longitude,
                valid_times=tuple(datetime.fromisoformat(t) for t in request["ready_valid_times"]),
            )
            prepared = prepare_bundle(raw_dir)
            identifier = prepared["observations_artifact_id"]
            result.update(
                downloaded_bytes=source["raw_bytes"],
                observations_reused=False,
                raw_directory=str(raw_dir),
            )
    result["observations_artifact_id"] = identifier
    result["observation_source"] = source
    # This standalone command uses the existing window path; restore its process-local setting.
    previous = os.environ.get("MESOFORGE_OBSERVATIONS_ARTIFACT_ID")
    try:
        os.environ["MESOFORGE_OBSERVATIONS_ARTIFACT_ID"] = identifier
        result["verification"] = service.verify_window(
            latitude=latitude,
            longitude=longitude,
            start_valid_time=start_valid_time,
            end_valid_time=end_valid_time,
        )
    finally:
        if previous is None:
            os.environ.pop("MESOFORGE_OBSERVATIONS_ARTIFACT_ID", None)
        else:
            os.environ["MESOFORGE_OBSERVATIONS_ARTIFACT_ID"] = previous
    return {**result, "status": "completed"}


def run_batch(
    config_path: Path,
    *,
    start_valid_time: datetime,
    end_valid_time: datetime,
) -> dict[str, Any]:
    """Process the shared locations list sequentially through the unchanged single-location path."""
    window = IntervalDefinition(
        start=start_valid_time, end=end_valid_time, closure=IntervalClosure.left_closed_right_open
    )
    locations = load_locations(config_path)
    results = []
    summary = {"completed": 0, "nothing_to_verify": 0, "errors": 0}
    for index, location in enumerate(locations):
        row: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
        except ValueError as exc:
            row.update(status="error", error={"code": "invalid_location", "message": str(exc)})
        else:
            try:
                validate_coordinate(latitude, longitude)
                outcome = run_window(
                    latitude=latitude,
                    longitude=longitude,
                    start_valid_time=window.start,
                    end_valid_time=window.end,
                )
            except UnsupportedCoordinateError as exc:
                row.update(
                    status="error", error={"code": "unsupported_coordinate", "message": str(exc)}
                )
            except ValueError as exc:
                row.update(
                    status="error",
                    error={"code": "invalid_verification_request", "message": str(exc)},
                )
            except Exception:
                row.update(
                    status="error",
                    error={
                        "code": "verification_failed",
                        "message": "Could not complete verification for this location.",
                    },
                )
            else:
                verification = outcome["verification"]
                row.update(
                    status=outcome["status"],
                    result=outcome,
                    summary=verification["summary"] if verification else None,
                )
                if verification and verification["summary"]["errors"]:
                    row.update(
                        status="error",
                        error={
                            "code": "verification_incomplete",
                            "message": "Some saved hours failed; see per-hour results.",
                        },
                    )
        summary["errors" if row["status"] == "error" else row["status"]] += 1
        results.append(row)
    return {
        "start_valid_time": window.start.isoformat(),
        "end_valid_time": window.end.isoformat(),
        "summary": summary,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    locations = parser.add_mutually_exclusive_group(required=True)
    locations.add_argument("--lat", type=float)
    locations.add_argument(
        "--config", type=Path, help="Process a locations JSON file sequentially."
    )
    parser.add_argument("--lon", type=float)
    parser.add_argument("--start-valid-time", type=datetime.fromisoformat, required=True)
    parser.add_argument("--end-valid-time", type=datetime.fromisoformat, required=True)
    args = parser.parse_args(argv)
    if (args.config is not None and args.lon is not None) or (
        args.config is None and args.lon is None
    ):
        parser.error("Use --config alone, or provide both --lat and --lon")
    try:
        if args.config is not None:
            result = run_batch(
                args.config,
                start_valid_time=args.start_valid_time,
                end_valid_time=args.end_valid_time,
            )
        else:
            result = run_window(
                latitude=args.lat,
                longitude=args.lon,
                start_valid_time=args.start_valid_time,
                end_valid_time=args.end_valid_time,
            )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.config is not None:
        return int(result["summary"]["errors"] > 0)
    return int(bool(result["verification"] and result["verification"]["summary"]["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
