# MesoForge

MesoForge is intended to be an automatically updating, location-aware forecasting
engine exposed through an API, with issued forecast history and measured performance
against suitable observations. That is the product direction; the current implementation
is described below. Start with [VISION.md](VISION.md) for release boundaries and
[AGENTS.md](AGENTS.md) for working rules.

## Current status

The working localhost endpoint, `GET /forecast?lat=45.8&lon=-93.1`, returns
**synthetic** temperature guidance for hours 1–3 within latitude 45.5–46.0 and
longitude -93.5–-93.0. It reuses prepared files and existing extraction/blending,
keeps the approved 70% HRRR / 30% GFS weights, and reports Kelvin units, source
cycles, valid times, and explicit missingness. Its fixed August 30, 2026 inputs
are invented demonstration data, not a current weather forecast.

Validation: on September 9, **39 API + 148 retained Phase 2 tests passed**, along
with quality checks and a localhost HTTP demonstration; the server was stopped.
The **39 API tests passed again on September 10** during focused review. Full
database/storage acceptance, the full coverage gate, live-provider tests, and
real-guidance use through this endpoint have not been verified. See the detailed
commands and validation record below; passing this slice does not verify the whole app.

The separate Phase 2 pipeline remains an unpublished HRRR/NBM/GFS station baseline
for hours 1–36 at KCBG, KJMR, and KROS, with temperature, dew point, wind, gust,
QPF, PoP, METAR verification, provenance, and retained-input replay. Its defaults
are unchanged. Standalone Phase 1 hours 0–6 generation is retired; shared science,
its required configuration overlay, and historical readers remain.

**Proposed next milestone:** feed one fixed real HRRR/GFS temperature snapshot
through the same endpoint, area, hours, and weights. A separate preparation command
would acquire/decode only the required messages, preserve native grids and source
evidence, and write shared local inputs before serving. Real-data labels and
per-model projection handling are required; real files cannot simply replace the
synthetic files today. Implementation and acquisition require a separate approval.

There is no operational forecast API, shared-cache job system, or registered-coordinate
history service. RRFS, precipitation type, learned weights, AI adjustments, and
publication remain disabled or absent. Existing `_v2` names describe Phase 2 contracts.

Local Codex development has replaced the paused Hermes development pipeline. The
[V2 architecture RFC](docs/rfcs/mesoforge-v2-architecture.md) is proposed design input,
not approval to implement its entire release plan.

## Existing forecast path

1. [The live runner](scripts/run_phase2_live.py), `main()`, builds `Phase2Request`
   from explicit UTC times. `_load_configuration()` merges
   [base](configs/base.yaml), [Phase 1](configs/phase1-grasston.yaml), then
   [Phase 2](configs/phase2-grasston.yaml) configuration and registers a snapshot.
2. [Phase2Coordinator.run()](src/mesoforge/application/phase2.py) composes the
   [production adapters](src/mesoforge/application/phase2_production.py).
   `Phase2ProductionProvider.discover()` tries eligible cycles and acquires selected
   GRIB messages; acquisition is not merely metadata discovery.
3. Model-specific [normalizers](src/mesoforge/guidance/normalization_v2.py) decode
   guidance, standardize units/wind/interval semantics, and retain native-grid bbox
   subsets plus halo. `align_station_to_model()` in
   [station_frame.py](src/mesoforge/alignment/station_frame.py) extracts values at
   exact valid times with bilinear interpolation.
4. Availability and configured fallback rows feed deterministic blend operators.
   [Baseline assembly](src/mesoforge/forecasting/baseline_v2.py) fixes three stations
   and 36 horizons. PostgreSQL stores metadata and S3-compatible storage holds
   immutable artifacts. The coordinator then acquires observations and verifies;
   the runner reads stored results into JSON, CSV, and Markdown exports.

## Setup and commands

Run commands from the repository root. **The general setup, service, and full-suite
commands below remain unverified on this local Windows checkout.** The focused
Python 3.12 checks and synthetic demonstration have separate execution results below.

The project requires Python **3.12** and `uv`; see [pyproject.toml](pyproject.toml)
and [uv.lock](uv.lock). Dependencies include NumPy/xarray, Pint, pyproj,
ecCodes/cfgrib/Herbie, NetCDF/HDF5, PyArrow, Pydantic, SQLAlchemy/psycopg, and boto3.
Development dependencies include pytest, Hypothesis, Ruff, mypy, and pgserver.

