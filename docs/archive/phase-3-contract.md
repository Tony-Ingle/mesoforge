> **HISTORICAL DOCUMENT — donor Phase 3 contract.** This preserves the old donor
> design and its scientific reasoning. Its singleton, phase, proof and instruction
> requirements do not govern current MesoForge. Use [ARCHITECTURE.md](../../ARCHITECTURE.md)
> for current technical structure and the active [retained contracts](../data-contracts/phase-2.md)
> only within their stated scope. The historical contents follow.

# Phase 3 coordinate-keyed verification contract

> **Historical reference (2026-09-09):** This is the preserved old Phase 3 contract,
> not an instruction to implement V2. Its authority statements below apply to that
> historical phase. The old implementation is a read-only donor; it is not the
> checked-out Phase 2 baseline. This path remains for existing links and technical
> comparison. See [VISION.md](../../VISION.md), the
> [proposed V2 RFC](mesoforge-v2-architecture-rfc.md), and the
> [archive index](README.md). Incompatible singleton, lattice, and proof
> requirements do not silently govern new V2 work; V2 choices still need owner approval.

This is the authoritative, implementation-enabling contract for Phase 3. The canonical
architecture remains [`docs/architecture/v1.md`](mesoforge-v1-architecture.md); where that
future-state document is broader, this contract governs Phase 3. The implemented
[Phase 2 contract](../data-contracts/phase-2.md) remains authoritative for inherited source, scientific,
blend, storage, and provenance behavior unless this document explicitly replaces a key.

Phase 3 establishes trustworthy identity and measurement for exactly one configured,
observation-backed forecast coordinate. It makes no forecast-skill, learned-correction,
publication, or product claim.

## Normative language and fixed boundary

`MUST`, `MUST NOT`, `SHOULD`, and `MAY` are normative. A missing required value,
unrecognized enum, extra schema field, digest disagreement, unsupported relationship,
or violated invariant fails closed; it is never repaired by an implicit default.

The production slice is fixed as follows:

| Dimension | Phase 3 value |
| --- | --- |
| Forecast locations | Exactly one: normalized `45.80265, -93.07956` in EPSG:4326 |
| Human label | Grasston, Minnesota; non-identifying and never part of scientific identity |
| Models | HRRR, NBM, GFS, in that order |
| Target horizons | Integer hours 1 through 36; bands h01-h18 and h19-h36 |
| Deterministic fields | 2 m temperature/dew point; earth-relative 10 m U/V; derived speed/direction; 10 m gust; one-hour liquid-equivalent QPF |
| Probability field | NBM one-hour PoP01 only, for `P(QPF > 0.254 kg m-2)` |
| Observation provider | AviationWeather METAR, selected under the contract below |
| Storage | PostgreSQL immutable manifests plus immutable S3-compatible objects; analytical rows MAY additionally use pinned Parquet artifacts |
| Correction | `IdentityBiasCorrection`, exactly zero and applied once |
| Output | Unpublished `complete`, `degraded`, or `invalid` forecast plus verification/evaluation artifacts |

The Phase 2 cycle selection, inventories, source products, units, native-grid validation,
pointwise physical conversions, interval semantics, approved fallback tables, blending,
coupled dew-point and wind/gust invariants, PoP/QPF treatment, and failure states are
reused unchanged. Forecast values are extracted at the requested forecast coordinate,
not at an observation station. Bilinear extraction remains
`bilinear-native-grid.v1`: four finite corners, no extrapolation, one-cell halo, and
weight sum tolerance `1e-12`.

Phase 3 does not include RRFS, precipitation type, snow, severe weather, new fields or
horizons, learned bias, learned weights, training, model promotion, AI-provider calls,
AI artifacts, publication, API/UI, imagery, subscriber accounts, dashboard/email,
billing, subscriptions, marketing, or Phase 4+ behavior. Arbitrary coordinates and
unobserved-location applicability are deferred.

## Location identity

### `location-snapshot.v1`

A location is one immutable canonical JSON artifact. Its validated payload has exactly
these fields:

```text
schema_version: literal "location-snapshot.v1"
location_id: LocationId
latitude: JSON number
longitude: JSON number
horizontal_crs: literal "EPSG:4326"
coordinate_decimal_places: literal 5
coordinate_source:
  kind: literal "operator_configured"
  source_reference: nonempty ASCII string, 1..256 characters
  captured_at: UTC instant
  supplied_latitude: decimal string
  supplied_longitude: decimal string
elevation:
  status: "known" or "unknown"
  metres_msl: JSON number or null
  vertical_datum: literal "NAVD88" or null
  source_kind: "usgs_3dep" or "operator_survey" or "unavailable"
  source_reference: nonempty ASCII string or null
  source_artifact_id: ArtifactId or null
  source_content_digest: sha256 digest or null
  sampled_at: UTC instant or null
  horizontal_resolution_m: positive JSON number or null
  vertical_accuracy_m: positive JSON number or null
timezone_name: IANA timezone string
privacy_class: literal "public-nonpersonal"
retention_class: literal "phase3-scientific-permanent"
```

Phase 3 uses `timezone_name="America/Chicago"`. Timezone is presentation metadata only.
It MUST NOT alter UTC storage, issue/cutoff selection, valid times, matching, regime
assignment, artifact identity other than the snapshot's own identity, or numerical
forecast values. There is no local-day product in Phase 3.

`privacy_class="public-nonpersonal"` means the coordinate is an owner-approved public
scientific test location and carries no account/person association.
`retention_class="phase3-scientific-permanent"` means the snapshot and every manifest
or evaluation record that references it are retained for the life of the Phase 3
evidence. This contract defines no customer privacy model, deletion workflow, consent,
or broader retention tiers.

