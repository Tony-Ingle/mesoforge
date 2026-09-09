# RFC: MesoForge V2 architecture

Status: Proposed for owner review

Decision owner: MesoForge owner

Architecture author: Codex

Source baseline: `origin/main` at `ce0e0d77e645d31ca33caaac2d20f4f8748dc90e`

Primary read-only implementation donor: `claude/phase3-coordinate-verification` at
`17968e79749bcd958a69e3a4f0fc03eddea00efd`, including its preserved uncommitted
four-file test/support diff

Primary read-only design donor: Phase 3 de-bloat RFC at
`fecb2a2b2db10799f18b18357e7e4b0bb1e1a65e`

## Terminology

These definitions are deliberately first. Later sections use these words only with the
meanings below.

- **Model guidance:** Numerical weather predictions supplied by a weather model, such as
  HRRR, NBM, or GFS. Guidance is an input to MesoForge, not the MesoForge forecast itself.
- **Guidance cycle/run:** One model's prediction set identified by the model, product, and
  reference time at which the model run began. Different models may contribute cycles with
  different reference times to one MesoForge forecast.
- **Shared cached guidance:** Model data downloaded and normalized once for a model cycle,
  then stored in spatial chunks that many locations can read. It is never a separately
  downloaded model dataset for each location.
- **Forecast:** MesoForge's deterministic values for a location and future valid times,
  including the model contributors, blend, any eligible deterministic correction, quality,
  missingness, and source lineage.
- **Forecast snapshot/version:** One immutable issued forecast. It pins its location,
  issue time, information available at issuance, guidance inputs, configuration, weights,
  corrections, values, code, and provenance. A later refresh creates another version; it
  never changes the earlier one.
- **Forecast state:** The small mutable view that says which immutable forecast snapshot is
  current for a registered location and whether refresh is pending, fresh, stale, degraded,
  or failed. State is a pointer and status, not the forecast values themselves.
- **Registered location:** A normalized latitude/longitude deliberately saved for recurring
  refresh, durable forecast history, observation support assessment, verification, later
  learning, and optional delivery.
- **One-off location/request:** A bounded forecast request for a latitude/longitude that is
  not registered. It may use a short-lived response cache but does not silently create
  durable history, verification eligibility, learning state, or delivery state.
- **Observation:** A measured weather value from a named provider and station or sensor at
  an event time, with units, quality information, spatial support, availability time,
  ingestion time, and revision identity.
- **Verification:** The deterministic process that decides whether one forecast fact and
  one observation revision can be compared, records the match or explicit reason they
  cannot be compared, and calculates an error only when the pair is valid.
- **Verification fact:** An immutable record of one matching opportunity. It identifies the
  forecast fact, selected observation revision or explicit absence, matching policy,
  cutoffs, support decision, statuses, and provenance.
- **Normalized fact:** A small, schema-versioned, typed, unit-explicit row with a stable
  logical identity and provenance. Its meaning does not depend on an in-memory object,
  report layout, API response, or current configuration.
- **Error fact:** An immutable record derived from a valid verification fact. It stores the
  score status and, when scoreable, forecast minus observation in the canonical unit.
  Unscoreable opportunities have an explicit null error and reason; they are not dropped.
- **Baseline weights:** Reviewed deterministic contributor weights used before any learning
  is eligible. They are versioned configuration, include explicit rows for approved
  available-model sets, and are never invented or silently renormalized at request time.
- **Learned weights:** Immutable contributor weights estimated from eligible historical
  verification and error facts and activated only after minimum-data and evaluation gates.
  Before activation they are candidates, not production weights.
- **Bias correction:** A deterministic offset or transformation learned from historical
  errors and applied exactly once after the uncorrected blend. With insufficient history,
  the correction is explicitly absent/zero rather than fabricated.
- **Model weighting/blend:** The deterministic operation that combines eligible model
  guidance for a variable, location, and valid time using one approved weight row. Wind
  components, probabilities, and accumulations follow their own scientific operators.
- **As-of cutoff:** A UTC instant limiting what information a computation was allowed to
  use. Provider availability and local ingestion must both satisfy the applicable cutoff;
  event time is not a substitute for either.
- **Provenance:** The identities and metadata needed to explain where a value came from:
  source bytes, provider and cycle, transformations, code, configuration, weights, policy,
  timestamps, and parent facts or artifacts.
- **Missingness:** An explicit machine-readable status explaining why a value, observation,
  pair, or score is absent. Missing does not mean zero and is never omitted merely to make a
  denominator or result look complete.
- **Replay:** Recomputing a result from pinned, checksum-verified historical inputs without
  source discovery or network access. Replay reproduces canonical logical values and
  identities under the retained compatible implementation; it does not promise identical
  Parquet bytes across unrelated encoder versions.
- **Background worker:** A process outside the request/response path that performs bounded
  acquisition, decode, normalization, recurring forecast refresh, observation work,
  verification, learning, or delivery.
- **Thin API:** An HTTP boundary that validates bounded requests, reads prepared state,
  performs only small point extraction or fact reductions, and returns status. It does not
  perform model downloads, full-file decode, historical rebuilds, training, or other heavy
  work while a caller waits.

## 1. Executive architecture decision and recommendation

Build V2 as a Python modular monolith with three runtime roles and one durable integration
boundary: normalized state. The roles are a thin FastAPI application, one serial science
worker, and one bounded I/O worker. PostgreSQL remains the searchable authority for state
and fact indexes; S3-compatible storage remains the authority for immutable source and
array bytes. One PostgreSQL jobs table provides at-least-once background work. Do not add a
workflow framework, broker, Redis, microservices, or dashboard in V2.

The central decision is **ingest once, derive many locations**. A scheduled worker acquires
one copy of each selected model field for a guidance cycle, decodes one bounded field group
at a time, and writes shared native-grid spatial chunks with overlap needed for point
interpolation. A one-off or registered forecast reads only the chunks enclosing its
coordinate. No API request and no location registration starts a model download or creates
a location-specific copy of the model dataset.

Normalized forecast, observation, verification, and error facts are the durable product and
learning boundary. Reports are bounded queries over those facts. V2 will not recreate the
old Phase 3 Cartesian report lattice, denominator partitions, corpus-root universe,
all-in-one proof coordinator, or host-specific evidence machinery.

The recommended implementation path is **selective rebuilding from current main**, not
merging and refactoring the old Phase 3 branch. Preserve current Phase 0-2 storage,
provenance, acquisition, normalization, blend, matching, and metric kernels where their
contracts remain correct. Add small V2 boundaries around them and manually port only
cohesive Phase 3 algorithms. The old branch changes 76 files with 37,013 insertions, yet
adds no HTTP endpoint or physical table; deleting its proof/report coupling is more work and
risk than porting its validated scientific pieces.

This design is useful from its first implementation increment. Shared guidance plus
baseline weights can return deterministic one-off forecasts before observations,
registration, learning, AI, or email exist. Each later capability consumes immutable facts
and may fail or remain ineligible without preventing that baseline.

## 2. Product behavior and invariants

### 2.1 Product behavior

1. A caller may request a forecast for any valid latitude/longitude inside currently cached
   model coverage. Registration is not required.
2. A registered location is refreshed after an eligible shared guidance update and keeps an
   immutable history of issued snapshots.
3. Observation acquisition and verification happen after valid time in background work.
4. Evaluation computes only requested, bounded groups from normalized facts. Unrequested
   combinations and zero cells are not materialized.
5. Bias and learned weights remain inactive until valid facts satisfy fixed gates. A new
   location therefore begins with baseline weights, zero eligible learned-history count,
   no learned bias, no learned confidence, and no skill claim.
6. A later AI forecaster may propose bounded adjustments to an immutable baseline. It cannot
   alter source guidance, observations, verification facts, baseline facts, or policies.
7. Email is a presentation/delivery step over an immutable selected forecast snapshot. It
   cannot calculate or modify meteorology.

### 2.2 Non-negotiable invariants

- **Shared acquisition:** the cache key is model/product/cycle/field/lead/spatial chunk and
  configuration, never location. Many locations reference the same asset identities.
- **Stable location identity:** normalize EPSG:4326 coordinates by the reviewed Phase 3
  algorithm: decimal input, range validation, `180 -> -180`, five decimal places,
  round-half-to-even, canonical positive zero, JCS bytes, and full SHA-256 logical key.
  A presentation label or timezone is not scientific identity.
- **Location and station separation:** a forecast coordinate is never replaced by an
  observation station coordinate. Observation support, distance, elevation difference, and
  provider station identity remain explicit provenance.
