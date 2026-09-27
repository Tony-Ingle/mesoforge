"""Retain and extract one fixed MRMS hourly analysis event; no verification or scoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

from mesoforge.application.artifacts import (
    ArtifactService,
    SourceRegistrationRequest,
    TransformationInputRef,
    TransformationRequest,
)
from mesoforge.application.code_revision import current_code_revision
from mesoforge.application.configuration import ConfigurationService
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.identifiers import ArtifactId, ConfigurationSnapshotId, Digest
from mesoforge.contracts.artifacts import Availability
from mesoforge.guidance.runtime import SystemClock
from mesoforge.observations.interfaces import Clock, HttpTransport
from mesoforge.observations.mrms import (
    SCHEMA_VERSION,
    extract_mrms,
    inspect_mrms,
    validate_support_alignment,
)
from mesoforge.observations.sources.mrms import default_transport, fetch_product, source_url
from mesoforge.storage.json import CanonicalJsonSerializer
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore

_ROOT = Path(__file__).resolve().parents[3]
_JSON = CanonicalJsonSerializer()
PRODUCTS = (
    "MultiSensor_QPE_01H_Pass2",
    "GaugeInflIndex_01H_Pass2",
    "RadarAccumulationQualityIndex_01H",
)
_BUNDLE_SCHEMA = "mesoforge.mrms-raw-bundle.v1"
_EXTRACTION_SCHEMA = "mesoforge.mrms-coordinate-extraction.v1"


def _identity() -> dict[str, Any]:
    paths = (
        "application/prepared_mrms.py",
        "observations/mrms.py",
        "observations/sources/mrms.py",
    )
    return {
        "git_commit": current_code_revision(_ROOT),
        "source_sha256": {
            path: hashlib.sha256((_ROOT / "src/mesoforge" / path).read_bytes()).hexdigest()
            for path in paths
        },
        "lock_sha256": hashlib.sha256((_ROOT / "uv.lock").read_bytes()).hexdigest(),
        "dependency_versions": {name: version(name) for name in ("eccodes", "numpy")},
    }


def _coordinate(latitude: float, longitude: float) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("Latitude must be finite and within [-90,90]")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("Longitude must be finite and within [-180,180]")


def acquire_bundle(
    raw_dir: Path,
    *,
    product_time: datetime,
    transport: HttpTransport | None = None,
    clock: Clock | None = None,
    archive: bool = False,
) -> dict[str, Any]:
    """Retain exactly three fixed-hour sources, once; never overwrite a previous bundle."""
    clock = clock or SystemClock()
    for product in PRODUCTS:
        source_url(product, product_time, archive=archive)
    if product_time > clock.now():
        raise ValueError("MRMS acquisition accepts fixed past product times only")
    if raw_dir.resolve().is_relative_to(_ROOT):
        raise ValueError("Raw MRMS sources must be retained outside the repository")
    raw_dir.mkdir(parents=True, exist_ok=False)
    transport = transport or default_transport()
    sources = {}
    for product in PRODUCTS:
        raw, acquired = fetch_product(
            product, product_time, transport=transport, clock=clock, archive=archive
        )
        # Keep original bytes even if subsequent contract validation fails.
        with (raw_dir / acquired["filename"]).open("xb") as output:
            output.write(raw)
        retained_source = {
            **acquired,
            "content_digest": str(Digest.of_bytes(raw)),
            "byte_size": len(raw),
        }
        with (raw_dir / (acquired["filename"] + ".json")).open("xb") as output:
            output.write(_JSON.serialize(retained_source))
        parsed = inspect_mrms(raw, product=product)
        if datetime.fromisoformat(parsed["product_time"]) != product_time:
            raise ValueError("Encoded MRMS product time differs from the requested object time")
        sources[product] = {
            **retained_source,
            "parsed_metadata": parsed,
        }
    metadata = {
        "schema_version": _BUNDLE_SCHEMA,
        "product_time": product_time.astimezone(UTC).isoformat(),
        "acquisition_code_identity": _identity(),
        "sources": sources,
    }
    with (raw_dir / "manifest.json").open("xb") as output:
        output.write(_JSON.serialize(metadata))
    return metadata


def load_bundle(raw_dir: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Verify retained byte identity before decoding, with no HTTP or storage writes."""
    metadata = json.loads((raw_dir / "manifest.json").read_bytes())
    if metadata.get("schema_version") != _BUNDLE_SCHEMA:
        raise ValueError("Unsupported retained MRMS bundle schema")
    if set(metadata["sources"]) != set(PRODUCTS):
        raise ValueError("MRMS bundle requires QPE and both hourly support products")
    instant = datetime.fromisoformat(metadata["product_time"])
    sources = {}
    for product in PRODUCTS:
        record = metadata["sources"][product]
        urls = {source_url(product, instant, archive=archive) for archive in (False, True)}
        if (
            record["url"] not in urls
            or record["filename"] != source_url(product, instant).rsplit("/", 1)[1]
        ):
            raise ValueError("Retained MRMS source locator differs from its fixed-hour identity")
        retained_path = record.get("retained_path", record["filename"])
        if retained_path not in (record["filename"], f"{product}/{record['filename']}"):
            raise ValueError("Invalid retained MRMS relative source path")
        data = (raw_dir / retained_path).read_bytes()
        if (
            str(Digest.of_bytes(data)) != record["content_digest"]
            or len(data) != record["byte_size"]
        ):
            raise ValueError("Retained MRMS raw checksum/size mismatch")
        acquired = datetime.fromisoformat(record["acquired_at"])
        if acquired.tzinfo is None or acquired < instant:
            raise ValueError("Invalid retained MRMS acquisition/availability time")
        _validate_metadata(inspect_mrms(data, product=product), record, instant)
        sources[product] = data
    return metadata, sources


