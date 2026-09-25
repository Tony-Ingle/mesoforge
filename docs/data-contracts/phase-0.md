# Foundation data-contract scope

This is the limited reference for shared typed data, identity, configuration and
storage contracts introduced in Phase 0 and still used by current code. The path
is retained because the package documentation refers to it. It is not a product
architecture or a statement that later capabilities are absent.

Use [ARCHITECTURE](../../ARCHITECTURE.md) for current technical structure,
[VISION](../../VISION.md) for target direction and [README](../../README.md) for
usage. The completed foundation plan is [historical](../archive/plans/2026-08-27_182932-phase-0-foundations.md).

## Contract boundaries

| Boundary | Retained contract | Definition / implementation |
| --- | --- | --- |
| Identity | Artifact record identity is separate from content identity; typed IDs and digests are not interchangeable. | [Vocabulary](vocabulary.md#storage-and-identity), [ADR 0002](../decisions/0002-artifact-identity-and-provenance.md), [identifiers](../../src/mesoforge/common/identifiers.py) |
| Time | Reference, issue, availability, ingestion and registration times have distinct meanings. Intervals retain explicit bounds and closure. | [Vocabulary](vocabulary.md#availability-and-time), [time contracts](../../src/mesoforge/common/time.py) |
| Arrays | Canonical datasets validate dimensions, units, grid identity, time relationships and quality masks against catalog definitions. | [Dataset contracts](../../src/mesoforge/contracts/datasets.py), [grids](../../src/mesoforge/catalog/grids.py), [variables](../../src/mesoforge/catalog/variables.py) |
| Configuration | Validated configuration is canonicalized and content-addressed; scientific changes create new identities. Secrets are not scientific configuration. | [ADR 0003](../decisions/0003-configuration-snapshots.md), [configuration](../../src/mesoforge/catalog/configuration.py) |
| Provenance | Activities connect named input/output artifact roles; replay pins recorded identities rather than selecting newer inputs. | [Vocabulary](vocabulary.md#configuration-and-provenance), [provenance contracts](../../src/mesoforge/contracts/provenance.py) |
| Storage | PostgreSQL records metadata/lineage; immutable scientific bytes live in content-addressed object storage and are checksum-verified on read. | [ADR 0004](../decisions/0004-postgresql-and-s3-storage.md), [storage interfaces](../../src/mesoforge/storage/interfaces.py) |

These shared contracts do not prescribe a forecast model set, station population,
horizon, blend policy or publication schedule. Current forecast and observation
artifacts add narrower versioned semantics without rewriting historical records.

## Compatibility and evidence

Historical `RunManifest` and array formats remain meaningful within their own
schema versions. A newer issuance or baseline is not retroactively forced into a
Phase 0 run representation. See the [retained scientific contracts](phase-1.md)
and [Phase 2 contract](phase-2.md) for the shared/legacy boundaries they actually
describe.

The [foundation acceptance test](../../tests/acceptance/test_phase0_artifact_lineage.py)
exercises source registration, transformation and lineage using PostgreSQL and
S3-compatible storage. Its existence describes a validation boundary; this
documentation does not claim that a particular test run or deployment passed.
