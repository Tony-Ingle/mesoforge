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
Proposed next milestone: calculate and persist one temperature verification result
linked to the exact issued version, observation revision, and matching policy.

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
$LASTEXITCODE # 1: the deliberately unsupported location failed; both others completed.
```

| Input order | Latitude, longitude | Actual result | Hour 1 / hour 36, K (rounded) |
| --- | --- | --- | --- |
| 0 | 45.8, -93.1 | 36 hours; none missing | 283.705085 / 298.287692 |
| 1 | 44.98, -93.27 | `unsupported_coordinate` | No forecast |
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
forecast-error calculation and verification persistence remain future work.

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
