# MesoForge

MesoForge builds a local gridded weather forecast for configured latitude/longitude
locations. The forecast is the **MesoForge field-specific blend**; native models
remain inspectable contributors and evidence. Current operation is on demand from
local commands, with immutable forecasts and numerical verification.

Read these documents in order:

1. [README](README.md): current capabilities, setup, commands and checks.
2. [VISION](VISION.md): canonical product direction and long-term principles.
3. [ARCHITECTURE](ARCHITECTURE.md): current technical structure and explicitly future stages.
4. [AGENTS](AGENTS.md): guardrails for changing the repository.

Current code defines current behavior. VISION defines the target product.
ARCHITECTURE reconciles them. Detailed [contracts](docs/data-contracts/phase-2.md)
and [durable decisions](docs/decisions/0001-python-modular-monolith.md) have narrower
roles; archived plans are history.

## What works today

MesoForge can discover available model cycles, acquire and retain real guidance,
prepare shared spatial coverage, and build a 36-hour numerical forecast for each
configured location. A background command materializes baseline domains before
location jobs run. Location jobs pin and extract that baseline; they do not
reblend fields, rerun baseline coherence or download guidance.

| Capability | Current behavior |
| --- | --- |
| Guidance | HRRR/GFS active contributors; RAP/IFS zero-weight shadows where supported; NBM active for selected fields |
| Prepared state | Immutable contributor snapshots and `latest_complete` |
| Numerical baseline | `FieldBlendEngine`, current coherence, immutable baseline snapshots and separate `latest_baseline` |
| Local domain | Current 7×7 grid at 6 km spacing, context/editable masks and exact configured center point |
| Issuance | Immutable PostgreSQL/S3-backed versions, exact readback and explicit reissues |
| Presentation | Hourly reports; deterministic conditions, transitions and period summaries from saved grids |
| Temperature verification | Automatic coordinate-driven METAR discovery/acquisition, matching, immutable facts and analysis |
| QPF verification | Bounded automatic MRMS accumulation before issuance, exact-hour facts, canonical samples and paired scoring |
| Prospective operator cycle | Current-clock discovery, background preparation/build, then one pinned baseline for configured issuance |

### Current numerical policies

The generalized infrastructure is implemented. Today's scientific recipes remain
**temporary scientific scaffolding**, not the finished MesoForge blend.

| Field or evidence | Current delivery/policy |
| --- | --- |
| Temperature | HRRR/GFS 70/30 demonstration recipe |
| Dew point, vector wind, gust, hourly liquid QPF | Existing approved field-specific lead policies and missingness behavior |
| Relative humidity | Bolton-based diagnostic from coherent blended temperature/dew point |
| PoP, total cloud/sky, thunder | Temporary active NBM products with native event semantics |
| Precipitation type | Temporary HRRR/GFS agreement policy; disagreement/ambiguity remains explicit |
| Native visibility, SWE, snowfall, Kuchera, SLR, freezing-rain liquid and ice | Evidence only where prepared; no invented delivered blend |
| RAP/IFS and additional probability sources | Shadow/evidence roles; missing or incompatible times remain explicit |

Wind is blended as components, not compass degrees. QPF retains exact accumulation
bounds. PoP is actual probabilistic guidance, not deterministic QPF converted into
probability. Native snowfall amount, SWE, snow depth and ice are distinct concepts.
The [architecture field inventory](ARCHITECTURE.md) describes source/policy limits.

The coherence framework executes only approved current rules. Registered future
relationships among QPF, PoP, thunder, p-type, snow, ice and visibility are **not**
new enforcement rules. Conditions do not promote evidence-only fields.

### What is not implemented

There is no continuously hosted guidance/baseline worker, scheduler or VPS deployment.
There is no applied learned site correction, AI forecast desk, delivery/email service,
adaptive production weighting or calibrated multi-source precipitation blend.
QPF accumulation runs on demand with configured issuance or explicit bounded
backfill; it is not a continuous MRMS poller or archive mirror.

This is a configured-location system, not an arbitrary-coordinate public API.
A newly configured coordinate or uncovered reference hour needs a background rebuild.
Retained legacy/development commands do not replace that normal baseline path.

## Development setup

