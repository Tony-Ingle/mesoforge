# MesoForge

MesoForge is intended to be an automatically updating, location-aware forecasting
engine exposed through an API, with issued forecast history and measured performance
against suitable observations. That is the product direction; the current implementation
is described below. Start with [VISION.md](VISION.md) for release boundaries and
[AGENTS.md](AGENTS.md) for working rules.

## Current status

The localhost endpoint, `GET /forecast?lat=45.8&lon=-93.1`, now returns temperature
from **real prepared HRRR/GFS guidance** for hours 1-36 at exact coordinates within
the native model domains. Coordinate preparation now derives and shares spatial
coverage automatically; the former Grasston demonstration rectangle is retired.
It retains the approved 70% HRRR / 30% GFS demonstration
weights throughout the window; these are demonstration weights, not optimized
weights. It reports Kelvin units, source cycles/leads, valid times, checksums,
and explicit missingness. Preparation now automatically selects complete current
HRRR/GFS temperature guidance before serving; explicit source cycles and target
reference time remain an override. See [automatic cycle selection](#automatic-current-guidance). The demonstrated September 10, 2026 snapshots are
fixed historical guidance, not current live forecasts. Retained raw messages can
now rebuild a dataset offline with the same preparation command's `--from-raw` mode.
Starting without `--data-dir` still selects the clearly labeled synthetic example.
The spatial milestone returned all 36 hours for each of the three requested coordinates,
including 44.98, -93.27, with zero new downloads. **229 focused tests passed**, as did
Ruff, mypy, import contracts, documentation/hygiene checks, and `git diff --check`.
The localhost demonstration and offline repeat passed; PostgreSQL/MinIO integration
and full acceptance/coverage were not rerun for this change. See
[automatic spatial coverage](#automatic-spatial-coverage) for commands and evidence.

Validation on September 10: **262 tests passed** (111 API/preparation, 148 retained
Phase 2, and 3 existing acquisition tests), along with quality checks. One bounded
36-hour acquisition supplied all **36/36 hourly API results, with no missing hours**.
An offline rebuild, with network calls blocked, reproduced exact values and preserved
the source snapshot. Independent calculations from the raw messages matched all 36
API temperatures. The server was stopped. Full database/storage acceptance,
the full coverage gate, and the live-provider canary suite were not run. This verifies
the small slice, not the entire application or forecast skill.

Automatic-cycle validation on September 10: **167 focused offline tests and 14
PostgreSQL/MinIO integration tests passed**, along with Ruff, mypy, import contracts,
documentation/hygiene checks and `git diff --check`. The real automatic batch and
localhost HTTP demonstration returned all 36 future hours for Fresno, Wichita and
Raleigh using provider-checked 18Z guidance. Independent calculations, immutable
readback, unchanged previous versions and offline reuse/rebuilding passed. Three
stale storage-test assumptions also failed against committed HEAD; their setup was
corrected without changing scientific assertions or production storage behavior.
Temporary PostgreSQL, MinIO and API processes were stopped. Full acceptance/coverage
and live-provider canaries were not run. See [automatic mode](#automatic-current-guidance).

The separate Phase 2 pipeline remains an unpublished HRRR/NBM/GFS station baseline
for hours 1–36 at KCBG, KJMR, and KROS, with temperature, dew point, wind, gust,
QPF, PoP, METAR verification, provenance, and retained-input replay. Its defaults
are unchanged. Standalone Phase 1 hours 0–6 generation is retired; shared science,
its required configuration overlay, and historical readers remain.

The local batch command now saves immutable issued versions of its 36-hour forecasts
using the existing PostgreSQL/S3 storage. Each explicit run has a new batch ID;
location failures do not stop later coordinates. One-off `GET /forecast` remains
read-only. `GET /issued-forecasts/{issued_forecast_id}` now retrieves one exact saved
version, including its complete forecast and provenance, without calculation or writes.
`GET /issued-forecast-hours` also selects saved hours for one exact coordinate and a
valid-time window, keeping overlapping issued versions separate. **327 offline and
33 PostgreSQL/MinIO integration tests passed** for this selection milestone. Actual
HTTP selection returned three hours from each of two retained versions, with original
payloads and unchanged storage. Temporary services were stopped. Full database/storage
acceptance and coverage remain unverified. See [saved-hour selection](#select-saved-forecast-hours).
`GET /issued-forecasts/{id}/observation-match?valid_time=...` now previews one saved
hour against a retained temperature observation, choosing a station automatically
within 50 km and ±15 minutes after QC. It explains candidate exclusions and preserves
provenance without scoring or writes. **477 focused offline and 37 PostgreSQL/MinIO
integration tests passed** for this milestone. The localhost demonstration used real
saved model forecasts with explicitly **synthetic observation fixtures**; no live
observations were acquired. All stored contents stayed unchanged during requests.
See [observation preview](#preview-one-observation-match) for the rules and limitations.
The explicit local verification command now saves one immutable temperature error
(`forecast - observation`) tied to the exact issued version, observation revision,
matching rules, and retained-input cutoff. Retries with the same inputs and code
reuse the saved artifact; ineligible attempts return reasons without a score.
**680 focused offline and 45 PostgreSQL/MinIO integration tests passed**. The real
saved 18:00 UTC forecast was compared with a synthetic KROS observation, read back,
and retried without duplicates or changes to issued forecasts. See
[single-hour verification](#verify-one-issued-temperature-hour).
The `window` verification command now processes all saved versions for one coordinate
and valid-time window, reporting new, reused, unavailable, and ineligible results.
Its demonstration and repeat preserved issued forecasts; **56 focused offline and
13 PostgreSQL/MinIO integration tests passed**. See [window verification](#verify-a-coordinate-and-time-window).
One bounded **real METAR dataset** now feeds that unchanged verification command.
The September 10 demonstration acquired 27 reports from three retained stations,
saved four verification results across two issued versions, and safely reused all
four on repeat. Raw observations and provenance remain outside Git and in existing
artifact storage. See [real observation preparation](#prepare-one-real-metar-dataset).
The new [automatic verification command](#automatically-prepare-observations-and-verify)
now derives that observation request from saved past hours using only coordinate and
valid-time bounds. Its real demonstration saved six results, reused all six on repeat,
and downloaded nothing for a window with no eligible hours. **75 focused offline and
11 PostgreSQL/MinIO integration tests passed**; broader operational gaps remain below.
The command now also accepts the [existing locations JSON](#verify-configured-locations-sequentially),
processes coordinates sequentially, and continues after location errors. The real batch
demonstration verified both supported locations around an unsupported entry and reused
all 12 results on repeat. **43 focused offline and 2 PostgreSQL/MinIO integration tests
passed** for that increment. The new spatial preparation milestone is documented
[below](#automatic-spatial-coverage). On-demand station discovery now queries and saves
nearby METAR metadata from each
coordinate, feeding the existing 50 km / ±15-minute verification path. See
[station discovery](#discover-and-reuse-nearby-metar-stations). Its four-coordinate real
demonstration saved 25 candidates and reused them with zero discovery calls on repeat.
Nine eligible real-observation verification results were saved and safely reused;
all 10 issued versions stayed unchanged. **109 focused offline and 16 PostgreSQL/MinIO
integration tests passed**. Broader acceptance/coverage was not rerun.
Newly issued hours now retain individual HRRR/GFS temperatures. The read-only
[model comparison command](#compare-temperature-models-and-blends) reports both models,
the unchanged 70/30 control, a comparison-only 50/50 blend, and saved observation errors
with descriptive MAE/bias/RMSE. Its real demonstration used 19 existing verified hours;
these small, overlapping samples do not establish a better recipe.
Contributor capabilities and named/versioned recipes now share a generic scalar path.
A synthetic third shadow contributor can be retained and compared without changing the
active forecast; no additional real model is implemented. Proposed next milestone:
add a bounded RAP temperature adapter in shadow mode and evaluate it against the saved
control. The combined locations lifecycle remains future work.

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

[The HTTP entry point](src/mesoforge/api.py) accepts an exact coordinate and
returns 36 hourly temperatures through [prepared point extraction](src/mesoforge/application/point_forecast.py).
Every JSON response identifies its inputs as real prepared guidance or synthetic
demonstration data. Neither mode claims to be a current live forecast. Invalid
coordinates or points outside the native model domain return HTTP 422. A valid point
outside prepared coverage returns HTTP 409 (`coverage_required`), with instructions
to prepare coverage first. GET never downloads or prepares data.

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
and ensures shared coverage from one **36-hour** source snapshot before issuing
forecasts. Save this as `locations.json`:

```json
{
  "locations": [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 45.9, "lon": -93.0}
  ]
}
```

Each location contains only numeric `lat` and `lon`; no station IDs, counties,
bounding boxes, or other geographic configuration is needed. **Batch runs now issue
and persist forecasts**, so they require migrated PostgreSQL and an S3-compatible
object store. One-off `/forecast` requests still need only the prepared files.

Use the existing storage environment variables, supplied outside Git:
`MESOFORGE_DATABASE_DSN`, `MESOFORGE_S3_ENDPOINT`, `MESOFORGE_S3_BUCKET`,
`MESOFORGE_S3_ACCESS_KEY`, and `MESOFORGE_S3_SECRET_KEY`. The DSN uses the
`postgresql+psycopg://` dialect. See [local development](docs/operations/local-development.md)
for existing service options; the command does not install or start them. Apply
migration `0004_issued_forecasts` through the existing Alembic chain before running.
From the repository root with the locked environment configured:

```text
uv run --locked alembic upgrade head
uv run --locked python -m mesoforge.application.batch_forecast --config locations.json --data-dir PATH_TO_PREPARED_SNAPSHOT
```

The portable `uv run` wrappers have not been executed on this Windows checkout.
The command writes JSON to standard output: `batch_run_id` identifies this
invocation; `results` preserves input order and each entry contains its
zero-based `index`, input `location`, and `status`. An `ok` entry has the complete
existing `forecast`, including all 36 hours, units, source cycles/leads, valid times,
weights, missing reasons, source URLs, and checksums. It also has an `issued` header
with a unique `issued_forecast_id`, batch ID, location index, coordinates, issuance
UTC, target reference UTC, and the saved object's content digest. An `error` entry
instead has an error `code` and `message`; no successful issuance is reported for
that location. Missing hourly guidance remains null with reasons inside a saved
forecast; it does not silently change the blend.
An unrepresentably large JSON number is retained as text in its location error.

Every invocation creates new issued versions, even when inputs and numerical values
are identical. Issuance time is the current UTC time when the record is created;
target/reference and source-cycle times retain their prepared-input meanings. This
historical demonstration still makes no operational cutoff or live-forecast claim.
The full forecast, issuance metadata, and current source/dependency identity are
serialized with the existing canonical JSON serializer and saved through the
existing S3 adapter. Verified bytes precede a single PostgreSQL transaction for
stored-object metadata and the `issued_forecasts` header. Scientific payloads are
not stored in PostgreSQL. A new run never updates the old header or overwrites its
object; a database trigger also rejects header UPDATE/DELETE operations.

An unsupported coordinate never reaches storage. Upload, integrity, or database
errors return `issuance_failed` for that location and processing continues. A failed
transaction may leave an unreferenced object; it does not publish partial metadata.
No automatic retries or cleanup are added. Preserve the PostgreSQL data and S3
objects together to retain issued history; saving a record does not copy the original
GRIB guidance into that history or extend its existing retention guarantees.

Exit code **0** means every location succeeded; **1** means at least one location
failed, after all locations were processed. **2** reports an unusable config or
dataset, or unavailable storage setup, on standard error. The command rejects older
three-hour snapshots. It loads and verifies guidance once, then reuses the same arrays
for every coordinate without provider calls or per-location preparation. Network I/O
is limited to the configured persistence services. It starts no HTTP server or location
registration/scheduling process. Models, area, temperature scope, and fixed 70/30
demonstration weights are unchanged. `GET /forecast` calls no issuance service and
does not create history, even for coordinates previously issued by a batch.

The earlier calculation-only demonstration on September 10 used HRRR 12Z / GFS 06Z:
an unsupported coordinate was inserted between the two supported points above.
Its example config and prepared snapshot remain available outside Git. With the
storage environment variables configured and the database migrated, the equivalent
PowerShell issuance command is:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$batchDemo = "$env:LOCALAPPDATA\MesoForge\baselines\20260910-coordinate-batch"
$snapshot = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
& $python -B -m alembic upgrade head
& $python -B -m mesoforge.application.batch_forecast --config "$batchDemo\locations.json" --data-dir $snapshot
$LASTEXITCODE # Historical result was 1; automatic coverage now supports all three points.
```

| Input order | Latitude, longitude | Actual result | Hour 1 / hour 36, K (rounded) |
| --- | --- | --- | --- |
| 0 | 45.8, -93.1 | 36 hours; none missing | 283.705085 / 298.287692 |
| 1 | 44.98, -93.27 | Historical rectangle rejection | Now supported after automatic preparation |
| 2 | 45.9, -93.0 | 36 hours; none missing | 283.629142 / 298.173662 |

The earlier calculation-only output is `actual-batch.json` in the external directory
above. A repeated run with network calls blocked produced identical results, loaded the dataset once
(two prepared-file opens), and left the source snapshot unchanged. The first
coordinate's full forecast exactly matches the earlier captured API response.
These are fixed historical model inputs, not a current live forecast.

Prior calculation-only validation: **27 focused batch tests passed**, plus **262 existing API,
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
no model data was downloaded and no services were started for that earlier milestone.

Issuance validation on September 10: **376 offline tests passed** (34 batch/issuance, 262
retained API/preparation/Phase 2, 20 serializers, and 60 shared application tests).
The storage unit tests use explicit in-memory doubles. **24 integration tests also
passed against actual PostgreSQL 16.2 and MinIO RELEASE.2025-09-07T16-13-09Z**:
issuance/readback, immutable UPDATE/DELETE rejection, explicit missingness,
transaction rollback with continued processing, read-only GETs, migration roundtrips,
and existing S3 integrity/concurrency checks. Mypy, nine import contracts, offline
lock validation, formatting/lint, documentation, hygiene, and whitespace checks passed.

```text
python -B -m pytest tests/unit/application/test_batch_forecast.py tests/unit/application/test_forecast_issuance.py -q -p no:cacheprovider
python -B -m pytest tests/integration/application/test_batch_issuance.py tests/integration/storage/test_migrations.py tests/integration/storage/test_s3_object_store.py -q -p no:cacheprovider
```

The actual batch CLI was run twice with the real 36-hour snapshot and the three-location
config above, using the isolated Python environment and existing storage variables:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& $python -B -m mesoforge.application.batch_forecast --config "$env:LOCALAPPDATA\MesoForge\baselines\20260910-coordinate-batch\locations.json" --data-dir "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
```

Both runs returned `ok, error, ok` and exit **1**, as expected for the unsupported
middle coordinate. Both supported coordinates retained **two distinct versions**, each
with all 36 hours and no missing values. PostgreSQL held **four issuance headers and
four stored-object metadata rows**; MinIO held **four canonical JSON payloads totaling
134,510 bytes**. Readback through a new issuance service reproduced each complete
forecast and provenance exactly, with verified checksums. Two actual localhost GETs
matched the earlier real API response and left both storage counts unchanged.
The original prepared snapshot was unchanged; no guidance was downloaded.

Evidence, batch output, and readback JSON are outside Git at
`%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-forecasts\demonstration-report.json`
and its containing directory. PostgreSQL and MinIO used separate test/demo storage
outside Git and loopback listeners; both services and the temporary API were stopped.
Data files remain locally. No Docker/WSL or permanent service setup was installed.
Full Phase 2 database/storage acceptance, the coverage gate, live-provider canaries,
backup/restore, and production deployment were not validated by this focused check.

### Retrieve one issued version

`GET /issued-forecasts/{issued_forecast_id}` returns the **complete saved JSON envelope**:
issuance ID/time, batch ID, coordinates, target reference time, stored code/dependency
identity, and the nested `forecast` with all values, units, source cycles/leads, valid
times, weights, checksums, and explicit missingness. It uses the existing PostgreSQL
metadata lookup and checksum-verified MinIO/S3 reader. It does not calculate a forecast,
replace its provenance with current code/input identity, or create rows, buckets, or
objects. It returns the saved version even if `/forecast` has different inputs loaded.

Use the same `MESOFORGE_DATABASE_DSN` and `MESOFORGE_S3_*` variables documented for
batch issuance and the existing API startup command. The existing launcher still loads
prepared guidance for `/forecast`; that calculation route needs no storage connection.
Retrieval requires the already-migrated issuance database and its original object
bucket. No additional migration is needed. Example request using a retained local ID:

```text
GET http://127.0.0.1:8765/issued-forecasts/b80e231a-c6e6-4066-ab4c-1e5d38bc2592
```

This is distinct from `GET /forecast?lat=45.8&lon=-93.1`, which calculates from the
currently loaded prepared guidance and returns no issuance header. An unknown valid
UUID returns **404** with `issued_forecast_not_found`; a malformed UUID returns **422**
with `invalid_issued_forecast_id`. Storage unavailability, missing bytes for a known
version, or a checksum failure returns **500** with `issued_forecast_read_failed`.
Retrieval never reconstructs a damaged version or exposes connection details in errors.

September 10 validation: **156 API/batch/issuance/preparation offline tests** and the
**148-test recorded Phase 2 selection** passed. **29 PostgreSQL/MinIO integration
tests** passed, including exact repeated HTTP readback, no calculation/write calls,
invalid/unknown IDs, corrupted/missing payloads, and no bucket creation. Commands run
through the isolated Python 3.12 environment (`PYTHONPATH=src`):

```text
python -B -m pytest tests/unit/test_issued_forecast_api.py tests/unit/test_forecast_api.py tests/unit/test_real_forecast_api.py tests/unit/application/test_batch_forecast.py tests/unit/application/test_forecast_issuance.py tests/unit/application/test_prepared_temperature.py -q -p no:cacheprovider
python -B -m pytest tests/integration/application/test_batch_issuance.py tests/integration/storage/test_migrations.py tests/integration/storage/test_s3_object_store.py -q -p no:cacheprovider
```

Actual localhost requests retrieved the two prior versions for `(45.8, -93.1)`:
`b80e231a-c6e6-4066-ab4c-1e5d38bc2592` and `cb485961-fd26-4927-a670-38831f76b3b3`.
Both returned 200 and matched the complete original JSON and MinIO checksums. Repeated
retrieval, malformed/unknown ID requests, and a normal `/forecast` GET left **four
issuance rows, four metadata rows, and four objects (134,510 bytes)** unchanged.
No new forecasts or guidance downloads were needed. The API, PostgreSQL, and MinIO
were stopped afterward, preserving their data outside Git. Evidence and captured
responses are in `%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-retrieval`.
Quality checks passed; the full acceptance, coverage, live-provider, backup/restore,
and deployment gaps above remain. The read-only observation preview is documented below;
single-hour verification is also documented below.

### Select saved forecast hours

Use the same API startup and PostgreSQL/S3 settings as saved-version retrieval:

```text
GET http://127.0.0.1:8765/issued-forecast-hours?lat=45.8&lon=-93.1&start_valid_time=2026-09-10T13:00:00Z&end_valid_time=2026-09-10T16:00:00Z
```

Coordinates match the stored latitude/longitude exactly; there is no nearest-location
search. Supply timezone-aware ISO timestamps with **start before end**. Offsets normalize
to UTC; the window includes the start and excludes the end (`[start, end)`). Each
matching saved hour appears separately in `results`, including every issued version
for the same valid time. Results sort by valid time, issuance time, then issuance ID.
An empty window match or a valid coordinate with no history returns HTTP 200 and
`"results": []`. Invalid/missing query parameters return 422 with
`invalid_issued_forecast_hour_query`.

Each result contains `issued` (original ID, issuance time, coordinate, batch ID,
target reference time, and the complete saved object's `content_digest`), the stored
`code_identity`, `forecast_context` (all forecast-level metadata except the hours list),
and one unchanged `hour`. That hour retains its valid time, value/unit, model cycles,
leads, weights, source checksums, and missing reasons. Null values remain explicit.
The digest identifies the complete original saved forecast, not this selected view.

Selection reads each saved version for the exact coordinate through the existing
checksum-verified reader and filters its **actual saved valid times**. It neither
regenerates forecasts nor reads observations, scores error, or writes storage. It
does not apply an issuance cutoff or declare a forecast eligible for verification;
historically issued demonstration records retain their distinct issuance/valid times.
No versions are silently dropped at the repository's usual 100-record listing limit.
This initial implementation reads all versions for that coordinate; it has no pagination
or hour-level query index. A storage/integrity failure returns 500 with
`issued_forecast_hour_selection_failed`, without a partial successful result.

On September 10, the example query returned **six matches**: 13:00, 14:00, and 15:00 UTC
from each of `b80e231a-c6e6-4066-ab4c-1e5d38bc2592` and
`cb485961-fd26-4927-a670-38831f76b3b3`. Every hour and its provenance matched the original
stored JSON. A September 12 01:00–03:00 UTC query returned no matches. Repeated queries
left **four forecast rows, four object-metadata rows, and four MinIO objects (134,510
bytes)** unchanged. PostgreSQL, MinIO, and the localhost API were stopped afterward.
Evidence is outside Git in `%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-hour-selection`.

**179 focused offline API/preparation/batch/issuance tests plus 148 retained Phase 2
tests passed**. **33 real PostgreSQL/MinIO integration tests passed**, including a
101-version completeness check, interval boundaries, exact coordinates, empty results,
unchanged storage, and missing/corrupt payloads. The new unit module is
`tests/unit/test_issued_forecast_hours.py`; it was run alongside the six offline modules
listed above. The same three integration modules above were rerun. Quality checks
passed. Full acceptance, coverage, live-provider, backup/restore, and deployment remain
unvalidated. No observations were fetched or verification performed.

### Preview one observation match

Use the existing API startup and PostgreSQL/S3 settings. The operator additionally
sets `MESOFORGE_OBSERVATIONS_ARTIFACT_ID` to one already-retained
`normalized-metar-observations` artifact with schema `metar-observations.v2` in that
storage. The preview loads its referenced station snapshots and original QC
configuration through existing checksum-verified readers. This selects an input
dataset, not a manually chosen station; the saved forecast supplies latitude/longitude.
There is no observation discovery or acquisition in this endpoint.

```text
GET http://127.0.0.1:8765/issued-forecasts/b80e231a-c6e6-4066-ab4c-1e5d38bc2592/observation-match?valid_time=2026-09-10T13:00:00Z
```

The response preserves the saved hour, forecast context and code identity, and returns
the selected station's METAR identity, coordinates, elevation, WGS84 distance, observation
timestamp, Kelvin temperature, QC and raw/revision provenance. `candidates` explains
exclusions. Both **50 km** and **±15 minutes** are inclusive. Eligible candidates rank
by distance, then absolute time difference, then station ID; remaining ties use earlier
observation time and revision identity. The latest retained revision of each logical
observation is chosen before QC. Temperature-specific failures exclude a candidate;
unrelated missing wind does not. The provider's opaque `qcField` is reported as evidence,
not interpreted as a temperature failure by itself.

No acceptable observation, missing forecast temperature, or an unconfigured dataset
returns HTTP 200 with `status: unavailable`, `selected: null`, and an explicit reason.
An empty observation artifact has no referenced station inventory to list. Invalid
IDs or missing/naive times return 422; an unknown saved ID/hour returns 404; damaged
or unavailable configured storage returns 500, not a successful unavailable match.
This is a proxy preview over the configured snapshot, not an issuance-cutoff eligibility
decision, an observation at the forecast coordinate itself, or a forecast skill score.

September 10 localhost demonstration used the existing real-model forecast at
`(45.8, -93.1)` for 13:00 UTC (**283.70508538821554 K**) and clearly labeled synthetic
observation fixtures registered before the requests:

| Station | Distance | Outcome |
| --- | --- | --- |
| KROS | 16.174 km | Selected: 293.15 K at 13:10 UTC, QC eligible |
| KJMR | 16.407 km | Excluded: temperature failed range QC |
| KCBG | 29.878 km | Eligible at 13:00 UTC, but farther away |

The 15:00 UTC hour returned unavailable because no retained observation met the rules.
Repeated requests preserved every PostgreSQL table's contents and all **seven MinIO
objects**, including **four issued forecasts** and three fixture input artifacts.
No verification/error artifacts or activities were created. The API and temporary
services were stopped. Captured responses and the input manifest are outside Git at
`%LOCALAPPDATA%\MesoForge\baselines\20260910-observation-match-preview`.

Executed in the isolated locked environment: the previously listed API/batch/issuance/
preparation modules, `tests/unit/observations`, `tests/unit/test_observation_preview_api.py`,
and the recorded 148-test Phase 2 selection (**477 offline tests** total). The same
three PostgreSQL/MinIO modules plus `tests/integration/application/test_observation_preview.py`
passed (**37 integration tests**). Ruff, formatting, mypy, import contracts, lockfile,
documentation, hygiene, and whitespace checks passed. Live observation handling,
full acceptance/coverage, backup/restore, and deployment remain unvalidated.

### Verify one issued temperature hour

This is an explicit write command; all existing API GET endpoints remain read-only.
Use the same PostgreSQL/S3 settings and `MESOFORGE_OBSERVATIONS_ARTIFACT_ID` as the
observation preview. With the project environment activated and `PYTHONPATH=src`:

```text
python -B -m mesoforge.application.issued_temperature_verification verify --issued-forecast-id b80e231a-c6e6-4066-ab4c-1e5d38bc2592 --valid-time 2026-09-10T18:00:00Z
python -B -m mesoforge.application.issued_temperature_verification read --verification-id art_4a491043-aa94-49e2-b2b3-cb6b579cd902
```

These commands were demonstrated locally using the isolated locked Python 3.12
environment. The IDs refer to retained local demonstration data. `verify` returns
the saved artifact ID, manifest, and result; `read` returns that exact result without
reselecting observations or recalculating. The existing `ArtifactService` stores the
JSON fact in MinIO and its manifest, activity, and input links in PostgreSQL. No new
tables or migrations were needed. The result retains the full match, issued-forecast
ID and checksum, observation revision, source/QC/proxy metadata, rules, and code identity.

The verification cutoff is fixed to the normalized observation artifact's availability
time, making it a comparison against that retained snapshot. Forecast issuance must
strictly precede both valid time and observation time; those times, observation
availability/ingestion, and selected station metadata must be within the cutoff, which
cannot be in the future. Temperatures must be finite Kelvin values and the match must
pass the existing 50 km / ±15-minute / QC rules. Saved model cycles cannot follow
issuance. **Acquisition timestamps are not embedded in saved forecasts**, so this is
not a complete operational input-cutoff audit or a claim of forecast skill.

Ineligible/unavailable attempts return explicit reasons, a null error and null
verification ID, with no score artifact. Exit codes are 0 for a saved/read result,
1 for an ineligible/unavailable attempt, and 2 for invalid input or storage failure.
Identical forecast version/hour, observation snapshot/revision, cutoff, rules, and
code/environment identity reuse the same result, including concurrent requests.
Changed inputs, policy, or code identity produce a separate artifact; older results
remain intact. Verification facts contain no aggregate performance statistics.

The September 10 demonstration used forecast **296.5107933539454 K** at 18:00 UTC,
issued at 17:09:48 UTC, and a **synthetic** KROS observation of **293.15 K** at 18:10 UTC.
The saved error was **+3.3607933539454393 K**, tied to the issued ID above and observation
revision `sha256:86ea1c81d54e31cce73da4a3d4dcda450dba6cf62fae49e1d491f93275a9e650`.
Repeated execution and readback returned the same artifact. An older observation
snapshot returned unavailable for 18:00 UTC; the 13:00 UTC forecast was ineligible
because issuance followed valid/observation time. Neither attempt wrote a score.
All four issued forecasts remained unchanged. One verification artifact/activity
and one 16,525-byte MinIO object were added after fixture setup. Temporary services
were stopped; evidence is outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-temperature-verification`.

Validation: **680 offline tests** passed across the API/batch/issuance/preparation,
observation, verification, artifact-service, and recorded Phase 2 selections.
**45 PostgreSQL/MinIO integration tests** passed, including concurrent retry safety,
independent issued versions, exact readback, explicit ineligibility, and damaged inputs.
The new modules are `tests/unit/verification/test_issued_temperature.py`,
`tests/unit/application/test_issued_temperature_verification.py`, and
`tests/integration/application/test_issued_temperature_verification.py`.
Quality checks passed. That milestone used synthetic observations; the later bounded
real-observation check is below. Full acceptance/coverage, backup/restore, and deployment
remain unvalidated.

### Verify a coordinate and time window

Use the same environment and retained observation artifact as single-hour verification:

```text
python -B -m mesoforge.application.issued_temperature_verification window --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T20:00:00Z
```

The window includes the start and excludes the end. Existing selection returns every
issued version separately; each then uses unchanged matching, eligibility, and
persistence logic. Results contain issued ID, valid time, verification ID, temperature
error, and reasons. Read the complete saved provenance with the existing `read` command.
Summary categories are exclusive: `verified` means newly saved, `already_existing`
means reused with identical input/policy/code identity; `unavailable`, `ineligible`,
and `errors` retain failures without stopping later hours. An empty selection returns
zero counts. Exit 0 means processing completed (including expected unavailable/ineligible
hours), 1 reports per-hour processing errors, and 2 indicates invalid input or failure
to select/start. GET endpoints remain read-only; no observations are downloaded.

The command above was demonstrated with two retained real-model forecast versions and
one synthetic observation dataset. One eligible result was saved beforehand:

| Run | Newly verified | Already existing | Ineligible | Unavailable | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| First window | 3 | 1 | 2 | 0 | 0 |
| Repeated window | 0 | 4 | 2 | 0 | 0 |

Both versions' 18:00 and 19:00 UTC hours were eligible; 17:00 UTC preceded issuance.
Repeated execution/readback changed no PostgreSQL rows or MinIO objects. All four
issued forecasts remained unchanged, and temporary services were stopped. Captured
results are outside Git in `%LOCALAPPDATA%\MesoForge\baselines\20260910-verification-window`.
Only focused coverage was added: six window/CLI unit cases and one integration case;
the existing concurrency check also verifies reuse reporting. **56 offline tests**
(verification application and hour selection) and **13 integration tests** (verification
and observation preview) passed, along with applicable quality checks. The integration
window includes an unavailable hour between eligible hours. Broader suites were not
rerun; operational validation gaps remain. The later real-observation demonstration follows.

### Compare temperature models and blends

New forecast responses and immutable issuances include each source's extracted
`temperature` in Kelvin and `missing_reasons`. The production control remains
**70% HRRR / 30% GFS**. No weights are learned or changed by comparison.

Model definitions in `catalog/contributors.py` describe provider, family/lineage,
domain, supported fields, nominal UTC cycles, adapter-supported leads, grid type,
and lifecycle status. Nominal cycles are capability metadata, not proof of provider
availability. Current defaults describe the existing temperature adapter envelope,
not every product the models publish; unknown lineage is left empty.
`forecasting/recipes.py` defines `temperature_control_v1` and `temperature_equal_v1`
as configurations evaluated by the same scalar function. The existing output keys
`blend_70_30` and `blend_50_50` are retained for compatibility.

An explicit batch can load model/recipe JSON without changing the locations file:

```powershell
python -c "from pathlib import Path; from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION; Path('contributors.json').write_text(DEFAULT_CONFIGURATION.model_dump_json(indent=2), encoding='utf-8')"
python -B -m mesoforge.application.batch_forecast --config locations.json --data-dir PREPARED_DIRECTORY --contributors-config contributors.json
```

The default export contains only HRRR/GFS. A future adapter registers its capabilities
and provides normalized prepared inputs; its metadata and a comparison recipe can be
added to this configuration. This milestone does not acquire a new real model.
Batch entry points reject changes to the approved control. Shadow, evaluated, and
deprecated inputs already prepared as `MODEL_ID.nc` can be read with zero active weight;
retired models are not loaded for new forecasts. A real shadow input must carry the
existing manifest/raw/prepared checksum evidence. Synthetic inputs remain labeled.

New issuances save the configuration snapshot and separate `shadow_sources`, including
values, applied active weights, cycles/leads and provenance, in the existing immutable
payload. Shadow missingness does not change active forecast eligibility. Comparison
reads that saved configuration, retains named recipe definitions, and uses the existing
eligibility checks before scoring shadow sources. Missing required contributors produce
null recipe values without weight redistribution. Mixed history lacking a shadow is
explicitly excluded from the common paired sample; different recipe definitions under
one output key cannot be pooled. No historical forecast is rewritten.

The lifecycle is **shadow → evaluated → active → deprecated → retired**. Status is an
explicit configuration decision, not an automatic promotion or weight-learning policy.
Evaluation provides evidence; activation requires an approved versioned recipe change.
Historical snapshots remain readable after later configuration changes.

With the existing PostgreSQL/MinIO environment, compare an exact saved verification:

```powershell
python -B -m mesoforge.application.model_comparison --verification-id art_dd886879-272b-4c54-b3e9-c3e0fa9eb495 --guidance-root "$env:LOCALAPPDATA\MesoForge\prepared"
```

Repeat `--verification-id` or use `--verification-ids-file ids.json`, containing a JSON
list of saved verification IDs. This explicitly selects the observation revision;
the command never rematches observations, generates issuances, or writes storage.
Repeated identical IDs count once. Different issued versions remain separate samples;
selecting two verification revisions of the same issued hour is rejected to avoid
double counting. Missing contributors remain explicit and cannot be inferred from
the blended value.

To view all 36 prediction comparisons without selecting an observation or scoring:

```text
python -B -m mesoforge.application.model_comparison --issued-forecast-id ISSUED_FORECAST_UUID
```

`--guidance-root` is optional for old issuances that lack contributor values. It searches
retained manifests by the exact issued checksum and reuses the existing prepared-input
reader/extraction. Raw/prepared checksums, recorded scientific code/dependencies, source
provenance and the original control must agree. Recovered contributors are labeled
**reconstructed**, never inserted into old records. Older identity records omit the
temporal-alignment module; that limitation and the current extraction identity are
reported. No recovery data is downloaded or prepared. New issuances need no guidance
directory for comparison because their contributor values are already saved.

Verified output preserves issuance/verification IDs, coordinates, source cycles and
leads, valid times, observation revision/QC, and provenance. Errors are prediction minus
observation. Aggregate metrics use the **same complete paired samples** for all requested
predictions (four by default). Buckets **1–6, 7–18, 19–36** use saved horizons since target reference time;
native model leads and elapsed hours since issuance remain separate. Zero samples
produce null metrics. Counts and descriptive statistics are not claims of forecast skill.

Actual September 10 read-only demonstration: **19** previously verified real-observation
hours, with bucket counts **4 / 15 / 0**. At Fresno 22Z, HRRR **314.459054 K**, GFS
**314.750977 K**, control **314.546631 K**, and 50/50 **314.605015 K** were compared
with **313.15 K** observed. Complete rows and metrics are outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-model-comparison`.
Repeat output was identical; all 10 issued versions and all PostgreSQL/MinIO contents
stayed unchanged. No provider calls occurred. These CLI entry points were exercised
using the isolated locked interpreter; `uv run --locked` remains unverified here.

The contributor generalization was checked against committed `c42e3dd`: all **360 real
hours across 10 saved versions** matched exactly, including control values and source
provenance. All 19 real comparison values/errors and bucket metrics also matched.
A separate synthetic fixture batch retained a third contributor at zero active weight;
the 283 K control stayed unchanged while a named three-model comparison returned
287.5 K. Both issued versions and their verification readbacks remained immutable.
Those synthetic numbers demonstrate mechanics, not forecast skill. Raw numerical
output/configuration is outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-generic-contributors`.

Final focused checks: **314 offline tests** and **14 PostgreSQL/MinIO integration tests**
passed, plus Ruff, formatting, typing, import, documentation, hygiene and diff checks.
An initial integration repeat-check failure passed its isolated and complete reruns;
no assertion or production behavior was weakened. Temporary services were stopped.
No provider downloads occurred. Full acceptance/coverage and broader forecast-skill
evaluation were not run. Missing shadow inputs are isolated; malformed shadow files
(wrong units, times or checksums) intentionally fail loading closed.

### Discover and reuse nearby METAR stations

The geographic configuration still contains only latitude/longitude. With the existing
PostgreSQL/MinIO environment, discover candidates independently of forecast eligibility:

```text
python -B -m mesoforge.application.station_discovery --config locations.json
python -B -m mesoforge.application.station_discovery --lat 36.7378 --lon -119.7871
```

The [official Aviation Weather Center station-information API](https://aviationweather.gov/data/api/)
supports bounded queries. MesoForge derives a small bounding box internally, queries
`/api/data/stationinfo`, keeps METAR-capable candidates at a WGS84 distance of at most
50 km, and orders them deterministically. It does not download a nationwide catalog.
Dateline footprints use two bounded queries. Failed, oversized, malformed or possibly
truncated provider responses are errors rather than cached empty results.

Successful discovery, including a genuinely empty list, is saved with the existing
artifact service. PostgreSQL retains manifests, coordinate/policy lookup metadata and
lineage; MinIO retains exact raw responses and the immutable candidate snapshot.
The snapshot includes station ID/network, coordinates, elevation when available,
distance, metadata source, acquisition time, query bounds and raw checksums.
A repeated coordinate loads the saved snapshot without a station-discovery request.
A coordinate lock prevents concurrent first runs from rediscovering it independently.
Discovery has a versioned policy and an internal explicit-refresh option, preserving
older snapshots; there is no scheduled refresh or automatic expiry yet.

The existing automatic verification command invokes this lookup only when saved hours
are ready, then reuses the same METAR acquisition, normalization, QC, station ranking,
and verification persistence. With no ready hours it downloads neither station metadata
nor observations. The standalone discovery command can still prepare station candidates
in advance. A new station snapshot cannot silently reuse observations normalized against
an older snapshot. Normal forecast GET requests do not trigger discovery or verification.

A discovered station is a candidate, not a promise of a usable observation. The unchanged
QC requires metadata comparisons, including elevation: a missing elevation is saved as
null and reported as unavailable for that QC, never filled with zero or an invented value.
Station metadata acquired today can reject older reports if their metadata differs beyond
the existing tolerance. Nearest eligible station, ±15-minute matching, QC, and deterministic
ties are unchanged. The retained Phase 2 station catalog and historical readers remain;
the new coordinate workflow no longer selects candidates from Minnesota's fixed list.

Use the existing verification command; station IDs and padded observation windows are
still derived internally:

```text
python -B -m mesoforge.application.automatic_verification --config locations.json --start-valid-time 2026-09-10T20:00:00Z --end-valid-time 2026-09-10T23:00:00Z
```

These command forms use the isolated interpreter documented above; the `uv run --locked`
wrapper remains unverified here. Both discovery CLI forms and the automatic-verification
command were executed on September 10; the discovery CLI reused the saved snapshots.

The four-coordinate demonstration supplied only these coordinates. Candidate distances
below are rounded to 0.1 km; saved metadata retains full precision.

| Coordinate | Discovered METAR candidates and distance (km) |
| --- | --- |
| Fresno: 36.7378, -119.7871 | KFCH 3.0; KFAT 7.6; KO32 30.9; KMAE 39.8; KHJO 49.5 |
| Wichita: 37.6872, -97.3301 | KIAB 9.1; KICT 9.9; KBEC 10.2; KAAO 12.0; K1K1 21.3; K3AU 22.2; KEGT 40.5; KEWK 41.9; KEQA 46.5 |
| Raleigh: 35.7796, -78.6382 | KRDU 18.0; KJNX 34.9; KTQV 36.9; KLHZ 38.5; KHRJ 45.3; KTTA 47.8 |
| Minnesota: 45.8, -93.1 | KROS 16.2; KJMR 16.4; K04W 29.4; KCBG 29.9; KPNM 48.0 |

Four bounded metadata requests downloaded **7,180 bytes** and saved four immutable
snapshots containing all metadata listed above. Repeat discovery made zero provider
calls and created no PostgreSQL rows or MinIO objects. The real verification run used
**30,486 bytes** of METAR observations and saved **9** results: one at each CONUS
coordinate and six across two Minnesota issued versions. Six earlier CONUS hours were
not issued before their valid times; explicit reasons were returned without scores.
Repeat verification reused the observations and all nine results without network calls
or storage changes. All **10** existing issued forecasts stayed unchanged.

Focused checks passed: **109 offline tests** covering discovery, retention, preparation,
automatic verification and existing AWC parsing/acquisition; **16 PostgreSQL/MinIO
integration tests** in the existing verification and observation-preview modules.
The final provenance assertion also passed on rerun. Ruff, mypy, all nine import
contracts, documentation/hygiene and whitespace checks passed. Evidence and raw data
remain outside Git under `%LOCALAPPDATA%\MesoForge\baselines\20260910-station-discovery`
and existing artifact storage. Temporary PostgreSQL and MinIO were stopped.
Full acceptance/coverage, scheduled refresh and provider
reliability remain unverified; current metadata does not prove historical station validity.

### Prepare one real METAR dataset

[Observation preparation](src/mesoforge/application/prepared_observations.py) reuses the
retained AviationWeather.gov acquisition, strict parser, Phase 2 normalizer, and
PostgreSQL/MinIO artifact path. It now uses coordinate-driven discovery and saves
nearby METAR candidates within 50 km; station IDs come from the official metadata
response, not the locations file. Older retained bundles still read their original
configuration-pinned station snapshot. New discovery captures metadata at acquisition
time; it does not assert historical station metadata validity. Matching, QC, and
verification math are unchanged. An opaque provider `qcField` is preserved alongside existing field QC;
its numeric value is not interpreted as a newly invented pass/fail rule.

With the same configured PostgreSQL/MinIO environment as verification, the acquisition
command is below. `$python` is the isolated interpreter and `PYTHONPATH` points to `src`
as shown earlier. Use a **new directory outside the repository** for a new acquisition;
this demonstrated directory already exists, so use `--from-raw` to reuse it.

```powershell
$observations = "$env:LOCALAPPDATA\MesoForge\observations\20260910T17-1930Z-grasston-metar"
& $python -B -m mesoforge.application.prepared_observations --raw-dir $observations --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T19:30:00Z
# Rebuild/register from retained bytes, without constructing an HTTP transport:
& $python -B -m mesoforge.application.prepared_observations --raw-dir $observations --from-raw
```

The acquisition function was exercised with those exact inputs; the equivalent first
CLI wrapper was not used for the real request. The `--from-raw` CLI was executed twice.
Preparation prints `observations_artifact_id`; set it before running the **unchanged**
window command. These commands were executed against the retained demonstration:

```powershell
$env:MESOFORGE_OBSERVATIONS_ARTIFACT_ID = 'art_0071726d-b60a-4df2-a215-35b341db342e'
& $python -B -m mesoforge.application.issued_temperature_verification window --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T19:30:00Z
```

Preparation accepts up to six hours per explicit request and pads its bounds by
15 minutes; the entire padded interval must be in the past. It uses the official
[METAR API's date and hours query](https://aviationweather.gov/data/api/) through the
existing retry/rate-limit adapter. This example requested **16:45–19:45 UTC** and
received **27 reports (nine each from KCBG, KJMR, KROS), 11,146 response bytes**, at
19:59:25 UTC on September 10. All normalized successfully. Exact response bytes,
URL, headers, status, acquisition time, configuration/code identity, and checksums
remain in `metar.json`, `manifest.json`, and `configuration.json` outside Git.
The existing adapter represents HTTP 204 as canonical `[]`.
Raw and normalized payloads live in MinIO; PostgreSQL holds their manifests,
configuration, transformation lineage, and verification metadata. Observation event,
report, provider-receipt, and ingestion times stay distinct. Original record indices,
logical/revision digests, raw checksums, and station snapshot references are retained.

At 18:00 UTC, the 17:55 and 18:15 reports at all three stations passed temperature QC.
KROS won at **16.174 km**, ahead of KJMR (16.407 km) and KCBG (29.878 km); its 17:55
report was closer in time than its 18:15 report. The other 21 reports were explicitly
outside the time tolerance. The selected real temperature was **296.25 K**;
the saved forecast was **296.510793354 K**, giving **+0.260793354 K** error.
At 19:00 UTC the error was **+0.201772811 K**. Both issued versions were verified
independently at each hour: **four new records**, then **four reused** on repeat.
Both 17:00 hours remained ineligible because issuance followed the valid/observation
time; no scores were fabricated. There were no unavailable hours or processing errors.

Raw readback matched the downloaded bytes. Offline preparation returned the same
artifact, and repeating preparation/verification changed no rows or objects. All four
issued forecast records and payloads remained unchanged. Evidence is outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-real-metar`.
Focused validation passed **70 offline tests** (new preparation plus existing provider
parser/acquisition/normalization tests) and **10 PostgreSQL/MinIO integration tests**
(existing verification module, including one new preparation-to-window case).
The integration fixture covers a missing hour and blocks new observation HTTP transport
construction after acquisition. No dependencies, policies, or forecast calculations changed.
Ruff lint/format, mypy, all nine import contracts, documentation/hygiene checks, and
`git diff --check` passed.
Full acceptance/coverage, operational input-cutoff validation, long-term provider
reliability, and station metadata history remain unverified; this is not a skill claim.
Temporary services were stopped after validation. No polling or scheduling was added.

### Automatically prepare observations and verify

With the existing PostgreSQL/MinIO services and storage environment configured,
provide only latitude, longitude, and the saved forecast valid-time window:

```text
python -B -m mesoforge.application.automatic_verification --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T21:00:00Z
```

This exact command was demonstrated using the isolated interpreter and `PYTHONPATH=src`.
It selects all issued versions in `[start, end)` and reuses the existing forecast-only
eligibility checks. A finite Kelvin forecast must have valid source cycles, no missingness,
and issuance before its valid time. Future hours and incomplete ±15-minute observation
margins are deferred. These are **preflight** checks: station/observation QC, observation
timing relative to issuance, and retained-input eligibility are still decided by the
existing verifier after preparation. No observation is invented for preflight.

For ready valid times, acquisition spans exactly **earliest time − 15 minutes** through
**latest time + 15 minutes**. A single hour requests 30 minutes; duplicate issued versions
do not widen the request. The retained six-hour limit applies to the span between ready
valid times; a wider span fails explicitly before downloading rather than truncating hours.
Station IDs come from the same retained catalog and 50 km rule. Users supply neither
station IDs nor METAR query bounds. No models, forecast fields, weights, or matching rules change.

The command first looks for an existing real observation preparation covering that
coordinate, station set, time range, and configuration. It checks stored source/normalized
checksums and transformation provenance and reuses the first suitable retained snapshot.
Synthetic fixtures do not qualify. Otherwise it uses the existing acquisition/normalization
and artifact storage. Raw bundles default to `%LOCALAPPDATA%\MesoForge\observations` on
Windows, or `$XDG_DATA_HOME/MesoForge/observations` (otherwise `~/.local/share/MesoForge/observations`)
on Linux. `MESOFORGE_OBSERVATIONS_DIR` optionally changes this service-level path; it must
remain outside Git. The result reports the source URL, stations, bounds, bytes, raw directory
when newly acquired, and observation artifact ID. Raw files can still use `--from-raw`.

The existing verification window implementation then processes the original requested
window, preserving all issued versions and explicit reasons. Its results are separate
from preflight: an hour rejected before acquisition can subsequently appear as unavailable
with its issuance/missingness reason because no observation was acquired for it.
When **no hours are ready**, the command returns `status: "nothing_to_verify"`, per-hour
reasons, `verification: null`, and `downloaded_bytes: 0`, without observation acquisition
or preparation. Exit 0 means completed or nothing to verify, 1 means per-hour verification
errors, and 2 means invalid input or orchestration failure.

The real September 10 demonstration found two issued versions at each of 18:00, 19:00,
and 20:00 UTC. It automatically requested **17:45–20:15 UTC** from **KCBG/KJMR/KROS**,
receiving **24 METAR reports (eight each), 9,934 bytes**. Six verification facts were saved.
For one issued version, errors were **+0.260793 K**, **+0.201773 K**, and **+0.365296 K**
at those respective hours; the other version was independently verified with the same values.
Both 17:00 hours failed preflight because issuance was later than valid time. The unchanged
window verifier reported them unavailable with that explicit reason; no scores were saved.
Repeating the command reused the exact observation artifact and all six verification IDs,
with zero downloads and no new rows or objects. This no-eligible command also made no writes
or downloads:

```text
python -B -m mesoforge.application.automatic_verification --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T18:00:00Z
```

Issued forecasts remained unchanged. Evidence and exact saved results are outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-automatic-verification`.
**75 offline tests passed** in `test_automatic_verification.py`, `test_prepared_observations.py`,
and `tests/unit/verification/test_issued_temperature.py`; **11 PostgreSQL/MinIO integration
tests passed** in the existing verification module. New coverage is limited to nine unit
cases and one integration case: exact bounds, separate versions, no-ready behavior, real
input discovery, synthetic exclusion, overlapping-window reuse, unavailable hours, and
corrupt retained evidence failing without reacquisition. Quality checks passed and temporary
services were stopped.

Repeats intentionally keep a fixed observation snapshot, including missing reports; this
command does not refresh delayed/corrected observations automatically. Verification reuse
continues to require the same inputs, policies, and code identity (including Git HEAD).
Changing code or choosing a different snapshot can create another auditable verification
fact; older facts remain immutable. Full acceptance, coverage, operational cutoff validation,
and provider reliability remain unverified. This remains an on-demand command; the
coordinate-list extension below adds sequential processing without scheduling.

### Verify configured locations sequentially

Use the same locations JSON as batch forecast issuance. For example, `locations.json`:

```json
{
  "locations": [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 44.98, "lon": -93.27},
    {"lat": 45.9, "lon": -93.0}
  ]
}
```

With the existing storage settings/services and isolated interpreter, run:

```text
python -B -m mesoforge.application.automatic_verification --config locations.json --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T21:00:00Z
```

This command was executed with the example config saved outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-batch-verification\locations.json`.
Use either `--config` or `--lat/--lon`. The shared time window is validated before
processing. Each supported coordinate calls the existing automatic verification path
in config order; it derives its own eligible hours, stations, and bounded observation
request. No observation geography or model data path belongs in the locations JSON.
No new forecast is generated or issued by this command.

Each result has its original `index` and `location`, a status, the verification summary,
and the complete single-location `result` with provenance. Unsupported/malformed
coordinates and runtime failures have an explicit `error` and do not stop later entries.
Per-hour processing failures also mark that location as an error while preserving its
successful and failed hour results. No-ready locations return `nothing_to_verify` and
perform zero observation downloads. Existing snapshot/reuse rules remain unchanged.
The batch summary counts `completed`, `nothing_to_verify`, and `errors` locations.
Exit 0 means no location errors, 1 means processing finished with location/hour errors,
and 2 means unusable config or global arguments. The former middle-coordinate
rectangle rejection is retired. With no saved eligible hours it now returns
`nothing_to_verify`; eligible hours use saved or newly discovered station candidates.
If no candidates can pass the existing metadata QC, the result is explicitly
`unavailable` with zero METAR observation downloads; the batch counts those separately.

Historical result before automatic spatial coverage removed the fixed rectangle:

| Coordinate | First run | Repeat | Observation bytes: first / repeat |
| --- | --- | --- | ---: |
| 45.8, -93.1 | 6 verified | 6 reused | 0 / 0 |
| 44.98, -93.27 | Historical rectangle rejection | Historical rectangle rejection | No acquisition |
| 45.9, -93.0 | 6 verified | 6 reused | 9,934 / 0 |

The supported coordinates each had two issued versions at 18:00–20:00 UTC; their
17:00 hours remained unscored with explicit late-issuance reasons. The first location
reused its retained real snapshot. The second acquired 24 METAR reports from KCBG,
KJMR, and KROS for **17:45–20:15 UTC**, through the same preparation path. Repeating
preserved all 12 verification IDs and made no PostgreSQL/MinIO writes. A second batch
ending at 18:00 returned `nothing_to_verify` for both supported locations, with zero
downloads/writes, while still reporting the unsupported coordinate. All four issued
forecast records and their complete payloads remained unchanged.

Evidence is in `%LOCALAPPDATA%\MesoForge\baselines\20260910-batch-verification`.
**43 offline tests passed** in the automatic-verification and batch-forecast unit modules;
**2 integration tests passed** using `-k 'automatic_batch or automatic_window'` in the
existing verification integration module. New coverage is six unit cases and one
integration case focused on sequential processing, failure isolation, no-work results,
reuse, and immutable forecasts. Ruff, mypy, import contracts, documentation/hygiene
checks, and `git diff --check` passed. Temporary services were stopped. Broader acceptance,
coverage, and operational reliability were not tested in this increment.

### Automatic spatial coverage

The locations file contains **only lat/lon**. Each point remains exact; MesoForge
internally derives a minimum **50 km preparation buffer** and **150 km context
footprint**, conservatively enclosed in geographic rectangles. It prepares the larger
footprint plus native interpolation cells, merges overlapping footprints, and handles
distant groups separately. These defaults live in
[spatial coverage](src/mesoforge/application/spatial_coverage.py), not in locations JSON.
The observation search stays **50 km**, using discovered/saved stations and existing suitability
rules. That spatial milestone added no station discovery, fields, or weather-dependent sizing.
At a physical model edge, context is clipped to the available domain and the exact
forecast point is checked separately. The 150 km footprint currently retains temperature
only; it is not a new weather-context analysis product.

```json
{"locations": [
  {"lat": 45.8, "lon": -93.1},
  {"lat": 44.98, "lon": -93.27},
  {"lat": 45.9, "lon": -93.0}
]}
```

From the repository root, with the existing locked interpreter and `PYTHONPATH=src`:

```text
python -B -m mesoforge.application.prepared_temperature --config locations.json --from-raw RETAINED_SNAPSHOT --output-dir COVERAGE_DIR
python -B -m mesoforge.api --data-dir COVERAGE_DIR
```

A single point can use `--lat 44.98 --lon -93.27` instead of `--config`.
Open `http://127.0.0.1:8765/forecast?lat=44.98&lon=-93.27`. The coverage directory
contains a small local index of shared prepared snapshots. The API loads those
snapshots before accepting requests. Normal GET requests still create no history.
Existing batch issuance also ensures the entire collection's coverage automatically:

```text
python -B -m mesoforge.application.batch_forecast --config locations.json --data-dir COVERAGE_DIR
```

Batch issuance needs the existing PostgreSQL/MinIO settings and services. Geographic
expansion reuses checksum-verified full raw messages from the selected source cycles;
it does not redownload a model for each coordinate. Original snapshots remain intact.
A repeat reuses sufficient prepared regions; corrupt/incomplete evidence is reported,
not silently replaced with different inputs. Missing hourly temperatures remain explicit
and the fixed 70/30 demonstration weights never change.

For a **new explicit cycle pair**, use the same preparation command with
`--config locations.json`, `--target-reference-time`, `--hrrr-cycle`, and `--gfs-cycle`
instead of `--from-raw`. It plans the collection first, acquires the selected temperature
messages once, and prepares the needed regions before HTTP startup. Repeating the same
command/output/cycles reuses retained data. Omitting all three time arguments now
selects complete current guidance automatically, as described below. There is no scheduling.

The spatial demonstration reused HRRR **2026-09-10 12Z**, GFS **06Z**, target **12Z**:
HRRR leads 1–36 and GFS leads 7–42 share valid times 13Z September 10 through 00Z
September 12. All three points share one derived context region, approximately
**43.62345–47.25655 N, -95.18797–-91.05049 E**. Downloaded for this expansion: **0 bytes**.
Retained raw temperature messages: **63,399,300 bytes**; inventories: **1,861,814 bytes**.
Original acquisition: **65,261,114 bytes**. Raw hashes, source URLs/ranges, retrieval
metadata, cycles/leads, prepared hashes, and preparation code identity are preserved.
The shared HRRR/GFS prepared files total **5,029,082 bytes**. The localhost API
returned **36/36 hours for each point, with zero missing hours**:

| Exact coordinate | First temperature (K) | Last temperature (K) |
| --- | ---: | ---: |
| 45.8, -93.1 | 283.705085 | 298.287692 |
| 44.98, -93.27 | 287.999758 | 300.493731 |
| 45.9, -93.0 | 283.629142 | 298.173662 |

Independent native-grid interpolation and 70/30 arithmetic matched all 108 values.
A repeat/offline run, with network access blocked during preparation, reused the
shared region and reproduced exact forecasts. HTTP repeats also matched. The original
raw/source files remained unchanged, and the localhost server was stopped.
Evidence and complete hourly responses are outside Git under
`%LOCALAPPDATA%\MesoForge\baselines\20260910-spatial-coverage`.

Preparation and API commands were exercised using the isolated Windows interpreter
(substitute actual paths); portable `uv run --locked` wrappers remain unexecuted.
Batch issuance was checked with existing in-memory storage fixtures in this milestone.
No new provider acquisition, database service, observation acquisition, or full acceptance suite was
needed for this spatial demonstration. The focused checks cover footprints, curved
native-grid bounds, shared/offline preparation, actual-point extraction, missingness,
HTTP read-only behavior, issuance/readback, and the retired rectangle's consumers.
**229 focused tests passed** across the spatial geometry/preparation, temperature
preparation/API, batch forecast, automatic verification, observation preparation,
issuance, and saved-version API modules. Ruff check/format, mypy (`src scripts`), all
nine import contracts, documentation/hygiene checks, and `git diff --check` passed.
PostgreSQL/MinIO integration and the full acceptance/coverage gates were not rerun.
The new spatial checks alone can be run with:

```text
python -B -m pytest tests/unit/application/test_spatial_coverage.py tests/unit/application/test_spatial_preparation.py -q -p no:cacheprovider
```

The final shared index is at
`%LOCALAPPDATA%\MesoForge\prepared\20260910-spatial-coverage-final`.
The preparation command and equivalent default-port startup command are:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$locations = "$env:LOCALAPPDATA\MesoForge\baselines\20260910-spatial-coverage\locations.json"
$raw = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
$coverage = "$env:LOCALAPPDATA\MesoForge\prepared\20260910-spatial-coverage-final"
& $python -B -m mesoforge.application.prepared_temperature --config $locations --from-raw $raw --output-dir $coverage
& $python -B -m mesoforge.api --data-dir $coverage
```

The demonstration used an ephemeral localhost port; the startup command's default is
8765. Stop the API with Ctrl+C.

Only fixed demonstration-region restrictions were retired: the hardcoded temperature
crop, the rectangle validator and its consumers, and the redundant observation-prep
rectangle gate. Boundary tests now distinguish native support from insufficient
prepared coverage. Shared scientific functions, synthetic fixtures, Phase 2 defaults,
observation matching policy, historical readers, and storage remain in place.

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
numbers is not required. These explicit overrides do not substitute different cycles
when a requested pair is unavailable. Omit all three arguments for automatic selection.

The command now downloads **72 selected temperature messages**, one per model per
hour, so run it only when acquiring a new dataset. Choose
a new, empty directory outside Git; occupied directories are refused. The example
directory is already populated on this machine, so use the startup command above
to serve it again. The equivalent portable command is below; its `uv run` wrapper
has not been executed.

```text
uv run --locked python -m mesoforge.application.prepared_temperature --config locations.json --output-dir OUTPUT_DIR --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
```

Equivalent PowerShell preparation and startup with the installed isolated environment:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$snapshot = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
& $python -B -m mesoforge.application.prepared_temperature --config locations.json --output-dir $snapshot --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
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

### Automatic current guidance

Normal preparation needs only the coordinate collection and an external output
folder. The batch command can perform the same preparation before immutable issuance,
using the existing PostgreSQL/MinIO settings. Acquisition never runs inside HTTP GET.
The output includes the actual prepared `directory` to pass to API startup:

```text
python -B -m mesoforge.application.prepared_temperature --config locations.json --output-dir EXTERNAL_PREPARED_ROOT
python -B -m mesoforge.application.batch_forecast --config locations.json --output-dir EXTERNAL_PREPARED_ROOT
python -B -m mesoforge.api --data-dir RETURNED_DIRECTORY
```

Choose either preparation alone or the batch command; the batch command already
prepares once for the whole collection. With an existing snapshot, use
`batch_forecast --config locations.json --data-dir RETURNED_DIRECTORY` to issue
another version without discovery or downloads. A repeated automatic run checks
providers again and creates a separate snapshot; `--data-dir` / `--from-raw` are
explicit offline reuse modes. Distant locations get separate spatial views of the
same retained full model messages, rather than separate model downloads.

Automatic selection snapshots execution time and uses its UTC whole-hour floor as
the target reference. Hours 1–36 therefore start at the next UTC hour. It considers
00/06/12/18Z HRRR extended runs and GFS runs newest first, independently for each
model, within the existing preparation limit of lead 48. Both selected cycles must
cover every requested valid time. Nominal cycle time only enumerates candidates;
it does **not** establish availability. Selection acquires the temperature message
for the final required lead first, then every other required lead, using the existing
provider URLs, inventories, byte-range checks and strict GRIB decoders. A missing
middle hour also rejects a cycle. Provider mirrors are tried before falling back
to an older cycle. Missing, corrupt or incomplete guidance is never filled or
renormalized. No complete usable pair means an explicit failure and no issuance.
If preparation reaches the first valid time, automatic issuance is refused and
requires a new run; it does not silently issue an elapsed hour as a new forecast.

The 128 MiB acquisition body budget still applies, including rejected candidates.
All acquired complete temperature messages and inventories remain outside Git.
`discovery/selection.json` records the candidates examined, failures and selection
reasons; each prepared manifest carries this evidence, exact cycles/leads, source
URLs/ranges, provider availability versus retrieval times, hashes and code identity.
Issued payloads retain the selection summary, per-source acquisition metadata, and
prepared-manifest checksum. Raw
rebuilding preserves the original selection and acquisition evidence. Fixed 70/30
weights remain demonstration weights, and the Phase 2 defaults are unchanged.

For reproducibility, supply **all three** explicit timestamps with either preparation
or `batch_forecast --output-dir`:

```text
python -B -m mesoforge.application.batch_forecast --config locations.json --output-dir EXTERNAL_FIXED_SNAPSHOT --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
```

These are command forms; the fixed historical example is not a claim of current
forecast availability. Use the isolated interpreter documented above, or the existing
`uv run --locked python` wrapper (wrapper unverified on this Windows checkout).

Automatic-mode demonstration on September 10, 2026: both discovered **18Z** cycles
were complete, so HRRR and GFS selected **2026-09-10 18Z**, leads **5–40**, against
reference **22Z**. Valid times were **September 10 23Z through September 12 10Z**.
All three locations had **36/36 nonmissing hours**, still future at issuance around
22:13Z. One acquisition downloaded **65,282,097 bytes** (63,420,870 raw temperature
bytes plus 1,861,227 inventory bytes). Three separate regions totaled **12,440,910
prepared NetCDF bytes**, reusing the same acquired model messages. No giant
cross-country region was prepared.

| Location | Coordinate | Immutable issued-forecast ID | First / last temperature (K) |
| --- | --- | --- | --- |
| Fresno | 36.7378, -119.7871 | `c347fa99-6200-4b5e-8960-8c0b9fcf380c` | 314.377960 / 294.946714 |
| Wichita | 37.6872, -97.3301 | `89f85924-3764-4792-9155-6c8e7da414a9` | 302.619608 / 296.541069 |
| Raleigh | 35.7796, -78.6382 | `e6456163-ba83-4a1a-b818-a1d56c50f811` | 304.502940 / 296.255264 |

The automatic batch command and actual localhost HTTP requests were executed with
the isolated interpreter. All **108** temperatures matched independent native-grid
interpolation/blending calculations. Issued payloads and acquisition provenance read
back exactly from PostgreSQL/MinIO; the seven older versions remained unchanged.
HTTP calculation and saved-version retrieval created no rows or objects. Offline
coverage reuse downloaded zero bytes; a raw-only rebuild reproduced the source
region's values and selection provenance. An initial demonstration-runner check
requested Fresno from a Raleigh-only rebuilt view and correctly failed for missing
coverage; checking the matching region passed without changing application code.
Evidence and full 36-hour responses remain outside Git under
`%LOCALAPPDATA%/MesoForge/baselines/20260910-automatic-cycles`.

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
