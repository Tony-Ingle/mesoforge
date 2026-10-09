"""Read-only accumulation status of one coordinate's issued history and verification facts.

Counts come from issuance metadata rows, verification-fact attributes and retained
observation-source attributes only: no forecast payload, observation or provider
read happens, nothing is written, and no weights, bias or skill are derived.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mesoforge.application.automatic_verification import ELIGIBILITY_MARGIN
from mesoforge.application.issued_temperature_verification import (
    VERIFICATION_ARTIFACT_TYPE,
    VERIFICATION_SCHEMA_VERSION,
)
from mesoforge.application.prepared_observations import load_observation_configuration
from mesoforge.application.station_discovery import POLICY_VERSION
from mesoforge.catalog.configuration import compute_configuration_digest
from mesoforge.contracts.artifacts import ArtifactManifest
from mesoforge.contracts.serialization import canonical_json_bytes
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork
from mesoforge.verification.model_comparison import (
    LEAD_BUCKETS,
    analytical_lead_bucket,
    analytical_lead_buckets,
)

SCHEMA_VERSION = "mesoforge.accumulation-status.v1"
HOUR_STATES = (
    "verified",
    "pending",
    "no_retained_observations",
    "retained_observations_without_fact",
)
STATUS_POLICY: dict[str, Any] = {
    "id": "mesoforge-accumulation-status.v1",
    "hours": (
        "Each saved version holds its declared horizon (legacy 36 h), "
        "valid at target_reference_time + horizon, "
        "so hours are enumerated from issuance metadata without reading forecast payloads."
    ),
    "verified": (
        "A saved issued-temperature-verification fact carries this version and valid time "
        "in its attributes. Facts persisted before attributes existed are not counted."
    ),
    "pending": "valid_time + 15 min is still in the future; observation matching is incomplete.",
    "eligible": (
        "Not pending. Forecast-only eligibility (missingness, source cycles) is not "
        "re-evaluated here; it is decided when verification runs."
    ),
    "no_retained_observations": (
        "Eligible, no fact, and no retained real METAR acquisition for this coordinate and "
        "the current observation configuration covers valid_time +/- 15 min: verification "
        "retained no inputs for this hour (not attempted yet, or the attempt retained nothing)."
    ),
    "retained_observations_without_fact": (
        "Eligible, no fact, but a retained acquisition covers the hour: the attempt found no "
        "eligible station observation, QC failed, or the hour was ineligible. Those reasons "
        "are kept in run reports (previous-verification.json), not in storage."
    ),
    "lead_buckets": list(LEAD_BUCKETS),
    "not_included": "No learned weights, bias corrections, regime labels or skill claims.",
}


def _validate(latitude: Any, longitude: Any) -> tuple[float, float]:
    for value, bound in ((latitude, 90), (longitude, 180)):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not -bound <= value <= bound
        ):
            raise ValueError("latitude and longitude must be finite geographic coordinates")
    return float(latitude), float(longitude)


def _instant(value: Any) -> datetime:
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Stored instants must be timezone-aware")
    return instant.astimezone(UTC)


def _iso(instant: datetime | None) -> str | None:
    return None if instant is None else _instant(instant).isoformat().replace("+00:00", "Z")


def _configured_factory() -> PostgresUnitOfWork:
    return PostgresUnitOfWork(resolve_database_dsn("MESOFORGE_DATABASE_DSN"))


def _fact_index(
    facts: tuple[ArtifactManifest, ...],
) -> tuple[dict[tuple[str, datetime], list[ArtifactManifest]], int]:
    index: dict[tuple[str, datetime], list[ArtifactManifest]] = {}
    skipped = 0
    for manifest in facts:
        attributes = manifest.attributes or {}
        if (
            manifest.artifact_type != VERIFICATION_ARTIFACT_TYPE
            or manifest.artifact_schema_version != VERIFICATION_SCHEMA_VERSION
            or manifest.quality_state == "invalid"
            or attributes.get("verification_status") != "verified"
        ):
            skipped += 1
            continue
        key = (str(attributes["issued_forecast_id"]), _instant(attributes["valid_time"]))
        index.setdefault(key, []).append(manifest)
    return index, skipped


def _coverage(sources: tuple[ArtifactManifest, ...]) -> list[tuple[datetime, datetime]]:
    windows = []
    for source in sources:
        attributes = source.attributes or {}
        if source.quality_state == "invalid":
            continue
        windows.append(
            (_instant(attributes["query_window_start"]), _instant(attributes["query_window_end"]))
        )
    return windows


def _station_evidence(
    sources: tuple[ArtifactManifest, ...], discoveries: tuple[ArtifactManifest, ...]
) -> dict[str, Any]:
    retained = [source for source in sources if source.quality_state != "invalid"]
    if retained:
        newest = retained[-1]
        attributes = newest.attributes or {}
        station_ids = attributes.get("station_ids")
        discovery = attributes.get("station_discovery")
        candidates = discovery.get("candidates") if isinstance(discovery, dict) else None
        exclusions = attributes.get("station_metadata_exclusions")
        return {
            "source": "retained_metar_acquisition",
            "artifact_id": str(newest.artifact_id),
            "acquired_at": attributes.get("acquired_at"),
            "station_ids": list(station_ids) if isinstance(station_ids, list) else [],
            "station_discovery_artifact_id": attributes.get("station_discovery_artifact_id"),
            "discovery_candidates": len(candidates) if isinstance(candidates, list) else None,
            "metadata_exclusions": len(exclusions) if isinstance(exclusions, list) else None,
        }
    for manifest in discoveries:
        if manifest.quality_state == "invalid":
            continue
        attributes = manifest.attributes or {}
        return {
            "source": "station_discovery_response",
            "artifact_id": str(manifest.artifact_id),
            "acquired_at": attributes.get("acquired_at"),
            "discovery_id": attributes.get("discovery_id"),
            "note": "Station metadata was discovered, but no observation acquisition is retained.",
        }
    return {
        "source": None,
        "note": "No station discovery or observation acquisition is retained for this coordinate.",
    }


def accumulation_status(
    latitude: float,
    longitude: float,
    *,
    now: datetime | None = None,
    unit_of_work_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Count issued versions and their hours by verification state, from metadata only."""
    latitude, longitude = _validate(latitude, longitude)
    evaluated_at = _instant(now if now is not None else datetime.now(UTC))
    configuration_digest = compute_configuration_digest(load_observation_configuration())
    factory = unit_of_work_factory or _configured_factory
    with factory() as uow:
        records = uow.issued_forecasts.list_for_coordinate(latitude, longitude, limit=None)
        facts = uow.artifacts.find_issued_temperature_verifications(
            latitude=latitude, longitude=longitude
        )
        sources = uow.artifacts.find_real_metar_sources(
            latitude=latitude, longitude=longitude, configuration_digest=configuration_digest
        )
        discoveries = uow.artifacts.find_station_discovery_sources(
            latitude=latitude, longitude=longitude, policy_version=POLICY_VERSION
        )

    index, skipped_facts = _fact_index(facts)
    windows = _coverage(sources)
    totals: Counter[str] = Counter()
    buckets: Counter[str] = Counter(dict.fromkeys(LEAD_BUCKETS, 0))
    matched_keys: set[tuple[str, datetime]] = set()
    hours_with_multiple_facts = 0
    per_issuance = []
    for record in sorted(records, key=lambda r: (r.issued_at, str(r.issued_forecast_id))):
        counts: Counter[str] = Counter(dict.fromkeys(HOUR_STATES, 0))
        target = _instant(record.target_reference_time)
        for horizon in range(1, record.forecast_horizon_hours + 1):
            valid = target + timedelta(hours=horizon)
            key = (str(record.issued_forecast_id), valid)
            matches = index.get(key, [])
            if matches:
                state = "verified"
                matched_keys.add(key)
                buckets[analytical_lead_bucket(horizon)] += 1
                if len(matches) > 1:
                    hours_with_multiple_facts += 1
            elif valid + ELIGIBILITY_MARGIN > evaluated_at:
                state = "pending"
            elif any(
                start <= valid - ELIGIBILITY_MARGIN and end >= valid + ELIGIBILITY_MARGIN
                for start, end in windows
            ):
                state = "retained_observations_without_fact"
            else:
                state = "no_retained_observations"
            counts[state] += 1
        totals.update(counts)
        per_issuance.append(
            {
                "issued_forecast_id": str(record.issued_forecast_id),
                "issued_at": _iso(record.issued_at),
                "target_reference_time": _iso(target),
                "hours": {state: counts[state] for state in HOUR_STATES},
            }
        )

    targets = Counter(_iso(record.target_reference_time) for record in records)
    issued_ats = sorted(_instant(record.issued_at) for record in records)
    target_times = sorted(_instant(record.target_reference_time) for record in records)
    registered = sorted(
        manifest.registered_at for matches in index.values() for manifest in matches
    )
    total_hours = sum(totals.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "status_policy": STATUS_POLICY,
        "coordinate": {"latitude": latitude, "longitude": longitude},
        "evaluated_at": _iso(evaluated_at),
        "issuances": {
            "count": len(records),
            "earliest_issued_at": _iso(issued_ats[0]) if issued_ats else None,
            "latest_issued_at": _iso(issued_ats[-1]) if issued_ats else None,
            "earliest_target_reference_time": _iso(target_times[0]) if target_times else None,
            "latest_target_reference_time": _iso(target_times[-1]) if target_times else None,
            "distinct_target_reference_times": len(targets),
            "targets_with_multiple_versions": sum(count > 1 for count in targets.values()),
        },
        "hours": {
            "total": total_hours,
            "eligible": total_hours - totals["pending"],
            **{state: totals[state] for state in HOUR_STATES},
        },
        "verified_by_lead_bucket": {
            bucket: buckets[bucket]
            for bucket in analytical_lead_buckets(
                lead for record in records for lead in range(1, record.forecast_horizon_hours + 1)
            )
        },
        "verification_facts": {
            "count": sum(len(matches) for matches in index.values()),
            "earliest_registered_at": _iso(registered[0]) if registered else None,
            "latest_registered_at": _iso(registered[-1]) if registered else None,
            "hours_with_multiple_facts": hours_with_multiple_facts,
            "facts_without_an_enumerated_hour": sum(
                len(matches) for key, matches in index.items() if key not in matched_keys
            ),
            "facts_without_usable_attributes": skipped_facts,
        },
        "observation_coverage": {
            "configuration_digest": str(configuration_digest),
            "retained_acquisitions": len(windows),
            "earliest_query_window_start": _iso(min(w[0] for w in windows)) if windows else None,
            "latest_query_window_end": _iso(max(w[1] for w in windows)) if windows else None,
        },
        "station_evidence": _station_evidence(sources, discoveries),
        "per_issuance": per_issuance,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        payload = canonical_json_bytes(accumulation_status(args.lat, args.lon))
    except ValueError as exc:
        error = {"code": "invalid_coordinate", "message": str(exc)}
    except Exception:
        error = {
            "code": "accumulation_status_failed",
            "message": "Could not read the saved issuance and verification metadata.",
        }
    else:
        sys.stdout.buffer.write(payload + b"\n")
        return 0
    print(canonical_json_bytes({"error": error}).decode("utf-8"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