### Normalization and identity algorithm

1. Parse supplied latitude/longitude as base-10 `Decimal`; exponent notation, NaN,
   Infinity, locale separators, signs on zero, and more than 12 fractional digits are
   rejected.
2. Require `-90 <= latitude <= 90` and `-180 <= longitude <= 180`. Values are not
   wrapped. The exact longitude `180` canonicalizes to `-180`.
3. Quantize both coordinates to five fractional decimal places with IEEE 754
   round-half-to-even. Canonical negative zero becomes positive zero.
4. Build the coordinate-key payload with exactly `schema_version="location-key.v1"`,
   normalized `latitude`, normalized `longitude`, `horizontal_crs`, and
   `coordinate_decimal_places`. Serialize it as RFC 8785/JCS JSON in UTF-8. JSON
   numbers are the quantized numeric values.
5. Let `H` be lowercase SHA-256 hex over the coordinate-key bytes. Set
   `location_id = "location.v1:" + H`.
6. Insert `location_id`, serialize the complete validated payload as JCS, and register
   those exact bytes. The artifact manifest's `content_digest` is the ordinary
   `sha256:<64 lowercase hex>` over the complete bytes and is distinct from `H`.

The full 256-bit location digest is never truncated. Recomputing either digest is
mandatory on every load. The same normalized coordinate key always produces the same
`location_id`; coordinate provenance, elevation evidence, timezone, classification, and
capture time version the immutable snapshot through its artifact ID/content digest
without fragmenting coordinate identity. Changing a coordinate-key field creates a new
`location_id`. Changing any other field creates a new snapshot artifact under the same
location ID. Mutation is forbidden, and one run pins exactly one snapshot artifact. A
display label or mutable alias MAY point to a location ID but MUST NOT be accepted at
scientific or replay boundaries.

`source_reference` for this slice is the repository-relative path and versioned config
key that supplied the coordinate. `captured_at` records when that source value was
accepted, not when the physical location came into existence.

### Elevation policy

Elevation is provenance, support evidence, and future applicability metadata; Phase 3
applies no lapse-rate, terrain, or elevation correction.

The preferred source is a point sample from USGS 3DEP at the normalized forecast
coordinate. The exact response bytes, endpoint/query, retrieval time, dataset/product
name and edition, NAVD88 vertical datum, and content digest MUST be
retained as a source artifact before a `known` elevation snapshot is registered. Only
an explicitly documented operator survey referenced to NAVD88, with its measurement
reference retained, may be used instead. No datum conversion occurs in Phase 3: a
source in another or unspecified datum is rejected. Station elevation, model-grid
terrain, geocoding, and an unversioned web value MUST NOT be used as fallback.

If neither approved source is available, the only fallback is the explicit tuple
`status="unknown"`, `metres_msl=null`, `source_kind="unavailable"`, and all other
elevation value/provenance fields, including `vertical_datum`, null. Forecast generation
may continue because no elevation correction is
performed, but observation support and verification MUST be `unsupported` and MUST NOT
emit scored pairs. The production acceptance location therefore requires a `known`
elevation before it can satisfy Phase 3 exit.

Known elevation requires finite `-500 <= metres_msl <= 9000`,
`vertical_datum="NAVD88"`, a non-null source
reference, source artifact ID/digest, sample time, horizontal resolution, and vertical
accuracy. Unknown elevation requires all value/provenance fields null. Partial tuples,
datum ambiguity, digest mismatch, or a sample taken at any coordinate other than the
normalized requested coordinate fail closed.

### Location invariants

Every coordinate-keyed forecast, contribution, correction, match, verification, corpus,
and evaluation artifact MUST carry `location_id` and the location-snapshot artifact ID
and content digest. The three must resolve to the same verified bytes. Dataset location
coordinates MUST equal the snapshot's normalized coordinates exactly. Exactly one
location is permitted; duplicate, missing, aliased, or additional coordinates fail.

An observation station ID is never a `location_id`. Its coordinates MUST NOT replace,
round, snap, or alias the forecast coordinate. A forecast dataset whose location axis
contains a station ID, or whose latitude/longitude equal station metadata but disagree
with the location snapshot, is invalid.

## Observation support, revisions, and matching

### Separate station identity and support decision

`observation-support.v1` is an immutable artifact keyed by location snapshot, station
snapshot, and `observation-support-policy.v1`. It records:

- forecast `location_id`, snapshot artifact ID/digest, latitude, longitude, and elevation;
- observation `station_id`, provider ICAO ID, station-snapshot artifact ID/digest,
  effective interval, latitude, longitude, elevation, exposure/instrument identities,
  and the retained station-response source artifact;
- geodesic library/version, ellipsoid `WGS84`, distance in metres, absolute elevation
  difference in metres, threshold values, and decision/reason;
- candidate station IDs and their computed distances in ascending `(distance,
  station_id)` order.

Candidate stations are exactly the three Phase 2 stations: `station.kcbg`,
`station.kjmr`, and `station.kros`. Select the nearest station by WGS84 ellipsoidal
inverse distance; an exact distance tie is broken by ascending station ID. The selected
station must have metadata effective at each observation event time, distance no more
than `30_000 m`, and absolute station-versus-forecast elevation difference no more than
`150 m`. Both elevations must be known. Phase 2 provider-metadata checks remain in force:
provider coordinates within `0.02 degree` and provider elevation within `30 m` of the
pinned station snapshot.

