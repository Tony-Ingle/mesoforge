# MesoForge

MesoForge is intended to be an automatically updating, location-aware forecasting
engine exposed through an API, with issued forecast history and measured performance
against suitable observations. That is the product direction; the current implementation
is described below. Start with [VISION.md](VISION.md) for release boundaries and
[AGENTS.md](AGENTS.md) for working rules.

## Current status

The localhost endpoint, `GET /forecast?lat=45.8&lon=-93.1`, now returns temperature
from **real prepared HRRR/GFS guidance** for hours 1–36 within latitude 45.5–46.0
and longitude -93.5–-93.0. It retains the approved 70% HRRR / 30% GFS demonstration
weights throughout the window; these are demonstration weights, not optimized
weights. It reports Kelvin units, source cycles/leads, valid times, checksums,
and explicit missingness. Preparation accepts explicit source cycles and a target
reference time before serving. The demonstrated September 10, 2026 snapshots are
fixed historical guidance, not current live forecasts. Retained raw messages can
now rebuild a dataset offline with the same preparation command's `--from-raw` mode.
Starting without `--data-dir` still selects the clearly labeled synthetic example.

Validation on September 10: **262 tests passed** (111 API/preparation, 148 retained
Phase 2, and 3 existing acquisition tests), along with quality checks. One bounded
36-hour acquisition supplied all **36/36 hourly API results, with no missing hours**.
An offline rebuild, with network calls blocked, reproduced exact values and preserved
the source snapshot. Independent calculations from the raw messages matched all 36
API temperatures. The server was stopped. Full database/storage acceptance,
the full coverage gate, and the live-provider canary suite were not run. This verifies
the small slice, not the entire application or forecast skill.

The separate Phase 2 pipeline remains an unpublished HRRR/NBM/GFS station baseline
for hours 1–36 at KCBG, KJMR, and KROS, with temperature, dew point, wind, gust,
QPF, PoP, METAR verification, provenance, and retained-input replay. Its defaults
are unchanged. Standalone Phase 1 hours 0–6 generation is retired; shared science,
its required configuration overlay, and historical readers remain.