Use **Python 3.12** and `uv`; [.python-version](.python-version),
[pyproject.toml](pyproject.toml) and [uv.lock](uv.lock) define the environment.

```text
uv sync --locked --all-groups
```

The locked dependencies include NumPy/xarray, Pint, pyproj, ecCodes/cfgrib/Herbie,
NetCDF/HDF5, PyArrow, Pydantic, SQLAlchemy/psycopg, boto3, FastAPI and Uvicorn.
Development tools include pytest, Hypothesis, Ruff, mypy and import-linter.

Commands below run from the repository root. Replace uppercase placeholders with
real paths/IDs; use directories **outside Git** for raw guidance, prepared state,
baselines, observations and generated reports. Do not commit credentials or data.

The examples use single-line `uv run --locked` commands to avoid shell-continuation
differences. Windows PowerShell sets variables with `$env:NAME = 'value'`; POSIX
shells use `export NAME='value'`. Direct `uv` commands do not require Make.
CI runs Ubuntu. Windows can use the locked environment and native services;
WSL/Docker is not required by the application. Availability of native dependencies
and server binaries still depends on the local environment.

### PostgreSQL and object storage

Issuance, persisted observations and verification need PostgreSQL plus an
S3-compatible object store. They use one existing storage path, not a local-file
history fallback. Numerical background preparation/baseline files are separate.

[Local Compose](deploy/local/compose.yaml) supplies PostgreSQL 16 and MinIO when
Docker is already available:

```text
docker compose -f deploy/local/compose.yaml up -d --wait
```

[.env.example](.env.example) lists development Compose/test settings. Copy it to
`.env` and edit local values if using Compose. Application Python reads process
environment variables; it does **not** automatically import `.env`.

Set these in the process that runs application commands:

| Variable | Purpose |
| --- | --- |
| `MESOFORGE_DATABASE_DSN` | SQLAlchemy `postgresql+psycopg://...` connection |
| `MESOFORGE_S3_ENDPOINT` | S3/MinIO endpoint, e.g. `http://127.0.0.1:59000` |
| `MESOFORGE_S3_BUCKET` | Application artifact bucket |
| `MESOFORGE_S3_ACCESS_KEY` | Object-store access key |
| `MESOFORGE_S3_SECRET_KEY` | Object-store secret key |

The application `MESOFORGE_S3_*` variables are distinct from the
`MESOFORGE_TEST_S3_*` settings in `.env.example`. Configure both deliberately when
using separate application and test stores. Read-only artifact commands expect the
bucket to exist. Apply database migrations before persistence:

```text
uv run --locked alembic upgrade head
```

Standalone PostgreSQL/MinIO also work. The integration fixture can start ephemeral
PostgreSQL through the `pgserver` development dependency when no test DSN is set;
MinIO still needs a running endpoint. No service is started by reading this guide.

Stop Compose without deleting retained volumes:

```text
docker compose -f deploy/local/compose.yaml stop
```

`make services-down` uses `down -v` and removes Compose volumes; do not use it to
preserve a development forecast history. Integration fixtures also drop schemas:
point them only at dedicated disposable test storage, never application history.

## Configure locations

The initial prospective registry is [configs/locations.json](configs/locations.json):

| ID | Name | Latitude | Longitude | Display timezone |
| --- | --- | --- | --- | --- |
| `minneapolis` | Minneapolis | 44.98861 | -93.25553 | `America/Chicago` |
| `surley` | Surley | 44.97304 | -93.20901 | `America/Chicago` |
| `grasston` | Grasston | 45.80268 | -93.07952 | `America/Chicago` |

An alternate file uses the same `{"locations": [{"lat": ..., "lon": ...}]}`
structure; pass its path with `--config`.

Latitude/longitude are the only required geographic inputs. IDs, names and
`display_timezone` are optional presentation metadata. Do not supply stations,
counties, bounding boxes or model grid coordinates. Spatial preparation inspects
the collection, shares suitable regions and separates distant regions internally.

The current baseline stores exact configured domains and covered reference-hour
views. Its 7×7/6 km geometry is an implementation default, not a permanent product
constraint. New domains are prepared in the background, never during HTTP GET.

## Run one prospective cycle now

