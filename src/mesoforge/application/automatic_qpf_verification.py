"""Bounded retryable MRMS accumulation around immutable hourly QPF verification."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mesoforge.application.batch_forecast import _coordinates, load_locations
from mesoforge.application.issuance import validate_hour_selection, version_may_overlap
from mesoforge.application.issued_qpf_verification import (
    SCHEMA,
    IssuedQpfVerificationService,
    configured_service,
)
from mesoforge.common.identifiers import ArtifactId
from mesoforge.verification.issued_qpf import evaluate_qpf_verification

# Lookup pacing from the approved MRMS product contract, NOT proof of availability.
MRMS_EXPECTED_LATENCY = timedelta(hours=1)
DEFAULT_LOOKBACK_HOURS = 72


def accumulate_window(
    *,
    service: IssuedQpfVerificationService,
    resolve_hour: Callable[..., dict[str, Any]],
    latitude: float,
    longitude: float,
    start_valid_time: datetime,
    end_valid_time: datetime,
    max_issuances: int,
    max_opportunities: int,
    now: datetime | None = None,
    stages: Sequence[str] = ("baseline", "final_issued"),
) -> dict[str, Any]:
    """Visit bounded saved opportunities; only actual retained evidence creates a fact.

    Bounds select valid END times in [start,end). The opportunity work budget counts
    unresolved issued-version/stage/hour attempts; saved facts do not spend it.
    Recent decisions/hours take priority so unavailable older history cannot exhaust
    every forward run. Omitted historical work requires a narrower explicit window.
    This is orchestration, not another forecast/matching or canonicalization policy.
    """
    from mesoforge.application.prepared_mrms import MRMSContractError, MRMSUnavailableError

    window = validate_hour_selection(latitude, longitude, start_valid_time, end_valid_time)
    evaluation = now or service.clock()
    if evaluation.tzinfo is None or evaluation.utcoffset() is None:
        raise ValueError("Evaluation time must include a timezone")
    if evaluation > service.clock():
        raise ValueError("Evaluation time cannot be in the future")
    if any(type(n) is not int or not 1 <= n <= 10000 for n in (max_issuances, max_opportunities)):
        raise ValueError("Explicit maximum issuances/opportunities must be within 1..10000")
    stages = tuple(dict.fromkeys(stages))
    if not stages or set(stages) - {"baseline", "final_issued"}:
        raise ValueError("Select baseline and/or final_issued stages")
    with service.factory() as uow:
        records = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        facts = uow.artifacts.find_issued_qpf_verifications(
            latitude=latitude,
            longitude=longitude,
            start_valid_time=window.start,
            end_valid_time=window.end,
            limit=10001,
        )
    if len(facts) > 10000:
        raise ValueError("Fact inventory exceeds bounded limit; narrow the requested window")
    existing: dict[str, list[str]] = {}
    completed: dict[tuple[str, str, datetime], dict[str, Any]] = {}
    record_index = {str(r.issued_forecast_id): r for r in records}
    for manifest in facts:
        fact = (manifest.attributes or {}).get("analysis", {})
        if manifest.artifact_schema_version != SCHEMA or manifest.quality_state == "invalid":
            continue
        record = record_index.get(fact.get("issued_forecast_id"))
        if record is None or fact.get("issued_forecast_digest") != str(record.payload_digest):
            continue
        if (
            fact.get("status") == "verified"
            or set(fact.get("reasons", []))
            in ({"observation_missing"}, {"observation_no_coverage"})
            and fact.get("observation") is not None
        ):
            existing.setdefault(fact["opportunity_id"], []).append(str(manifest.artifact_id))
            completed[
                (
                    fact["issued_forecast_id"],
                    fact["stage"],
                    datetime.fromisoformat(fact["valid_time"]),
                )
            ] = fact
    records = sorted(
        (r for r in records if version_may_overlap(r, window)),
        key=lambda r: (r.target_reference_time, r.issued_at, str(r.issued_forecast_id)),
        reverse=True,
    )
    rows: list[dict[str, Any]] = []
    resolved: dict[datetime, dict[str, Any]] = {}
    attempts = 0
    issuance_reads = issuance_omissions = 0
    acquired_bytes = provider_calls = extraction_bytes = 0
    for record in records:
        # Fully answered versions do not consume the read budget or starve newer
        # unresolved versions. These are known immutable fact identities, not new facts.
        keys = [
            (
                str(record.issued_forecast_id),
                stage,
                record.target_reference_time + timedelta(hours=lead),
            )
            for lead in range(1, record.forecast_horizon_hours + 1)
            for stage in stages
            if window.start <= record.target_reference_time + timedelta(hours=lead) < window.end
        ]
        if keys and all(key in completed for key in keys):
            for key in keys:
                retained = completed[key]
                rows.append(
                    {
                        "issued_forecast_id": key[0],
                        "stage": key[1],
                        "valid_time": key[2].isoformat(),
                        "opportunity_id": retained["opportunity_id"],
                        "interval_start": retained["interval_start"],
                        "interval_end": retained["interval_end"],
                        "status": "already_existing",
                        "verification_ids": existing[retained["opportunity_id"]],
                    }
                )
            continue
        if issuance_reads >= max_issuances:
            issuance_omissions += 1
            continue
        issuance_reads += 1
        try:
            saved = service.issuer.read(record.issued_forecast_id)
        except Exception as exc:
            rows.append(
                {
                    "issued_forecast_id": str(record.issued_forecast_id),
                    "status": "error",
                    "reasons": [f"issuance_read_failed: {exc}"],
                }
            )
            continue
        for hour in sorted(
            saved["forecast"]["hours"],
            key=lambda h: datetime.fromisoformat(h["valid_time"]),
            reverse=True,
        ):
            valid = datetime.fromisoformat(hour["valid_time"])
            if not window.start <= valid < window.end:
                continue
            for stage in stages:
                fact = evaluate_qpf_verification(
                    saved,
                    valid,
                    stage=stage,
                    issued_forecast_digest=record.payload_digest,
                    verification_cutoff=evaluation,
                )
                row: dict[str, Any] = {
                    "issued_forecast_id": str(record.issued_forecast_id),
                    "stage": stage,
                    "valid_time": hour["valid_time"],
                    "opportunity_id": fact["opportunity_id"],
                    "interval_start": fact["interval_start"],
                    "interval_end": fact["interval_end"],
                    "mrms_expected_after": (valid + MRMS_EXPECTED_LATENCY).isoformat(),
                }
                rows.append(row)
                if fact["opportunity_id"] in existing:
                    row.update(
                        status="already_existing", verification_ids=existing[fact["opportunity_id"]]
                    )
                    continue
                if valid > evaluation:
                    row.update(status="future", reasons=["forecast_interval_not_yet_complete"])
                    continue
                reasons = [r for r in fact["reasons"] if r != "observation_missing"]
                if reasons:
                    row.update(status="excluded", reasons=reasons)
                    continue
                if valid + MRMS_EXPECTED_LATENCY > evaluation:
                    row.update(
                        status="not_yet_expected", reasons=["mrms_documented_latency_not_elapsed"]
                    )
                    continue
                if attempts >= max_opportunities:
                    row.update(status="limit_reached", reasons=["maximum_opportunities_reached"])
                    continue
                attempts += 1
                row["eligibility"] = "eligible_for_mrms_lookup"
                if valid not in resolved:
                    try:
                        resolved[valid] = resolve_hour(
                            latitude=latitude,
                            longitude=longitude,
                            product_time=valid,
                        )
                        acquisition = resolved[valid]["acquisition"]
                        acquired_bytes += acquisition["acquired_bytes"]
                        provider_calls += acquisition["provider_calls"]
                        extraction_bytes += acquisition["extraction_bytes"]
                    except MRMSUnavailableError as exc:
                        provider_calls += exc.provider_calls
                        acquired_bytes += exc.acquired_bytes
                        resolved[valid] = {"status": "retryable", "reasons": [str(exc)]}
                    except MRMSContractError as exc:
                        provider_calls += exc.provider_calls
                        acquired_bytes += exc.acquired_bytes
                        resolved[valid] = {"status": "malformed_unusable", "reasons": [str(exc)]}
                    except Exception as exc:
                        provider_calls += getattr(exc, "provider_calls", 0)
                        acquired_bytes += getattr(exc, "acquired_bytes", 0)
                        resolved[valid] = {
                            "status": "retryable",
                            "reasons": [f"provider_or_storage_failure: {exc}"],
                        }
                evidence = resolved[valid]
                if "extraction_artifact_id" not in evidence:
                    row.update(evidence)
                    row["retryable"] = True
                    continue
                try:
                    outcome = service._verify_saved(
                        record,
                        saved,
                        valid,
                        stage=stage,
                        extraction_id=ArtifactId(evidence["extraction_artifact_id"]),
                        # Acquisition/registration may finish after the initial preflight.
                        # Use the real current cutoff, never invent earlier availability.
                        cutoff=service.clock(),
                    )
                    result = outcome["result"]
                    status = "matched" if result["status"] == "verified" else "excluded"
                    if "observation_no_coverage" in result["reasons"]:
                        status = "native_no_coverage"
                    elif "observation_missing" in result["reasons"]:
                        status = "native_missing"
                    row.update(
                        status=status,
                        reasons=result["reasons"],
                        verification_id=outcome["verification_id"],
                        already_existing=outcome["already_existing"],
                        extraction_artifact_id=evidence["extraction_artifact_id"],
                    )
                except Exception as exc:
                    row.update(status="error", retryable=True, reasons=[str(exc)])
    return {
        "latitude": latitude,
        "longitude": longitude,
        "evaluated_at": evaluation.isoformat(),
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "closure": "[start,end)",
        },
        "limits": {
            "priority": "newest_decision_then_latest_valid_time",
            "max_issuances": max_issuances,
            "max_opportunities": max_opportunities,
            "issuances_considered": len(records),
            "issuances_read": issuance_reads,
            "issuances_omitted": issuance_omissions,
            "attempts": attempts,
        },
        "summary": dict(Counter(r["status"] for r in rows)),
        "results": rows,
        "acquisition": {
            "acquired_bytes": acquired_bytes,
            "provider_calls": provider_calls,
            "extraction_bytes_resolved": extraction_bytes,
            "hours": [
                {"product_time": t.isoformat(), **{k: v for k, v in e.items() if k != "extraction"}}
                for t, e in sorted(resolved.items())
            ],
        },
        "status": (
            "partial"
            if issuance_omissions
            or any(
                r["status"] in {"error", "retryable", "malformed_unusable", "limit_reached"}
                for r in rows
            )
            else "completed"
            if rows
            else "nothing_to_verify"
        ),
    }


def configured_accumulator(*, archive: bool = False) -> tuple[IssuedQpfVerificationService, Any]:
    from mesoforge.application.prepared_mrms import MRMSHourResolver
    from mesoforge.storage.postgres.database import resolve_database_dsn
    from mesoforge.storage.postgres.idempotency_lock import PostgresIdempotencyLock

    service = configured_service()
    config = service.configuration()
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_DATA_HOME")
        or Path.home() / ".local/share"
    )
    resolver = MRMSHourResolver(
        Path(os.environ.get("MESOFORGE_MRMS_DIR", str(base / "MesoForge/observations/mrms"))),
        artifacts=service.artifacts,
        unit_of_work_factory=service.factory,
        configuration_snapshot_id=config.configuration_snapshot_id,
        configuration_digest=config.configuration_digest,
        idempotency_lock=PostgresIdempotencyLock(resolve_database_dsn("MESOFORGE_DATABASE_DSN")),
        archive=archive,
    )
    return service, resolver


def verify_previous_qpf(
    latitude: float,
    longitude: float,
    *,
    now: datetime,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    max_opportunities: int = 36,
) -> dict[str, Any]:
    if type(lookback_hours) is not int or not 1 <= lookback_hours <= 744:
        raise ValueError(
            "Automatic QPF lookback must be within 1..744 hours; "
            "use bounded backfill for older work"
        )
    service, resolver = configured_accumulator()
    return accumulate_window(
        service=service,
        resolve_hour=resolver.resolve_hour,
        latitude=latitude,
        longitude=longitude,
        start_valid_time=now - timedelta(hours=lookback_hours),
        end_valid_time=now,
        max_issuances=100,
        max_opportunities=max_opportunities,
        now=now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--start-valid-time", type=datetime.fromisoformat, required=True)
    parser.add_argument("--end-valid-time", type=datetime.fromisoformat, required=True)
    parser.add_argument(
        "--max-issuances", type=int, required=True, help="Per-location issuance read bound"
    )
    parser.add_argument(
        "--max-opportunities",
        type=int,
        required=True,
        help="Per-location unresolved stage/hour work bound",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Use fixed-hour NOAA historical archive for missing sources",
    )
    args = parser.parse_args(argv)
    try:
        locations = load_locations(args.config)
        service, resolver = configured_accumulator(archive=args.archive)
        rows = []
        for index, location in enumerate(locations):
            try:
                lat, lon = _coordinates(location)
                result = accumulate_window(
                    service=service,
                    resolve_hour=resolver.resolve_hour,
                    latitude=lat,
                    longitude=lon,
                    start_valid_time=args.start_valid_time,
                    end_valid_time=args.end_valid_time,
                    max_issuances=args.max_issuances,
                    max_opportunities=args.max_opportunities,
                )
                result["analysis"] = service.analyze_window(
                    latitude=lat,
                    longitude=lon,
                    start_valid_time=args.start_valid_time,
                    end_valid_time=args.end_valid_time,
                    stages=("baseline", "final_issued"),
                )
                rows.append({"index": index, "location": location, **result})
            except Exception as exc:
                rows.append(
                    {"index": index, "location": location, "status": "error", "reason": str(exc)}
                )
        print(json.dumps({"results": rows}, indent=2, allow_nan=False))
        return int(any(r["status"] in {"error", "partial"} for r in rows))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