- **Immutable values:** guidance assets, forecast snapshots/facts, observation revisions,
  verification facts, error facts, learned snapshots, AI proposals/decisions, and delivery
  attempts are append-only. Mutable rows are limited to job leases and versioned current
  pointers/preferences.
- **UTC and intervals:** issue, reference, availability, ingestion, event, valid, and
  interval-bound times are UTC and separately named. Instantaneous and interval fields
  cannot be interchanged.
- **Units:** canonical temperature is K, wind is m/s, direction is degree clockwise from
  north using the meteorological wind-from convention, QPF is `kg m-2`, and PoP is a
  probability in `[0,1]` for a named event and interval. Conversion precedes blending.
- **Wind:** grid-relative U/V is rotated to earth-relative before blending. U and V are
  blended, then speed and direction are derived; directions are never angle-averaged.
  U/V are the required coupled source pair; derived speed is valid only from a valid pair,
  while direction is explicitly null/unscoreable under the policy calm threshold. Gust is
  independently nullable and never required to retain otherwise valid sustained wind. If a
  configured source-integrity rule establishes that a present gust corrupts its associated
  sustained-wind values, reject that source's U/V and gust together only for the affected
  point and horizon while preserving independent fields and other points/horizons.
- **Dew point:** `dew_point <= temperature + 1e-6 K`; invalid source values are not clamped
  into agreement.
- **QPF and PoP:** QPF is nonnegative and keeps exact accumulation bounds. NBM PoP01 keeps
  its named threshold/interval semantics. Deterministic QPF never synthesizes PoP, and a
  missing observation accumulation is never treated as zero.
- **Blend:** use the exact reviewed baseline row for the available contributor set and lead
  band. No arbitrary surviving set is dynamically renormalized. A fallback is marked
  degraded and explained.
- **Cutoffs:** forecast input requires both authoritative provider availability and local
  ingestion no later than the forecast information cutoff. Observation revision selection
  requires both no later than the verification cutoff and query/build cutoff.
- **Signed errors:** every scalar error is forecast minus observation. Direction error is a
  circular difference in `[-180,180]` with absolute circular metrics computed separately.
- **Explicit missingness:** every registered forecast opportunity and verification
  opportunity has a status row. Null value plus reason is distinct from zero.
- **No leakage:** observations, regimes derived from observations, later revisions, and
  future forecasts cannot enter forecast issuance, training examples, or as-of replay
  before their recorded cutoffs.
- **No fabricated learning:** counts, sample dates, uncertainty, confidence, bias, weights,
  and skill are persisted only when actually calculated from eligible facts. Otherwise the
  response says `cold_start` or `insufficient_data` with exact zero counts/null identities.
- **Layer separation:** raw model, configured blend, bias-corrected baseline, learned-weight
  baseline, AI proposal, policy-approved adjustment, and delivered forecast are separately
  identified. Learning and AI never rewrite an earlier layer.
- **Replay:** retained input identities, canonical query, policies, code revision, lockfile,
  environment/OCI identity, and configuration are sufficient for zero-network logical
  replay during the declared replay window. Original bytes remain authoritative.

## 3. Component model and runtime/deployment boundaries

### 3.1 Modular-monolith packages

Keep one `mesoforge` deployment and enforce dependency direction with import-linter:

```text
api -> application -> domain packages -> contracts -> common
workers -> application
application -> storage interfaces
storage adapters -> storage interfaces + contracts
learning -> verification/forecast contracts only
adjustments -> forecast/fact contracts + deterministic policy
presentation/delivery -> immutable forecast read models only
```

Existing `guidance`, `alignment`, `forecasting`, `observations`, `verification`, `catalog`,
`provenance`, `storage`, `contracts`, and `common` packages remain the scientific and
infrastructure core. Add at most five top-level packages: `api`, `workers`, `learning`,
`adjustments`, and `delivery`. Neither API nor delivery may import decoder modules. Learning
may read facts but cannot write observations, forecast baselines, or source artifacts.

### 3.2 Runtime roles

1. **API process:** two small workers behind one HTTP service. It validates requests, queries
   PostgreSQL, reads at most the necessary cached chunks, invokes pure point forecast or
   bounded evaluation functions, and serializes compact JSON. It does not own schedules.
2. **Science worker:** one process with concurrency one for model acquisition/decode/
   normalization, registered-location refresh batches, replay, and later training. It owns
   the only memory-heavy lease. It processes one model/lead/field group or one bounded
   location batch at a time.
3. **I/O worker:** one process with concurrency at most two for observation requests,
   matching/verification, and eventual email. CPU-heavy evaluation or training is not
   placed here.

Initially these roles run on the same approximately 8 GB KVM2 host under systemd or the
existing deployment mechanism. PostgreSQL and S3-compatible storage remain external
protocols; local/CI may use co-resident PostgreSQL and MinIO. Extracting a worker to another
host later must not change contracts or fact identity.

### 3.3 Scheduling and queue

Use one PostgreSQL `jobs` table, `SELECT ... FOR UPDATE SKIP LOCKED`, lease expiry, bounded
attempt counts, and canonical idempotency keys. Scheduled producers insert jobs for model
cycles, registered refreshes, observation windows, verification, learning, and delivery.
At-least-once execution is safe because every durable output has a unique logical-key digest
and immutable content digest. There is no second broker or workflow UI.

Priority is fixed: current model ingestion, registered forecast refresh, observation cutoff
work, delivery, verification, then replay/training/backfill. A lower-priority science job
finishes or yields after its current bounded unit when issuance work is queued.

### 3.4 Shared guidance cache

The ingestion worker retains exact selected GRIB index/message bytes as existing generic
artifacts. It decodes one field group at a time and writes native-grid cache tiles with a
64 x 64 core plus the one-cell overlap needed by `bilinear-native-grid.v1`. Tile ownership,
core bounds, overlap, source grid, cycle, lead, variable group, transform version, units,
and content digest are explicit. The point resolver deterministically chooses the core tile
that owns the coordinate and validates four finite corners without extrapolation.

Cache coverage is a configured service region, initially a bounded CONUS/Minnesota region
approved by the owner. "Arbitrary" means the coordinate is chosen at request time rather
than pre-enumerated; it does not claim coverage outside an ingested model domain. An
outside-domain request is unsupported. A covered coordinate whose required cycle tiles are
not ready is not-ready; the API never fills the gap by downloading.

## 4. End-to-end runtime flows

### 4.1 Scheduled model ingestion

1. The scheduler creates one idempotent ingestion job per expected model/product/cycle.
2. The science worker discovers eligible provider assets under an information cutoff and
   bounded retry/deadline policy.
3. Exact selected index and GRIB message bytes are registered once with provider and local
   timestamps and content digests.
4. The worker decodes one model/lead/field group at a time, validates grid, time, interval,
   unit, probability, and lineage semantics, and writes shared overlapped tiles.
5. `guidance_assets` rows atomically index verified tile artifacts. A cycle becomes ready
   only when the configured minimum field/lead set is present; partial state is explicit.
6. Duplicate jobs resolve the same logical keys. A provider revision creates a new immutable
   asset revision rather than mutating prior bytes.
7. Ready-cycle publication enqueues bounded registered-location refresh batches. It does not
   calculate forecasts for every possible coordinate.

### 4.2 Arbitrary one-off forecast

1. `POST /v1/forecasts` accepts one normalized coordinate, up to 36 approved horizons, and
   optional presentation units.
2. The API resolves the latest eligible shared assets at the request's server-assigned
   information cutoff. It rejects outside-domain, not-ready, or over-limit requests before
   object reads.
3. It reads only enclosing tiles, performs native-grid bilinear point extraction, applies
   per-variable availability and scientific validation, selects baseline weight rows, and
   computes a deterministic baseline.
4. The response names every source cycle/asset digest, value/unit/interval, contributor and
   weight, exclusion, quality, cutoff, and learning state. Before learning, learned history
   count is zero and learned IDs/confidence are null.
5. The result may enter a 15-minute, 1,000-entry/128 MiB maximum response cache keyed by
   normalized coordinate, exact guidance asset digest set, configuration, baseline weight
   version, learned snapshot identities, and cutoff policy. It is not durable history and
   is not verification eligible.

### 4.3 Registered-location forecast refresh

1. Registration persists the location identity/current snapshot and enqueues an initial
   refresh; it does not start provider acquisition.
2. A ready guidance cycle enqueues batches of at most 25 locations. The science worker uses
   the same shared tiles as one-off requests and computes each forecast independently.
3. It atomically inserts one immutable `forecast_snapshots` header and all declared
   `forecast_facts`, then compare-and-swaps the location's current forecast pointer.
4. A duplicate or concurrent refresh reuses the logical snapshot key. An older completion
   may persist as history but cannot replace a newer current pointer.