The selected production station is an output of that deterministic calculation, not a
hard-coded substitute for the forecast location. If no candidate passes, station
metadata changes outside tolerance, metadata is not effective at event time, or any
required provenance is unavailable, support is `unsupported`. The forecast remains at
the requested coordinate; verification rows are emitted as unscored support-missing
rows and no other station is silently tried after selection.

For one run, the selected station snapshot's effective interval MUST contain the full
possible matching interval from `first_valid_time - PT15M` through
`last_valid_time + PT15M`. A metadata boundary inside that interval invalidates the
run rather than mixing station identities. A multi-run corpus carries one independently
verified station/support snapshot pair per run; adjacent runs MAY reference different
immutable snapshots, but each matched row references exactly its run's pair.

Passing support means only that METAR is an allowed proxy under this bounded Phase 3
policy. It does not assert that station exposure is identical to the forecast coordinate
or establish skill at other coordinates.

### Time and revision cutoffs

All stored instants are UTC. Each forecast run pins:

- `forecast_issue_time`: the nominal MesoForge issue time;
- `information_cutoff`: the latest authoritative availability and local ingestion time
  allowed for forecast inputs; it MUST be no later than `forecast_issue_time`;
- for each target valid time, `verification_cutoff = valid_time + PT24H` exactly;
- `corpus_build_cutoff`: the UTC instant at which the immutable corpus was assembled;
  it MUST be at or after every included verification cutoff.

The next-day policy ID is `metar-next-day-asof-24h.v1`. A source forecast artifact is
eligible only when both authoritative `available_at` and local `ingested_at` are no
later than `information_cutoff`. An observation revision is eligible only when its
provider availability/receipt time and local ingestion time are both no later than the
row's `verification_cutoff` and no later than `corpus_build_cutoff`.

Provider event time is never used as availability. Missing provider availability or
local ingestion time makes a revision ineligible. A timestamp later discovered to be
incorrect creates a new append-only source/revision record and corpus; it does not
rewrite prior evidence.

### Closed observation candidate set

For each of the 36 valid times, the operational verifier performs one bounded
AviationWeather query for the selected station and inclusive event window
`valid_time +/- PT15M`. The query is launched no earlier than
`valid_time + PT23H45M` and must finish no later than that row's
`verification_cutoff`. The exact request, response bytes (including a successful empty
response), request start/completion, provider headers, retry attempts, and local
ingestion time are retained. Retries use the inherited bounded Phase 2 policy and do not
extend the cutoff.

`observation-candidate-set.v1` contains exactly 36 entries ordered by target horizon.
Each entry names its retained response artifact or an explicit terminal
`acquisition_failed` outcome and enumerates every canonical logical observation and
revision parsed from that response. Its watermark is the successful response completion
time, or the final failed-attempt time, and cannot exceed the row cutoff. Candidate-set
construction validates both directions: every parsed eligible revision appears once,
and every enumerated revision resolves to the response bytes. Matching receives this
candidate set, never an arbitrary caller list. Request and retry evidence for each entry
is canonically serialized in a retained `observation-acquisition-log.v1` artifact, so
failed attempts are replayable even when no response artifact exists.

This proves completeness relative to MesoForge's recorded next-day query, not universal
provider revision-history completeness. A failed query produces explicit observation
missingness and makes the evidence run degraded; it cannot be represented as a
successful empty response. Omitted response bytes, attempts, revisions, or watermark
invalidate the candidate set.

### Revision selection and field matching

Matching policy is `coordinate-metar-nearest-15m-nextday.v1`:

1. Start only from the selected station's retained, append-only METAR revisions.
2. Group by stable logical observation identity `(provider, station_id, event_time)`.
3. Within each group choose the latest eligible provider revision by provider
   availability time, then local ingestion time, then ascending revision content digest.
   A duplicate revision key with different canonical content fails closed.
4. Keep logical observations whose event time is within the inclusive interval
   `[valid_time - PT15M, valid_time + PT15M]`.
5. Choose the event with smallest absolute time delta, then earlier event time, then
   ascending logical-observation digest.
6. Normalize and score fields independently under inherited Phase 2 physical conversion,
   plausibility, QC, calm-wind, and interval rules.

Instantaneous temperature, dew point, U/V, speed, direction, and gust use the selected
event. Direction is scoreable only when forecast and observed speed are each at least
`1.5 m/s`. One-hour observed QPF is scoreable only when the retained report explicitly
represents the exact forecast interval `(valid_time - PT1H, valid_time]`; point reports,
traces, unknown intervals, or accumulated amounts over another window are missing, not
zero. The binary PoP observation is derived only from a scoreable exact-interval QPF:
`1` when QPF is strictly greater than `0.254 kg m-2`, otherwise `0`. No observation
probability is fabricated.

Each `(run, location_id, target_horizon, forecast_layer, variable)` opportunity emits
exactly one row with four independent, exhaustive axes:

- `forecast_status`: `available`, `source_unavailable`, `scientific_invalid`, or
  `abstained`;
- `observation_status`: `eligible`, `missing`, `qc_rejected`, `interval_mismatch`,
  `support_unsupported`, or `acquisition_failed`;
- `pair_status`: `matched` only when forecast is `available` and observation is
  `eligible`, otherwise `unmatched`;
- `score_status`: `scored`, `calm_excluded`, or `unscored`. It is `scored` for a
  matched non-direction row; a matched direction row is `calm_excluded` when either
  speed is below `1.5 m/s`, otherwise `scored`; every unmatched row is `unscored`.

