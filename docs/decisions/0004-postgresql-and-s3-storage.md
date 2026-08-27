# 0004: PostgreSQL and S3 Storage

Status: Accepted

## Context

Scientific array data (gridded fields, potentially large) and structured
metadata/provenance have very different storage, query, and durability
requirements. Storing large binary payloads in a relational database
degrades backup, replication, and query performance; storing structured,
relational, transactional metadata (foreign keys, uniqueness constraints,
advisory locks) in an object store is awkward and loses transactional
guarantees. The reviewed architecture requires that scientific array
bytes never live in PostgreSQL.

## Decision

Two storage systems have distinct, non-overlapping responsibilities:

- **PostgreSQL 16** is the sole authority for structured metadata:
  configuration snapshots, grid definitions, runs, artifact manifests,
  activity manifests, and lineage edges. Access is through SQLAlchemy 2
  typed mappings and psycopg 3; schema evolution is through Alembic
  migrations (`migrations/versions/0001_phase0_foundations.py` for
  Phase 0). No protocol exposes update or delete for immutable records;
  only `add` / `add_if_absent` operations exist. `stored_objects`
  contains object *metadata* only (content digest, storage URI, media
  type, byte size) — never payload bytes.
- **An S3-compatible object store** (implemented against the S3 protocol
  with `boto3`; MinIO is the local/CI service) holds all immutable,
  content-addressed scientific bytes at
  `objects/sha256/<first-2-hex>/<remaining-62-hex>`. No filesystem
  production adapter exists in Phase 0 — the storage interface is the S3
  protocol, and MinIO is only the local implementation of that protocol.

Because PostgreSQL and the object store cannot participate in one
distributed transaction, promotion is ordered deliberately: bytes are
uploaded and content-verified (via a temporary key promoted to the final
content-addressed key, never overwriting an existing key) *before* any
PostgreSQL manifest row is created. The failure mode is asymmetric by
design: an unreferenced content-addressed object with no manifest is
harmless and garbage-collectable; a database manifest referencing
unverified or missing bytes is never allowed to exist.

Array serialization (NetCDF4/HDF5 via `h5netcdf`) is implemented behind
a `DatasetSerializer` protocol (`serialize`/`deserialize`) in
`storage/netcdf.py`. Domain contracts (`contracts/datasets.py`) validate
an `xarray.Dataset` directly and never import `h5netcdf`; the serializer
is swappable (e.g., to Zarr in a later phase) without touching domain
validation.

## Consequences

- Metadata queries (idempotency lookups, lineage traversal, run
  eligibility) run against PostgreSQL with normal relational tooling and
  transactional guarantees; they never need to read array bytes.
- Backing up or migrating structured metadata does not require moving
  potentially large scientific arrays, and vice versa.
- Content-addressing gives automatic byte-level deduplication across
  artifacts with different provenance, and integrity is verified on
  every retrieval (`get_verified`), not assumed from the URI alone.
- The recovery story for partial failure is simple and safe: garbage
  objects, never phantom metadata. Garbage collection of unreferenced
  objects is deferred, but its safety is not — dangling manifests are
  structurally impossible because manifests are only ever created after
  successful, verified object promotion.
- Swapping the array serialization format (e.g., adopting Zarr for
  normalized long-term storage) is an infrastructure change behind
  `storage.interfaces.DatasetSerializer`, not a domain-contract change.

## Deferred decisions

- Zarr vs. NetCDF for normalized, long-term array storage.
- Object store retention/garbage-collection policy for unreferenced
  content-addressed objects.
- Any filesystem-only or non-S3-compatible production object store
  adapter.
