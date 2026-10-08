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
prepare shared spatial coverage, and build a 36-hour or explicitly selected
120-hour numerical forecast for each configured location. A background command materializes baseline domains before
location jobs run. Location jobs pin and extract that baseline; they do not
reblend fields, rerun baseline coherence or download guidance.

| Capability | Current behavior |
| --- | --- |
| Guidance | Historical/default 36-hour policies remain available; explicit 120-hour policy uses compatible HRRR/RAP/GFS/IFS/NBM native guidance |
| Prepared state | Immutable contributor snapshots and `latest_complete` |
| Numerical baseline | `FieldBlendEngine`, current coherence, immutable baseline snapshots and separate `latest_baseline` |
| Local domain | Current 7×7 grid at 6 km spacing, context/editable masks and exact configured center point |
| Issuance | Immutable PostgreSQL/S3-backed versions, exact readback and explicit reissues |
| Presentation | Hourly reports; deterministic conditions, transitions and period summaries from saved grids |
| Temperature verification | Automatic coordinate-driven METAR discovery/acquisition, matching, immutable facts and analysis |
| QPF verification | Bounded automatic MRMS accumulation before issuance, exact-hour facts, canonical samples and paired scoring |
| Prospective operator cycle | Current-clock discovery, background preparation/build, then one pinned baseline for configured issuance |
| Hosted operation | One image; a polling guidance/baseline worker and a scheduled forecast/issuance worker over PostgreSQL and S3-compatible storage |
| Learning stages | Explicit local temperature correction/no-op, immutable candidate policy data, background shadow overlays and one temperature/QPF variant evaluator |
| Policy governance | Append-only lifecycle events; explicit register/evaluate/activate/rollback/retire; deterministic identical-sample eligibility; nothing is active by default |
| AI forecast desk | Always attempted after correction; bounded structured provider actions, deterministic field edits, validated checkpoints and automatic fallback |
| PDF/email | Deterministic two-page 36-hour or declared 120-hour outlook and explicit SMTP delivery from saved final issuances |
| Retention | Verified-backup-bound rolling native payload expiry, case pins and protection of unresolved dependencies |

### Current numerical policies

