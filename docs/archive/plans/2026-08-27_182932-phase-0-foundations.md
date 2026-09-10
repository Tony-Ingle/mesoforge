> **Historical plan (archived 2026-09-09):** This completed Phase 0-2 development plan is retained for reference. Its agent instructions and delivery checklist are not current work orders. Follow the current owner request and root AGENTS.md; see the [archive index](../README.md). Original path: .hermes/plans/2026-08-27_182932-phase-0-foundations.md. The original contents follow unchanged.

# MesoForge Phase 0 Foundations Implementation Plan

> **For Hermes:** Implement this plan task-by-task with strict RED-GREEN-REFACTOR cycles. The pre-created Claude implementation task owns production changes; Codex reviews the resulting branch.

**Goal:** Establish MesoForge's reproducible foundation and prove that a synthetic scientific artifact can be stored immutably, registered transactionally, transformed, and traced through exact configuration and activity lineage.

**Architecture:** Build only the foundation of the approved Python-first modular monolith. Domain contracts remain independent of infrastructure; PostgreSQL is authoritative for metadata and provenance, while an S3-compatible MinIO service stores immutable bytes. A thin application service composes repository and object-store protocols without introducing model ingestion, forecasting, orchestration, APIs, or AI.

**Tech Stack:** Python 3.12, uv, Pydantic 2, xarray/NumPy, Pint/pint-xarray, pyproj, SQLAlchemy 2, Alembic, psycopg 3, boto3/MinIO, h5netcdf, pytest, Hypothesis, mypy, Ruff, pre-commit, Docker Compose.

---

## 1. Authority, scope, and implementation rules

This plan implements only `docs/architecture/v1.md` Phase 0 and incorporates the architecture-review changes in commit `547957cf88a445856c5c4702210bfe545c12a1ee`, especially:

- forecast issue time is distinct from source reference time;
- event, creation, authoritative availability, local ingestion, and registration times are distinct;
- interval, probability, and spatial support are explicit contracts;
- configuration and selected inputs are immutable snapshots;
- lineage is per artifact/activity and extensible to variable/slice mappings;
- domain code depends on protocols, never concrete infrastructure;
- scientific array bytes never live in PostgreSQL.

### In scope

- vocabulary and Phase 0 ADRs;
- canonical time, unit, vertical, grid, variable, dataset, artifact, activity, run, configuration, and lineage contracts;
- immutable configuration resolution and snapshots;
- MinIO-backed artifact-object storage and PostgreSQL metadata repositories;
- migration, packaging, quality, CI, and local-development scaffolding;
- a real PostgreSQL + MinIO synthetic register/transform/trace proof.

### Explicitly out of scope

Do not scaffold or implement guidance-provider adapters, GRIB decoding, Herbie/cfgrib, operational model catalogs, regridding, forecasts, blending, observations, verification, bias learning, AI, policy, publication, FastAPI/UI, Prefect/Dagster, Dask, DuckDB/Parquet, PostGIS, Zarr benchmarking, or deployment beyond local test services. Empty future-domain packages are also prohibited.

### TDD and commit discipline

For every behavior below:

1. add one focused failing test;
2. run that exact test and confirm it fails for the missing behavior rather than a test error;
3. add the smallest implementation;
4. run the exact test, then the affected suite;
5. refactor only while green;
6. commit a coherent vertical slice.

Do not write all contracts and then add tests afterward. Preserve RED output in the implementation-task handoff or commit notes when practical.

## 2. Decisions resolved for Phase 0

Implementation must record these as the named ADRs in Task 1. They are not left to the implementer.

| Decision | Phase 0 choice | Rationale / deferral |
|---|---|---|
| Python | Exactly the 3.12 minor line: `requires-python = ">=3.12,<3.13"`; `.python-version` contains `3.12` | Stable scientific ecosystem; avoids “newest” drift. |
| Environment | uv with committed `pyproject.toml` and `uv.lock` | One reproducible resolver and runner. |
| Architecture | Python modular monolith; domain protocols with adapters composed in `application` | Keeps scientific/domain behavior independently testable. |
| Metadata | PostgreSQL 16, SQLAlchemy 2 typed mappings, psycopg 3, Alembic | Production-shaped constraints and transactions from the start. SQLite is not a supported substitute. |
| Objects | S3 protocol implemented with boto3; MinIO is the local/CI service | Matches the approved S3-compatible seam. No filesystem production adapter in Phase 0. |
| Array serialization | NetCDF4/HDF5 through `h5netcdf` for the proof | Small and portable. Serializer is an interface; Zarr choice remains deferred. |
| IDs | UUID4 for run, artifact, and activity record identity; typed prefixed string wrappers at Python boundaries (`run_`, `art_`, `act_`) and PostgreSQL UUID columns | Avoid a UUIDv7 dependency. Identity is deliberately separate from content. |
| Digests | Lowercase `sha256:<64 hex>` strings | One representation for content, config, environment, parameters, and idempotency digests. |
| Object key | `objects/sha256/<first-2-hex>/<remaining-62-hex>` | Bytes are content-addressed and deduplicated; artifact records are not. |
| Artifact identity | Generated record ID plus independent content digest | Identical bytes may have distinct valid provenance. Idempotency, not checksum, deduplicates a transformation request. |
| Configuration | YAML source → strict Pydantic model → RFC 8785/JCS canonical JSON → SHA-256 snapshot | Formatting and mapping order cannot change identity; list order remains meaningful. Use `jcs` rather than home-grown JSON canonicalization. |
| Units | One application-owned Pint registry with offset conversion enabled; definitions are controlled by the variable registry | Free-form unit strings never pass contract validation. |
| Datetimes | UTC-aware `datetime` in control-plane contracts; naive `datetime64[ns]` plus mandatory `time_encoding="UTC"` in xarray | Matches reviewed architecture and xarray limitations. |
| Reproducibility | Logical reproducibility plus retained artifact reproducibility | Cross-platform bitwise NetCDF reproduction is not promised. Stored historical bytes remain exact. |
| Provenance | Artifact nodes connected by first-class activity nodes with named input/output roles | A plain `parents` list cannot explain the transformation. |
| Idempotency | SHA-256 over activity type/version, ordered role+artifact inputs, config digest, parameter digest, code revision, environment digest, and output schema | Repeat requests return the original succeeded activity/output. Failed attempts do not reserve the key forever. |
| Availability | Source availability is caller-supplied with authority/method; derived availability is computed as `max(parent.available_at, activity.completed_at, registration transaction time)` | Prevents derived data from appearing earlier than inputs or successful registration. |
| Orchestration/API | None | The proof is an application service invoked by pytest; orchestration and HTTP are later decisions. |
| Import enforcement | `import-linter` contracts in CI | Package boundaries must be executable, not prose only. |

