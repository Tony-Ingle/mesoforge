"""Explicit hourly QPF measurement and read-only analysis; no forecast/provider work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from mesoforge.application.artifacts import (
    ArtifactService,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.issuance import (
    ForecastIssuanceService,
    validate_hour_selection,
    version_may_overlap,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.errors import IntegrityError, NotFound
from mesoforge.common.identifiers import ArtifactId, Digest, IssuedForecastId
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.issued_qpf import evaluate_qpf_verification
from mesoforge.verification.qpf_analysis import analyze_qpf_facts

SCHEMA = "issued-qpf-verification.v1"
TYPE = "issued-qpf-verification"
_JSON = CanonicalJsonSerializer()
_ROOT = Path(__file__).resolve().parents[3]


def _validate_fact(fact: dict[str, Any]) -> None:
    if fact.get("schema_version") != SCHEMA or fact.get("status") not in {"verified", "excluded"}:
        raise IntegrityError("Not a supported QPF verification fact")


def _identity() -> dict[str, Any]:
    paths = (
        "application/issued_qpf_verification.py",
        "verification/issued_qpf.py",
        "verification/qpf_analysis.py",
        "verification/metrics.py",
        "observations/mrms.py",
    )
    return {
        "git_commit": subprocess.run(  # noqa: S603
            ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "source_sha256": {
            p: hashlib.sha256((_ROOT / "src/mesoforge" / p).read_bytes()).hexdigest() for p in paths
        },
        "lock_sha256": hashlib.sha256((_ROOT / "uv.lock").read_bytes()).hexdigest(),
    }


class IssuedQpfVerificationService:
    """Compact immutable facts through the existing artifact transaction/advisory lock."""

    def __init__(
        self,
        artifacts: ArtifactService,
        *,
        issuer: ForecastIssuanceService,
        unit_of_work_factory: Callable[[], Any],
        configuration: Any,
        code_identity: dict[str, Any],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.artifacts = artifacts
        self.issuer = issuer
        self.factory = unit_of_work_factory
        self.configuration = configuration
        self.identity = code_identity
        self.clock = clock

    def read(self, identifier: ArtifactId) -> dict[str, Any]:
        manifest, raw = self.artifacts.load_verified_payload(identifier)
        if manifest.artifact_type != TYPE or manifest.artifact_schema_version != SCHEMA:
            raise NotFound("Not an issued QPF verification artifact")
        fact = _JSON.deserialize(raw)
        _validate_fact(fact)
        return {
            "verification_id": str(identifier),
            "content_digest": str(manifest.content_digest),
            "byte_size": len(raw),
            "result": fact,
        }

    def verify(
        self,
        issued_forecast_id: IssuedForecastId,
        valid_time: datetime,
        *,
        stage: str = "final_issued",
        extraction_id: ArtifactId | None = None,
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a matched or explicitly excluded opportunity; retries reuse exact evidence."""
        # Existing issuance repositories use UUID; retain that storage contract.
        version = UUID(IssuedForecastId(str(issued_forecast_id)))
        with self.factory() as uow:
            record = uow.issued_forecasts.get(version)
        return self._verify_saved(
            record,
            self.issuer.read(version),
            valid_time,
            stage=stage,
            extraction_id=extraction_id,
            cutoff=cutoff,
        )

    def _verify_saved(
        self,
        record: Any,
        saved: dict[str, Any],
        valid_time: datetime,
        *,
        stage: str,
        extraction_id: ArtifactId | None,
        cutoff: datetime | None,
    ) -> dict[str, Any]:
        """Internal window reuse of bytes already checksum-verified by the issuance reader."""
        issued_forecast_id = record.issued_forecast_id
        now = self.clock()
        cutoff = cutoff or now
        if cutoff.tzinfo is None or now.tzinfo is None or cutoff > now:
            raise ValueError("Verification cutoff must be aware and not in the future")
        if (
            saved["issued_forecast_id"] != str(issued_forecast_id)
            or saved["latitude"] != record.latitude
            or saved["longitude"] != record.longitude
            or datetime.fromisoformat(saved["issued_at"]) != record.issued_at
            or datetime.fromisoformat(saved["target_reference_time"])
            != record.target_reference_time
        ):
            raise IntegrityError("Selected issuance does not match the requested version")
        extraction, reference, inputs = None, None, []
        if extraction_id is not None:
            manifest, raw = self.artifacts.load_verified_payload(extraction_id)
            if (
                manifest.artifact_type != "mrms-coordinate-extraction"
                or manifest.artifact_schema_version != "mesoforge.mrms-coordinate-extraction.v1"
            ):
                raise ValueError(
                    "Observation artifact is not a supported MRMS coordinate extraction"
                )
            try:
                extraction = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                extraction = {"malformed": True}
            reference = {
                "artifact_id": str(extraction_id),
                "content_digest": str(manifest.content_digest),
                "available_at": manifest.availability.available_at.isoformat(),
            }
            inputs.append(TransformationInputRef(role="mrms-extraction", artifact_id=extraction_id))
        fact = evaluate_qpf_verification(
            saved,
            valid_time,
            stage=stage,
            issued_forecast_digest=record.content_digest,
            extraction=extraction,
            extraction_reference=reference,
            verification_cutoff=cutoff,
        )
        _validate_fact(fact)
        # A later invocation with unchanged evidence/status reuses the earlier fact and its
        # truthful original cutoff. A newly eligible event or revised evidence has a new key.
        scientific_identity = {k: v for k, v in fact.items() if k != "verification_cutoff"}
        parameters = {
            "opportunity_id": fact["opportunity_id"],
            "evidence_digest": str(Digest.of_bytes(_JSON.serialize(scientific_identity))),
            "code_identity": self.identity,
        }
        configuration = self.configuration()
        request = TransformationRequest(
            activity_type="verify-issued-qpf",
            activity_version="v1",
            inputs=tuple(inputs),
            output_role="verification",
            output_artifact_type=TYPE,
            output_artifact_schema_version=SCHEMA,
            output_media_type="application/json",
            parameters=parameters,
            configuration_snapshot_id=configuration.configuration_snapshot_id,
            configuration_digest=configuration.configuration_digest,
            code_revision=self.identity["git_commit"],
            environment_digest=Digest.of_bytes(_JSON.serialize(self.identity)),
            attributes={
                "latitude": record.latitude,
                "longitude": record.longitude,
                "valid_time": valid_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "issued_forecast_id": str(issued_forecast_id),
                "analysis": fact,
            },
        )
        called = False

        def transform(*payloads: bytes) -> dict[str, Any]:
            nonlocal called
            called = True
            if (
                reference is not None
                and str(Digest.of_bytes(payloads[0])) != reference["content_digest"]
            ):
                raise IntegrityError("MRMS extraction changed before verification")
            return fact

        result = self.artifacts.execute_raw_transformation(
            request,
            transform,
            _JSON,
            input_loader=bytes,
            output_validator=_validate_fact,
        )
        return {**self.read(result.output.artifact_id), "already_existing": not called}

    def verify_window(
        self,
        *,
        latitude: float,
        longitude: float,
        start_valid_time: datetime,
        end_valid_time: datetime,
        extraction_ids: Sequence[ArtifactId] = (),
        stages: Sequence[str] = ("final_issued",),
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        """Reuse retained extractions without observation acquisition or forecast calculation."""
        window = validate_hour_selection(latitude, longitude, start_valid_time, end_valid_time)
        cutoff = cutoff or self.clock()
        # Index every supplied revision, not just the latest; conflicts remain visible to analysis.
        index: dict[datetime, list[ArtifactId]] = {}
        unselected = []
        for identifier in dict.fromkeys(extraction_ids):
            manifest, raw = self.artifacts.load_verified_payload(identifier)
            try:
                extraction = json.loads(raw)
                qpe = extraction["qpe"]
                coordinate = qpe["extraction"]["forecast_coordinate"]
                end = datetime.fromisoformat(qpe["temporal"]["interval_end"])
                if (
                    coordinate != {"latitude": latitude, "longitude": longitude}
                    or end.tzinfo is None
                ):
                    raise ValueError("MRMS extraction describes another coordinate")
                index.setdefault(end, []).append(identifier)
            except (ValueError, KeyError, TypeError):
                unselected.append(
                    {"extraction_id": str(identifier), "reason": "malformed_or_other_coordinate"}
                )
        with self.factory() as uow:
            records = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        rows = []
        for record in records:
            if not version_may_overlap(record, window):
                continue
            try:
                saved = self.issuer.read(record.issued_forecast_id)
            except Exception as exc:
                rows.append(
                    {
                        "issued_forecast_id": str(record.issued_forecast_id),
                        "status": "error",
                        "reason": str(exc),
                    }
                )
                continue
            for hour in saved["forecast"]["hours"]:
                valid = datetime.fromisoformat(hour["valid_time"])
                if not window.start <= valid < window.end:
                    continue
                candidates: Sequence[ArtifactId | None] = index.get(valid) or (None,)
                for stage in dict.fromkeys(stages):
                    for candidate_id in candidates:
                        try:
                            result = self._verify_saved(
                                record,
                                saved,
                                valid,
                                stage=stage,
                                extraction_id=candidate_id,
                                cutoff=cutoff,
                            )
                            rows.append(
                                {
                                    "issued_forecast_id": str(record.issued_forecast_id),
                                    "valid_time": hour["valid_time"],
                                    "stage": stage,
                                    "verification_id": result["verification_id"],
                                    "status": result["result"]["status"],
                                    "reasons": result["result"]["reasons"],
                                    "already_existing": result["already_existing"],
                                }
                            )
                        except Exception as exc:
                            rows.append(
                                {
                                    "issued_forecast_id": str(record.issued_forecast_id),
                                    "valid_time": hour["valid_time"],
                                    "stage": stage,
                                    "status": "error",
                                    "reason": str(exc),
                                }
                            )
        return {
            "results": rows,
            "summary": dict(Counter(r["status"] for r in rows)),
            "already_existing": sum(bool(r.get("already_existing")) for r in rows),
            "unselected_extractions": unselected,
            "provider_calls": 0,
        }

    def analyze_window(
        self,
        *,
        latitude: float,
        longitude: float,
        start_valid_time: datetime,
        end_valid_time: datetime,
        stages: Sequence[str] = ("final_issued",),
        limit: int = 5000,
        contributors: Sequence[str] | None = None,
        payload_only: bool = False,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Read compact fact attributes and issuance metadata; never read/rebuild forecast grids.

        ``as_of`` is an explicit information cutoff: facts, revisions and issuances that
        were not registered, available and verified by then are removed before
        canonicalization, and opportunities are counted at that cutoff, not the clock.
        """
        started = time.perf_counter()
        window = validate_hour_selection(latitude, longitude, start_valid_time, end_valid_time)
        if not 1 <= limit <= 10000:
            raise ValueError("Analysis limit must be within 1..10000")
        if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
            raise ValueError("Evidence cutoff must be timezone-aware")
        with self.factory() as uow:
            records = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
            manifests = uow.artifacts.find_issued_qpf_verifications(
                latitude=latitude,
                longitude=longitude,
                start_valid_time=start_valid_time,
                end_valid_time=end_valid_time,
                limit=limit + 1,
                **({"available_by": as_of} if as_of is not None else {}),
            )
        if len(manifests) > limit:
            raise ValueError("Too many QPF facts; narrow the window or raise --limit")
        as_of_exclusions: Counter[str] = Counter()
        if as_of is not None:
            records = tuple(r for r in records if r.issued_at <= as_of)
            visible = []
            for manifest in manifests:
                if max(manifest.registered_at, manifest.availability.available_at) > as_of:
                    as_of_exclusions["fact_not_available_at_evidence_cutoff"] += 1
                else:
                    visible.append(manifest)
            manifests = tuple(visible)
        rows, attribute_bytes, payload_bytes = [], 0, 0
        records_by_id = {str(r.issued_forecast_id): r for r in records}
        for manifest in manifests:
            fact = (manifest.attributes or {}).get("analysis")
            if payload_only or not isinstance(fact, dict):
                _, raw = self.artifacts.load_verified_payload(manifest.artifact_id)
                fact = json.loads(raw)
                payload_bytes += len(raw)
            else:
                attribute_bytes += len(_JSON.serialize(fact))
            _validate_fact(fact)
            if fact["stage"] not in stages:
                continue
            if as_of is not None:
                verified = fact.get("verification_cutoff")
                try:
                    verified_at = datetime.fromisoformat(str(verified))
                except ValueError:
                    verified_at = None
                if verified_at is None or verified_at.tzinfo is None:
                    as_of_exclusions["learning_evidence_availability_unproven"] += 1
                    continue
                if verified_at > as_of:
                    as_of_exclusions["fact_verified_after_evidence_cutoff"] += 1
                    continue
            record = records_by_id.get(fact["issued_forecast_id"])
            exclusion = None
            if (
                record is None
                or fact["issued_forecast_digest"] != str(record.content_digest)
                or fact["latitude"] != record.latitude
                or fact["longitude"] != record.longitude
                or datetime.fromisoformat(fact["issued_at"]) != record.issued_at
                or datetime.fromisoformat(fact["target_reference_time"])
                != record.target_reference_time
            ):
                exclusion = "fact_disagrees_with_immutable_issuance_metadata"
            rows.append(
                {
                    **fact,
                    "artifact_id": str(manifest.artifact_id),
                    "registered_at": manifest.registered_at.isoformat(),
                    "quality_state": manifest.quality_state,
                    "integrity_exclusion": exclusion,
                }
            )
        analysis = analyze_qpf_facts(rows, contributors=contributors)
        cutoff = as_of if as_of is not None else self.clock()
        potential = 0
        for record in records:
            for lead in range(1, 37):
                end = record.target_reference_time + timedelta(hours=lead)
                if (
                    window.start <= end < window.end
                    and record.issued_at <= end - timedelta(hours=1)
                    and end <= cutoff
                ):
                    potential += len(set(stages))
        return {
            **analysis,
            "window": {
                "latitude": latitude,
                "longitude": longitude,
                "start_valid_time": window.start.isoformat(),
                "end_valid_time": window.end.isoformat(),
                "selection_closure": "[start,end)",
                "stages": list(stages),
            },
            "eligible_issued_stage_opportunities": potential,
            "evidence_cutoff": as_of.isoformat() if as_of is not None else None,
            "evidence_cutoff_exclusions": dict(sorted(as_of_exclusions.items())),
            "opportunity_inventory_basis": (
                "issuance metadata horizons 1..36; field/interval validity assessed by facts"
            ),
            "storage": {
                "fact_payload_bytes": sum(m.byte_size for m in manifests),
                "analytical_attribute_bytes_read": attribute_bytes,
                "payload_bytes_read": payload_bytes,
            },
            "analysis_seconds": time.perf_counter() - started,
        }


def configured_service() -> IssuedQpfVerificationService:
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    factory = cast(Any, lambda: PostgresUnitOfWork(dsn))
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    artifacts = ArtifactService(
        unit_of_work_factory=factory,
        object_store=cast(Any, objects),
        idempotency_lock=PostgresIdempotencyLock(dsn),
    )

    def configuration() -> Any:
        config, _ = load_configuration_source(base_path=_ROOT / "configs/base.yaml")
        return ConfigurationService(factory).register(config)

    return IssuedQpfVerificationService(
        artifacts,
        issuer=ForecastIssuanceService(objects, factory, code_identity={}),
        unit_of_work_factory=factory,
        configuration=configuration,
        code_identity=_identity(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("window", "analyze"):
        p = commands.add_parser(name)
        p.add_argument("--lat", type=float, required=True)
        p.add_argument("--lon", type=float, required=True)
        p.add_argument("--start-valid-time", type=datetime.fromisoformat, required=True)
        p.add_argument("--end-valid-time", type=datetime.fromisoformat, required=True)
        p.add_argument("--stage", action="append", choices=("baseline", "final_issued"))
        if name == "window":
            p.add_argument("--mrms-extraction", type=ArtifactId, action="append", default=[])
            p.add_argument("--as-of", type=datetime.fromisoformat)
        else:
            p.add_argument("--contributor", action="append")
            p.add_argument("--limit", type=int, default=5000)
            p.add_argument("--payload-only", action="store_true")
    p = commands.add_parser("read")
    p.add_argument("--verification-id", type=ArtifactId, required=True)
    args = parser.parse_args(argv)
    try:
        service = configured_service()
        if args.command == "read":
            result = service.read(args.verification_id)
        else:
            common = {
                "latitude": args.lat,
                "longitude": args.lon,
                "start_valid_time": args.start_valid_time,
                "end_valid_time": args.end_valid_time,
                "stages": args.stage or ["final_issued"],
            }
            if args.command == "window":
                result = service.verify_window(
                    **common, extraction_ids=args.mrms_extraction, cutoff=args.as_of
                )
            else:
                result = service.analyze_window(
                    **common,
                    contributors=args.contributor,
                    limit=args.limit,
                    payload_only=args.payload_only,
                )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
