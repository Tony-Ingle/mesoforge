"""Offline deployment contract: dormant, DST-aware, serial daily invocation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "mesoforge-daily.yml"


def workflow() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    return loaded


def test_single_named_timezone_schedule_preserves_cst_and_cdt_wall_clock() -> None:
    events = workflow()["on"]
    assert set(events) == {"schedule", "workflow_dispatch"}
    assert events["schedule"] == [{"cron": "5 6 * * *", "timezone": "America/Chicago"}]
    zone = ZoneInfo(events["schedule"][0]["timezone"])
    minute, hour, day, month, weekday = events["schedule"][0]["cron"].split()
    assert (day, month, weekday) == ("*", "*", "*")
    for date, expected_utc_hour in (("2026-01-15", 12), ("2026-07-15", 11)):
        local = datetime.fromisoformat(date).replace(
            hour=int(hour), minute=int(minute), tzinfo=zone
        )
        assert local.astimezone(UTC).hour == expected_utc_hour
        assert local.astimezone(UTC).minute == 5


def test_schedule_is_opt_in_and_manual_status_is_default_branch_only() -> None:
    document = workflow()
    assert set(document["jobs"]) == {"daily"}
    guard = document["jobs"]["daily"]["if"]
    assert "github.repository == 'Tony-Ingle/mesoforge'" in guard
    assert "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in guard
    assert (
        "((github.event_name == 'workflow_dispatch' && inputs.operation == 'status') || "
        "vars.MESOFORGE_DAILY_ENABLED == 'true')" in guard
    )
    operation = document["on"]["workflow_dispatch"]["inputs"]["operation"]
    assert operation["type"] == "choice"
    assert operation["default"] == "status"
    assert operation["options"] == ["status", "run"]
    assert "MESOFORGE_DAILY_ENABLED" not in document.get("env", {})


def test_workflow_serializes_and_delegates_to_one_host_boundary_without_forecast_logic() -> None:
    document = workflow()
    assert document["concurrency"] == {
        "group": "mesoforge-daily-minneapolis",
        "cancel-in-progress": False,
    }
    job = document["jobs"]["daily"]
    assert job["runs-on"] == ["self-hosted", "Linux", "X64"]
    assert job["timeout-minutes"] == 240
    steps = job["steps"]
    assert '"$ACTUAL_RUNNER" != "vps"' in steps[0]["run"]
    command = steps[2]["run"]
    assert command.count("python3 ") == 1
    assert (
        'python3 deploy/hosted/daily_cycle.py --config /etc/mesoforge/daily.json "$OPERATION"'
        in command
    )
    text = WORKFLOW.read_text("utf-8")
    for forbidden in (
        "docker ",
        "systemctl ",
        "forecast_worker",
        "guidance_worker",
        "OPENAI_API_KEY",
        "MESOFORGE_SMTP_PASSWORD",
        "ingle.j.anthony@gmail.com",
        "Grasston",
        "Surley",
    ):
        assert forbidden not in text
    assert steps[-1]["if"] == "${{ always() && steps.checkout.outcome == 'success' }}"
    assert '>> "$GITHUB_STEP_SUMMARY"' in steps[-1]["run"]


def test_checkout_is_pinned_read_only_and_preserves_existing_runner_workspace() -> None:
    document = workflow()
    assert document["permissions"] == {"contents": "read"}
    checkout = document["jobs"]["daily"]["steps"][1]
    assert checkout["uses"] == "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
    assert checkout["with"] == {"persist-credentials": False, "clean": False}
