# 0002: Artifact Identity and Provenance

Status: Accepted

## Context

The reviewed v1 architecture requires that scientific outputs be
reproducible and auditable: every derived value must be traceable to the
exact inputs, code, configuration, and activity that produced it.
Content-based identity alone is insufficient, because identical bytes can
legitimately arise from different provenance (e.g., two independent
ingestions of the same source file), and a plain `parents` list on an
artifact cannot express which activity, version, or named role produced
an output from which inputs.

## Decision

Artifact and content identity are deliberately separate:

- **Record identity** (`ArtifactId`, `ActivityId`, `RunId`) is a
  server-generated `uuid.uuid4()`, rendered as a typed prefixed string
  (`art_`, `act_`, `run_`) at Python boundaries and stored as a
  PostgreSQL `uuid` column. Record identity says nothing about content.
- **Content identity** (`Digest`) is `sha256:<64 lowercase hex>` over the
  exact serialized bytes. Object storage keys are content-addressed:
  `objects/sha256/<first-2-hex>/<remaining-62-hex>`. Bytes are
  deduplicated; artifact records referencing those bytes are not — two
  artifact records may point at the same stored object with different
  provenance.

Provenance is expressed with first-class activity nodes, never a bare
parent-pointer list:

- An `ArtifactManifest` never carries a `parents` field.
- An `ActivityManifest` connects ordered, uniquely-named input and output
  artifact roles (`ActivityArtifactRef`), together with activity type,
  version, status, timestamps, configuration snapshot, code revision,
  environment digest, and (for derived artifacts) an idempotency digest.
- Source/root artifacts (external ingestion) carry an optional
  `source_registration_digest` and `source_identity` instead of a
  producing activity, because external bytes have no MesoForge activity
  that created them.
- Lineage is queried by walking activity input/output edges
  (`LineageReader.ancestors` / `.descendants`), producing a
  deterministic, canonically sorted `LineageGraph` for export.

Idempotency for derived (transformed) artifacts is a SHA-256 digest over
activity type/version, ordered role+artifact inputs, configuration
digest, parameter digest, code revision, environment digest, and output
schema — not a checksum of output bytes. Repeating an identical
transformation request returns the original succeeded activity and
output rather than creating a duplicate. A PostgreSQL session advisory
lock serializes concurrent identical requests before any row is
inserted; a partial unique index on `activities(idempotency_digest)
WHERE status='succeeded'` is a fail-closed backstop for any client that
bypasses the lock.

Cycles are rejected before commit by walking ancestors from each proposed
input; every output has exactly one successful producer.

## Consequences

- Two independently registered source artifacts with byte-identical
  content have separate, correct provenance records (different
  `source_identity`) while sharing one stored object.
- Lineage can answer "what activity, version, and configuration produced
  this exact output" and "what did this activity consume, by role" for
  any artifact — not just "what were the raw inputs."
- Idempotency is a request-shape property, not a content property. A
  transformation that reads the same logical inputs from a different
  configuration snapshot is a distinct idempotency key and a distinct
  output.
- Detecting and rejecting lineage cycles is a first-class, tested
  invariant, not an accidental consequence of how identity is derived.

## Deferred decisions

- Artifact garbage collection for unreferenced content-addressed objects
  (an unreferenced object is deliberately harmless in Phase 0; cleanup
  policy is a later-phase concern).
- Cross-artifact-type provenance policies beyond the generic
  activity/role model (e.g., ensemble-member fan-in, variable/slice
  mappings) beyond what Section 4.8 of the Phase 0 plan requires.

## Implementation clarification (Phase 0 build)

Plan Section 3's exact file tree lists `storage/postgres/{database,
models,repositories}.py` but omits a file for the PostgreSQL
`IdempotencyLock` implementation that Section 4.9's prose explicitly
requires ("The PostgreSQL `IdempotencyLock` maps the first signed 64
bits of the digest to `pg_advisory_lock`..."). Rather than force that
connection-lifecycle-sensitive logic into `repositories.py` (whose
methods all operate on a shared `Session`, while the advisory lock
needs its own dedicated, non-transactional connection held for the
duration of a multi-step transformation attempt), it is implemented in
a new `storage/postgres/idempotency_lock.py`, consistent with Section
3's `storage.postgres -> storage.interfaces, contracts, common`
dependency rule and its own package. Flagged for Codex review as a
plan-tree completion, not a scope expansion.