5. Failure preserves the prior last-good snapshot and updates only refresh state/reason.
   The API exposes age and stale/degraded status rather than pretending the old forecast is
   fresh.

### 4.4 Observation acquisition, matching, and verification

1. For each registered forecast valid time, the I/O worker schedules the versioned support
   and observation policy. One-off forecasts are excluded.
2. It selects observation support from immutable station metadata using WGS84 distance,
   effective time, elevation/provenance rules, and deterministic ties. Forecast and station
   identities never merge.
3. It performs bounded provider queries, preserving every attempt, terminal failure, and
   successful-empty response plus raw bytes, provider availability, completion, and local
   ingestion.
4. Normalization appends observation revisions. Revision identity includes provider,
   station/sensor, logical event, interval, variable, and canonical content. Corrections do
   not overwrite earlier revisions.
5. At the verification cutoff, matching first enumerates only logical observation events in
   the inclusive policy window that have at least one revision whose provider availability
   and local ingestion satisfy both verification and build cutoffs. It chooses the nearest
   eligible event using deterministic ties, then selects the latest cutoff-eligible revision
   of that event. Field QC is independent; exact-interval QPF and calm-direction rules
   apply. Post-cutoff event existence cannot affect selection.
6. Each declared opportunity emits one immutable verification fact, including unsupported,
   acquisition-failed, missing, QC-rejected, interval-mismatch, unmatched, and scored
   outcomes. Each emits one corresponding error fact whose value is null unless scoreable.
7. Facts pin the forecast, observation revision/raw response, support and matching policy,
   cutoffs, units, code/configuration, and logical/content digests.

### 4.5 On-demand evaluation

1. `POST /v1/evaluations` validates exact filters, groupings, metrics, and row/group limits.
2. PostgreSQL indexes select normalized fact rows by location, issue/valid window, variable,
   layer, lead, and status. S3 scanning or generic artifact-attribute scanning is forbidden.
3. The evaluator reports the complete selected opportunity denominator and independent
   forecast/observation/pair/score status counts before metrics.
4. It computes only requested groupings. A requested empty group appears once with zero
   counts and explicit null reasons; unrelated combinations are absent.
5. It keeps variable, unit, location, and layer separate; PoP and QPF are never pooled.
   Non-exclusive regimes are labelled as overlapping views and never summed as disjoint
   totals.
6. Layer comparisons inner-pair the exact same opportunity and observation revision.
   Unpaired aggregate differences cannot be called improvement.
7. The result includes canonical query, ordered input fact logical/content digests, policy,
   algorithm/code/environment identity, deterministic ordering, and no NaN/Infinity. It is
   not persisted by default.

### 4.6 Later learning and weight activation

1. A science-worker job selects only valid, scored, cutoff-eligible error facts and records
   its exact training query.
2. A location/variable/lead-band bias candidate requires at least 60 scored facts across at
   least 20 distinct issue dates in the last 90 days. Below that gate the operational
   correction remains explicit zero/absent.
3. A learned-weight candidate requires at least 200 common scored opportunities across at
   least 40 issue dates with all compared contributors represented. It cannot learn from
   unmatched layer populations.
4. Training produces an immutable `learned_snapshots` row/artifact with sample counts,
   date range, algorithm, parameters, feature/fact digests, applicability, fallback, and
   uncertainty. It does not activate itself.
5. A candidate must pass a predeclared rolling-origin holdout, named primary metric,
   subgroup/tail non-harm limits, calibration rules for probabilities, coverage/abstention
   rules, and owner approval. Activation is a new versioned configuration/superseding row.
6. Forecast issuance pins the eligible learned snapshot as of its information cutoff. If it
   is missing, stale, inapplicable, below gate, or fails validation, baseline weights and
   zero correction remain in force.
7. Rollback chooses an earlier immutable configuration. Neither training nor rollback
   rewrites forecasts or facts.

### 4.7 Later bounded AI adjustment

1. The adjustment worker receives one immutable deterministic baseline snapshot plus a
   compact evidence packet built from persisted guidance summaries, model spread, recent
   valid facts, and applicable learned snapshots. Raw national grids and hidden current data
   are excluded.
2. The provider returns a strict proposal: supported variable/valid-time keys, numeric
   deltas, rationale, cited evidence IDs, and abstentions. Free text cannot alter numbers.
3. A deterministic policy checks allowlisted variables, absolute and rate-of-change bounds,
   dew-point and wind/gust coupling, probability/QPF semantics, evidence completeness,
   freshness, hazardous-regime restrictions, and kill switches atomically.
4. In shadow mode the proposal is never selected. Later, only a policy-approved and, at
   first, human-approved complete bundle creates a new immutable adjusted snapshot
   referencing the baseline, proposal, policy decision, and reviewer.
5. AI outage, malformed output, missing evidence, or policy failure leaves the deterministic
   baseline unchanged and deliverable. AI never changes observations, source guidance,
   baseline weights, learned state, verification, or prior snapshots.

### 4.8 Eventual email generation

1. A due subscription creates a delivery job keyed by subscription version, immutable
   forecast snapshot, template version, and scheduled occurrence.
2. The I/O worker selects a snapshot only under the approved freshness/degraded policy and
   renders presentation units/timezone without changing scientific facts.
3. The email names issue/valid times, freshness, degraded/missing fields, baseline versus
   approved-adjusted layer, and unsubscribe/suppression behavior. It never invents
   confidence or unavailable products.
4. The provider response and terminal status create an immutable delivery attempt. Retries
   reuse the idempotency key and cannot duplicate a successful send.
5. If no eligible snapshot exists, the worker records `not_sent:not_ready`; it does not run
   ingestion or forecast computation. Dashboard work remains out of scope.

## 5. Storage/state model

### 5.1 Authorities and twelve-table ceiling

Keep the existing Phase 0-2 tables and generic artifact/activity lineage. V2 may add at most
these twelve PostgreSQL tables; adding another requires owner-approved architecture change:

| V2 table | Purpose and ownership |
| --- | --- |
| `jobs` | Mutable lease/attempt/status rows for the single background queue; worker-owned. |
| `guidance_assets` | Append-only searchable index of shared source/cycle/field/lead/tile artifact identities and readiness. |
| `registered_locations` | Immutable coordinate identity plus versioned current location snapshot, forecast pointer, and refresh state. |
| `forecast_snapshots` | Immutable issued-version headers and exact input/config/weight/correction identities. |
| `forecast_facts` | Immutable normalized values or explicit missingness for raw/model/blend/corrected/adjusted layers. |
| `observation_facts` | Append-only normalized logical observations and provider revisions. |
| `verification_facts` | Immutable match opportunities, statuses, support, cutoffs, and selected revision. |
| `error_facts` | Immutable score status and forecast-minus-observation or circular error. |
| `learned_snapshots` | Immutable bias/weight candidates, approvals/supersessions, applicability, gates, and training provenance. |
| `ai_adjustments` | Immutable proposal, policy decision, optional reviewer, and adjusted-snapshot references. |
| `delivery_subscriptions` | Versioned opt-in preference/suppression state; delivery-owned and not scientific input. |
| `delivery_attempts` | Append-only send/not-send outcomes and provider references. |

Large bytes remain in S3-compatible storage. Raw selected GRIB messages, normalized guidance
tiles, large forecast arrays if ever needed, raw observation responses, canonical fact
exports, learned model payloads, AI request/response bodies subject to policy, and rendered
email bodies are generic immutable artifacts. PostgreSQL contains queryable normalized
facts and artifact references, not full model arrays.

### 5.2 Stable identities and idempotency

Preserve ADR 0002's distinction: record IDs remain server UUIDs and exact serialized bytes
have SHA-256 content digests. Every normalized V2 row additionally has a unique deterministic
`logical_key_digest` over its schema-versioned natural key:

- guidance asset: model/product/cycle/lead/field group/grid/tile/transform configuration and
  source revision;
- forecast snapshot: location, scheduler-pinned issue instant, scheduler-pinned information
  cutoff, ordered guidance asset digests, configuration, baseline/learned weights, bias, and
  parent snapshot when adjusted;
- forecast fact: snapshot, location, variable, layer/model, valid time, interval, vertical
  definition, and unit;
- observation revision: provider, station/sensor, logical event/interval/variable, provider
  revision identity, authoritative provider availability, and content digest; local
  ingestion time is provenance and cutoff evidence, not part of the natural key;
- verification fact: forecast fact, selected observation revision or missing-status key,
  support/matching policies, scheduler-pinned verification cutoff, and scheduler-pinned
  build cutoff;
- error fact: verification fact and error algorithm version;
- learned/AI/delivery records: their exact ordered input identities and governing policy.