Phase 0 intentionally leaves operational geography, model/product selection, observations, PoP/QPF definitions, regridding, forecast deadlines, publication, retention policy, AI, and workflow-orchestrator choice unresolved because no Phase 0 interface depends on them.

## 3. Exact repository boundary

Create only this production tree:

```text
src/mesoforge/
├── __init__.py
├── common/
│   ├── __init__.py
│   ├── errors.py
│   ├── identifiers.py
│   └── time.py
├── contracts/
│   ├── __init__.py
│   ├── artifacts.py
│   ├── datasets.py
│   ├── provenance.py
│   └── runs.py
├── catalog/
│   ├── __init__.py
│   ├── configuration.py
│   ├── grids.py
│   ├── units.py
│   └── variables.py
├── provenance/
│   ├── __init__.py
│   ├── lineage.py
│   └── services.py
├── storage/
│   ├── __init__.py
│   ├── interfaces.py
│   ├── netcdf.py
│   ├── s3.py
│   └── postgres/
│       ├── __init__.py
│       ├── database.py
│       ├── models.py
│       └── repositories.py
└── application/
    ├── __init__.py
    ├── configuration.py
    └── artifacts.py
```

Dependency direction:

```text
application -> provenance, catalog, contracts, storage.interfaces, common
provenance  -> contracts, storage.interfaces, common
catalog     -> contracts, common
contracts   -> common
storage.s3/postgres -> storage.interfaces, contracts, common
common      -> Python standard library only
```

Rules:

- `contracts`, `catalog`, and `provenance` must not import boto3, SQLAlchemy, psycopg, Alembic, CLI/API, or orchestration modules.
- `storage.interfaces` contains `Protocol` declarations and contract types only.
- Concrete storage modules must not contain scientific transformations.
- `application` owns transaction ordering and composition, not meteorological algorithms.
- SQLAlchemy objects never escape repository adapters.
- Large payload bytes never appear in database JSON columns or log output.

## 4. Canonical contracts

All Pydantic models use `ConfigDict(extra="forbid", frozen=True, strict=True)`. Enumerations serialize to the lowercase values shown. All public models include `schema_version: Literal[...]` where listed. Unsupported major versions fail validation; no silent migration occurs.

### 4.1 Identifiers and digest

`src/mesoforge/common/identifiers.py` defines immutable validated string types:

- `ArtifactId`: `art_` plus canonical lowercase UUID text;
- `ActivityId`: `act_` plus canonical lowercase UUID text;
- `RunId`: `run_` plus canonical lowercase UUID text;
- `ConfigurationSnapshotId`: exactly the configuration digest rendered as `cfg_sha256_<64 hex>`;
- `GridId`, `VariableId`, `VerticalDefinitionId`: lowercase kebab/dot identifiers matching `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`;
- `Digest`: exactly `sha256:<64 lowercase hex>`.

Factories use `uuid.uuid4()` for record IDs. Parsers reject uppercase, braces, noncanonical UUIDs, unknown digest algorithms, and malformed lengths.

### 4.2 Time contracts

`src/mesoforge/common/time.py`:

- `UtcInstant` validator accepts only timezone-aware `datetime`, rejects naive values, and normalizes to offset `+00:00`;
- serialized control-plane instants use ISO 8601 microsecond precision with `Z`;
- `IntervalClosure`: `left_closed_right_open`, `closed`, `open`, `left_open_right_closed`;
- `TemporalSemantics`: `instantaneous`, `average`, `maximum`, `minimum`, `accumulation`, `probability`;
- `TimeAxisDefinition(schema_version="time-axis.v1")` has `forecast_reference_time`, strictly increasing/nonnegative `lead_times: tuple[timedelta, ...]`, and derived `valid_times`;
- user-provided `valid_times`, if supported only at deserialization, must exactly equal reference plus lead at nanosecond resolution or fail;
- `IntervalDefinition` has `start < end`, closure, and no implicit timezone conversion;
- interval-valued variables require one interval per lead and may not use `instantaneous`.

Control-plane tests must distinguish `forecast_reference_time`, `forecast_issue_time`, `information_cutoff`, `created_at`, `available_at`, `ingested_at`, and `registered_at`; none is an alias.

### 4.3 Units and vertical definitions

`src/mesoforge/catalog/units.py` owns one lazily constructed Pint `UnitRegistry(autoconvert_offset_to_baseunit=True)`. It exposes only:

- `parse_registered_unit(unit_id) -> pint.Unit`;
- `assert_compatible(from_unit_id, to_unit_id) -> None`;
- `convert(values, from_unit_id, to_unit_id)`.

The initial controlled unit IDs are `K`, `degC`, `m/s`, `kg/m^2`, `percent`, `dimensionless`, `m`, and `Pa`. Persist the controlled ID, not Pint's display formatting.

`VerticalDefinition(schema_version="vertical-definition.v1")` fields:

- `vertical_definition_id`;
- `coordinate_type`: `surface`, `height_above_ground`, `pressure`, `model_level`;
- `value` and `unit_id`, both required together for non-surface physical levels;
- optional `positive_direction`: `up` or `down`.

Reject `air_temperature_2m` definitions that do not reference exactly 2 m height above ground in registry validation.

### 4.4 Grid contract

`GridDefinition(schema_version="grid-definition.v1")` in `catalog/grids.py`:

