"""Independent, dormant location workflows share one serial host boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
LOCATIONS = ("minneapolis", "grasston")


def workflow_path(location: str) -> Path:
    return WORKFLOWS / f"mesoforge-daily-{location}.yml"


def workflow(location: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(workflow_path(location).read_text("utf-8"))
    return loaded


@pytest.mark.parametrize(
    ("location", "cron", "winter_local", "summer_local"),
    [
        ("minneapolis", "15 12 * * *", "06:15", "07:15"),
        ("grasston", "15 11 * * *", "05:15", "06:15"),
    ],
)
def test_one_daily_location_schedule_is_a_fixed_utc_cron(
    location: str, cron: str, winter_local: str, summer_local: str
) -> None:
    # GitHub evaluates plain crons in UTC; the local wall clock shifts across CST/CDT.
    events = workflow(location)["on"]
    assert set(events) == {"schedule", "workflow_dispatch"}
    assert events["schedule"] == [{"cron": cron}]
    minute, hour, day, month, weekday = cron.split()
    assert (day, month, weekday) == ("*", "*", "*")
    zone = ZoneInfo("America/Chicago")
    for date, expected_local in (("2026-01-15", winter_local), ("2026-07-15", summer_local)):
        fired = datetime.fromisoformat(date).replace(hour=int(hour), minute=int(minute), tzinfo=UTC)
        assert fired.astimezone(zone).strftime("%H:%M") == expected_local


@pytest.mark.parametrize("location", LOCATIONS)
def test_each_schedule_is_independently_opt_in_and_default_branch_only(location: str) -> None:
    document = workflow(location)
    assert set(document["jobs"]) == {"daily"}
    guard = document["jobs"]["daily"]["if"]
    assert "github.repository == 'Tony-Ingle/mesoforge'" in guard
    assert "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in guard
    assert (
        "((github.event_name == 'workflow_dispatch' && inputs.operation == 'status') || "
        f"vars.MESOFORGE_DAILY_{location.upper()}_ENABLED == 'true')" in guard
    )
    operation = document["on"]["workflow_dispatch"]["inputs"]["operation"]
    assert operation["type"] == "choice"
    assert operation["default"] == "status"
    assert operation["options"] == ["status", "run"]
    assert not document.get("env")
    other = next(item for item in LOCATIONS if item != location)
    assert f"MESOFORGE_DAILY_{other.upper()}_ENABLED" not in guard
    assert "MESOFORGE_DAILY_ENABLED" not in guard


@pytest.mark.parametrize("location", LOCATIONS)
def test_workflow_serializes_and_delegates_explicit_location_to_host_boundary(
    location: str,
) -> None:
    document = workflow(location)
    assert document["concurrency"] == {
        "group": "mesoforge-daily-host",
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
        "python3 deploy/hosted/daily_cycle.py --config /etc/mesoforge/daily.json "
        f'--location {location} "$OPERATION"' in command
    )
    text = workflow_path(location).read_text("utf-8")
    for forbidden in (
        "docker ",
        "systemctl ",
        "forecast_worker",
        "guidance_worker",
        "OPENAI_API_KEY",
        "MESOFORGE_SMTP_PASSWORD",
        "ingle.j.anthony@gmail.com",
        "Surley",
        "matrix:",
        "for location",
    ):
        assert forbidden not in text
    assert steps[-1]["if"] == "${{ always() && steps.checkout.outcome == 'success' }}"
    assert '>> "$GITHUB_STEP_SUMMARY"' in steps[-1]["run"]
    assert f"--location {location} status" in steps[-1]["run"]


@pytest.mark.parametrize("location", LOCATIONS)
def test_checkout_is_pinned_read_only_and_preserves_existing_runner_workspace(
    location: str,
) -> None:
    document = workflow(location)
    assert document["permissions"] == {"contents": "read"}
    checkout = document["jobs"]["daily"]["steps"][1]
    assert checkout["uses"] == "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
    assert checkout["with"] == {"persist-credentials": False, "clean": False}


def test_independent_workflows_replace_old_workflow_and_share_host_lock() -> None:
    assert not (WORKFLOWS / "mesoforge-daily.yml").exists()
    documents = [workflow(location) for location in LOCATIONS]
    assert len({item["name"] for item in documents}) == 2
    assert len({item["on"]["schedule"][0]["cron"] for item in documents}) == 2
    assert {item["concurrency"]["group"] for item in documents} == {"mesoforge-daily-host"}
