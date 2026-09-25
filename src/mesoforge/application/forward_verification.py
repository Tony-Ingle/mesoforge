"""Derive bounded previous-hour verification work for one forward-run coordinate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from mesoforge.application.automatic_verification import derive_request, run_window
from mesoforge.application.issuance import select_issued_forecast_hours
from mesoforge.application.issued_temperature_verification import VERIFICATION_SCHEMA_VERSION
from mesoforge.storage.postgres.database import resolve_database_dsn
from mesoforge.storage.postgres.repositories import PostgresUnitOfWork


def _key(row: dict[str, Any]) -> tuple[str, datetime]:
    return row["issued_forecast_id"], datetime.fromisoformat(row["valid_time"])


def saved_fact_index(latitude: float, longitude: float) -> dict[tuple[str, datetime], str]:
    """Verified facts already saved for this coordinate, from fact attributes alone.

    No fact payload or forecast is read. Facts saved before attributes existed are
    not indexed; they are still found idempotently by the existing verifier.
    """
    dsn = resolve_database_dsn("MESOFORGE_DATABASE_DSN")
    with PostgresUnitOfWork(dsn) as uow:
        facts = uow.artifacts.find_issued_temperature_verifications(
            latitude=latitude, longitude=longitude
        )
    index: dict[tuple[str, datetime], str] = {}
    for manifest in facts:
        attributes = manifest.attributes or {}
        if (
            manifest.artifact_schema_version != VERIFICATION_SCHEMA_VERSION
            or manifest.quality_state == "invalid"
            or attributes.get("verification_status") != "verified"
        ):
            continue
        valid_time = datetime.fromisoformat(str(attributes["valid_time"])).astimezone(UTC)
        index.setdefault(
            (str(attributes["issued_forecast_id"]), valid_time), str(manifest.artifact_id)
        )
    return index


def verify_previous(
    latitude: float, longitude: float, *, now: datetime | None = None
) -> dict[str, Any]:
    """Reuse automatic verification in bounded windows without a user-supplied lookback.

    The existing selector retains every issued version. Its window filters saved
    payloads, so this lower bound does not become a provider acquisition request.
    Hours that already hold a saved fact are reported without any observation work,
    so a later, wider acquisition cannot add a second fact for the same hour. Only
    eligible unverified valid times enter the existing METAR acquisition path.
    """
    evaluation = datetime.now(UTC) if now is None else now
    if evaluation.tzinfo is None or evaluation.utcoffset() is None:
        raise ValueError("The evaluation time must include a timezone")
    evaluation = evaluation.astimezone(UTC)
    selected = select_issued_forecast_hours(
        latitude=latitude,
        longitude=longitude,
        start_valid_time=datetime.min.replace(tzinfo=UTC),
        end_valid_time=evaluation,
    )
    saved_facts: dict[str, Any] = {"status": "read", "hours_already_verified": 0}
    try:
        existing = saved_fact_index(latitude, longitude)
    except Exception:
        # Without the index the existing verifier still reuses identical inputs.
        existing = {}
        saved_facts = {
            "status": "unavailable",
            "hours_already_verified": 0,
            "note": "Saved facts could not be indexed; hours re-enter idempotent verification.",
        }
    preflight: list[dict[str, Any]] = []
    ready: set[datetime] = set()
    for selected_hour in selected["results"]:
        # A single-hour preflight uses the existing eligibility/margin rules
        # without invoking its six-hour maximum before partitioning the work.
        request = derive_request({"results": [selected_hour]}, now=evaluation)
        for row in request["hours"]:
            fact_id = existing.get(_key(row))
            if fact_id is not None:
                row.update(
                    status="already_existing",
                    verification_id=fact_id,
                    reasons=[],
                    source="saved_fact_attributes",
                )
                saved_facts["hours_already_verified"] += 1
            elif row["status"] == "ready":
                ready.add(datetime.fromisoformat(row["valid_time"]).astimezone(UTC))
        preflight.extend(request["hours"])
    groups: list[list[datetime]] = []
    for valid_time in sorted(ready):
        if not groups or valid_time - groups[-1][0] > timedelta(hours=6):
            groups.append([])
        groups[-1].append(valid_time)

    outcomes = {_key(row): dict(row) for row in preflight}
    windows = []
    downloaded_bytes = 0
    for group in groups:
        start, end = group[0], group[-1] + timedelta(microseconds=1)
        window: dict[str, Any] = {
            "start_valid_time": start.isoformat(),
            "end_valid_time": end.isoformat(),
        }
        pending = [
            row
            for row in outcomes.values()
            if row["status"] == "ready" and start <= _key(row)[1] < end
        ]
        try:
            result = run_window(
                latitude=latitude,
                longitude=longitude,
                start_valid_time=start,
                end_valid_time=end,
            )
        except Exception:
            window["error"] = {
                "code": "verification_failed",
                "message": "Could not complete automatic verification for this saved-hour window.",
            }
            for row in pending:
                row.update(status="error", reasons=[window["error"]["message"]])
        else:
            window["result"] = result
            downloaded_bytes += result["downloaded_bytes"]
            verification = result.get("verification")
            if verification is not None:
                for row in verification["results"]:
                    # Hours answered from saved facts keep that answer; the window
                    # verifier may still report them when it reuses retained inputs.
                    if _key(row) in outcomes and outcomes[_key(row)]["status"] == "ready":
                        outcomes[_key(row)].update(row)
            else:
                for row in pending:
                    row.update(
                        status="unavailable",
                        reasons=[result.get("reason") or "Verification is unavailable."],
                    )
        windows.append(window)

    summary = dict.fromkeys(
        ("verified", "unavailable", "ineligible", "already_existing", "errors", "deferred"), 0
    )
    for row in outcomes.values():
        if row["status"] == "ready":
            row.update(status="error", reasons=["Verification did not return this saved hour."])
        category = "errors" if row["status"] == "error" else row["status"]
        summary[category] += 1
    worked = bool(windows) or saved_facts["hours_already_verified"] > 0
    return {
        "latitude": latitude,
        "longitude": longitude,
        "evaluated_at": evaluation.isoformat(),
        "status": (
            "partial" if summary["errors"] else "completed" if worked else "nothing_to_verify"
        ),
        "reason": None if worked else "No saved forecast hours are ready for observation matching.",
        "summary": summary,
        "downloaded_bytes": downloaded_bytes,
        "saved_facts": saved_facts,
        "preflight": {
            "hours": preflight,
            "ready_valid_times": [value.isoformat() for value in sorted(ready)],
        },
        "windows": windows,
        "results": list(outcomes.values()),
    }


def verify_previous_fields(
    latitude: float,
    longitude: float,
    *,
    now: datetime,
    qpf_lookback_hours: int = 72,
    qpf_max_opportunities: int = 36,
) -> dict[str, Any]:
    """Independent prior-field attempts; neither failure gates a new issuance.

    Temperature keeps its existing coordinator and scientific behavior. QPF has
    an explicitly bounded operational lookback; older work uses bounded backfill.
    """
    from mesoforge.application.automatic_qpf_verification import verify_previous_qpf

    results = {}
    operations: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("temperature", lambda: verify_previous(latitude, longitude, now=now)),
        (
            "qpf",
            lambda: verify_previous_qpf(
                latitude,
                longitude,
                now=now,
                lookback_hours=qpf_lookback_hours,
                max_opportunities=qpf_max_opportunities,
            ),
        ),
    )
    for field, operation in operations:
        try:
            results[field] = operation()
        except Exception as exc:
            results[field] = {
                "status": "error",
                "retryable": True,
                "reason": f"{type(exc).__name__}: {exc}",
            }
    return results