- `grid_id`, globally unique for the complete immutable definition; use versioned values such as `synthetic-grid.v1` and never reuse an ID for changed coordinates or metadata;
- `crs_wkt2: str` normalized by `pyproj.CRS.from_user_input(...).to_wkt("WKT2_2019")` before snapshotting;
- `shape_y`, `shape_x`: positive integers;
- `x_coordinates`, `y_coordinates`: finite, strictly monotonic tuples whose lengths match shape;
- `coordinate_reference`: `cell_center` or `cell_bounds`;
- `longitude_convention`: `minus_180_to_180` or `zero_to_360`;
- `orientation`: `x_east_y_north`, `x_east_y_south`, or explicit `other` with required note;
- `spatial_support`: `point`, `cell_mean`, `cell_total`, `categorical_cell`;
- optional `domain_geometry_artifact_id`, `terrain_artifact_id`, and `land_sea_mask_artifact_id`.

The JCS digest of all fields except `grid_id` is `definition_digest`. `grid_id` is registered globally in PostgreSQL with that digest when a configuration snapshot is registered: the same ID+digest is idempotent; the same ID with different bytes is rejected. Any coordinate, CRS, orientation, or support change therefore requires a new globally unique `grid_id` (for example, `.v2`) and a new configuration snapshot. There is no separately addressable grid revision in Phase 0, so dataset lookup by `grid_id` is unambiguous.

### 4.5 Variable contract

`VariableDefinition(schema_version="variable-definition.v1")` in `catalog/variables.py`:

- `variable_id`, `standard_name`, `canonical_unit_id`, `dtype` (`float32`, `float64`, `int16`, `int32`, `uint8`, `bool`, or `str`);
- `temporal_semantics`, `spatial_support`, `vertical_definition_id`;
- `allowed_dimension_variants`, an ordered tuple selected from:
  - `("lead_time", "location")`;
  - `("lead_time", "y", "x")`;
  - `("member", "lead_time", "location")`;
  - `("member", "lead_time", "y", "x")`;
  - `("lead_time", "level", "location")`;
  - `("lead_time", "level", "y", "x")`;
  - ensemble variants with `member` first and `level` before spatial dimensions;
- `missing_value_policy`: `nan_with_quality_mask`, `explicit_fill_with_quality_mask`, or `not_applicable`;
- optional finite `valid_min`, `valid_max` in canonical units;
- `interval_required` true for accumulation/average/minimum/maximum/probability and false for instantaneous;
- optional `probability_event` with `operator`, threshold, threshold unit, population/support, and processing status; required only when temporal semantics is probability.

Initial fixtures register only `air_temperature_2m` (`K`, float32, instantaneous, cell_mean, `height-agl-2m`) and a synthetic source variant in `degC`. Do not define operational PoP/QPF products in Phase 0.

### 4.6 Canonical xarray dataset contract

`contracts/datasets.py` validates an `xarray.Dataset`; it is not coupled to NetCDF serialization.

Required:

- scalar `forecast_reference_time: datetime64[ns]`;
- one-dimensional `lead_time: timedelta64[ns]`, nonnegative and strictly increasing;
- one-dimensional `valid_time: datetime64[ns]` with exact invariant `reference + lead`;
- dataset attributes `schema_version="canonical-guidance.v1"`, `time_encoding="UTC"`, `grid_id`, `configuration_snapshot_id`, and `variable_lineage_manifest_id`;
- for each data variable, exact dtype and dimension ordering from its `VariableDefinition`;
- variable attributes `unit_id`, `temporal_semantics`, `spatial_support`, `vertical_definition_id`, and `quality_mask` name;
- a `uint16` quality mask with identical dimensions, with Phase 0 bits `1=missing`, `2=outside_coverage`, `4=calculated_invalid`; unknown set bits fail validation;
- finite values unless the corresponding mask marks missing/invalid;
- grid dimensions and coordinates matching the referenced immutable `GridDefinition`;
- interval variables each reference their own `<variable>_interval_bounds(lead_time,bounds)` of `datetime64[ns]`, `bounds` length 2, plus `interval_closure`;
- no undeclared dimensions, no timezone-bearing object arrays, and no arbitrary unit strings.

`storage.interfaces.DatasetSerializer` defines `serialize(dataset) -> bytes` and `deserialize(bytes) -> Dataset`. The concrete `storage/netcdf.py:H5NetcdfDatasetSerializer` adapter owns the h5netcdf dependency and writes a deterministic logical schema (fixed variable order, fixed dtypes, no wall-clock history attribute); tests assert logical equality after round trip, not cross-platform byte identity. Domain contracts never import h5netcdf.

### 4.7 Configuration snapshot

`catalog/configuration.py` defines strict `MesoForgeConfiguration(schema_version="mesoforge-config.v1")` with only:

- `grids: tuple[GridDefinition, ...]`;
- `vertical_definitions: tuple[VerticalDefinition, ...]`;
- `variables: tuple[VariableDefinition, ...]`;
- `artifact_store: ArtifactStoreSettings` containing bucket and endpoint reference only;
- `metadata_store: MetadataStoreSettings` containing a DSN environment-variable name only.

Secrets and actual credentials/DSNs are forbidden in snapshots. YAML composition order is:

1. required base file `configs/base.yaml`;
2. optional explicitly named environment file `configs/environments/<name>.yaml`;
3. explicit in-process override mapping, restricted to the exact paths `artifact_store.endpoint_reference`, `artifact_store.bucket`, and `metadata_store.dsn_environment_variable`; overrides of `grids`, `vertical_definitions`, `variables`, schema versions, IDs, or any nested scientific field are rejected.

Deep merge maps; replace lists wholesale; reject duplicate IDs and unknown keys. Environment variables may provide credentials and service locations but may not override scientific catalog values.

`ConfigurationSnapshot(schema_version="configuration-snapshot.v1")` contains:

- `configuration_snapshot_id`;
- `configuration_digest`;
- `canonical_json`;
- `source_references`, each with repository-relative path and SHA-256 content digest;
- `created_at` (audit metadata excluded from digest).

Digest input is only the JCS bytes of the validated `MesoForgeConfiguration`. Thus YAML comments, whitespace, and key order do not affect identity; list order does. Registering an existing digest is idempotent and returns the original snapshot record.