With the existing PostgreSQL/object-storage settings configured, run from the
repository root:

```text
uv run --locked python -m mesoforge.application.prospective_cycle
```

This defaults to `configs/locations.json`. No date, source cycle, reference hour,
station or region argument is needed. The command reads the timezone-aware computer
UTC clock, refreshes shared contributor guidance, builds the numerical baseline,
then samples the clock again for location analysis. The reference time is that
current UTC hour, rounded down; background processing does not silently leave a
finished forecast anchored to an old manually supplied date.

The batch pins one exact published baseline for all configured locations. It
attempts prior temperature and QPF verification, then extracts and issues each
forecast through the existing baseline path. Observation failures remain explicit
and do not block issuance; a location failure does not stop later locations.
Location jobs do not download guidance or rerun blending/coherence.

The operator command composes the separate publication boundaries below. A failed
refresh preserves `latest_complete`; a failed build preserves `latest_baseline`
even if contributor publication advanced. It does not issue an uncovered or
fabricated current forecast. The existing advisory-lock
and decision-window checks prevent duplicate primary issuances. If every location
already has an issued version for the current reference hour and the current baseline is
valid, a repeat reuses that baseline, attempts verification and skips issuance
without preparing guidance again.

Use `--root RUNTIME_ROOT` to select retained guidance, baseline and report directories
outside Git. The default is the user-local MesoForge prospective directory
(`LOCALAPPDATA` on Windows; XDG data home on Linux). A compact console summary and
run `result.json` record timestamps, pinned identities and per-location outcomes.
Exit codes are 0 for a completed batch, 1 for isolated location failures, and 2
for a configuration/background/batch failure.
`--replay-reference-time ISO_UTC_HOUR` is an explicit replay/debug override, never
needed for normal operation.

The command is noninteractive and suitable for a future external scheduler invoking
the same command at **08:00 and 20:00 `America/Chicago`**, including daylight-saving
changes. Those times are an initial evidence-collection strategy, not forecast
science. No schedule, GitHub Actions workflow or hosted worker is installed.

## Prepare guidance, build the baseline, issue forecasts

The operator command above composes these three independently callable boundaries:

```text
refresh_guidance → prepared contributor state / latest_complete
build_baseline   → MesoForge numerical baseline / latest_baseline
forecast_from_baseline → pinned location forecast → optional immutable issuance
```

### 1. Refresh shared contributor state

This command accesses providers and can acquire substantial model data. It selects
current cycles from actual availability, validates required coverage and retains
raw/prepared provenance. It does not merely assume nominal cycles are ready.

```text
uv run --locked python -m mesoforge.application.refresh_guidance --config LOCATIONS_JSON --root GUIDANCE_ROOT
```

The default preparation window includes refresh grace beyond the 36 forecast
hours; `--coverage-hours` accepts 36–42. `--no-visibility` skips optional visibility
evidence. Required active inputs remain strict. Unavailable RAP/IFS discovery or
optional shadow acquisition remains explicit and does not promote another source.

`latest_complete` points to prepared **evidence**, not the delivered numerical
forecast. Publication is atomic and monotonic across processes; failed refreshes
preserve the previous published state. Source and attachment cutoffs remain separate.

### 2. Build the background numerical baseline

```text
uv run --locked python -m mesoforge.application.build_baseline --guidance-root GUIDANCE_ROOT --baseline-root BASELINE_ROOT --config LOCATIONS_JSON
```

This reads retained guidance without provider calls. It runs current field blends,
coherence/derivations and temporary field policies, then saves complete configured
domains/reference views and publishes `latest_baseline`. The default builds every
covered reference hour; repeat `--reference-time ISO_UTC_HOUR` to select exact views.

A new contributor snapshot may publish even if its baseline build fails. In that
case `latest_complete` advances while `latest_baseline` retains its previous good
forecast. Retry the background build; do not treat the two pointers as interchangeable.

### 3. Extract configured forecasts and optionally issue

Without `--issue`, this reads/extracts the pinned baseline and writes only requested
local report outputs:

```text
uv run --locked python -m mesoforge.application.forecast_from_baseline --root BASELINE_ROOT --config LOCATIONS_JSON --display-timezone America/Chicago --output-dir REPORT_DIR
```