def _validate_metadata(
    parsed: dict[str, Any], source: dict[str, Any], product_time: datetime
) -> None:
    if datetime.fromisoformat(parsed["product_time"]) != product_time:
        raise ValueError("Encoded MRMS product time differs from the retained object identity")
    complete = {key: value for key, value in parsed.items() if key not in ("extraction", "value")}
    if complete != source["parsed_metadata"]:
        raise ValueError("Retained MRMS metadata differs from offline parsed source")


def _extract(
    metadata: dict[str, Any],
    payloads: dict[str, bytes],
    *,
    latitude: float,
    longitude: float,
    raw_artifacts: dict[str, dict[str, str]],
    identity: dict[str, Any],
) -> dict[str, Any]:
    _coordinate(latitude, longitude)
    parsed = {}
    for product in PRODUCTS:
        raw = payloads[product]
        source = metadata["sources"][product]
        if str(Digest.of_bytes(raw)) != source["content_digest"]:
            raise ValueError("MRMS transformation raw checksum mismatch")
        row = extract_mrms(raw, product=product, latitude=latitude, longitude=longitude)
        # Require the complete metadata contract, including indicated object time.
        _validate_metadata(row, source, datetime.fromisoformat(metadata["product_time"]))
        parsed[product] = row
    for product in PRODUCTS[1:]:
        validate_support_alignment(parsed[PRODUCTS[0]], parsed[product])
    return {
        "schema_version": _EXTRACTION_SCHEMA,
        "normalization_schema_version": SCHEMA_VERSION,
        "reference_type": "gridded_precipitation_analysis_not_point_gauge_or_perfect_truth",
        "qpe": parsed[PRODUCTS[0]],
        "quality_support": {product: parsed[product] for product in PRODUCTS[1:]},
        "quality_threshold_policy": "none_approved_raw_evidence_only",
        "raw_sources": raw_artifacts,
        "source_bundle": metadata,
        "preparation_code_identity": identity,
    }


def register_bundle(
    raw_dir: Path,
    *,
    latitude: float,
    longitude: float,
    artifacts: ArtifactService,
    configuration_snapshot_id: ConfigurationSnapshotId,
    configuration_digest: Digest,
) -> dict[str, Any]:
    """Use existing immutable source/artifact activities, without creating verification facts."""
    _coordinate(latitude, longitude)
    metadata, payloads = load_bundle(raw_dir)
    return _register_payloads(
        metadata,
        payloads,
        latitude=latitude,
        longitude=longitude,
        artifacts=artifacts,
        configuration_snapshot_id=configuration_snapshot_id,
        configuration_digest=configuration_digest,
    )