`application/configuration.py:ConfigurationService.register(snapshot)` opens one unit of work, calls `GridRepository.add_if_absent` for every grid in the snapshot, then `ConfigurationRepository.add_if_absent`, and commits atomically. A reused `grid_id` with a different definition aborts the entire snapshot registration; an identical grid and snapshot are idempotent.

### 4.8 Run, artifact, activity, and availability contracts

`RunManifest(schema_version="run-manifest.v1")`:

- `run_id`, `forecast_issue_time`, `information_cutoff`;
- `configuration_snapshot_id`, `configuration_digest`;
- `code_revision` (exact 40-character lowercase Git SHA);
- `environment_digest`, `lockfile_digest`;
- `random_seed: int`;
- `selected_input_artifact_ids: tuple[ArtifactId, ...]` preserving selection order;
- `created_at`.

Invariant: each selected source input has authoritative `available_at <= information_cutoff`; selected revisions are never re-resolved during replay.

`Availability(schema_version="availability.v1")`:

- `available_at`;
- `authority` (controlled identifier);
- `method` (versioned identifier);
- optional `ingested_at`;
- optional explanatory metadata free of credentials.

`ArtifactManifest(schema_version="artifact-manifest.v1")`:

- `artifact_id`, `artifact_type` (controlled identifier), `artifact_schema_version`;
- `media_type`, `byte_size >= 0`, `content_digest`;
- `storage_uri` using `s3://<bucket>/<content-addressed-key>`;
- `created_at`, `registered_at`, `availability`;
- optional `run_id`;
- `configuration_snapshot_id`, `configuration_digest`;
- `code_revision`, `environment_digest`;
- `quality_state`: `valid`, `partial`, `invalid`;
- optional `source_registration_digest`, required only for lineage-root/source artifacts and absent from derived artifacts;
- optional `source_identity: SourceIdentity` with required `authority`, `locator`, and `revision`; required together with `source_registration_digest` for lineage roots and forbidden on derived artifacts;
- immutable JSON `attributes` limited to 32 KiB after canonical serialization.

Do not include authoritative `parents` in this model. Lineage comes from activities. The manifest references a content-addressed stored-object row; several artifact records with different provenance may intentionally reference the same immutable bytes.

`ActivityManifest(schema_version="activity-manifest.v1")`:

- `activity_id`, `activity_type`, `activity_version`;
- `status`: `started`, `succeeded`, `failed`;
- `started_at`, optional `completed_at`;
- `idempotency_digest`, `parameters_digest`;
- `configuration_snapshot_id`, `configuration_digest`;
- `code_revision`, `environment_digest`, optional `run_id`;
- ordered, unique-role `inputs: tuple[ActivityArtifactRef, ...]`;
- ordered, unique-role `outputs: tuple[ActivityArtifactRef, ...]`;
- optional structured error `{error_type, message_digest, retryable}` without stack traces or payload data.

Invariants:

- started has no completion, outputs, or error;
- succeeded has completion and at least one input/output, no error;
- failed has completion and error, no outputs;

- every input exists before activity start;
- every output has exactly one successful producer;
- output availability follows the derived rule in Section 2;
- a succeeded idempotency digest is unique; the service acquires a PostgreSQL session advisory lock before creating a started activity, so a concurrent loser rechecks after waiting, returns the winner, and never creates a second activity;
- lineage insertion and artifact metadata registration commit in one PostgreSQL transaction after object promotion;
- cycles are rejected before commit by walking ancestors from each proposed input.

### 4.9 Repository and object-store protocols

`storage/interfaces.py` defines behaviorally complete protocols:

```text
ArtifactObjectStore
  put_if_absent(content_digest, bytes, media_type) -> StoredObject
  get_verified(storage_uri, expected_digest) -> bytes
  exists_verified(storage_uri, expected_digest) -> bool
  delete_unregistered(storage_uri) -> None

UnitOfWork
  __enter__() -> self
  stored_objects: StoredObjectRepository
  artifacts: ArtifactRepository
  activities: ActivityRepository
  configurations: ConfigurationRepository
  grids: GridRepository
  runs: RunRepository
  commit() -> None
  rollback() -> None

ArtifactRepository
  add(manifest) -> ArtifactManifest
  get(artifact_id) -> ArtifactManifest
  get_many(ids) -> tuple[ArtifactManifest, ...]
  find_by_source_registration_digest(digest) -> ArtifactManifest | None

StoredObjectRepository
  add_if_absent(stored_object) -> StoredObject
  get(content_digest) -> StoredObject

ActivityRepository
  add_started(manifest) -> ActivityManifest
  finish_succeeded(activity_id, outputs, completed_at) -> ActivityManifest
  finish_failed(activity_id, error, completed_at) -> ActivityManifest
  find_succeeded_by_idempotency(digest) -> ActivityManifest | None
  producer_of(artifact_id) -> ActivityManifest | None
  consumers_of(artifact_id) -> tuple[ActivityManifest, ...]

ConfigurationRepository
  add_if_absent(snapshot) -> ConfigurationSnapshot
  get(snapshot_id) -> ConfigurationSnapshot

GridRepository
  add_if_absent(grid_id, definition_digest, canonical_json) -> GridDefinition
  get(grid_id) -> GridDefinition

RunRepository
  add(manifest) -> RunManifest
  get(run_id) -> RunManifest

LineageReader
  ancestors(artifact_id) -> LineageGraph
  descendants(artifact_id) -> LineageGraph

IdempotencyLock
  acquire(digest) -> context manager
```

The PostgreSQL `IdempotencyLock` maps the first signed 64 bits of the digest to `pg_advisory_lock`, holds a dedicated connection (not an open SQL transaction) for the transformation attempt, and always calls `pg_advisory_unlock` in `finally`; connection loss also releases the lock.

No protocol exposes update/delete for immutable records. Missing records raise typed `NotFound`; conflicting identity or immutability raises `Conflict`; invalid lineage raises `LineageViolation`; checksum failures raise `IntegrityError`.

### 4.10 PostgreSQL schema