With application storage configured, persist immutable versions:

```text
uv run --locked python -m mesoforge.application.forecast_from_baseline --root BASELINE_ROOT --config LOCATIONS_JSON --display-timezone America/Chicago --issue --output-dir ISSUANCE_REPORT_DIR
```

The default reference is the current UTC hour. `--reference-time ISO_UTC_HOUR`
selects an explicitly covered view; it does not create additional coverage.
A single location may use `--lat`, `--lon` and optional `--name` instead of `--config`.

The command pins one baseline for the whole batch. It does not load native guidance
or rerun field blending/coherence. Missing domain/reference coverage is an explicit
failure requiring background preparation. Location failures remain separate and do
not stop later configured locations.

With issuance enabled, a PostgreSQL advisory lock protects lookup/publication.
A coordinate/reference hour already issued is skipped. `--reissue` explicitly saves
another immutable version; it never overwrites the previous one. Baseline identity,
contributor-state lineage, source cycles and actual cutoffs travel with the forecast.

Before taking the issuance lock, the pinned-baseline command independently attempts
previous temperature and QPF verification for each location. Provider/verification
failures are reported under `previous_verification`; they do not block issuance or
later locations. Forecast extraction still performs no model acquisition/reblending.
The QPF default is a 72-hour lookback, at most 100 issuance reads and 36 unresolved
stage/hour attempts per location. Tune `--qpf-lookback-hours` (1–744) and
`--qpf-max-opportunities`; omitted work is reported. `--skip-verification` is an
explicit issuance-only replay option. Without `--issue`, no verification runs.
Recent decisions and valid hours take priority, so absent older MRMS files cannot
exhaust every forward run. For omitted historical work, narrow the explicit
backfill window or raise its bounds. Fully answered versions require no payload read.

`--output-dir` must be outside the repository. It retains full `result.json` and
readable `hourly-report.md`; stdout intentionally omits huge forecast payloads.
Use UTC timestamps with offsets. Display time zones do not change forecast science.

## Read saved forecasts and conditions

Saved issuance readback verifies immutable payload identity. Conditions, transitions
and periods derive presentation from those saved fields without rebuilding the
numerical forecast or writing history. Old issuances without saved grids remain
readable but cannot supply a grid-based conditions preview.

CLI previews use configured storage and need no HTTP server:

```text
uv run --locked python -m mesoforge.application.weather_conditions --issued-forecast-id ISSUED_ID
uv run --locked python -m mesoforge.application.weather_conditions --issued-forecast-id ISSUED_ID --scope editable
uv run --locked python -m mesoforge.application.weather_transitions --issued-forecast-id ISSUED_ID --display-timezone America/Chicago
uv run --locked python -m mesoforge.application.weather_periods --issued-forecast-id ISSUED_ID --display-timezone America/Chicago
```

Conditions default to the exact point; `--scope grid` includes the full grid.
They preserve known/unknown/ambiguous/unavailable/not-applicable states and exact
field intervals. Current versioned wording/transition/period policies are described
in [ARCHITECTURE](ARCHITECTURE.md). Evidence-only visibility does not imply fog.

### Local HTTP interface

The current localhost API serves a retained local-grid directory, **not** a
`latest_baseline` root. That remains a development/readback interface beside the
normal configured baseline command:

```text
uv run --locked python -m mesoforge.api --data-dir LOCAL_GRID_DIR --port 8765
```

`LOCAL_GRID_DIR` contains `local-grids.json`. For explicit development/replay, an
existing selected preparation can be converted before starting HTTP:

```text
uv run --locked python -m mesoforge.application.prepared_local_grid --config LOCATIONS_JSON --prepared-run PREPARATION_DIR --output-dir LOCAL_GRID_DIR
```

That development preparation **does** run numerical grid construction; it is not
part of a baseline-consuming location job. Omitting API `--data-dir` selects a
labeled synthetic temperature demonstration. It does not serve current real weather.
The server binds only to `127.0.0.1`; stop it with Ctrl+C.

