# 0001: Python Modular Monolith

Status: Accepted

## Context

MesoForge Phase 0 must establish a reproducible foundation for scientific
data contracts, provenance, and storage before any guidance ingestion,
forecasting, or AI-adjustment logic exists. The [reviewed v1 architecture](../architecture/v1.md)
calls for domain logic that is independently testable and infrastructure
that can be swapped without touching scientific code. A distributed
services architecture would be premature: there is no operational load,
team topology, or deployment requirement that justifies the cost of
network boundaries between domain packages this early.

## Decision

Implement MesoForge Phase 0 as a single Python 3.12 package
(`src/mesoforge`) organized into internally cohesive, externally decoupled
subpackages: `common`, `contracts`, `catalog`, `provenance`, `storage`, and
`application`. Dependency direction is fixed and enforced with
`import-linter`:

```text
application -> provenance, catalog, contracts, storage.interfaces, common
provenance  -> contracts, storage.interfaces, common
catalog     -> contracts, common
contracts   -> common
storage.s3/postgres -> storage.interfaces, contracts, common
common      -> Python standard library only
```

`contracts`, `catalog`, and `provenance` never import infrastructure
libraries (boto3, SQLAlchemy, psycopg, Alembic) or orchestration/CLI/API
modules. `storage.interfaces` contains only `Protocol` declarations and
contract types. Concrete storage adapters (`storage.postgres`,
`storage.s3`, `storage.netcdf`) implement those protocols and never embed
scientific transformations. `application` composes protocols into
transactional services; it owns transaction ordering, not meteorological
algorithms.

Python 3.12 is pinned exactly (`requires-python = ">=3.12,<3.13"`,
`.python-version` = `3.12`) to keep the scientific dependency stack
(NumPy, xarray, Pint, pyproj) on a single, well-tested minor line rather
than tracking the newest interpreter. `uv` is the sole environment and
dependency manager, with a committed `uv.lock` for reproducible
resolution across machines and CI.

## Consequences

- Domain packages (`contracts`, `catalog`, `provenance`) can be unit
  tested with no database, object store, or network dependency.
- Infrastructure can be replaced (e.g., a different object store) by
  writing a new adapter against `storage.interfaces` without touching
  `contracts` or `catalog`.
- Package boundaries are enforced mechanically in CI via `import-linter`,
  not left to code review discipline alone.
- A future move to separate services or a workflow orchestrator (Prefect,
  Dagster) is deferred; the modular monolith's internal boundaries are
  designed to make that split mechanical if it becomes necessary later.
- No placeholder packages exist yet for later-phase domains (guidance
  ingestion, forecasting, verification, bias learning, AI); adding them
  before Phase 0 needs them is explicitly out of scope.

## Deferred decisions

- Whether/when to split into separate deployable services.
- Workflow orchestration engine (Prefect vs. Dagster) — no Phase 0
  interface depends on this choice.
- Public API layer (FastAPI or otherwise) and UI.