Unique constraints make duplicate execution return the existing record. Same logical key
with conflicting canonical content fails closed. New provider or policy revisions receive
new logical keys; nothing is silently repaired in place. Job creation pins every issue,
information, verification, and build cutoff in the idempotent job payload. Retries reuse
those instants and never substitute their execution or ingestion time.

### 5.3 Update and immutability rules

- Cache assets and every scientific fact are insert-only. Readiness is derived from required
  immutable asset rows, not set by mutating payloads.
- `registered_locations.current_forecast_snapshot_id` and refresh status are the only
  mutable forecast state. Updates use optimistic version/CAS and cannot point backward in
  issue/cutoff order.
- Location coordinates are immutable. Label, timezone, privacy, retention, or delivery
  changes create a new location-snapshot/preferences version under the same coordinate key
  when scientifically irrelevant; a coordinate change creates a new location identity.
- Job status/lease is mutable and recoverable after lease expiry. A job cannot be the sole
  evidence for a scientific outcome.
- Observations are revisions, never updates. A corrected timestamp or value appends a new
  revision and causes a new verification/error fact set for a new build cutoff.
- Learned snapshots and AI decisions are immutable and supersede earlier records. Forecasts
  select the latest eligible approved version as of their cutoff, never a mutable `active`
  object with unrecorded history.
- Delivery subscriptions are versioned mutable intent. Attempts are immutable audit rows.

### 5.4 Provenance, cutoffs, and missingness columns

Every scientific fact carries schema version, record ID, logical-key digest, content/logical
payload digest, created/ingested time, configuration ID/digest, code revision, environment
or OCI digest, source/parent IDs and digests, governing policy IDs, and applicable as-of
cutoffs. Forecast and observation facts carry canonical value/unit or a required missingness
status/reason. Verification keeps independent forecast, observation, pair, and score status;
error is null unless scoreable. A null without a status/reason is invalid.

The relational row schema is canonical. Exported Arrow/Parquet uses a pinned Arrow schema,
column order, row sort, units, timestamps, and null encoding. A canonical logical-table
SHA-256 covers that normalized representation; a separate artifact digest covers physical
Parquet bytes. Logical replay compares the former, avoiding brittle claims across Parquet
encoder upgrades while retaining exact original bytes.

### 5.5 Ownership and concurrency

- Ingestion owns `guidance_assets`; only verified object promotion may precede row insert.
- Forecast issuance owns snapshot/fact transactions and location pointer CAS.
- Observation acquisition owns observation revisions; verification reads but cannot mutate
  them.
- Verification owns verification/error facts and cannot modify forecasts or observations.
- Learning reads facts and owns learned snapshots only.
- Adjustment reads immutable evidence and owns AI records/new adjusted snapshots only.
- Delivery reads selected snapshots and owns subscription versions/attempts only.

PostgreSQL transaction boundaries are small. Object bytes are uploaded and verified before
rows reference them, preserving ADR 0004's harmless-orphan rather than dangling-manifest
failure mode. A bounded orphan reconciler may remove unregistered temporary objects older
than 24 hours; generalized artifact garbage collection remains deferred.

### 5.6 Retention and replay

Initial hard retention classes, subject to the owner decisions in Section 14:

| State | Minimum retention | Replay meaning |
| --- | ---: | --- |
| Raw selected guidance/index bytes | 90 days | Full source-to-forecast zero-network replay within 90 days. |
| Normalized shared guidance tiles | 30 days after cycle | Fast point replay while retained; rebuildable from retained raw bytes through day 90. |
| One-off response cache | 15 minutes, max 1,000 entries/128 MiB | Convenience only; no historical promise. |
| Registered forecast snapshots/facts | 2 years | Exact issued values, lineage, and fact-query replay. |
| Raw observation responses | 1 year | Re-normalization/revision audit within one year. |
| Observation, verification, and error facts | 5 years | Deterministic evaluation and learning replay. |
| Learned and AI audit state | 5 years after supersession | Training/query/policy replay from retained facts and original provider bytes where policy permits. |
| Delivery attempts/bodies | 1 year; suppression record 5 years | Duplicate-send and compliance audit. |

Each response states `replay_capability`: `full_source`, `fact_only`, or `expired`. Full
replay requires the exact source/configuration/fact roots plus retained implementation
source, lockfile, and OCI/environment digest. V2 release artifacts must remain retrievable
for the 90-day full-source window. After source retention expires, MesoForge can reproduce
queries over preserved facts but must not claim it can regenerate the original forecast from
source bytes.

## 6. Thin API boundary

FastAPI is the initial adapter; public Pydantic/OpenAPI contracts are versioned independently
of internal objects. Exactly these ten endpoint operations are allowed in V2:

| Endpoint | Responsibility |
| --- | --- |
| `GET /v1/guidance/status` | Return cached cycle coverage/readiness/freshness only; no discovery. |
| `POST /v1/forecasts` | Compute one bounded one-off point forecast from ready shared tiles, or return explicit not-ready/unsupported. |
| `GET /v1/forecasts/{forecast_id}` | Return one immutable persisted registered/adjusted snapshot and compact facts. |
| `POST /v1/locations` | Register one normalized coordinate and enqueue refresh; return `201` with pending/current state. |
| `GET /v1/locations/{location_id}` | Return registration, refresh state, learning cold-start/eligibility counts, and current snapshot link. |
| `GET /v1/locations/{location_id}/forecasts` | Paginate immutable history, maximum 100 versions/page. |
| `GET /v1/locations/{location_id}/verification` | Return paginated fact/status summaries or bounded filters, never an implicit full report. |
| `POST /v1/evaluations` | Run a synchronous bounded fact query under Section 12 limits. |
| `PUT /v1/locations/{location_id}/delivery` | Create a versioned opt-in email preference when delivery is enabled. |
| `DELETE /v1/locations/{location_id}/delivery` | Create a suppression/unsubscribe version; never delete scientific history. |

Worker acquisition, verification-build, training, AI-provider, and send operations are not
public endpoints. They are internal job handlers. No generic "run arbitrary job" endpoint
exists.

Every response carries request ID, schema version, server cutoff, data freshness, quality,
missingness, and relevant immutable identities. `POST /v1/forecasts` accepts exactly one
coordinate and at most 36 horizons; it reads at most 64 cached tile objects and returns at
most 1,000 value/contribution records. `POST /v1/evaluations` accepts at most 10 locations,
366 days, 100,000 selected fact rows, 10 group dimensions, and 1,000 output groups.

### 6.1 Work prohibited inside requests

An API request must not:

- discover provider cycles or download/index/decode a full model file;
- create normalized regional guidance tiles;
- acquire observations in bulk or retry a provider workflow;
- refresh all registered locations;
- rebuild historical verification/error facts;
- train or activate bias/weight models;
- call an AI provider, wait for human approval, or send email;
- scan S3 listings, load a full corpus/dataframe, render images, or run replay/backfill;
- hold the science-worker heavy lease.

### 6.2 Fresh, stale, not-ready, and degraded behavior

- **Fresh:** required selected guidance is no older than the per-model freshness policy;
  return `200`, `quality=complete` or a scientifically valid source fallback.
- **Degraded:** an approved contributor subset or field is missing but a reviewed fallback
  exists; return `200`, exact missingness/contributors, `quality=degraded`, and no invented
  value for unavailable fields.
- **Stale:** for registered-location reads, the last-good immutable snapshot may be served
  with `200` and `freshness=stale` for at most six hours beyond its policy deadline. It is
  never relabelled with a new issue time. One-off computation may use an eligible cached
  cycle only within the same six-hour ceiling.
- **Not ready:** required shared assets are absent, incomplete, corrupt, or older than six
  hours beyond policy. Return `503` with stable code `guidance_not_ready`, missing asset
  classes, status URL, and `Retry-After`; do not start ingestion.
- **Unsupported:** coordinate outside configured source coverage or unsupported
  variable/horizon returns `422` with exact limits.
- **Persisted historical read:** `GET /forecasts/{id}` always returns that immutable version
  if authorized, with its original freshness/quality; current staleness does not rewrite it.

The warm one-off and ordinary fact-query latency objective is p95 <= 750 ms and p99 <=
1.5 s, with a hard 2 s application timeout. Requests that cannot remain within bounds fail
explicitly; V2 does not add an asynchronous evaluation endpoint.

## 7. One-off versus registered-location behavior