The explicit 120-hour path uses `mesoforge.provisional-multimodel-120h.v1`: transparent
field/lead/model-role priors, native-horizon and freshness eligibility, smooth tapers,
and renormalization over eligible contributors. These weights are **provisional,
not skill-optimized or verification-derived**. Temperature, dew point, vector wind,
gust, QPF and total cloud have separate policy identities. RH uses current coherence.
Compatible state fields use adjacent native endpoints; gust retains its native
instantaneous times. QPF uses exact contiguous hourly or coarser accumulation events,
never invented hourly splits. Six-hour PoP uses compatible native NBM/GEFS events
with separate meta-model/ensemble priors (2:1), not hourly probability interpolation.
ECMWF ENS24-hour probabilities retain their different threshold and window as evidence.
Hourly PoP/thunder and p-type preserve their current
event/agreement contracts and become unavailable where unsupported. Native source
values, eligibility, missingness and applied weights remain in each saved grid.
See the [source/field audit and policy](ARCHITECTURE.md#36-hour-presentation-and-explicit-email-delivery).

The following table describes the retained **36-hour policy family**:

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

The hosted worker roles, image, Compose stack and scheduler units exist (see
[Hosted deployment](#hosted-deployment)). Supervised Linux and real Minneapolis
commissioning have completed; unattended operation remains disabled.
No correction or blend policy has been activated. There is no automatic delivery,
adaptive production weighting or calibrated multi-source precipitation blend.
The explicit PDF/SMTP command supports saved 36-hour and declared 120-hour outlooks.
The legacy five-complete-calendar-day renderer retains its separate coverage gate. Only an explicitly activated governed temperature correction changes values
in the correction stage. No approved promotion rule exists for blend, QPF or AI
policies, so those families are never eligible. The operational AI desk may make
bounded edits to this forecast; it cannot promote policies or change persistent
blend science.
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
Windows can use the locked environment and native services;
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

Set `MESOFORGE_DATABASE_DSN` and the four `MESOFORGE_S3_*` settings in the process
that runs application commands; every variable is listed once under
[Environment variables](#environment-variables).

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

The maintained prospective registry is [configs/locations.json](configs/locations.json):

| ID | Name | Latitude | Longitude | Display timezone |
| --- | --- | --- | --- | --- |
| `minneapolis` | Minneapolis | 44.98861 | -93.25553 | `America/Chicago` |
| `grasston` | Grasston | 45.80268 | -93.07952 | `America/Chicago` |

The current hosted rollout selects Minneapolis only through the existing alternate
location file (`--config` on both workers). Grasston remains in the maintained
registry. Surley is no longer an operational target; its historical artifacts and
verification records remain valid and are not removed.

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
Location jobs do not download guidance or rerun baseline blending/coherence. The
local learning stage is a no-op without an ACTIVE governed correction; a changed
temperature reruns only the existing affected T/Td/RH consistency/diagnostic rules.
The governed state is read before any background work and once per batch at the
request time; if it cannot be read, the cycle fails with
`failed_phase: governance_resolution` and issues nothing.
Every new configured forecast then attempts the bounded AI desk. Its latest fully
validated state drives conditions, transitions, periods and immutable issuance.
No justified edit, missing provider configuration, timeout or provider failure
retains the last valid state; an AI stage is recorded only when the model acted. The raw baseline and corrected stage remain separate
immutable controls for later identical-observation evaluation.

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

The command remains a one-shot composition for development and recovery. Hosted
operation splits it into the two worker roles under [Hosted deployment](#hosted-deployment),
whose forecast role is scheduled at **08:00 and 20:00 `America/Chicago`**, including
daylight-saving changes. Those times are an initial evidence-collection strategy, not
forecast science.

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

The build resolves ACTIVE governed blend policies once at its analysis cutoff and
executes them only through `FieldBlendEngine` overrides; the manifest pins the
resolved heads and full policy JSON in `blend_governance`. With none active (today)
the fields and `field_policies` are unchanged. A governance read failure fails the
build and retains the previous baseline. `--without-governance` is a development
build recorded as `not_configured`: it cannot replace a governed `latest_baseline`,
and configured issuance refuses it.

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
another immutable version; it never overwrites the previous one.
Issuance never proceeds under an unproven governed state. The batch status is
`governance_unavailable` when governance cannot be read,
`baseline_governance_unproven` when the pinned baseline lacks resolved blend
governance (including baselines built before governance existed), and
`baseline_governance_revoked` when a blend it pinned was rolled back. Each of these
issues nothing, calls no provider and exits 2. Each location's issuance transaction
re-checks, under the shared governance locks held until commit, that no correction or
blend it pinned was rolled back since resolution; otherwise that location is refused
with `policy_rolled_back_before_issuance` and the next cycle issues it under the
restored state. Baseline identity,
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

## Learning stages and shadow comparisons

The normal prospective path now records a deterministic correction stage after
local extraction. **No policy / insufficient evidence is a normal no-op.** There
are no promoted corrections or real blend candidates in the shipped configuration;
the deterministic stage and current weights remain unchanged without an active
correction. The subsequent AI desk is a separately traceable per-forecast stage.

For v1, normal jobs retain verification and read-only site evidence but do **not**
automatically create or register persistent correction candidates. Reports identify
this as `policy_generation: operator_only`. Learning/Governance, explicit proposals,
registered shadows and ACTIVE governed policy resolution remain available unchanged.
Verification, AI and immutable control/corrected/final stages continue normally.

Temperature proposals reuse canonical site evidence independently in the 1–6,
7–18 and 19–36 hour buckets: at least 30 samples, 10 UTC decision dates, no date
above 25% of samples, and a decision-date-clustered 95% bias interval excluding
zero. Qualified evidence permits a candidate, never promotion. Positive
forecast-minus-observation bias gives a negative candidate correction. QPF has
comparison infrastructure, **not** a new correction or weight proposal.
Raw-baseline correction proposals exclude errors from already transformed
temperature stages. Their final-issued verification facts remain valid for stage
evaluation. The legacy contributor-comparison command directs adjusted issuances
to the unified evaluator rather than treating the corrected value as raw HRRR/GFS.

Policies and compact stages use the existing PostgreSQL/S3 artifact store. Inspect
or register an explicit immutable definition, or attach read-only numerical stages
to an existing issuance without rewriting it:

```text
uv run --locked python -m mesoforge.application.learning register-policy --file POLICY_JSON
uv run --locked python -m mesoforge.application.learning read --artifact-id ARTIFACT_ID
uv run --locked python -m mesoforge.application.learning stage-issued --issued-forecast-id ISSUED_ID
```

Storing a policy does not make it execute. An operator-created proposal stays PROPOSED.
Only a governance `register` event (below) makes a candidate shadow, and only an
explicit `activate` event makes it operational. `learning stage-issued` never
consults governance and binds only an unchanged no-op stage, so no policy is
applied retroactively. The former `--learning-policies` JSON selector has been
removed; payload lifecycle roles never select execution.

Registered blend candidates build their overlays in the background of each
prospective cycle. The explicit command accepts only candidates registered at the
cutoff, without acquiring guidance or changing active fields:

```text
uv run --locked python -m mesoforge.application.learning build-candidate --baseline-root BASELINE_ROOT --policy-id POLICY_ARTIFACT_ID
```

A shadow cannot change active issuance. Every governed correction shadow and blend
projection attempt is recorded with the issuance binding (`stored`,
`candidate_failed` or `storage_failed`), so a candidate is not judged only where it
succeeded. Background overlay build failures are reported in the cycle result. Policy evidence, creation,
registration/activation and storage availability must precede the applicable
forecast analysis cutoff. A policy learned today cannot silently enter an earlier
decision, and a shadow failure does not block the active forecast.

Compare saved stages against the same canonical observation events:

```text
uv run --locked python -m mesoforge.application.learning analyze --field air_temperature_2m --lat 44.98861 --lon -93.25553 --start START_UTC --end END_UTC
uv run --locked python -m mesoforge.application.learning analyze --field liquid_equivalent_precipitation_amount_1h --lat 44.98861 --lon -93.25553 --start START_UTC --end END_UTC
```

Use an actual saved valid-time window, with end exclusive. Optional repeated
`--variant-id ARTIFACT_ID` arguments bound the comparison. The evaluator reports
shared sample count, bias/MAE/RMSE, metric differences, exact/provisional leads,
locations/date concentration and exclusions. Temperature retains its station-proxy
contract; QPF requires the exact same MRMS hourly interval and revision. Missing
variants exclude that event from the common comparison population. There is no
overall winner score or automatic promotion. Operational AI stages use this same
evaluator; no separate AI observation population or evaluation system exists.

## Policy governance

Persistent scientific behavior changes only through explicit, append-only
governance events in PostgreSQL (`governance_events`). MesoForge learns, evaluates
and determines eligibility automatically, but no forecast job, AI desk or provider
can register, activate, roll back or retire a policy. Three families are governed:

| Family | Scope | Lifecycle |
| --- | --- | --- |
| `temperature_correction` | Exact configured coordinate | register → evaluate → activate → rollback/emergency rollback → retire |
| `blend_policy` | Field | Mechanism only: no approved blend/QPF promotion rule, so never eligible; temperature recipes never activate |
| `ai_desk_policy` | Global | Explicitly recorded code-versioned desk versions; register/retire only, never eligible or activated |

Commands are read-only by default and print JSON. Errors, including invalid
arguments, print `{"error": {"code", "message"}}` to stderr and exit 2:

```text
uv run --locked python -m mesoforge.application.governance status [--family FAMILY]
uv run --locked python -m mesoforge.application.governance candidates
uv run --locked python -m mesoforge.application.governance history --policy-artifact-id POLICY_ID
uv run --locked python -m mesoforge.application.governance resolve --family temperature_correction --lat LAT --lon LON --at ISO_UTC
uv run --locked python -m mesoforge.application.governance evaluate --policy-artifact-id POLICY_ID --information-cutoff ISO_UTC
uv run --locked python -m mesoforge.application.governance show-evaluation --evaluation-id EVALUATION_ID
```

State changes need exact IDs, an actor and a reason:

```text
uv run --locked python -m mesoforge.application.governance register --policy-artifact-id POLICY_ID --actor NAME --reason TEXT
uv run --locked python -m mesoforge.application.governance evaluate --policy-artifact-id POLICY_ID --information-cutoff ISO_UTC --record --actor NAME --reason TEXT
uv run --locked python -m mesoforge.application.governance activate --policy-artifact-id POLICY_ID --evaluation-id EVALUATION_ID --expected-head gev_...|genesis --actor NAME --reason TEXT
uv run --locked python -m mesoforge.application.governance rollback --expected-head gev_... --target art_...|none --actor NAME --reason TEXT
uv run --locked python -m mesoforge.application.governance emergency-rollback --expected-head gev_... --actor NAME --reason TEXT
uv run --locked python -m mesoforge.application.governance retire --policy-artifact-id POLICY_ID --actor NAME --reason TEXT
uv run --locked python -m mesoforge.application.governance register-desk-version --actor NAME --reason TEXT
```

Semantics:

- **Time.** The database clock stamps every event after a transaction-scoped
  per-family lock. Effective intervals are derived and half-open: a chain event
  (activation or rollback) is in force from its `recorded_at` until the next one.
  Readers take the shared lock and require the decision time to be no later than the
  database clock, so a job at time T and a later `resolve --at T` agree exactly.
  Every governed stage seals its `governance_resolution` (head event, sequence and
  decision time), and issued history is never re-resolved.
- **Evaluation.** `evaluate` requires an explicit information cutoff. It compares the
  candidate's prospective shadow stages with the raw CONTROL stages on their own
  identical canonical samples, decided after registration and at or before the
  cutoff. It reports total, common and excluded samples (with reasons and IDs),
  decision and valid dates, lead buckets, and wet/non-wet composition. For
  temperature, wet/non-wet is reported as not available: no approved precipitation
  join exists. Unrelated sparse series never shrink the cohort.
- **Eligibility** (`mesoforge-governance-eligibility.v1`). For temperature corrections
  every rule must hold:
  - the candidate digest reproduces from its own evidence;
  - every applied lead bucket meets the unchanged `mesoforge-bias-evidence-policy.v1`
    (all criteria) on the prospective raw errors;
  - MAE and RMSE are both strictly lower than CONTROL in every applied bucket;
  - unapplied buckets are unchanged;
  - no candidate-caused shadow failure occurred;
  - no correction was ACTIVE at the cutoff.

  The improvement is labelled descriptive, not a significance claim, and the paired
  interval is reported only. A candidate cannot replace an active policy without an
  audited rollback first. Every other family is `not_eligible` with
  machine-readable reasons. The evaluation artifact is deterministic and clock-free.
- **Activation** names the exact candidate and recorded eligible evaluation and the
  expected current head event (`genesis` when nothing was ever active). The command
  re-runs the evaluation at the same cutoff and refuses on:
  - changed evidence;
  - a stale rule, evidence policy or code identity;
  - a changed head (compare-and-set on event IDs, safe against ABA);
  - a retired or unregistered candidate.
- **Rollback** is a new event: to `none` or to a previously active, non-retired policy.
  **Emergency rollback** restores the policy the current activation superseded and
  is refused when the head is itself a rollback. **Retirement** is refused for the
  active policy, and a retired policy never shadows or activates again.
- **Idempotency.** A request key excludes actor/reason; a retried identical request
  returns the existing event, and a reused key with another actor/reason is refused.
  PostgreSQL constraints and a trigger enforce contiguous per-scope sequences,
  predecessor heads, registration/retirement rules and artifact availability even
  for a writer that skips the service. No row is ever updated or deleted.
- **Failure.** Configured issuance is refused when the governed state cannot be
  proven (above). Development/replay issuance (`forecast_from_snapshot`,
  `forward_run`, batch issuance) is refused for a coordinate with an ACTIVE
  correction, or while any blend is ACTIVE, with
  `governed_policy_active_use_baseline_path`.
- **AI.** AI performance is compared against each AI stage's exact deterministic
  corrected parent, per runtime series, labelled conditional on the retained AI
  stage, over `[desk registration or --window-start, cutoff]`, with earlier issuances
  counted. Promotion state never gates desk attempts; only an unprovable governed state
  stops a configured job before any stage. The negative final review stays a
  versioned desk behavior; changing it requires a desk-policy version bump and a new
  explicit registration. Registered historical desk versions remain evaluable.

## Bounded operational AI forecast desk

Normal `prospective_cycle` and configured `forecast_from_baseline` jobs always
attempt the desk after deterministic correction. There is no off/shadow/active
mode switch. Missing credentials, an unavailable provider or a failed first request
produce an explicit desk outcome and issue the complete corrected forecast; no AI
stage claims a model decision that did not happen. Lower-level tests use
deterministic providers, remove any operator `MESOFORGE_AI_*` settings and
credential, and fail if the real transport is reached.

The current concrete adapter uses OpenAI Responses with strict structured output.
Configure an explicit model supporting structured outputs; the coding agent's
model or login is never selected as the runtime weather provider:

```text
MESOFORGE_AI_PROVIDER=openai
MESOFORGE_AI_MODEL=gpt-6-sol
OPENAI_API_KEY=<set through the runtime environment or secret manager>
```

The current operator-selected meteorologist is `gpt-6-sol`; model selection remains
runtime configuration rather than a dependency of the forecast/domain code.
After changing Windows User environment variables, start a new terminal/runtime
process so it inherits them. Credentials are never read from locations JSON.

Optionally set `MESOFORGE_AI_REASONING_EFFORT` to `none`, `minimal`, `low`,
`medium`, `high`, `xhigh` or `max` (case-insensitive); a value the chosen model does
not accept fails that request with a sanitized HTTP code and the job falls back.
The adapter otherwise omits the setting. The output-token ceiling includes any
reasoning tokens; an incomplete response is a failed attempt, not an edit.
An invalid runtime setting (unknown provider, unparsable price or budget, unknown
effort) ends the attempt as `configuration_invalid`, naming the variable but never
its value.

Do not put credentials in locations JSON, forecast artifacts or Git. No weather
model download is needed to exercise the desk on an already retained baseline.
The adapter permits only inference at its fixed endpoint, with no hosted tools,
redirects, retries, arbitrary URLs or runtime shell/filesystem/database access.
See the [official structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs).

The default desk target is 600 seconds, hard analysis ceiling 900 seconds,
finalization reserve 60 seconds and individual provider timeout 60 seconds. Caps
are 20 provider calls, 20 inspection calls, 12 proposals, 6 accepted edits and
3 edits per field; assessment and final review each run at most once. Context
is bounded to 64 KiB, each inspection to 8 KiB, each response to 2,048 output
tokens and total usage to 300,000 tokens. The preflight counts one request byte as
one token, and a configuration must fit two worst-case requests. These are versioned
execution controls, not forecast science or a requirement to use the allowance.
Reaching the target or the last permitted call moves the desk to its final review
inside the hard window. The configured job holds the issuance lock only for the
decision-window lookup and the locked recheck-and-publish, not during the desk.
`MESOFORGE_AI_MIN_PROVIDER_INTERVAL_SECONDS` paces request starts for an account's
rate limit (default 0). The current development account uses 65 seconds, a 4 KiB
inspection cap (`MESOFORGE_AI_MAX_TOOL_OUTPUT_BYTES=4096`) and 300,000 total tokens;
these are account-specific operator settings. Waiting consumes the same analysis
budget; it never retries a rejected request or extends the deadline.

Budget overrides use `MESOFORGE_AI_` plus the upper-case `DeskConfig` field,
for example `MESOFORGE_AI_TARGET_SECONDS`, `MESOFORGE_AI_MAX_PROVIDER_CALLS`
or `MESOFORGE_AI_MAX_TOTAL_TOKENS`. The policy version is `1`.
An optional `MESOFORGE_AI_MAX_COST_USD` cap requires explicit
`MESOFORGE_AI_INPUT_USD_PER_MILLION` and `MESOFORGE_AI_OUTPUT_USD_PER_MILLION`
prices. Unknown pricing cannot satisfy a cost cap and prevents a paid request.
Usage and configured-price estimates are reported separately from billed invoices.
Validated usage is retained even when a response is incomplete or rejected.
Unavailable usage after transport failure/timeout is explicitly unknown, not a
claim of zero billable consumption. HTTP failures, including 429 quota or rate
limits, end the attempt as `provider_failure` and retain only the status and an
allowlisted error code, never response bodies or credentials.

Currently editable fields are temperature (bounded additive K adjustment) and
hourly liquid QPF (add, scale, conservative spatial smoothing, optional edge taper).
Tool policy v1 caps per-proposal temperature additions to ±5 K, QPF additions to
±10 kg/m², scaling to 0–2 and smoothing strength to 0–1. These are intervention
permission limits, not claimed scientific improvement. A temperature edit that
would make an available dew point or RH missing is rejected, not clamped. Taper
weight falls to zero at the editable-domain edge, so on the current 3×3 editable
domain only the centre node changes under a taper. Wind, dew point, probability,
cloud, thunder, p-type, visibility and winter/ice fields remain inspect-only; there
is no temporal QPF retiming or new precipitation-family coherence rule.

The context gives each field's availability, range, point extremes, compatible
contributor spread (or `no_comparable_pairs`), per-contributor point QPF timing,
taper edge distances and cutoff-proven verification facts. Six inspection tools
(`summarize_field`, `inspect_baseline`, `inspect_contributors`,
`inspect_disagreement`, `inspect_verification_history`, `inspect_dependencies`)
accept a field, pinned valid times, a region and optional cell ids. A wrong-phase or
malformed action, an exhausted inspection or per-field budget, or a repeated,
inverse or out-of-scope edit is a counted rejection rather than the end of the
analysis. A final review with `accepted=false` discards all accepted edits.

The report retains compact context identity, finite task priorities, inspection
evidence, proposals, rejection reasons, ordered accepted recipes, checkpoint
references, completion state and time/token/call usage. Recipes replay without
another model call, and the AI stage verifies the digest chain from the corrected
parent to the issued grid plus the edit scope before retention. No hidden
chain-of-thought is requested or retained. Checkpoints store only their new recipe
and a link to the previous checkpoint, never a grid copy; the issued forecast
carries a compact desk summary while the full report lives in the AI stage.

On 2026-09-25 a real `gpt-6-sol` run (reasoning effort `low`, 65-second pacing,
10-call cap) replayed the Minneapolis 2026-09-18 00 UTC reference from a baseline
built offline from already retained prepared guidance; no guidance was downloaded.
The desk assessed a 17.7 mm point QPF event, prioritized QPF, inspected contributor
disagreement at the forecast point for three hours, judged it peak-timing
disagreement rather than a supported correction, and made no edit; its final review
accepted the unchanged forecast. It used 5 provider calls, 1 inspection, 59,071
input and 519 output tokens and 264 desk seconds, 245 of them pacing. The job
retained raw, corrected and AI stages, issued the forecast and bound all three for
evaluation. A following batch without a credential skipped the issued Minneapolis
hour and issued Surley and Grasston through the explicit provider-unavailable
fallback. This demonstrates runtime and lineage, not forecast skill.

AI cannot alter its own rules, tool permissions, blend weights or correction
policies, and has no governance tool. Its desk versions are only recorded explicitly
and can never be activated or made eligible.

## Hosted deployment

One repository, one codebase and one image run two process roles against shared
PostgreSQL and S3-compatible storage. An external scheduler decides only **when**
the forecast role runs; all meteorology stays in MesoForge. The artifacts are the
[Dockerfile](Dockerfile), [deploy/hosted/compose.yaml](deploy/hosted/compose.yaml)
and the scripts and units beside it. There is no web UI, public API, Kubernetes or
per-forecast approval.

| Role | Command (`python -m ...`) | Lifecycle |
| --- | --- | --- |
| Guidance/baseline worker | `mesoforge.application.guidance_worker run` | Long-running, restarts automatically |
| Forecast/issuance worker | `mesoforge.application.forecast_worker run --scheduled` | One run per scheduler trigger |
| Operator commands | `mesoforge.application.operations <command>` | One-shot `admin` container |

### Guidance/baseline worker

Every `--interval-seconds` (default 300) one poll composes the existing workflows:

1. Compare the database revision with the repository head. A mismatch
   (`schema_not_at_head`) stops the poll; the worker never migrates. An unreachable
   database still permits guidance refresh but defers builds.
2. **Refresh** (existing `refresh_guidance`) when there is no prepared state, when the
   current UTC reference hour or the next two hours (`--coverage-margin-hours`) are no
   longer usable, when a configured coordinate lies outside the prepared footprint, in
   the UTC hour before a scheduled slot (fresh NBM/HRRR/GFS for issuance), or when the
   hourly discovery probe (existing `select_model_set`, metadata only) finds a newer
   HRRR/GFS cycle. That refresh reuses the probe's selection instead of rediscovering.
   Otherwise nothing is downloaded (`no_material_change`). Missing RAP/IFS shadows or
   NBM PoP are tolerated exactly as the refresh workflow already tolerates them.
3. **Build** (existing `build_baseline` with governed blend resolution) when the latest
   baseline is missing, pins an older prepared snapshot, lacks resolved blend
   governance, was rolled back (`blend_revoked`), pins different governed blend heads,
   uses another/unproven code revision, or lacks a configured coordinate the prepared
   snapshot covers. Each build-input
   fingerprint (prepared snapshot and digest, coordinates, code revision, governed
   heads) suppresses repeated deterministic failures; a transient failure backs off
   and retries. Interrupted builds (watchdog, memory limit, forced stop) have two
   total attempts. A previously successful fingerprint does not prevent recovery of
   a missing or obsolete publication. Restart also recovers orphaned in-flight
   fingerprint markers.
4. Build overlays for registered blend candidates on the current baseline (existing
   Learning Core background; already retained overlays are reused).
5. Record readiness, guidance and next-slot coverage in `status/guidance-worker.json`.

Refreshes are bounded: at most two attempts and one success per UTC hour, none
starting after minute 40 (`--latest-start-minute`; discovery and preparation must
finish inside their hour), a free-space floor (`--min-free-gb`, default 6, category
`disk_low`), and backoff `min(cap, max(interval, base * 2^(n-1)))` from 5 minutes to
1 hour. Counters, fingerprints and the in-flight phase persist across restarts; an
interrupted phase counts as a failure at the next start. A failed refresh or build
never replaces `latest_complete` or `latest_baseline`. SIGTERM/SIGINT end the poll at
the next phase boundary (Compose allows 20 minutes for a running refresh or build); a
second signal exits at once and the abandoned phase is recorded. A watchdog exits a
phase or poll that exceeds its bound so the restart policy recovers the process. Only
an unexpected internal error slows the poll cadence; a database outage or an
unmigrated schema keeps polling at the normal interval so recovery is noticed
promptly. The refresh-start gate and stop signal are checked again after a discovery
probe. One process may own a runtime root (`worker_busy` otherwise). Baseline and
candidate-overlay builds also require the local free-space floor; unknown capacity
fails closed.

`--refresh off` builds from the retained `latest_complete` only (recovery; no
downloads). `--no-hourly-probe` refreshes only for coverage, footprint and pre-slot
reasons: fewer downloads, less current intermediate guidance. `once` runs one poll
with the same busy heartbeat and phase/poll watchdog bounds as `run`, then exits;
it does not schedule another poll. `status` prints the state file; `health` exits 0/1;
`discover` runs a read-only availability probe and deletes its evidence (`--keep`
retains it under `discovery/manual-*`; the worker clears only its own
`discovery/worker/` scratch).

### Forecast/issuance worker

`run` loads the configured locations, applies the `--scheduled` gate (outside a slot
window it prints `not_due` and exits 0), takes a PostgreSQL run lock (a scheduled
trigger waits for it inside its window and exits 2 with
`run_lock_busy_until_window_closed` if another run held it throughout; a manual
overlapping run exits 0 as `already_running`), checks the schema head, the bucket
(never created here) and governance, then reads the latest baseline pointer **once** and
checks readiness with the same rules issuance applies. The pinned pointer, one clock
sample and lookup-only candidate overlays go to `forecast_from_baseline`, which
performs prior verification, extraction, correction, the always-attempted AI desk,
validation, presentation and issuance per location. One location's failure never
stops the next; the existing locked lookup turns a repeated trigger into
`skipped_already_issued`, never a second issuance. It never refreshes guidance.
Before entering issuance it checks local runtime free space (`--min-free-gb`,
default 6); low or unreadable capacity returns an infrastructure failure.

With `--scheduled`, readiness is rechecked every minute until the slot window closes
(the window is clipped to the slot's UTC hour, so the reference hour cannot change).
Without a ready baseline the run exits 3 (`baseline_not_ready`) with the reasons.
Exit codes: 0 completed, already issued, not due or already running; 1 some location
failed; 2 infrastructure, configuration or governance failure; 3 no ready baseline.
Each run writes structured JSON events to stderr, `runs/forecast-<time>-<id>/result.json`
and `status/forecast-worker.json`, including each location's AI desk outcome and a
network-free desk configuration check. `readiness` and `next-run` are read-only;
`--reference-time` (explicit replay) and `--skip-verification` are recovery options.

### Readiness, health and status

Health is process health only: the guidance worker's heartbeat is fresh and no phase
exceeds its bound. Corrupt or future heartbeat timestamps fail health explicitly.
Docker health status alone does not restart a container; the worker's watchdog
terminates a stuck process so its restart policy can act. Readiness is separate and
never an age threshold: the pinned
publication verifies (`load_baseline`), timestamps precede the request, blend
governance is resolved and not revoked, the request's reference hour is covered and
at least one configured coordinate has a domain (others fail per location). Hosted
readiness also requires the baseline's recorded code revision to match the running
image; older manifests without that proof require a background rebuild. Historical
readers remain compatible. Both
report the baseline and contributor-state IDs, publication and build times,
information cutoff, reference views, contributor cycles, age and reasons.

`operations status` (human text, `--json` for machines) answers: is the worker
healthy; is guidance current and which cycles it holds; which baseline is pinned and
when it was built; which governed policies are active; the last forecast run and each
location's AI desk outcome; recent refresh/build failures; the latest issuance per
location; and the next scheduled run. Other commands: `migration-status`,
`migrate --expect-database NAME`, `apply-grants --expect-database NAME`,
`init-storage`, `baseline`, `issuances`, `export-objects --destination DIR`,
`validate-export --source DIR`, `check-empty-storage`, and `import-objects --source DIR`.
Imports validate the entire export before any object writes. Governance stays an explicit CLI, run through the admin
service (`docker compose run --rm admin mesoforge.application.governance ...`); the
worker database role cannot append governance events, and nothing is served over HTTP.

### Once-daily Minneapolis delivery — disabled until owner enablement

The [daily workflow](.github/workflows/mesoforge-daily.yml) composes existing workers
through [daily_cycle.py](deploy/hosted/daily_cycle.py). It schedules **one** deliberate
Guidance `once` at **06:05 America/Chicago**, forecast preparation at **07:15**, and
SMTP release **not before 08:00**. The measured five-day Guidance cycle took roughly
50 minutes; 70 minutes before Forecast leaves headroom. Forecast itself took about
29 minutes plus saved-issuance delivery reads, so starting Forecast at 08:00 would
not achieve morning 08:00 delivery. These are operator times, not scientific rules.
Slow builds, verification, providers or SMTP can make delivery late; no arrival SLA
is implied. Late preparation uses the actual current reference hour and sends a valid
completed forecast when ready. Native coverage and first-valid-hour expiry checks
still apply; a missed reference cannot be backdated or trigger another paid attempt.
For late runs, `forecast_min_remaining_minutes` defaults to 40 minutes: if less of
the current reference hour remains, wait once until the next hour **before** readiness
and paid work. This operational headroom reflects measured runtime, not meteorology;
the final actual-clock expiry check still prevents an expired issuance.

[GitHub's schedule contract](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
supports IANA timezone/DST scheduling and warns that dispatch can be delayed or
dropped. The workflow uses `timezone: America/Chicago`, runs on the existing `vps`
self-hosted runner, and becomes schedulable only when present on the **default branch**
(`main`). Feature-branch presence does not enable a schedule. There is no hourly
Guidance loop, cron or systemd timer in this v1 daily path. Legacy systemd examples
remain alternatives only; **do not install them alongside GitHub Actions**.

Both scheduled and manual `run` require repository variable
`MESOFORGE_DAILY_ENABLED=true`; this change does not set it. Leave it unset or false.
Manual `status` is read-only
and permitted while disabled. Default-branch and repository guards also apply.
The workflow never builds/deploys an image, migrates schema or enables policies.

Before enablement, build/deploy one exact committed daily-capable image through the
existing procedure, keeping the **existing live project, database and volumes**.
Set `MESOFORGE_DAILY_IMAGE` in its deployment environment, and provision
`/etc/mesoforge/daily.json` from [daily.json.example](deploy/hosted/daily.json.example).
Use the base Compose file plus [daily.compose.yaml](deploy/hosted/daily.compose.yaml),
not old proof overlays that clear AI credentials or select Grasston. Replace example
paths; approve the explicit `mesoforge-120-hour-presentation.v1` template.
Create the configured private `state_root` outside the checkout, writable by the
runner operator. Preserve that directory in backups: it contains daily retry receipts.
Credentials remain in the existing live environment, `/etc/mesoforge/ai.env` and
`/etc/mesoforge/email.env`, with role-specific exposure. Each canonical location row
owns an `email_recipients` list: zero, one or many plain addresses. Domains are
normalized and exact duplicates collapse in configured order. An empty list allows
issuance/PDF but skips SMTP. Minneapolis is configured for the owner; Grasston has
an empty list and remains unscheduled. Change this configuration, not forecast code.
A failed recipient does not prevent remaining recipients or backup of the issuance.

The runner derives a Minneapolis-only selection from `configs/locations.json` and
gives all roles the same selection/root. Grasston remains in the maintained registry;
all historical locations remain untouched. The daily overlay selects 120h explicitly;
historical/default 36h behavior is unchanged.

```text
python3 deploy/hosted/daily_cycle.py --config /etc/mesoforge/daily.json status
python3 deploy/hosted/daily_cycle.py --config /etc/mesoforge/daily.json run
gh workflow run mesoforge-daily.yml --ref main -f operation=status
gh workflow run mesoforge-daily.yml --ref main -f operation=run
```

The GHA Actions UI provides the same `workflow_dispatch` choices. `run` uses the
computer clock and today's local date; no date argument or backfill is supported.
It refuses heavy work before 06:05 or across a changed local day, preserves phase
logs/receipts outside Git, and reports IDs, cycles, verification, AI outcome, PDF
digest, delivery audit and sampled host memory/swap/load/disk in the job summary.
Container memory peaks are not collected by this wrapper.

Guidance stops before Forecast. A persistent host lock and the workers' shared
runtime OS lock serialize heavy work; GitHub concurrency does not cancel a running
job. The wrapper also refuses an already-running Guidance/Forecast container in
the live project. Compose sets `MESOFORGE_WORKER_LOCK_ROOT` to the shared volume root,
so new-image manual workers using different sub-roots also serialize. Do not start
legacy images that lack this lock contract. The handoff requires successful Guidance,
a 120h baseline prepared/published this morning, coverage for
the forecast slot, normal readiness and the **exact pinned baseline ID**. Current
same-morning eligible state may be reused; previous-day fallback is deliberately
disallowed. Failed Guidance leaves old good pointers/history intact and skips delivery.
Shadow-candidate failure alone remains a reported warning, not an active forecast blocker.

Completed phases are reused for the local day. Existing issuance and delivery locks
remain authoritative. A crashed/incomplete forecast attempt requires operator
inspection of its saved worker record; the runner will not repeat uncertain paid
work. Email-only retries read the saved issuance/PDF; they never reacquire or invoke
AI. Accepted deliveries are reused. An ambiguous or failed durable SMTP intent stays
suppressed pending explicit reconciliation, never automatically resent. AI failure
uses the existing validated fallback; email failure never invalidates issuance.

Enable only after storage/local recovery checks below and explicit recurring
AI/email authorization. Set `MESOFORGE_DAILY_ENABLED=true`; to disable new runs,
unset it or set `false`. Do not cancel an in-flight scientific phase merely to disable
tomorrow's run. One normal forecast/day means approximately **365 bounded AI desk
jobs/year**, with existing request/token/time limits and no implied dollar estimate.

### Deploying the stack

Prerequisites: one Linux host with Docker Engine and Compose v2.24 or newer, a Git
checkout, outbound HTTPS, and enough disk on a filesystem dedicated to Docker volumes.
No inbound port needs to be opened.

```text
cp deploy/hosted/.env.example deploy/hosted/.env      # fill in generated secrets
cp deploy/hosted/ai-settings.env.example deploy/hosted/ai-settings.env # optional non-secret AI settings
sh deploy/hosted/build-image.sh                       # set MESOFORGE_IMAGE to the tag
docker compose -f deploy/hosted/compose.yaml build minio # pinned upstream source build
make hosted-migrate DB=mesoforge                      # explicit: migrate, then create bucket
make hosted-status
```

`hosted-migrate` starts only PostgreSQL and MinIO, runs `operations migrate` through
the owner credentials, verifies the head and applies the worker grants (after any
other migration path, run `operations apply-grants`); `hosted-up` refuses to start the
worker unless `migration-status` reports the head. It is the manual continuous-worker
alternative; do not use it for daily v1 operation. Workers never migrate, so two processes
can never race a migration. To upgrade: build the new commit, back up, stop the
guidance worker, migrate, then use the daily one-shot path; run both roles from one
image tag (overlay identities include the code identity).

Set `MESOFORGE_AI_PROVIDER` and `MESOFORGE_AI_MODEL` in `deploy/hosted/.env` (or
the invoking shell); defaults are `openai` and `gpt-6-sol`. Compose's explicit
environment entries override those two names in environment files. Keep non-secret
reasoning effort, pacing, pricing and budget overrides in `ai-settings.env` or an
existing Forecast service environment override; application defaults are unchanged.

Provision the provider credential separately in the deployment-level host file
`/etc/mesoforge/ai.env`, containing `OPENAI_API_KEY`. Forecast alone reads it through
Compose `env_file`; Guidance and Admin receive neither the file nor its values.
`MESOFORGE_AI_SECRET_FILE` may select another host path without putting its contents
in Compose interpolation. The invoking operator must be able to read the file;
use restrictive ownership/permissions (for example, `root:<operator-group>`, file
`0640`, directory `0750`). The file is not mounted into the container or included
in images, runtime data or backups. Missing credentials retain the existing
provider-unavailable fallback; verify presence separately before a paid run.
Do not duplicate the key into the project directory, `.env`, build arguments or
logs, or print expanded `docker compose config` output. A runtime check should emit
only whether `OPENAI_API_KEY` is non-empty, plus the non-secret provider/model.
When upgrading an older deployment, move only its non-secret desk settings from
the old project `ai.env` into `ai-settings.env`; the old file is no longer read.

The former MinIO image is no longer anonymously pullable. Compose now builds
[the upstream security release](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z)
from an exact commit using [the auxiliary Dockerfile](deploy/hosted/minio/Dockerfile).
This preserves the S3 contract and volume layout. [Upstream is archived and
unmaintained](https://github.com/minio/minio); this is a supervised first-deployment
option, not a maintenance guarantee. Both images and the Compose stack have been
exercised on Debian 12 with Docker 29.7.2 / Compose 5.5.0 using isolated synthetic
data; this does not establish live-provider capacity or unattended readiness.

The application image retains the locked ecCodes/eckit and Psycopg binaries but
uses native local library loading instead of globally preloading every eckit
library. Global preloading caused a Linux interpreter-shutdown crash when ecCodes
loaded before Psycopg. Two non-root, fresh-process GRIB/libpq checks in the image
build cover both import orders and require normal process exit.

### Storage, backups and retention

| Volume | Contents |
| --- | --- |
| `postgres-data` | Issuance metadata, verification facts, artifacts, governance events |
| `minio-data` | Content-addressed issuance, stage and verification payloads |
| `runtime` (`/var/lib/mesoforge/runtime`) | `guidance/` snapshots and `latest_complete`, `baseline/` baselines and `latest_baseline`, `runs/`, `status/`, `observations/` |

Both workers mount `runtime` at the same absolute path (baselines record absolute
prepared paths; publication locks are local), so the stack runs on one host.
Native development artifacts with different absolute source paths cannot simply be
copied into this volume and treated as portable hosted baselines. Nothing
is deleted automatically: baselines, issuances, governance history and verification
evidence are permanent. A prepared snapshot is 1.1–1.8 GB and a failed refresh
leaves about 1 GB; expect several refreshes per day. The worker reports free space
and stops refresh/build/issuance admission below its floor. This is an admission
check on the runtime filesystem, not a reservation or a remote PostgreSQL/S3 capacity
guarantee; monitor those volumes and backup space separately.

The explicit retention planner defaults to a dry-run:

```text
uv run --locked python -m mesoforge.application.guidance_retention --runtime-root RUNTIME --dry-run
uv run --locked python -m mesoforge.application.guidance_retention --runtime-root RUNTIME --pin SNAPSHOT_ID --reason "Retain this research case"
uv run --locked python -m mesoforge.application.guidance_retention --runtime-root RUNTIME --unpin SNAPSHOT_ID
```

Defaults retain HRRR/RAP/NBM's newest four complete usable cycles, GFS/IFS three,
and acquired GEFS/ECMWF ensemble generations three. Configure `retention_cycles` in
`daily.json`, or `--keep-hrrr`, `--keep-rap`, `--keep-gfs`, `--keep-ifs`, `--keep-nbm`,
`--keep-gefs`, `--keep-ecmwf-ens` on the operator command. These are storage settings,
not weights. Whole bundles stay protected if any contributor is inside its window.

`--status` and `--dry-run` show KEEP/DELETE/PINNED/CURRENT/RECOVERY/IN-FLIGHT/UNRESOLVED
with reasons, bytes and exact candidate digests. `--apply --backup-receipt RECEIPT`
requires a verified same-host backup containing the exact plan and candidate bytes.
It rechecks the protection graph under the shared worker lock, records deletion intent,
and expires only recognized raw GRIB/index/prepared-array payloads outside all windows.
An interrupted apply resumes only that exact protected/digested plan. All original JSON
manifests, source documents, baseline grids and immutable scientific history remain.
Current/previous prepared states and baselines, in-flight jobs, manual case pins and
unresolved cross-generation dependencies prevent deletion. Links/path escapes fail closed.

Native re-preparation of expired payloads requires a retained recovery copy or source
reacquisition; it is no longer promised indefinitely. Saved baseline/issuance readback,
point verification and AI recipe lineage retain their exact existing data. Pin research
cases **before** cleanup. Failed/incomplete generations can contain reusable scientific
state and remain UNRESOLVED; age alone never makes them disposable. Runner files,
PostgreSQL/MinIO volumes, observation evidence and arbitrary temp directories are not
visited. No automatic persistent Learning or new feature store is introduced.

**Complete storage is not a fixed-size working set yet.** Historical baseline grids
remain necessary for full-grid controls/candidate inheritance/AI recipe replay. Point
stage summaries cannot replace them safely. The measured baseline cost is about 393 MB
per daily build, plus roughly 60 MB per issuance, observations and verification. Recent
raw/prepared cycles are bounded where references permit; those permanent artifacts
continue growing. Legacy temperature facts also retain their authoritative large payload.
Do not advertise indefinite disk capacity or delete those dependencies to meet a floor.

The central [disk policy](src/mesoforge/application/disk_admission.py) reports normal
above30 GiB, warning at20–30 GiB and refusal below20 GiB. The same defaults apply before
acquisition, baseline writes, issuance persistence and backup creation. Configure
`MESOFORGE_GUIDANCE_MIN_FREE_GB`/`MESOFORGE_GUIDANCE_WARN_FREE_GB` in the deployment and
host environment when overriding. Guards never delete protected data. They measure the
local filesystem, not capacity reserved on an external database/object service.

The daily pipeline calls [backup.sh](deploy/hosted/backup.sh) after delivery attempts.
It uses the existing `pg_dump -Fc`/`pg_restore --list`, checksummed object export and
complete runtime archive; retains daily receipts and non-secret deployment metadata;
and holds the shared worker lock across creation and validation. The
preflight estimates the complete recovery copy without assuming compression savings;
it refuses work if that copy would cross the central disk reserve. Interrupted copies
stop only their own named client/export containers, never PostgreSQL or MinIO. The
[`local_backup`](src/mesoforge/application/local_backup.py) validator rereads archive
bytes, verifies every exported object and proves planned deletion bytes exist in the
archive before publishing `local-backup-receipt.json`. Failed backup means no pruning.
Secrets stay in deployment-level files and are never copied into recovery manifests.

Standalone `sh deploy/hosted/backup.sh DEST` still requires an operator maintenance
window with external triggers suspended; it pauses/restores an existing Guidance loop
and refuses a running Forecast worker. Private `.incomplete` sets never count as good
backups. Restore never overwrites live state. The daily recovery root keeps the newest
two validated sets and the current set; only unchanged sets created by this receipt
contract can expire. Legacy backups, unknown additions and incomplete sets are retained
for explicit inspection. This does not prune other backup directories.

Same-host recovery is the owner-approved v1 mechanism. It **does not protect against
loss of the VPS**. Off-host backup is a future hardening recommendation, not a v1
scheduling prerequisite; no paid storage service or off-host credentials are required.
Manual Docker cleanup must classify exact MesoForge-only images/volumes first, retain
current/rollback images and preserve runner data. No global cache/system prune is used.

[restore.sh](deploy/hosted/restore.sh) requires stopped workers, an empty database,
runtime volume and bucket, and the same database owner and bucket name as the backup.
It validates the checksums, archive and every exported object before restoring rows.
The database restore is transactional; the combined PostgreSQL/S3/runtime restore is
not atomic. A later failure requires inspection and a fresh empty destination before
retrying. Older backups without the completion/checksum contract are rejected.
Shell failure tests cover these guards. The supervised Linux proof also ran these
scripts against meaningful isolated state and restored into new PostgreSQL, object
store and runtime volumes with identical database rows, object digests and issuances.

### Security

No service publishes a host port. PostgreSQL and MinIO sit on an internal network;
only the workers also reach the internet. Workers log in as a member of the
[`mesoforge_runtime` group role](deploy/hosted/postgres-init/worker-role.psql), whose
table grants `operations migrate` applies: SELECT and INSERT, UPDATE of activity status
only, read-only governance events, and no persistent DDL, DELETE, TRUNCATE or trigger
changes. Temporary tables are permitted by the database grant.
Only the `admin` application role holds the owner credentials (PostgreSQL itself
also needs its initialization credentials); for an external S3 endpoint admin
also needs the egress network. The OpenAI key reaches only the forecast
worker (the deployment-level `/etc/mesoforge/ai.env`). MesoForge application
containers run as uid 10001 with all
capabilities dropped; PostgreSQL uses its upstream initialization/runtime identity. The
image contains no credentials and records its commit. Logs and status files redact
credential values, DSN passwords, API tokens and signed-URL authorization values. For a bucket-scoped object-store
credential, create a MinIO user with get/put/list on the bucket only (no delete) and
use it as `MESOFORGE_S3_ACCESS_KEY`/`MESOFORGE_S3_SECRET_KEY`. If the root pair is
used to bootstrap the bucket, replace the application credentials before starting
workers. Bucket-scoped IAM provisioning is an operator step, not automated by Compose;
the example's root bootstrap option does not enforce least privilege. The development HTTP
interface (`mesoforge.api`) is not part of the hosted stack.

A real VPS deployment needs operator-supplied inputs that this repository does not
assume: the provider and host, its Linux distribution, SSH access, disk capacity, the
generated secrets and optionally an OpenAI key. DNS names and public ports are not
needed.

### Saved-forecast PDF and email delivery

Daily v1 uses the **120-hour outlook** described below. The historical/default
presentation product remains **MesoForge 36-Hour Weather Outlook**.
The two-page server-side ReportLab renderer reads the saved final issuance, including
validated AI edits or the recorded deterministic fallback. It presents the exact
36-hour window, hourly trends, interval QPF and covered-period summaries; a partial
local day is never presented as a complete daily forecast.

**Five-day means 120 elapsed hours.** Select the provisional policy in background
preparation with `MESOFORGE_FORECAST_HORIZON_HOURS=120` (Guidance Worker) or
`refresh_guidance --forecast-hours 120 --coverage-hours 126`. The extra six
hours are a bounded reference buffer, not a longer delivered product. The default
remains 36 hours so an existing rollout does not change implicitly. Forecast Worker
extracts the declared saved baseline; it never reblends or extends source horizons.

Use `--product 120-hour` on both render and send for that saved issuance. The PDF
shows five elapsed 24-hour periods with local start/end labels; DST and partial local
calendar days are explicit. Temperature extrema are sampled forecast extrema.
Canonical QPF events are summed only when completely contained: an event crossing
a card boundary makes that card's complete total unavailable rather than distributing
rain artificially. Maximum available hourly PoP is not a daily probability.

The separate legacy `--product 5-day` fixture renderer still requires five complete
local calendar days, including 23-/25-hour DST days. Neither renderer can turn a
36-hour issuance into five days. The default delivery product remains 36 hours.

These commands read an existing issuance. They cannot acquire guidance, reblend,
invoke AI, reissue a forecast or change stored forecast state:

```text
uv run --locked python -m mesoforge.application.forecast_delivery render --issued-id ISSUED_UUID --location grasston --pdf OUTSIDE_REPOSITORY/grasston.pdf
uv run --locked python -m mesoforge.application.forecast_delivery send --issued-id ISSUED_UUID --location grasston --pdf OUTSIDE_REPOSITORY/grasston.pdf --recipient recipient@example.com --confirm-reviewed
uv run --locked python -m mesoforge.application.forecast_delivery status --delivery-id sha256:DELIVERY_DIGEST
```

`--config` optionally selects the existing location-registry format. Render verifies
the final saved grid digest and exact point, then derives covered-period hourly-sample extrema,
vector-mean wind, maximum gust, consecutive-interval QPF, most frequent hourly conditions
and transitions. Maximum hourly PoP is labeled as such, never called a daily probability.
Missing fields remain unavailable. Charts include overnight hours. No externally hosted
fonts, logos or assets are used. The PDF contains compact issue/timezone/AI/revision
provenance, not internal paths, identifiers or prompts.

Before `send`, visually review both pages. The command verifies two readable pages,
real guidance identity, unexpired coverage and byte identity with a fresh deterministic
render of the same saved issuance. Fixture/unknown-source PDFs cannot be emailed by this
command. Rendering and sending are independent; an email failure cannot roll back issuance.

The explicitly approved daily template uses `--approved-template
mesoforge-120-hour-presentation.v1` instead of claiming a new human visual review
each morning. The version must match the saved document policy and every PDF still
passes deterministic content/byte validation. `--not-before` accepts an aware UTC
instant, prepares the validated attachment first, then holds SMTP for at most 60 minutes
and rechecks expiration. A template-version change requires fresh operator approval.

Copy [email-settings.env.example](deploy/hosted/email-settings.env.example) to
`deploy/hosted/email-settings.env` for non-secret SMTP settings. Securely provision
`MESOFORGE_SMTP_USERNAME` and `MESOFORGE_SMTP_PASSWORD` in the MesoForge-specific
`/etc/mesoforge/email.env` (restricted owner permissions, outside repository/image/runtime).
Do not supply secrets in chat or copy another application's credentials. Compose's
explicit `delivery` profile alone reads this file; Guidance, Forecast and Admin do not.
The delivery service has worker-scoped storage credentials and no AI secret or public port.
For a deployed compatible image, `docker compose run --rm delivery
mesoforge.application.forecast_delivery ...` invokes the same command; place its output
under `/var/lib/mesoforge/runtime/delivery/` or an explicit operator output mount.
Provision the SMTP secret before using `send`; never borrow another application's
credentials. PDF rendering requires no SMTP credentials.

STARTTLS with certificate verification is the default; implicit TLS is supported.
Unauthenticated plaintext is allowed only for a loopback test server. Timeout is capped
at 60 seconds. One recipient is sent per command. A PostgreSQL advisory lock and durable
content-addressed intent precede SMTP. Issuance + recipient + document-policy identity
suppresses duplicate attempts even after a crash or ambiguous DATA response. The compact
audit retains intent/result, attachment digest/size and sanitized status/code; it excludes
SMTP credentials and raw server text. SMTP `250` means server acceptance, not inbox proof.
An intent without a result is ambiguous: inspect/reconcile with the provider; do not
blindly retry or bypass the key. Automatic resend and recurring delivery are not enabled.

### Environment variables

Application commands read the process environment; they never import `.env` files.

| Variable | Used by | Purpose |
| --- | --- | --- |
| `MESOFORGE_DATABASE_DSN` | All persistence | SQLAlchemy `postgresql+psycopg://...`; the worker role in the hosted stack |
| `MESOFORGE_ALEMBIC_DSN` | Migrations only | Owner DSN; falls back to `MESOFORGE_DATABASE_DSN` |
| `MESOFORGE_S3_ENDPOINT` | Object storage | S3-compatible endpoint (`http://minio:9000` in the hosted stack) |
| `MESOFORGE_S3_BUCKET` | Object storage | Application bucket; workers never create it |
| `MESOFORGE_S3_ACCESS_KEY` | Object storage | Access key identifier |
| `MESOFORGE_S3_SECRET_KEY` | Object storage | Secret key |
| `AWS_DEFAULT_REGION` | Object storage | boto3 region (`us-east-1` for MinIO) |
| `MESOFORGE_PROSPECTIVE_ROOT` | Workers, cycle, operations | Runtime root (`guidance/`, `baseline/`, `runs/`, `status/`) |
| `MESOFORGE_WORKER_LOCK_ROOT` | Guidance, Forecast, retention pins | Optional shared lock root; hosted Compose sets the runtime volume root to serialize different sub-roots |
| `MESOFORGE_GUIDANCE_MIN_FREE_GB` / `MESOFORGE_GUIDANCE_WARN_FREE_GB` | Guidance / daily host admission | Free-disk GiB floor/warning (20/30); no automatic deletion |
| `MESOFORGE_OBSERVATIONS_DIR` | Temperature verification | Retained METAR/station evidence root |
| `MESOFORGE_MRMS_DIR` | QPF verification | Retained MRMS evidence root |
| `MESOFORGE_OBSERVATIONS_ARTIFACT_ID` | Observation preview | Set internally during verification; optional explicit preview input |
| `MESOFORGE_CODE_REVISION` | Code identity | Commit baked into the image; otherwise `git rev-parse HEAD` |
| `MESOFORGE_FORECAST_TIMEZONE` | Both workers | Slot time zone (default `America/Chicago`) |
| `MESOFORGE_FORECAST_HORIZON_HOURS` | Guidance worker | Explicit scientific horizon: `36` (legacy default) or `120` (provisional five-day policy); prepares a six-hour reference buffer |
| `MESOFORGE_FORECAST_TIMES` | Both workers | Comma-separated local `HH:MM` slots (default `08:00,20:00`) |
| `MESOFORGE_AI_PROVIDER` | AI desk | `openai` |
| `MESOFORGE_AI_MODEL` | AI desk | Model, currently `gpt-6-sol` |
| `MESOFORGE_AI_SECRET_FILE` | Hosted Compose | Host credential file; default `/etc/mesoforge/ai.env`, Forecast only |
| `OPENAI_API_KEY` | AI desk | Provider credential (forecast worker only) |
| `MESOFORGE_AI_REASONING_EFFORT` | AI desk | Optional effort |
| `MESOFORGE_AI_INPUT_USD_PER_MILLION` | AI desk | Optional price; `..._OUTPUT_USD_PER_MILLION` pairs with it |
| `MESOFORGE_AI_<FIELD>` | AI desk | Optional `DeskConfig` budget override (see the AI desk section) |
| `MESOFORGE_SMTP_HOST` | Explicit delivery | SMTP server hostname |
| `MESOFORGE_SMTP_PORT` | Explicit delivery | Port, default `587` |
| `MESOFORGE_SMTP_SECURITY` | Explicit delivery | `starttls` (default), `tls`, or test-only `local_plaintext` |
| `MESOFORGE_SMTP_USERNAME` | Explicit delivery | Optional authentication username, paired with password |
| `MESOFORGE_SMTP_PASSWORD` | Explicit delivery | Secret; provision outside repository/runtime |
| `MESOFORGE_SMTP_TIMEOUT_SECONDS` | Explicit delivery | Network timeout, default 30; maximum 60 |
| `MESOFORGE_EMAIL_FROM` | Explicit delivery | Plain sender mailbox; recipient is a command argument |
| `MESOFORGE_EMAIL_SECRET_FILE` | Hosted Compose | Delivery-only secret file; default `/etc/mesoforge/email.env` |
| `MESOFORGE_DELIVERY_MEM_LIMIT` | Hosted Compose | One-shot delivery memory limit, default `3g` |
| `MESOFORGE_IMAGE` | Compose | Image tag for every role |
| `MESOFORGE_PG_DB` | Compose | Database name |
| `MESOFORGE_PG_USER` | Compose | Owner role (migrations, admin) |
| `MESOFORGE_PG_PASSWORD` | Compose | Owner password |
| `MESOFORGE_PG_WORKER_USER` | Compose | Least-privilege worker role |
| `MESOFORGE_PG_WORKER_PASSWORD` | Compose | Worker role password |
| `MESOFORGE_MINIO_ROOT_USER` | Compose | MinIO server root user |
| `MESOFORGE_MINIO_ROOT_PASSWORD` | Compose | MinIO server root password |
| `MESOFORGE_GUIDANCE_MEM_LIMIT` | Compose | Guidance worker memory limit (default `8g`) |
| `MESOFORGE_FORECAST_MEM_LIMIT` | Compose | Forecast worker memory limit (default `6g`) |

The development Compose file and tests use `MESOFORGE_PG_PORT`, the MinIO port
settings and `MESOFORGE_TEST_*` variables from [.env.example](.env.example); live
tests use `MESOFORGE_LIVE_*`. Never point test variables at retained operational data.

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
| `Dockerfile`, `deploy/hosted/` | The one image, hosted Compose stack, scheduler units and backup scripts |
| `tests/`, `scripts/`, `Makefile` | Existing checks, scientific contracts and command wrappers |

`forecast_from_snapshot`, `forward_run`, explicit selected-model preparation and
retained Phase 2 runners are development/replay/reference paths. `forward_run` still
combines verification with inline acquisition/blending; it is not the normal
baseline-consuming command. Historical paths remain readable but do not define the
current configured-location architecture. Inspect `--help` before using these tools.
They issue nothing while a governed policy is ACTIVE for the coordinate (or any blend
is ACTIVE), and nothing when governance cannot be read.

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
GitHub Actions checks are not configured; the validation commands above remain
available for explicit operator use. `make test-all` and
`make coverage` include service-backed tests and are not offline-only shortcuts.
Migration downgrade/upgrade round trips belong only in disposable databases.

Live tests are opt-in: `MESOFORGE_LIVE_TESTS=1`, plus explicit
`MESOFORGE_LIVE_HRRR_CYCLE`, `MESOFORGE_LIVE_NBM_CYCLE` and
`MESOFORGE_LIVE_GFS_CYCLE` values (`YYYYMMDDTHH`), then
`uv run --locked pytest -m live tests/live -q`. They exercise narrow provider
contracts, not forecast skill or production readiness.

### Verification status of this guide

The original hosted milestone reported a retained-guidance process demonstration
against PostgreSQL 16 and moto: a 21-view, 342 MB baseline built in 775 s and three
replay issuances in 117 s, plus a 3.6 MB metadata probe. These are the original
measurements, not independently remeasured results from the deployment review.

The independent review used fresh dedicated PostgreSQL 16.2 and native MinIO
services. It exercised real worker grants, governance protections, immutable
issuance/idempotency and digest-verified object export/import. Shell tests with a
Docker stub and real tar/checksums cover backup failures, incomplete publication and
restore preconditions. The review's final offline run passed 3,981 tests with the
same three failures independently reproduced at its starting revision (the batch
mock signature and two typed-boundary inventory checks). All 150 integration and
acceptance tests passed against the dedicated services.

The subsequent supervised Debian 12 Docker proof built both images, migrated an
isolated database, checked worker privileges and produced a Linux-native synthetic
baseline for the three locations configured at that time. Twelve domain/reference
views took 204 s and retained 10.3 MB; the three-location issuance took 36.8 s and the repeat
skipped all three in under a second. Concurrent workers respected the database lock.
The proof exercised service outages/restarts, corrupt pointers, schema/revision
checks, unavailable AI and fake HTTP 429 fallback without paid calls.

The actual backup/restore scripts preserved 12 database tables, 24 verified objects
(162.4 MB), five readable synthetic issuances, verification records and register/retire
governance history in fresh volumes. Three issuances were the configured worker
batch; two were separate historical verification fixtures. No policy was activated.
Native systemd unit validation and both America/Chicago DST transitions passed;
no timer was installed or enabled. These are fixture deployment measurements, not
real-weather verification, a live guidance memory benchmark or authorization for
unattended operation. Test object storage used bootstrap credentials; a live stack
still needs the bucket-scoped application credential described above.

Command arguments were checked against current parsers. The prospective operator
milestone passed focused operator/snapshot/baseline/QPF checks and 25
PostgreSQL/MinIO integration tests, including immutable readback, location/verification
failure isolation and repeat-run storage identity. Ruff, formatting, mypy, import
contracts, lock consistency, documentation/links and repository hygiene were checked.
Examples remain usage instructions, not a claim that their chosen paths, archive
hours or local services exist.

A real computer-clock prospective run refreshed current guidance and issued the
three locations configured at that time against one baseline, with checksum-verified
36-hour readback. The same-hour repeat skipped all three issuances without provider
acquisition or changes to PostgreSQL rows/MinIO objects. Both verification fields
reported `nothing_to_verify` at these exact coordinates; the new forward hours had
not matured. Temporary validation services were stopped afterward.

Known broader-suite failures carried forward from the previous code checkpoint are
one batch mock-signature failure and two identifier/code-revision inventory failures.
All three reproduced at the Learning Core starting revision. Its broader offline
run had 3,592 passes and those three failures; final focused reruns cover the
subsequent Learning Core guards. The final typed-boundary inventory adds no new
findings to the starting revision. Learning Core validation also passed 32 distinct
relevant PostgreSQL/MinIO integration checks. The previously corrected Windows
MRMS-cache rename regression did not recur.
Those three failures have since been resolved: the batch mock now checks the current
arguments, and the identifier inventory recognizes the existing issued/batch UUID and
provider ICAO contracts. Prepared-snapshot path labels, retained-manifest digests and
temperature-verification code revisions now validate at their boundaries. Historical
identities and forecast science are unchanged. Check current test results when changing
code; a saved commit does not certify the whole application.

## Data attribution

ECMWF data is used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
and [ECMWF attribution terms](https://apps.ecmwf.int/datasets/licences/general/):
**This service is based on data and products of the European Centre for Medium-Range
Weather Forecasts (ECMWF).** MesoForge transformations and source/licence metadata
remain in retained provenance. No ECMWF endorsement is implied.