| GET endpoint | Result |
| --- | --- |
| `/forecast?lat=44.98859&lon=-93.25557` | Prepared/grid point forecast; no issuance/history write |
| `/issued-forecasts/ISSUED_ID` | One exact saved version |
| `/issued-forecasts/ISSUED_ID/conditions` | Saved-grid point conditions; optional `?scope=grid` or `editable` |
| `/issued-forecasts/ISSUED_ID/conditions/transitions` | Read-only weather evolution |
| `/issued-forecasts/ISSUED_ID/conditions/periods` | Read-only period summaries |
| `/issued-forecasts/ISSUED_ID/observation-match?valid_time=ISO_TIME` | Retained temperature observation-match preview |
| `/issued-forecast-hours?lat=LAT&lon=LON&start_valid_time=START&end_valid_time=END` | Matching hours with separate issued versions |
| `/accumulation-status?lat=LAT&lon=LON` | Saved issuance/temperature-verification status |
| `/verification-analysis?lat=LAT&lon=LON` | Read-only temperature site analysis |

For example, open `http://127.0.0.1:8765/issued-forecasts/ISSUED_ID/conditions`
after replacing the ID. Retrieval endpoints require storage configuration. They do
not create missing history or recompute a missing forecast. There is no QPF-analysis
HTTP endpoint; use the explicit command below.

## Verify previous temperature forecasts

Automatic temperature verification selects saved eligible hours, discovers/reuses
nearby METAR stations, derives bounded observation requests and persists/reuses
verification facts. It requires application storage and may call observation providers.

```text
uv run --locked python -m mesoforge.application.automatic_verification --config LOCATIONS_JSON --start-valid-time 2026-09-14T12:00:00Z --end-valid-time 2026-09-14T17:00:00Z
```

Use `--lat LAT --lon LON` instead of `--config` for one location. The dates are
illustrative: use an eligible retained forecast window within provider availability.
Locations without eligible work do not download observations; reusable verification
results avoid unnecessary acquisition. Per-location failures do not stop the batch.

The current match uses suitable retained candidates within 50 km, temperature within
±15 minutes, available QC, nearest station, then time/station-ID tie-breaking.
A station is a recorded observation proxy, not the forecast coordinate itself.
Unavailable verification does not imply zero error or block a new numerical forecast.

Read-only canonical temperature analysis:

```text
uv run --locked python -m mesoforge.application.site_verification_analysis --lat 44.98859 --lon -93.25557 --display-timezone America/Chicago
```

Facts, primary/reissued versions, observation revisions and analytical samples are
not interchangeable counts. Model/recipe comparisons use matched samples; the
retained small demonstrations do not establish production weight superiority.

**Known METAR normalization defect:** legacy `P0000` precipitation is currently
normalized as numeric zero even though it means trace. Historical records are not
rewritten. That path is not the approved hourly-QPF verification reference.

## MRMS hourly-QPF accumulation and analysis

Normal baseline issuance attempts QPF accumulation beside the unchanged temperature
coordinator. The explicit retained-evidence commands remain available. None changes
forecasts, weights, PoP or precipitation type.

An incomplete hour is deferred. After its end, the documented approximate one-hour
MRMS Pass-2 latency determines earliest lookup, not guaranteed availability. HTTP
absence/provider failure stays retryable without an `observation_missing` fact.
Acquired native missing/no-coverage values remain explicit immutable evidence.
No availability timestamp is inferred from nominal latency.

For bounded historical work on saved issuances, specify every bound:

```text
uv run --locked python -m mesoforge.application.automatic_qpf_verification --config LOCATIONS_JSON --start-valid-time 2026-09-14T12:00:00Z --end-valid-time 2026-09-14T17:00:00Z --max-issuances 10 --max-opportunities 10 --archive
```

Bounds/caps are per location; times select hourly ends in `[start,end)`, and work
counts each unresolved issued-version/stage/hour. Baseline/final-issued stages stay
separate. The command includes read-only analysis. Narrow the window or raise an
explicit cap for reported omissions. Normal accumulation uses operational MRMS;
`--archive` explicitly selects historical retrieval. Set `MESOFORGE_MRMS_DIR` to an
external raw-cache root, or use the platform-local MesoForge observations directory.
A PostgreSQL hour lock shares retained sources across stages, versions and locations;
coordinate extractions and completed facts are reused. Conflicting retained MRMS
revisions require explicit review, not silent selection. Failed partial acquisition
keeps completed source packets for retry.