def _register_payloads(
    metadata: dict[str, Any],
    payloads: dict[str, bytes],
    *,
    latitude: float,
    longitude: float,
    artifacts: ArtifactService,
    configuration_snapshot_id: ConfigurationSnapshotId,
    configuration_digest: Digest,
) -> dict[str, Any]:
    """The same transformation serves retained filesystem and object-store inputs."""
    identity = _identity()
    refs = {}
    for product in PRODUCTS:
        source = metadata["sources"][product]
        acquired = datetime.fromisoformat(source["acquired_at"])
        original = source.get("acquisition_code_identity", metadata["acquisition_code_identity"])
        manifest = artifacts.register_source(
            SourceRegistrationRequest(
                source_authority="noaa.ncep.mrms",
                source_locator=source["url"],
                # MRMS provides object identity, not a stable meteorological revision number.
                source_revision=source["content_digest"],
                artifact_type="mrms-grib-source",
                artifact_schema_version="mrms-grib-source.v1",
                media_type="application/gzip",
                expected_content_digest=Digest(source["content_digest"]),
                created_at=acquired,
                availability=Availability(
                    available_at=acquired,
                    ingested_at=acquired,
                    authority="mesoforge",
                    method="local-acquisition-complete",
                ),
                configuration_snapshot_id=configuration_snapshot_id,
                configuration_digest=configuration_digest,
                code_revision=original["git_commit"],
                environment_digest=Digest.of_bytes(_JSON.serialize(original)),
                attributes=source,
            ),
            payloads[product],
        )
        refs[product] = {
            "artifact_id": str(manifest.artifact_id),
            "content_digest": str(manifest.content_digest),
        }
    request = TransformationRequest(
        activity_type="extract-mrms-coordinate",
        activity_version="v1",
        inputs=tuple(
            TransformationInputRef(
                role=product, artifact_id=ArtifactId(refs[product]["artifact_id"])
            )
            for product in PRODUCTS
        ),
        output_role="extraction",
        output_artifact_type="mrms-coordinate-extraction",
        output_artifact_schema_version=_EXTRACTION_SCHEMA,
        output_media_type="application/json",
        parameters={
            "latitude": latitude,
            "longitude": longitude,
            "source_bundle": metadata,
            "preparation_code_identity": identity,
        },
        configuration_snapshot_id=configuration_snapshot_id,
        configuration_digest=configuration_digest,
        code_revision=identity["git_commit"],
        environment_digest=Digest.of_bytes(_JSON.serialize(identity)),
        attributes={
            "product_time": metadata["product_time"],
            "latitude": latitude,
            "longitude": longitude,
        },
    )

    def transform(*inputs: bytes) -> dict[str, Any]:
        return _extract(
            metadata,
            dict(zip(PRODUCTS, inputs, strict=True)),
            latitude=latitude,
            longitude=longitude,
            raw_artifacts=refs,
            identity=identity,
        )

    def validate(value: dict[str, Any]) -> None:
        if value["schema_version"] != _EXTRACTION_SCHEMA:
            raise ValueError("Invalid MRMS extraction schema")
        for support in value["quality_support"].values():
            validate_support_alignment(value["qpe"], support)

    result = artifacts.execute_raw_transformation(
        request, transform, _JSON, input_loader=bytes, output_validator=validate
    )
    _, saved = artifacts.load_verified_payload(result.output.artifact_id)
    return {
        "extraction_artifact_id": str(result.output.artifact_id),
        "content_digest": str(result.output.content_digest),
        "byte_size": len(saved),
        "extraction": _JSON.deserialize(saved),
    }


