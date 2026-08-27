"""Unit tests for mesoforge.provenance.services.compute_idempotency_digest
(HIGH finding: the public digest boundary must reconstruct/validate
every ID/digest/revision argument at runtime, not merely rely on its
static annotation, so a caller bypassing static type checking fails
closed).
"""

from __future__ import annotations

import pytest

from mesoforge.common.errors import InvalidIdentifier
from mesoforge.provenance.services import compute_idempotency_digest

_VALID_ARTIFACT_ID = "art_00000000-0000-0000-0000-000000000001"
_VALID_DIGEST = "sha256:" + "a" * 64
_VALID_CODE_REVISION = "b" * 40


def _kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        activity_type="unit-conversion",
        activity_version="1.0.0",
        ordered_inputs=(("primary", _VALID_ARTIFACT_ID),),
        configuration_digest=_VALID_DIGEST,
        parameters_digest=_VALID_DIGEST,
        code_revision=_VALID_CODE_REVISION,
        environment_digest=_VALID_DIGEST,
        output_schema="synthetic-derived.v1",
    )
    kwargs.update(overrides)
    return kwargs


class TestComputeIdempotencyDigestValidRoundTrip:
    def test_valid_call_returns_a_deterministic_sha256_digest(self) -> None:
        digest_one = compute_idempotency_digest(**_kwargs())
        digest_two = compute_idempotency_digest(**_kwargs())
        assert digest_one == digest_two
        assert digest_one.startswith("sha256:")

    def test_reordering_inputs_changes_the_digest(self) -> None:
        first = compute_idempotency_digest(
            **_kwargs(
                ordered_inputs=(
                    ("primary", "art_00000000-0000-0000-0000-000000000001"),
                    ("secondary", "art_00000000-0000-0000-0000-000000000002"),
                )
            )
        )
        second = compute_idempotency_digest(
            **_kwargs(
                ordered_inputs=(
                    ("secondary", "art_00000000-0000-0000-0000-000000000002"),
                    ("primary", "art_00000000-0000-0000-0000-000000000001"),
                )
            )
        )
        assert first != second


class TestComputeIdempotencyDigestRejectsMalformedRuntimeValues:
    """Direct probes reproducing the Codex-review finding: a runtime
    caller bypassing static type checking must fail closed with
    ``InvalidIdentifier``, not silently succeed."""

    def test_rejects_malformed_artifact_id_in_ordered_inputs(self) -> None:
        with pytest.raises(InvalidIdentifier):
            compute_idempotency_digest(
                **_kwargs(ordered_inputs=(("primary", "not-an-artifact-id"),))
            )

    def test_rejects_malformed_configuration_digest(self) -> None:
        with pytest.raises(InvalidIdentifier):
            compute_idempotency_digest(**_kwargs(configuration_digest="not-a-digest"))

    def test_rejects_malformed_parameters_digest(self) -> None:
        with pytest.raises(InvalidIdentifier):
            compute_idempotency_digest(**_kwargs(parameters_digest="not-a-digest"))

    def test_rejects_malformed_environment_digest(self) -> None:
        with pytest.raises(InvalidIdentifier):
            compute_idempotency_digest(**_kwargs(environment_digest="not-a-digest"))

    def test_rejects_malformed_code_revision(self) -> None:
        with pytest.raises(InvalidIdentifier):
            compute_idempotency_digest(**_kwargs(code_revision="bad-revision"))

    def test_reproduction_all_malformed_at_once_fails_closed(self) -> None:
        """Exact reproduction from the Codex review: artifact ID
        ``not-an-artifact-id``, digest strings ``not-a-digest``, and
        revision ``bad-revision`` must now be rejected, not silently
        hashed into a SHA-256 digest."""
        with pytest.raises(InvalidIdentifier):
            compute_idempotency_digest(
                activity_type="unit-conversion",
                activity_version="1.0.0",
                ordered_inputs=(("primary", "not-an-artifact-id"),),
                configuration_digest="not-a-digest",
                parameters_digest="not-a-digest",
                code_revision="bad-revision",
                environment_digest="not-a-digest",
                output_schema="synthetic-derived.v1",
            )