Phase 3 identity correction cannot independently abstain, but the enum is present so
coverage remains explicit for future layers. A row preserves both cutoffs;
forecast/station/location snapshot identities; forecast,
observation, logical-observation, selected-revision, raw-response, and matching-policy
artifact IDs/digests where applicable; event delta; QC; availability state; and a
machine-readable reason for each non-available/non-eligible axis. Forecast and
observation values needed for audit MAY be retained when the other axis prevents a
pair, but error/score is null for every `unscored` pair. Missingness is never imputed,
converted to zero, or removed from denominators.

### Next-day denominators

For every run and each existing Phase 2 variable, report counts by forecast layer,
location, exact lead, lead band, UTC meteorological season, regime, and source-
availability state. For each stratum:

```text
expected = count(all opportunity rows)
forecast_available = count(forecast_status == available)
observation_eligible = count(observation_status == eligible)
matched = count(pair_status == matched)
scored = count(score_status == scored)
forecast_coverage = forecast_available / expected
observation_coverage = observation_eligible / expected
paired_coverage = matched / expected
scored_coverage = scored / expected
conditional_pair_coverage = matched / observation_eligible
```

A zero denominator yields JSON `null` plus an explicit reason. Counts are integers;
each forecast-status total and observation-status total independently equals `expected`,
as do the pair-status and score-status totals; `scored <= matched`, and `matched` cannot
exceed either available total. The report includes the full forecast-status by
observation-status cross-tab so simultaneous gaps are not hidden.
PoP and QPF have separate denominators even though PoP truth is
derived from QPF. Direction calm exclusion is only a `score_status`; forecast and
observation status/coverage remain independent and the companion speed/U/V opportunities
remain independently scoreable.

## Immutable as-of corpus and zero-network replay

### Corpus specification and roots

The initial exit corpus uses `phase3-evidence-window.v1`: exactly one operational run.
Its whole-hour UTC issue time has hour `00`, `06`, `12`, or `18` and is written into the
configuration snapshot and registered before any provider discovery for that issue.
The singleton has `issue_start == issue_end == forecast_issue_time`; no opportunistic
second run may be added. This deliberately validates protocol and replay mechanics while
leaving every multi-day score/uncertainty that lacks its minimum explicitly
`insufficient_sample`. A later corpus may use another predeclared bounded range, but it
creates a new spec/manifest and makes no Phase 3 skill claim.

One `phase3-asof-corpus-spec.v1` artifact pins that window, exactly one location
snapshot, one station/support snapshot pair per run, target horizons, fields, forecast
layers, information and verification cutoff policies, observation revision/candidate-
set policy, matching policy, metric/evaluation protocol, configuration snapshot/digest,
code revision, environment digest, lockfile digest, random seed, and
`corpus_build_cutoff`. It contains no artifact-root list and never references an output
derived from itself.

For each included run, `phase3-run-spec.v1` pins all inherited Phase 2 discovery and
acquisition evidence plus the coordinate identity. A separate
`phase3-asof-corpus-manifest.v1` consumes the corpus spec and lists complete input roots,
ordered by `(forecast_issue_time, role, model, source_reference_time, target_horizon,
artifact_id)`. It never lists itself or downstream evaluation/replay/resource evidence.
Its roots include:

1. the corpus spec and every run spec;
2. location snapshot and its elevation source artifact;
3. each run's station-response source, station snapshot, and observation-support
   decision;
4. every selected guidance index and exact GRIB message artifact, including all GFS QPF
   parents and variable-lineage artifacts;
5. uncorrected blend, identity correction, final baseline, contribution, availability,
   and extraction artifacts;
6. every observation candidate-set, query/attempt, METAR response, and parsed revision,
   including eligible and rejected revisions needed to prove selection;
7. matched-pair, denominator, and verification artifacts;
8. all referenced policy/configuration snapshots.

The evaluation activity consumes the verified corpus manifest and emits the evaluation
report. A final `phase3-evidence-bundle.v1` manifest references the corpus spec, corpus
manifest, evaluation report, replay report, and resource report; it does not include
itself. Thus every content-addressed edge is acyclic:
`spec -> input artifacts -> corpus manifest -> evaluation/replay/resource reports ->
evidence bundle`.

Every corpus-root entry carries exact role, artifact ID, artifact type/schema, content digest,
availability authority/method, authoritative `available_at`, local `ingested_at`, and
run ownership. Missing timestamps, roles, roots, or bidirectional membership; extra
roots; same-role substitutions; digest/type/schema disagreement; or a transitive input
not reachable from the declared roots invalidates the corpus.

### As-of resolution

The as-of resolver receives only the verified corpus spec and persisted manifests. It
MUST NOT perform provider discovery, network access, current catalog/config lookup, or
filesystem fallback. It selects forecast inputs using the historical information
cutoff and observations using each historical verification cutoff. Derived artifact
availability is the maximum of all transitive input availability, activity completion,
and registration transaction time.

A historically missing source, field, revision, station snapshot, or timestamp produces
an explicit missing/ineligible row. Present-day data, a newer provider revision, a
nearby cycle, another station, and mutable live configuration MUST NOT fill the gap.
The corpus records the gap permanently for that corpus version.

### Replay guarantees

`phase3-zero-network-replay.v1` starts in a fresh process with an unavailable provider,
an HTTP transport that raises on every request, socket creation disabled, and no live
Phase 3 configuration object. It loads every root through checksum-verifying object
reads and recomputes location, configuration, content, parameter, and idempotency
digests before executing the production activity graph.