def replay_registered(artifact_id: ArtifactId, *, artifacts: ArtifactService) -> dict[str, Any]:
    """Reparse immutable raw objects and compare exact saved extraction; reads only."""
    manifest, saved = artifacts.load_verified_payload(artifact_id)
    if manifest.artifact_schema_version != _EXTRACTION_SCHEMA:
        raise ValueError("Artifact is not an MRMS coordinate extraction")
    original = _JSON.deserialize(saved)
    payloads, refs = {}, {}
    for product in PRODUCTS:
        row = original["raw_sources"][product]
        raw_manifest, raw = artifacts.load_verified_payload(ArtifactId(row["artifact_id"]))
        if str(raw_manifest.content_digest) != row["content_digest"]:
            raise ValueError("Saved extraction refers to inconsistent raw identity")
        payloads[product] = raw
        refs[product] = {key: row[key] for key in ("artifact_id", "content_digest")}
    coordinate = original["qpe"]["extraction"]["forecast_coordinate"]
    reproduced = _extract(
        original["source_bundle"],
        payloads,
        latitude=coordinate["latitude"],
        longitude=coordinate["longitude"],
        raw_artifacts=refs,
        identity=original["preparation_code_identity"],
    )
    if _JSON.serialize(reproduced) != saved:
        raise ValueError("Offline MRMS extraction does not exactly reproduce saved artifact")
    return {
        "extraction_artifact_id": str(artifact_id),
        "content_digest": str(manifest.content_digest),
        "offline_replay_identical": True,
        "provider_calls": 0,
        "storage_writes": 0,
        "extraction": reproduced,
    }


class MRMSResolutionError(ValueError):
    """Resolution failed, with actual acquisition cost retained for reporting."""

    provider_calls: int = 0
    acquired_bytes: int = 0


class MRMSUnavailableError(MRMSResolutionError):
    """A fixed provider object is absent now; a later attempt may find it."""


class MRMSProviderError(MRMSResolutionError):
    """The provider failed; this is not a native missing/no-coverage observation."""


class MRMSContractError(MRMSResolutionError):
    """Retained or acquired evidence cannot satisfy the approved source contract."""


