"""Phase 2 application orchestration contracts (plan Task 13)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mesoforge.application.phase2 import Phase2Request
from mesoforge.common.identifiers import ConfigurationSnapshotId, Digest, RunId

_DIGEST = Digest("sha256:" + "1" * 64)


def request(**changes: object) -> Phase2Request:
    values: dict[str, object] = {
        "run_id": RunId("run_00000000-0000-0000-0000-000000000001"),
        "configuration_snapshot_id": ConfigurationSnapshotId("cfg_sha256_" + "1" * 64),
        "configuration_digest": _DIGEST,
        "code_revision": "a" * 40,
        "environment_digest": _DIGEST,
        "lockfile_digest": _DIGEST,
        "target_reference_time": datetime(2026, 8, 31, 12, tzinfo=UTC),
        "forecast_issue_time": datetime(2026, 8, 31, 12, 30, tzinfo=UTC),
        "information_cutoff": datetime(2026, 8, 31, 12, 20, tzinfo=UTC),
        "verification_cutoff": datetime(2026, 9, 2, 2, tzinfo=UTC),
        "target_horizons": tuple(range(1, 37)),
    }
    values.update(changes)
    return Phase2Request.model_validate(values, strict=True)


def test_request_is_strict_frozen_and_exact() -> None:
    value = request()
    with pytest.raises(ValidationError):
        value.random_seed = 4  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Phase2Request.model_validate({**value.model_dump(), "extra": True}, strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"target_reference_time": datetime(2026, 8, 31, 12, 1, tzinfo=UTC)},
        {"target_horizons": tuple(range(36))},
        {"forecast_issue_time": datetime(2026, 8, 31, 11, tzinfo=UTC)},
        {"information_cutoff": datetime(2026, 8, 31, 13, tzinfo=UTC)},
        {"verification_cutoff": datetime(2026, 8, 31, 12, 29, tzinfo=UTC)},
    ],
)
def test_request_rejects_invalid_frame(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


def test_request_requires_aware_utc_instants() -> None:
    with pytest.raises(ValidationError):
        request(target_reference_time=datetime(2026, 8, 31, 12))