Alembic revision `0001_phase0_foundations.py` creates:

- `configuration_snapshots(id text PK, digest text UNIQUE NOT NULL, schema_version text, canonical_json jsonb, source_references jsonb, created_at timestamptz)`;
- `grids(id text PK, definition_digest text NOT NULL, canonical_json jsonb NOT NULL, created_at timestamptz)`; `add_if_absent` compares digest and canonical JSON and rejects ID reuse with a changed definition;
- `stored_objects(content_digest text PK, storage_uri text UNIQUE NOT NULL, media_type text, byte_size bigint CHECK >= 0, verified_at timestamptz)`; this table contains object metadata only, never payload bytes;
- `runs(id uuid PK, schema_version text, forecast_issue_time timestamptz, information_cutoff timestamptz, configuration_snapshot_id text FK, configuration_digest text, code_revision char(40), environment_digest text, lockfile_digest text, random_seed bigint, selected_inputs jsonb, created_at timestamptz)`;
- `artifacts(id uuid PK, schema_version text, artifact_type text, artifact_schema_version text, content_digest text FK stored_objects, source_registration_digest text NULL, source_authority text NULL, source_locator text NULL, source_revision text NULL, created_at timestamptz, registered_at timestamptz DEFAULT transaction_timestamp(), available_at timestamptz, availability_authority text, availability_method text, ingested_at timestamptz NULL, run_id uuid NULL FK, configuration_snapshot_id text FK, configuration_digest text, code_revision char(40), environment_digest text, quality_state text CHECK IN (...), attributes jsonb)`; a CHECK requires all four source fields together or all null, and repositories join the stored-object row to reconstruct manifest `media_type`, `byte_size`, and `storage_uri`;
- `activities(id uuid PK, schema_version text, activity_type text, activity_version text, status text CHECK IN (...), started_at timestamptz, completed_at timestamptz NULL, idempotency_digest text, parameters_digest text, configuration_snapshot_id text FK, configuration_digest text, code_revision char(40), environment_digest text, run_id uuid NULL FK, error jsonb NULL)`;
- `activity_inputs(activity_id uuid FK, ordinal smallint, role text, artifact_id uuid FK, PRIMARY KEY(activity_id, ordinal), UNIQUE(activity_id, role))`;
- `activity_outputs(activity_id uuid FK, ordinal smallint, role text, artifact_id uuid UNIQUE FK, PRIMARY KEY(activity_id, ordinal), UNIQUE(activity_id, role))`.

Add partial unique indexes on `activities(idempotency_digest) WHERE status='succeeded'` and `artifacts(source_registration_digest) WHERE source_registration_digest IS NOT NULL`; indexes on input/output artifact IDs, artifact run ID, artifact availability, and activity run ID. Add database checks for status shape where expressible; enforce graph acyclicity and cross-row availability in the service transaction. No mutable `updated_at` columns exist.

Migration downgrade is supported for local development and drops only these Phase 0 tables in reverse dependency order. `stored_objects` outlives no referencing artifact during a downgrade, and normal application repositories expose no delete operation for either record.

## 5. Application-service transaction algorithm

`application/artifacts.py` exposes both APIs below; infrastructure is injected:

- `SourceRegistrationRequest` is frozen/strict and contains `source_authority`, `source_locator`, `source_revision`, `artifact_type`, `artifact_schema_version`, `media_type`, optional expected content digest, `created_at`, `Availability`, optional run ID, configuration snapshot ID/digest, code revision, environment digest, quality state, and bounded attributes. The service, never the caller, computes `source_registration_digest` over the named source identity fields, actual content digest, schema/config/code/environment, and quality state.
- `ArtifactService.register_source(request: SourceRegistrationRequest, payload: bytes) -> ArtifactManifest` validates caller-owned source availability, computes both digests, acquires the idempotency lock, rechecks `find_by_source_registration_digest`, hashes/promotes/verifies bytes, and then atomically inserts the stored-object and source-artifact rows. It has no activity producer because external source bytes are lineage roots. Repeating the request returns the original source artifact. Upload failure or digest mismatch creates no manifest.
- `ArtifactService.execute_transformation(request, transform, serializer) -> TransformationResult` handles derived artifacts. The function parameter is a pure callable over validated datasets.

Exact order:

1. validate the request, ordered input roles, referenced artifacts, configuration snapshot, code/environment revisions, and parameters;
2. compute the idempotency digest, acquire its PostgreSQL session advisory lock, and recheck for an existing succeeded result; if found, return it without creating an activity; hold the lock through step 10 but hold no database transaction during computation;
3. retrieve every input through `get_verified`; reject checksum mismatch;
4. deserialize and contract-validate inputs;
5. create and commit a `started` activity so a crash is auditable;
6. call the pure transform; validate output dataset before serialization;
7. serialize, compute SHA-256 and byte size, and `put_if_absent` to a temporary MinIO key followed by server-side copy/promotion to the content key; never overwrite an existing content key;
8. while still holding the advisory lock, begin one PostgreSQL transaction and obtain one database value `registration_time = transaction_timestamp()`; set both `registered_at = registration_time` and `available_at = GREATEST(max(parent.available_at), activity.completed_at, registration_time)` in the insert/RETURNING statement, then reconstruct the frozen manifest from returned values, insert the activity output edge, mark activity succeeded, and commit; callers do not pre-populate derived `available_at` or `registered_at`;
9. the advisory lock makes an in-process concurrent winner impossible after step 2; the partial unique index remains a fail-closed backstop for nonconforming/older clients, whose constraint failure rolls back with no manifest/output edge;
10. on transform/storage/registration failure, mark the activity failed in a separate bounded transaction, remove temporary unregistered objects, and re-raise a typed error;
11. retrieval always streams/hash-verifies bytes before returning them, and `finally` releases the advisory lock.

The PostgreSQL row and final content object cannot participate in one distributed transaction. The recovery rule is deliberate: an unreferenced final content-addressed object is harmless and garbage-collectable; a database manifest may never be committed until final object verification succeeds. Phase 0 tests cover the no-manifest-on-upload-failure direction. Garbage collection itself is deferred.