The approved reference is NOAA MRMS `MultiSensor_QPE_01H_Pass2`, contract
`mrms.multisensor-qpe-01h-pass2.v1`. NOAA product documentation defines indicated
time `T` as the end of the one-hour event **`(T-1h,T]`**; GRIB template 4.0 does not
encode those accumulation bounds. The nearest native grid point is an analysis
proxy for the configured coordinate, not exact point truth.

Retain one fixed MRMS hour and its gauge-influence/radar-quality evidence:

```text
uv run --locked python -m mesoforge.application.prepared_mrms --archive --time 2026-09-14T16:00:00Z --lat 44.98859 --lon -93.25557 --raw-dir MRMS_RAW_DIR
```

Use a new directory outside Git. `--archive` uses NOAA's public historical archive;
omitting it uses limited-retention operational files. Acquisition remains bounded
and explicit. The command prints the saved extraction artifact ID.

Reuse the raw bundle for another coordinate, or verify exact offline replay:

```text
uv run --locked python -m mesoforge.application.prepared_mrms --from-raw --lat 45.016 --lon -94.264 --raw-dir MRMS_RAW_DIR
uv run --locked python -m mesoforge.application.prepared_mrms --replay-artifact EXTRACTION_ID
```

Verify saved QPF opportunities against retained extractions; repeat
`--mrms-extraction` for additional hours/revisions:

```text
uv run --locked python -m mesoforge.application.issued_qpf_verification window --lat 44.98859 --lon -93.25557 --start-valid-time 2026-09-14T12:00:00Z --end-valid-time 2026-09-14T17:00:00Z --mrms-extraction EXTRACTION_ID --stage baseline --stage final_issued
uv run --locked python -m mesoforge.application.issued_qpf_verification analyze --lat 44.98859 --lon -93.25557 --start-valid-time 2026-09-14T12:00:00Z --end-valid-time 2026-09-14T17:00:00Z --stage baseline --stage final_issued --contributor HRRR --contributor GFS
uv run --locked python -m mesoforge.application.issued_qpf_verification read --verification-id QPF_FACT_ID
```

Selection uses hourly end times in `[start,end)`. Direct samples require the exact
same forecast/MRMS `(start,end]` event, a forecast issued by interval start, and
retained evidence available by verification cutoff. There is no ±15-minute matching,
interval redistribution or fabricated hourly IFS accumulation.

Numeric zero, positive amount, missing and no coverage remain distinct. Native
quality values are descriptive evidence; no quality rejection threshold is approved.
An opportunity is not a fact, and a stored fact is not automatically a sample.
Immutable revisions remain available; conflicting evidence is explicitly ambiguous.

Analysis keeps baseline/final-issued stages separate and reports exclusions,
canonical counts, bias/MAE/RMSE, totals, exact leads, provisional lead groups,
location/date concentration and compatible contributors on identical samples.
Current RAP/IFS hourly QPF is unavailable for this comparison. `--payload-only`
reads verified fact payloads instead of compact analytical attributes for an
independent audit. Acquisition, verification and read-only analysis are separate.

Per-stage `readiness` is a factual inventory: positive/zero samples, observed amount
distribution, locations/dates, exact/provisional leads, contributor coverage and
MRMS quality distributions. Positive means >0 mm; no additional measurable/heavy-rain
threshold or automatic readiness gate is approved. Date/time concentration is
reported without claiming hourly samples are independent storms or ranking models
on different sample populations.

## Repository map and development checks

| Path | Job |
| --- | --- |
| `src/mesoforge/application/` | Command/application orchestration and immutable artifact workflows |
| `src/mesoforge/forecasting/` | Field registry/dispatch, coherence and scientific/presentation kernels |
| `src/mesoforge/guidance/` | Provider adapters, normalized guidance and capability definitions |
| `src/mesoforge/observations/` | Station/METAR and MRMS source/normalization contracts |
| `src/mesoforge/verification/` | Matching, facts, canonicalization and read-only statistics |
| `src/mesoforge/storage/`, `provenance/`, `contracts/` | Durable metadata/payload boundaries and identities |
| `configs/`, `migrations/` | Current configuration inputs and PostgreSQL migrations |
| `tests/`, `scripts/`, `Makefile` | Existing checks, scientific contracts and command wrappers |