```text
uv sync --locked --all-groups
```

This is the dependency setup used by [CI](.github/workflows/ci.yml).
The [Makefile](Makefile) supplies convenience targets; `make sync` uses
`uv sync --all-groups` without `--locked`.

| Check | Existing command | Requirements |
| --- | --- | --- |
| Fast offline tests | `uv run pytest tests/unit tests/contracts tests/property -q` | Development dependencies; no services/providers |
| Scientific tests | `uv run pytest -m scientific tests/unit tests/property -q` | Offline numerical checks |
| Documentation | `uv run python scripts/validate_docs.py` | Existing documentation validator |
| Quality | `make quality` | Lock, formatting, lint, types, imports, docs, hygiene |
| Integration | `uv run pytest -m integration tests/integration -q` | Real PostgreSQL and/or MinIO |
| Phase 2 acceptance | `make phase2-acceptance` | Heavier fixture-GRIB workflow, real PostgreSQL/MinIO, replay/failure scenarios |
| Live canaries | `uv run pytest -m live tests/live -q` | Explicit opt-in and provider cycles |

`make test` runs the fast offline suite. `make phase2-offline` combines quality,
offline tests, scientific checks, and the default live-test skip check. `make test-all`
and `make coverage` include service-backed tests and are not fast offline commands.

For integration/acceptance, follow [local development](docs/operations/local-development.md):

```text
docker compose -f deploy/local/compose.yaml up -d --wait
uv run alembic upgrade head
```

Configure the database and test S3 environment variables before migration/testing.
`MESOFORGE_TEST_DATABASE_DSN` selects the test database; without it,
[tests/conftest.py](tests/conftest.py) starts ephemeral PostgreSQL through pgserver.
MinIO must still be available. These fixtures reset schemas: use a dedicated test
database. Live canaries require `MESOFORGE_LIVE_TESTS=1` and explicit
`MESOFORGE_LIVE_HRRR_CYCLE`, `MESOFORGE_LIVE_NBM_CYCLE`, and
`MESOFORGE_LIVE_GFS_CYCLE` values in `YYYYMMDDTHH` format. They check provider contract
shape, not forecast skill or operational availability.

### Run the existing baseline

The operational entry point is `uv run python scripts/run_phase2_live.py` with
required arguments `--target-reference-time`, `--forecast-issue-time`,
`--information-cutoff`, `--verification-cutoff`, and `--output-dir`.
Supply explicit ISO UTC timestamps and a writable output directory; the runner's
module docstring contains a historical invocation example. Provider retention and
cutoffs determine whether those historical inputs remain available.

It requires migrated PostgreSQL and `MESOFORGE_DATABASE_DSN`,
`MESOFORGE_S3_ENDPOINT`, `MESOFORGE_S3_BUCKET`, `MESOFORGE_S3_ACCESS_KEY`, and
`MESOFORGE_S3_SECRET_KEY`. It downloads live guidance and observations and writes
artifacts. `--replay` adds a zero-network replay **after the live run**; it is not a
standalone offline CLI mode. Keep credentials and generated model/output files out
of Git.

### Windows and validation status

CI runs Ubuntu. Unix `export`, inline environment assignments, shell continuations,
and the Linux MinIO binary example in the operations guide need adaptation on Windows.
Use PowerShell `$env:NAME = 'value'` assignments and direct `uv` commands when Make
or a POSIX shell is unavailable. The lockfile includes Windows pgserver wheels;
that does not establish that the whole stack works on this machine. WSL/Linux offers
an environment closer to CI.

Checks passed during this consolidation: `python -B scripts/validate_docs.py`,
`python -B scripts/check_repository_hygiene.py`, and `git diff --check`.
The existing Python check scripts used the available Python 3.14.4 interpreter,
not the project's Python 3.12 runtime; `uv` was not available on PATH. Product setup,
provider access, services, and product tests were not exercised. Documentation checks
do not establish runtime compatibility.

## Approved localhost demonstration

