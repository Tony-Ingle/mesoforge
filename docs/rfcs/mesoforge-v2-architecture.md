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

MesoForge V2 will be a selective rebuild from current `main`, not a merge-and-refactor of
the old Phase 3 branch. It will reuse scientific kernels and infrastructure whose contracts
remain valid, port cohesive donor algorithms with independent tests, and replace obsolete
Phase 3 product architecture.

The central data-flow decision is **ingest shared guidance once, derive forecasts for many
locations**. Background work acquires and normalizes each supported model cycle once into a
shared spatial cache. Forecast computation reads that cache for a coordinate; registration
never creates a location-specific guidance download or copy.

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

This RFC is the architecture gate. Implementation begins only after owner approval. Its
delivery sequence is a non-binding estimate, not a required PR count.

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

**Owner direction update, 2026-09-10:** the long-term model mix includes HRRR,
RAP, NAM 3 km, NAM, GFS, RRFS / REFS, and NBM, with useful deterministic and
ensemble guidance including GEFS, ECMWF, and Canadian models where appropriate.
These are planned integrations, not completed support or one implementation task.
Current Phase 2 supports HRRR/NBM/GFS; the separately approved localhost slice
uses only HRRR/GFS temperature, now approved through hours 1–36 with fixed
demonstration weights of 70/30. Phase 2's late-horizon defaults remain unchanged.

