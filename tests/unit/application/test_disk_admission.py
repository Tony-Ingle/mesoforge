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


def test_publication_guard_rechecks_current_capacity(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from mesoforge.application import disk_admission as disk

    monkeypatch.setenv("MESOFORGE_PROSPECTIVE_ROOT", str(tmp_path))
    monkeypatch.setenv("MESOFORGE_GUIDANCE_MIN_FREE_GB", "20")
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda _: SimpleNamespace(free=19 * GIB))
    with pytest.raises(OSError, match="before persistence"):
        disk.require_runtime_capacity()
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda _: SimpleNamespace(free=21 * GIB))
    disk.require_runtime_capacity()
    monkeypatch.delenv("MESOFORGE_PROSPECTIVE_ROOT")
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda _: pytest.fail("Replay does not probe disk")
    )
    disk.require_runtime_capacity()


def test_low_disk_never_writes_baseline_artifact(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from mesoforge.application import baseline_snapshot, disk_admission

    monkeypatch.setenv("MESOFORGE_PROSPECTIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(disk_admission.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="disk reserve"):
        baseline_snapshot.write_artifact(tmp_path, "never.json.gz", {"valid": True})
    assert not (tmp_path / "never.json.gz").exists()