## 6. Ordered implementation tasks

### Task 1: Record vocabulary and decisions

**Create:**

- `docs/decisions/0001-python-modular-monolith.md`
- `docs/decisions/0002-artifact-identity-and-provenance.md`
- `docs/decisions/0003-configuration-snapshots.md`
- `docs/decisions/0004-postgresql-and-s3-storage.md`
- `docs/data-contracts/vocabulary.md`
- `docs/data-contracts/phase-0.md`

ADRs use status `Accepted`, context, decision, consequences, and deferred decisions. Vocabulary defines artifact, object, manifest, activity, run, source/derived availability, ingestion, registration, configuration snapshot, content digest, idempotency digest, lineage, logical replay, artifact replay, source reference time, forecast issue time, valid time, lead time, interval, spatial support, and quality state exactly as this plan uses them.

**Validation:** `uv run python scripts/validate_docs.py` checks required ADR headings, unique numbers, accepted status, and relative links. This validator is introduced test-first in Task 2; until then manually inspect links without adding a one-off production script.

### Task 2: Bootstrap packaging and quality gates

**Create/modify:**

- `pyproject.toml`, `uv.lock`, `.python-version`, `Makefile`;
- `.pre-commit-config.yaml`, `.importlinter`, `.github/workflows/ci.yml`;
- `src/mesoforge/__init__.py`, package `__init__.py` files from Section 3;
- `scripts/validate_docs.py`, `tests/unit/test_validate_docs.py`;
- `tests/conftest.py`, `tests/unit/`, `tests/contracts/`, `tests/integration/`, `tests/property/`, `tests/acceptance/`;
- update `.gitignore` for `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage`, `htmlcov/`, `dist/`, `build/`, `*.egg-info/`, `.hypothesis/`, `var/`, MinIO/PostgreSQL volumes, NetCDF/Zarr/runtime artifacts while permitting only explicit synthetic fixtures.

Runtime dependency ranges:

- `pydantic>=2.10,<3`, `jcs>=0.2,<1`;
- `numpy>=2,<3`, `xarray>=2025.1,<2027`, `pint>=0.24,<1`, `pint-xarray>=0.4,<1`, `pyproj>=3.7,<4`, `h5netcdf>=1.4,<2`;
- `sqlalchemy>=2.0,<3`, `psycopg[binary]>=3.2,<4`, `alembic>=1.14,<2`;
- `boto3>=1.35,<2`.

Development dependency ranges:

- `pytest>=8,<10`, `pytest-cov>=6,<8`, `hypothesis>=6,<7`;
- `mypy>=1.14,<2`, `ruff>=0.9,<1`, `import-linter>=2.1,<3`, `pre-commit>=4,<5`;
- `types-PyYAML>=6,<7` only if PyYAML is selected; prefer Pydantic's JSON path plus `PyYAML>=6,<7` for YAML loading;
- no testcontainers dependency: Compose services and environment DSNs are explicit.

Ruff owns formatting and linting; line length 100; Python target 3.12. Mypy uses strict mode for `src/mesoforge` and `scripts`, with justified per-library overrides only. Pytest registers `integration` and `acceptance` markers and treats warnings as errors except narrowly documented third-party warnings.

CI jobs:

1. `quality`: checkout, setup Python/uv, `uv sync --locked --all-groups`, lock check, Ruff format/lint, mypy, import-linter, docs validation, secret/forbidden-artifact script;
2. `unit`: unit + contract + property tests with coverage;
3. `integration`: PostgreSQL 16 and MinIO service containers, migration up/down/up, integration and acceptance tests.

Pin action major versions and container image versions; before merging implementation, replace third-party action tags with commit SHAs and record them in comments.

**RED:** docs validator test and import-boundary failure test first.
**GREEN:** smallest packaging/configuration to pass.
**Verify:** `uv lock --check && uv run ruff format --check . && uv run ruff check . && uv run mypy src scripts && uv run lint-imports && uv run pytest tests/unit -q`.

### Task 3: Implement common identity and time primitives

**Create:** Section 4.1/4.2 files.
**Tests:**

- `tests/unit/common/test_identifiers.py`;
- `tests/unit/common/test_time.py`;
- `tests/property/test_time_axis.py`.

Test malformed/noncanonical IDs and digests, naive datetimes, UTC normalization, invalid intervals, duplicate/decreasing/negative leads, and the exact valid-time property over generated reference times/leads.

**RED command:** `uv run pytest tests/unit/common/test_identifiers.py tests/unit/common/test_time.py tests/property/test_time_axis.py -q`.
**GREEN verification:** same command, then `uv run mypy src/mesoforge/common`.

### Task 4: Implement unit, vertical, grid, and variable registries

**Create:** Section 4.3–4.5 files.
**Tests:**

- `tests/unit/catalog/test_units.py`;
- `tests/unit/catalog/test_vertical_definitions.py`;
- `tests/contracts/test_grid_definition.py`;
- `tests/contracts/test_variable_definition.py`;
- `tests/property/test_unit_conversions.py`.

Required assertions include °C↔K expected values, incompatible conversion rejection, offset-unit correctness, finite/monotonic grid coordinates, canonical WKT stability, shape mismatch rejection, exact dimension-order allowlist, interval/probability requirements, and 2 m vertical binding.

### Task 5: Implement canonical dataset validation and NetCDF serializer

**Create/modify:**

- `src/mesoforge/contracts/datasets.py`;
- `src/mesoforge/storage/netcdf.py`;
- `tests/fixtures/synthetic.py` (Python-generated fixture; do not commit binary NetCDF);
- `tests/contracts/test_canonical_dataset.py`;
- `tests/property/test_dataset_time_invariant.py`;
- `tests/unit/storage/test_h5netcdf_serializer.py`.

Build the synthetic 2×2 grid and two leads in Python. Test all exact Section 4.6 requirements, including each quality-mask state and interval bounds. Round-trip through bytes and compare dataset structure, dtypes, coordinates, attributes, masks, and values. Do not assert identical regenerated HDF5 bytes.

### Task 6: Implement immutable configuration resolution

**Create:**