NAM/NAM 3 km are transition candidates. The September 9 NWS SCN 26-47/26-48
updates schedule NAM and its nests' retirement and RRFS/REFS replacement for
October 14, 2026 at 12:00 UTC, subject to weather-related delay. The source links
and verification date are in [VISION.md](../../VISION.md#long-term-model-direction).
Earlier donor descriptions and older NOAA target dates do not override those notices.

Retain raw model files/messages actually acquired, including currently unused
fields, separately from prepared subsets. Later field/product trimming requires
a separate decision. This direction does not authorize unbounded acquisition of
fields, levels, leads, or models, nor settle indefinite-retention guarantees.

- learned bias correction and model weighting;
- bounded structured AI proposals and deterministic approval policy;
- email or other delivery;
- public/multi-user access, accounts, ownership, privacy controls, sharing, export,
  deletion, billing, and public SLOs;
- broader geography, model mix, fields, horizons, retention guarantees, and analytics.

Roadmap components may consume first-release facts but cannot reshape or delay first-release
contracts. No first-release acceptance depends on simulated learning, AI, email, public
accounts, long-term retention approval, or public SLOs.

### 2.3 Coordinate-driven operating direction

**Owner direction update, 2026-09-10:** the intended user-facing geographic input is
only latitude/longitude. This operating direction is approved; its implementation
details and the rest of this RFC remain proposed unless separately approved. The
current implementation provides the localhost HRRR/GFS temperature demonstration
for hours 1–36 and local batch issuance with coordinate-derived shared preparation. It has no
registered-location lifecycle or VPS deployment.

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
expand spatial coverage using retained HRRR/GFS temperature messages. Both example
coordinates forecast successfully; the former fixed demonstration rectangle is retired.
New coordinates require no code changes. Owner-approved internal defaults are:

- Exact latitude/longitude is the forecast point.
- 50 km minimum model-data preparation buffer.
- 150 km surrounding weather-context footprint, currently temperature only.
- Existing 50 km observation-station search, with suitability and time/QC rules unchanged.

Preparation inspects the whole collection, derives conservative geographic envelopes,
merges overlapping footprints, reuses sufficient prepared guidance, and otherwise
prepares shared native-grid views from retained full messages. New source cycles use
existing bounded acquisition once before serving. Distant footprints can use separate
views of the same acquisition. Native-cell interpolation and raw/prepared checksums
remain intact. Context stops at physical model boundaries; unsupported coordinates
are invalid geographic input or points outside the native model domain. Insufficient
prepared coverage instead requires preparation outside HTTP (HTTP 409).

These defaults are internal and do not change the locations file. Dynamic
weather-dependent mesoscale/synoptic sizing is deferred. This implements coverage
preparation, not the combined operating lifecycle below. MesoForge derives location
identity, nearby observation candidates, bounding boxes, surrounding counties,
native-grid coordinates, and any spatial zone or surrounding context internally
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
- **Forecast:** deterministic MesoForge values with contributors, configuration, units,
  quality, missingness, cutoffs, and lineage.
- **Issued forecast snapshot:** one immutable persisted forecast version. Refresh creates a
  new version and never edits an earlier one.
- **Forecast state:** a small mutable pointer/status for the current issued snapshot; it
  contains no mutable forecast values.
- **Registered location:** a normalized scientific coordinate selected by an operator for
  recurring issuance, persistent history, observation matching, and verification.
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

- **Shared acquisition:** cache identity includes model/product/cycle/revision, field,
  lead/interval, grid/partition, transform, and content identity; never a location.
- **Coordinate identity:** EPSG:4326 decimal input is range-validated; `180` becomes
  `-180`; values use five decimal places, round-half-to-even, positive zero, JCS bytes, and
  a full SHA-256 key.
- **Coordinate/station separation:** a station never replaces the forecast coordinate.
  Distance, elevation, support decision, effective time, and station identity remain clear.
  Observation sources/proxies are selected automatically using suitability rules;
  users are not required to configure stations or context zones.
- **Immutable issuance:** published assets, lineage headers, snapshots, forecast facts,
  observation revisions, and verification facts are append-only. Mutable state is limited
  to leases and versioned pointers.
- **Correct time:** issue, reference, provider availability, ingestion, event, valid,
  interval-bound, verification, and query/build times are distinct UTC fields.
- **No leakage:** forecast inputs meet both availability and ingestion cutoffs. Observation
  selection meets verification and evaluation as-of cutoffs. Event time is insufficient.
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

Future:
learning    -> immutable forecast/verification query contracts
adjustments -> immutable forecast/evidence contracts + deterministic policy
delivery    -> immutable selected-forecast read model
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

Store native-grid spatial partitions with overlap sufficient for deterministic interpolation.
Manifests pin grid, coordinates, field/level, lead/interval, units, transform, bounds, parent
raw object, and digest. Publication is atomic after validation.

Partition shape, field grouping, compression, packaging, cache size, and retention are
**provisional hypotheses**. A `64 x 64` core plus one-cell overlap is a benchmark candidate,
not acceptance. Section 7 chooses initial packaging and measured work limits.

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
worker reuses the one-off cache/kernel in measured batches. A transaction inserts an
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

This future lifecycle composes the responsibilities above; it is not implemented
today. An explicitly configured collection may be processed by a GitHub Actions
caller one coordinate at a time or in bounded batches. For each location:

1. Validate and identify the location from latitude/longitude, deriving geographic
   metadata and any needed spatial context internally.
2. Verify eligible previous forecasts against suitable available observations,
   using the version originally issued and the applicable time, quality, spatial
   support, and cutoff rules. Record unavailable verification explicitly and
   proceed with the new forecast when no suitable observation is available.
3. Generate the new numerical forecast using ready shared guidance and the same
   extraction/blending functions used for one-off forecasts. No provider acquisition
   is triggered inside the forecast HTTP request.
4. In a later milestone, allow an AI adjustment/discussion stage using the numerical
   forecast, surrounding weather context, and prior verification. Keep the numerical
   baseline intact; any accepted adjustment must satisfy approved deterministic
   bounds and remain separately traceable. Missing verification is not invented history.
5. Save the immutable issued forecast and its provenance under the issuance rules.
6. Deliver the saved forecast once delivery is implemented, then continue to the
   next coordinate.

A location failure is recorded for that location and does not prevent the remaining
coordinates from being processed. Missing required model guidance keeps its explicit
forecast missingness; unavailable verification does not block a new forecast. One-off
API requests remain outside registration/tracking and this recurring lifecycle unless
the operator explicitly configures the location. AI and delivery are later roadmap
stages, not prerequisites for numerical issuance or first-release acceptance.

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

## 9. Retention capability levels

| Retained material and execution dependencies | Available capability | Lost on expiry |
|---|---|---|
| Issued snapshots; forecast, observation, and verification facts; lineage headers; named error/evaluation algorithms and compatible code/configuration | Serve issued history and recompute errors/evaluations under the retained semantics | Expired facts cannot be served or rescored; aggregates lacking their selected facts cannot be recomputed |
| Raw observation responses plus station metadata, lineage, exact normalizer/matcher code, configuration, and environment; rebuilding matches additionally requires the corresponding forecast facts, declared opportunities, pinned cutoffs, and support/matching policies | Renormalize observations; rebuild cutoff-correct matches only while the additional forecast/opportunity dependencies remain | Existing normalized facts remain usable, but source normalization and revision audit become impossible; matching also becomes impossible when either raw responses or its forecast/opportunity dependencies expire |
| Normalized guidance plus grid/coordinate metadata, lineage, extraction/blend algorithms, baseline configuration, compatible code, and environment | Reproduce a forecast without acquiring or decoding regional source data | Issued facts remain, but forecast reproduction from the normalized cache and unissued point extraction become impossible |
| Raw guidance/index bytes plus lineage, decoder/normalizer/extraction/blend code, configuration, dependencies, and environment | Full zero-network raw-to-cache-to-forecast replay | Shallower retained replay may remain, but full source replay becomes impossible |

Classes expire independently. Raw-guidance expiry does not invalidate issued history; cache
expiry does not erase it; raw-observation expiry does not rewrite normalized facts.

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
4. Packaging and measured input/work/output/concurrency/cancellation/timeout limits.
5. Retention costs/durations and advertised capability levels.
6. Private authentication/operator authorization.
7. Separately later: learning promotion, AI, delivery, public accounts/privacy/billing/SLOs.

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
| Roadmap | Learning/bias/weights, AI, email, and public accounts later |
| Guidance | Ingest once into shared cache; derive many |
| Issuance | Immutable snapshots/facts; mutable pointer/status only |
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