[The HTTP entry point](src/mesoforge/api.py) accepts a coordinate near Grasston and
returns three hourly temperatures from [prepared synthetic guidance](src/mesoforge/application/point_forecast.py).
Every response identifies itself as synthetic demonstration data, not a current
weather forecast. The supported rectangle includes latitude **45.5–46.0** and
longitude **-93.5–-93.0**; unsupported or invalid coordinates return HTTP 422.

With the locked dependencies installed, the portable start command is:

```text
uv run --locked python -m mesoforge.api
```

The module was exercised directly with the isolated Python 3.12 environment and
`PYTHONPATH=src` on Windows; the `uv run` wrapper above has not been executed.
Open <http://127.0.0.1:8765/forecast?lat=45.8&lon=-93.1>. Stop with Ctrl+C.
The launcher binds only to `127.0.0.1`; `--port` changes the port, not the host.

At startup, an empty directory receives tiny `HRRR.nc` and `GFS.nc` files; existing
files are never overwritten. The default directory is
`mesoforge-synthetic-temperature-demo` under the operating system's temporary
directory; `--data-dir PATH` selects another directory. Both files are loaded and
closed before requests begin, and reused across coordinates. Restart to load changed
inputs. Their invented latitude/longitude grids are not real HRRR/GFS native grids.

The inputs fix the target reference at **2026-08-30 12:00 UTC**, HRRR's source cycle
at 12:00, and GFS's at 06:00. Hours 1–3 are valid at 13:00, 14:00, and 15:00 UTC.
Existing `align_station_to_model()` and `blend_scalar()` produce **286.14, 287.14,
and 288.14 K** at the example coordinate. Responses include cycles, source leads,
valid times, units, fixed weights, and explicit missing reasons.

Demo weights are **70% HRRR / 30% GFS**, matching
`scalar-vector.hg.h01-h18` in [the existing configuration](configs/phase2-grasston.yaml).
That scalar/vector row applies to temperature and hours 1–3. Phase 2 defaults are
unchanged. If either required model or hour is missing, that hour is null; weights
are never redistributed. Invalid prepared-file units or time metadata prevent startup.

This demonstration does not acquire real guidance or use databases. A real prepared
guidance API remains later work requiring its own bounded approval; jobs, registration,
history, verification, evaluation, and AI are outside this increment.

### Demonstration validation

On 2026-09-09, the isolated locked Python 3.12 environment passed **39 focused API
tests** in [test_forecast_api.py](tests/unit/test_forecast_api.py) and the **148-test
recorded Phase 2 selection** in [CLEANUP.md](CLEANUP.md), run together: **187 passed**.
Checks cover independent expected temperatures, boundaries, units/times/source cycles,
missing files/hours, nonfinite extraction, labeled errors, repeated requests, and reuse
of the same files for different coordinates. No existing scientific tests were changed.

Ruff lint/format, mypy, all nine import contracts, lock validation, documentation and
repository hygiene checks, and `git diff --check` passed. A real localhost HTTP request
returned the temperatures above; repeated requests matched, another coordinate worked,
and unsupported coordinates returned 422. The demonstration server was stopped afterward.
Full database/storage acceptance and live-provider tests were not run.

The only dependency additions support HTTP serving/testing. Existing locked package
versions are unchanged. Starlette's test client needs `httpx2` and an AnyIO version
below 4.15 to avoid deprecated aliases under the repository's warnings-as-errors rule;
the initial collection errors were resolved through those dependency choices.

## References

- [VISION.md](VISION.md): product intent, release boundary, and proposal status.
- [AGENTS.md](AGENTS.md): working rules and document responsibilities.
- [V2 architecture RFC](docs/rfcs/mesoforge-v2-architecture.md): detailed proposed design.
- [Phase 0](docs/data-contracts/phase-0.md), [Phase 1](docs/data-contracts/phase-1.md),
  [Phase 2](docs/data-contracts/phase-2.md), and [vocabulary](docs/data-contracts/vocabulary.md):
  technical references for the existing implementation. The Phase 1 lifecycle
  description is historical; its shared scientific/schema references remain applicable.
- [Modular monolith ADR](docs/decisions/0001-python-modular-monolith.md) and
  [storage ADR](docs/decisions/0004-postgresql-and-s3-storage.md): accepted foundation decisions.
- [Archive index](docs/archive/README.md): completed implementation plans, retained
  historical references, and donor identities. Historical plans are not work orders.
