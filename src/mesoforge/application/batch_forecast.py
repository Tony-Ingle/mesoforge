"""Preparation/development batch helpers and shared immutable issuance wiring.

Normal configured-location generation reads ``forecast_from_baseline``. Explicit
prepared-guidance tools retain this inline path for development and replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_temperature import _code_identity, prepare_locations
from mesoforge.application.spatial_coverage import CoverageRequiredError, UnsupportedCoordinateError
from mesoforge.application.spatial_preparation import ensure_coverage
from mesoforge.contracts.policy_governance import GovernanceBlockedError
from mesoforge.forecasting.recipes import (
    DEFAULT_CONFIGURATION,
    ContributorConfiguration,
    with_qpf_fields,
    with_surface_fields,
)
from mesoforge.guidance.runtime import SystemClock
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore


def create_issuer(*, ensure_bucket: bool = True) -> ForecastIssuanceService:
    """Use the established PostgreSQL/S3 environment settings, with no local-file fallback.

    Hosted workers pass ``ensure_bucket=False``: a missing or mistyped bucket must fail
    instead of silently creating an empty one; bucket creation is an operator step.
    """
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    names = (
        "MESOFORGE_S3_BUCKET",
        "MESOFORGE_S3_ENDPOINT",
        "MESOFORGE_S3_ACCESS_KEY",
        "MESOFORGE_S3_SECRET_KEY",
    )
    for name in names:
        if not os.environ.get(name):
            raise RuntimeError(f"{name} must be set for batch issuance")
    identity = _code_identity()
    package = Path(__file__).resolve().parents[1]
    for path in (
        "application/batch_forecast.py",
        "application/issuance.py",
        "application/forward_run.py",
        "application/forward_verification.py",
        "application/forecast_from_snapshot.py",
        "application/forecast_from_baseline.py",
        "application/baseline_snapshot.py",
        "application/baseline_codec.py",
        "application/prepared_snapshot.py",
        "application/hourly_report.py",
        "application/surface_forecast.py",
        "application/local_surface_grid.py",
        "forecasting/surface.py",
        "forecasting/coherence.py",
        "forecasting/vector_blend.py",
        "forecasting/gust_blend.py",
        "contracts/issued_forecasts.py",
        "storage/json.py",
        "storage/s3.py",
        "storage/postgres/models.py",
        "storage/postgres/repositories.py",
    ):
        identity["source_sha256"][path] = hashlib.sha256((package / path).read_bytes()).hexdigest()
    identity["dependency_versions"].update(
        {name: version(name) for name in ("pydantic", "sqlalchemy", "psycopg", "boto3", "jcs")}
    )
    try:
        objects = S3ArtifactObjectStore(
            bucket=os.environ["MESOFORGE_S3_BUCKET"],
            endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
            access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
            secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
            ensure_bucket=ensure_bucket,
        )
    except Exception as exc:
        raise RuntimeError("Could not connect to configured issuance object storage") from exc
    return ForecastIssuanceService(objects, lambda: PostgresUnitOfWork(dsn), code_identity=identity)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _json_float(value: str) -> float | str:
    number = float(value)
    # Preserve an overflowing numeral as text in its location error, not JSON Infinity.
    return number if math.isfinite(number) else value


def _coordinates(location: object) -> tuple[float, float]:
    message = (
        "Each location requires finite numeric lat and lon, with optional string id/name "
        "and an optional string display_timezone."
    )
    if (
        not isinstance(location, dict)
        or not {"lat", "lon"} <= set(location) <= {"lat", "lon", "id", "name", "display_timezone"}
        or any(
            key in location and not isinstance(location[key], str)
            for key in ("id", "name", "display_timezone")
        )
    ):
        raise ValueError(message)
    if any(type(location[key]) not in (int, float) for key in ("lat", "lon")):
        raise ValueError(message)
    try:
        latitude, longitude = float(location["lat"]), float(location["lon"])
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError(message)
    return latitude, longitude


def location_display_timezone(location: dict[str, Any]) -> str | None:
    """Optional per-location presentation zone; it never selects data or changes values."""
    zone = location.get("display_timezone")
    if zone is None:
        return None
    if not isinstance(zone, str):
        raise ValueError("display_timezone must be an IANA zone name string")
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"display_timezone must be a resolvable IANA zone, not {zone!r}") from exc
    return zone


def load_locations(config_path: Path) -> list[Any]:
    """Read the shared locations JSON format without accessing guidance or storage."""
    config = json.loads(
        config_path.read_text(encoding="utf-8-sig"),
        parse_constant=_reject_constant,
        parse_float=_json_float,
    )
    if (
        not isinstance(config, dict)
        or set(config) != {"locations"}
        or not isinstance(config["locations"], list)
    ):
        raise ValueError("Config must be a JSON object containing a locations list.")

    return config["locations"]


def run_batch(
    config_path: Path,
    data_dir: Path,
    *,
    issuer: ForecastIssuanceService | None = None,
    require_future_hours: bool = False,
    contributor_configuration: ContributorConfiguration = DEFAULT_CONFIGURATION,
    shadow_directories: Mapping[str, Path] | None = None,
    forecast_report_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    pop_guidance: dict[str, Any] | None = None,
    probability_sources: list[dict[str, Any]] | None = None,
    ptype_guidance: dict[str, Any] | None = None,
    snowfall_guidance: dict[str, Any] | None = None,
    snowfall_amount_guidance: dict[str, Any] | None = None,
    cloud_guidance: dict[str, Any] | None = None,
    visibility_guidance: dict[str, Any] | None = None,
    thunder_guidance: dict[str, Any] | None = None,
    ice_guidance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load guidance once; independently calculate and persist each successful location."""
    locations = load_locations(config_path)
    validate_current_control(contributor_configuration)

    prepared, coverage = ensure_coverage(
        locations,
        data_dir,
        contributor_configuration=contributor_configuration,
        shadow_directories=shadow_directories,
    )
    if pop_guidance is not None or probability_sources:
        from mesoforge.application.spatial_preparation import attach_pop_guidance

        prepared = attach_pop_guidance(
            prepared, pop_guidance, probability_sources=probability_sources
        )
    if ptype_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_type_guidance

        prepared = attach_type_guidance(prepared, ptype_guidance)
    if snowfall_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_snowfall_guidance

        prepared = attach_snowfall_guidance(prepared, snowfall_guidance)
    if snowfall_amount_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_snowfall_amount_guidance

        prepared = attach_snowfall_amount_guidance(prepared, snowfall_amount_guidance)
    if cloud_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_cloud_guidance

        prepared = attach_cloud_guidance(prepared, cloud_guidance)
    if visibility_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_visibility_guidance

        prepared = attach_visibility_guidance(prepared, visibility_guidance)
    if thunder_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_thunder_guidance

        prepared = attach_thunder_guidance(prepared, thunder_guidance)
    if ice_guidance is not None:
        from mesoforge.application.spatial_preparation import attach_ice_guidance

        prepared = attach_ice_guidance(prepared, ice_guidance)
    if prepared.horizon_hours != tuple(range(1, 37)):
        raise ValueError("Batch forecasts require an existing dataset for hours 1..36.")

    issuer = issuer if issuer is not None else create_issuer()
    batch_run_id = uuid4()
    results: list[dict[str, Any]] = []
    for index, location in enumerate(locations):
        result: dict[str, Any] = {"index": index, "location": location}
        try:
            latitude, longitude = _coordinates(location)
        except ValueError as exc:
            result.update(status="error", error={"code": "invalid_location", "message": str(exc)})
        else:
            try:
                forecast = prepared.forecast(latitude=latitude, longitude=longitude)
                if forecast_report_builder is not None:
                    # Presentation stages are saved beside, never over, the numerical hours.
                    forecast["hourly_report"] = forecast_report_builder(deepcopy(forecast))
                if (
                    require_future_hours
                    and datetime.fromisoformat(forecast["hours"][0]["valid_time"])
                    <= SystemClock().now()
                ):
                    raise ValueError(
                        "Automatic guidance expired before issuance; rerun cycle selection"
                    )
            except UnsupportedCoordinateError as exc:
                result.update(
                    status="error", error={"code": "unsupported_coordinate", "message": str(exc)}
                )
            except CoverageRequiredError as exc:
                result.update(
                    status="error", error={"code": "coverage_required", "message": str(exc)}
                )
            except Exception as exc:
                # Isolate a calculation failure to this location; interrupts still propagate.
                result.update(
                    status="error",
                    error={"code": "forecast_failed", "message": f"{type(exc).__name__}: {exc}"},
                )
            else:
                try:
                    issued = issuer.issue(forecast, batch_run_id=batch_run_id, location_index=index)
                except GovernanceBlockedError as exc:
                    result.update(status="error", error={"code": exc.code, "message": str(exc)})
                except Exception:
                    # Keep connection details out of the public per-location result.
                    result.update(
                        status="error",
                        error={
                            "code": "issuance_failed",
                            "message": (
                                "Could not persist this forecast; "
                                "no successful issuance is reported."
                            ),
                        },
                    )
                else:
                    result.update(
                        status="ok", forecast=forecast, issued=issued.model_dump(mode="json")
                    )
        results.append(result)
    return {"batch_run_id": str(batch_run_id), "coverage": coverage, "results": results}


