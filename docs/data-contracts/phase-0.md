# Phase 0: Foundations

This document summarizes the Phase 0 scope for MesoForge and points to the
authoritative decision records and vocabulary.

## Scope

Phase 0 implements only the reproducible foundation described in
[`docs/architecture/v1.md`](../architecture/v1.md) and the Phase 0
implementation plan: canonical typed contracts (time, grids, variables,
units, artifacts, lineage, configuration), a provenance manifest and
repository interface layer, local PostgreSQL metadata and MinIO object
storage foundations, packaging/CI/quality scaffolding, and a synthetic
end-to-end proof that registers an immutable artifact, transforms it, and
traces lineage.

Phase 0 explicitly does not implement guidance ingestion, GRIB decoding,
regridding, operational forecasting, blending, observations,
verification, bias learning, AI, policy/publication, or any API/UI/
orchestration layer.

## Decisions

See the architecture decision records:

- [0001: Python Modular Monolith](../decisions/0001-python-modular-monolith.md)
- [0002: Artifact Identity and Provenance](../decisions/0002-artifact-identity-and-provenance.md)
- [0003: Configuration Snapshots](../decisions/0003-configuration-snapshots.md)
- [0004: PostgreSQL and S3 Storage](../decisions/0004-postgresql-and-s3-storage.md)

## Vocabulary

Terms used throughout the Phase 0 contracts and services are defined
precisely in [`vocabulary.md`](vocabulary.md). Notably: artifact vs.
object vs. manifest, activity vs. run, the five distinct time concepts
(source reference time, forecast issue time, created/available/ingested/
registered), content digest vs. idempotency digest, and logical replay
vs. artifact replay.

## Repository layout

Production code lives under `src/mesoforge/` in six subpackages —
`common`, `contracts`, `catalog`, `provenance`, `storage`, and
`application` — with a fixed, `import-linter`-enforced dependency
direction described in
[0001](../decisions/0001-python-modular-monolith.md). No packages for
later-phase domains exist yet.

## Exit criteria

Phase 0 is complete when every contract and persistence invariant in the
implementation plan has a passing test, the documented quality/test
commands pass (or a genuine external tooling blocker is reported
honestly), and `tests/acceptance/test_phase0_artifact_lineage.py`
registers a synthetic source artifact, transforms it, and traces its
lineage end to end against real PostgreSQL and MinIO.