class _MeasuredTransport:
    def __init__(self, transport: HttpTransport | None) -> None:
        self.transport = transport
        self.calls = 0
        self.byte_size = 0

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Any:
        self.calls += 1
        try:
            if self.transport is None:
                self.transport = default_transport()
            response = self.transport.get(url, headers=headers, timeout=timeout)
        except Exception as exc:
            raise MRMSProviderError(f"MRMS provider request failed: {url}: {exc}") from exc
        self.byte_size += len(response.content)
        if response.status_code == 404:
            raise MRMSUnavailableError(f"MRMS fixed object is not available yet (HTTP 404): {url}")
        if response.status_code != 200:
            raise MRMSProviderError(f"MRMS provider returned HTTP {response.status_code}: {url}")
        return response

    def head(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Bounded MRMS resolution does not perform discovery/HEAD requests")


def _commit_raw_packet(staging: Path, packet: Path) -> None:
    """Atomically retain a packet, tolerating only brief Windows sharing failures."""
    delays = (0.0, 0.05, 0.1, 0.2, 0.4)
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        if packet.exists():
            raise FileExistsError(f"Refusing to replace retained MRMS packet: {packet}")
        try:
            staging.rename(packet)
            return
        except OSError as exc:
            # Antivirus/indexer handles can briefly deny an otherwise valid rename.
            # Never retry other errors or alter/remove either directory to force it.
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == len(delays) - 1:
                raise


class MRMSHourResolver:
    """Reuse one immutable source hour across coordinates, jobs and bounded retries.

    PostgreSQL's existing cross-process advisory lock coordinates the hour. Raw
    product packets are committed by directory rename, so an interrupted/failed
    later product never discards a completed earlier source. PostgreSQL/MinIO
    remain authoritative for registered extraction/raw identities; local packets
    are a resumable acquisition cache outside the repository.
    """

    def __init__(
        self,
        raw_root: Path,
        *,
        artifacts: ArtifactService,
        unit_of_work_factory: Callable[[], Any],
        configuration_snapshot_id: ConfigurationSnapshotId,
        configuration_digest: Digest,
        idempotency_lock: Any,
        transport: HttpTransport | None = None,
        clock: Clock | None = None,
        archive: bool = False,
    ) -> None:
        if raw_root.resolve().is_relative_to(_ROOT):
            raise ValueError("Raw MRMS sources must be retained outside the repository")
        self.raw_root = raw_root
        self.artifacts = artifacts
        self.factory = unit_of_work_factory
        self.configuration_snapshot_id = configuration_snapshot_id
        self.configuration_digest = configuration_digest
        self.lock = idempotency_lock
        self.transport = transport
        self.clock = clock or SystemClock()
        self.archive = archive

    def _retained(self, product_time: datetime) -> list[dict[str, Any]]:
        with self.factory() as uow:
            candidates = uow.artifacts.find_mrms_extractions(product_time=product_time, limit=1000)
        if len(candidates) == 1000:
            raise MRMSContractError("Retained MRMS lookup limit reached; resolve explicitly")
        matching = []
        identities = set()
        for manifest in candidates:
            _, payload = self.artifacts.load_verified_payload(manifest.artifact_id)
            value = _JSON.deserialize(payload)
            if (
                manifest.artifact_schema_version != _EXTRACTION_SCHEMA
                or value.get("schema_version") != _EXTRACTION_SCHEMA
            ):
                raise MRMSContractError("Unsupported retained MRMS extraction schema")
            if datetime.fromisoformat(value["source_bundle"]["product_time"]) != product_time:
                continue
            identities.add(tuple(value["raw_sources"][p]["content_digest"] for p in PRODUCTS))
            matching.append(
                {
                    "extraction_artifact_id": str(manifest.artifact_id),
                    "content_digest": str(manifest.content_digest),
                    "byte_size": len(payload),
                    "extraction": value,
                }
            )
        if len(identities) > 1:
            raise MRMSContractError("Conflicting retained MRMS source revisions; select explicitly")
        return matching

    def _acquire(self, product_time: datetime, transport: _MeasuredTransport) -> Path:
        directory = self.raw_root / product_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "manifest.json").exists():
            existing = json.loads((directory / "manifest.json").read_bytes())
            if datetime.fromisoformat(existing["product_time"]) != product_time:
                raise MRMSContractError("Retained MRMS cache belongs to another source hour")
            return directory
        sources = {}
        for product in PRODUCTS:
            packet = directory / product
            if not packet.exists():
                # Incomplete staging directories retain any received evidence, but
                # are never mistaken for a complete source packet on retry.
                staging = Path(tempfile.mkdtemp(prefix=f".{product}-", dir=directory))
                raw, acquired = fetch_product(
                    product,
                    product_time,
                    transport=cast(HttpTransport, transport),
                    clock=self.clock,
                    archive=self.archive,
                )
                (staging / acquired["filename"]).write_bytes(raw)
                record = {
                    **acquired,
                    "content_digest": str(Digest.of_bytes(raw)),
                    "byte_size": len(raw),
                    "retained_path": f"{product}/{acquired['filename']}",
                    "acquisition_code_identity": _identity(),
                }
                # Acquisition identity survives a malformed GRIB for inspection.
                (staging / "source.json").write_bytes(_JSON.serialize(record))
                parsed = inspect_mrms(raw, product=product)
                if datetime.fromisoformat(parsed["product_time"]) != product_time:
                    raise MRMSContractError("MRMS indicated time differs from requested hour")
                record["parsed_metadata"] = parsed
                (staging / "source.json").write_bytes(_JSON.serialize(record))
                _commit_raw_packet(staging, packet)
            sources[product] = json.loads((packet / "source.json").read_bytes())
        metadata = {
            "schema_version": _BUNDLE_SCHEMA,
            "product_time": product_time.astimezone(UTC).isoformat(),
            "acquisition_code_identity": _identity(),
            "sources": sources,
        }
        temporary = directory / "manifest.pending.json"
        temporary.write_bytes(_JSON.serialize(metadata))
        temporary.replace(directory / "manifest.json")
        return directory

    def resolve_hour(
        self,
        *,
        latitude: float,
        longitude: float,
        product_time: datetime,
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        """Resolve exactly one hour; cutoff limits requested time, not receipt identity."""
        _coordinate(latitude, longitude)
        source_url(PRODUCTS[0], product_time, archive=self.archive)
        now = cutoff or self.clock.now()
        if now.tzinfo is None or product_time > now:
            raise ValueError("MRMS resolution needs an aware cutoff at or after product time")
        product_time = product_time.astimezone(UTC)
        key = Digest.of_bytes(f"mrms.retained-hour.v1:{product_time.isoformat()}".encode())
        # Recheck after waiting: a competing process may have completed this hour.
        with self.lock.acquire(key):
            measured = None
            try:
                retained = self._retained(product_time)
                for result in retained:
                    coordinate = result["extraction"]["qpe"]["extraction"]["forecast_coordinate"]
                    if coordinate == {"latitude": latitude, "longitude": longitude}:
                        return {
                            **result,
                            "acquisition": {
                                "provider_calls": 0,
                                "acquired_bytes": 0,
                                "raw_reused": True,
                                "extraction_reused": True,
                                "extraction_bytes": result["byte_size"],
                            },
                        }
                if retained:
                    previous = retained[0]["extraction"]
                    payloads = {}
                    for product, reference in previous["raw_sources"].items():
                        manifest, raw = self.artifacts.load_verified_payload(
                            ArtifactId(reference["artifact_id"])
                        )
                        if str(manifest.content_digest) != reference["content_digest"]:
                            raise MRMSContractError("Retained MRMS raw reference checksum differs")
                        payloads[product] = raw
                    result = _register_payloads(
                        previous["source_bundle"],
                        payloads,
                        latitude=latitude,
                        longitude=longitude,
                        artifacts=self.artifacts,
                        configuration_snapshot_id=self.configuration_snapshot_id,
                        configuration_digest=self.configuration_digest,
                    )
                else:
                    measured = _MeasuredTransport(self.transport)
                    directory = self._acquire(product_time, measured)
                    result = register_bundle(
                        directory,
                        latitude=latitude,
                        longitude=longitude,
                        artifacts=self.artifacts,
                        configuration_snapshot_id=self.configuration_snapshot_id,
                        configuration_digest=self.configuration_digest,
                    )
                return {
                    **result,
                    "acquisition": {
                        "provider_calls": measured.calls if measured else 0,
                        "acquired_bytes": measured.byte_size if measured else 0,
                        "raw_reused": measured is None or measured.calls == 0,
                        "extraction_reused": False,
                        "extraction_bytes": result["byte_size"],
                    },
                }
            except (MRMSUnavailableError, MRMSProviderError) as exc:
                exc.provider_calls = measured.calls if measured else 0
                exc.acquired_bytes = measured.byte_size if measured else 0
                raise
            except Exception as exc:
                failure = MRMSContractError(f"MRMS source/extraction contract failed: {exc}")
                failure.provider_calls = measured.calls if measured else 0
                failure.acquired_bytes = measured.byte_size if measured else 0
                raise failure from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--time", type=datetime.fromisoformat)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument(
        "--archive", action="store_true", help="Acquire the fixed hour from NOAA's CONUS archive"
    )
    parser.add_argument(
        "--from-raw", action="store_true", help="Reuse raw bundle, no provider calls"
    )
    parser.add_argument(
        "--replay-artifact", help="Read-only offline replay of a saved extraction ID"
    )
    args = parser.parse_args(argv)
    if args.replay_artifact:
        if (
            args.from_raw
            or args.archive
            or any(v is not None for v in (args.raw_dir, args.time, args.lat, args.lon))
        ):
            parser.error("Use --replay-artifact alone")
    elif args.raw_dir is None or args.lat is None or args.lon is None:
        parser.error("Supply --raw-dir, --lat and --lon")
    elif (args.from_raw and args.time is not None) or (not args.from_raw and args.time is None):
        parser.error("Supply --time for acquisition, or --from-raw for retained data")
    elif args.from_raw and args.archive:
        parser.error("--archive selects acquisition only; --from-raw uses retained source URLs")
    try:
        if not args.replay_artifact and not args.from_raw:
            _coordinate(args.lat, args.lon)
            acquire_bundle(args.raw_dir, product_time=args.time, archive=args.archive)
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
        if args.replay_artifact:
            result = replay_registered(ArtifactId(args.replay_artifact), artifacts=artifacts)
        else:
            configuration, _ = load_configuration_source(base_path=_ROOT / "configs/base.yaml")
            snapshot = ConfigurationService(factory).register(configuration)
            result = register_bundle(
                args.raw_dir,
                latitude=args.lat,
                longitude=args.lon,
                artifacts=artifacts,
                configuration_snapshot_id=snapshot.configuration_snapshot_id,
                configuration_digest=snapshot.configuration_digest,
            )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
