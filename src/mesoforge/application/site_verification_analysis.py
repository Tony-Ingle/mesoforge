"""Read-only site verification analysis over saved temperature verification facts.

Reads issuance metadata rows and, per fact, either its compact analytical attributes
or (for facts saved before those existed) its immutable payload. Both go through one
projection, so the result does not depend on the path. No issued forecast object is
read. Nothing is written, acquired, calculated as a forecast or corrected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mesoforge.application.accumulation_status import _validate
from mesoforge.application.weather_transitions import validate_display_timezone
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.issued_forecasts import IssuedForecastRecord
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.storage.s3 import S3ArtifactObjectStore
from mesoforge.verification.analytical_attributes import (
    ANALYTICAL_SCHEMA_VERSION,
    analytical_block,
    build_analytical_attributes,
)
from mesoforge.verification.model_comparison import is_raw_temperature_control
from mesoforge.verification.site_analysis import (
    ANALYSIS_POLICY,
    CANONICALIZATION_POLICY,
    DECISION_WINDOW_POLICY,
    EVIDENCE_POLICY,
    SCHEMA_VERSION,
    analyze_facts,
    fact_exclusion_reason,
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


def fact_record(
    manifest: ArtifactManifest, block: dict[str, Any], *, indexed: bool
) -> dict[str, Any]:
    """Join repository metadata with one compact block, whichever path produced it."""
    return {
        **{
            key: value
            for key, value in block.items()
            if key not in {"schema_version", "fact_schema_version"}
        },
        "artifact_id": str(manifest.artifact_id),
        "registered_at": manifest.registered_at,
        "indexed": indexed,
        "quality_state": manifest.quality_state,
        "schema_version": block.get("fact_schema_version"),
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
    payload_only: bool = False,
    now: datetime | None = None,
    as_of: datetime | None = None,
    raw_baseline_only: bool = False,
    unit_of_work_factory: Callable[[], Any] | None = None,
    load_payload: Callable[[ArtifactManifest], bytes] | None = None,
) -> dict[str, Any]:
    """Describe verified temperature errors for one coordinate from canonical samples.

    ``payload_only`` ignores compact attributes and projects every fact from its
    authoritative payload; it exists to audit that both paths agree.
    """
    latitude, longitude = _validate(latitude, longitude)
    if display_timezone is not None:
        validate_display_timezone(display_timezone)
    evaluated_at = now if now is not None else datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("The evaluation time must include a timezone")
    if as_of is not None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("The evidence cutoff must include a timezone")
        if as_of > evaluated_at:
            raise ValueError("The evidence cutoff cannot follow the evaluation time")
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
    loader: list[Callable[[ArtifactManifest], bytes]] = [load_payload] if load_payload else []
    payload_bytes = payloads_read = attribute_bytes = from_attributes = 0
    fallback_reasons: Counter[str] = Counter()
    unreadable: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    legacy_for_coordinate = 0
    legacy_examined = legacy[:LEGACY_SCAN_LIMIT]
    for manifest, is_indexed in [(m, True) for m in indexed] + [
        (m, False) for m in legacy_examined
    ]:
        block, reason = (None, "unindexed_legacy_fact")
        if is_indexed:
            block, reason = analytical_block(manifest.attributes)
            if payload_only:
                block, reason = None, "payload_only_requested"
            elif raw_baseline_only and block is not None and "forecast_stage" not in block:
                block, reason = None, "legacy_forecast_stage_identity_unavailable"
            elif (
                raw_baseline_only
                and block is not None
                and isinstance(block.get("forecast_stage"), dict)
                and block["forecast_stage"].get("transformation_type") == "ai_adjusted"
                and "raw_baseline_temperature" not in block
            ):
                # Only AI-stage facts can carry a saved raw projection in the payload.
                # Historical corrected facts are excluded from the compact attributes
                # below without reading multi-megabyte payloads.
                block, reason = None, "legacy_raw_baseline_projection_unavailable"
        if block is not None:
            from_attributes += 1
            attribute_bytes += len(canonical_json_bytes(block))
            source = "compact_attributes"
        else:
            if not loader:
                loader.append(_configured_loader())
            try:
                raw = loader[0](manifest)
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
            block, source = build_analytical_attributes(payload), "payload"
        fact = fact_record(manifest, block, indexed=is_indexed)
        if not is_indexed:
            # Identity recovered from the immutable payload; other coordinates are not ours.
            if fact["latitude"] != latitude or fact["longitude"] != longitude:
                continue
            legacy_for_coordinate += 1
        if source == "payload":
            fallback_reasons[reason] += 1
        exclusion = _metadata_exclusion(fact, records)
        if exclusion is not None:
            fact["integrity_exclusion"] = exclusion
        elif as_of is not None:
            # Filter facts before canonicalization: a future revision must neither
            # teach this decision nor make previously known evidence ambiguous.
            try:
                cutoff = datetime.fromisoformat(str(fact.get("verification_cutoff")))
                if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                    raise ValueError("unproven verification availability")
            except (TypeError, ValueError):
                fact["integrity_exclusion"] = "learning_evidence_availability_unproven"
            else:
                if cutoff > as_of or manifest.registered_at > as_of:
                    fact["integrity_exclusion"] = "learning_evidence_after_cutoff"
        if raw_baseline_only and not is_raw_temperature_control(fact.get("forecast_stage")):
            # Validate the immutable issued-stage fact before projecting another
            # saved stage. This cannot hide an invalid issued error or bypass the
            # availability filter above. Observations/canonicalization stay shared.
            prior_exclusion = fact_exclusion_reason(fact)
            raw_stage = fact.get("raw_baseline_temperature")
            if prior_exclusion is not None:
                fact["integrity_exclusion"] = prior_exclusion
            elif not isinstance(raw_stage, dict) or raw_stage.get("status") != "available":
                fact["integrity_exclusion"] = (
                    raw_stage.get("reason", "nonbaseline_temperature_stage")
                    if isinstance(raw_stage, dict)
                    else "nonbaseline_temperature_stage"
                )
            else:
                fact["stage_projection"] = {
                    **raw_stage,
                    "issued_stage": fact.get("forecast_stage"),
                    "issued_temperature_k": fact["forecast_temperature_k"],
                    "issued_error_k": fact["temperature_error_k"],
                }
                fact["forecast_temperature_k"] = raw_stage["value"]
                fact["temperature_error_k"] = (
                    raw_stage["value"] - fact["observation"]["temperature_k"]
                )
                fact["forecast_stage"] = {
                    "variant_id": raw_stage["variant_id"],
                    "transformation_type": "active_baseline",
                }
        facts.append(fact)

    analysis = analyze_facts(facts, display_timezone=display_timezone)
    if raw_baseline_only:
        projections = {
            str(fact["artifact_id"]): fact["stage_projection"]
            for fact in facts
            if "stage_projection" in fact
        }
        for sample in analysis["samples"]:
            sample["provenance"]["raw_baseline_projections"] = [
                {"fact_id": identity, **projections[identity]}
                for version in sample["provenance"]["versions"]
                for identity in version["fact_ids"]
                if identity in projections
            ]
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
        "decision_window_policy": DECISION_WINDOW_POLICY,
        "evidence_policy": EVIDENCE_POLICY,
        "coordinate": {"latitude": latitude, "longitude": longitude},
        "evaluation": {
            "evaluated_at": evaluated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "note": "Without as_of, only this block depends on the clock.",
            **({"forecast_stage_scope": "raw_baseline_only"} if raw_baseline_only else {}),
            **(
                {
                    "evidence_cutoff": as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    "evidence_availability": "verified_input_cutoff_and_fact_registration",
                }
                if as_of is not None
                else {}
            ),
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
            "fact_sources": {
                "analytical_schema_version": ANALYTICAL_SCHEMA_VERSION,
                "compact_attributes": from_attributes,
                "payload_fallback": sum(fallback_reasons.values()),
                "payload_fallback_reasons": dict(sorted(fallback_reasons.items())),
            },
            "analytically_usable_facts": canonical["usable_facts"],
            "excluded_facts": len(canonical["excluded_facts"]),
            "excluded_by_reason": canonical["excluded_by_reason"],
            "ambiguous_opportunities_or_samples": len(canonical["ambiguous"]),
        },
        **analysis,
        "reads": {
            "issuance_metadata_rows": len(records),
            "facts_from_compact_attributes": from_attributes,
            "compact_attribute_bytes": attribute_bytes,
            "fact_payloads_read": payloads_read,
            "fact_payload_bytes": payload_bytes,
            "issuance_payloads_read": 0,
            "provider_calls": 0,
            "writes": 0,
            "note": (
                "Compact analytical attributes answer facts that carry them; older facts are "
                "projected from their immutable payload. No issued forecast object is needed."
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
    parser.add_argument(
        "--payload-only",
        action="store_true",
        help="Audit mode: ignore compact attributes and read every fact payload",
    )
    args = parser.parse_args(argv)
    try:
        payload = canonical_json_bytes(
            analyze_site_verification(
                args.lat,
                args.lon,
                display_timezone=args.display_timezone,
                payload_only=args.payload_only,
            )
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