Replay MUST return the original artifact IDs and content digests when idempotency reuses
persisted outputs. A forced scientific re-execution used by tests MUST satisfy:

- canonical arrays: identical dimensions, coordinate order/dtypes/values, variable
  dtypes/values, masks, attributes, and location identity; NaNs compare by mask;
- JCS JSON and canonical Parquet artifacts: byte-identical payload and content digest;
- activities: identical ordered role/artifact inputs, parameter/configuration/code/
  environment identities, output schema, and idempotency digest;
- reports: identical rows, sort order, statuses, counts, null reasons, metric values,
  uncertainty values, and random-seed use.

Original retained bytes always remain available by artifact replay. A normalized array
encoding that is not contractually canonical need not regenerate byte-identically, but
its original content digest MUST remain bound to the retained bytes and the forced
re-execution MUST pass logical equality. Replay never replaces the historical artifact
with regenerated bytes.

Any attempted network call, root mismatch, digest mismatch, unsupported schema,
late-as-of input, non-deterministic ordering, or logical/report mismatch is a terminal
replay failure.

## Predeclared evaluation protocol

The immutable policy ID is `phase3-coordinate-evaluation.v1`. Registration of the
policy precedes opening any issue time included in its report. This is protocol
validation for a one-coordinate foundation, not training or promotion.

### Forecast layers and pairing

The ordered layers are:

1. raw `HRRR`, `NBM`, and `GFS` contributors when that contributor supplied the exact
   variable/location/lead;
2. `uncorrected_blend`;
3. `identity_corrected_baseline`.

All comparisons are paired on the same run, location, lead, matched observation
revision, matching policy, information cutoff, and source-availability state. Raw
layers retain their own availability; unavailable raw contributors are coverage
outcomes, not zero-valued forecasts. PoP has only the raw NBM contributor. The identity
baseline MUST have exactly the same values, availability, coverage, and metrics as the
uncorrected blend; any difference fails Phase 3.

Every row is keyed by variable, layer, and the single location. The aggregation lattice
is the Cartesian product of four dimensions with explicit rollups:

- `lead_group`: `all`, `h01-h18`, `h19-h36`, or exactly one of `h01` through `h36`;
  an opportunity contributes to `all`, its one band, and its one exact lead, never to a
  redundant exact-lead x band combination;
- `season`: `all`, `DJF`, `MAM`, `JJA`, or `SON` by UTC valid-time month;
- `regime`: `all`, `freezing`, `calm`, `windy`, `wet`, or `heavy_qpf`;
- `source_availability`: `all` or exactly one of `none`, `hrrr`, `nbm`, `gfs`,
  `hrrr_nbm`, `hrrr_gfs`, `nbm_gfs`, `hrrr_nbm_gfs`. The non-`all` value is the
  ordered set of models eligible for that variable/location/lead before layer selection;
  it is shared by every layer row for that opportunity. PoP therefore uses `nbm` when
  available and `none` otherwise.

The report materializes every lattice key in canonical order, including zero-count
cells. These regimes are non-exclusive and observation-defined:

- `all`;
- `freezing`: observed temperature `<= 273.15 K`;
- `calm`: observed wind speed `< 1.5 m/s`;
- `windy`: observed speed `>= 10 m/s` or gust `>= 15 m/s`;
- `wet`: exact-interval observed QPF `> 0.254 kg m-2`;
- `heavy_qpf`: exact-interval observed QPF `>= 2.54 kg m-2`.

Regimes are contextual labels applicable to every variable and used only after
observation matching. They MUST NOT enter a forecast, source selection, correction, or
availability decision. For each non-`all` regime the report separately counts
`regime_true`, `regime_false`, and `regime_unknown`; only `regime_true` enters that
regime's scores. `regime_unknown` means the observation needed to define the regime was
not eligible and is never treated as false. Zero-count and unknown cells are not omitted.

### Metrics

Errors are forecast minus observation and use float64 accumulation:

| Variable family | Primary metric | Required secondary metrics |
| --- | --- | --- |
| Temperature, dew point | MAE (K) | bias, RMSE |
| U, V, wind speed, gust | MAE (m/s) | bias, RMSE |
| Wind direction | mean absolute circular error (degree) | median absolute circular error |
| QPF | MAE (kg m-2) | bias, RMSE; CSI, POD, FAR, frequency bias, ETS at 0.254 and 2.54 kg m-2 |
| PoP01 | Brier score | reliability by fixed deciles `[0,.1), ... [.9,1]`; observed frequency/count per bin; Brier reliability and resolution components |

Metrics are reported separately for every layer and stratum. Undefined divisions and
empty scores serialize as JSON `null` with a reason; NaN and Infinity are forbidden.
Only an explicit `all` lattice value may pool its dimension. Such a rollup still keeps
variable, unit, location, and forecast layer separate and records counts for every
component band/season/regime/availability state; no other cross-cell pooling is allowed.

For finite errors `e_i = forecast_i - observed_i`, bias is `sum(e_i)/N`, MAE is
`sum(abs(e_i))/N`, and RMSE is `sqrt(sum(e_i^2)/N)`. Circular error is
`abs(((forecast-observed+180) mod 360)-180)`; report its arithmetic mean and
median after ascending sort (middle value for odd `N`, arithmetic mean of the two
middle values for even `N`).

QPF contingency events use `forecast >= threshold` and `observed >= threshold`. With
hits `H`, misses `M`, false alarms `F`, correct negatives `C`, and
`N=H+M+F+C`: `CSI=H/(H+M+F)`, `POD=H/(H+M)`, `FAR=F/(H+F)`, frequency bias
`=(H+F)/(H+M)`, random hits `Hr=(H+F)*(H+M)/N`, and
`ETS=(H-Hr)/(H+M+F-Hr)`. Counts are always present; each zero denominator yields null
and `zero_denominator:<metric>`.

