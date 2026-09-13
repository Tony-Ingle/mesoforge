# RFC: MesoForge V2 architecture

Status: Proposed for owner architecture review

Decision owner: MesoForge owner

Architecture author: Codex

> **Reference status (2026-09-09):** The source/donor descriptions below record the
> inputs used when this RFC was written, not current branch or working-tree state.
> The later preserved Phase 3 donor is
> `43f56bc0c67ab782c94fb6349d65523793e1a836`; see the
> [archive index](../archive/README.md). This RFC remains proposed: its decision log
> records design choices under review, not blanket owner approval. The entry-point
> summary is [VISION.md](../../VISION.md).

Source baseline: `origin/main` at `ce0e0d77e645d31ca33caaac2d20f4f8748dc90e`

Read-only implementation donor: `claude/phase3-coordinate-verification` at
`17968e79749bcd958a69e3a4f0fc03eddea00efd`, including its preserved uncommitted
four-file test/support diff.

Read-only design donor: `docs/rfcs/phase-3-debloat.md` at
`fecb2a2b2db10799f18b18357e7e4b0bb1e1a65e`.

## 1. Decision summary

MesoForge's long-term north star is an automated GFE-style local digital forecast
system. A configured latitude/longitude centers a local forecast domain: shared
model guidance becomes coherent MesoForge fields, learned deterministic corrections
and bounded AI tool recipes may later adjust those fields, and interpolation at the
exact coordinate produces the delivered spot forecast. A point-value blending API
is an early capability, not the complete product architecture.

The selective rebuild reuses scientific kernels and infrastructure whose contracts
remain valid, ports cohesive donor algorithms with independent tests, and replaces
obsolete Phase 3 product architecture. The `main` baseline and donor revisions above
describe the original design inputs, not instructions to restart current development.

The central data-flow decision is **ingest shared guidance once, derive forecasts for many
locations**. Background work acquires and normalizes each supported model cycle once into a
shared spatial cache. Nearby locations reuse source data. Local MesoForge forecast
fields are derived products, distinct from complete native model datasets; registration
does not require a separate source download or native-dataset copy for each location.

The **First Usable Release** provides only:

- a private, operator-controlled API on a bounded supported region;
- deterministic baseline forecasts for arbitrary supported coordinates;
- persistent immutable forecast and verification history for registered locations;
- observation acquisition and deterministic matching;
- basic bounded performance queries over normalized facts.

Learning, bias correction, learned model weighting, a bounded AI forecaster, email, and
public multi-user/account capabilities are **Future Roadmap** items. They do not gate a
first-release slice, acceptance, or usefulness.

The first release is useful without learned history: it turns shared model guidance into a
reproducible, unit-correct point forecast; preserves what was issued; matches later
observations; and exposes measured baseline performance. A new location reports no learned
history and makes no learned-skill claim. The system never invents samples, confidence,
bias, weights, or skill.

The owner has clarified this long-term direction and separately approved implemented
slices. The rest of this RFC remains proposed; documenting the direction does not
approve new forecast behavior or every release stage. Its delivery sequence is a
non-binding estimate, not a required PR count.

## 2. Release boundary

### 2.1 First Usable Release

1. Operators configure service coverage, model/product mix, fields, cadence, horizons,
   deterministic baseline weights, and safety bounds. Each user-facing location needs
   only latitude/longitude; geographic metadata is derived internally (section 2.3).
2. Production automation acquires each eligible cycle once, normalizes it, and publishes a
   shared cache only after required assets are complete and verified.
3. The private API computes deterministic point forecasts from ready shared guidance.
4. Operators may register locations. Background issuance stores immutable snapshots and
   normalized facts and advances a small mutable current-state pointer.
5. Background observation work acquires revisioned observations, selects support under an
   explicit policy, and creates immutable verification facts after valid time.
6. Operators run basic bounded performance queries on demand without a persisted report
   cube.

Authentication may initially be deployment-level operator authentication or a small service
credential boundary. It is not public SaaS identity or tenancy.

### 2.2 Future Roadmap

The long-term field direction includes temperature, dew point/RH, wind/gust,
clouds, QPF, PoP, precipitation type, snow, and other useful fields as their
scientific contracts are implemented. Conditions derive from underlying forecast
fields rather than an unexplained standalone prediction. The code-grounded canvas
inventory and proposed deterministic condition layer are in section 6.7. Precipitation
type and subsequent native evidence increments are complete; the next proposed slice
is a conservative condition preview, not another meteorological field.

**Owner model direction, 2026-09-10:** the long-term model mix includes HRRR,
RAP, NAM 3 km, NAM, GFS, RRFS / REFS, and NBM, with useful deterministic and
ensemble guidance including GEFS, ECMWF, and Canadian models where appropriate.
These are planned integrations, not completed support or one implementation task.
The retained Phase 2 HRRR/NBM/GFS station pipeline includes QPF/PoP; it remains a
technical reference, not evidence of precipitation support in the V2 coordinate path.

**Implemented core status, with the full 2026-09-13 inventory in section 6.7:**
the on-demand forward run provides real
36-hour temperature, dew point, derived RH, vector wind and gust. HRRR/GFS are
active: temperature retains the fixed 70/30 demonstration recipe throughout;
added dew-point and U/V/gust fields use the retained Phase 2 70/30 row for hours
1–18 and 60/40 row for hours 19–36, with approved fallbacks. RAP and IFS are real
zero-active-weight shadows. IFS retains native three-hourly values and gaps, and
its interval-maximum gust is unavailable under the instantaneous-gust contract.
Automatic current-cycle discovery, coordinate-derived shared preparation, local
batch/forward runs, immutable PostgreSQL/MinIO issuance, temperature verification,
and model comparison exist. A coordinate-derived local surface grid covers the
context domain and its smaller editable subset; the exact center-node forecast is
extracted from those fields. Delivered cloud cover remains unavailable, while optional
attachments now retain cloud and other surface/winter evidence. Temporary native
p-type agreement is implemented. There is no deterministic bias correction, AI editing,
complete weather-condition engine, production deployment or scheduling yet. Detailed commands, evidence and limits
belong in [README.md](../../README.md).

HRRR/GFS liquid precipitation now uses retained interval normalization and QPF rows
across the same context/editable grid. Each amount retains exact hourly bounds and
native parent provenance; GFS same-bucket differencing precedes spatial extraction.
RAP/IFS precipitation is not enabled. Deterministic QPF does not imply probability
or precipitation type, and small positive amounts are not spatially cleaned.
Native NBM one-hour PoP now accompanies this grid as a separate field: probability
of liquid accumulation strictly greater than 0.254 kg/m² (0.01 inch), with NBM-only
passthrough. Native percentages, event identity, accumulation bounds and source evidence
are retained; missing or incompatible intervals are unavailable. Other surface/QPF
rules are unchanged and no probabilities are inferred from deterministic amounts.

Contributor capabilities and named/versioned recipes retain configuration snapshots,
active contributions/applied weights, and separate shadows in immutable issuance.
Historical records remain readable; shadow gaps do not change the active blend or
its verification eligibility. No silent weight redistribution or learned weighting
is introduced.

The contributor lifecycle is **shadow → evaluated → active → deprecated → retired**.
These are explicit configuration states, not automatic promotions. Evaluated evidence
does not approve activation; changing active recipes requires owner authorization.
Retirement disables new use while preserving historical identities and retained facts.
New real models still require suitable acquisition/normalization adapters and
capability registration; lineage-aware weighting and automatic promotion are not
implemented.