| Concern | One-off request | Registered location |
| --- | --- | --- |
| Input | Any valid coordinate inside ready cached coverage | One persisted normalized coordinate identity |
| Model acquisition | Never; reads shared tiles | Never per location; refresh reads the same shared tiles |
| Result persistence | No scientific history by default; 15-minute bounded response cache | Immutable snapshots/facts plus mutable current pointer |
| Refresh | Caller requests again | Background refresh after ready cycles, batched <= 25 locations |
| Verification eligibility | None | Future issued snapshots eligible under versioned support policy |
| Observation acquisition | None | Background bounded provider work after valid time |
| History/evaluation | None; response is not training data | Durable normalized forecast/observation/verification/error facts |
| Learning | Reports `eligible_fact_count=0` unless an approved broadly applicable snapshot genuinely covers it; otherwise baseline only | Begins with count zero; location-specific learning only after actual minimum gates |
| Rate/compute bound | 1 coordinate, <= 36 horizons, <= 64 tile reads, <= 1,000 result rows, normal API rate limit | <= 25 locations/refresh job, one science job at a time |
| Failure | Explicit unsupported/not-ready/degraded; no hidden job | Last-good history remains readable; refresh status/reason visible |
| Promotion/registration | `POST /locations` registers the coordinate for future refresh; it does not convert cached response into history | Already registered |
| Delivery | None | Optional only after delivery capability and opt-in |

Registration is prospective. It enqueues a fresh snapshot from the then-current eligible
shared assets. It does not backdate the one-off response, manufacture observation history,
retroactively verify forecasts, or seed learning. A deliberate future backfill would be a
separate bounded operator action and must preserve original as-of cutoffs; it is not an API
side effect.

Cold start is always honest: baseline weight version is present; learned bias/weight IDs are
null; eligible scored fact count and distinct issue-date count are numeric zero; learning
status is `cold_start`; confidence and skill are null. If approved regional/global learned
state is later allowed to apply, the response must name its applicability evidence and may
not call it location-specific history.

## 8. KEEP / SIMPLIFY / MOVE-LATER / DELETE donor map

### 8.1 Current main at `ce0e0d7`

| Disposition | Files/modules/concepts | V2 treatment |
| --- | --- | --- |
| KEEP | `common/identifiers.py`, `contracts/artifacts.py`, `contracts/provenance.py`, `provenance/*`, `storage/interfaces.py`, `storage/postgres/*`, `storage/s3.py`, `storage/json.py`, ADRs 0001/0002/0004 | Preserve typed identities, activity lineage, content verification, PostgreSQL/S3 split, and transaction/idempotency rules. Extend repository queries rather than scanning artifact attributes. |
| KEEP | `guidance/acquisition_v2.py`, `guidance/index_parsing.py`, source adapters/decoders, `guidance/normalization_v2.py`, `guidance/canonical_v2.py`, `alignment/spatial.py`, `alignment/temporal.py` | Reuse provider and scientific semantics. Generalize fixed bbox retention into shared 64 x 64 overlapped cycle tiles; acquisition remains once per cycle. |
| KEEP | `forecasting/{scalar_blend,vector_blend,gust_blend,pop_blend,precipitation_blend,availability,contributions}.py` | Reuse pure operators, approved fallback semantics, source-value provenance, and localized coupled wind/gust exclusion. |
| KEEP | `observations/{acquisition,normalization_v2,quality}.py`, `verification/{matching,metrics,qpf_pop_metrics}.py` | Reuse normalization/QC and metric kernels where they satisfy V2 fact schemas; add revision-aware, coordinate-aware boundaries. |
| SIMPLIFY | `application/phase2_production.py` (2,678 physical lines), `application/phase2.py`, `phase2_adapters.py`, `phase2_replay.py` | Do not extend the all-phase coordinator. Extract/reuse small provider/science services behind V2 application boundaries. Replay is per durable boundary, not an all-in-one coordinator proof. |
| SIMPLIFY | `forecasting/baseline_v2.py` and fixed station tuples/configuration | Replace fixed three-station assembly with arbitrary point facts and registered location snapshots. Preserve formulas and status semantics. |
| MOVE-LATER | Broad backtest warehouse, workflow-orchestrator selection, full release checkout certification, platform-wide telemetry/admission framework from `docs/architecture/v1.md` | Keep local bounds and release identities now; add platform infrastructure only after measured demand. |
| DELETE | V2 dependence on fixed `station.kcbg/kjmr/kros` forecast identity, per-phase monolithic coordinators, and location-bbox cache creation as a request prerequisite | Retire legacy Phase 1/2 entry points only after V2 parity. Do not delete scientific kernels or historical artifacts. |

### 8.2 Old Phase 3 donor at `17968e7` and dirty support diff

| Disposition | Donor material | V2 treatment |
| --- | --- | --- |
| KEEP | `contracts/location.py` from `f257f3e`; `LocationId` additions | Manually port decimal normalization, full coordinate digest, immutable snapshot, metadata-versus-key mutation, and dataset binding. |
| KEEP | `forecasting/coordinate_baseline.py` from `caec54a`/`d1e4819` | Port pure coordinate extraction, source-value retention, localized coupled exclusion, approved fallback, blend, and derived wind; adapt to shared tiles. |
| KEEP | `verification/coordinate_matching.py` from `f257f3e`/`d1e4819` | Port dual-cutoff eligibility, latest-time/smallest-digest tie, nearest-event match, QPF/PoP/calm rules, and signed error. |
| KEEP | Pure kernels in `verification/coordinate_evaluation.py` from `f257f3e`/`caec54a`; mixed-coordinate rejection from `coordinate_corpus_binding.py` at `ce2c002` | Port only deterministic metric/grouping logic and identity validation. |
| SIMPLIFY | `contracts/observation_support.py`, `observation_candidates.py`, `coordinate_verification.py`, `asof_corpus.py` | Collapse into normalized observation/verification/error rows plus one support/matching policy. Do not duplicate the same fact as candidate set, corpus, denominator, and report roots. |
| SIMPLIFY | Acquisition behavior from `application/phase3_observations.py` and commits `5668b80`, `93cf491`, `7f282c0`, `1490c40`, `06bbbef`, `32edcac`, `17968e7` | Preserve attempts/outcomes, successful-empty versus failure, manifest authority, provider/local times, response digest binding, and downstream missingness in one compact ledger/fact path. |
| SIMPLIFY | Dirty `tests/support/phase3_group_b.py` and related preserved diff | Use only as scenario inventory for real canonical-guidance-to-coordinate extraction, corrupt-boundary labels, truthful timestamps, and degraded acquisition propagation. Never copy it wholesale. |
| MOVE-LATER | `provenance/runtime_revision.py`, `application/phase3_{resources,calibration,telemetry,replay}.py`, `verification/coordinate_scientific_payload.py`, replay child script | Retain code/lock/environment/OCI identities and local memory bounds now. Defer full checkout attestation, subprocess proof, and host-wide certification. |
| DELETE | `verification/coordinate_{denominators,joint_lattice,evaluation_report}.py`; `application/phase3_{sampler,outside_observer,evidence_directory}.py`; `scripts/run_phase3_resource_acceptance.sh`; monolithic report/corpus/publication portions of `application/phase3_coordinate.py` | Do not port. On-demand fact reductions eliminate their purpose. No donor commit is cherry-picked intact and the branch is never merged as a unit. |

The measured donor delta is 37,013 insertions/77 deletions across 76 files: 16,744
production insertions, 18,701 test/support insertions, and 1,462 script lines. Twenty-four
new Phase 3 production modules total 16,209 physical lines. Its 39 x 5 x 6 x 9 aggregation
shape expands 1,476 opportunities into 768,690 rows. These are evidence for deletion of the
representation layers, not evidence that scientific safeguards should be weakened.

### 8.3 Phase 3 de-bloat RFC at `fecb2a2`

| Disposition | Concept | V2 treatment |
| --- | --- | --- |
| KEEP | Coordinate forecast -> immutable verification facts -> on-demand evaluation; facts as integration boundary; no whole-branch cherry-pick | This is the V2 foundation and first four product PRs. |
| KEEP | Explicit missingness, as-of cutoffs, location/station separation, zero-network replay, scientific kernels, no skill claim | Carry forward as V2 invariants. |
| SIMPLIFY | Exact three-slice internal APIs and five generic artifact schemas | Convert into resource-oriented product/API/storage boundaries. PostgreSQL fact indexes are necessary for location/time discovery and learning. |
| SIMPLIFY | Exact count mandates of nine modules/six APIs | Use hard maxima and dependency rules. Do not require empty modules merely to hit an exact count. |
| MOVE-LATER | Bootstrap uncertainty until a multi-date corpus; platform resource certification | Activate only with genuine sample size or production deployment need. |
| DELETE | No-new-table assumption for facts | V2 requires searchable normalized fact tables. Current `ArtifactRepository` is ID-oriented; S3 or JSON attribute scans are not a product query plan. |