- `configs/base.yaml` with only the synthetic Phase 0 grid/variables and environment-reference names;
- `configs/environments/local.yaml` with non-secret endpoint/bucket names;
- `src/mesoforge/catalog/configuration.py`;
- `src/mesoforge/application/configuration.py`;
- `tests/unit/catalog/test_configuration.py`;
- `tests/integration/catalog/test_configuration_registration.py`;
- `tests/property/test_configuration_digest.py`.

Tests prove strict unknown-key rejection, secret/DSN rejection, deterministic deep merge, list replacement, duplicate-ID rejection, same digest under YAML formatting/key-order changes, changed digest for any semantic change, preserved list order, source-file checksums, frozen models, acceptance of the three named operational override paths, rejection of every scientific/unknown override path, atomic grid+snapshot registration, idempotent identical grid reuse, and rollback when an existing grid ID has a changed definition.

### Task 7: Implement artifact, activity, run, and lineage contracts

**Create:**

- `src/mesoforge/contracts/artifacts.py`;
- `src/mesoforge/contracts/provenance.py`;
- `src/mesoforge/contracts/runs.py`;
- `src/mesoforge/provenance/lineage.py`;
- matching unit/contract/property tests.

**Tests:** `tests/contracts/test_artifact_manifest.py`, `test_activity_manifest.py`, `test_run_manifest.py`, and `tests/unit/provenance/test_lineage.py` cover every invariant in Section 4.8, deterministic idempotency input ordering, role uniqueness, cycle rejection, graph ordering, and canonical JSON lineage export.

`LineageGraph(schema_version="lineage-graph.v1")` contains sorted artifact nodes, sorted activity nodes, typed directed edges with roles/ordinals, and root artifact ID. Sorting is by canonical ID so JSON export is deterministic.

### Task 8: Add database schema and PostgreSQL repositories

**Create:**

- `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_phase0_foundations.py`;
- `src/mesoforge/storage/postgres/{database,models,repositories}.py`;
- `tests/integration/storage/test_migrations.py`;
- repository contract suite in `tests/contracts/storage/repository_contract.py` executed against PostgreSQL by `tests/integration/storage/test_postgres_repositories.py`.

Tests run upgrade→downgrade→upgrade on an empty database; assert every constraint/index; round-trip timezone-aware timestamps and frozen contract objects; reject missing FKs, duplicate producer, duplicate succeeded idempotency, and updates; and verify rollback leaves no partial graph. Do not expose ORM entities.

**Verify:** `uv run alembic upgrade head && uv run pytest tests/integration/storage/test_migrations.py tests/integration/storage/test_postgres_repositories.py -q`.

### Task 9: Implement immutable MinIO object storage

**Create:**

- `src/mesoforge/storage/s3.py`;
- `deploy/local/compose.yaml` with PostgreSQL 16 and MinIO services, health checks, named ignored volumes, fixed local ports, non-production test credentials supplied via `.env.example`;
- `tests/contracts/storage/object_store_contract.py`;
- `tests/integration/storage/test_s3_object_store.py`.

Tests verify content-addressed key derivation, `put_if_absent`, same-byte idempotency, concurrent writers, no overwrite, ranged/streamed checksum verification, corruption/wrong-digest detection, missing object behavior, temporary cleanup, and that returned URIs contain no credentials. Unit-test boto request construction only where needed; behavior runs against real MinIO.

**Verify:** `docker compose -f deploy/local/compose.yaml up -d --wait` then `uv run pytest tests/integration/storage/test_s3_object_store.py -q`.

### Task 10: Implement registration and lineage services

**Create:**

- `src/mesoforge/provenance/services.py`;
- `src/mesoforge/application/artifacts.py`;
- `tests/unit/application/test_artifact_service.py` using behaviorally complete in-memory test doubles defined under tests only;
- `tests/integration/application/test_artifact_registration.py` using real PostgreSQL and MinIO.

Implement the Section 5 algorithm. Unit tests drive ordering/error branches; integration tests prove transaction and object behavior. Add no CLI or workflow engine.

Required source-registration tests: successful root registration with exact authority/locator/revision round trip, service-computed idempotency, all-or-none source identity constraint, wrong supplied checksum, upload failure, duplicate content with distinct source provenance, and source availability after a run cutoff. Required transformation failure tests: input checksum mismatch, transform exception, output contract violation, object upload failure, database commit failure, concurrent duplicate idempotency, dangling input, attempted cycle, and derived availability earlier than a parent. Verify a failed upload has neither manifest nor successful activity/output; a failed transform retains only a sanitized failed activity; concurrent identical requests return the same winner and only one activity/output exists with no second started record.

### Task 11: Build the synthetic vertical acceptance proof

**Create:**

- `tests/acceptance/test_phase0_artifact_lineage.py`;
- `docs/operations/local-development.md`.

Dataset:

- reference time `2026-01-01T00:00:00Z`;
- leads `[PT0H, PT1H]` and exact derived valid times;
- 2×2 projected synthetic cell-center grid;
- `air_temperature_2m`, float32, source unit `degC`, finite fixed values;
- pure test transformation validates source contract and converts to canonical `K`.

Against real PostgreSQL and MinIO, the test must:

1. resolve and register the immutable configuration snapshot;
2. register a synthetic source artifact with exact checksum, authoritative availability, code/environment/config identity;
3. execute the pure °C→K transformation;
4. register the canonical output and successful activity atomically;
5. retrieve and checksum-verify the output;
6. assert exact coordinates, Kelvin values, dtype, units, grid, quality mask, and valid-time invariant;
7. trace backward to source and forward to output, recovering input/output roles, activity version, config snapshot, digests, run, availability, and storage URIs;
8. export deterministic lineage JSON, reconstruct the graph from persisted records only, and compare;
9. repeat the identical request and assert one succeeded activity and the same output artifact;
10. change one semantic configuration value and assert a new snapshot, idempotency key, activity, and output record while the original remains unchanged;
11. request an input with `available_at > information_cutoff` and assert run creation fails closed;
12. attempt retrieval with a wrong digest and assert `IntegrityError`.

