"""Explicit single-hour verification and readback through existing artifact storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from mesoforge.application.artifacts import (
    ArtifactService,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.issuance import (
    read_issued_forecast,
    select_issued_forecast_hours,
    validate_hour_selection,
)
from mesoforge.application.observation_preview import preview_observation_match
from mesoforge.application.prepared_temperature import _code_identity
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.issued_temperature import evaluate_temperature_verification

_SCHEMA = "issued-temperature-verification.v1"
_TYPE = "issued-temperature-verification"
_JSON = CanonicalJsonSerializer()


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_result(value: dict[str, Any]) -> None:
    if (
        value["schema_version"] != _SCHEMA
        or value["status"] != "verified"
        or value["temperature_error"]["value"] is None
    ):
        raise IntegrityError("Only eligible temperature verification facts can be persisted")


class IssuedTemperatureVerificationService:
    """One immutable verification artifact per fixed input, policy, and code identity."""

    def __init__(
        self,
        artifacts: ArtifactService,
        *,
        read_forecast: Callable[[UUID], dict[str, Any]],
        preview_match: Callable[[UUID, datetime], dict[str, Any]],
        code_identity: dict[str, Any],
        code_revision: str,
        environment_digest: Digest,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._artifacts = artifacts
        self._read_forecast = read_forecast
        self._preview_match = preview_match
        self._code_identity = code_identity
        self._code_revision = code_revision
        self._environment_digest = environment_digest
        self._clock = clock

    def read(self, verification_id: ArtifactId) -> dict[str, Any]:
        """Read the exact saved fact, with no calculation, input reselection, or writes."""
        manifest, payload = self._artifacts.load_verified_payload(verification_id)
        if (
            manifest.artifact_type != _TYPE
            or manifest.artifact_schema_version != _SCHEMA
            or manifest.media_type != "application/json"
        ):
            raise NotFound("This artifact is not an issued temperature verification")
        result = _JSON.deserialize(payload)
        _validate_result(result)
        return {
            "status": "verified",
            "verification_id": str(manifest.artifact_id),
            "artifact": manifest.model_dump(mode="json"),
            "result": result,
        }

    def verify(
        self, issued_forecast_id: UUID, valid_time: datetime, *, report_reuse: bool = False
    ) -> dict[str, Any]:
        if valid_time.tzinfo is None or valid_time.utcoffset() is None:
            raise ValueError("valid_time must include a timezone")
        saved = self._read_forecast(issued_forecast_id)
        match = self._preview_match(issued_forecast_id, valid_time)
        forecast = saved["forecast"]
        hour = next(
            (h for h in forecast["hours"] if datetime.fromisoformat(h["valid_time"]) == valid_time),
            None,
        )
        if (
            hour is None
            or saved["issued_forecast_id"] != str(issued_forecast_id)
            or match["issued_forecast_id"] != str(issued_forecast_id)
            or match["issued_at"] != saved["issued_at"]
            or match["forecast"]
            != {"latitude": forecast["latitude"], "longitude": forecast["longitude"], **hour}
            or match["forecast_context"] != {k: v for k, v in forecast.items() if k != "hours"}
            or match["forecast_code_identity"] != saved["code_identity"]
        ):
            raise IntegrityError("Observation match does not describe the exact saved forecast")
        now = self._clock()
        provenance = match["input_provenance"]
        cutoff = (
            datetime.fromisoformat(provenance["observations"]["availability"]["available_at"])
            if provenance is not None
            else now
        )
        result = evaluate_temperature_verification(
            match, verification_cutoff=cutoff, evaluated_at=now
        )
        result.update(
            issued_forecast_digest=str(Digest.of_bytes(_JSON.serialize(saved))),
            code_identity=self._code_identity,
        )
        if result["status"] != "verified":
            return {"status": result["status"], "verification_id": None, "result": result}

        observations = ArtifactManifest.model_validate_json(
            _JSON.serialize(provenance["observations"])
        )
        snapshots = sorted(
            (
                ArtifactManifest.model_validate_json(_JSON.serialize(row))
                for row in provenance["station_snapshots"]
            ),
            key=lambda manifest: manifest.artifact_id,
        )
        inputs = [observations, *snapshots]
        request = TransformationRequest(
            activity_type="verify-issued-temperature",
            activity_version="v1",
            inputs=tuple(
                TransformationInputRef(role=f"input-{index}", artifact_id=manifest.artifact_id)
                for index, manifest in enumerate(inputs)
            ),
            output_role="verification",
            output_artifact_type=_TYPE,
            output_artifact_schema_version=_SCHEMA,
            output_media_type="application/json",
            parameters={
                "issued_forecast_id": str(issued_forecast_id),
                "issued_forecast_digest": result["issued_forecast_digest"],
                "valid_time": match["forecast"]["valid_time"],
                "observation_revision": match["selected"]["provenance"]["revision_digest"],
                "verification_cutoff": result["verification_cutoff"],
                "matching_policy": match["selection_policy"],
                "verification_policy": result["verification_policy"],
                "code_identity": self._code_identity,
            },
            configuration_snapshot_id=observations.configuration_snapshot_id,
            configuration_digest=observations.configuration_digest,
            code_revision=self._code_revision,
            environment_digest=self._environment_digest,
        )

        transform_called = False

        def transform(*payloads: bytes) -> dict[str, Any]:
            nonlocal transform_called
            transform_called = True
            # Bind the already-validated match to the exact bytes reread by ArtifactService.
            for manifest, payload in zip(inputs, payloads, strict=True):
                if Digest.of_bytes(payload) != manifest.content_digest:
                    raise IntegrityError("Verification input changed since observation matching")
            return result

        transformed = self._artifacts.execute_raw_transformation(
            request,
            transform,
            _JSON,
            input_loader=bytes,
            output_validator=_validate_result,
        )
        # Verify readback for both a newly saved result and an existing idempotent winner.
        response = self.read(transformed.output.artifact_id)
        if report_reuse:
            # Only the new winner executes the transform under the existing advisory lock.
            response["already_existing"] = not transform_called
        return response

    def verify_window(
        self,
        *,
        latitude: float,
        longitude: float,
        start_valid_time: datetime,
        end_valid_time: datetime,
    ) -> dict[str, Any]:
        """Process each selected issued version independently through single-hour verification."""
        selection = select_issued_forecast_hours(
            latitude=latitude,
            longitude=longitude,
            start_valid_time=start_valid_time,
            end_valid_time=end_valid_time,
        )
        summary = dict.fromkeys(
            ("verified", "unavailable", "ineligible", "already_existing", "errors"), 0
        )
        results = []
        for selected in selection["results"]:
            identifier = selected["issued"]["issued_forecast_id"]
            valid_time = selected["hour"]["valid_time"]
            row: dict[str, Any] = {"issued_forecast_id": identifier, "valid_time": valid_time}
            try:
                outcome = self.verify(
                    UUID(identifier), datetime.fromisoformat(valid_time), report_reuse=True
                )
                status = (
                    "already_existing" if outcome.get("already_existing") else outcome["status"]
                )
                row.update(
                    status=status,
                    verification_id=outcome["verification_id"],
                    reasons=outcome["result"]["reasons"],
                    temperature_error=outcome["result"]["temperature_error"],
                )
                summary[status] += 1
            except Exception:
                summary["errors"] += 1
                row.update(
                    status="error",
                    verification_id=None,
                    reasons=["Could not verify this saved hour or read its existing result."],
                    temperature_error=None,
                )
            results.append(row)
        return {**selection, "results": results, "summary": summary}


def configured_service() -> IssuedTemperatureVerificationService:
    """Compose existing adapters, using the same storage and observation input settings."""
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    # ArtifactService's retained structural protocols are narrower than concrete adapters.
    artifacts = ArtifactService(
        unit_of_work_factory=cast(Any, lambda: PostgresUnitOfWork(dsn)),
        object_store=cast(Any, objects),
        idempotency_lock=PostgresIdempotencyLock(dsn),
    )
    package = Path(__file__).resolve().parents[1]
    identity = _code_identity()
    for name in (
        "application/issued_temperature_verification.py",
        "application/observation_preview.py",
        "application/issuance.py",
        "application/artifacts.py",
        "observations/selection.py",
        "observations/quality.py",
        "verification/issued_temperature.py",
        "provenance/services.py",
        "storage/json.py",
        "storage/s3.py",
        "storage/postgres/repositories.py",
        "storage/postgres/idempotency_lock.py",
    ):
        identity["source_sha256"][name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
    identity["dependency_versions"].update(
        {name: version(name) for name in ("pydantic", "sqlalchemy", "psycopg", "boto3", "jcs")}
    )
    revision = subprocess.run(  # noqa: S603
        ["git", "-C", str(package.parents[1]), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    identity["git_commit"] = revision
    environment = {
        "python": platform.python_version(),
        "dependencies": identity["dependency_versions"],
        "lockfile_sha256": identity.get("lockfile_sha256"),
    }
    return IssuedTemperatureVerificationService(
        artifacts,
        read_forecast=read_issued_forecast,
        preview_match=preview_observation_match,
        code_identity=identity,
        code_revision=revision,
        environment_digest=Digest.of_bytes(_JSON.serialize(environment)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "verify", help="Calculate and save one eligible verification fact."
    )
    verify.add_argument("--issued-forecast-id", type=UUID, required=True)
    verify.add_argument("--valid-time", type=datetime.fromisoformat, required=True)
    read = commands.add_parser("read", help="Read an exact saved verification artifact.")
    read.add_argument("--verification-id", type=ArtifactId, required=True)
    window = commands.add_parser(
        "window", help="Verify saved hours for one coordinate/time window."
    )
    window.add_argument("--lat", type=float, required=True)
    window.add_argument("--lon", type=float, required=True)
    window.add_argument("--start-valid-time", type=datetime.fromisoformat, required=True)
    window.add_argument("--end-valid-time", type=datetime.fromisoformat, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify" and args.valid_time.tzinfo is None:
        parser.error("--valid-time must include a timezone")
    if args.command == "window":
        try:
            validate_hour_selection(args.lat, args.lon, args.start_valid_time, args.end_valid_time)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        service = configured_service()
        if args.command == "window":
            result = service.verify_window(
                latitude=args.lat,
                longitude=args.lon,
                start_valid_time=args.start_valid_time,
                end_valid_time=args.end_valid_time,
            )
        elif args.command == "verify":
            result = service.verify(args.issued_forecast_id, args.valid_time)
        else:
            result = service.read(args.verification_id)
    except NotFound:
        print(
            json.dumps({"error": "Requested saved forecast hour or verification was not found."}),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps({"error": "Could not verify the retained inputs or saved result."}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.command == "window":
        return int(result["summary"]["errors"] > 0)
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