## 9. Refactor-versus-selective-rebuild comparison

Measured references are distinguished from estimates. Current main contains 105 production
Python files and approximately 23,361 physical lines. The old branch numbers above are
measured. Effort, retained-line, and PR estimates below are planning estimates for one
experienced engineer and exclude owner waiting time.

| Dimension | Merge old Phase 3 then refactor | Selective rebuild from current main |
| --- | --- | --- |
| Main code retained | Nearly 100%, but overlaid by 36 changed production files and legacy fixed-station coupling | 100% initially; targeted adapters/kernels reused behind V2 boundaries |
| Old Phase 3 production retained | Estimated 3,000-4,000 of 16,744 inserted lines (18-24%); at least 12,700 lines deleted or substantially rewritten | Estimated 1,200-1,800 cohesive algorithm lines (7-11%) manually ported with provenance; obsolete glue never lands |
| Coupling/blast radius | Starts at 76 changed files, 24 new Phase 3 modules, one 3,949-line coordinator, multiple report/corpus/proof representations | Hard maximum 18 new production modules and five new packages; no all-V2 coordinator; normalized facts isolate later work |
| Data migration risk | High semantic migration: at least 19 branch-only schemas/artifact types must be declared obsolete or adapted despite no SQL migration | Low: no Phase 3 production schema is on main; Phase 0-2 artifacts remain immutable, branch-only schemas are noncanonical/read-only |
| Test burden | Salvage perhaps 4,000-6,000 of 18,701 test lines, delete/rewrite 12,700-14,700, then add API/table/worker tests | <= 220 focused tests and <= 7,500 new/modified test LOC across seven vertical PRs; donor cases harvested selectively |
| Operational complexity | Begins with 8 proof/resource/replay application modules, two scripts, host lease/calibration/evidence lifecycle, then must add real workers/API | Two background worker processes, one PostgreSQL queue, one heavy concurrency slot, ten endpoint operations |
| Report behavior | Must dismantle 768,690-row singleton lattice without breaking duplicated denominator/proof validators | Query only requested groups, <= 100,000 input rows and <= 1,000 result groups |
| Delivery sequence | Estimated 10-14 PRs and 45-70 engineer-days before comparable V2 value | Seven PRs and 35-55 engineer-days to email-complete V2; first one-off value in PR 1 |
| Regression risk | High: removal cuts across intertwined scientific and proof code; a green test may still preserve wrong representation coupling | Moderate: existing Phase 2 tests stay intact; each port has a small independent oracle and acceptance flow |

A greenfield rebuild of Phase 0-2 is rejected: estimated 80-130 engineer-days and it would
recreate already validated provider, grid, unit, blend, provenance, and storage behavior.
"Selective rebuild" means new V2 product boundaries on main plus selective porting, not
throwing away the scientific core.

The single approach selected in Section 1 is supported by this comparison: there is no
merged Phase 3 data to migrate, the desired product needs API/query indexes/workers that the
branch lacks, and most branch volume implements representations explicitly rejected by this
RFC. Refactoring the branch first delays user value and creates a larger negative-diff
review than the positive V2 surface.

## 10. Selective-port strategy

### 10.1 Commit/file sequence

1. Freeze current main at `ce0e0d7` as the V2 budget and semantic baseline.
2. Build shared tile ingestion by reusing current source acquisition, decoding,
   `canonical_v2`, grid, unit, interval, and artifact services. Do not port Phase 3 code.
3. Port location identity from `f257f3e` and coordinate forecast kernels from `caec54a` and
   `d1e4819` into new small contracts/forecasting modules. Adapt inputs to shared tiles.
4. Build normalized forecast state and API around that frozen contract.
5. Port support/revision/matching semantics from `f257f3e`, `5668b80`, `d1e4819`,
   `93cf491`, `7f282c0`, `1490c40`, `06bbbef`, `32edcac`, `17968e7`, and mixed-location
   rejection from `ce2c002`. Rewrite persistence into the four fact tables.
6. Port pure metrics/grouping from `f257f3e`/`caec54a`. Do not port report constructors.
7. Implement learning, AI, and delivery only against accepted fact/snapshot contracts; they
   have no donor-branch implementation authority.

No commit is cherry-picked intact. Every manually ported symbol must cite donor commit/path
in its implementation PR and receive focused tests. The dirty donor worktree remains
read-only; its diff is a scenario source, not a patch source.

### 10.2 Dependency and contamination rules

Automated review must reject:

- any V2 import of `phase3_*`, `coordinate_evaluation_report`,
  `coordinate_denominators`, `coordinate_joint_lattice`, `phase3_resources`, telemetry,
  sampler, outside-observer, evidence-directory, or replay-child symbols;
- any persisted denominator/report partition, zero-cell cube, all-subgroup expansion, corpus
  root universe, evidence bundle, or proof-only publication artifact;
- any API-to-provider/decoder import or synchronous worker invocation;
- any location-keyed model download/cache asset;
- any training/AI write into guidance, observation, baseline forecast, verification, or error
  tables;
- any current-config or network fallback during replay;
- any extension of the 2,678-line `phase2_production.py` coordinator for V2 orchestration;
- any framework whose metadata replaces MesoForge's PostgreSQL/fact provenance.

Old branch-only artifacts, if present in a developer store, remain readable historical
objects under old schema IDs but are excluded from V2 indexes, learning, API responses, and
migration. Never rewrite or relabel them.

## 11. Explicit non-goals and deferred capabilities

V2 does not include:

- a dashboard, map UI, image/tile rendering API, mobile app, or workflow UI;
- a separate service per domain, Kubernetes, distributed workflow engine, Kafka/RabbitMQ,
  Redis, Dask cluster, warehouse, PostGIS, or horizontal scaling without measurements;
- per-location model downloads, full national arrays in API memory, or unbounded backfills;
- RRFS, full ensembles/member grids, precipitation type, snow, severe-weather products,
  climatology, elevation/lapse-rate correction, or new variables beyond approved contracts;
- verification claims for locations without an approved observation-support policy;
- synthetic observations, synthetic learned history, fake bias/weights/confidence/skill, or
  a product claim based on fixture data;
- automatic learned-model activation without a predeclared gate and owner approval;
- automatic AI acceptance at launch, AI-generated baseline meteorology, free-form numeric AI
  control, or AI dependency for forecast issuance;
- billing, subscriptions/payment processing, marketing automation, broad public launch, or
  entitlement coupling to scientific issuance;
- a generalized data lake/report cube, persisted Cartesian evaluation lattice, full corpus
  closure proof, byte-identical Parquet regeneration, or exact-head runtime mutation harness;
- host-wide resource telemetry/evidence certification beyond V2's local admission and memory
  bounds; production capacity expansion needs separate measured operations work;
- infinite retention or full source-to-forecast replay after the declared 90-day source
  window.

Email is included near the end only as opt-in delivery for an immutable forecast snapshot.
A dashboard remains explicitly deferred.

## 12. Quantified complexity budgets

All ceilings are relative to base `ce0e0d7`. Exceeding one stops the PR until scope is
removed or an owner-approved RFC changes the budget. Comments, blank lines, generated
source, and modifications to existing files count toward LOC.

| Measure | Hard V2 limit |
| --- | ---: |
| New/modified production Python LOC | 5,500 total; <= 900 per vertical PR; no new module > 500 physical lines |
| New top-level packages | 5: `api`, `workers`, `learning`, `adjustments`, `delivery` |
| New production modules | 18 maximum; 30 total created-or-modified production modules maximum |
| New physical PostgreSQL tables | 12 maximum, exactly the allowlist in Section 5 if/when its capability ships |
| Background worker types/processes | 2 types and 2 processes: one science, one I/O |
| Heavy worker concurrency | 1 host-wide science unit; no concurrent model decodes/training/replay |
| Queues/brokers | 1 PostgreSQL `jobs` queue; 0 external brokers; 0 Redis |
| Public API endpoint operations | 10 maximum, exactly Section 6; no generic job endpoint |
| Persisted artifact/fact families | 9 maximum: raw guidance, normalized guidance tiles, forecast snapshots/facts, observation facts/raw responses, verification facts, error facts, learned snapshots, AI adjustment audit, delivery state |
| One-off synchronous work | 1 coordinate, <= 36 horizons, <= 64 tile reads, <= 1,000 result records, no provider/decode work |
| Evaluation synchronous work | <= 10 locations, <= 366 days, <= 100,000 selected facts, <= 10 group dimensions, <= 1,000 output groups |
| API latency | Warm p95 <= 750 ms, p99 <= 1.5 s, hard timeout 2 s on representative host |
| Registered refresh unit | <= 25 locations/job; one science job at a time |
| Guidance tile | 64 x 64 core plus one-cell overlap; decode one model/lead/field group at a time |
| Observation work | <= 36 valid times/forecast job, <= 4 attempts/provider request, <= 64 MiB raw response/request |
| In-memory fact processing | 1,000-row default chunk, 5,000-row maximum; no full-corpus dataframe/join |
| Tests | <= 220 new test functions and <= 7,500 new/modified test LOC: >= 60% unit/contract/property, <= 25% integration, <= 15% acceptance/live; exactly one compact acceptance path per PR maximum |
| Vertical PRs | 7 maximum after owner gate; <= 900 production LOC and <= 1,200 test LOC each; no PR > 2,500 total changed lines without renewed owner review |
| Offline gate | <= 15 minutes total; focused tests per PR <= 90 seconds excluding provider canaries |