This acceptance test is the Phase 0 exit criterion. A fake/in-memory-only execution does not satisfy it.

### Task 12: Final quality and scope audit

Run all commands from a clean checkout with Docker available:

```bash
uv sync --locked --all-groups
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src scripts
uv run lint-imports
uv run python scripts/validate_docs.py
uv run python scripts/check_repository_hygiene.py
docker compose -f deploy/local/compose.yaml up -d --wait
uv run alembic upgrade head
uv run pytest tests/unit tests/contracts tests/property -q
uv run pytest -m integration -q
uv run pytest tests/acceptance/test_phase0_artifact_lineage.py -q
uv run pytest --cov=mesoforge --cov-report=term-missing --cov-fail-under=90 -q
uv run alembic downgrade base
uv run alembic upgrade head
docker compose -f deploy/local/compose.yaml down -v
git status --short
git diff --check
```

`check_repository_hygiene.py` fails on credentials, `.env`, GRIB/index files, NetCDF/Zarr files outside explicitly permitted fixture rules, database/MinIO volumes, caches, and files over the configured fixture size limit.

## 7. Exit-criterion-to-test matrix

| Phase 0 exit requirement | Required automated evidence |
|---|---|
| Architecture decisions and vocabulary are unambiguous | `test_validate_docs.py`; ADR link/status checks; vocabulary term checks |
| Canonical times distinguish issue/reference/valid/availability concepts | `test_time.py`, `test_time_axis.py`, dataset contract tests |
| Grid identity and spatial support are explicit | `test_grid_definition.py`, canonical dataset grid mismatch tests |
| A changed grid cannot hide behind an existing identity | grid ID reuse/revision rejection test and configuration lookup test |
| Variables, vertical levels, dimensions, units, intervals, and quality states are explicit | variable/unit/vertical and dataset contract suites |
| Configuration is validated, immutable, secret-free, and deterministic | configuration unit/property tests and PostgreSQL round trip |
| Artifacts are immutable and checksum verified | artifact contract, repository contract, and MinIO integration tests |
| A source/root artifact can be registered idempotently | source registration unit/integration tests, then acceptance proof step 2 |
| Provenance names exact activities, versions, inputs, outputs, config, code, and environment | activity/lineage contract tests and acceptance graph assertions |
| Source and derived availability obey reviewed bitemporal rules | run eligibility, derived-availability, and cutoff failure tests |
| Derived availability uses the same DB transaction instant as registration | PostgreSQL insert/RETURNING integration assertion with controlled parent/activity times |
| PostgreSQL is authoritative and migrations are reversible | migration and repository integration suites |
| Large bytes remain in object storage | schema inspection test forbids payload columns; acceptance verifies S3 URI and retrieval |
| Repeated work is idempotent and concurrency-safe | duplicate/concurrent integration tests and acceptance repeat |
| Concurrent requests cannot leave a loser activity in progress | advisory-lock integration test asserts one succeeded activity/output and no second activity or `started` residue |
| Package boundaries preserve modular-monolith seams | import-linter contract in quality job |
| Synthetic artifact registers, transforms, and traces end to end | `tests/acceptance/test_phase0_artifact_lineage.py` against PostgreSQL + MinIO |
| Historical bytes and logical replay inputs are pinned | checksum retrieval, run/config/code/environment assertions, lineage JSON reconstruction |
| No later-phase implementation is added | repository hygiene/scope review: only Section 3 production packages and named scaffolding |
| CI, formatting, linting, typing, tests are usable | all Task 12 commands and CI jobs pass from clean checkout |

## 8. Acceptance criteria for Claude's implementation handoff

Implementation is complete only when:

1. every contract and persistence invariant in Sections 4–5 has a failing-then-passing test;
2. Task 12 commands pass with real output, or a genuine external Docker/tool blocker is reported without substituting fakes;
3. the acceptance proof uses real PostgreSQL and MinIO and reconstructs lineage from persisted state;
4. only Phase 0 packages/docs/scaffolding exist;
5. no credentials, generated arrays, caches, service volumes, or large data are tracked;
6. the implementation branch contains focused commits and is pushed but not merged;
7. the handoff lists RED/GREEN evidence, command outputs, changed files, commit SHAs, and any deviation from this plan;
8. any required contract change is returned to Codex for design review rather than silently implemented.

## 9. Risks and mitigations

- **PostgreSQL/object-store atomicity:** no distributed transaction exists. Promote and verify bytes first, then atomically register metadata/lineage; tolerate only unreferenced content-addressed objects, never dangling manifests.
- **Concurrent idempotency:** pre-checks are insufficient. Serialize identical work with a session advisory lock, recheck after acquisition, and retain the partial unique PostgreSQL index as a fail-closed backstop.
- **NetCDF bitwise instability:** promise logical equality and retained historical bytes, not regenerated cross-platform byte identity. Keep serializer behind a protocol.
- **Offset units:** °C conversions are easy to mishandle. Use one Pint registry and property/boundary tests.
- **xarray timezone loss:** control-plane datetimes remain aware; arrays use naive UTC nanoseconds plus a required encoding attribute.
- **Configuration secrets:** snapshots store reference names only. Runtime credential injection must stay outside canonical scientific configuration.
- **Overbuilding future domains:** import/file scope is intentionally narrow; no placeholder packages for later phases.
- **MinIO test flakiness:** health checks and isolated buckets; tests create unique prefixes and clean their own temporary objects.
- **Lineage cycles and ambiguous parents:** first-class named activity edges, a unique producer constraint, and pre-commit ancestor validation.
- **Dependency drift:** bounded direct ranges plus committed uv lock; CI uses `uv sync --locked` and `uv lock --check`.

## 10. Unresolved decisions after Phase 0

No unresolved decision blocks Phase 0 implementation. The following remain deliberately deferred to the phase where evidence exists: operational geography, source models/products, observations and latency truth, PoP/QPF product definitions, regridding methods, normalized Zarr versus NetCDF policy, retention periods, forecast deadlines and correction policy, Prefect versus Dagster, FastAPI/UI, bias promotion thresholds, AI providers/retention/limits, and operational SLOs.