`forecast_from_snapshot`, `forward_run`, explicit selected-model preparation and
retained Phase 2 runners are development/replay/reference paths. `forward_run` still
combines verification with inline acquisition/blending; it is not the normal
baseline-consuming command. Historical paths remain readable but do not define the
current configured-location architecture. Inspect `--help` before using these tools.

### Offline checks

```text
uv run --locked pytest tests/unit tests/contracts tests/property -q
uv run --locked pytest -m scientific tests/unit tests/property -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src scripts
uv run --locked lint-imports
uv lock --check
uv run --locked python scripts/validate_docs.py
uv run --locked python scripts/check_repository_hygiene.py
git diff --check
```

[Makefile](Makefile) provides `make quality`, `make test`, `make scientific` and
`make phase2-offline`. `make sync` is not locked; prefer the setup command above.
Run relevant focused tests first. Tests alongside `field_blend`, `coherence`,
`baseline_snapshot`, `issued_qpf` and `mrms` cover their respective contracts.

### Service-backed and live checks

```text
uv run --locked pytest -m integration tests/integration -q
uv run --locked pytest -m integration tests/acceptance/test_phase2_multimodel_baseline.py -q
```

Set `MESOFORGE_TEST_DATABASE_DSN` for a dedicated disposable PostgreSQL database;
without it the fixture starts ephemeral pgserver. Set `MESOFORGE_TEST_S3_ENDPOINT`,
`MESOFORGE_TEST_S3_ACCESS_KEY`, `MESOFORGE_TEST_S3_SECRET_KEY` and the appropriate
`MESOFORGE_TEST_S3_BUCKET` for test object storage. Integration/acceptance fixtures
may delete their test data. Do not point them at retained operational evidence.

The retained Phase 2 acceptance workflow is heavier than offline scientific tests.
CI uses PostgreSQL 16, MinIO, migrations and coverage. `make test-all` and
`make coverage` include service-backed tests and are not offline-only shortcuts.
Migration downgrade/upgrade round trips belong only in disposable databases.

Live tests are opt-in: `MESOFORGE_LIVE_TESTS=1`, plus explicit
`MESOFORGE_LIVE_HRRR_CYCLE`, `MESOFORGE_LIVE_NBM_CYCLE` and
`MESOFORGE_LIVE_GFS_CYCLE` values (`YYYYMMDDTHH`), then
`uv run --locked pytest -m live tests/live -q`. They exercise narrow provider
contracts, not forecast skill or production readiness.

### Verification status of this guide

Command arguments were checked against current parsers. The prospective operator
milestone passed focused operator/snapshot/baseline/QPF checks and 25
PostgreSQL/MinIO integration tests, including immutable readback, location/verification
failure isolation and repeat-run storage identity. Ruff, formatting, mypy, import
contracts, lock consistency, documentation/links and repository hygiene were checked.
Examples remain usage instructions, not a claim that their chosen paths, archive
hours or local services exist.

A real computer-clock prospective run refreshed current guidance and issued all
three configured locations against one baseline, with checksum-verified 36-hour
readback. The same-hour repeat skipped all three issuances without provider
acquisition or changes to PostgreSQL rows/MinIO objects. Both verification fields
reported `nothing_to_verify` at these exact coordinates; the new forward hours had
not matured. Temporary validation services were stopped afterward.

Known broader-suite failures carried forward from the previous code checkpoint are
one batch mock-signature failure and two identifier/code-revision inventory failures.
All three reproduced at earlier checkpoints. The current broader offline run had
3,529 passes and exactly those three failures. The previously corrected Windows
MRMS-cache rename regression did not recur.
The three pre-existing failures remain unwaived and unchanged. Check current test results
when changing code; a saved commit does not certify the whole application.

## Data attribution

ECMWF data is used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
and [ECMWF attribution terms](https://apps.ecmwf.int/datasets/licences/general/):
**This service is based on data and products of the European Centre for Medium-Range
Weather Forecasts (ECMWF).** MesoForge transformations and source/licence metadata
remain in retained provenance. No ECMWF endorsement is implied.