### 12.1 Shared approximately 8 GB host budget

The host's nominal physical memory is approximately 8 GiB; nominal is not deployable
capacity. Reserve 2 GiB for Linux, PostgreSQL, object storage/MinIO when co-resident, Hermes
and other co-resident services, plus a 1 GiB no-swap safety margin. All MesoForge API and
worker processes together therefore have a **5 GiB aggregate ceiling**, not 8 GiB.

- science worker: <= 2.5 GiB RSS, one heavy unit;
- two API workers combined: <= 768 MiB RSS;
- I/O worker: <= 512 MiB RSS;
- queue/client/buffer/headroom inside MesoForge allocation: <= 1.25 GiB;
- start a heavy unit only with `MemAvailable >= 3.5 GiB`; finish/yield before another unit
  when `MemAvailable < 1 GiB`; never depend on swap;
- API requests cannot load model arrays larger than the 64 object-read/128 MiB response-cache
  limits and cannot wait for the heavy lease;
- background retries reacquire admission and cannot increase chunk, batch, or concurrency
  limits.

Representative tests record maximum RSS and elapsed time for each bounded unit. This is
minimal local correctness and backpressure, not the donor's five-run calibration,
cgroup/OOM proof, outside observer, or platform-wide certification. If the measured host
cannot maintain the 1 GiB margin, reduce tile/batch sizes or move the science worker; do not
relax the floor informally.

## 13. Small vertical PR sequence

**Owner-review gate:** no implementation begins until the owner approves this RFC's
architecture, budgets, and the decisions in Section 14. The seven PRs are ordered. Each must
be independently deployable, preserve prior value, pass full existing tests plus its
focused checks, and receive architecture review before the next dependency begins.

### PR 1 — shared guidance cache and one-off baseline

- **User value:** a caller receives an honest deterministic forecast at an arbitrary covered
  coordinate from one shared cache.
- **Schema/API:** add `jobs` and `guidance_assets`; add `GET /v1/guidance/status` and
  `POST /v1/forecasts`; add FastAPI dependency and tile contract.
- **Acceptance:** one fixture ingestion creates shared tiles once; two coordinates reuse the
  same asset digests; warm requests satisfy limits; missing/stale/outside-domain cases are
  explicit; source units/time/grid/PoP/QPF/wind/gust rules and baseline cold start pass.
- **Allowed donor:** current Phase 2 acquisition/normalization/blend; Phase 3 location and
  coordinate kernels from `f257f3e`, `caec54a`, `d1e4819` only.
- **Deferred:** registration, persistent forecasts, observations, learning, AI, delivery.

### PR 2 — registered locations and forecast history

- **User value:** one saved location refreshes automatically and exposes immutable history.
- **Schema/API:** add `registered_locations`, `forecast_snapshots`, `forecast_facts`; add
  `POST/GET /v1/locations`, `GET /v1/locations/{id}/forecasts`, and
  `GET /v1/forecasts/{id}`.
- **Acceptance:** registration enqueues but never downloads; refresh uses PR 1 assets; CAS
  prevents old completion replacing new; duplicate jobs are idempotent; last-good stale and
  failure states are honest; zero learning history is explicit.
- **Allowed donor:** Phase 3 location identity/tests and coordinate provenance only.
- **Deferred:** verification, evaluation, learning, AI, delivery.

### PR 3 — observations and immutable verification/error facts

- **User value:** a registered forecast later shows what was observed, what matched, what was
  missing, and signed errors.
- **Schema/API:** add `observation_facts`, `verification_facts`, `error_facts`; add bounded
  `GET /v1/locations/{id}/verification`.
- **Acceptance:** provider/local cutoff edges, revision tie, support/location separation,
  successful-empty versus failure, nearest +/-15-minute match, independent QC, exact QPF,
  PoP truth, calm direction, explicit missingness, signed error, corruption, idempotency,
  and zero-network fact replay pass.
- **Allowed donor:** matching/acquisition commits listed in Section 10 and dirty tests only as
  scenario inventory; no corpus/report/resource code.
- **Deferred:** aggregate evaluation, learning, AI, delivery.

### PR 4 — bounded on-demand evaluation

- **User value:** a caller asks a precise quality question and gets counts/metrics without a
  precomputed cube.
- **Schema/API:** no table; add `POST /v1/evaluations` and canonical query/result contracts.
- **Acceptance:** requested-only/empty groups, denominator reconciliation, unit/layer/
  location separation, overlapping-regime labels, circular metrics, QPF contingencies, PoP
  Brier/reliability, paired comparisons, limits, deterministic ordering/identity, and
  zero-network replay pass. No persisted report/denominator/lattice artifacts appear.
- **Allowed donor:** pure metric/grouping kernels only.
- **Deferred:** training/promotion, AI, email.

### PR 5 — gated bias and learned weights

- **User value:** locations with enough valid history may improve deterministically; every
  other location remains safely on baseline weights.
- **Schema/API:** add `learned_snapshots`; expose eligibility/counts and applied snapshot IDs
  through existing location/forecast responses; no new endpoint.
- **Acceptance:** 59/60 and 19/20 bias boundaries, 199/200 and 39/40 weight boundaries,
  cutoff leakage, common-pair selection, rolling holdout, inapplicability, stale snapshot,
  activation/rollback, apply-once correction, and baseline fallback pass. Fixture results
  make no skill claim.
- **Allowed donor:** evaluation facts and current configured blend semantics; bootstrap code
  only if real minimum dates and the approved promotion protocol require it.
- **Deferred:** AI and email.

### PR 6 — bounded AI shadow and reviewed adjustment seam

- **User value:** an operator can inspect auditable, bounded AI proposals against the exact
  deterministic baseline without risking issuance.
- **Schema/API:** add `ai_adjustments`; existing forecast response may expose separately
  authorized adjusted snapshot. No AI-provider public endpoint.
- **Acceptance:** schema rejection, evidence/cutoff binding, per-variable/coupled bounds,
  atomic policy decision, shadow isolation, human-review requirement, kill switch,
  malformed/outage fallback, immutable baseline, and later verification layer separation
  pass.
- **Allowed donor:** no old Phase 3 AI code; use only current architecture safety principles
  and accepted V2 facts.
- **Deferred:** automatic AI acceptance, hazardous fields, delivery.

### PR 7 — opt-in email delivery

- **User value:** an allowlisted owner receives the latest eligible immutable forecast by
  email while the API and deterministic baseline remain authoritative.
- **Schema/API:** add `delivery_subscriptions`, `delivery_attempts`; add delivery `PUT` and
  `DELETE` endpoint operations.
- **Acceptance:** opt-in/auth, timezone/unit presentation-only behavior, immutable version
  reference, stale/not-ready policy, degraded/missing labels, baseline-only fallback,
  idempotent send, retry, duplicate suppression, provider failure, unsubscribe/suppression,
  and retention pass.
- **Allowed donor:** none beyond immutable forecast/provenance conventions.
- **Deferred:** dashboard, billing, broad launch, marketing, automatic AI acceptance.

## 14. Architecture risks, alternatives rejected, and unresolved owner decisions

### 14.1 Principal risks and mitigations