The local batch command now reads a latitude/longitude collection and returns the
same 36-hour temperature forecasts from one shared prepared dataset. A location
error does not stop later coordinates. See [the batch command](#local-coordinate-batch).
Proposed next milestone: save immutable issued-forecast versions from explicit
batch runs, while keeping one-off API requests free of history side effects.

Future direction: configure locations using latitude/longitude only, with geographic
context and suitable observation sources derived internally. The intended VPS workflow
can process a coordinate collection through GitHub Actions while sharing prepared
guidance; it is not implemented. See [VISION.md](VISION.md#intended-coordinate-driven-operation).

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
returns 36 hourly temperatures through [prepared point extraction](src/mesoforge/application/point_forecast.py).
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

New real snapshots declare `target_horizon_hours` as hours 1–36. The API always
returns every declared hour, including explicit nulls where either model is missing.
Older three-hour snapshots without that declaration still serve and rebuild their
original three hours. The default synthetic example also remains three hours.

The latest snapshot exercised on Windows is outside Git at
`%LOCALAPPDATA%\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36`.
This exact PowerShell command starts it from the repository root using the existing
isolated environment; it does not download anything:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe" -B -m mesoforge.api --data-dir "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
```

These real inputs use target reference **2026-09-10 12:00 UTC**, HRRR's **12Z** cycle
at leads **1–36**, and GFS's **06Z** cycle at leads **7–42**. The 36 matching valid
times run hourly from **2026-09-10 13:00 UTC through 2026-09-12 00:00 UTC**.
Existing `align_station_to_model()` and `blend_scalar()` produced all 36 values at
the example coordinate, with empty missing-reason lists throughout. Example values
from the actual HTTP response, rounded to six decimals:

| Horizon | Valid time UTC | Temperature, K |
| --- | --- | --- |
| 1 | 2026-09-10 13:00 | 283.705085 |
| 18 | 2026-09-11 06:00 | 289.244578 |
| 19 | 2026-09-11 07:00 | 289.120690 |
| 36 | 2026-09-12 00:00 | 298.287692 |

Each response includes all 36 hourly rows, source URLs and raw/prepared checksums,
plus the manifest hash. The full captured response is retained locally at
`%LOCALAPPDATA%\MesoForge\baselines\20260910-temperature-36h\actual-response.json`.

Omitting `--data-dir` retains the earlier synthetic example. Its tiny invented grids
live in `mesoforge-synthetic-temperature-demo` under the system temporary directory;
existing files are not overwritten. Its August 30, 2026 inputs still produce
**286.14, 287.14, and 288.14 K** at the example coordinate, labeled synthetic.

Demo weights are **70% HRRR / 30% GFS throughout hours 1–36**, explicitly approved
for this demonstration and **not optimized weights**. They match
`scalar-vector.hg.h01-h18` in [the existing configuration](configs/phase2-grasston.yaml)
for hours 1–18. Phase 2's `scalar-vector.hg.h19-h36` row remains **60/40**;
the endpoint's approved demonstration weights are separate from that policy.
If either required model or hour is missing, that hour is null; weights
are never redistributed. Invalid prepared-file units or time metadata prevent startup.

### Local coordinate batch

[The batch command](src/mesoforge/application/batch_forecast.py) reads one JSON config
and one existing **36-hour** prepared snapshot. Save this as `locations.json`:

```json
{
  "locations": [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 45.9, "lon": -93.0}
  ]
}
```

Each location contains only numeric `lat` and `lon`; no station IDs, counties,
bounding boxes, or other geographic configuration is needed. Run from the repository
root with the locked dependencies installed:

```text
uv run --locked python -m mesoforge.application.batch_forecast --config locations.json --data-dir PATH_TO_PREPARED_SNAPSHOT
```

The Python module was exercised with the isolated Windows environment below;
the portable `uv run` wrapper has not been executed. The command writes JSON to
standard output: `results` preserves input order and each entry contains its
zero-based `index`, input `location`, and `status`. An `ok` entry has the complete
existing `forecast`, including all 36 hours, units, source cycles/leads, valid times,
weights, missing reasons, source URLs, and checksums. An `error` entry instead has
an error `code` and `message`. Missing hourly guidance remains null with reasons
inside a successful forecast response; it does not silently change the blend.
An unrepresentably large JSON number is retained as text in its location error.

Exit code **0** means every location succeeded; **1** means at least one location
failed, after all locations were processed. **2** reports an unusable config or
dataset on standard error. The command rejects older three-hour snapshots. It loads
and verifies guidance once, then reuses the same arrays for every coordinate without
network calls or per-location preparation. It starts no HTTP server and stores no
registered locations or issued history. Models, supported area, temperature scope,
and fixed 70/30 demonstration weights are unchanged.

Actual demonstration on September 10, using the retained HRRR 12Z / GFS 06Z snapshot:
an unsupported coordinate was inserted between the two supported points above.
These commands were executed; the example files are already present outside Git:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$batchDemo = "$env:LOCALAPPDATA\MesoForge\baselines\20260910-coordinate-batch"
$snapshot = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
& $python -B -m mesoforge.application.batch_forecast --config "$batchDemo\locations.json" --data-dir $snapshot | Set-Content -Encoding utf8 "$batchDemo\actual-batch.json"
$LASTEXITCODE # 1: the deliberately unsupported location failed; both others completed.
```

| Input order | Latitude, longitude | Actual result | Hour 1 / hour 36, K (rounded) |
| --- | --- | --- | --- |
| 0 | 45.8, -93.1 | 36 hours; none missing | 283.705085 / 298.287692 |
| 1 | 44.98, -93.27 | `unsupported_coordinate` | No forecast |
| 2 | 45.9, -93.0 | 36 hours; none missing | 283.629142 / 298.173662 |

The full output is `actual-batch.json` in the external directory above. A repeated
run with network calls blocked produced identical results, loaded the dataset once
(two prepared-file opens), and left the source snapshot unchanged. The first
coordinate's full forecast exactly matches the earlier captured API response.
These are fixed historical model inputs, not a current live forecast.

Batch validation: **27 focused batch tests passed**, plus **262 existing API,
preparation/acquisition, and retained Phase 2 tests**. The new tests independently
calculate expected temperatures and check all 36 hours, provenance, continued
processing after bad coordinates/calculation failures, shared loading without I/O,
repeatable CLI output, and exit codes:

```text
python -B -m pytest tests/unit/application/test_batch_forecast.py -q -p no:cacheprovider
```

Ruff lint/format, mypy, nine import contracts, offline lock validation, documentation,
hygiene, and whitespace checks passed. Full database/storage acceptance, the full
coverage gate, and live-provider canaries were not run. Dependencies were unchanged;
no model data was downloaded and no services were started for this batch milestone.

### Prepare real inputs before serving

[The preparation module](src/mesoforge/application/prepared_temperature.py) reuses
the HRRR/GFS acquisition and strict GRIB decoders plus existing projection/subsetting
functions. It selects only 2 m temperature, retains each complete acquired message
and inventory, and writes small native-grid subsets with a one-cell halo. HRRR stays
on its Lambert grid; GFS retains its geographic grid and north-to-south value order.
The six-field Phase 2 normalizers and Phase 2 defaults are unchanged.

The existing acquisition command already supports different fixed cycles without
code changes. Supply explicit UTC whole-hour timestamps ending in `Z`. The current
source contract permits cycles at **00/06/12/18Z**, at or before the target reference,
with all selected model leads at most **48 hours**. Each source cycle must therefore
be no more than **12 hours** older than the target for this 36-hour window.
For each requested valid time,
the model lead is `target reference + horizon - source cycle`; matching model lead
numbers is not required. The command does not search for the latest available cycle.
Provider availability still determines whether a chosen pair can be acquired.

The command now downloads **72 selected temperature messages**, one per model per
hour, so run it only when acquiring a new dataset. Choose
a new, empty directory outside Git; occupied directories are refused. The example
directory is already populated on this machine, so use the startup command above
to serve it again. The equivalent portable command is below; its `uv run` wrapper
has not been executed.

```text
uv run --locked python -m mesoforge.application.prepared_temperature --output-dir OUTPUT_DIR --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
```

Equivalent PowerShell preparation and startup with the installed isolated environment:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$snapshot = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
& $python -B -m mesoforge.application.prepared_temperature --output-dir $snapshot --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
# Run startup only after preparation succeeds; stop the server with Ctrl+C.
& $python -B -m mesoforge.api --data-dir $snapshot
```

The one acquisition for this expansion retrieved **65,261,114 HTTP body bytes**
(about **65.26 MB / 62.24 MiB**): **63,399,300** bytes in 72 raw temperature messages
and **1,861,814** bytes in 72 inventories. No requested hours were missing.
The earlier three-hour snapshots remain unchanged in `20260910T06Z-hrrr06-gfs00`
and `20260910T12Z-hrrr12-gfs06` under the same external `prepared` directory.
HRRR came from NOAA's AWS archive; GFS from its Google Cloud archive. The streaming
transport caps each inventory at 1 MiB, each selected message at 16 MiB, and the run
at **128 MiB** for the expanded 72-message run, rejecting responses that ignore
byte ranges. No other fields are selected. If acquisition or decoding fails, the
command stops with an error and retains acquired raw files; it does not publish a
completed manifest or invent replacements for unavailable inputs.

The manifest preserves URLs, byte ranges, retrieval and provider timestamps, cycles,
leads, valid times, hashes, configuration/code identity, and decoder versions.
Provider availability and retrieval time are distinct; this manual historical
demonstration does not apply an operational issuance cutoff. Raw messages remain
intact outside Git even though the prepared views use only the small region.

### Rebuild from retained raw messages without downloads

Use the same preparation module with `--from-raw SOURCE_DIR` and a different,
empty `--output-dir`. This mode takes cycles and the target time from the retained
manifest, including its 36-hour declaration (or the earlier three-hour window);
it rejects additional cycle/time arguments. It checks the original source
configuration, raw/index byte counts and checksums, and source times, then reuses the
same decoders, projection, subsetting, and prepared-file writer. It does not create
an HTTP transport. The source `HRRR.nc` and `GFS.nc` files are not needed.

Portable commands (the `uv run` wrapper remains unverified):

```text
uv run --locked python -m mesoforge.application.prepared_temperature --from-raw SOURCE_DIR --output-dir REBUILT_DIR
uv run --locked python -m mesoforge.api --data-dir REBUILT_DIR
```

The equivalent rebuild was exercised through the module's CLI with network calls
blocked, using these arguments and the isolated interpreter:

```powershell
$rebuilt = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36-rebuilt"
& $python -B -m mesoforge.application.prepared_temperature --from-raw $snapshot --output-dir $rebuilt
& $python -B -m mesoforge.api --data-dir $rebuilt
```

Both snapshots already exist locally. To serve either again, run only its API startup
command. The rebuild copies raw evidence and the original manifest without changing
the source directory. It records **zero downloaded bytes**, a new preparation time
and code identity, and the original manifest's checksum. It preserves original
acquisition/retrieval timestamps. Prepared arrays, units, coordinates, and forecast
values reproduced exactly in the locked environment; newly serialized files and
manifest identities need not be byte-identical. Replay with changed dependencies
or scientific code has not been established.

No databases are needed. Jobs, registration, history, verification, evaluation, and
AI remain outside this increment.

### Demonstration validation

On 2026-09-10, the isolated locked Python 3.12 environment passed **262 tests** in
two focused runs: the **148-test recorded Phase 2 selection** in [CLEANUP.md](CLEANUP.md)
plus **3 existing acquisition tests**, and **111 API/preparation tests**. The latter consist
of the existing [synthetic API tests](tests/unit/test_forecast_api.py) and new
[GRIB preparation](tests/unit/application/test_prepared_temperature.py) and
[real-input API](tests/unit/test_real_forecast_api.py) tests. To select the four
affected modules (append the recorded selection to run all 262 together):

```text
python -B -m pytest tests/unit/test_forecast_api.py tests/unit/application/test_prepared_temperature.py tests/unit/test_real_forecast_api.py tests/unit/guidance/test_acquisition_v2.py -q -s -p no:cacheprovider
```

These offline tests generate GRIB messages with known values and assert independent
interpolation/blend results, native-grid gradients, actual valid-time alignment,
units, provenance checksums, missing models/hours, unchanged weights, labeled errors,
repeated requests without I/O, and acquisition limits. No retained scientific
assertions were weakened. Independent calculations directly from all 72 downloaded
raw messages matched the 36 actual API temperatures at the example coordinate within
1e-8 K, accounting for the existing cfgrib decoder's float32 precision.

The 15 added extension cases check the default 72-message acquisition, source-age
limits, all 36 independently expected values, UTC date rollover, unchanged Phase 2
late-hour weights, missing models or hours 19/36, repeated requests without I/O,
and exact offline rebuilding. The earlier three-hour tests remain, including old
manifests without horizon metadata and rebuilding without the original NetCDF files.
The actual expanded snapshot was rebuilt with network calls blocked: both native
datasets were identical and every hourly temperature reproduced exactly at two
coordinates. The original snapshot remained unchanged.

Ruff lint/format, mypy, all nine import contracts, lock validation, documentation and
repository hygiene checks, and `git diff --check` passed. Actual localhost requests
returned the real temperatures above; repeated responses matched, another coordinate
worked, and unsupported coordinates returned 422. The listener was confirmed to be
`127.0.0.1` only, then stopped. Full database/storage acceptance, the full coverage
gate, and the live-provider canary suite were not run. This fixed acquisition does
not establish operational provider reliability or forecast skill. No dependencies
or lockfile entries changed for the 36-hour extension.

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
