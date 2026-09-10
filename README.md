# MesoForge

MesoForge is intended to be an automatically updating, location-aware forecasting
engine exposed through an API, with issued forecast history and measured performance
against suitable observations. That is the product direction; the current implementation
is described below. Start with [VISION.md](VISION.md) for release boundaries and
[AGENTS.md](AGENTS.md) for working rules.

## Current status

The localhost endpoint, `GET /forecast?lat=45.8&lon=-93.1`, now returns temperature
from **real prepared HRRR/GFS guidance** for hours 1–3 within latitude 45.5–46.0
and longitude -93.5–-93.0. It retains the approved 70% HRRR / 30% GFS demonstration
weights and reports Kelvin units, source cycles/leads, valid times, checksums,
and explicit missingness. Preparation happens before serving. The demonstrated
September 10, 2026 snapshot is fixed historical guidance, not a current live forecast.
Starting without `--data-dir` still selects the clearly labeled synthetic example.

Validation on September 10: **229 tests passed** (78 API/preparation, 148 retained
Phase 2, and 3 existing acquisition tests), along with quality checks. One bounded
acquisition and real localhost responses succeeded; independent calculations from
the raw messages agreed. The server was stopped. Full database/storage acceptance,
the full coverage gate, and the live-provider canary suite were not run. This verifies
the small slice, not the entire application or forecast skill.

The separate Phase 2 pipeline remains an unpublished HRRR/NBM/GFS station baseline
for hours 1–36 at KCBG, KJMR, and KROS, with temperature, dew point, wind, gust,
QPF, PoP, METAR verification, provenance, and retained-input replay. Its defaults
are unchanged. Standalone Phase 1 hours 0–6 generation is retired; shared science,
its required configuration overlay, and historical readers remain.

**Proposed next milestone:** rebuild the same prepared temperature inputs from the
retained raw messages without downloading them again, and demonstrate matching
forecast values and source evidence. This would make local replay practical;
it does not add fields, models, scheduling, or a broader cache platform.

There is no operational forecast API, shared-cache job system, or registered-coordinate
history service. RRFS, precipitation type, learned weights, AI adjustments, and
publication remain disabled or absent. Existing `_v2` names describe Phase 2 contracts.

Local Codex development has replaced the paused Hermes development pipeline. The
[V2 architecture RFC](docs/rfcs/mesoforge-v2-architecture.md) is proposed design input,
not approval to implement its entire release plan.

## Existing forecast path