For PoP, binary outcome is `1` only when exact-interval observed QPF is strictly greater
than `0.254 kg m-2`, matching the source probability event, otherwise `0`. Brier score
is `sum((p-o)^2)/N`. Decile bins are `[0,.1), ... [.8,.9), [.9,1]`; each reports count,
mean forecast `pbar_k`, and observed frequency `obar_k`. With overall `obar`, Brier
reliability is `sum(n_k/N*(pbar_k-obar_k)^2)`, resolution is
`sum(n_k/N*(obar_k-obar)^2)`, and uncertainty is `obar*(1-obar)`. Decomposition is null
with `single_class` unless both outcomes occur.

### Minima and uncertainty

These are reportability minima, deliberately not Phase 4 promotion thresholds:

- continuous/circular score: at least 30 scored pairs;
- paired layer difference: at least 30 common pairs from at least 10 distinct UTC issue
  dates;
- each contingency score: at least 10 observed events and 10 observed non-events;
- PoP reliability decomposition: at least 100 pairs overall; each decile still reports
  its actual count and observed frequency, with frequency null for an empty bin;
- regime/season/availability subgroup comparison: at least 30 common pairs from at
  least 10 distinct issue dates.

Below a minimum, counts and coverage are still emitted but score, comparison, or
uncertainty status is `insufficient_sample` with null value. Samples are never borrowed
from another stratum.

The ordered paired comparisons are each available raw contributor versus
`uncorrected_blend`, followed by `identity_corrected_baseline` versus
`uncorrected_blend`. Difference is always first-layer primary metric minus second-layer
primary metric; negative is better for the declared primary metrics. Comparison rows
name both artifact IDs and contain only their common matched population.
For direction, "common" means `score_status="scored"` in both layers; calm-excluded
pairs never enter a metric or paired difference.

Uncertainty for eligible paired differences uses this exact paired moving-block
bootstrap over sorted distinct UTC forecast issue dates. Let `D` be the date count and
`L=min(7,D)`. For each stratum derive a 128-bit seed from the first 16 bytes of
`SHA-256(UTF8("20260903|") + JCS(stratum-key))` and initialize NumPy `PCG64` with that
unsigned big-endian integer. For each of `2,000` replicates, draw `ceil(D/L)` start
indices independently and uniformly from `[0,D)`. Each block is `L` adjacent indices
in the sorted observed-date sequence with circular wrap; concatenate blocks and truncate
to the first `D` indices. Duplicate selected dates duplicate all paired rows for that
date; calendar gaps are not synthesized. Compute the paired primary-metric difference
for each replicate in canonical row order. The interval is NumPy `quantile` at
`0.025` and `0.975` with `method="linear"`.

Fewer than 10 distinct issue dates yields `insufficient_sample`; a degenerate interval
is valid and reported. Point estimates are computed from the original pairs, not
bootstrap means. NumPy version is pinned by the environment/lockfile, and the report
records the derived seed and algorithm parameters.

### Vetoes, tails, calibration, and coverage

Phase 3 has no challenger eligible for operational skill promotion. The report MUST set
`promotion_eligibility="not_eligible_phase3_identity_only"`; it MUST NOT label any raw
model, blend, or identity baseline as improved, promoted, or skilled. Tony's Phase 3
approval accepts contract/evidence integrity only.

The evaluation artifact is invalid, rather than merely degraded, if any of these
protocol vetoes occurs:

- identity-baseline values, paired population, score, or coverage differ from the
  uncorrected blend;
- a predeclared subgroup, tail regime, source-availability state, denominator, count,
  null reason, or applicable metric is omitted;
- an unavailable prediction or missing/rejected observation is dropped from its
  opportunity denominator or imputed;
- a metric is reported below its minimum without `insufficient_sample`;
- an eligible uncertainty interval is absent, or an ineligible one is fabricated;
- PoP values fall outside `[0,1]`, calibration bins are changed after registration,
  calibration is pooled with QPF, or exact-interval binary truth is unavailable;
- a tail (`windy` or `heavy_qpf`) result is hidden by an aggregate result;
- any input or selected observation revision violates its as-of cutoff;
- unpaired aggregate scores are presented as layer improvement.

For completeness, a future non-identity challenger protocol MUST define owner-approved
non-inferiority margins, improvement thresholds, rollback thresholds, untouched windows,
and spatial/event holdouts before data are opened. Those thresholds are intentionally
not invented in Phase 3 and cannot be inferred from this report.

For every layer, coverage uses the full expected opportunity population:

```text
forecast_opportunities = runs * 1 location * 36 leads * applicable variables
forecast_coverage = finite eligible predictions / forecast_opportunities
observation_coverage = eligible observations / forecast_opportunities
paired_coverage = matched pairs / forecast_opportunities
scored_coverage = scored pairs / forecast_opportunities
abstentions = explicit layer decisions not to emit despite otherwise eligible inputs
source_unavailable = opportunities lacking that layer's required contributor(s)
invalid = opportunities rejected by scientific or contract validation
```

Identity correction never abstains independently: its coverage and statuses equal the
uncorrected blend. Raw source absence is `source_unavailable`, not abstention.
Phase 3's identity correction has `abstentions=0`; any nonzero value fails. Counts must
sum exactly, including zero-count categories.

## Shared-host resource and admission contract

