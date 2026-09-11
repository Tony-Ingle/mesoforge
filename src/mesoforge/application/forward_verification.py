"""Derive bounded previous-hour verification work for one forward-run coordinate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mesoforge.application.automatic_verification import derive_request, run_window
from mesoforge.application.issuance import select_issued_forecast_hours


def _key(row: dict[str, Any]) -> tuple[str, datetime]:
    return row["issued_forecast_id"], datetime.fromisoformat(row["valid_time"])


def verify_previous(
    latitude: float, longitude: float, *, now: datetime | None = None
) -> dict[str, Any]:
    """Reuse automatic verification in bounded windows without a user-supplied lookback.

    The existing selector retains every issued version. Its window filters saved
    payloads, so this lower bound does not become a provider acquisition request.
    Only eligible actual valid times enter the existing METAR acquisition path.
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
    preflight: list[dict[str, Any]] = []
    ready: set[datetime] = set()
    for selected_hour in selected["results"]:
        # A single-hour preflight uses the existing eligibility/margin rules
        # without invoking its six-hour maximum before partitioning the work.
        request = derive_request({"results": [selected_hour]}, now=evaluation)
        preflight.extend(request["hours"])
        ready.update(datetime.fromisoformat(value) for value in request["ready_valid_times"])
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
                    if _key(row) in outcomes:
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
    return {
        "latitude": latitude,
        "longitude": longitude,
        "evaluated_at": evaluation.isoformat(),
        "status": (
            "partial" if summary["errors"] else "completed" if windows else "nothing_to_verify"
        ),
        "reason": None
        if windows
        else "No saved forecast hours are ready for observation matching.",
        "summary": summary,
        "downloaded_bytes": downloaded_bytes,
        "preflight": {
            "hours": preflight,
            "ready_valid_times": [value.isoformat() for value in sorted(ready)],
        },
        "windows": windows,
        "results": list(outcomes.values()),
    }
