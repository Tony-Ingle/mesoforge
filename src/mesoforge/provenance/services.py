"""Idempotency digest computation for derived-artifact transformations
(plan Section 4.8, Section 4.9's IdempotencyLock consumer).

The idempotency digest is a SHA-256 over activity type/version, ordered
role+artifact inputs, configuration digest, parameter digest, code
revision, environment digest, and output schema -- never over output
bytes. Deterministic input ordering is load-bearing: two logically
identical requests must always hash to the same digest.
"""

from __future__ import annotations

import jcs

from mesoforge.common.identifiers import Digest


def compute_idempotency_digest(
    *,
    activity_type: str,
    activity_version: str,
    ordered_inputs: tuple[tuple[str, str], ...],  # (role, artifact_id) pairs, in order
    configuration_digest: str,
    parameters_digest: str,
    code_revision: str,
    environment_digest: str,
    output_schema: str,
) -> Digest:
    payload = {
        "activity_type": activity_type,
        "activity_version": activity_version,
        "inputs": [
            {"role": role, "artifact_id": artifact_id} for role, artifact_id in ordered_inputs
        ],
        "configuration_digest": configuration_digest,
        "parameters_digest": parameters_digest,
        "code_revision": code_revision,
        "environment_digest": environment_digest,
        "output_schema": output_schema,
    }
    canonical_bytes = jcs.canonicalize(payload)
    return Digest.of_bytes(canonical_bytes)