The representative workload is one 36-hour operational forecast followed by its
next-day observation acquisition/matching/verification and a zero-network replay of the
resulting as-of corpus. It runs on the shared approximately 8 GB host.

`phase3-resource-policy.v1` fixes these controls:

- exactly one forecast location and one forecast issue time per operational job;
- acquisition handles one model/source lead at a time. A memory-heavy decode/normalize
  unit is exactly one `(model, source lead, field group)`, where a field group is one
  scalar field, the coupled U/V/gust tuple, or the GFS QPF parent pair required for one
  output interval. No units or model decodes run concurrently, and no full-domain or
  national array is retained;
- retained native fields use `coordinate-halo-subset.v1`: exactly the 2 x 2 bilinear
  interpolation corners plus one complete exterior source cell on each side, yielding
  a 4 x 4 native-index window. A coordinate too near a source-grid edge to supply that
  window fails rather than clipping. Decoded full source arrays, where the inherited
  decoder requires them, are released before the next heavy unit and are never
  accumulated across leads;
- matching processes one run and at most 36 target rows at a time;
- corpus/evaluation scans at most one issue time or 10,000 analytical rows per batch,
  whichever is smaller, and writes incremental immutable partitions; no whole-corpus
  in-memory join or dataframe is allowed;
- exactly one memory-heavy job (`decode`, corpus replay/backtest, future training, or
  rendering) may hold the host-wide admission lease; the lease identity and lifetime
  are telemetry. Observation parsing and report writing may run concurrently only if
  they are measured non-heavy and the memory floor remains enforceable;
- operational forecast work has strict priority. New replay/verification work is not
  admitted while forecast work is queued; an admitted lower-priority job yields after
  its current bounded unit when forecast work arrives;
- before the first operational run, calibrate with the same code/environment/lockfile
  and a contract fixture that exercises each unit's maximum array shape. Calibration
  obtains the exclusive lease and is admitted without a prior estimate only when
  `MemAvailable >= 2 GiB`; it runs exactly five repetitions and obeys the same one-second
  floor abort. For each unit/repetition, incremental demand is
  `max(0, MemAvailable_immediately_before_unit - minimum_MemAvailable_during_unit)`.
  `measured_p95_incremental_demand` is NumPy quantile `0.95`, `method="linear"`, over
  the five demands for each exact `(model, field group)` unit type. Retain inputs,
  samples, and per-type estimates as an immutable artifact;
- after calibration, admit a heavy unit only when
  `MemAvailable >= measured_p95_incremental_demand + 512 MiB`. An absent/stale estimate
  or `MemAvailable < 1 GiB` queues/refuses non-calibration work. Estimates apply only to
  the same unit type and code/environment/lockfile digests and become stale when any
  digest changes;
- during every heavy unit, sample at least once per second. Abort before starting the
  next unit if `MemAvailable < 512 MiB`. Crossing the floor at any sample fails exit
  evidence even if the job later succeeds;
- retries are bounded by inherited source policy and must reacquire the lease; they may
  not increase batch/window/concurrency bounds.

Telemetry is an immutable `phase3-resource-report.v1` artifact containing host physical
memory, code/environment/lockfile/configuration digests, workload/run/corpus IDs, stage
and bounded-unit IDs, lease acquire/release times, queue priority/waits/refusals, UTC
sample time, `MemAvailable`, process RSS and high-water RSS, cgroup memory current/peak
and `memory.events` when available, swap total/free, batch/window/chunk size, and exit
time. The exit report states the minimum observed `MemAvailable` and baseline/maximum
swap used. OOM evidence is the unit's cgroup v2 `memory.events` counters when exposed,
otherwise the host `/proc/vmstat` `oom_kill` counter sampled before and after the
exclusive representative workload; at least one source is mandatory.

Acceptance requires minimum `MemAvailable >= 512 MiB` at every sample, maximum swap used
no greater than baseline swap used (zero KiB growth), no process OOM/kill, no increment
in the selected OOM counters, one-heavy-job lease exclusivity, and successful workload
completion. Missing telemetry or unavailable OOM evidence is an
exit-evidence failure, not an assumed pass. This contract does not authorize a larger
host, horizontal workers, or relaxed floor.

## Artifact graph

The Phase 3 production graph preserves Phase 2's stage boundaries:

```text
location source + elevation source -> location-snapshot.v1
station response -> station snapshot
location snapshot + station snapshot -> observation-support.v1
phase3 run spec + exact guidance roots -> coordinate extraction
  -> raw coordinate contributors -> availability -> uncorrected blend
  -> identity correction -> identity-corrected baseline
scheduled query attempts/responses -> closed observation candidate set
candidate set + station snapshot + row cutoffs -> as-of observation selection
baseline layers + support + selected observations -> exhaustive matched-pair rows
matched-pair rows -> denominators + verification report
predeclared corpus spec + ordered input roots -> acyclic corpus manifest
corpus manifest -> evaluation report + zero-network replay report
corpus/evaluation/replay/resource reports -> evidence bundle
```

Every arrow is a named, versioned activity with exact ordered input roles. Location and
station identities remain separate at every boundary. Forecast truth arriving after
issuance is downstream verification input and never retroactively becomes a forecast
run input.

## Acceptance tests

Implementation is not complete until deterministic tests prove all of the following:

1. Exact coordinate normalization vectors cover half-even ties, `180 -> -180`, negative
   zero, range edges, excessive precision, exponent/NaN/Infinity rejection, JCS bytes,
   full location ID, and independent artifact content digest.