NAM/NAM 3 km are transition candidates. The September 9 NWS SCN 26-47/26-48
updates schedule NAM and its nests' retirement and RRFS/REFS replacement for
October 14, 2026 at 12:00 UTC, subject to weather-related delay. The source links
and verification date are in [VISION.md](../../VISION.md#long-term-model-direction).
Earlier donor descriptions and older NOAA target dates do not override those notices.

Retain raw model files/messages actually acquired, including currently unused
fields, separately from prepared subsets. Later field/product trimming requires
a separate decision. This direction does not authorize unbounded acquisition of
fields, levels, leads, or models, nor settle indefinite-retention guarantees.

- deterministic site/regime bias correction from verified history, inspectable site
  knowledge, and evaluated model weighting;
- bounded GFE-style spatial/temporal AI edit recipes executed by deterministic,
  versioned tools, with adjustment performance measured against the bias-corrected baseline;
- email or other delivery;
- public/multi-user access, accounts, ownership, privacy controls, sharing, export,
  deletion, billing, and public SLOs;
- broader geography, model mix, fields, horizons, retention guarantees, and analytics.

Learning, AI and delivery build on the numerical and verification foundation in separately
approved stages. No first-release acceptance depends on simulated learning, AI, email,
public accounts, long-term retention approval, or public SLOs.

### 2.3 Coordinate-driven operating direction

**Owner direction clarified, 2026-09-11:** the user-facing geographic input is only
latitude/longitude. Configured/registered locations are the persistent centers of
local forecast domains, history, verification and eventual learning, AI forecast-desk
work and delivery. Ordinary one-off requests may return numerical point forecasts
without registration or tracking. This operating direction is approved; its
implementation details and the remaining RFC remain proposed unless separately approved.

A configurable collection should look conceptually like:

```json
{
  "locations": [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 44.98, "lon": -93.27}
  ]
}
```

The local preparation and batch commands now accept this format and automatically
expand spatial coverage using retained model messages, including the surface fields
selected by the forward run. Both example
coordinates forecast successfully; the former fixed demonstration rectangle is retired.
New coordinates require no code changes. Owner-approved internal defaults are:

- Exact latitude/longitude is the forecast point.
- 50 km minimum model-data preparation buffer.
- 150 km surrounding weather-context footprint for the selected available fields.
- Existing 50 km observation-station search, with suitability and time/QC rules unchanged.

Preparation inspects the whole collection, derives conservative geographic envelopes,
merges overlapping footprints, reuses sufficient prepared guidance, and otherwise
prepares shared native-grid views from retained full messages. New source cycles use
existing bounded acquisition once before serving. Distant footprints can use separate
views of the same acquisition. Native-cell interpolation and raw/prepared checksums
remain intact. Context stops at physical model boundaries; unsupported coordinates
are invalid geographic input or points outside the native model domain. Insufficient
prepared coverage instead requires preparation outside HTTP (HTTP 409).

These are current internal preparation and observation defaults, not the radius,
resolution, shape or extent policy for an editable MesoForge grid. They do not
change the locations file. The current surface baseline uses one WGS84
azimuthal-equidistant lattice for the context domain and nested editable subset.
Each node explicitly records editable/context-only and forecast-point membership;
the exact configured point remains the center node. Signed projected Euclidean
distance to the editable boundary is positive inside, zero on the boundary and
negative outside. This supports a future smooth taper without applying edits or
settling taper policy. Geometry parameters, axes, extents, membership masks and
source/code identities are retained for deterministic replay. Measured defaults
are recorded in [README.md](../../README.md#local-surface-baseline-grid); permanent
grid spacing, geometry, taper distances, weather-dependent sizing and storage
layout remain open. This does not implement the full future lifecycle below.
MesoForge derives location identity, nearby observation candidates, bounding boxes,
surrounding counties, native-grid coordinates, and any spatial zone or surrounding context internally
when needed. Users do not maintain those derived geographic inputs. Service coverage,
available guidance, and scientific suitability still bound what can be supported.

The API and persistent forecast data belong on a VPS. A GitHub Actions workflow
can read the configurable coordinate collection and invoke processing sequentially
or in bounded batches. Acquisition/preparation stays outside forecast HTTP requests
and produces shared guidance reusable across nearby coordinates, not per-location
downloads. Section 6.6 describes the intended location lifecycle, including later
AI and delivery stages. This direction does not implement or approve a combined
Actions, registration, verification, AI, and delivery milestone.

## 3. Terminology

- **Guidance cycle:** a model/product run identified by reference time and revision.
- **Shared cached guidance:** guidance acquired and normalized once per cycle into spatial
  objects reused by many coordinates.
- **Source guidance domain:** shared model coverage sufficient for interpolation and
  surrounding meteorological context, reused across nearby locations.
- **Context domain:** the larger area the automated forecaster may inspect for incoming
  systems, gradients, fronts, precipitation structures, freezing lines, surrounding
  observations and model disagreement, including evidence outside the editable area.
- **Editable domain:** the smaller bounded area where approved tools may modify local
  MesoForge forecast fields. Context access does not grant permission to edit it all.
- **Forecast point:** the exact configured latitude/longitude used to interpolate the
  delivered spot forecast from final local fields, never a replacement grid-cell center.
- **Local MesoForge forecast field:** a derived field on MesoForge's forecast grid,
  distinct from each source model's native-grid guidance. Grid design remains open.
- **Forecast:** MesoForge values with contributors, configuration, units, quality,
  missingness, cutoffs and lineage; the deterministic baseline remains identifiable
  separately from later corrected and adjusted products.
- **Issued forecast snapshot:** one immutable persisted forecast version. Refresh creates a
  new version and never edits an earlier one.
- **Forecast state:** a small mutable pointer/status for the current issued snapshot; it
  contains no mutable forecast values.
- **Registered location:** a normalized scientific coordinate selected by an operator for
  recurring issuance, persistent history, observation matching and verification;
  it is also the home for later site learning, bias correction, AI and delivery.
- **One-off request:** a bounded coordinate forecast that does not silently register a
  location or create durable history.
- **Observation revision:** a value with provider/station, event or interval time,
  authoritative availability, local ingestion, unit, quality, and revision identities.
- **Verification fact:** an immutable matching opportunity with a selected observation or
  explicit missing/unscoreable reason.
- **Normalized fact:** a typed, schema-versioned, unit-explicit row with stable identity and
  references to shared lineage.
- **Evaluation:** an on-demand bounded reduction over normalized facts, not a
  pre-materialized report lattice.
- **As-of cutoff:** a UTC instant limiting eligible information. Provider availability and
  local ingestion independently must meet it.
- **Missingness:** a machine-readable absence reason. Missing is not zero.
- **Lineage header:** a compact immutable record for a snapshot or batch that pins
  code/configuration/environment, cutoffs, activity, and parent artifacts. Facts reference
  it rather than duplicating the metadata.
- **Replay:** zero-network recomputation from retained checksum-verified inputs and pinned
  implementation/configuration without source discovery. Retained original source bytes are
  authoritative; available replay depth depends on which inputs and execution dependencies
  remain retained.
- **Thin API:** a private HTTP boundary that rejects unsupported work before expensive reads
  and performs only bounded extraction or reduction.

The coordinate ID is strictly scientific and derives only from the canonical coordinate.
Labels, accounts, ownership, authentication, privacy, sharing, export, deletion, billing,
and delivery preferences are separate product metadata or later concerns and never enter it.

## 4. Invariants

These describe the proposed full-release architecture alongside applicable retained
scientific contracts. In particular, the current model-set selection proves provider
availability at decision time and records later acquisition/issuance separately; it
does not implement the broader availability-and-ingestion cutoff design below.

- **Shared acquisition:** cache identity includes model/product/cycle/revision, field,
  lead/interval, grid/partition, transform, and content identity; never a location.
  Derived local fields reference shared source guidance rather than duplicating complete
  native datasets per configured location.
- **Spatial authority:** the system derives source/context/editable coverage from the
  coordinate and approved policies. Tools enforce edit-domain limits even when relevant
  meteorological evidence lies outside them. Users configure no boxes, cells or stations.
- **Coordinate identity:** EPSG:4326 decimal input is range-validated; `180` becomes
  `-180`; values use five decimal places, round-half-to-even, positive zero, JCS bytes, and
  a full SHA-256 key.
- **Coordinate/station separation:** a station never replaces the forecast coordinate.
  Distance, elevation, support decision, effective time, and station identity remain clear.
  Observation sources/proxies are selected automatically using suitability rules;
  users are not required to configure stations or context zones.
- **Immutable issuance:** published assets, lineage headers, snapshots, forecast facts,
  observation revisions, and verification facts are append-only. Mutable state is limited
  to leases and versioned pointers. Original baseline, bias-corrected fields, AI
  proposal/edit recipe and final adjusted fields remain separately identifiable and
  immutable when those stages exist; an adjustment never overwrites its baseline.
- **Correct time:** issue, reference, provider availability, ingestion, event, valid,
  interval-bound, verification, and query/build times are distinct UTC fields.
- **No leakage:** forecast inputs meet both availability and ingestion cutoffs. Observation
  selection meets verification and evaluation as-of cutoffs. Event time is insufficient.
  Later correction history, site knowledge and AI context also retain eligibility evidence
  and their applicable cutoffs; a later run must not inject future information.
- **Explicit missingness:** every declared registered forecast and verification opportunity
  has a status. Null plus reason never becomes zero.
- **Units:** temperature K; wind m/s; meteorological wind-from direction in degrees; QPF
  `kg m-2`; PoP `[0,1]` for a named event/interval. Conversion precedes blending/scoring.
- **Wind:** rotate grid-relative U/V before blending; blend U/V before speed/direction;
  never angle-average. Calm direction is unscoreable. Gust is independently nullable. When
  a configured source-integrity rule proves that a present gust corrupts its associated
  sustained-wind values, reject that source's U/V and gust together only for the affected
  coordinate and horizon; preserve independent fields, other sources, and other horizons.
- **Thermodynamics/precipitation:** source dew point must satisfy
  `dew_point <= temperature + 1e-6 K`; reject invalid source values rather than clamping
  them. A violation makes only that source's dew-point value missing for the affected
  coordinate and horizon; preserve its temperature and all unrelated fields, sources,
  coordinates, and horizons. QPF is nonnegative with exact interval bounds. QPF does not
  synthesize PoP; missing accumulation is not zero.
- **Baseline blend:** contributor set and lead band select a reviewed versioned weight row.
  Fallback is explicit/degraded; weights are not invented or silently renormalized.
- **Deterministic editing:** AI proposes bounded tool recipes, never unrestricted grid
  writes or unchecked numerical publication. Versioned deterministic execution and
  validation enforce physical consistency, approved bounds, spatial/temporal continuity,
  information cutoffs, edit-domain limits and applicable cross-field relationships.
- **Deterministic replay:** fixed retained inputs and configuration reproduce the
  numerical baseline, correction and execution of a saved edit recipe. Rerunning an AI
  model need not reproduce its original proposal; preserve that proposal as evidence.
- **Errors:** scalar error is forecast minus observation. Direction uses circular difference
  in `[-180,180]` with separate absolute circular metrics.
- **No fake learning:** first-release responses state learning is unavailable; roadmap
  fields are not presented as populated.
- **Replay identity:** identities, parent links, cutoffs, units, missingness, algorithms, and
  lineage support the replay level advertised for retained data.

## 5. Architecture and responsibilities

### 5.1 Modular monolith

```text
private API -> application services -> scientific/domain contracts
workers     -> application services -> scientific/domain contracts
application services -> storage interfaces
storage adapters      -> storage interfaces + contracts

Future configured-location path:
shared guidance -> local grid baseline -> deterministic bias correction
bias-corrected fields + context/evidence -> AI tool recipe
saved recipe + corrected fields -> deterministic tools/validation -> final fields
final fields -> exact-coordinate interpolation -> immutable spot forecast/delivery
learning    -> immutable forecast/verification query contracts
```

Current scientific and infrastructure packages remain donors after contract review. Package
and module counts are design outcomes, not compliance targets. Another runtime role, durable
authority, dependency cycle, broker/workflow framework, or data representation triggers
architecture review.

### 5.2 Runtime roles

| Role | First-release responsibility | Prohibited |
|---|---|---|
| Private API | Authenticate operators; validate measured bounds; bounded point extraction/evaluation | Download, regional decode, rebuild, training, AI, email, arbitrary jobs |
| Science worker | Scheduled guidance acquisition, bounded normalization, cache publication, registered issuance, repair/replay | Request-coupled acquisition or concurrent unbounded heavy work |
| I/O worker | Observation acquisition, revision ingestion, matching, verification | Training or unbounded analytical scans |

The intended deployment places the API and persistent forecast data on a VPS. These
roles may share an approximately 8 GB host. Worker concurrency and unit size are enforced
safety bounds; one memory-heavy science unit runs at a time and I/O concurrency is bounded.
A representative measurement must show a real margin using peak RSS and system
`MemAvailable`. Per-process allocations are provisional until measured. If the margin
cannot be held under co-load, reduce units or move science work; do not consume the reserve.

### 5.3 Queue and governance

The proposed internal queue is a PostgreSQL jobs table with lease expiry,
`FOR UPDATE SKIP LOCKED`, idempotent keys, and atomic publication. The optional
GitHub Actions workflow in section 2.3 is an external caller for configured location
processing; persistent forecast data stays on the VPS. This does not settle the
queue or scheduling implementation. Do not add a second broker/workflow system
without measured need and owner architecture review.

Human governance and production automation are separate:

- humans approve architecture, review code, run release checks, and decide merges;
- schedulers/workers perform only configured ingestion, issuance, observation, verification,
  and approved deterministic policies;
- automation cannot approve architecture, merge code, widen scope, activate future
  learning/AI, or bypass policy.

### 5.4 Shared cache

Shared source guidance remains separate from the local MesoForge forecast grid.
Store native-grid spatial partitions with overlap sufficient for deterministic interpolation
and coverage for the surrounding context required by configured locations.
Manifests pin grid, coordinates, field/level, lead/interval, units, transform, bounds, parent
raw object, and digest. Publication is atomic after validation.

Partition shape, field grouping, compression, packaging, cache size, and retention are
**provisional hypotheses**. A `64 x 64` core plus one-cell overlap is a benchmark candidate,
not acceptance or a local forecast-grid specification. Section 7 chooses initial packaging
and measured work limits.

For a configured location, the system derives context and editable domains around the
coordinate and regrids/blends eligible numerical guidance into coherent local MesoForge
fields. The context domain is larger than the smaller editable area: an approaching
front or precipitation feature outside the edit boundary may justify a change inside it.
The exact point is sampled from the final local fields. Source grids, local grid geometry
and interpolation/regridding transforms retain their separate identities and semantics.
Nearby local domains reuse shared guidance; this does not require duplicating complete
native datasets or invent a new storage authority. Resolution, geometry, overlap and
boundary handling remain implementation decisions subject to scientific checks and
measurement, not values settled by the current preparation defaults.

The later AI forecast desk inspects context, model disagreement, eligible observations,
verification history and versioned site knowledge. It acts like a meteorologist using
GFE tools by selecting structured, bounded operations with a field, region, time range
and permitted parameters. Future examples include a regional/time-bounded delta, spatial
or temporal taper, value anchor with surrounding blending, artifact smoothing, shifting
or retiming a precipitation feature, adjusting a freezing-line/rain-snow transition,
removing unsupported isolated trace QPF, and modifying a coherent region while preserving
continuity at its boundary. These are examples for later design, not implemented tools
or approved numerical algorithms.

Deterministic versioned tools execute retained recipes against the identified corrected
forecast. Validation checks physical and cross-field consistency, parameter/value bounds,
continuity, cutoff eligibility and the editable domain before any result can be issued.
Invalid proposals retain an explicit rejection reason; a permitted fallback to unchanged
corrected fields must be recorded. Human approval governs tool/policy development and
release, not each normal configured forecast. The AI cannot bypass that policy.

## 6. First-release flows

### 6.1 Ingest

The scheduler creates one idempotent job per configured cycle. The science worker acquires
provider objects once; records revision, availability, ingestion, and content identity;
decodes one measured bounded group at a time; normalizes grid/unit/interval semantics; and
writes shared candidates. One lineage header covers the normalization activity. The cycle
becomes ready only after the required manifest is complete and verified, then may enqueue
bounded registered issuance batches.

### 6.2 One-off forecast

`POST /v1/forecasts` accepts one coordinate and approved field/horizon subsets. Up to 36
horizons is the desired maximum envelope, not an unconditional promise. Before cache reads,
the API checks region/readiness and a packaging-aware work estimator populated by the
representative benchmark. Unsupported combinations or work beyond measured read, decode,
memory, output, or timeout bounds receive stable `422`.

The API reads enclosing objects, extracts the point, applies deterministic weights and
field-specific operators, and returns values, units, contributors/exclusions, freshness,
cutoff, missingness, baseline configuration, and compact lineage. It does not persist
registered history. The benchmark may support every required field across 36 horizons or
only measured subsets; the advertised matrix follows evidence.

### 6.3 Registered issuance

Registration stores scientific coordinate separately from operator metadata. The science
worker reuses shared guidance and scientific kernels in measured batches; later local
field generation feeds the same issuance boundary (section 6.6). A transaction inserts an
immutable snapshot header, lineage reference, and all declared facts including missing
rows; only completeness permits compare-and-swap of the current pointer. Failure leaves the
prior snapshot immutable and visible with honest stale/failed state.

### 6.4 Observation and verification

The I/O worker acquires raw observation windows once and stores revisions with
event/interval, availability, ingestion, units, quality, and lineage. Effective-dated
station support uses deterministic distance/elevation/variable/tie policy. Matching uses
exact variable/interval semantics and both cutoffs. Every declared registered opportunity
emits an immutable verification fact: scoreable, missing, unsupported, quality rejected,
interval mismatch, cutoff excluded, or another stable reason. Late revisions create new
identities and, if policy permits, superseding facts; earlier as-of results remain replayable.

Support selection is automatic from the forecast coordinate and records the selected
observation station/source as a proxy, with the suitability decision. Users do not
supply a station list. No suitable observation means explicit unavailable verification,
not a fabricated score or a reason to block generation of the next forecast.

Implemented local increment: station discovery is lazy and coordinate-driven. It
checks saved candidates by coordinate and versioned 50 km policy, otherwise queries
bounded official AviationWeather stationinfo metadata and filters by WGS84 distance.
Raw metadata, acquisition time, candidate metadata/distances and lineage use the existing
PostgreSQL/MinIO artifact path. Repeated coordinates reuse that immutable snapshot.
Refresh/revalidation can create a new version without changing locations.json; scheduled
refresh, a nationwide catalog mirror and full effective-dated station history remain
future work. The existing time/QC and nearest-station rules still govern observation
suitability. A missing elevation remains explicit and cannot satisfy the current
metadata-tolerance QC. Historical configuration-based snapshots remain readable.
This increment does not implement the full worker, registration or lifecycle design.

### 6.5 Evaluation

The API validates filters, groups, metrics, range, as-of cutoff, rows/work, and output before
querying. Indexed normalized facts are selected without scanning object attributes or
enumerating unrequested Cartesian cells. Errors are derived on demand and missing counts
remain explicit.

Ordinary responses contain canonical query identity, aggregate lineage summary/watermark,
algorithm/version, eligible/scored/missing counts, and requested aggregates, with an
optional audit handle. Per-source digests/fact references appear only in explicitly
requested, separately bounded audit detail.

### 6.6 Intended configured-location lifecycle

The full future lifecycle composes the responsibilities above. Local coordinate
batch/forward runs already combine verification and numerical issuance; persistent
registration, learning, AI, delivery and production scheduling are not
implemented. A configured collection may eventually be processed by a GitHub Actions
caller one coordinate at a time or in bounded batches. Shared model acquisition and
retention precede the per-location work. For each configured/registered location:

1. Validate and identify the location from latitude/longitude; derive geographic
   metadata, the larger context domain and the smaller editable domain internally.
2. Verify eligible previous forecasts against suitable available observations,
   using the version originally issued and the applicable time, quality, spatial
   support, and cutoff rules. Record unavailable verification explicitly and
   proceed with the new forecast when no suitable observation is available.
3. Regrid and blend ready shared guidance into coherent local MesoForge fields using
   deterministic scientific operators. Save the original numerical baseline and its
   source/transform/configuration identity. No acquisition or regional grid preparation
   occurs inside a forecast HTTP request.
4. Apply approved deterministic site/regime bias correction learned from eligible
   verified history, keeping the corrected fields separate from the original baseline.
   With insufficient history or no approved correction, report that status explicitly.
5. Let the later AI forecast desk inspect surrounding meteorology, disagreement,
   eligible observations, verification history and structured site knowledge, then
   propose a bounded spatial/temporal edit recipe against the corrected fields.
6. Execute the saved recipe through versioned deterministic tools and validate physical
   and cross-field consistency, bounds, continuity, cutoffs and edit-domain limits.
   Retain the proposal, validation decision and final adjusted fields separately.
7. Interpolate the final spot forecast at the exact configured coordinate. Persist the
   complete immutable issuance and its stage lineage, then deliver the saved version
   when delivery is implemented.
8. When suitable observations become available, compare the issued baseline, corrected
   and final forecasts on identical verified samples. Use measured results to assess
   statistical corrections, site knowledge and which AI edit types help in each regime
   (section 11), without rewriting the forecast originally issued.

A location failure is recorded for that location and does not prevent the remaining
coordinates from being processed. Missing required model guidance keeps its explicit
forecast missingness; unavailable verification does not block a new forecast. One-off
API requests remain outside registration/tracking and this recurring lifecycle unless
the operator explicitly configures the location. AI and delivery are later roadmap
stages, not prerequisites for numerical issuance or first-release acceptance.

### 6.7 Forecast canvas and deterministic conditions

**Inventory as of `49656edfadd53601f40356a4c4dee4b4d6c59347`, 2026-09-13.**
This section records inspected code, then proposes a condition-layer design. The
owner has approved the guardrails and this design exercise, not new source weights,
weather thresholds, a condition engine or field promotion. No runtime/schema change
is made by this documentation milestone. It supersedes older field-status and
next-field descriptions elsewhere in this RFC, not the remaining proposed architecture.

#### 6.7.1 What the saved canvas actually contains

[`build_local_surface_grid` and `extract_grid_point`](../../src/mesoforge/application/local_surface_grid.py)
store `mesoforge.local-surface-baseline.v2`: `geometry`, transformation/code identity,
shared `forecast_context`, and `cells[].hours[]`. The measured default is 7×7 nodes
at 6 km spacing, context bounds ±18 km, editable 3×3 nodes within ±6 km, and an exact
center node. Domain membership and signed distance to the editable boundary are
explicit; no taper or adjustment is applied. These are implementation defaults,
not permanent geometry policy. A different exact point gets its own coordinate-derived
grid from shared guidance; arbitrary off-node interpolation is not implemented.

Each cell/hour preserves the same numerical fields and evidence structure as the
point column. Below, `F` abbreviates `hours[].surface.fields`, and `S` abbreviates
`hours[].surface`, relative to the forecast object. In a retained full grid use
`cells[].hours[]`; in an issued JSON artifact the forecast is under `/forecast`.
`hours[].temperature` and `F.air_temperature_2m` are the same baseline temperature,
not two independent predictions. Optional attachments can be absent from older
issuances or preparations. The inventory describes supported representations,
not guaranteed availability in every 36-hour run.

[`ForecastIssuanceService.issue/read`](../../src/mesoforge/application/issuance.py)
stores the complete immutable `issued-forecast.v1` payload, including the original
grid, in MinIO, with PostgreSQL issuance/location/time/content-digest metadata.
The reader verifies saved bytes; it does not regenerate forecasts. The point retains
`local_grid.sha256`, geometry and `exact_center_node` extraction metadata.
[`build_hourly_report`](../../src/mesoforge/application/hourly_report.py) copies the
baseline into final display fields, labels bias `not_implemented`, AI `not_run`, and
delivery `not_delivered`. In this inventory **delivered baseline** means the active
numerical output eligible for presentation, not evidence that external delivery ran.

#### 6.7.2 Complete field and evidence inventory

Time notation: **I** = instantaneous sample at `valid_time`; **A** = accumulation
over explicit `(start,end]`; **P** = probability of a defined event over explicit
`(start,end]`; **C/I** = categorical/state evidence at an instant. An hour number is
an index, not a license to replace native intervals or model source leads. Original
units and native supports remain attached even where canonical/display units differ.

| Field / payload key | Physical meaning | Canonical units; temporal semantics | Actual contributor products / supporting data |
|---|---|---|---|
| Temperature — `F.air_temperature_2m`, `hours[].temperature` | 2-m air temperature | K; I; °F display | HRRR `wrfsfc`, GFS `pgrb2.0p25`, RAP `awp130pgrb` TMP; IFS open-data `2t` |
| Dew point — `F.dew_point_temperature_2m` | 2-m dew-point temperature | K; I | HRRR/GFS/RAP DPT; IFS `2d` |
| RH — `F.relative_humidity_2m` | Relative humidity with respect to **liquid water**, also below freezing | Baseline `%`, contributor `percent`; I diagnostic | Derived from the corresponding baseline or each contributor's T/Td, not a separately blended native RH field |
| U — `F.eastward_wind_10m` | Earth-relative eastward wind component | m/s; I | HRRR/GFS/RAP UGRD, IFS `10u`; native grid-relative vectors rotated before use |
| V — `F.northward_wind_10m` | Earth-relative northward wind component | m/s; I | HRRR/GFS/RAP VGRD, IFS `10v` |
| Wind speed — `F.wind_speed_10m` | Magnitude of the resulting 10-m vector | m/s; I; mph display | Derived from blended U/V; each contributor also retains its own diagnostic |
| Wind direction — `F.wind_from_direction_10m` | Meteorological direction **from** which the wind blows | degree; I diagnostic | Derived from U/V; never an average of compass directions; exactly calm has no direction |
| Gust — `F.wind_gust_10m` | Near-surface model gust under the current instantaneous contract; native level retained | m/s; I; mph display | HRRR/GFS/RAP GUST. IFS interval-maximum gust is incompatible with this contract |
| QPF — `F.liquid_equivalent_precipitation_amount_1h` | Total liquid-equivalent precipitation amount, irrespective of type | kg/m²; A, 1 h; 1 kg/m² = 1 mm water; inches display | HRRR APCP hourly amount; GFS APCP from compatible native bucket parents. RAP/IFS QPF is disabled in this path |
| PoP — `F.probability_of_precipitation_1h` | P(liquid accumulation **>0.254 kg/m²**), not P(any nonzero precipitation) | Fraction `1`; P, 1 h; native/display percent retained | NBM core native one-hour APCP probability, separately from deterministic QPF |
| Other PoP events — `S.probability_guidance.contributors` | Each row's exact exceedance event | Fraction `1`; P, native 1/6/24 h | NBM/GEFS/REFS/ECMWF products in the event table below |
| P-type — `F.precipitation_type`, `S.precipitation_type_guidance.contributors` | Native supported hydrometeor types; type is not occurrence | `category`; C/I; null accumulation bounds | HRRR/GFS/RAP CRAIN/CSNOW/CFRZR/CICEP flags; IFS `ptype` codes; NBM conditional type percentages |
| SWE — `F.snowfall_water_equivalent_amount`, `S.snowfall_guidance.contributors` | Liquid-equivalent water associated with **new snowfall**, not snowpack SWE | kg/m²; A, HRRR/RAP hourly, IFS native 3 h | HRRR/RAP accumulated WEASD bindings, IFS `sf`; exact parents/intervals retained |
| Native snowfall amount — `F.snowfall_amount`, `S.snowfall_amount_guidance.native_contributors` | Newly accumulated snow depth over an interval, not total depth on the ground | m; A, hourly normalized/native intervals; inches display | HRRR/RAP ASNOW snow; NBM ASNOW **snow and sleet**. These hydrometeor definitions are not identical |
| Kuchera snowfall — `S.snowfall_amount_guidance.derived_contributors` | Derived new-snow amount from compatible SWE and profile-derived SLR | m; A, 1 h in current RAP path | RAP SWE and native-corner thermodynamics; amount calculated before spatial interpolation |
| Kuchera SLR — retained within each derived contributor's profile/spatial evidence | Ratio used to turn that SWE into derived new-snow amount | Dimensionless `1`; interval-end I diagnostic applied to the recorded SWE interval | `kuchera_surface_to_500hpa_25hpa_profile.v1`; not native model SLR or a delivered ratio |
| Native SLR — `S.snowfall_amount_guidance.native_slr` | Provider model snow-to-liquid ratio | Dimensionless `1`; **I**, not interval-average SLR | NBM SNOWLR, retained separately from ASNOW and Kuchera |
| Cloud — `F.cloud_area_fraction`; `S.cloud_guidance.contributors` | Entire-column total cloud / provider total sky cover; not individual layers | Active placeholder fraction `1`; evidence `percent`; I | HRRR/GFS/RAP TCDC entire atmosphere; NBM deterministic total sky TCDC; IFS `tcc` fraction converted to percent |
| Sky category — per native cloud contributor `sky_category` | Deterministic display category of that contributor's total cloud percentage | Category; C/I diagnostic | Existing `native-cloud-percentage-display.v1`, not a separate active sky forecast |
| Visibility — `F.visibility`, `S.visibility_guidance.contributors` | Native horizontal surface visibility, not ceiling, slant range or cause | m; I; miles display | HRRR/GFS/RAP/NBM VIS; no compatible visibility in the inspected IFS open feed |
| Thunder — `F.probability_of_thunder_1h`, `S.thunder_guidance.contributors` | Provider-defined native thunder potential; not exact-point lightning certainty | Fraction `1`; P, separate 1/3/6 h; native/display percent | NBM TSTM. Physical flash threshold/event geometry are not encoded; retain that uncertainty |
| Freezing-rain liquid — `F.freezing_rain_liquid_equivalent_amount`, corresponding `S.ice_guidance.contributors` | Liquid-equivalent freezing-rain precipitation | kg/m²; A, hourly differences of cumulative parents | HRRR/RAP FRZR; no liquid-to-ice conversion |
| Flat ice — `F.flat_ice_accretion_mass_equivalent`, corresponding `S.ice_guidance.contributors` | Native elevated flat-surface accreted ice in the provider's mass-equivalent encoding | kg/m²; A, separate native 1/6 h | NBM FICEAC / provider FRAM. Not geometric thickness, radial ice, road icing or freezing-rain liquid |

Probability events stay separate even when attached to the same hourly cell:

| Source ID | Native event and spatial meaning | Current relationship |
|---|---|---|
| Active hourly NBM | >0.254 kg/m² over 1 h; native grid probability sampled bilinearly | Sole delivered PoP source; no QPF-derived occurrence probability |
| `NBM_6H`, `GEFS_6H` | >0.254 kg/m² over 6 h; registered grid-point support; GEFS native bias-corrected ensemble PQPF | Zero-weight shadows; comparable only for identical actual bounds/threshold/support |
| `REFS_1H` | >12.7 kg/m² over 1 h; heavy-rain neighborhood, radius unencoded | Zero-weight, distinct event; cannot substitute for hourly NBM PoP |
| `ECMWF_ENS_24H` | ≥1 kg/m² over 24 h; grid-box mean, effective event footprint unencoded | Zero-weight, incompatible event; no 24→6/1 h conversion or six-hour replacement |
| Thunder `NBM_1H` | Native one-hour TSTM; physical threshold and event footprint unencoded | Temporary active passthrough `nbm-native-hourly-thunder-baseline.v1`; retain provider-qualified meaning |
| Thunder `NBM_3H`, `NBM_6H` | Corresponding separate native TSTM periods | Zero-weight evidence; never rescaled to hourly probabilities |

No ensemble-member fraction calculation or calibrated multi-source PoP/thunder blend
is implemented here. Native member/population metadata is retained where supplied;
missing member identities or neighborhood definitions must not be invented. REFS/RRFS,
HREF and ECMWF thunder/lightning candidates are not active substitutes: inspected
unbound/unsupported products and deterministic lightning diagnostics stay explicit.

Additional data present in code, but **not additional delivered weather fields**:

- RAP thermodynamic support for Kuchera: 2-m T, surface pressure (Pa), and temperatures
  (K) on 500–1000 hPa levels every 25 hPa. Complete required above-ground inputs are
  checked; below-ground levels are excluded. Profiles, maximum temperature, native
  corner SLR/SWE/amounts and provenance remain attached. The interval-end profile is
  an explicit one-hour approximation, not proof of the interval's complete evolution.
- P-type evidence also preserves IFS `wet_snow` and `freezing_drizzle`, NBM conditional
  type probabilities, multi-type sets and zero/no-classification flags. These are
  evidence distinctions; they do not add delivered freezing-drizzle or wet-snow rules.
- GFS six-hour QPF buckets and native cumulative snow/ice parents are normalization
  inputs, not a delivered `qpf_6h` field. Interval aggregates are separate derivations.
- SNOD, GFS snowpack WEASD and IFS snowpack `sd`/`rsn` are explicitly excluded from
  the new-snow amount path. Total snow depth, snowpack water and density have different
  meanings; no ground-snow-depth field is emitted on this canvas. A constant 10:1
  benchmark and locally derived accretion are not implemented active fields.
- `temperature_equal_v1` / `blend_50_50` is a comparison recipe evaluated from saved
  contributors, not a second delivered grid field. Observation values, errors,
  MAE/bias/RMSE, verification status and sample counts belong to verification/evaluation,
  not the forecast-condition input canvas. No future observation may rewrite as-issued
  conditions. Report bias/AI deltas of zero mean stages did not run, not computed corrections.

#### 6.7.3 Delivered versus evidence-only policy matrix

The classes overlap: an active diagnostic can also be derived; an evidence field can
have a null active placeholder. **B** = delivered multi-source baseline, **S** = active
single-source, **E** = zero-weight evidence/shadow, **P** = no approved active policy,
**D** = derived, **U** = unsupported/unavailable input. Status refers to the actual
field, not merely the model's global registration. NBM rows in the retained Phase 2
configuration do not activate NBM in the current HRRR/GFS surface or QPF composition.

Verification key: **issued** = current exact-version temperature observation matching,
persisted error and contributor/recipe comparison; **P2 only** = separate retained
Phase 2 station-path scientific verification support, not wired to these issued grid
fields; **none** = no current verification for this field/condition. No new verification
capability is approved or implemented by this design.

| Field(s) | Class and current blend/source policy | Missingness / exclusions | Suitable for conditions today; remaining gap | Verification |
|---|---|---|---|---|
| Temperature | B; `temperature_control_v1`, HRRR/GFS 70/30 for all 36 h; RAP/IFS E | `require_all`: missing active contributor → null, no redistributed weights | Numeric context; never determines p-type/fog/ice alone | issued |
| Dew point | B; applicable Phase 2 scalar/vector HRRR/GFS rows, 70/30 h1–18, 60/40 h19–36 | Approved eligible sole-source row only; source and final dew-point consistency required; invalid → null | Moisture context; no fog inference by itself | P2 only |
| RH | B+D; Bolton liquid-water diagnostic from baseline T/Td; per-model diagnostics E | Missing/invalid T/Td or RH outside 0–100 → null; no supersaturation clipping | Numeric moisture context; not a fog detector | none |
| U/V, speed/direction | B; U/V blended as vectors with coupled eligible gust set and same Phase 2 rows; speed/direction D | Eligible sole-source rows explicit; invalid coupled source excluded; calm speed 0, direction null with reason | Numeric wind is usable; breezy/windy thresholds and duration meaning unapproved; calm-direction can be not applicable | P2 only |
| Gust | B; same U/V contributor set/row; RAP E; IFS U | Existing approved ≤0.1 m/s source shortfall floor and ≤1e−6 m/s final floor recorded; incompatible IFS interval maximum stays missing | Numeric gust usable; sustained descriptors must not use gust interchangeably | P2 only |
| Hourly QPF | B; Phase 2 precipitation HRRR/GFS rows 70/30 h1–18, 60/40 h19–36; eligible sole-source fallback | Exact hourly intervals and finite corners required; native GFS bucket/reset/tolerance contract retained; zero ≠ missing; RAP/IFS U | State amount and period; not PoP, instant occurrence or observed intensity. Amount/intensity-class thresholds need policy | P2 only |
| Hourly PoP | S; NBM-only weight-1 passthrough | Exact >0.254 kg/m² event and 1-h bounds; missing/invalid → null; no longer-period fallback | Numeric event probability is usable with its period/threshold; qualitative bands and type-specific event combination unapproved | P2 only |
| Other precipitation probabilities | E; no delivered weights or calibration | Exact threshold/comparator/window/spatial support required for comparison; native gaps explicit | Supporting diagnostics only; neither substitute nor extra vote for delivered PoP | none |
| P-type | Temporary B categorical agreement, no scalar weights; RAP/IFS/NBM E | Complete HRRR/GFS flag sets must agree; equal multi-type set → mixed; disagreement → ambiguous; zero flags → unknown, not dry; no one-source fallback | Report the exact endpoint state, including mixed/unknown. Joint occurrence/type and interval interpretation still need policy | none |
| SWE | E+P; no approved blend; active value null | HRRR/RAP hourly and IFS native 3-h bounds stay separate; GFS snowpack WEASD and absent NBM SWE unsupported; no QPF/type conversion | Evidence-only; cannot make delivered snow occurrence or amount | none |
| Native snowfall amount | E+P; no approved blend; active value null | Native parents/windows required; NBM snow-and-sleet not silently equated to HRRR/RAP snow; GFS/IFS new-depth U | Evidence-only; no inference of occurrence/type from positive snowfall alone | none |
| Kuchera snowfall / SLR | E+D+P for delivered amount; versioned RAP method | Missing required profile/SWE → missing; no constant-ratio fallback; preserve interval-end approximation and native-corner calculation | Evidence-only; no automatic promotion over native guidance | none |
| Native NBM SLR | E; separate instantaneous SNOWLR | Missing/invalid remains unavailable; no unapproved cross-model or interval-average use | Supporting evidence only, not a condition | none |
| Cloud / per-source sky category | E+P; active `cloud_area_fraction` remains null; categories D on each native source | Incompatible layers/averages rejected; IFS native 3-h gaps explicit; zero cloud valid | Delivered sky clause blocked by missing active source/blend, not by lack of a category function | none |
| Visibility | E+P; active visibility null; HRRR/GFS/RAP/NBM E, IFS U | Native finite nonnegative horizontal visibility required; no invented cap or filling | Reduced-visibility clause blocked by active-policy gap; fog additionally requires suitable causal evidence and a validated rule | none |
| Hourly thunder | Temporary S; NBM hourly native passthrough | Missing/invalid → null; no 3/6-h replacement; unencoded threshold/footprint remain unknown | Qualified native probability display only; unqualified point thunder/“likely” wording needs event-support and phrase policy | none |
| Longer-period thunder / other lightning products | E or U; no combined source policy | Unlike events/periods/support are incompatible; deterministic diagnostics not probabilities | Evidence-only; cannot determine a delivered thunder clause | none |
| Freezing-rain liquid | E+P; HRRR/RAP FRZR; no active blend | Same-cycle cumulative parent differencing; negative/nonfinite increments missing, not zero; unsupported sources explicit | Evidence-only amount; does not establish occurrence or accreted ice | none |
| Native flat ice | E+P; NBM FICEAC native 1/6 h; no active blend | Retain kg/m² native meaning; no density/thickness/period conversion; GFS/IFS U | Evidence-only hazard amount; not a road-icing diagnosis or a substitute for p-type | none |

Across every row, original cycles, **model source leads versus target horizons**, valid
times, interval bounds, field/recipe identities, raw/prepared hashes, spatial extraction,
units, exclusions and applied weights remain inspectable. Optional-field absence in
an old record is `unavailable`, not zero. Surface table code can report `fallback`
even for a selected two-source row: inspect the applied weights/row and exclusions,
not the status string alone. Report rounding is never input to a condition decision.

Code anchors for these inventory facts:

- [`extract_surface_hour`](../../src/mesoforge/application/surface_forecast.py),
  [`blend_surface` / `relative_humidity_percent`](../../src/mesoforge/forecasting/surface.py),
  [named recipes](../../src/mesoforge/forecasting/recipes.py), and
  [applicable retained rows](../../configs/phase2-grasston.yaml).
- [`extract_precipitation_hour`](../../src/mesoforge/application/precipitation_forecast.py),
  [`extract_probability_hour`](../../src/mesoforge/application/probability_forecast.py),
  [probability compatibility](../../src/mesoforge/application/probability_contributors.py),
  [native probability products](../../src/mesoforge/guidance/sources/probabilistic.py),
  and [`resolve_type_evidence`](../../src/mesoforge/application/precipitation_type.py).
- [SWE extraction/aggregation](../../src/mesoforge/application/snowfall_forecast.py),
  [snowfall/SLR extraction](../../src/mesoforge/application/snowfall_amount_forecast.py),
  [Kuchera calculation](../../src/mesoforge/forecasting/snowfall_amount.py), and
  [native snow definitions](../../src/mesoforge/guidance/sources/snowfall_amount.py).
- [Cloud extraction](../../src/mesoforge/application/cloud_cover.py) and
  [`sky_category`](../../src/mesoforge/forecasting/cloud_cover.py),
  [visibility](../../src/mesoforge/application/visibility.py),
  [thunder policy/events](../../src/mesoforge/forecasting/thunder.py),
  [ice extraction](../../src/mesoforge/application/ice.py) and
  [native ice definitions](../../src/mesoforge/guidance/sources/ice.py).
- [Issued temperature verification](../../src/mesoforge/application/issued_temperature_verification.py),
  [saved-model comparison](../../src/mesoforge/verification/model_comparison.py),
  and separate retained [Phase 2 metrics](../../src/mesoforge/verification/metrics.py)
  / [QPF-PoP metrics](../../src/mesoforge/verification/qpf_pop_metrics.py).

#### 6.7.4 Proposed structured condition object

Use one pure, versioned derivation over the chosen **saved field stage** for each
cell/hour. Produce structured components first; a separate deterministic renderer
turns eligible components into text. It must not acquire guidance/observations, choose
new blends, re-run extraction/blending, consult an LLM or alter the input fields.
The same function serves the whole grid; the point copies its center result rather
than running a different condition algorithm. Do not interpolate categorical labels.

Proposed component contract (a design sketch, not a new executable schema):

| Property | Meaning |
|---|---|
| `state` | `known`, `unknown`, `ambiguous`, `unavailable`, or `not_applicable` |
| `value`, `unit` | Typed value/set/probability, or null; probability always fraction with separate event definition |
| `valid_time`, `temporal_semantics`, `interval` | Component's own instant or exact event/accumulation bounds and closure; not inherited blindly from its hourly container |
| `event`, `spatial_support` | Threshold, comparator, hydrometeor scope, point/grid/neighborhood meaning and any unknown definition metadata |
| `source_policy`, `rule_id` | Applied saved-field policy and condition rule identity; never silently substitute current configuration for the issuance snapshot |
| `evidence_refs`, `reasons`, `excluded_evidence` | Exact immutable field/native/provenance pointers, structured reason codes, and why a source cannot support a delivered clause |

Component states have different meanings. `unknown` means valid evidence does not
resolve the concept (for example zero p-type flags); `ambiguous` means conflicting
eligible evidence or unresolved alternatives, not proven simultaneous mixed
precipitation. `unavailable` includes missing/unsupported data, invalid semantics,
unimplemented derivation or an unapproved policy. `not_applicable` requires a rule
that positively establishes irrelevance, such as direction for exactly calm wind;
it is never a default for an absent field. Agreed multiple native types are a known
`mixed` set, not collapsed into disagreement.

The object includes these independent components:

- `sky`: numerical total cloud and eligible sky category.
- `precipitation`: **separate** occurrence assessment, event probability, type set,
  liquid amount and optional amount/intensity class. No component stands in for another.
- `thunder`: exact native event probability, plus a separately gated categorical/phrase
  assessment; no assumed point-lightning meaning.
- `visibility`: distance/restriction descriptor and **separate** fog/cause assessment.
- `wind`: U/V, speed, direction, gust and separately gated descriptors.
- `transitions`: source/target states, supporting sample times and transition window;
  no exact transition minute fabricated between samples.
- `assessment`: overall availability, unresolved tensions, missing prerequisites and
  uncalibrated confidence. A probability of precipitation is not confidence in the whole
  condition. No sample-count-based, agreement-based or LLM confidence percentage is invented.

Illustrative preview shape, using hypothetical values, not a real issuance. All JSON
pointers below are relative to the saved issued payload. Omitted components in this
short example follow the same contract; a full implementation would emit their states.

```json
{
  "schema_version": "mesoforge.weather-condition-preview.v1",
  "ruleset_id": "condition-preview.draft.1",
  "input": {
    "issued_forecast_id": "00000000-0000-0000-0000-000000000000",
    "issued_payload_digest": "sha256:<saved-issued-payload-digest>",
    "grid_digest": "sha256:<saved-local-grid-digest>",
    "field_stage": "numerical_baseline",
    "cell": {"x_index": 3, "y_index": 3},
    "horizon_hours": 1
  },
  "valid_time": "2026-09-12T19:00:00Z",
  "components": {
    "sky": {"state": "unavailable", "value": null, "reasons": ["active_cloud_policy_missing"]},
    "precipitation": {
      "occurrence": {"state": "unavailable", "value": null, "reasons": ["no_categorical_occurrence_rule"]},
      "probability": {
        "state": "known", "value": 0.4, "unit": "1",
        "temporal_semantics": "interval_probability",
        "interval": {"start": "2026-09-12T18:00:00Z", "end": "2026-09-12T19:00:00Z", "closure": "left_open_right_closed"},
        "event": {"quantity": "liquid_equivalent_precipitation_amount", "comparison": "gt", "threshold": 0.254, "threshold_unit": "kg/m^2"},
        "evidence_refs": ["/forecast/hours/0/surface/fields/probability_of_precipitation_1h"]
      },
      "type": {
        "state": "known", "value": ["rain"], "unit": "category",
        "temporal_semantics": "instantaneous", "valid_time": "2026-09-12T19:00:00Z", "interval": null,
        "source_policy": "temporary-hrrr-gfs-native-type-agreement.v1",
        "evidence_refs": ["/forecast/hours/0/surface/fields/precipitation_type"],
        "reasons": ["endpoint_state_only_not_joint_rain_probability"]
      },
      "intensity_class": {"state": "unavailable", "value": null, "reasons": ["intensity_policy_not_approved"]}
    }
  },
  "assessment": {"availability": "partial", "confidence": {"value": null, "status": "not_calibrated"}},
  "rendering": {
    "template_version": "condition-preview-text.draft.1", "locale": "en", "timezone": "UTC",
    "text": "Precipitation chance 40% for 18–19 UTC (>0.01 inch liquid). Model precipitation type at 19 UTC: rain."
  }
}
```

This example does **not** assert a 40% probability of rain at the exact point, rain
throughout the hour, or dry weather for the remaining 60%. Actual output must include
the saved event/spatial metadata and policy references omitted from the abbreviated
example, with hashes/code identity for the derivation and renderer. IDs, digests and
draft versions above are illustrative, not registered artifacts or approved policies.

#### 6.7.5 Proposed deterministic rule hierarchy

1. **Identify and validate the input.** Read one exact saved forecast/grid version,
   chosen field-stage digest, coordinate/cell and target horizon. Validate units,
   statuses, finite ranges, event definitions and temporal/spatial support. No current
   model substitution, retrospective observation input or implicit field correction.
2. **Admit only the permitted field role.** Active baseline/single-source fields can
   support delivered components under their existing policy. Evidence-only inputs can
   explain disagreement/exclusion, never become a fallback or vote. A future recipe
   needs explicit activation; conditions do not make that decision. Preserve actual
   fallback rows and quality flags. Invalid data cannot produce a default “clear/dry.”
3. **Align semantics, not just timestamps.** An interval ends at its stored endpoint;
   instantaneous T/wind/type retain that instant. Identical endpoints alone do not
   make hourly QPF, six-hour PoP and instantaneous p-type a joint event. Do not split
   accumulated fields/probabilities, fill IFS gaps, multiply PoP by conditional type
   percentages, or assume independence. Exact compatible accumulation sums may use
   existing helpers, but an average liquid rate is not instantaneous intensity.
4. **Translate component state without losing meaning.** Preserve p-type mixed versus
   ambiguous/unknown, invalid/missing probabilities versus true zero, and calm direction
   versus absent wind. PoP zero only describes its thresholded event; QPF zero and zero
   type flags do not prove no trace/drizzle/snow. No currently approved dry classifier
   makes precipitation type `not_applicable`; retain its raw state for now.
5. **Derive components using versioned, explicit rules.** Reuse existing numerical
   diagnostics and sky categories where their active inputs qualify. Every later
   probability band, intensity class, wind threshold, persistence window and boundary
   comparator must be explicit in the condition ruleset. Never round before classification.
   Unsupported rules remain unavailable, rather than silently choosing familiar thresholds.
6. **Gate combinations and flag tensions.** QPF and PoP disagreements are descriptive
   tension, not instructions to repair either field. Reuse applicable logic from
   [`check_probability_deterministic_tension`](../../src/mesoforge/forecasting/consistency.py)
   only after checking threshold, comparator and window. That scalar helper flags
   PoP zero with QPF **≥0.254**, whereas current NBM's event is **>0.254**; it does
   not itself check metadata. Do not treat its boundary as the same event. Reuse
   requires an explicitly named applicable tension rule; otherwise report the
   semantic mismatch without applying it.
   Missing sky does not block a precipitation component, but missing type blocks a
   specific rain/snow claim. Missing snow/ice amount does not inherently block an
   otherwise justified type/occurrence claim. Never infer fog from visibility alone.
7. **Derive transitions only from eligible adjacent results.** Use one issuance/stage,
   consecutive samples and explicit windows; unknown/gaps break the sequence. Initially
   report endpoint type changes as endpoint changes. “Rain changing to snow” later
   requires approved occurrence/type temporal linkage. Do not invent a transition time
   or carry one source's type through its missing hours.
8. **Render deterministically.** Render eligible structured components in a stable
   order (sky, precipitation, thunder, visibility/cause, wind, transitions/qualifiers),
   with a versioned locale/template and explicit timezone. Preserve combinations rather
   than selecting a single weather-code winner. Do not let a thunder phrase erase
   freezing-rain or visibility information. Missing nonessential components can be
   omitted from text but remain explicit in the object; if nothing supports a statement,
   render “Weather conditions unavailable,” not a forced condition or confidence claim.
9. **Retain reproducibility and keep AI outside the renderer.** Numerical, later
   bias-corrected and accepted edited fields have separate identities. The same pure
   rules derive conditions from whichever explicitly selected stage is saved. An AI
   proposal does not supply final condition text or a probability/confidence shortcut.
   Re-rendering an issued version uses its saved ruleset/inputs; newer rules produce
   a separately identified reinterpretation, not a silent rewrite of issued wording.

Specific phrase families and prerequisites:

| Concept | Eligible derivation / constraint |
|---|---|
| Clear → cloudy | Approved active total-cloud input plus existing unrounded upper-inclusive 5/25/50/87/100% display categories. Current per-source categories are not delivered sky. No total from summed cloud layers |
| Rain/snow; chance/likely variants | Approved occurrence assessment plus resolved compatible type; qualitative probability bands need approval. For now expose numeric thresholded PoP and endpoint type separately, not a calibrated type-specific probability |
| Mixed precipitation | Known supported multi-type set plus appropriate occurrence semantics; disagreement alone remains ambiguous, with alternatives shown rather than false certainty |
| Freezing rain possible | Eligible occurrence/type linkage and actual freezing-rain type evidence; temperature below freezing, positive FRZR/FICEAC, or surface QPF alone cannot establish it |
| Thunderstorms / likely | Native probabilistic event, acceptable support and a defined phrase policy; current NBM probability can be quoted with its provider-defined qualification, not asserted as point lightning or unqualified categorical thunder |
| Reduced visibility / fog | Active compatible visibility plus approved restriction bins for the former; additional suitable fog evidence/validated causal diagnostic for the latter. Low visibility, high RH, small T−Td, snow or rain independently are insufficient |
| Breezy / windy | Approved sustained-wind threshold and duration policy; gust remains a separate modifier. No silently adopted Beaufort/NWS threshold or instantaneous-to-hourly averaging assumption |
| Snow with wind | Both separately eligible components at compatible support. “Snow; windy” need not imply **blowing snow**, which needs additional suitable blowing-snow evidence/validated rule |
| Mostly cloudy with chance of rain | Both sky and typed occurrence gates must pass. A missing active cloud policy cannot be filled from an arbitrary shadow just to complete the sentence |

#### 6.7.6 Which gaps block which behavior

| Gap / decision | What it blocks | Conservative treatment without changing current forecast policies |
|---|---|---|
| Cloud has no approved active source/blend | Delivered sky words and sky-plus-precipitation combinations | Sky unavailable; preserve native sky evidence. Does not block an honest precipitation-only preview |
| Visibility has no approved active policy; fog has no validated cause rule | Reduced-visibility descriptors and fog | Both unavailable. Retain evidence without inferring fog, rain intensity or blowing snow |
| SWE/snowfall/SLR/ice have no approved active amount policy | Delivered winter amounts, amount classes and accretion/road-ice claims | Keep E/P fields excluded. Does not block future snow/freezing-rain type wording if independent occurrence/type rules qualify |
| Temporary p-type agreement; no dry/not-applicable or interval-occurrence linkage | Confident typed interval phrases, transitions and dry/type suppression | Keep endpoint type/state and disagreement; zero flags do not mean dry. Agreement policy remains unchanged |
| NBM sole-source hourly PoP; no calibrated multi-source policy | Claims of optimized/multi-source/type-specific probability | Existing native event may be displayed numerically; more probability sources/calibration are not prerequisites for that limited use |
| NBM thunder event threshold/footprint not fully established; no phrase bands | Exact-point thunder claims and unqualified “thunderstorms likely” | Provider-qualified native probability only, or unavailable clause. No new product or deterministic proxy substitution |
| Occurrence wording, probability bands, amount/intensity thresholds, wind descriptors and transitions not approved | The full condition renderer's qualitative claims | First slice uses numeric event/amount/wind and distinct endpoint type. Proposed values/bands must be reviewed before enabling these descriptors |
| Condition verification/calibrated confidence absent | Skill claims, calibrated whole-condition confidence and automatic rule promotion | `confidence: not_calibrated`; independent behavioral tests are not forecast-skill evidence |

The **minimum decision before the first implementation** is approval of the bounded
preview contract below: exact numeric event/amount and endpoint type remain separate;
unknown/ambiguous/unavailable are explicit; no qualitative probability, dry, intensity,
wind or fog thresholds are invented. Choose its actual ruleset/template version when
implemented. No cloud, visibility, snow, ice or production-weight decision is needed
for that preview. Conversely, a first release that must already say “mostly cloudy
with rain likely” needs an active cloud policy and explicit occurrence/type/phrase
rules before implementation. This RFC does not approve them by describing them.

#### 6.7.7 Smallest proposed implementation slice and checks

Add a **read-only condition preview for one saved issued forecast ID**. Reuse the
existing verified storage reader and one pure cell/hour function over its saved local
grid, then extract the center's 36 structured results and render them. First scope:
native active hourly PoP with exact threshold/window, active hourly liquid amount,
and separately qualified instantaneous p-type/state; numeric wind may be displayed
without qualitative descriptors. Other components are explicitly unavailable/excluded
under the matrix above. Do not acquire data, fill missing attachments, add a new field,
promote a shadow, change p-type/blend rules or create new history rows/objects.

Likely code seams, **proposed, not files added now**: a small pure
`forecasting/conditions.py` for components/rules, an application read/preview command
using `ForecastIssuanceService.read`, and an additive renderer in `hourly_report.py`.
Use the existing canonical serialization/types when implementing the contract; no
parallel forecast-history system or new observation/framework layer. An illustrative
CLI shape is `python -m mesoforge.application.weather_conditions --issued-forecast-id ID`;
that command does not exist yet. Saved legacy point-only or missing-grid records
return a clear unsupported-preview reason rather than regenerating a grid.

Acceptance should use existing real retained Minneapolis/Glacier issued/grid fixtures
where available, with zero provider calls. Independently test zero versus missing,
unknown versus mixed/ambiguous, calm direction, identical versus mismatched intervals,
no promotion of evidence-only inputs, p-type endpoint semantics, absent optional fields,
QPF/PoP tension without repair, exact grid-center extraction, deterministic bytes/text,
and unchanged issued payload digest/database/object counts. Reuse existing storage
fixtures; do not create a duplicate suite or claim condition skill from those tests.
If a retained grid was never issued, use it for pure preview tests and a controlled
existing issuance fixture for readback; do not invent a historical issuance ID.

Later, separately approved integration can build an additive condition layer before
new point extraction/issuance, linked to its baseline/stage grid digest. Store that
layer's schema, rule/config/code and renderer identities through existing artifact/
issuance infrastructure. Keep the original baseline grid and historical issued JSON
unchanged; any persisted reinterpretation of an old issuance must be a separately
linked immutable derivation. First preview deliberately needs no new persistence.

## 7. Representative benchmark and admission

Before finalizing API support and cache packaging, benchmark the intended host using the
supported-region representative model mix, required fields, arbitrary interior and
partition-edge coordinates, up to 36 horizons, cold/warm cache, and production code.

Record:

| Measurement | Decision informed |
|---|---|
| Tile/object reads and fan-out | Spatial/field/horizon packaging and backend-work limit |
| Compressed bytes fetched and bytes decoded | I/O/decode amplification |
| Peak RSS and minimum `MemAvailable` | Shared-host reserve under representative co-load |
| Output bytes and result count | Payload/pagination limits |
| Cold/warm end-to-end latency | Timeout and initial private-service objective |

Candidate tile sizes, bundles, memory allocations, and latency targets are hypotheses. The
review gate chooses packaging, supported combinations, concurrency, timeout, and rejection
thresholds together. If all required fields across 36 horizons cannot fit demonstrated
safety bounds, publish a narrower combination matrix or approve redesign.

After measurement, configured input, concurrency, selected-row/group, output, and timeout
limits are genuine runtime safety controls. Cancellation must release resources. Expansion
requires new representative measurement and review.

## 8. Storage and provenance

PostgreSQL is authoritative for searchable normalized state, jobs, immutable headers, facts,
and pointers. S3-compatible storage owns large immutable source/cache bytes and audit
exports. Cross-store references use logical identities and verified digests.

Likely first-release relations are planning shape, not a ceiling:

| Relation | Purpose |
|---|---|
| `jobs` | Queue leases/attempts/status |
| `lineage_headers` | Shared immutable code/config/environment/cutoff/parent lineage |
| `guidance_assets` | Cache manifest/index/readiness |
| `registered_locations` | Coordinate plus operator metadata/current state |
| `forecast_snapshots` | Immutable issue and baseline headers |
| `forecast_facts` | Values or missingness by snapshot/variable/layer/valid interval |
| `observation_facts` | Provider/station revisions and cutoffs |
| `verification_facts` | Match opportunities, selected revision, status, support, policy |

There is **no separate `error_facts` table in the first release**. Error is a cheap
deterministic function of referenced forecast/observation values and algorithm version.
On-demand derivation avoids duplicated authority and drift. Add a score table only if
measurement establishes a boundary such as expensive algorithms, frozen regulatory
results, or query load indexing cannot address; that requires architecture review and an
authority rule.

One lineage header per coherent snapshot/batch pins parent sources, code revision,
lock/environment or OCI identity where available, configuration/policy digests, activity,
and cutoffs. Facts retain logical identity, value/unit/missingness, time, and parent links,
but do not repeat header metadata. Canonical serialization defines logical identity. The
retained original source bytes remain authoritative. Zero-network replay promises logical
values and identities, not identical Parquet bytes across encoder versions.

For later local-grid issuance, provenance must distinguish the shared native guidance
from the derived MesoForge grid, its coordinate/context/editable-domain definition,
regridding/interpolation transforms, and each forecast stage. Preserve separate immutable
original baseline, bias-corrected forecast, AI proposal/edit recipe and final adjusted
forecast with parent links; retain the delivered point's exact coordinate and extraction
method. This is a provenance requirement, not approval of a new table family, storage
layout or duplicate copies of native guidance for every location.

Correction identity includes the approved method/configuration and eligible training
history. AI evidence includes the inspected inputs and site-knowledge version, proposal,
model/configuration identity, tool versions/parameters, validation results and applied
changes. Preserve accepted and rejected decisions so evaluations can distinguish a
proposed edit from an applied one. None of these later records may replace the baseline
or imply that an absent stage has run.

## 9. Retention capability levels

| Retained material and execution dependencies | Available capability | Lost on expiry |
|---|---|---|
| Issued snapshots; forecast, observation, and verification facts; lineage headers; named error/evaluation algorithms and compatible code/configuration | Serve issued history and recompute errors/evaluations under the retained semantics | Expired facts cannot be served or rescored; aggregates lacking their selected facts cannot be recomputed |
| Raw observation responses plus station metadata, lineage, exact normalizer/matcher code, configuration, and environment; rebuilding matches additionally requires the corresponding forecast facts, declared opportunities, pinned cutoffs, and support/matching policies | Renormalize observations; rebuild cutoff-correct matches only while the additional forecast/opportunity dependencies remain | Existing normalized facts remain usable, but source normalization and revision audit become impossible; matching also becomes impossible when either raw responses or its forecast/opportunity dependencies expire |
| Normalized guidance plus native/local grid and coordinate metadata, lineage, applicable regridding/extraction/blend algorithms, baseline configuration, compatible code, and environment | Reproduce the numerical baseline without acquiring or decoding regional source data | Issued facts remain, but baseline reproduction from the normalized cache and unissued extraction become impossible |
| Raw guidance/index bytes plus lineage, applicable decoder/normalizer/regridding/extraction/blend code, configuration, dependencies, and environment | Full zero-network raw-to-cache-to-baseline replay | Shallower retained replay may remain, but full source replay becomes impossible |

Classes expire independently. Raw-guidance expiry does not invalidate issued history; cache
expiry does not erase it; raw-observation expiry does not rewrite normalized facts.

Later correction and adjustment replay additionally requires the exact preceding fields,
retained correction parameters, saved proposal/edit recipe, tool/validator versions,
domain definitions, configuration and compatible execution dependencies. Rebuilding a
learned correction or its evaluation further requires the eligible verified history and
training/evaluation implementation. Replaying deterministic execution of a saved AI recipe
does not mean an AI rerun will generate the same proposal. Serving an immutable final
forecast may remain possible after deeper replay dependencies expire; advertise only the
capability supported by what is actually retained.

Durations are provisional pending storage cost, use, privacy, and owner approval. No
provisional duration gates first release. The correctness minimum is transactional: retain
objects long enough to complete/roll back publication and never advertise scheduled-deleted
objects. Any promised window must be configured, monitored, and accurately exposed.

## 10. Private API

FastAPI is the initial private/operator-controlled adapter, not a public SaaS contract.

| Operation | Behavior |
|---|---|
| `POST /v1/forecasts` | One bounded baseline request; matrix may allow up to 36 horizons |
| `POST /v1/locations` | Register one operator-managed coordinate; no acquisition |
| `GET /v1/locations/{location_id}` | Refresh state and current snapshot link |
| `GET /v1/locations/{location_id}/forecasts` | Bounded paginated immutable history |
| `GET /v1/locations/{location_id}/verification` | Bounded paginated verification history and statuses |
| `GET /v1/forecasts/{forecast_id}` | One immutable snapshot and compact facts |
| `POST /v1/evaluations` | Basic bounded fact aggregates |
| `GET /v1/audits/{audit_id}` | Optional bounded requested detail |

Acquisition, decode, issuance, observation, and verification are internal handlers.
Training, AI, email, and account endpoints are absent. Responses expose cutoff, freshness,
missingness, algorithm/configuration, compact lineage, and support status. Exact limits
follow Section 7. Unsupported combinations reject before reads; not-ready is retryable;
historical reads never substitute a newer snapshot. Registered current reads may serve the
last good snapshot only with explicit age/reason under operator policy.

## 11. Evaluation semantics

Indexed location, issue/valid time, variable, status, and cutoff fields select only requested
facts. Requested empty groups may return zero counts, but the database does not persist all
possible zero cells. Initial metrics may include count, missing rate, mean error, MAE, RMSE,
and approved variable-specific circular/probability scores. Responses name algorithm and
denominator semantics. Elaborate paired, reliability, regime, training, and report products
are later unless separately approved. Availability, ingestion, verification, and evaluation
as-of cutoffs prevent revisions from leaking into earlier results.

Long-term learning has three explicit forms, all future work:

- **Statistical learning:** deterministic site/regime bias correction derived from
  verified history, with versioned training samples, cutoffs, parameters and promotion
  evidence. A correction is not credited with skill merely because it fits its history.
- **Site knowledge:** structured, versioned, inspectable records of recurring local
  behavior and regimes with supporting evidence. Persistent knowledge does not depend
  on an LLM permanently remembering earlier runs.
- **AI performance learning:** compare proposed/applied edit types and their effects by
  location and regime on identical verified samples, retaining rejected and unapplied
  proposals as such. Evaluate AI against the bias-corrected baseline so ordinary
  statistical corrections are not counted as AI value.

Comparisons retain the original issued baseline, corrected and final forecasts and the
same suitable observation revisions, valid intervals, cutoffs and scoring opportunities.
Report sample counts, exclusions and uncertainty supported by evidence; distinguish
training from independent evaluation. Point verification does not establish skill across
the editable grid or for unscored fields. No suitable observations or too little history
means unscored or insufficient evidence, not fabricated skill or automatic promotion.

## 12. Selective rebuild, donors, and retirement

### 12.1 Measured donor facts

The old Phase 3 branch changes 76 files with 37,013 insertions and adds no HTTP endpoint or
physical PostgreSQL table. Additions include 18,701 test/support lines and 1,462 script
lines; 24 new production modules total 16,209 physical lines. Its `39 x 5 x 6 x 9`
aggregation lattice produces 768,690 cells before further dimensions. These are donor
measurements, not V2 caps.

| Source | Disposition | Treatment |
|---|---|---|
| Current identities, provenance, storage, ADRs | Keep/adapt | Preserve content verification, authority split, transactions/idempotency |
| Current guidance normalization/alignment | Keep/adapt | Reuse provider/grid/unit/interval semantics behind shared-cache output |
| Current blend kernels | Keep/adapt | Reuse pure operators, provenance, fallback, exclusions |
| Large coordinators/proof replay | Simplify | Cohesive services and replay per durable boundary |
| Phase 3 coordinate identity/baseline | Selectively port | Port algorithms and independent edge/property tests; adapt to shared cache |
| Phase 3 observation matching | Selectively port | Port cutoffs, ties, statuses, and independent tests |
| Phase 3 lattice/corpus/evidence/proof machinery | Do not port | Replaced by facts, on-demand evaluation, compact lineage |
| Phase 3 host telemetry/admission framework | Do not port | Use measured local bounds and real host margin |

No donor commit is cherry-picked intact. Each port identifies its donor, retained contract,
and independent V2 tests.

### 12.2 Superseded legacy contract

V2 supersedes incompatible old Phase 3 implementation contracts and
`docs/rfcs/phase-3-debloat.md` requirements for singleton scope, persisted Cartesian/
denominator/joint lattices, corpus-root/evidence-bundle/report authentication as product
architecture, all-in-one proof harnesses, and Phase 3 host telemetry/resource admission.
Useful science and measured evidence remain donors; compatibility with those representations
is not acceptance.

### 12.3 Retirement milestones

1. Inventory donor branches, commits, dirty evidence, schemas, algorithms, tests, consumers,
   and artifacts.
2. Selectively port approved contracts with independent V2 tests.
3. Demonstrate replacement parity for claimed first-release behavior: scientific edge
   cases, cutoffs, missingness, and declared replay. Obsolete proof/lattice behavior is out.
4. Owner-approved write-disable of superseded forms while retaining comparison reads.
5. Owner-approved read-disable after consumers/audits and rollback are addressed.
6. Preserve donor branches, history, inventory, and evidence read-only for the approved
   archive/reference period.
7. Only then, after retention obligations and explicit owner authorization, may named
   legacy artifacts become deletion-eligible.

Nothing is deleted now.

## 13. Delivery and review gates

| Estimated vertical slice | Class | Outcome |
|---|---|---|
| Shared cache + private one-off baseline | First release | Ingest once; multiple coordinates reuse; bounded honest response |
| Registered issuance/history | First release | Complete immutable snapshots and atomic current pointer |
| Observations/verification | First release | Revisioned observations and cutoff-correct explicit facts |
| Basic bounded evaluation | First release | On-demand operator metrics without lattice |
| Learning/bias/weights | Roadmap | Real history yields evaluated approved candidates |
| Bounded AI | Roadmap | Structured audited proposals cannot alter baseline |
| Delivery/public product | Roadmap | Separately approved email, then account/privacy/billing concerns |

These are sensible review units, not a mandatory seven-PR sequence. Adjacent slices may be
combined/split for reviewability. The first slice has no dependency on learning, AI, email,
accounts, long-term retention, or public SLOs.

The full inspected canvas, including completed p-type and subsequent native evidence,
is inventoried in section 6.7. The next proposed slice is its conservative read-only
condition preview from an exact saved forecast, with no new fields or active policies.
The final engine, richer phrases and any field promotion require separate approval.

File/module/table/code/test/change-size estimates are non-binding planning aids per slice.
Material overrun triggers review when it reveals changed design, not because of a line
counter. Owner architecture review is required before adding a durable authority, duplicated
canonical representation, broker/workflow, runtime role, per-location acquisition,
persisted evaluation/error lattice, heavy synchronous API work, account boundary, work that
cannot retain host margin, or materially broader support without a benchmark.

Tests cover identity, units, intervals, cutoffs, missingness, wind, blend, matching,
derivation, atomic publication, reuse, retry, immutable issuance, and bounded indexed
queries. Each shipped vertical has a compact acceptance path; there is no test ceiling or
all-in-one proof-harness requirement.

## 14. Non-goals

- public SaaS, accounts, ownership, sharing, privacy/export/deletion workflows, billing, or
  public SLOs;
- learning, AI, or email in the first release;
- per-location ingestion or persistent one-off history;
- coverage beyond the approved bounded matrix;
- a data lake, cube, Cartesian/joint lattice, corpus-root universe, evidence-bundle product
  model, or all-in-one coordinator;
- dashboard/map/mobile/workflow UI;
- indefinite retention or replay deeper than retained inputs;
- microservices, external broker, distributed workflow, or Phase 3 host certification
  without demonstrated need.

## 15. Risks

| Risk | Mitigation |
|---|---|
| Packaging amplifies work | Benchmark cold/warm requests; advertise measured combinations; reject pre-read |
| Nominal host memory misleads | Measure RSS and `MemAvailable` under co-load; reserve margin; bound concurrency |
| Normalizer assumes small bbox | Shared spatial output adapter; multi-coordinate/edge reuse tests |
| Provenance bloats facts | Shared lineage headers with fact links, units, cutoffs, missingness |
| On-demand evaluation is costly | Index measured paths; bound work/output/time; materialize only after review |
| Cutoffs leak future data | Separate fields and property-test exclusions |
| Missingness biases scores | Persist declared opportunities and return eligible/scored/missing counts |
| Baseline appears learned | Explicit baseline labeling; no placeholder skill |
| Retention sounds guaranteed | Expose capability level; promise only approved monitored windows |
| Coordinate becomes account ID | Keep it scientific; design accounts/privacy separately |
| Selective port changes science | Inventory contracts, independent oracles/tests, actual-diff review |

## 16. Alternatives rejected

- Merge/refactor Phase 3: its measured surface is coupled to replaced representations.
- Greenfield rewrite: tested Phase 0-2 science/infrastructure remains valuable.
- Per-location ingestion: cost scales with locations and destroys reuse.
- Persisted Cartesian reports: facts plus bounded reductions answer requested questions.
- Separate first-release error table: it duplicates a cheap deterministic derivation.
- Artifact-only querying: location/time/status discovery needs searchable fact indexes.
- Public accounts first: private operation proves science without identity/privacy coupling.
- Learning/AI first: value cannot be measured and baseline usefulness needs neither.

## 17. Open owner decisions

1. Supported region, model mix, fields, cadence, and desired horizon combinations.
2. Baseline weights, fallback behavior, automatic observation-support suitability
   rules, providers, quality, and cutoffs. Manual per-location station/zone configuration
   is not an open alternative to the coordinate-only geographic input direction.
3. Acceptable shared-host reserve and benchmark co-load.
4. Local-grid spacing, context/editable-domain geometry and extents, boundary/taper
   behavior, source/local packaging, and measured work/output/concurrency/timeout limits.
   The approved preparation/station defaults in section 2.3 do not settle these choices.
5. Retention costs/durations and advertised capability levels.
6. Private authentication/operator authorization.
7. Separately later: correction methods/promotion, site-knowledge representation,
   bounded AI tool algorithms/validation and evaluation policies, delivery, and public
   accounts/privacy/billing/SLOs. The long-term direction does not approve these details.

## 18. First Usable Release exit criteria

1. Owner approval precedes implementation and covers material review-trigger decisions.
2. The support matrix is explicit; representative cold/warm benchmarks record reads, bytes,
   RSS, `MemAvailable`, output, and latency; configured limits follow evidence.
3. A cycle is acquired once, published with verified shared manifests/lineage, and reused by
   multiple arbitrary coordinates.
4. The private API rejects unsupported work before reads and returns bounded deterministic
   forecasts with correct units, cutoffs, contributors, quality, missingness, and lineage.
5. A registered location publishes complete immutable forecast/verification history,
   exposes it through bounded reads, and atomically advances current forecast state.
6. Observations retain support, intervals, cutoffs, quality, units, and identities; matching
   is deterministic and cutoff-correct.
7. Every declared verification opportunity is represented; errors derive consistently with
   named algorithms.
8. Bounded evaluations return query identity, lineage/watermark, metrics, and explicit counts
   without a Cartesian lattice.
9. Retry, partial failure, stale/not-ready behavior, and cancellation preserve correctness
   and demonstrated host margin.
10. Retention accurately reports serving, evaluation, renormalization, normalized-guidance
    reproduction, and raw replay capabilities; representative retained-input replays make
    zero network calls and reproduce logical values/identities at each advertised depth; no
    unapproved duration is promised.
11. Applicable existing tests and independent port tests pass with vertical acceptance.
12. Learning, AI, email, accounts, public SLOs, and long-term retention do not gate release;
    no learned history or skill is fabricated.
13. Donor inventory/parity scope exists and no donor branch/history/evidence is deleted.

## 19. Decision log

| Decision | Choice |
|---|---|
| Rebuild | Selective rebuild from `main`; cohesive tested ports |
| First release | Private baseline, registered history, observations/verification, bounded evaluation |
| Roadmap | Local forecast fields, site/regime bias correction, bounded GFE-style AI tools, delivery and public accounts in separately approved stages |
| Guidance | Ingest once into shared source cache; derive local MesoForge fields using larger context and smaller editable domains, then interpolate the exact point |
| Issuance | Immutable baseline, corrected fields, proposal/recipe and final fields when implemented; mutable pointer/status only |
| Facts | Normalized facts and on-demand evaluation; no Cartesian lattice |
| Errors | Derive on demand; no first-release `error_facts` |
| Provenance | Compact shared lineage header plus fact identities/links/cutoffs/units/missingness |
| Eligibility | Availability and ingestion both meet explicit as-of cutoffs |
| Horizons | Up to 36 is a requested envelope conditional on measured support |
| Packaging | Select by representative benchmark; `64 x 64` only a candidate |
| Safety | Measured runtime bounds and real host margin; unmeasured targets provisional |
| Complexity | Non-binding estimates; material new machinery triggers owner review |
| Delivery | Small sensible verticals; no mandatory PR count |
| Retention | Capability levels separate; durations provisional and non-gating |
| Identity | Coordinate ID is scientific, not account/ownership identity |
| Governance | Humans approve code/merges; automation executes configured production work |
| Legacy | Supersede incompatible Phase 3/de-bloat contracts; preserve read-only; delete nothing |

This architecture ships useful deterministic forecasts and honest verification first while
preserving the immutable, cutoff-correct foundation needed by later learning and bounded
adjustment without making either a prerequisite.
