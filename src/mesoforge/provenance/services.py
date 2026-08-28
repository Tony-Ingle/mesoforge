"""Idempotency digest computation for derived-artifact transformations
(plan Section 4.8, Section 4.9's IdempotencyLock consumer).

The idempotency digest is a SHA-256 over activity type/version, ordered
role+artifact inputs, configuration digest, parameter digest, code
revision, environment digest, and output schema -- never over output
bytes. Deterministic input ordering is load-bearing: two logically
identical requests must always hash to the same digest.

``compute_idempotency_digest`` is a direct public function boundary (not
mediated by a Pydantic request model), so it must itself reconstruct/
validate every ID/digest/revision argument rather than trust its static
annotations -- a runtime caller that bypasses static type checking (e.g.
calling from untyped code, or passing a plain ``str``) must fail closed
with ``InvalidIdentifier`` before canonicalization, matching every other
public provenance/storage boundary (Codex re-review HIGH finding: the
prior version declared ``tuple[tuple[str, str], ...]``/``str`` for
``ordered_inputs``/the digest and revision arguments and performed no
boundary reconstruction at all).
"""

from __future__ import annotations

import jcs

from mesoforge.common.identifiers import ArtifactId, Digest, validate_code_revision


def compute_idempotency_digest(
    *,
    activity_type: str,
    activity_version: str,
    ordered_inputs: tuple[tuple[str, ArtifactId], ...],  # (role, artifact_id) pairs, in order
    configuration_digest: Digest,
    parameters_digest: Digest,
    code_revision: str,
    environment_digest: Digest,
    output_schema: str,
) -> Digest:
    validated_inputs = tuple(
        (role, ArtifactId(artifact_id)) for role, artifact_id in ordered_inputs
    )
    configuration_digest = Digest(configuration_digest)
    parameters_digest = Digest(parameters_digest)
    code_revision = validate_code_revision(code_revision)
    environment_digest = Digest(environment_digest)

    payload = {
        "activity_type": activity_type,
        "activity_version": activity_version,
        "inputs": [
            {"role": role, "artifact_id": str(artifact_id)}
            for role, artifact_id in validated_inputs
        ],
        "configuration_digest": str(configuration_digest),
        "parameters_digest": str(parameters_digest),
        "code_revision": code_revision,
        "environment_digest": str(environment_digest),
        "output_schema": output_schema,
    }
    canonical_bytes = jcs.canonicalize(payload)
    return Digest.of_bytes(canonical_bytes)