2. Coordinate-key mutation changes location identity; metadata mutation preserves
   location ID but creates a new snapshot artifact/content digest. Extra/missing fields,
   digest mismatch, alias use, mutable update, and dataset-coordinate mismatch fail.
3. Known USGS/survey elevation requires complete pinned provenance; unknown elevation
   uses the exact null tuple, applies no correction, and makes support unscoreable.
4. Timezone changes presentation only; UTC numerical arrays, selection, matching,
   regimes, and metrics remain identical.
5. Privacy/retention enums accept only the fixed Phase 3 values and no account/person
   field can enter the snapshot.
6. Forecast artifacts contain exactly one `location_id`, never a station ID, and produce
   inherited Phase 2 fields/horizons/equations at the requested coordinate.
7. WGS84 support distance, deterministic tie break, `30 km` and `150 m` inclusive
   boundaries, effective station metadata, provider coordinate/elevation tolerances,
   and every unsupported reason are tested on both sides of each threshold.
8. Replacing the requested coordinate with selected station coordinates, even while all
   values remain otherwise plausible, fails lineage and dataset validation.
9. Candidate-set fixtures prove exactly 36 bounded query entries, request/response/
   attempt/watermark retention, bidirectional revision completeness, failed-versus-empty
   distinction, both cutoff predicates, latest-as-of selection, duplicate conflict
   failure, stable event selection, and that a newer present-day revision is ignored.
10. Matching covers inclusive `+/-15 minute` edges and deterministic ties; temperature,
    dew point, wind/gust, calm direction, exact one-hour QPF interval, and PoP truth obey
    inherited semantics.
11. Every layer/field/lead emits exactly one status on each independent axis and all
    cross-tabs and next-day denominators
    reconcile, including wholly missing observations, unsupported station, unavailable
    raw contributor, calm direction, and zero denominators.
12. Corpus validation proves the singleton evidence window and acyclic spec/input-
    manifest/evaluation/evidence-bundle graph, and rejects missing/extra/substituted
    roots, wrong roles/types/schemas, late availability/ingestion, incomplete transitive
    lineage, digest corruption, and mutable live configuration.
13. A missing historical source remains explicit when a usable present-day source is
    available; no current fill, nearby cycle, or alternate station is consulted.
14. Fresh-process replay has raising provider/HTTP/socket adapters, makes zero network
    calls, returns original artifact IDs/digests, and passes forced logical/byte equality
    for the declared artifact classes.
15. Evaluation emits raw contributors, uncorrected blend, and identity baseline for the
    exact aggregation lattice, deterministic ordering, zero-count and regime-unknown
    cells, explicit insufficient-sample rows, and no NaN/Infinity.
16. Metric fixtures cover formulas, QPF thresholds, fixed PoP calibration bins,
    minima boundaries, paired populations, and the seeded 2,000-replicate date-block
    bootstrap.
17. Identity correction is all-zero, applied once, has zero abstentions, and is exactly
    equal to uncorrected blend in values, statuses, pairs, coverage, and metrics; each
    deliberate discrepancy triggers a protocol veto.
18. Leakage tests place source and observation revisions one unit before, on, and after
    each cutoff and prove only `<= cutoff` inputs are selected; regime labels never
    appear in forecast-stage inputs.
19. Protocol-veto tests prove aggregate scores cannot hide omitted subgroup/tail/
    availability rows, calibration, denominators, or below-minimum null reasons, and
    every report declares Phase 3 promotion ineligible.
20. Admission tests prove five-run bootstrap calibration, incremental-demand p95 and
    floor calculation, stale-estimate refusal, forecast priority/yielding, one-heavy-job
    exclusion, bounded units/batches, floor abort, bounded retry, and telemetry
    completeness.
21. A representative operational plus next-day plus replay acceptance run completes
    under real PostgreSQL/S3-compatible storage with every one-second resource sample at
    or above 512 MiB, zero swap growth, no OOM evidence, and the full artifact graph
    traceable to exact roots.
22. Repository quality, unit/contract/property/integration/acceptance suites and docs
    validation pass offline except explicitly opt-in provider canaries; canaries make no
    skill claim.

## Phase 3 exit evidence and owner gates

The review bundle MUST contain:

- exact merged contract base and implementation head, changed files, configuration/code/
  environment/lockfile digests, and test commands/results;
- verified location/elevation/station/support snapshots and a lineage export proving
  coordinate/station separation;
- one real coordinate forecast and next-day report covering every inherited field with
  reconciled matched/missing denominators and pinned revisions;
- an immutable as-of corpus manifest plus fresh-process zero-network replay evidence,
  original/replayed artifact IDs and digests, and forced logical equality results;
- the complete predeclared evaluation report with every layer/dimension/count/minimum/
  uncertainty/null reason/veto/coverage row and explicit promotion-ineligible status;
- the representative immutable resource report proving bounds, priority, lease
  exclusivity, the 512 MiB floor, zero swap growth, and no OOM;
- explicit statements that correction remained identity/no-op, no skill claim was made,
  and no AI, publication, subscriber, delivery, billing, subscription, or marketing path
  was implemented.

Tony must approve this contract before implementation starts and later approve the exit
evidence separately. Phase 3 success validates trustworthy coordinate identity,
measurement, as-of replay, and evaluation mechanics only. It does not authorize Phase 4
training or promotion.

No additional product decision is required to implement this fixed slice. Any request
to change the coordinate, coordinate precision, approved elevation sources,
station/support thresholds, observation provider, cutoffs, metrics/minima/uncertainty,
resource floor, fields, horizons, or scope requires a new contract version and owner
approval before implementation.