def validate_current_control(configuration: ContributorConfiguration) -> None:
    """Accept the explicit provisional registry or the unchanged historical control."""
    if configuration.field_policy_family is not None:
        from mesoforge.forecasting.recipes import PROVISIONAL_CONFIGURATION

        if configuration != PROVISIONAL_CONFIGURATION:
            raise ValueError("Provisional source registry differs from its versioned contract")
        return
    if configuration.control_recipe != DEFAULT_CONFIGURATION.control_recipe:
        raise ValueError("Batch issuance must retain the approved HRRR/GFS 70/30 control recipe")
    models = configuration.model_map()
    for model, expected in DEFAULT_CONFIGURATION.model_map().items():
        if models.get(model) not in (
            expected,
            with_surface_fields(DEFAULT_CONFIGURATION).model_map()[model],
            with_qpf_fields(with_surface_fields(DEFAULT_CONFIGURATION)).model_map()[model],
        ):
            raise ValueError(f"Batch issuance must retain the default {model} model definition")
    if any(
        definition.status == "active" and model not in DEFAULT_CONFIGURATION.model_map()
        for model, definition in models.items()
    ):
        raise ValueError("Additional models must remain outside the active issued control")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON locations list.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--data-dir", type=Path, help="Reuse an existing 36-hour snapshot offline.")
    mode.add_argument(
        "--output-dir", type=Path, help="Prepare current guidance here before issuance."
    )
    parser.add_argument("--target-reference-time", type=datetime.fromisoformat)
    parser.add_argument("--hrrr-cycle", type=datetime.fromisoformat)
    parser.add_argument("--gfs-cycle", type=datetime.fromisoformat)
    parser.add_argument(
        "--contributors-config",
        type=Path,
        help="Optional model/recipe JSON; active HRRR/GFS control must remain unchanged.",
    )
    parser.add_argument(
        "--shadow-data",
        action="append",
        default=[],
        metavar="MODEL=PATH",
        help="Separate prepared shadow directory or coverage index root; repeat per model.",
    )
    args = parser.parse_args(argv)
    times = (args.target_reference_time, args.hrrr_cycle, args.gfs_cycle)
    if args.data_dir is not None and any(value is not None for value in times):
        parser.error("Existing prepared guidance supplies its own cycles and reference time")
    if any(value is not None for value in times) and any(value is None for value in times):
        parser.error("Explicit override requires target reference time and both source cycles")
    try:
        contributor_configuration = (
            ContributorConfiguration.model_validate_json(
                args.contributors_config.read_text(encoding="utf-8-sig")
            )
            if args.contributors_config is not None
            else DEFAULT_CONFIGURATION
        )
        validate_current_control(contributor_configuration)
        shadow_directories = {}
        for attachment in args.shadow_data:
            model, separator, path = attachment.partition("=")
            if not separator or not model or not path or model in shadow_directories:
                raise ValueError("--shadow-data requires a unique MODEL=PATH entry per model")
            definition = contributor_configuration.model_map().get(model)
            if definition is None or definition.status not in ("shadow", "evaluated", "deprecated"):
                raise ValueError(f"{model}: shadow data requires an enabled non-active model")
            shadow_directories[model] = Path(path)
        preparation = None
        data_dir = args.data_dir
        if data_dir is None:
            # Validate storage configuration before acquiring provider data.
            issuer = create_issuer()
            preparation = prepare_locations(
                load_locations(args.config),
                args.output_dir,
                target_reference_time=args.target_reference_time,
                hrrr_cycle=args.hrrr_cycle,
                gfs_cycle=args.gfs_cycle,
            )
            data_dir = Path(preparation["directory"])
        else:
            issuer = None
        payload = run_batch(
            args.config,
            data_dir,
            issuer=issuer,
            require_future_hours=preparation is not None and all(value is None for value in times),
            contributor_configuration=contributor_configuration,
            shadow_directories=shadow_directories,
        )
        if preparation is not None:
            payload["preparation"] = preparation
        output = json.dumps(payload, indent=2, allow_nan=False)
    except Exception as exc:
        print(
            json.dumps({"error": {"code": "batch_failed", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 2
    print(output)
    return 1 if any(result["status"] == "error" for result in payload["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
