"""Deployment-level readiness of one published baseline, from existing contracts.

Readiness reuses the checks issuance applies, without guidance bytes or
provider access. It reads the pointer once and verifies that publication with
``load_baseline`` (manifest digest, completeness, coherence, prepared manifest digest,
information cutoff), then checks what ``forecast_from_baseline`` refuses: timestamps
after the request, unresolved or revoked blend governance and an uncovered reference
hour. There is no invented age threshold: staleness means the request's reference
hour is not covered. Age is reported for operators, not used as a gate. Hosted
callers additionally require a recorded baseline code revision matching their image;
historical/development readers may omit that admission requirement.

A configured coordinate without a saved domain does not make the baseline unready;
issuance reports that one row as ``coverage_required`` and delivers the others. At
least one valid configured coordinate must be covered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesoforge.application.baseline_snapshot import load_baseline, read_pointer
from mesoforge.application.batch_forecast import _coordinates
from mesoforge.application.prepared_snapshot import SnapshotError, derive_reference_time
from mesoforge.application.spatial_coverage import validate_coordinate
from mesoforge.common.identifiers import validate_code_revision


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Baseline timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _label(location: Any) -> str:
    if isinstance(location, dict):
        return str(location.get("id") or location.get("name") or location)
    return str(location)


def baseline_facts(
    pointer: dict[str, Any], manifest: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """The operator-facing identity of one publication (no validation)."""
    prepared = manifest.get("prepared_snapshot", {})
    views = manifest.get("coverage", {}).get("reference_times", [])
    published = _instant(pointer["published_at"])
    blend = manifest.get("blend_governance")
    return {
        "baseline_snapshot_id": manifest.get("baseline_snapshot_id"),
        "code_revision": manifest.get("code_revision"),
        "contributor_state_id": prepared.get("snapshot_id"),
        "published_at": _z(published),
        "built_at": manifest.get("built_at"),
        "completed_at": manifest.get("completed_at"),
        "information_cutoff": manifest.get("analysis_cutoff"),
        "information_cutoff_status": manifest.get("information_cutoff", {}).get("status"),
        "prepared_reference_time": prepared.get("coverage", {}).get("reference_time"),
        "reference_times": {"first": min(views, default=None), "last": max(views, default=None)},
        "contributor_cycles": prepared.get("contributor_cycles", {}),
        "nbm_product_cycles": prepared.get("nbm_product_cycles", {}),
        "blend_governance": blend.get("status") if isinstance(blend, dict) else "pre_governance",
        "governed_blend_policies": {
            target: f"{row.get('policy_id')}/{row.get('version')}"
            for target, row in (blend or {}).get("policies", {}).items()
        }
        if isinstance(blend, dict)
        else {},
        "failed_locations": len(manifest.get("coverage", {}).get("failed_locations", [])),
        "age_seconds": (now - published).total_seconds(),
    }


def mark_governance_unavailable(report: dict[str, Any], error: BaseException) -> None:
    """Governance that cannot be read makes a baseline unready, exactly as for issuance."""
    report["blend_revocation"] = "unavailable"
    report["reasons"].append(f"governance_unavailable: {type(error).__name__}: {error}")
    report["ready"] = False


def baseline_readiness(
    baseline_root: Path,
    locations: list[Any],
    *,
    now: datetime,
    reference_time: datetime | None = None,
    pointer: dict[str, Any] | None = None,
    governance: Any | None = None,
    expected_code_revision: str | None = None,
) -> dict[str, Any]:
    """Whether one publication can serve configured issuance at ``now``.

    ``pointer`` pins the exact publication a caller will issue from; without it the
    current pointer is read once. ``governance`` (a ``GovernanceService``) adds the
    rollback check issuance applies; without it the result says it was not checked.
    Hosted callers supply their image revision. Omitting it permits explicit
    historical/development inspection without rewriting older manifests.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Readiness time must be timezone-aware")
    now = now.astimezone(UTC)
    if reference_time is not None and (
        reference_time.tzinfo is None or reference_time.utcoffset() is None
    ):
        raise ValueError("Reference time must be timezone-aware")
    if expected_code_revision is not None:
        validate_code_revision(expected_code_revision)
    derived = derive_reference_time(now)
    reference = derived if reference_time is None else reference_time.astimezone(UTC)
    if reference.minute or reference.second or reference.microsecond or reference > derived:
        raise ValueError("Reference time must be an exact UTC hour not after the request hour")
    report: dict[str, Any] = {
        "ready": False,
        "checked_at": _z(now),
        "reference_time": _z(reference),
        "pointer": None,
        "baseline": None,
        "uncovered_locations": [],
        "blend_revocation": "not_checked",
        "code_revision_status": "not_checked",
        "reasons": [],
    }
    reasons: list[str] = report["reasons"]
    try:
        pointer = read_pointer(baseline_root) if pointer is None else dict(pointer)
    except (SnapshotError, OSError, ValueError, KeyError) as exc:
        reasons.append(f"baseline_unreadable: {type(exc).__name__}: {exc}")
        return report
    if pointer is None:
        reasons.append("no_published_baseline")
        return report
    report["pointer"] = pointer
    try:
        pinned = load_baseline(baseline_root, pointer=pointer)
    except (SnapshotError, OSError, ValueError, KeyError) as exc:
        # A missing guidance mount, changed prepared manifest or unproven cutoff all
        # stop issuance the same way (``no_current_baseline``).
        reasons.append(f"baseline_unverifiable: {type(exc).__name__}: {exc}")
        return report
    manifest = pinned.manifest
    try:
        report["baseline"] = baseline_facts(pointer, manifest, now)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        reasons.append(f"baseline_metadata_invalid: {type(exc).__name__}")
        return report
    if expected_code_revision is not None:
        actual_revision = manifest.get("code_revision")
        if actual_revision is None:
            report["code_revision_status"] = "unproven"
            reasons.append("baseline_code_revision_unproven")
        elif actual_revision != expected_code_revision:
            report["code_revision_status"] = "mismatch"
            reasons.append("baseline_code_revision_mismatch")
        else:
            report["code_revision_status"] = "matched"
    for label, value in (
        ("analysis_cutoff", manifest.get("analysis_cutoff")),
        ("built_at", manifest.get("built_at")),
        ("completed_at", manifest.get("completed_at")),
        ("published_at", pointer.get("published_at")),
    ):
        if value is None or _instant(value) > now:
            reasons.append(f"baseline_{label}_after_request")
    blend = manifest.get("blend_governance")
    if not isinstance(blend, dict) or blend.get("status") != "resolved":
        reasons.append("baseline_governance_unproven")
    elif governance is not None:
        try:
            revoked = governance.blend_revoked(blend)
        except Exception as exc:
            report["blend_revocation"] = "unavailable"
            reasons.append(f"governance_unavailable: {type(exc).__name__}: {exc}")
        else:
            report["blend_revocation"] = "revoked" if revoked else "not_revoked"
            if revoked:
                reasons.append("baseline_governance_revoked")
    views = manifest.get("coverage", {}).get("reference_times", [])
    if not any(_instant(value) == reference for value in views):
        reasons.append("reference_hour_not_covered")
    covered = {
        (row["latitude"], row["longitude"])
        for row in manifest.get("domains", [])
        if _instant(row["reference_time"]) == reference
    }
    valid = 0
    for location in locations:
        try:
            coordinate = _coordinates(location)
            validate_coordinate(*coordinate)
        except ValueError:
            continue
        valid += 1
        if coordinate not in covered:
            report["uncovered_locations"].append(_label(location))
    if valid and len(report["uncovered_locations"]) == valid:
        reasons.append("no_configured_location_covered")
    if not valid:
        reasons.append("no_valid_configured_location")
    report["ready"] = not reasons
    return report
