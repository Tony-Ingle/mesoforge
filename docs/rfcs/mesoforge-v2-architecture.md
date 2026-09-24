# RFC: MesoForge V2 architecture

Status: Active technical reference with implemented slices and remaining proposals

[VISION.md](../../VISION.md) is the canonical product direction. This RFC retains
scientific contracts, implementation details and unresolved design choices. Earlier
private-API release planning below is historical scope, not a competing product
vision or an instruction to implement public/registration services. Explicitly marked
implemented sections describe current code; remaining designs require approval.

Decision owner: MesoForge owner

Architecture author: Codex

> **Reference status (2026-09-09):** The source/donor descriptions below record the
> inputs used when this RFC was written, not current branch or working-tree state.
> The later preserved Phase 3 donor is
> `43f56bc0c67ab782c94fb6349d65523793e1a836`; see the
> [archive index](../archive/README.md). Remaining designs are proposed: the decision log
> does not imply blanket owner approval. The entry-point
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

**Canonical owner vision, 2026-09-18 — the blend is the forecast.** The target
background pipeline is native contributors → prepared contributor state →
field-specific blends → cross-field coherence → immutable MesoForge baseline snapshot.
A configured-location run pins one baseline → derives/extracts its local domain →
applies deterministic site correction → an AI
desk that sees the baseline, every contributor and the surrounding context → bounded
spatial/temporal field edits → final MesoForge grid → spot forecast. Individual models
are contributors, evidence, provenance and context, never competing final forecasts
and never a "selected model." Section 5.5 describes the field-specific blend layer;
section 5.6 separates the slow background guidance refresh from forecast requests.
[VISION.md](../../VISION.md#north-star) holds the
owner-facing statement. This clarifies direction; current fixed weights and
single-source rules remain in force as implementation scaffolding, and no future
stage is approved by this text.

The selective rebuild reuses scientific kernels and infrastructure whose contracts
remain valid, ports cohesive donor algorithms with independent tests, and replaces
obsolete Phase 3 product architecture. The `main` baseline and donor revisions above
describe the original design inputs, not instructions to restart current development.

The central data-flow decision is **ingest shared guidance once, derive forecasts for many
locations**. Background work acquires and normalizes each supported model cycle once into a
shared spatial cache. Nearby locations reuse source data. Local MesoForge forecast
fields are derived products, distinct from complete native model datasets; registration
does not require a separate source download or native-dataset copy for each location.

The original **private-baseline development slice** was scoped to:

- a private, operator-controlled API on a bounded supported region;
- deterministic baseline forecasts for arbitrary supported coordinates;
- persistent immutable forecast and verification history for registered locations;
- observation acquisition and deterministic matching;
- basic bounded performance queries over normalized facts.

Learning, bias correction, learned model weighting, a bounded AI forecaster, email, and
delivery are later stages. Generic public API and multi-user/account products are
outside the canonical product scope. These later stages do not gate the useful
numerical development slices already implemented.

The implemented numerical foundation is useful without learned history: it turns shared model guidance into a
reproducible, unit-correct point forecast; preserves what was issued; matches later
observations; and exposes measured baseline performance. A new location reports no learned
history and makes no learned-skill claim. The system never invents samples, confidence,
bias, weights, or skill.

The owner has clarified this long-term direction and separately approved implemented
slices. The rest of this RFC remains proposed; documenting the direction does not
approve new forecast behavior or every release stage. Its delivery sequence is a
non-binding estimate, not a required PR count.

## 2. Release boundary

### 2.1 Historical private-baseline release proposal

The following bounded API/registration plan predates the canonical configured-location
vision. Preserve its safety/scientific reasoning; do not treat its endpoint list,
registration service or exit checklist as current implementation or the next milestone.
The target operating model is sections 5.1 and 6.6; current commands are in section 5.6.

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
inventory and deterministic condition layer are in section 6.7. The read-only
saved-grid preview, initial presentation rules, multi-hour transition detection,
period summaries and the latest-complete prepared snapshot of section 5.6 are
implemented. The next proposed slice is recorded in [README.md](../../README.md),
not another meteorological field.

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
extracted from those fields. Native NBM total cloud is a temporary delivered sky
baseline, with HRRR/GFS/RAP/IFS comparison evidence; missing NBM has no substitute.
Optional attachments retain other surface/winter evidence. Temporary native
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

Historical provider-planning reference, last checked September 10, 2026: NWS
[SCN 26-47](https://www.weather.gov/media/notification/pdf_2026/SCN26-47_Updated_Retire_NAM_SREF_HREF_HiresW_NAM_MOS.aab.pdf)
and [SCN 26-48](https://www.weather.gov/media/notification/pdf_2026/scn26-048_Updated_RRFS_and_REFS_Implementation_aad.pdf)
described the NAM/NAM-nest retirement and RRFS/REFS replacement path. These historical
references are retained from the earlier vision; this audit did not reverify operational
dates. Recheck official notices before implementing transition-dependent adapters.

Retain raw model files/messages actually acquired, including currently unused
fields, separately from prepared subsets. Later field/product trimming requires
a separate decision. This direction does not authorize unbounded acquisition of
fields, levels, leads, or models, nor settle indefinite-retention guarantees.

- deterministic site/regime bias correction from verified history, inspectable site
  knowledge, and evaluated model weighting;
- bounded GFE-style spatial/temporal AI edit recipes executed by deterministic,
  versioned tools, with adjustment performance measured against the bias-corrected baseline;
- email or other delivery;
- operator access and delivery controls where needed for configured locations;
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
or in bounded batches. The orchestrator decides **when** MesoForge is invoked;
MesoForge owns **what** a forecast run means. Model discovery, blending and weather
science never move into workflow YAML. Acquisition/preparation stays outside forecast
HTTP requests and produces shared guidance reusable across nearby coordinates, not
per-location downloads; section 5.6 describes the intended background refresh.
Section 6.6 describes the intended location lifecycle, including later AI and
delivery stages. This direction does not implement or approve a combined Actions,
registration, verification, AI, and delivery milestone.

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
- **Prepared contributor snapshot (implemented):** normalized source evidence and
  manifests published by `refresh_guidance`; the current consumer still calculates
  the local blend/grid. It is not a blended baseline.
- **MesoForge baseline snapshot (future):** immutable field-specific blends after
  baseline coherence, maintained before location runs and linked to their exact
  contributor state. A location run pins it throughout correction/editing/issuance.
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
- **Baseline blend:** the delivered forecast is the MesoForge field-specific blend and
  its later stages, never one selected model. Each field's policy is versioned and
  uses mathematics valid for that field (section 5.5). Today contributor set and lead
  band select a reviewed versioned weight row. Fallback is explicit/degraded; weights
  are not invented or silently renormalized. Every contributor's values stay retained
  beside the blend whether or not they carry active weight.
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

Current command split (no scheduler):
refresh_guidance -> prepared contributor snapshot / latest_complete
forecast_from_snapshot -> pin prepared evidence -> local blend/grid -> optional issuance

Target background engine:
new eligible guidance -> prepared contributor state -> field-specific blends
field-specific blends -> baseline coherence -> immutable MesoForge baseline snapshot

Target configured-location path:
pin baseline snapshot -> derive/extract local grid baseline
local grid baseline -> deterministic bias correction
bias-corrected fields + every contributor + context/evidence -> AI tool recipe
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
or approved numerical algorithms. Every operation edits MesoForge's own blended field.
When HRRR/RAP support heavier QPF than GFS/IFS, the recipe is "raise the MesoForge QPF
field toward the stronger solution over this region and time window," with those
contributors cited as evidence; "use HRRR instead of GFS" is not an operation, and a
recipe never stores a global model-selection decision.

Deterministic versioned tools execute retained recipes against the identified corrected
forecast. Validation checks physical and cross-field consistency, parameter/value bounds,
continuity, cutoff eligibility and the editable domain before any result can be issued.
Invalid proposals retain an explicit rejection reason; a permitted fallback to unchanged
corrected fields must be recorded. Human approval governs tool/policy development and
release, not each normal configured forecast. The AI cannot bypass that policy.

### 5.5 Field-specific blend layer

**Owner direction, 2026-09-17; conceptual, not an approved algorithm.** The MesoForge
baseline grid is produced by one blend policy *per field*. A policy is a named,
versioned object that states its eligible contributors, the mathematics valid for
that field, its missingness/fallback behavior and the evidence that justified it.
There is no universal weight vector.

| Field family | Blend mathematics the policy must respect |
|---|---|
| Scalars (temperature, dew point, cloud fraction, visibility, …) | Weighted numerical blending after unit normalization; cross-field consistency (for example dew point ≤ temperature) still applies |
| Wind | Blend earth-relative U/V components, then derive speed/direction; never average direction degrees; gust stays coupled to its contributor set |
| QPF, SWE, snowfall, ice amounts | Blend only amounts with identical exact accumulation intervals; preserve interval semantics and native definitions (snow versus snow-and-sleet, liquid versus flat ice) |
| PoP, thunder and other probabilities | Combine/calibrate real probabilistic guidance for one identical event (threshold, period, spatial support); never infer probability from deterministic QPF |
| Precipitation type | Weighted categorical/probabilistic support for rain, snow, freezing rain, sleet and mixed states across all suitable sources, replacing the interim requirement that two particular deterministic models agree |
| Derived fields (RH, speed/direction, sky category) | Derived from compatible blended parents by a versioned method, not blended independently. Current Kuchera remains separate native-source evidence using its own profile/SWE contract |

**Dynamic-weight inputs (conceptual).** A policy may eventually let weights depend on
field, forecast lead, which contributors are actually available, the age/freshness of
each available cycle, verified historical skill of each contributor for that field,
site-specific performance and, later, weather regime. Freshness is one input, never
the sole decision: a newer cycle does not automatically outweigh a better-verifying
model. This RFC defines no equations, multipliers or default values; each must come
from suitable verification evidence through a separately approved, versioned policy,
following the existing shadow → evaluated → active lifecycle.

**Contributor preservation.** Whatever the weights, an issuance retains every
contributor's field values, cycle, lead, units, missingness and provenance beside the
blend, including zero-weight contributors. Verification must be able to answer: what
did the baseline say, what did each contributor say, what did deterministic
correction change, what did the AI change, and did the AI improve the forecast?

**Target stage order.** Prepared contributors → field-specific baseline blend →
baseline coherence → publish/pin immutable baseline → local domain → deterministic
verified site/regime correction → bounded AI adjustment → final validation → final grid →
verification. Persistent statistical bias is removed by the deterministic correction
first, so the AI is judged against the bias-corrected baseline and earns no credit
for rediscovering a mean bias (section 11).

**Current scaffolding (generalized dispatch implemented).** The policies are fixed
and remain in force; none of them is the final philosophy:

| Field | Current representation | Where it lives |
|---|---|---|
| Temperature | Named recipe `temperature_control_v1`, version `1`, HRRR/GFS 70/30, `require_all`; issuance refuses any other control recipe | `forecasting/recipes.py`, dispatched by `forecasting/field_blend.py`; `validate_current_control` in `application/batch_forecast.py` |
| Dew point, U/V, gust, QPF | Retained Phase 2 fallback tables keyed by available-model set and lead band (70/30 h1–18, 60/40 h19–36); active HRRR/GFS set and table column order preserved | `blend_configuration` in `configs/phase2-grasston.yaml`, dispatched by `forecasting/field_blend.py` |
| RH | `bolton-1980-relative-humidity-liquid-water.v1`, diagnostic from blended temperature/dew point | `forecasting/surface.py` kernel, dispatched by `forecasting/field_blend.py` |
| PoP, sky, thunder | Temporary single-source NBM passthrough policies with weight 1 and no substitute | `pop_policy` in the same configuration; policy constants in `forecasting/cloud_cover.py` and `forecasting/thunder.py` |
| Precipitation type | Temporary HRRR/GFS categorical agreement rule | `application/precipitation_type.py` |
| Visibility, SWE, snowfall, Kuchera, ice | Evidence only; "no approved policy" placeholders with a null active value | the corresponding `application/*` and `forecasting/*` modules |
| RAP, IFS, other NBM/GEFS/REFS/ECMWF products | Zero-weight shadows/evidence | contributor registry and attachments |

**Implemented numerical blend core.** `forecasting/field_blend.py` contains one
immutable `FIELD_REGISTRY` for temperature, dew point, wind, gust, QPF and derived RH.
Each definition states its semantic kind, output units, existing policy binding,
execution handler, missingness contract and present dependencies. `FieldBlendEngine`
binds the existing `ContributorConfiguration` and `Phase2BlendConfiguration` once
for a column. `policy_for` returns the original recipe/table/diagnostic identity;
there is no duplicate policy schema, copied weight table or new policy ID.

`blend_field(field_id, BlendState)` is the common production dispatch boundary.
State contains already extracted native values for one cell/hour, exact QPF bounds,
cached dependency results and source-validation evidence. Specialized handlers call
the retained scalar, vector, gust, QPF and RH kernels. Native input dictionaries are
not modified. Wind/gust share the same accepted U/V/gust contributor tuple; gust
depends on blended sustained speed. Dew point retains native and blended-temperature
checks; RH is derived, never independently weighted. Dependencies are the few
explicit handler calls needed today, not a generalized coherence/graph engine.

Temperature remains 70/30 with both inputs required at all 36 hours. Dew point,
wind and gust reuse `phase2-scalar-vector-fallback.v1`; QPF reuses the distinct
`phase2-qpf-fallback.v1`. Their HRRR/GFS rows and singleton fallbacks are unchanged.
`gust-blend-policy.v1` still governs source and final floors. QPF extraction checks
exact one-hour `(start,end]` intervals, native units/corners and retained parents
before dispatch; incompatible interval metadata cannot be combined. Zero remains
a valid amount. All policy row identities/digests, exclusions and shadow evidence
stay available beside the resulting fields.

The old `blend_surface` orchestration and application-level QPF blend block are
removed. Application code extracts evidence; the engine produces migrated fields.
`surface_fields` only assembles the existing saved shape, including the legacy
surface-temperature label `unchanged-temperature-control` and cloud placeholder;
the containing forecast still retains the actual temperature recipe and weights.
Temperature-only historical prepared artifacts still use the same recipe kernel
through dispatch. No historical payload, grid schema, policy ID or read path changes.
Execution-source hashes and resulting new-grid digests change intentionally.

NBM PoP/sky/thunder, p-type agreement and all evidence-only products remain on their
existing field-specific paths, outside this migration. The separate retained Phase 2
station assembler remains a compatible consumer of the scientific kernels and its
three-model contracts. Comparison recipes still use the generic scalar recipe kernel.
Continuous blended-baseline publication, generalized coherence, dynamic weights and
corrections remain future work; section 5.6 still publishes prepared contributors.

### 5.6 Implemented guidance refresh and the latest complete prepared snapshot

**Implemented command boundary, 2026-09-18.** Model acquisition/preparation and
forecast generation have separate clocks. The diagram below describes today's
prepared-contributor snapshot, not the target preblended baseline of section 5.1.
Continuous background operation and production scheduling are not implemented.

```text
BACKGROUND GUIDANCE REFRESH
new model cycles become available
  -> discover / acquire / decode / prepare
  -> validate completeness and provenance
  -> build/update shared prepared guidance
  -> publish an atomic `latest complete` prepared snapshot
  -> retain the previous good snapshot until the replacement is complete

LOCAL FORECAST COMMAND
configured coordinates (or development lat/lon request)
  -> use the newest complete prepared snapshot already available
  -> construct/read the local MesoForge baseline grid
  -> later: deterministic correction + AI desk
  -> return forecast
```

- The refresh is slow and never runs inside a forecast request. A request never waits
  for GRIB downloads or for the next clock-hour decision window; with no usable
  snapshot it reports that state honestly instead of acquiring guidance.
- A snapshot is immutable once published. Publication is one atomic pointer change
  made only after every required input passed validation; a failed or partial refresh
  leaves the previous good snapshot current. Still-publishing provider cycles are
  incomplete candidates: the refresh falls back to the newest complete cycle and
  records the rejected candidate as evidence.
- A snapshot records its own information cutoff, each contributor's cycle, provider
  availability and acquisition times, and the valid-time range it can serve.
  Contributing cycles are not required to match one another or the issuance/reference
  hour: an issuance at 17:37 local time may use HRRR 18Z, RAP 21Z, GFS 18Z and IFS 12Z
  if those are the newest complete eligible inputs. The rule is that every input used
  was legitimately available before that issuance's information cutoff. Issuance time,
  source cycles and source availability stay separately recorded facts.
- Issued/scheduled forecasts use the same snapshot. An external orchestrator such as
  GitHub Actions decides when to invoke issuance and delivery; MesoForge decides what
  the run means (section 2.3). The decision-window policy of section 6.7.14 continues
  to define the canonical scheduled forecast.
- Snapshot cadence, continuous operation on the VPS, retention of superseded
  snapshots and a shared (non-filesystem) pointer location are open (section 17).

**Implemented slice (2026-09-18; correctness updated 2026-09-24).** The refresh and the
snapshot-consuming forecast exist as separate commands; the operator/compatibility
`forward_run` path is unchanged.

- *Prepared window and coverage policy* (`mesoforge-prepared-coverage-policy.v1`,
  `mesoforge.guidance.coverage`): a prepared window is hours 1..N after its reference
  time, 36 ≤ N ≤ 42. Cycle acceptance still uses the first 36 hours exactly as before;
  hours 37..N are probed only on the accepted cycle, and N is the largest hour every
  selected deterministic model reaches. Prepared datasets were already keyed by absolute
  valid time, so a later reference hour R is served by a *reference view*
  (`PreparedPointForecast.reference_view`) that reads hours R+1..R+36 from the same
  files: source cycles, leads and evidence are exactly what was prepared, and the
  forecast records `prepared_window` (prepared reference, prepared hours, offset)
  beside its own `target_reference_time`. The 42-hour target is 36 forecast hours plus
  six hours of refresh grace; it is not forced. HRRR and GFS are limited by their
  48-hour adapter envelope (42 hours needs a cycle no older than six hours), RAP by
  its 51-hour extended cycles, IFS keeps native three-hourly slots. NBM hourly core
  products are requested lead by lead up to the same 48-hour envelope and their actual
  availability is recorded per valid time.
- *Usability rule*: a snapshot serves R only if HRRR and GFS hold all of R+1..R+36. The
  NBM-based active products (hourly PoP, sky, thunder) and the shadow/evidence
  attachments report covered and missing valid times per product; they never shorten
  the 36-hour forecast and never block publication, matching the explicit per-hour
  unavailability the current policies already produce. An unusable snapshot yields
  `no_current_snapshot`.
- *Refresh* (`python -m mesoforge.application.refresh_guidance --config … --root …`):
  discovery → `prepare_selected` with NBM PoP → p-type → cloud → thunder → visibility
  evidence (optional, non-blocking) → offline load and one validation column per
  configured coordinate → `snapshot.json` (`mesoforge.prepared-snapshot.v1`) →
  atomic `latest_complete.json` (`mesoforge.latest-complete-pointer.v1`, written to a
  temporary file and `os.replace`d). A stable OS lock file per local root serializes
  reference-time comparison and publication across processes; an older reference
  cannot replace a newer one, and equal-reference replacement remains permitted.
  Failed publication leaves the previous pointer unchanged and
  retains `failure.json`. The manifest distinguishes native deterministic
  contributors (HRRR/GFS active; RAP/IFS shadow evidence), the blended meta-model NBM
  with its active-current-policy products, and evidence-only inputs; it names the
  current field-policy identities, references every preparation artifact by path and
  digest, and carries refresh timings and bytes.
- *NBM candidate fallback* (owner-approved compatibility behaviour): when the newest
  NBM cycle fails identity/range validation, typically because it is still
  publishing, PoP selection rejects it, records `rejected_candidates`, and tries the
  next older cycle; completeness is judged on the required 36 hours and extension hours
  are explicit gaps. Validation itself is unchanged.
- *Optional shadow discovery/preparation*: the refresh opts into
  `require_complete_shadows=False` for both discovery and preparation. HRRR/GFS
  discovery stays strict. Unavailable or unprovable RAP/IFS cycles are explicit
  `shadow_discovery_shortfalls`; their acquisition is omitted, and the snapshot records
  null cycles, unavailable shadows and missing valid times. Future refreshes may restore
  them normally. No source is substituted or promoted. The active HRRR/GFS objects and each
  zero-weight shadow use separate pinned views of the same discovery (the shadow views
  do not latch on a first failure), so a provider failure on a RAP/IFS object is
  recorded per object and per missing hour instead of aborting the preparation. The
  standalone selection and issuance-bound forward run keep the strict default.
  Successfully selected shadow coverage can still limit the extension envelope.
- *Forecast from snapshot* (`python -m mesoforge.application.forecast_from_snapshot
  --root … (--lat --lon | --config) [--reference-time] [--issue]`): resolves the
  pointer, verifies manifest and retained-artifact digests, derives the reference hour
  as the request's current UTC hour, checks absolute coverage, builds the local grid
  with the existing policies and returns the 36-hour forecast with `prepared_snapshot`
  provenance (snapshot ID, publication time, request/reference times, contributor
  cycles, field policies, coverage). It makes no provider calls. `--issue` reuses the
  existing forward-run PostgreSQL advisory lock through metadata lookup, grid build
  and storage. Concurrent snapshot issuers wait and recheck committed versions before
  creating an issuance; a second primary attempt skips the existing coordinate/reference
  version, while `--reissue` explicitly permits another version. A lookup failure is isolated as
  `issuance_lookup_failed` and later coordinates continue.
- *Information provenance*: additive `source_information` in the existing v1 manifest
  pins retained source/region manifests and hashes, each input's availability/acquisition
  clocks and recorded source-specific discovery cutoff. Attachments do not inherit the
  original model-set cutoff. An actual retrieval time may be a labeled conservative
  known-by bound when provider publication time is absent. Snapshot issuance sets
  `forecast_analysis_cutoff` to request time and rejects unavailable timing proof or
  inputs/discovery/completion/publication after that cutoff. Reference time, source
  cycles, publication and actual issuance remain distinct. Historical snapshots without
  the additive field remain readable through reconstruction of retained evidence, with
  limitations reported rather than fabricated timestamps; historical issuances stay
  immutable. This does not implement the broader future cutoff/storage architecture.
- *Still bound*: the refresh must still complete inside its decision hour (the
  existing selection expiry), coordinates outside the refreshed collection's footprint
  are refused rather than prepared on demand, conditions/transitions/period previews
  remain read-only over saved issuances, and the local-grid build still dominates
  request time. It reuses one projection transformer per native CRS and takes
  ownership of the ephemeral grid instead of copying it, which cut a measured
  49-node, 36-hour build from 178.6 s to 25.4 s with byte-identical output; the
  remainder is per-column evidence copying and the required canonical grid digest.

The independent audit at `5eb1ae6` reproduced the pointer race and location-lookup
failure and found the unsupported universal cutoff claim and strict optional-shadow
discovery. The bounded fixes above preserve forecast science and current field policies.
The retained real sample had later PoP discovery but no demonstrated historical leakage.
See [README's snapshot correctness notes](../../README.md#refresh-guidance-and-forecast-from-the-latest-complete-snapshot)
for locking details, compatibility and remaining operational limits.

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

The request consumes the newest complete prepared snapshot (section 5.6) and never
waits for acquisition or for a clock-hour decision window. The API reads enclosing
objects, extracts the point, applies the field-specific blend policies (section 5.5),
and returns values, units, contributors/exclusions, freshness,
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
3. Pin one immutable MesoForge baseline snapshot maintained by the target background
   engine, then derive/extract the local fields. Today section 5.6 pins prepared
   contributors and calculates the local blend at consumption time; preblending and
   generalized baseline coherence remain future work. Save
   the original numerical baseline, every contributor's values and the
   source/transform/configuration identity. No acquisition or regional grid preparation
   occurs inside a forecast HTTP request.
4. Apply approved deterministic site/regime bias correction learned from eligible
   verified history, keeping the corrected fields separate from the original baseline.
   With insufficient history or no approved correction, report that status explicitly.
5. Let the later AI forecast desk inspect the baseline, every contributor field,
   surrounding meteorology, disagreement, eligible observations, verification history
   and structured site knowledge, then propose a bounded spatial/temporal edit recipe
   against the corrected MesoForge fields. It never selects a model as the forecast.
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
The inventory records inspected code at that checkpoint. Subsequent owner approvals
enabled the saved-ID read-only preview, temporary NBM active sky cover, and the bounded
presentation policy in §6.7.8. Those explicit approvals do not approve other source
weights, field promotion, intensity/fog rules or the remaining proposed
architecture. Transitions and periods were subsequently implemented in sections
6.7.10–6.7.11. This section supersedes older field-status and next-field descriptions
elsewhere in this RFC.

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
| Cloud — `F.cloud_area_fraction`; `S.cloud_guidance.contributors` | Entire-column total cloud / provider total sky cover; not individual layers | Active fraction `1` plus unrounded percentage; evidence `percent`; I | Temporary native NBM deterministic total sky TCDC baseline; separate HRRR/GFS/RAP TCDC and native three-hourly IFS `tcc` evidence |
| Sky category — active field and per native contributor `sky_category` | Deterministic category of unrounded total cloud percentage | Category; C/I diagnostic | Existing `native-cloud-percentage-display.v1`; delivered sky uses only the approved active NBM field |
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
| U/V, speed/direction | B; U/V blended as vectors with coupled eligible gust set and same Phase 2 rows; speed/direction D | Eligible sole-source rows explicit; invalid coupled source excluded; calm speed 0, direction null with reason | Numeric wind and approved breezy/windy presentation descriptors (§6.7.8); no invented sustained duration; calm-direction can be not applicable | P2 only |
| Gust | B; same U/V contributor set/row; RAP E; IFS U | Existing approved ≤0.1 m/s source shortfall floor and ≤1e−6 m/s final floor recorded; incompatible IFS interval maximum stays missing | Numeric gust and separately specified gust thresholds in §6.7.8; never relabel gust as sustained wind | P2 only |
| Hourly QPF | B; Phase 2 precipitation HRRR/GFS rows 70/30 h1–18, 60/40 h19–36; eligible sole-source fallback | Exact hourly intervals and finite corners required; native GFS bucket/reset/tolerance contract retained; zero ≠ missing; RAP/IFS U | State amount and period; not PoP, instant occurrence or observed intensity. Amount/intensity-class thresholds need policy | P2 only |
| Hourly PoP | S; NBM-only weight-1 passthrough | Exact >0.254 kg/m² event and 1-h bounds; missing/invalid → null; no longer-period fallback | Numeric probability plus approved presentation bands (§6.7.8); no calibrated joint type-specific probability | P2 only |
| Other precipitation probabilities | E; no delivered weights or calibration | Exact threshold/comparator/window/spatial support required for comparison; native gaps explicit | Supporting diagnostics only; neither substitute nor extra vote for delivered PoP | none |
| P-type | Temporary B categorical agreement, no scalar weights; RAP/IFS/NBM E | Complete HRRR/GFS flag sets must agree; equal multi-type set → mixed; disagreement → ambiguous; zero flags → unknown, not dry; no one-source fallback | Native endpoint state retained separately from rendering applicability; §6.7.8 supports conservative labels, not type persistence through an interval or transition claims | none |
| SWE | E+P; no approved blend; active value null | HRRR/RAP hourly and IFS native 3-h bounds stay separate; GFS snowpack WEASD and absent NBM SWE unsupported; no QPF/type conversion | Evidence-only; cannot make delivered snow occurrence or amount | none |
| Native snowfall amount | E+P; no approved blend; active value null | Native parents/windows required; NBM snow-and-sleet not silently equated to HRRR/RAP snow; GFS/IFS new-depth U | Evidence-only; no inference of occurrence/type from positive snowfall alone | none |
| Kuchera snowfall / SLR | E+D+P for delivered amount; versioned RAP method | Missing required profile/SWE → missing; no constant-ratio fallback; preserve interval-end approximation and native-corner calculation | Evidence-only; no automatic promotion over native guidance | none |
| Native NBM SLR | E; separate instantaneous SNOWLR | Missing/invalid remains unavailable; no unapproved cross-model or interval-average use | Supporting evidence only, not a condition | none |
| Cloud / per-source sky category | Active temporary NBM under `nbm-native-total-cloud-baseline.v1`; HRRR/GFS/RAP/IFS E; categories D | Incompatible layers/averages rejected; IFS native 3-h gaps explicit; zero cloud valid; missing NBM has no fallback | Eligible for sky using saved active percentage/category. Multi-source cloud weighting/calibration remains open and requires verification evidence | none |
| Visibility | E+P; active visibility null; HRRR/GFS/RAP/NBM E, IFS U | Native finite nonnegative horizontal visibility required; no invented cap or filling | Reduced-visibility clause blocked by active-policy gap; fog additionally requires suitable causal evidence and a validated rule | none |
| Hourly thunder | Temporary S; NBM hourly native passthrough | Missing/invalid → null; no 3/6-h replacement; unencoded threshold/footprint remain unknown | Approved probability wording (§6.7.8), retaining provider-event uncertainty; never deterministic exact-point lightning | none |
| Longer-period thunder / other lightning products | E or U; no combined source policy | Unlike events/periods/support are incompatible; deterministic diagnostics not probabilities | Evidence-only; cannot determine a delivered thunder clause | none |
| Freezing-rain liquid | E+P; HRRR/RAP FRZR; no active blend | Same-cycle cumulative parent differencing; negative/nonfinite increments missing, not zero; unsupported sources explicit | Evidence-only amount; does not establish occurrence or accreted ice | none |
| Native flat ice | E+P; NBM FICEAC native 1/6 h; no active blend | Retain kg/m² native meaning; no density/thickness/period conversion; GFS/IFS U | Evidence-only hazard amount; not a road-icing diagnosis or a substitute for p-type | none |

Every **B**, **S** and temporary row above is current scaffolding. Each is expected to
become a field-specific blend policy under section 5.5, and each **E+P** row needs one
before its field can be delivered; none changes without separate owner approval and
suitable verification evidence.

Across every row, original cycles, **model source leads versus target horizons**, valid
times, interval bounds, field/recipe identities, raw/prepared hashes, spatial extraction,
units, exclusions and applied weights remain inspectable. Optional-field absence in
an old record is `unavailable`, not zero. Surface table code can report `fallback`
even for a selected two-source row: inspect the applied weights/row and exclusions,
not the status string alone. Report rounding is never input to a condition decision.

Code anchors for these inventory facts:

- [`extract_surface_inputs`](../../src/mesoforge/application/surface_forecast.py),
  [`FieldBlendEngine.blend_field`](../../src/mesoforge/forecasting/field_blend.py),
  [`relative_humidity_percent`](../../src/mesoforge/forecasting/surface.py),
  [named recipes](../../src/mesoforge/forecasting/recipes.py), and
  [applicable retained rows](../../configs/phase2-grasston.yaml).
- [`extract_precipitation_contributors`](../../src/mesoforge/application/precipitation_forecast.py),
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
    "sky": {"state": "unavailable", "value": null, "reasons": ["active_nbm_cloud_guidance_unavailable"]},
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

#### 6.7.5 Deterministic rule hierarchy

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
   type flags do not prove no trace/drizzle/snow. The approved §6.7.8 applicability
   rule can make precipitation `not_applicable` for rendering while retaining the
   native p-type state unchanged. It is not a new meteorological dry classifier.
5. **Derive components using versioned, explicit rules.** Reuse existing numerical
   diagnostics and sky categories where their active inputs qualify, plus the approved
   probability/wind presentation bands in §6.7.8. Any later intensity class,
   persistence window or changed boundary comparator requires explicit policy.
   Never round before classification.
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
7. **Derive transitions only from eligible adjacent results (§6.7.10).** That layer
   uses eligible adjacent results from one issuance/stage,
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
| Clear → cloudy | Temporary active native NBM total-cloud input plus existing unrounded upper-inclusive 5/25/50/87/100% categories. Shadow categories cannot replace missing active sky. No total from summed cloud layers |
| Rain/snow; chance/likely variants | §6.7.8 hourly applicability/bands plus resolved native endpoint type. Preserve the distinct timed inputs; this is a presentation phrase, not a calibrated type-specific probability or interval-wide type claim |
| Mixed precipitation | Known supported multi-type set plus appropriate occurrence semantics; disagreement alone remains ambiguous, with alternatives shown rather than false certainty |
| Freezing rain possible | Eligible occurrence/type linkage and actual freezing-rain type evidence; temperature below freezing, positive FRZR/FICEAC, or surface QPF alone cannot establish it |
| Thunderstorms / likely | §6.7.8 active hourly probability bands with provider-event uncertainty retained; not deterministic point lightning or a replacement event definition |
| Reduced visibility / fog | Active compatible visibility plus approved restriction bins for the former; additional suitable fog evidence/validated causal diagnostic for the latter. Low visibility, high RH, small T−Td, snow or rain independently are insufficient |
| Breezy / windy | §6.7.8 separate sustained/gust thresholds; windy takes precedence. Presentation descriptors, not NWS hazard criteria or an instantaneous-to-hourly averaging assumption |
| Snow with wind | Both separately eligible components at compatible support. “Snow; windy” need not imply **blowing snow**, which needs additional suitable blowing-snow evidence/validated rule |
| Mostly cloudy with chance of rain | Both sky and typed occurrence gates must pass. A missing active cloud policy cannot be filled from an arbitrary shadow just to complete the sentence |

#### 6.7.6 Which gaps block which behavior

| Gap / decision | What it blocks | Conservative treatment without changing current forecast policies |
|---|---|---|
| Cloud multi-source skill/calibration remains open | Claims of optimized or permanent multi-source sky delivery | Native NBM is the explicitly approved interim sky baseline; other models remain comparison evidence. Missing NBM yields unavailable sky. No temperature-weight copying |
| Visibility has no approved active policy; fog has no validated cause rule | Reduced-visibility descriptors and fog | Both unavailable. Retain evidence without inferring fog, rain intensity or blowing snow |
| SWE/snowfall/SLR/ice have no approved active amount policy | Delivered winter amounts, amount classes and accretion/road-ice claims | Keep E/P fields excluded. Does not block future snow/freezing-rain type wording if independent occurrence/type rules qualify |
| Temporary p-type agreement; no interval-wide type persistence rule | Confident type-throughout-interval claims and transitions | Keep endpoint type/state and disagreement. §6.7.8 rendering applicability is separate; agreement policy and native unknown states remain unchanged |
| NBM sole-source hourly PoP; no calibrated multi-source policy | Claims of optimized/multi-source/type-specific probability | Existing native event may be displayed numerically; more probability sources/calibration are not prerequisites for that limited use |
| NBM thunder event threshold/footprint not fully established | Deterministic exact-point thunder claims | §6.7.8 probability wording retains provider-event uncertainty; no new product or deterministic proxy substitution |
| Amount/intensity classes and fog rules remain unapproved | Intensity and fog claims | Approved endpoint transitions and period grouping exist in §6.7.10–6.7.11; do not extend them to intensity or unsupported temporal claims |
| Condition verification/calibrated confidence absent | Skill claims, calibrated whole-condition confidence and automatic rule promotion | `confidence: not_calibrated`; independent behavioral tests are not forecast-skill evidence |

The bounded preview, temporary NBM sky and §6.7.8 presentation thresholds have now
received explicit owner approval. Exact numeric events, amounts and endpoint type
remain separate. No visibility, winter-amount or production-weight decision is needed
to render those approved components. Other gaps above remain open; documenting a
future rule does not approve it.

#### 6.7.7 Implemented read-only slice and checks

Implementation status: the saved-ID read-only preview
is implemented in `forecasting/conditions.py` and `application/weather_conditions.py`.
The owner then approved native NBM instantaneous total cloud as a temporary active
sky source, using the existing category mapping. Preview/template v2 adds that sky
component without changing other condition policies or historical forecasts.
NBM-only cloud enables deterministic sky wording while the broader measured
multi-source architecture remains the intended direction. The next increment adds
the explicitly approved precipitation/wind/thunder policy in §6.7.8 under ruleset
`saved-active-fields-condition-preview.v3` and template `compositional-conditions-text.v1`.
Fog remains gated; later endpoint transitions and periods are described in §6.7.10–6.7.11.

The **read-only condition preview for one saved issued forecast ID** reuses the
existing verified storage reader and one pure cell/hour function over its saved local
grid, then extracts the center's 36 structured results and renders them. Current scope:
native active hourly PoP with exact threshold/window, active hourly liquid amount,
and separately qualified instantaneous p-type/state; numeric wind remains separately
available alongside the new presentation descriptors. Other components are unavailable/excluded
under the matrix above. Do not acquire data, fill missing attachments, add a new field,
promote a shadow, change p-type/blend rules or create new history rows/objects.

The implementation uses pure forecasting components/rules and an application
read/preview command using `ForecastIssuanceService.read`. Existing canonical
serialization is retained; there is no parallel forecast-history system or new
observation framework. The CLI is
`python -m mesoforge.application.weather_conditions --issued-forecast-id ID`.
Saved legacy point-only or missing-grid records
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

#### 6.7.8 Approved initial presentation policy

**`mesoforge-condition-wording.v1`** records the owner's September 16, 2026 approval
for deterministic precipitation, thunder and wind wording. The saved-grid preview
uses ruleset **`saved-active-fields-condition-preview.v3`** and template
**`compositional-conditions-text.v1`**. These are MesoForge presentation rules, not
NWS advisory/warning criteria, measured calibration or new blend policies. Preserve
the explicit policy values/identity in the result so a future revision is identifiable.
Behavioral validation cannot establish forecast skill; revisions should follow
verification and product-design evidence.

The numerical canvas, temporary NBM sky source and existing category mapping remain
unchanged. Use unrounded active values; never promote shadow/evidence-only inputs.
The same derivation serves every saved cell/hour and its center preview, with no
field recalculation, provider calls, new forecast history or storage writes.

**Applicability and native type are separate.** For matching valid one-hour QPF/PoP
events, active hourly PoP **<20%** plus active hourly QPF **exactly zero** makes
precipitation `not_applicable` for rendering. Preserve the original native type,
including unknown/ambiguous, and its instantaneous valid time separately. This is
an omission policy, not proof that all trace precipitation is absent. Positive
QPF **or** PoP **≥20%** makes precipitation relevant. A native p-type flag alone
cannot do so. Missing values never become zero, and incompatible windows cannot
establish joint applicability. Retain unavailable/reason states when support is
insufficient.

Relevance does not automatically authorize a phrase: positive QPF with PoP <20%
remains relevant but its precipitation text is omitted under the approved probability
band. Missing PoP cannot acquire a qualifier from deterministic QPF. Preserve these
tensions numerically rather than altering the saved fields or inventing confidence.

| Active hourly PoP | Qualifier |
|---|---|
| 0 ≤ p < 20% | Omit precipitation text |
| 20% ≤ p < 30% | Slight chance |
| 30% ≤ p < 60% | Chance |
| 60% ≤ p < 80% | Likely |
| 80% ≤ p ≤ 100% | Supported precipitation label without a probability qualifier |

Qualifiers apply only to the existing active native hourly event, with its exact
>0.254 kg/m² threshold and `(start,end]` bounds. Neither 3/6-hour shadows nor a
different threshold may fill a missing hour. When occurrence wording qualifies,
map known native endpoint rain → rain, snow → snow, freezing rain → freezing rain,
ice pellets → **sleet**, and supported multiple types → mixed precipitation.
Unknown, ambiguous and unavailable type all use generic **precipitation** in text,
but keep those three states distinct internally. Ambiguity does not become a known
mixed-type set. SWE, snowfall/Kuchera, FRZR and ice evidence cannot override the
active type policy. No light/moderate/heavy classes are enabled.

QPF amount, thresholded PoP and instantaneous endpoint type remain independent
structured components with independent evidence references. The compositional
phrase does not assert a calibrated type-specific probability or that the endpoint
type occurs throughout the accumulation period. Any future interval-wide type or
transition interpretation needs an additional approved rule.

| Active native hourly thunder probability | Wording |
|---|---|
| 0 ≤ p < 10% | Omit thunder text |
| 10% ≤ p < 30% | Thunder possible |
| 30% ≤ p < 60% | Chance of thunderstorms |
| 60% ≤ p ≤ 100% | Thunderstorms likely |

Retain the numeric probability, exact interval and unresolved native physical-event
threshold/footprint in the structured result, which records that the wording
describes native provider-event potential, not an exact-point deterministic
thunder/lightning claim. No longer-period substitution or CAPE/QPF/reflectivity-derived
probability is enabled. When both precipitation and thunder qualify, compose them
coherently without discarding either component or erasing freezing-rain/type uncertainty.

| Descriptor | Active sustained wind or active gust |
|---|---|
| Breezy | Sustained ≥15 mph **or** gust ≥25 mph |
| Windy | Sustained ≥25 mph **or** gust ≥35 mph |

**Windy takes precedence.** Test converted unrounded values; retain exact original
speed, direction, gust, units and timing. A valid threshold-crossing component can
support a descriptor independently; missing wind is not calm. This does not change
gust into sustained wind, create an averaging duration, or imply a hazard product.

The deterministic order is sky → precipitation applicability → PoP qualifier →
supported type → thunder → independent wind descriptor. Compose one phrase; do
not hard-code demonstration strings. A known sky leads and joins weather wording
with "with"; weather parts and the wind descriptor join with "and". When
precipitation or thunder wording is at least **likely** (≥60%), the sky words are
omitted from the text ("Rain likely", "Thunderstorms likely and windy") while the
known sky remains in the structured presentation with an explicit omission reason.
Type-scope and thunder-event caveats are structured fields, not text. Missing sky
does not block supported weather wording, and shadow cloud cannot repair it. Omit
any unsafe component from text while retaining its structured reason. If no
component supports text, return an explicit unavailable result. All five states
remain distinct: known, unknown, ambiguous, unavailable and not_applicable.

The saved-ID demonstration uses **`3bd4cada-d4c3-4f1e-94d8-2d5182c61991`**, including
all 36 center hours. Acceptance covers dry 00Z with preserved unknown native type,
positive-QPF/ambiguous 15Z, rain at 17Z, available wind/thunder threshold crossings
and final-hour missingness. Require byte-identical repeated output and unchanged
PostgreSQL rows/MinIO objects. Independently test each boundary, active/shadow gating,
composition, timing, missingness and unchanged input; reuse existing readback tests.
These are acceptance requirements, not a claim that an unexecuted check passed.
The September 16 real validation used a fresh issuance in a separate environment,
`9e989662-551e-4918-92d8-77005eb7e474`, because the earlier ID was unreadable there;
README records its byte-identical CLI/HTTP repeats, forbidden-hook replay, exact
readback, unchanged storage counts and the naturally occurring wording.

Fog/visibility wording, intensity and winter-amount delivery remain disabled.
Multi-hour evolution is now a separate read-only resource under §6.7.10; it claims
no exact transition minute, no type persistence across missing hours and no
precipitation-intensity change.

#### 6.7.10 Weather evolution and transitions

**Implemented September 16, 2026** as `mesoforge-transition-policy.v1`
(`forecasting/transitions.py`, `application/weather_transitions.py`,
`GET /issued-forecasts/{id}/conditions/transitions`). Input is the point-scoped
conditions preview of one exact issuance; the layer reads no grid cell and
recalculates nothing. Facts are structured and versioned, separate from
`transition-text.v1` rendering, and carry type, track, status, window, both endpoint
states, hour and evidence references, reasons and policy id.

- **Timing:** a change is known only within `(previous endpoint, next endpoint]`,
  matching the hourly interval closure; sub-hourly timing is never inferred.
- **Gaps:** an unavailable endpoint breaks its track; nothing is inferred across it
  and the gap is reported. Known/unknown/ambiguous/unavailable/not_applicable stay distinct.
- **Tracks:** applicability onset/ending (structured only), presentation-band
  wording onset/ending (rendered with the endpoint's type label), endpoint p-type
  changes on consecutive applicable hours (status known/ambiguous/unknown; only
  known→known rendered; unknown↔ambiguous is not an event; rain→ambiguous→snow stays
  two conservative facts), and sky trends on the ordered category scale requiring a
  two-category move that persists three hours, with the window starting at the last
  hour on the reference side.
- **Rendering:** "{Label} developing/ending between A and B", "{From} changing to
  {to} between A and B", "Becoming {category} between A and B", in the requested
  zone, else the issuance's saved report zone, else UTC. No LLM text.

Thunder and wind remain hourly-only; intensity, fog and narrative composition need
separate policies. Evidence-only fields never influence these facts.

#### 6.7.11 Period summaries

**Implemented September 16, 2026** as `mesoforge-period-summary.v1`
(`forecasting/periods.py`, `application/weather_periods.py`,
`GET /issued-forecasts/{id}/conditions/periods`), a presentation aggregation over
the §6.7.10 facts of one issuance. It derives no weather: no period maxima,
dominant categories, totals or representative conditions.

- **Boundaries:** local wall-clock 12-hour periods, day 06:00–18:00 and night
  18:00–06:00, left-closed/right-open, partial first/last periods allowed; UTC is
  used for membership and lengths so daylight-saving periods are 11 or 13 hours.
  A presentation convention, not a daylight definition.
- **Grouping:** a fact belongs to the period containing its window end; windows
  starting earlier are flagged and kept exactly. Omitted facts keep their
  transition reasons; gaps are listed per period and never bridged.
- **Labels:** natural local descriptors (late night 01–03, early morning 04–06,
  morning, afternoon, evening with early/late halves) only when every endpoint of
  the window is in one band; otherwise the explicit clock phrase.
- **Combination:** identical windows plus a listed rule (wording onset with
  increasing clouds; wording ending with clearing); otherwise separate sentences.

Limitations: no combination across different windows, no period-level condition
statements, and no thunder/wind evolution. Forward accumulation and read-only site
analysis now exist (§6.7.12–6.7.14); applied correction remains future work. The next
architecture increment is tracked in README rather than this historical milestone.

#### 6.7.9 Issuance payload measurement and proposed normalization

**Status: measured September 16, 2026; proposal only, no implementation approved.**
The 300.5 MB issuance `9e989662-…` (49 × 36 grid with PoP, p-type, cloud and thunder
attachments) contains about 3.5 MB of forecast values and 9 MB of extraction
geometry; roughly 95% of the grid is source provenance, GRIB keys, policy blocks,
documentation prose and timestamps copied into every cell-hour by
`build_local_surface_grid`, which deep-copies each column's fully annotated hours.
The same hour's 49 cells reference the same model messages, so those blocks are
identical across cells; storing each (path, hour, value) once needs 9.8 MB. Three
within-object duplicates add to it: `forecast.hours` repeats the center cell
(5.8 MB), `surface.fields` repeats the cloud/thunder/p-type guidance `field`
(22.9 MB), and thunder `comparisons` enumerate all 55 source pairs per cell-hour
although eight sources are unsupported (31.1 MB, three distinct variants). README
records the compression results (gzip-9 7×, zstd-19 127×) and projections.

Proposed, in order of safety (item 1 was implemented on September 16 without
changing any stored bytes or digests: point-scoped conditions with explicit
`editable`/`grid` scopes, and the metadata prefilter reported as `version_scan`;
per-hour prose hoisting was deferred; items 2 and 3 remain future work):

1. **Read-path fixes that change no stored bytes.** Default
   `GET /issued-forecasts/{id}/conditions` and the CLI to `scope=point`
   (center-point hours plus input, derivation, wording policy and a geometry
   summary; measured 0.71 MB instead of 35.7 MB), with explicit `scope=editable`
   (9 cells) and `scope=grid` (49 cells) opt-ins so the AI forecast desk and grid
   tools keep the full context. Hoist per-hour policy prose into one response-level
   `policies` map keyed by policy id and summarize the eleven always-unavailable
   placeholder components once per response. Let `select_hours`, window
   verification and the conditions route skip versions whose
   `target_reference_time` + 1..36 h cannot intersect the requested window before
   reading any object; this uses existing PostgreSQL columns only.
2. **Compression at rest** in the object store, keyed and verified by the digest
   of the canonical bytes (the body carries an encoding marker and readers verify
   after decoding). Existing uncompressed objects stay readable. This changes the
   storage adapter contract from ADR 0004 and needs owner approval; a filesystem
   or S3 service with transparent compression achieves the same with no code change.
3. **Normalized issuance layout, `issued-forecast.v2` / `local-surface-baseline.v3`.**
   Before serialization, intern every subtree that is identical across cells or
   hours — provenance, GRIB keys, inventory evidence, acquisition and source
   metadata, policy and event-definition blocks, documentation notes, comparisons —
   into a content-addressed `shared` table inside the same immutable object, and
   replace each copy with a short reference. A deterministic `inflate()` in the
   issuance reader reconstructs today's exact v2 shape, so `conditions.py`,
   `hourly_report.py`, verification and comparison code keep reading the inflated
   object unchanged; v2 records remain readable as-is. Expected size 12–20 MB raw and
   1.5–2.5 MB compressed, with every native evidence value, active/shadow role, hash,
   URL and cycle/lead retained and raw/prepared artifacts and replay untouched.
   Whether to keep the point column and the saved hourly report as copies, and
   whether to emit comparisons only for available sources, are owner decisions.

None of this is a prerequisite for the transition-detection milestone; item 1 should
precede sustained forward accumulation because read cost, not disk, is the first limit.

#### 6.7.12 Repeatable forward accumulation and read-only status

**Status: implemented September 17, 2026.** The learning loop's data source is the
existing forward run, made safe to call repeatedly by an external caller; no second
workflow, scheduler, VPS or CI deployment was added.

- **Registration is the coordinate list.** Each configured location is `lat`, `lon`,
  optional `name` and optional `display_timezone` (presentation only; it never
  selects data or changes values). No station IDs, grid cells or proxies.
- **One run, one decision window.** Per run: verify every eligible unverified hour of
  earlier versions through existing automatic verification (idempotent facts,
  unavailable/ineligible reasons kept in `previous-verification.json`), discover the
  current model set once, then issue immutable versions. Verification trouble never
  blocks issuance; one failed coordinate never stops the others.
- **Overlap protection.** The whole run holds one process-wide PostgreSQL session
  advisory lock (`pg_try_advisory_lock` on the key derived from
  `mesoforge.forward-run.v1`). A concurrent run fails immediately with
  `forward_run_overlap` (exit code 3) before creating its directory, verifying or
  issuing; it never waits. Connection loss releases the lock.
- **Decision-window guard.** After discovery, a coordinate that already holds a
  version for the discovered `target_reference_time` is reported
  `skipped_already_issued` from issuance metadata alone; `--reissue` adds a version
  deliberately. Only the remaining coordinates reach shared preparation
  (`issuance-locations.json`), so a fully covered retry downloads no guidance.
- **Queryable facts.** Saved verification facts now carry searchable attributes
  (`issued_forecast_id`, `valid_time`, `horizon_hours`, `latitude`, `longitude`,
  `verification_status`). They are output metadata only: not part of the idempotency
  digest or the fact payload, and facts persisted earlier would lack them.
- **Unverified means no saved fact.** `verify_previous` indexes the coordinate's
  saved facts from those attributes before any observation work and reports indexed
  hours as `already_existing`; only hours without a fact enter acquisition and
  verification. Fact identity still includes the observation revision, so without
  this index a wider later acquisition would add a second, equally valid fact for
  an already-verified hour. The index is a read; if it fails the run reports
  `saved_facts.status = unavailable` and falls back to idempotent re-verification.
- **Accumulation status** (`accumulation_status` CLI, `GET /accumulation-status`)
  counts, from issuance rows, fact attributes and retained observation-source
  attributes only (no payload reads, no writes): versions with earliest/latest
  issuance and target times; hours `verified`, `pending` (valid time + 15 min still
  future), `no_retained_observations` and `retained_observations_without_fact`;
  verified counts by lead bucket 1–6 / 7–18 / 19–36; and the newest station
  evidence. Unavailable/ineligible attempt reasons are not persisted, so the status
  distinguishes "no retained acquisition covers this hour" from "retained inputs but
  no fact" and points to the run reports for reasons. No weights, bias, regime or
  skill are derived; accumulating history is not learning corrections.
- **Boundary.** Manual repeated invocation is the orchestration today; a future
  external scheduler calls the same command and reads the same exit codes.

#### 6.7.13 Read-only site verification analysis

**Status: implemented September 17, 2026; temperature only; no correction is derived
or applied.** `site_verification_analysis` (CLI and `GET /verification-analysis`)
describes the verified history that forward accumulation produces. Its schema is
`mesoforge.site-verification-analysis.v1`.

- **A stored fact is evidence, not a statistical sample.** Canonicalization policy
  `mesoforge-verification-canonicalization.v1` resolves facts in two steps and never
  modifies or deletes one; every fact stays in the sample's provenance.
  1. *Opportunity* = (`issued_forecast_id`, `valid_time`, verification policy). Facts
     of one opportunity whose evidence signature is identical (forecast value and
     issuance digest, station, observation time and value, observation revision and
     logical-observation digests, matching policy, error) are one verification
     repeated over a re-acquired copy of the same observation revision; the earliest
     registered fact is canonical. Facts whose signatures differ (a revised
     observation, another station, another matching policy) make the opportunity
     **ambiguous**: it is excluded and reported with its reason, never resolved by rule.
  2. *Sample* = (coordinate, `target_reference_time`, `valid_time`, policy). Several
     issued versions of one target are one sample only when forecast value, selected
     observation revision and error are identical (a re-issue); versions that differ
     are ambiguous because no approved rule names the version that represents the
     decision window. Different targets verified at one valid time are separate
     samples at their own horizons, and the report counts the observations they share.
- **Usable fact:** verified status under `issued-temperature-verification.v1`, finite
  kelvin values, horizon equal to valid − target within 1..36 h, a saved error equal
  to forecast − observation, and agreement with the issuance metadata row. Facts
  without identity attributes are found by a bounded query (200) and identified from
  their immutable payload; nothing is backfilled.
- **Report:** inventory (indexed, legacy, usable, excluded by reason, ambiguous);
  overall and per lead bucket 1–6 / 7–18 / 19–36 N, bias, MAE, RMSE, min/max (empty
  buckets stay null, nothing is extrapolated); local day/night groups using the
  period-summary convention (06–18 / 18–06) in the requested, saved-report or UTC zone;
  per-station proxy accounting with distance, elevation and distinct observations;
  and regime *readiness*: which saved forecast dimensions (sky, wind, dew point/RH,
  precipitation, p-type, thunder, model spread) are reconstructable per sample, with
  availability and ranges only — no classes, thresholds or splits.
- **Evidence states.** Metrics are `descriptive_only` or `no_samples`.
  `correction_readiness` evaluates each lead bucket against the evidence policy of
  §6.7.14 (analysis policy `mesoforge-site-verification-analysis.v2`; v1 reported
  `evidence_policy: null` because no policy existed). It lists observed evidence and
  what it does not conclude, and never calculates a correction value.
- **Reads.** Issuance metadata rows and, per fact, either its compact analytical
  attributes or its immutable payload; values, observation identity and forecast
  context all live in the fact, so **no issued forecast object is read**.
- **Compact analytical attributes (implemented September 17, 2026).** A fact payload
  is 4–9 MB because it embeds the forecast context, so every newly saved fact also
  carries `attributes.analysis`, schema
  `mesoforge.verification-analytical-attributes.v1`: a pure projection of that payload
  holding identity (issued forecast ID and digest, coordinate, target, horizon, valid
  time, fact schema, verification policy, status), forecast and observed temperature,
  error and unit, observation identity (station, coordinates, distance, elevation,
  observation time and offset, revision, logical-observation and raw-record digests,
  acquisition artifacts), the matching-policy digest, cutoff, code commit, saved
  report zone, and a compact point context (ten saved surface values with units and
  the per-model temperatures). It holds no candidate list, provenance block, GRIB
  metadata, URL, policy prose or grid data, and measured 2.4–2.6 KB against
  4.2–9.3 MB payloads on 112 real facts (the Minneapolis analysis then opened 23
  payloads instead of 93, with identical results).
  - The payload stays the **authoritative, immutable evidence**; attributes are not
    part of the idempotency digest, so payload bytes and replay are unchanged.
  - The analysis prefers the block and **falls back to a bounded payload read** for
    facts without a usable one (facts saved earlier, unindexed legacy facts,
    unsupported or malformed blocks). Both paths run the same projection, so
    canonicalization and metrics are identical; the report counts facts and bytes per
    path and `--payload-only` audits that they agree. Nothing is migrated, rewritten
    or backfilled, and the canonicalization policy is unchanged.
  - The context snapshot is descriptive input for later regime analysis. It is not a
    learned model, a weight or a correction.

#### 6.7.14 Decision-window policy and bias evidence policy

**Status: owner-approved policies recorded September 17, 2026. No correction is
calculated or applied, no scheduler exists, and no stored issuance or fact is changed.**

**Decision-window policy `mesoforge-decision-window-policy.v1`.** The canonical
operational forecast of a scheduled decision window is the **first successful eligible
issuance** for that window (the primary version). An identical reissue is preserved and
collapses analytically. A materially different later reissue is preserved as an
alternate/reissue and never silently replaces the primary. Versions issued before this
policy carry no window identity or role; when the operational version cannot be
determined safely they stay ambiguous, and nothing is rewritten or relabelled.

- *Today:* a window is identified by coordinate and `target_reference_time`. The
  forward run's guard already refuses a second version for a window unless `--reissue`
  is given, so since that guard every ordinary issuance is the first of its window. The
  analysis collapses identical versions to the earliest issued and keeps differing
  versions ambiguous (§6.7.13). The only pre-guard pair in the retained history
  (`5bd637dd…`, `9e989662…`) is numerically identical.
- *Smallest future schema addition (not introduced now):* three nullable fields on the
  issuance header (`issued_forecasts` row and the saved metadata block):
  `decision_window_id` (stable identity of the scheduled window, independent of how the
  target hour is later derived), `issuance_role` (`primary` | `reissue`) and
  `reissue_of` (the primary's `issued_forecast_id`, only for a reissue), with a partial
  unique index allowing one `primary` per `decision_window_id`. Historical rows stay
  null, meaning "role not recorded". No migration is needed yet: no scheduler exists,
  the guard prevents accidental competing versions, and the role of every post-guard
  issuance remains derivable from `issued_at` order. Add the fields with scheduling.

**Evidence policy `mesoforge-bias-evidence-policy.v1`.** The minimum evidence before a
deterministic temperature-bias correction may even be *proposed for shadow evaluation*.
These are versioned initial governance thresholds, not a claim that they are
statistically sufficient.

- Canonical verified samples only (§6.7.13). Lead buckets 1–6, 7–18 and 19–36 h are
  evaluated **independently**; nothing is pooled or extrapolated between them.
- A bucket needs **at least 30 canonical samples** from **at least 10 distinct decision
  dates**. A decision date is the UTC date of `target_reference_time`, so the hourly
  windows of one evening count once.
- Evidence must span several forecast episodes rather than one contiguous weather
  event. No episode detection exists; the v1 proxy is the ten-date requirement (at
  least nine days) plus a concentration limit: **no decision date may supply more than
  25%** of the bucket's samples.
- The **uncertainty of the mean bias is always reported** when at least two decision
  dates exist. Hourly errors of one date are not independent, so the 95% interval is
  built from decision-date means (Student t, D − 1 degrees of freedom). Evidence is
  inconsistent, and no correction may be proposed, when that interval includes zero.
- The ten-date, 25% and 95% readings are the minimal operational proxies for the
  approved wording; the policy lists them separately from the owner-specified items.
- The analysis reports every criterion's required and observed value per bucket.
  `correction_readiness.status` is `insufficient_evidence` unless a bucket meets all
  four, and `candidate_correction` stays null: no correction value is calculated.
- No weather-regime thresholds exist yet.

**Correction lifecycle.** Meeting the evidence policy never activates a correction:
verified historical evidence → deterministic candidate correction → shadow correction
on future forecasts → identical-sample verification against the unchanged baseline →
human, versioned promotion decision only if improvement is demonstrated. Promotion must
weigh at least MAE and RMSE on identical samples, not mean bias alone. The later AI
forecast desk is evaluated against the bias-corrected baseline, not merely the raw blend.

**Current data (September 17, 2026).** Minneapolis has 65 canonical samples from one
roughly 19-hour weather episode, four decision windows on two decision dates, one
observation proxy (KMIC), and lead coverage dominated by hours 1–18 (N 24 / 39 / 2).
Under the policy: 1–6 fails sample count, dates and concentration; 7–18 has 39 samples
but two dates, 69% from one date and an interval of −2.4 to +5.2 K; 19–36 has two
samples. St. Paul (45 samples, one decision date, KSTP) fails every bucket. Both are
`insufficient_evidence`; the correct action is continued forward accumulation.

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

## 10. Historical private API proposal

These `/v1` routes are unimplemented design sketches, not current commands or a
commitment to a generic public API. Current development/readback endpoints are in
[README](../../README.md); configured-location forecasting is the canonical product.

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

## 13. Historical delivery slices and retained review principles

The original slice table below is a historical development plan. Current priorities
follow VISION and the owner's current task; it does not authorize registration or
public-product work. The scientific/replay review principles remain applicable.

| Estimated vertical slice | Class | Outcome |
|---|---|---|
| Shared cache + private one-off baseline | First release | Ingest once; multiple coordinates reuse; bounded honest response |
| Registered issuance/history | First release | Complete immutable snapshots and atomic current pointer |
| Observations/verification | First release | Revisioned observations and cutoff-correct explicit facts |
| Basic bounded evaluation | First release | On-demand operator metrics without lattice |
| Learning/bias/weights | Roadmap | Real history yields evaluated approved candidates |
| Bounded AI | Roadmap | Structured audited proposals cannot alter baseline |
| Configured-location delivery | Future | Separately approved delivery; generic public/account product excluded |

These are sensible review units, not a mandatory seven-PR sequence. Adjacent slices may be
combined/split for reviewability. The first slice has no dependency on learning, AI, email,
accounts, long-term retention, or public SLOs.

The full inspected canvas, including completed p-type and subsequent native evidence,
is inventoried in section 6.7. Its read-only saved-grid preview, bounded initial
wording policy, multi-hour transition detection and period summaries are implemented,
and the background guidance refresh is separated from forecast generation through
the latest-complete prepared snapshot (section 5.6). Further rules, field promotion,
dynamic blending and a coherence engine require separate approval.

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
7. Prepared-snapshot operation (section 5.6): refresh cadence on the VPS, retention
   of superseded snapshots, a shared pointer location beyond the local filesystem,
   whether the refresh may finish outside its decision hour, and whether an ad-hoc
   coordinate outside the refreshed footprint may trigger an offline regional rebuild.
   Resolved on 2026-09-18: absolute valid-time coverage with a 42-hour target window
   and the NBM still-publishing fallback.
8. Field-specific blend policies (section 5.5): the per-field mathematics, dynamic
   weight inputs and their evidence thresholds, and the order in which current
   scaffolding is replaced. No equation, multiplier or default is approved.
9. Separately later: correction methods/promotion, site-knowledge representation,
   bounded AI tool algorithms/validation and evaluation policies, and configured-location
   delivery. Generic public/account products are outside the vision; the long-term
   direction does not approve later implementation details.

## 18. Historical private-baseline exit criteria

This preserves the original proposed release checklist. It is not a claim that these
features exist or an acceptance gate for future baseline-snapshot work.

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
| Roadmap | Generalized blends and maintained baseline snapshots, site/regime correction, bounded AI tools and configured-location delivery; generic public accounts excluded |
| Guidance | Ingest once into shared source cache; derive local MesoForge fields using larger context and smaller editable domains, then interpolate the exact point |
| Forecast identity | The blend is the forecast: one coherent baseline grid from field-specific blend policies; models are contributors/evidence, preserved beside the blend, never a selected final forecast |
| Blend weights | Per-field policies with field-valid mathematics; dynamic inputs (lead, availability, freshness, verified skill, site, later regime) are conceptual; current fixed weights and single-source rules are scaffolding |
| AI edits | Persisted as bounded, interpretable edits to the MesoForge field after deterministic correction; contributors are cited evidence, not selections |
| Refresh versus request | Today refresh publishes prepared contributors and the consumer builds the grid; target background blending/coherence publishes an immutable MesoForge baseline pinned by each configured-location run. Scheduler controls timing, not science |
| Snapshot coverage | Usability is absolute valid-time coverage of R+1..R+36 for the active deterministic contributors (`mesoforge-prepared-coverage-policy.v1`, 42-hour target window, never forced); NBM-based products report their own coverage; the request hour is the reference hour |
| Cross-field coherence | Field-specific blends must stay mutually coherent (p-type/precipitation/thermal structure, thunder/convective support, gust/wind, RH/T/Td, later fog); snapshots keep every contributor field and its availability so a later coherence engine can evaluate them |
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
