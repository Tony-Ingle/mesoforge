"""Deployment readiness applies the same checks issuance applies, from one pointer read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mesoforge.application import baseline_readiness as module
from mesoforge.application.baseline_readiness import baseline_readiness
from mesoforge.application.prepared_snapshot import SnapshotError
from mesoforge.common.horizon import FIVE_DAY_HORIZON
from tests.unit.application import test_baseline_snapshot as baseline_tests
from tests.unit.application.test_batch_forecast import FIRST, LAST
from tests.unit.application.test_prepared_temperature import TARGET

baseline_case = baseline_tests.baseline_case
NOW = datetime(2026, 7, 1, 13, 10, tzinfo=UTC)
REFERENCE = "2026-07-01T13:00:00Z"
LOCATIONS = [{"id": "a", "lat": 45.0, "lon": -93.0}, {"id": "b", "lat": 45.1, "lon": -93.1}]
POINTER = {"baseline_snapshot_id": "b1", "published_at": "2026-07-01T12:40:00+00:00"}


def manifest(**overrides: Any) -> dict[str, Any]:
    value = {
        "baseline_snapshot_id": "b1",
        "analysis_cutoff": "2026-07-01T12:30:00Z",
        "built_at": "2026-07-01T12:30:00Z",
        "completed_at": "2026-07-01T12:39:00Z",
        "information_cutoff": {"status": "proven"},
        "blend_governance": {"status": "resolved", "heads": {}, "policies": {}},
        "prepared_snapshot": {
            "snapshot_id": "p1",
            "contributor_cycles": {"HRRR": "c"},
            "coverage": {"reference_time": "2026-07-01T12:00:00Z"},
        },
        "coverage": {
            "reference_times": ["2026-07-01T12:00:00Z", REFERENCE],
            "failed_locations": [],
        },
        "domains": [
            {"latitude": row["lat"], "longitude": row["lon"], "reference_time": REFERENCE}
            for row in LOCATIONS
        ],
    }
    value.update(overrides)
    return value


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"manifest": manifest(), "error": None, "calls": []}

    def load(root: Path, *, pointer: dict) -> Any:
        state["calls"].append(pointer)
        if state["error"] is not None:
            raise state["error"]
        return SimpleNamespace(manifest=state["manifest"], pointer=pointer)

    monkeypatch.setattr(module, "load_baseline", load)
    return state


def check(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    return baseline_readiness(tmp_path, LOCATIONS, now=NOW, pointer=dict(POINTER), **kwargs)


def test_ready_baseline_reports_operator_identity(tmp_path: Path, pinned: dict) -> None:
    report = check(tmp_path)
    assert report["ready"] is True and report["reasons"] == []
    facts = report["baseline"]
    assert facts["baseline_snapshot_id"] == "b1"
    assert facts["forecast_horizon_hours"] == 36
    assert facts["contributor_state_id"] == "p1"
    assert facts["published_at"] == "2026-07-01T12:40:00Z"
    assert facts["information_cutoff"] == "2026-07-01T12:30:00Z"
    assert facts["age_seconds"] == 1800.0
    assert facts["reference_times"] == {"first": "2026-07-01T12:00:00Z", "last": REFERENCE}
    assert report["blend_revocation"] == "not_checked"
    assert pinned["calls"] == [POINTER]


def test_readiness_reports_pinned_long_horizon_without_rebuilding(tmp_path, pinned):
    pinned["manifest"]["forecast_horizon"] = FIVE_DAY_HORIZON.payload()
    report = check(tmp_path)
    assert report["ready"]
    assert report["baseline"]["forecast_horizon_hours"] == 120
    assert pinned["calls"] == [POINTER]


def test_missing_pointer_and_unverifiable_publication(tmp_path: Path, pinned: dict) -> None:
    empty = baseline_readiness(tmp_path, LOCATIONS, now=NOW)
    assert empty["reasons"] == ["no_published_baseline"]
    pinned["error"] = SnapshotError("Referenced prepared snapshot manifest changed")
    report = check(tmp_path)
    assert report["ready"] is False
    assert report["reasons"][0].startswith("baseline_unverifiable: SnapshotError")


def test_uncovered_reference_hour_is_not_ready(tmp_path: Path, pinned: dict) -> None:
    report = baseline_readiness(
        tmp_path, LOCATIONS, now=NOW + timedelta(hours=1), pointer=dict(POINTER)
    )
    assert "reference_hour_not_covered" in report["reasons"]
    assert "no_configured_location_covered" in report["reasons"]


def test_one_uncovered_location_is_a_row_failure_not_unreadiness(
    tmp_path: Path, pinned: dict
) -> None:
    pinned["manifest"] = manifest(domains=manifest()["domains"][:1])
    report = check(tmp_path)
    assert report["ready"] is True
    assert report["uncovered_locations"] == ["b"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"blend_governance": {"status": "not_configured"}}, "baseline_governance_unproven"),
        ({"blend_governance": None}, "baseline_governance_unproven"),
        ({"built_at": "2026-07-01T13:20:00Z"}, "baseline_built_at_after_request"),
        ({"analysis_cutoff": None}, "baseline_analysis_cutoff_after_request"),
    ],
)
def test_issuance_refusals_are_readiness_failures(
    tmp_path: Path, pinned: dict, overrides: dict, reason: str
) -> None:
    pinned["manifest"] = manifest(**overrides)
    assert reason in check(tmp_path)["reasons"]


def test_publication_after_the_request_is_not_ready(tmp_path: Path, pinned: dict) -> None:
    report = baseline_readiness(
        tmp_path,
        LOCATIONS,
        now=NOW,
        pointer={**POINTER, "published_at": "2026-07-01T13:30:00+00:00"},
    )
    assert "baseline_published_at_after_request" in report["reasons"]


def test_revoked_blend_and_unreadable_governance(tmp_path: Path, pinned: dict) -> None:
    revoked = check(tmp_path, governance=SimpleNamespace(blend_revoked=lambda blend: True))
    assert revoked["reasons"] == ["baseline_governance_revoked"]
    assert revoked["blend_revocation"] == "revoked"
    fine = check(tmp_path, governance=SimpleNamespace(blend_revoked=lambda blend: False))
    assert fine["ready"] and fine["blend_revocation"] == "not_revoked"

    def unavailable(blend: dict) -> bool:
        raise ConnectionError("governance database unreachable")

    report = check(tmp_path, governance=SimpleNamespace(blend_revoked=unavailable))
    assert report["reasons"][0].startswith("governance_unavailable")


def test_reference_must_be_an_hour_not_after_the_request(tmp_path: Path, pinned: dict) -> None:
    with pytest.raises(ValueError):
        check(tmp_path, reference_time=NOW)
    with pytest.raises(ValueError):
        check(tmp_path, reference_time=NOW.replace(minute=0) + timedelta(hours=1))
    with pytest.raises(ValueError):
        baseline_readiness(tmp_path, LOCATIONS, now=datetime(2026, 7, 1, 13))


def test_real_baseline_is_verified_and_its_reference_views_are_honoured(baseline_case) -> None:
    root = baseline_case["baseline"]
    now = datetime.now(UTC)
    report = baseline_readiness(root, [FIRST, LAST], now=now, reference_time=TARGET)
    # The fixture baseline is a development build without governance: issuance refuses it.
    assert report["reasons"] == ["baseline_governance_unproven"]
    assert (
        report["baseline"]["contributor_state_id"] == (baseline_case["prepared"][1]["snapshot_id"])
    )
    tampered = {**report["pointer"], "manifest_sha256": "0" * 64}
    broken = baseline_readiness(root, [FIRST], now=now, reference_time=TARGET, pointer=tampered)
    assert broken["reasons"][0].startswith("baseline_unverifiable")
    later = baseline_readiness(root, [FIRST], now=now, reference_time=TARGET + timedelta(hours=5))
    assert "reference_hour_not_covered" in later["reasons"]