| Risk | Consequence | Mitigation |
| --- | --- | --- |
| Shared tile cache is too large or slow | Ingestion misses cycles or exhausts host | Start with approved region, 64 x 64 overlapped tiles, one field group decode, measured RSS/time, lifecycle retention; move science worker before expanding. |
| Current Phase 2 normalizer is coupled to a small bbox | Per-location artifacts reappear or rewrite grows | Treat shared tile generation as a new output adapter over proven decode semantics; never call it from API; test two locations share digests. |
| Fact tables duplicate artifact metadata | Conflicting authorities | Facts are canonical query rows; artifact manifests own large bytes/lineage. Transaction validates IDs/digests both ways and forbids JSON-attribute scans. |
| PostgreSQL fact volume grows | Slow evaluation/training | Composite location/time/variable/layer indexes, monthly partitions only when measured, 100,000-row query cap, streaming chunks; no warehouse now. |
| Observation proxy poorly represents a location | False hyperlocal skill | Separate identities, support/elevation/distance policy, explicit unsupported facts, location-specific claims only after valid evidence. |
| Sparse learning overfits | Degraded forecasts disguised as improvement | Hard count/date/common-pair gates, rolling holdout, subgroup/tail/calibration vetoes, immutable candidates, owner activation, baseline rollback. |
| Exact coordinate is personal data | Privacy/security exposure | Authenticated access, versioned privacy/retention, minimization and deletion policy before broad registration; never retain one-offs by default. |
| Replay claim outlives executable/source retention | Irreproducible promise | Expose capability level, retain source/lock/OCI for 90 days, preserve facts longer, and state fact-only replay after source expiry. |
| AI is plausible but unsafe | Forecast harm | Structured deltas only, deterministic atomic policy, shadow first, human review, kill switches, immutable baseline fallback. |
| Email sends stale or duplicate forecast | User harm/loss of trust | Snapshot/freshness policy, idempotency key, attempt ledger, not-send state, unsubscribe and suppression tests. |
| Orphan objects accumulate | Storage leak | Verified upload-before-row rule plus bounded temporary-object reconciler; general GC deferred. |
| Non-exclusive evaluation groups double-count | Misleading totals | Mark overlapping groups, show base denominator/status counts, prohibit summing regimes as disjoint totals. |

### 14.2 Alternatives rejected

- **Merge/refactor the old Phase 3 branch:** rejected by the measured 37,013-line/76-file
  surface, 768,690-row lattice, proof coupling, and absence of product API/tables.
- **Greenfield rewrite:** rejected because Phase 0-2 provider, scientific, provenance, and
  storage behavior is already validated and difficult to reproduce safely.
- **Per-location ingestion/cache:** rejected because cost scales with registrations and
  violates ingest-once/derive-many.
- **Precompute forecasts for every possible coordinate:** impossible and unnecessary; cache
  model space and extract bounded points.
- **Persisted report cube/Cartesian lattice:** rejected because normalized facts can answer
  bounded queries and zero cells have no durable product value.
- **Generic artifact store only, no fact index tables:** rejected because current repositories
  support identity lookup, not efficient location/time/variable discovery. S3 listings and
  JSON attributes are not query indexes.
- **Microservices/workflow platform/message broker:** rejected until team/load/failure-domain
  evidence justifies network boundaries; it adds operations without improving science.
- **Heavy async forecast API:** rejected because one point over ready tiles is bounded and
  useful synchronously; not-ready must remain honest rather than hiding downloads behind a
  job.
- **Dashboard before delivery reliability:** rejected; API and email prove useful product
  behavior with less surface. No dashboard now.
- **AI before verification/learning facts:** rejected because it cannot be evaluated,
  bounded by evidence, or attributed credibly.

### 14.3 Genuine unresolved owner decisions

Implementation must not infer these product/policy choices:

1. **Launch coverage and cadence:** exact initial service-region polygon, model cycles,
   issue cadence, fields/horizons, and forecast freshness/degraded deadlines within this
   architecture's hard resource limits.
2. **Location privacy and retention:** whether household-level coordinates may be stored,
   required authentication/authorization, privacy class, coordinate display precision,
   deletion/export behavior, and whether the proposed 2/5-year fact retention is acceptable.
3. **Observation truth policy:** approved provider/station inventory, distance/elevation
   support thresholds, revision cutoff latency, and whether METAR is sufficient for each
   launch variable/location claim.
4. **Learning promotion policy:** owner-approved primary metrics, holdout window,
   non-inferiority/tail/calibration thresholds, rollback threshold, and who may activate a
   learned snapshot. Section 4's sample minima are necessary, not sufficient for skill.
5. **AI policy:** provider and data-retention terms, variable/lead adjustment bounds,
   hazardous exclusions, evidence requirements, reviewer roles, and whether reviewed
   application is enabled after shadow evidence. Default is shadow-only.
6. **Email product policy:** provider, allowlist, delivery time/SLO, maximum acceptable stale
   age, whether degraded baseline is sent or skipped, sender/compliance identity, and
   retention of rendered bodies/provider metadata.
7. **Storage/replay cost:** whether 90-day raw/full replay, 30-day tiles, and longer fact
   retention meet cost and reproducibility expectations; changing full replay duration
   changes storage and release-artifact retention.

Technical choices such as modular monolith, native-grid overlapped tiles, PostgreSQL fact
indexes/queue, S3 artifact bytes, no Redis/broker, logical Parquet equality, two workers,
and request/resource bounds are decided by this RFC and are not owner questions unless a
budget must change.

## 15. Final recommended V2 exit criteria

V2 is complete only when all of the following are demonstrated with exact source/head,
configuration, artifact/fact digests, commands, and measured results:

1. The owner approved this RFC before implementation and separately approved every product
   decision required for the shipped scope.
2. The seven or fewer vertical PRs landed in dependency order and remained within all LOC,
   module/package, table, endpoint, worker, queue, test, latency, and memory ceilings.
3. A scheduled HRRR/NBM/GFS fixture/live-bounded cycle is acquired once, normalized into
   shared tiles, and used by at least two arbitrary coordinates with identical shared asset
   identities and no per-location provider request.
4. A warm one-off request returns the approved deterministic fields/horizons with exact
   cycles, values, units, intervals, baseline weights, contributor/exclusion provenance,
   freshness/missingness, and honest zero-history state within the latency/resource budget.
5. A registered location refreshes from the same cache, maintains immutable snapshot/fact
   history, handles duplicate/concurrent jobs, never moves its current pointer backward,
   and serves last-good stale/degraded/not-ready state honestly.
6. Scientific tests prove UTC/as-of cutoffs, canonical units, temporal validity, exact QPF
   windows, named PoP semantics, dew-point consistency, earth-relative U/V, vector-derived
   direction, circular scoring, no invalid partial sustained-wind tuple, calm-direction
   missingness, and independently nullable gust.
7. Observation revisions are append-only and provider/local times are distinct; support
   keeps forecast/station identity separate; every opportunity records explicit forecast,
   observation, pair, and score status; every scoreable scalar error is forecast minus
   observation.
8. A bounded evaluation returns only requested groups with reconciled denominators,
   explicit empty/null reasons, deterministic ordering/identity, correct continuous,
   circular, QPF, and PoP metrics, exact common-pair comparisons, and no report lattice or
   pre-materialized zero-cell artifacts.
9. Full source-to-forecast and observation-to-fact replay make zero network calls and
   reproduce logical values/identities under pinned source/config/code/lock/environment
   within the declared 90-day window. Fact-only evaluation replay works for the longer fact
   retention window and the API labels the capability accurately.
10. Learning below every minimum leaves baseline weights, zero/absent correction, null
    confidence, and no skill claim. Any activated bias/weight snapshot is immutable,
    cutoff-clean, out-of-time evaluated, owner-approved, attributable, applicable, and
    rollback-tested without rewriting prior forecasts/facts.
11. AI is structurally unable to alter deterministic baselines or facts. Shadow proposals
    are schema/evidence/policy bound; provider/policy failure returns the baseline. If
    reviewed application is owner-enabled, the complete adjusted snapshot is bounded,
    atomic, human-approved, auditable, and separately verified.
12. An opt-in allowlisted email references exactly one eligible immutable forecast snapshot,
    presents timezone/units without changing science, labels stale/degraded/missing state,
    falls back to baseline, and passes duplicate, retry, failure, unsubscribe, suppression,
    and not-ready tests.
13. The representative co-resident workload stays below 5 GiB aggregate MesoForge memory,
    the science worker stays below 2.5 GiB RSS with concurrency one, `MemAvailable` remains
    at least 1 GiB between bounded units, no swap is required, and API p95/p99/hard timeout
    objectives pass.
14. Existing Phase 0-2 quality, unit, contract, property, integration, and acceptance tests
    remain green; V2 OpenAPI compatibility, migrations, downgrade/upgrade where supported,
    import boundaries, docs, hygiene, and repository checks pass.
15. No dashboard, per-location ingestion, fake learning/confidence, report lattice,
    branch-wide Phase 3 import, automatic AI acceptance, billing, or unsupported forecast/
    verification/product claim has entered the V2 scope.

Owner review must resolve the seven product-policy decisions above before the seven bounded
product increments begin. Implementation then follows the single approach selected in
Section 1 without merging the old Phase 3 branch or weakening its valid scientific
safeguards.
