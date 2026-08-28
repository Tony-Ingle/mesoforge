# 0003: Configuration Snapshots

Status: Accepted

## Context

Deterministic reproducibility requires that "which configuration produced
this artifact" be an exact, immutable, and comparable fact — not a
mutable row that can silently drift, and not something sensitive to
incidental formatting (YAML comments, key order) that carries no semantic
meaning. At the same time, secrets and service locations must never be
baked into a scientific configuration snapshot, because snapshots are
retained indefinitely as provenance.

## Decision

Configuration flows through one strict pipeline:

1. **YAML source** — a required base file (`configs/base.yaml`) plus an
   optional named environment overlay (`configs/environments/<name>.yaml`),
   deep-merged (maps merge, lists replace wholesale), plus an explicit
   in-process override mapping restricted to exactly three operational
   paths: `artifact_store.endpoint_reference`, `artifact_store.bucket`,
   and `metadata_store.dsn_environment_variable`. Any attempt to override
   `grids`, `vertical_definitions`, `variables`, schema versions, IDs, or
   any other nested scientific field is rejected.
2. **Strict Pydantic model** (`MesoForgeConfiguration`,
   `extra="forbid"`, `frozen=True`, `strict=True`) — unknown keys,
   duplicate IDs, and embedded secrets/DSNs fail validation immediately.
3. **RFC 8785 / JCS canonical JSON** via the `jcs` library — not a
   hand-rolled canonicalizer — over the validated model's fields.
4. **SHA-256 snapshot digest** of the canonical JSON bytes, rendered as
   `configuration_digest` and reflected in the human-referenceable
   `ConfigurationSnapshotId` (`cfg_sha256_<64 hex>`).

Only the JCS bytes of the validated configuration participate in the
digest. YAML comments, whitespace, and mapping key order are therefore
irrelevant to identity; list order is semantically meaningful and does
affect identity, matching how `lead_times` and similar ordered sequences
are used elsewhere in the domain.

Grids are registered globally by `grid_id` with their own
`definition_digest`: the same ID with the same digest is idempotent: the
same ID with a different digest aborts the entire snapshot registration
before any row commits. `ConfigurationService.register` performs
`GridRepository.add_if_absent` for every referenced grid, then
`ConfigurationRepository.add_if_absent` for the snapshot, in one
transactional unit of work.

## Consequences

- Two engineers (or CI runs) who describe the same scientific
  configuration in differently formatted YAML produce byte-identical
  provenance, because the digest is computed after strict validation and
  canonicalization, not over the source file.
- Any semantic change — a different grid coordinate, a reordered lead
  time list, a new variable — produces a new configuration digest and
  therefore a distinct, auditable snapshot; old snapshots referenced by
  existing artifacts are never mutated.
- Secrets cannot leak into retained provenance, because the schema
  forbids credential-shaped fields and only reference names
  (environment variable names, bucket/endpoint identifiers) are
  representable in `ArtifactStoreSettings` / `MetadataStoreSettings`.
- Operational deployment concerns (which bucket, which DSN environment
  variable) can vary between environments without creating a new
  scientific configuration snapshot, because those three paths are the
  only sanctioned override surface.
- A grid ID collision with a changed definition is a hard registration
  failure, not a silent overwrite — grid revisions must mint a new
  versioned ID (e.g., `.v2`).

## Deferred decisions

- Long-term configuration source management (e.g., a config service or
  registry UI) beyond local YAML files.
- Per-environment secret injection mechanism beyond "environment
  variables outside the canonical scientific configuration."
