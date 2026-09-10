"""Ensure shared spatial coverage and issue immutable 36-hour temperature forecasts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from mesoforge.application.issuance import ForecastIssuanceService
from mesoforge.application.prepared_temperature import _code_identity
from mesoforge.application.spatial_coverage import CoverageRequiredError, UnsupportedCoordinateError
from mesoforge.application.spatial_preparation import ensure_coverage
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore


def create_issuer() -> ForecastIssuanceService:
    """Use the established PostgreSQL/S3 environment settings, with no local-file fallback."""
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
    message = "Each location must contain only finite numeric lat and lon."
    if not isinstance(location, dict) or set(location) != {"lat", "lon"}:
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
    config_path: Path, data_dir: Path, *, issuer: ForecastIssuanceService | None = None
) -> dict[str, Any]:
    """Load guidance once; independently calculate and persist each successful location."""
    locations = load_locations(config_path)

    prepared, coverage = ensure_coverage(locations, data_dir)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON locations list.")
    parser.add_argument("--data-dir", type=Path, required=True, help="Existing 36-hour snapshot.")
    args = parser.parse_args(argv)
    try:
        payload = run_batch(args.config, args.data_dir)
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