The long-term direction is a broader blend of HRRR, RAP, NAM 3 km, NAM, GFS,
RRFS / REFS, NBM, and appropriate GEFS, ECMWF, Canadian, and other guidance.
Only HRRR/NBM/GFS are implemented in Phase 2 today. NAM/NAM 3 km are legacy
transition candidates: the September 9 NWS notices schedule retirement and the
RRFS/REFS transition for October 14, 2026, subject to weather delay. See
[VISION.md](VISION.md#long-term-model-direction) for the verified official notices
and planned-versus-current support. Retain raw data actually acquired even when
the API uses only a subset; this does not expand the authorized downloads.

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
Python 3.12 checks and prepared-guidance demonstration have separate execution results below.

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

During the September 9 documentation consolidation, checks passed:
`python -B scripts/validate_docs.py`,
`python -B scripts/check_repository_hygiene.py`, and `git diff --check`.
The existing Python check scripts used the available Python 3.14.4 interpreter,
not the project's Python 3.12 runtime; `uv` was not available on PATH. Product setup,
provider access, services, and product tests were not exercised at that checkpoint.
The later Python 3.12 and real-input results below supersede that limited runtime status.

## Approved localhost demonstration

[The HTTP entry point](src/mesoforge/api.py) accepts a coordinate near Grasston and
returns three hourly temperatures through [prepared point extraction](src/mesoforge/application/point_forecast.py).
Every JSON response identifies its inputs as real prepared guidance or synthetic
demonstration data. Neither mode claims to be a current live forecast. The supported
rectangle includes latitude **45.5–46.0** and longitude **-93.5–-93.0**;
unsupported or invalid coordinates return HTTP 422.

With the locked dependencies installed, the portable start command is:

```text
uv run --locked python -m mesoforge.api --data-dir PATH_TO_PREPARED_SNAPSHOT
```

The module was exercised directly with the isolated Python 3.12 environment and
`PYTHONPATH=src` on Windows; the `uv run` wrapper above has not been executed.
Open <http://127.0.0.1:8765/forecast?lat=45.8&lon=-93.1>. Stop with Ctrl+C.
The launcher binds only to `127.0.0.1`; `--port` changes the port, not the host.

An explicit `--data-dir` requires existing prepared files; it never generates a
synthetic substitute. Real snapshots contain `HRRR.nc`, `GFS.nc`, `manifest.json`,
and retained source messages/indexes under `raw/`. Startup verifies their hashes,
units, cycles, valid times, and projection metadata, then loads and closes the
prepared datasets. Requests reuse these arrays across coordinates, with no file or
provider I/O. Restart to load different inputs.

The snapshot exercised on Windows is outside Git at
`%LOCALAPPDATA%\MesoForge\prepared\20260910T06Z-hrrr06-gfs00`.
This exact PowerShell command starts it from the repository root using the existing
isolated environment; it does not download anything:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe" -B -m mesoforge.api --data-dir "$env:LOCALAPPDATA\MesoForge\prepared\20260910T06Z-hrrr06-gfs00"
```

The real inputs use target reference **2026-09-10 06:00 UTC**, HRRR's **06Z** cycle
at leads **1/2/3**, and GFS's **00Z** cycle at leads **7/8/9**. Both models are valid
at **07Z/08Z/09Z**. Existing `align_station_to_model()` and `blend_scalar()` produced
**285.29782837432555, 284.75571509307554, and 284.2734065226455 K** at the example
coordinate. All three hours had empty missing-reason lists. Responses include source
URLs and raw/prepared checksums, plus the manifest hash.

Omitting `--data-dir` retains the earlier synthetic example. Its tiny invented grids
live in `mesoforge-synthetic-temperature-demo` under the system temporary directory;
existing files are not overwritten. Its August 30, 2026 inputs still produce
**286.14, 287.14, and 288.14 K** at the example coordinate, labeled synthetic.

Demo weights are **70% HRRR / 30% GFS**, matching
`scalar-vector.hg.h01-h18` in [the existing configuration](configs/phase2-grasston.yaml).
That scalar/vector row applies to temperature and hours 1–3. Phase 2 defaults are
unchanged. If either required model or hour is missing, that hour is null; weights
are never redistributed. Invalid prepared-file units or time metadata prevent startup.

### Prepare real inputs before serving

[The preparation module](src/mesoforge/application/prepared_temperature.py) reuses
the HRRR/GFS acquisition and strict GRIB decoders plus existing projection/subsetting
functions. It selects only 2 m temperature, retains each complete acquired message
and inventory, and writes small native-grid subsets with a one-cell halo. HRRR stays
on its Lambert grid; GFS retains its geographic grid and north-to-south value order.
The six-field Phase 2 normalizers and Phase 2 defaults are unchanged.

The following module invocation was executed with the isolated Python interpreter
and `PYTHONPATH=src`. It performs downloads and requires an empty output directory;
do not rerun it to start the already prepared server. `OUTPUT_DIR` here represents
the external snapshot path above; the `uv run` wrapper itself remains unverified.

```text
uv run --locked python -m mesoforge.application.prepared_temperature --output-dir OUTPUT_DIR --target-reference-time 2026-09-10T06:00:00Z --hrrr-cycle 2026-09-10T06:00:00Z --gfs-cycle 2026-09-10T00:00:00Z
```

The one authorized acquisition retrieved **5,342,777 HTTP body bytes**: **5,190,418**
bytes in six raw temperature messages and **152,359** bytes in six inventories.
HRRR came from NOAA's AWS archive; GFS from its Google Cloud archive. The streaming
transport caps each inventory at 1 MiB, each selected message at 16 MiB, and the run
at 64 MiB, rejecting responses that ignore byte ranges. No other fields were acquired.

The manifest preserves URLs, byte ranges, retrieval and provider timestamps, cycles,
leads, valid times, hashes, configuration/code identity, and decoder versions.
Provider availability and retrieval time are distinct; this manual historical
demonstration does not apply an operational issuance cutoff. Raw messages remain
intact outside Git even though the prepared views use only the small region. This
does not yet provide an offline raw-to-prepared replay command.

No databases are needed. Jobs, registration, history, verification, evaluation, and
AI remain outside this increment.

### Demonstration validation

On 2026-09-10, the isolated locked Python 3.12 environment passed **229 tests** in
one run: the **148-test recorded Phase 2 selection** in [CLEANUP.md](CLEANUP.md),
**3 existing acquisition tests**, and **78 API/preparation tests**. The latter consist
of the existing [synthetic API tests](tests/unit/test_forecast_api.py) and new
[GRIB preparation](tests/unit/application/test_prepared_temperature.py) and
[real-input API](tests/unit/test_real_forecast_api.py) tests. To select the four
affected modules (append the recorded selection to reproduce the combined run):

```text
python -B -m pytest tests/unit/test_forecast_api.py tests/unit/application/test_prepared_temperature.py tests/unit/test_real_forecast_api.py tests/unit/guidance/test_acquisition_v2.py -q -s -p no:cacheprovider
```

These offline tests generate GRIB messages with known values and assert independent
interpolation/blend results, native-grid gradients, actual valid-time alignment,
units, provenance checksums, missing models/hours, unchanged weights, labeled errors,
repeated requests without I/O, and acquisition limits. No retained scientific
assertions were weakened. Additional read-only calculations from the downloaded raw
messages matched all three endpoint results at two coordinates. The calculation
accounted for the existing cfgrib decoder's float32 precision and agreed within 1e-8 K.

Ruff lint/format, mypy, all nine import contracts, lock validation, documentation and
repository hygiene checks, and `git diff --check` passed. Actual localhost requests
returned the real temperatures above; repeated responses matched, another coordinate
worked, and unsupported coordinates returned 422. The listener was confirmed to be
`127.0.0.1` only, then stopped. Full database/storage acceptance, the full coverage
gate, and the live-provider canary suite were not run. The single acquisition is
evidence for this fixed pair, not operational provider reliability or forecast skill.
No dependencies or lockfile entries changed for the real-input milestone.

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
