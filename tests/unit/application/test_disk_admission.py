"""Operational disk reserves admit work explicitly and never trigger deletion."""

from __future__ import annotations

import pytest

from mesoforge.application.disk_admission import GIB, DiskPolicy


@pytest.mark.parametrize(
    ("free", "status", "admitted"),
    [
        (31 * GIB, "normal", True),
        (30 * GIB, "warning", True),
        (20 * GIB, "warning", True),
        (20 * GIB - 1, "refused", False),
        (0, "refused", False),
        (None, "unavailable", False),
    ],
)
def test_daily_guidance_reserves(free: int | None, status: str, admitted: bool) -> None:
    report = DiskPolicy().report(free)
    assert report["status"] == status
    assert report["heavy_work_admitted"] is admitted
    assert report["min_free_bytes"] == 20 * GIB
    assert report["warn_free_bytes"] == 30 * GIB
    assert report["cleanup_action"] == ("none" if status == "normal" else "operator_review_only")


def test_central_deployment_configuration() -> None:
    policy = DiskPolicy.from_environment(
        {"MESOFORGE_GUIDANCE_MIN_FREE_GB": "21.5", "MESOFORGE_GUIDANCE_WARN_FREE_GB": "35"}
    )
    assert policy.min_free_bytes == int(21.5 * GIB)
    assert policy.warn_free_bytes == 35 * GIB
    assert DiskPolicy.from_environment({}) == DiskPolicy()


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "unknown"])
def test_invalid_configuration_fails_closed(value: str) -> None:
    with pytest.raises(ValueError):
        DiskPolicy.from_environment({"MESOFORGE_GUIDANCE_MIN_FREE_GB": value})


def test_inverted_reserves_and_unknown_disk_are_not_admitted() -> None:
    with pytest.raises(ValueError, match="minimum <= warning"):
        DiskPolicy(30 * GIB, 20 * GIB)
    with pytest.raises(ValueError, match="Free disk"):
        DiskPolicy().report(-1)
