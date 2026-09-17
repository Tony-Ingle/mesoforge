"""Read-only site verification analysis over saved temperature verification facts.

Reads issuance metadata rows and the immutable verification fact payloads of one
coordinate; every value needed for error statistics and forecast context is inside
those facts, so no issued forecast object is read. Nothing is written, acquired,
calculated as a forecast or corrected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mesoforge.application.accumulation_status import _validate
from mesoforge.application.weather_transitions import validate_display_timezone
from mesoforge.common.identifiers import Digest
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.site_analysis import (
    ANALYSIS_POLICY,
    CANONICALIZATION_POLICY,
    SCHEMA_VERSION,
    analyze_facts,
)

LEGACY_SCAN_LIMIT = 200


def _configured_factory() -> PostgresUnitOfWork:
    return PostgresUnitOfWork(resolve_database_dsn("MESOFORGE_DATABASE_DSN"))


def _configured_loader() -> Callable[[ArtifactManifest], bytes]:
    objects = S3ArtifactObjectStore(
        bucket=os.environ["MESOFORGE_S3_BUCKET"],
        endpoint_url=os.environ["MESOFORGE_S3_ENDPOINT"],
        access_key=os.environ["MESOFORGE_S3_ACCESS_KEY"],
        secret_key=os.environ["MESOFORGE_S3_SECRET_KEY"],
        ensure_bucket=False,
    )
    return lambda manifest: objects.get_verified(manifest.storage_uri, manifest.content_digest)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _kelvin(value: Any) -> Any:
    block = _mapping(value)
    return block.get("value") if block.get("unit") == "K" else None


def extract_fact(
    manifest: ArtifactManifest, payload: dict[str, Any], *, indexed: bool
) -> dict[str, Any]:
    """Flatten one immutable fact payload into the identity and values the analysis uses."""
    match = _mapping(payload.get("match"))
    forecast = _mapping(match.get("forecast"))
    selected = _mapping(match.get("selected"))
    provenance = _mapping(selected.get("provenance"))
    context = _mapping(match.get("forecast_context"))
    inputs = _mapping(match.get("input_provenance"))
    fields = _mapping(_mapping(forecast.get("surface")).get("fields"))
    selection_policy = match.get("selection_policy")
    models = {}
    for group in ("sources", "shadow_sources"):
        for source in forecast.get(group) or []:
            if isinstance(source, dict) and isinstance(source.get("model"), str):
                models[source["model"]] = _kelvin(source.get("temperature"))
    return {
        "artifact_id": str(manifest.artifact_id),
        "registered_at": manifest.registered_at,
        "indexed": indexed,
        "quality_state": manifest.quality_state,
        "schema_version": payload.get("schema_version"),
        "status": payload.get("status"),
        "verification_policy_id": _mapping(payload.get("verification_policy")).get("policy_id"),
        "matching_policy_digest": (
            str(Digest.of_bytes(canonical_json_bytes(selection_policy)))
            if isinstance(selection_policy, dict)
            else None
        ),
        "issued_forecast_id": match.get("issued_forecast_id"),
        "issued_at": match.get("issued_at"),
        "issued_forecast_digest": payload.get("issued_forecast_digest"),
        "target_reference_time": context.get("target_reference_time"),
        "valid_time": forecast.get("valid_time"),
        "horizon_hours": forecast.get("horizon_hours"),
        "latitude": forecast.get("latitude"),
        "longitude": forecast.get("longitude"),
        "forecast_temperature_k": _kelvin(forecast.get("temperature")),
        "temperature_error_k": (
            _mapping(payload.get("temperature_error")).get("value")
            if _mapping(payload.get("temperature_error")).get("unit") == "K"
            else None
        ),
        "verification_cutoff": payload.get("verification_cutoff"),
        "code_commit": _mapping(payload.get("code_identity")).get("git_commit"),
        "observation": {
            "station_id": selected.get("station_id"),
            "catalog_station_id": selected.get("catalog_station_id"),
            "network": selected.get("network"),
            "provider": selected.get("provider"),
            "latitude": selected.get("latitude"),
            "longitude": selected.get("longitude"),
            "elevation_m": selected.get("elevation_m"),
            "distance_km": selected.get("distance_km"),
            "observation_time": selected.get("observation_time"),
            "time_difference_seconds": selected.get("time_difference_seconds"),
            "temperature_k": _kelvin(selected.get("temperature")),
            "revision_digest": provenance.get("revision_digest"),
            "logical_observation_digest": provenance.get("logical_observation_digest"),
            "raw_record_digest": provenance.get("raw_record_digest"),
            "raw_artifact_id": provenance.get("raw_artifact_id"),
            "observations_artifact_id": _mapping(inputs.get("observations")).get("artifact_id"),
        },
        "display_timezone": _mapping(context.get("hourly_report")).get("display_timezone"),
        "context": {
            "fields": {
                name: field.get("value")
                for name, field in fields.items()
                if isinstance(field, dict)
            },
            "model_temperatures_k": models,
        },
    }


def _metadata_exclusion(
    fact: dict[str, Any], records: dict[str, IssuedForecastRecord]
) -> str | None:
    record = records.get(str(fact.get("issued_forecast_id")))
    if record is None:
        return "issued_forecast_not_found_for_coordinate"
    try:
        target = datetime.fromisoformat(str(fact["target_reference_time"]))
        issued = datetime.fromisoformat(str(fact["issued_at"]))
    except (KeyError, TypeError, ValueError):
        return "payload_incomplete"
    if target.tzinfo is None or issued.tzinfo is None:
        return "payload_incomplete"
    if target != record.target_reference_time:
        return "target_reference_time_disagrees_with_issuance_metadata"
    if issued != record.issued_at:
        return "issued_at_disagrees_with_issuance_metadata"
    return None


def analyze_site_verification(
    latitude: float,
    longitude: float,
    *,
    display_timezone: str | None = None,
    now: datetime | None = None,
    unit_of_work_factory: Callable[[], Any] | None = None,
    load_payload: Callable[[ArtifactManifest], bytes] | None = None,
) -> dict[str, Any]:
    """Describe verified temperature errors for one coordinate from canonical samples."""
    latitude, longitude = _validate(latitude, longitude)
    if display_timezone is not None:
        validate_display_timezone(display_timezone)
    evaluated_at = now if now is not None else datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("The evaluation time must include a timezone")
    factory = unit_of_work_factory or _configured_factory
    with factory() as uow:
        issued = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        indexed = uow.artifacts.find_issued_temperature_verifications(
            latitude=latitude, longitude=longitude
        )
        legacy = uow.artifacts.find_unindexed_issued_temperature_verifications(
            limit=LEGACY_SCAN_LIMIT + 1
        )
    records = {str(record.issued_forecast_id): record for record in issued}
    load = load_payload or _configured_loader()
    payload_bytes = 0
    payloads_read = 0
    unreadable: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    legacy_for_coordinate = 0
    legacy_examined = legacy[:LEGACY_SCAN_LIMIT]
    for manifest, is_indexed in [(m, True) for m in indexed] + [
        (m, False) for m in legacy_examined
    ]:
        try:
            raw = load(manifest)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("fact payload is not an object")
        except Exception:
            if is_indexed:
                unreadable.append(
                    {"artifact_id": str(manifest.artifact_id), "reason": "payload_unreadable"}
                )
            continue
        payload_bytes += len(raw)
        payloads_read += 1
        fact = extract_fact(manifest, payload, indexed=is_indexed)
        if not is_indexed:
            # Identity recovered from the immutable payload; other coordinates are not ours.
            if fact["latitude"] != latitude or fact["longitude"] != longitude:
                continue
            legacy_for_coordinate += 1
        reason = _metadata_exclusion(fact, records)
        if reason is not None:
            fact["integrity_exclusion"] = reason
        facts.append(fact)

    analysis = analyze_facts(facts, display_timezone=display_timezone)
    canonical = analysis["canonicalization"]
    canonical["excluded_facts"] = sorted(
        [*canonical["excluded_facts"], *unreadable], key=lambda row: row["artifact_id"]
    )
    for row in unreadable:
        by_reason = canonical["excluded_by_reason"]
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_policy": ANALYSIS_POLICY,
        "canonicalization_policy": CANONICALIZATION_POLICY,
        "coordinate": {"latitude": latitude, "longitude": longitude},
        "evaluation": {
            "evaluated_at": evaluated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "note": "Only this block depends on the clock; all other content is deterministic.",
        },
        "inventory": {
            "issued_versions_for_coordinate": len(records),
            "indexed_facts": len(indexed),
            "legacy_unindexed_facts_examined": len(legacy_examined),
            "legacy_unindexed_facts_for_coordinate": legacy_for_coordinate,
            "legacy_scan": {
                "limit": LEGACY_SCAN_LIMIT,
                "truncated": len(legacy) > LEGACY_SCAN_LIMIT,
                "identity_source": "bounded read of each unindexed immutable fact payload",
            },
            "stored_facts_for_coordinate": len(indexed) + legacy_for_coordinate,
            "analytically_usable_facts": canonical["usable_facts"],
            "excluded_facts": len(canonical["excluded_facts"]),
            "excluded_by_reason": canonical["excluded_by_reason"],
            "ambiguous_opportunities_or_samples": len(canonical["ambiguous"]),
        },
        **analysis,
        "reads": {
            "issuance_metadata_rows": len(records),
            "fact_payloads_read": payloads_read,
            "fact_payload_bytes": payload_bytes,
            "issuance_payloads_read": 0,
            "provider_calls": 0,
            "writes": 0,
            "note": (
                "Forecast values, observation identity, errors and forecast context all come "
                "from the saved fact payloads; no issued forecast object is needed."
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument(
        "--display-timezone",
        help="IANA zone for the day/night grouping; default is the saved report zone or UTC",
    )
    args = parser.parse_args(argv)
    try:
        payload = canonical_json_bytes(
            analyze_site_verification(args.lat, args.lon, display_timezone=args.display_timezone)
        )
    except ValueError as exc:
        error = {"code": "invalid_analysis_request", "message": str(exc)}
    except Exception:
        error = {
            "code": "verification_analysis_failed",
            "message": "Could not read the saved verification history.",
        }
    else:
        sys.stdout.buffer.write(payload + b"\n")
        return 0
    print(canonical_json_bytes({"error": error}).decode("utf-8"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
