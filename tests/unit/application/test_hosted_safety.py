"""Hosted revision admission, credential redaction and corrupt health evidence."""

from datetime import UTC, datetime, timedelta

import pytest

from mesoforge.application import worker_status
from mesoforge.application.runtime_log import REDACTED, redact
from tests.unit.application import test_baseline_readiness as readiness_tests

pinned = readiness_tests.pinned


def test_hosted_revision_gate_preserves_explicit_historical_inspection(tmp_path, pinned):
    revision = "a" * 40
    assert readiness_tests.check(tmp_path)["ready"]
    unproven = readiness_tests.check(tmp_path, expected_code_revision=revision)
    assert not unproven["ready"]
    assert unproven["reasons"] == ["baseline_code_revision_unproven"]
    pinned["manifest"]["code_revision"] = "b" * 40
    mismatch = readiness_tests.check(tmp_path, expected_code_revision=revision)
    assert not mismatch["ready"]
    assert mismatch["reasons"] == ["baseline_code_revision_mismatch"]
    pinned["manifest"]["code_revision"] = revision
    matching = readiness_tests.check(tmp_path, expected_code_revision=revision)
    assert matching["ready"] and matching["code_revision_status"] == "matched"
    assert matching["baseline"]["code_revision"] == revision


def test_readiness_rejects_ambiguous_clock_and_corrupt_publication_time(tmp_path, pinned):
    with pytest.raises(ValueError, match="timezone-aware"):
        readiness_tests.check(tmp_path, reference_time=datetime(2026, 7, 1, 13))
    with pytest.raises(ValueError):
        readiness_tests.check(tmp_path, expected_code_revision="unknown")
    report = readiness_tests.baseline_readiness(
        tmp_path,
        readiness_tests.LOCATIONS,
        now=readiness_tests.NOW,
        pointer={**readiness_tests.POINTER, "published_at": "corrupted"},
    )
    assert not report["ready"]
    assert report["reasons"] == ["baseline_metadata_invalid: ValueError"]


def test_redaction_covers_short_worker_credentials_and_presigned_urls(monkeypatch):
    monkeypatch.setenv("MESOFORGE_PG_WORKER_PASSWORD", "s3cr!t")
    monkeypatch.setenv("MESOFORGE_S3_SECRET_KEY", "tiny")
    message = (
        "worker password s3cr!t, object credential tiny; "
        "https://bucket.test/object?X-Amz-Credential=abc%2Fscope"
        "&X-Amz-Signature=signature-value&X-Amz-Security-Token=session-value "
        "https://other.test/file?sig=azure-signature&api_key=another-key"
    )
    cleaned = redact({"reason": message, "nested": [message]})
    for secret in (
        "s3cr!t",
        "tiny",
        "abc%2Fscope",
        "signature-value",
        "session-value",
        "azure-signature",
        "another-key",
    ):
        assert secret not in str(cleaned)
    assert "https://bucket.test/object?" in cleaned["reason"]
    assert REDACTED in cleaned["reason"]
    assert cleaned["nested"] == [cleaned["reason"]]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"heartbeat_at": "2036-01-01T00:00:00Z"}, "heartbeat_in_future"),
        ({"poll_started_at": "corrupt"}, "poll_time_unreadable"),
        ({"poll_started_at": "2036-01-01T00:00:00Z"}, "poll_time_unreadable"),
        ({"next_poll_at": "corrupt"}, "next_poll_time_unreadable"),
        ({"in_flight": "corrupt"}, "phase_unreadable"),
        ({"in_flight": {"phase": []}}, "phase_unreadable"),
        (
            {"in_flight": {"phase": "build", "started_at": "2036-01-01T00:00:00Z"}},
            "phase_exceeded_bound",
        ),
    ],
)
def test_corrupt_heartbeat_cannot_claim_a_healthy_worker(tmp_path, changes, reason):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    directory = worker_status.status_directory(tmp_path)
    worker_status.write_json(directory / worker_status.GUIDANCE_STATUS, {"state": "busy"})
    worker_status.write_json(
        directory / worker_status.GUIDANCE_HEARTBEAT,
        {"heartbeat_at": worker_status.iso(now - timedelta(seconds=1)), **changes},
    )
    report = worker_status.guidance_health(tmp_path, now=now)
    assert not report["healthy"] and report["reason"] == reason


@pytest.mark.parametrize("state", [[], {}, None, 42])
def test_corrupt_worker_state_is_explicitly_unhealthy(tmp_path, state):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    directory = worker_status.status_directory(tmp_path)
    worker_status.write_json(directory / worker_status.GUIDANCE_STATUS, {"state": state})
    worker_status.write_json(
        directory / worker_status.GUIDANCE_HEARTBEAT,
        {"heartbeat_at": worker_status.iso(now)},
    )
    report = worker_status.guidance_health(tmp_path, now=now)
    assert not report["healthy"] and report["reason"] == "status_unreadable"
