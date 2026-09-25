# Retained Phase 2 scientific and replay contract

> **Active, limited scope:** this contract governs the retained Phase 2 station
> workflow and the scientific kernels/configuration rows reused by current V2 code.
> It is not the whole-product architecture. Use [ARCHITECTURE](../../ARCHITECTURE.md)
> for current implementation and [VISION](../../VISION.md) for product direction.
> The legacy three-station population, full HRRR/NBM/GFS model set, identity correction,
> phase flags and unpublished run states below apply only to that retained workflow.
> V2 uses only its explicitly selected policies; the other rows do not authorize
> new weights or sources. This path remains referenced by the Phase 2 CLI and checks.

The executable retained configuration is `configs/phase2-grasston.yaml`. Native
grid/lineage contracts, scalar/vector equations, QPF interval rules, literal fallback
tables and exact-artifact replay remain useful technical references. The historical
[v1 architecture](../archive/mesoforge-v1-architecture.md) is design history, not
current authority.

**Verification boundary:** the METAR matching/metrics section describes retained
Phase 2 behavior. It does not approve report-time tolerance for current issued-QPF
verification. Current issued QPF uses the MRMS exact-hour analysis-reference contract
and canonical samples described in [ARCHITECTURE](../../ARCHITECTURE.md). The known
legacy `P0000` trace normalization defect remains documented in the
[shared observation contract](phase-1.md#replay-and-scientific-limits).

## Fixed scope

Phase 2 is one deterministic, offline-replayable baseline and station-verification
workflow for `grasston-minnesota.v1`:

| Contract dimension | Fixed value |
| --- | --- |
| Models | HRRR, NBM, GFS, in that order |
| Stations | `station.kcbg`, `station.kjmr`, `station.kros` |
| Target horizons | Integer hours 1 through 36 |
| Horizon bands | h01-h18 and h19-h36 |
| Instantaneous fields | 2 m temperature/dew point; earth-relative 10 m U/V; 10 m gust |
| Interval fields | One-hour liquid-equivalent QPF and one-hour PoP |
| Observation source | AviationWeather METAR |
| Storage boundary | PostgreSQL manifests plus immutable S3-compatible objects |
| Output state | `complete`, `degraded`, or `invalid`; never published |

The Phase 1 domain and ordered station definitions are retained byte-for-byte.
Configuration is strict, frozen, versioned, and rejects extra fields. Source inputs
must be available no later than the information cutoff. A partially available model
cycle is rejected as a whole; leads or variables from different cycles are never
spliced.

## Source, alignment, and evidence contracts

Cycle selection uses `phase2-cycle-selection.v1`. HRRR candidates are 00/06/12/18Z,
at most 6 hours old, with a 120-minute completion deadline and source leads through
48; the source is CONUS `sfc` files named `hrrr.tCCz.wrfsfcfFF.grib2`. NBM uses the
operational CONUS core product `blend.tCCz.core.fFFF.co.grib2`, hourly cycles at most
3 hours old, and PoP01 as its only approved probability. GFS uses
`gfs.tCCz.pgrb2.0p25.fFFF`, 00/06/12/18Z cycles at most 12 hours old, and the approved
six-hour bucket accumulation series. Same-bucket differencing occurs before spatial
interpolation; bucket-reset hours pass through, and ambiguous early duplicate APCP
records require full-array equivalence. Endpoint order, inventory keys, units, retry
rules, and deadlines are pinned in configuration. Live canaries check only current
provider contract shape and are not forecast acceptance or provider-availability
guarantees.

Candidate selection is bounded by the request's `information_cutoff`, not merely by
cycle age. `_try_candidates` takes the cutoff as an explicit input and rejects any
candidate whose reference time is after it, and any candidate whose index or ranged
message bytes completed after it. A late candidate is discarded whole and the walk
continues to the next older candidate, so a run either uses inputs it was entitled
to see or omits that model entirely; a late acquisition is never retained.

NBM's native grid is an exactly pinned part of the contract, not incidental metadata.
`NbmSourceSettings.grid_profile` declares an approved `nbm-grid-profile.v1` from the
`mesoforge.catalog.grid_profiles` registry, and a configuration carrying an
unregistered or mutated profile fails to load. The operational profile is the Lambert
conformal conic CONUS core grid: 2345 x 1597 at 2.539703 km, LoV 265, LaD/Latin1/Latin2
25, spherical earth radius 6371200 m, `iScansNegatively=0`/`jScansPositively=1`/
`jPointsAreConsecutive=0`, first point (19.229 N, 233.7234 E). Decoding asserts every
one of those clauses against each decoded message, plus first/last-point coverage
derived from the message's own declared geometry, so a wrong projection, shape,
increment, scan order, or domain fails closed. Normalization then builds the real
projected CRS and inverse-projects the true lat/lon mesh rather than treating grid
indices as a geographic mesh. A reduced but equally approved
`nbm-core-conus-fixture.v1` profile — same projection parameters, smaller shape —
backs synthetic fixtures so tests exercise the identical code path at a workable size.

Every selected lead retains both the provider index evidence and the exact selected
GRIB message bytes. Inventory selection is by GRIB identity keys, not display names.
Canonical datasets retain source reference/valid/interval times, units, grid identity,
selected-message lineage, decode arguments, configuration snapshot, and source
artifact IDs. Ambiguous, duplicate, missing, or incompatible messages fail closed.

`canonical-guidance.v2` retains a **bounded window of each model's native grid**, not
the full grid. The retained window is the smallest native-grid rectangle covering the
configured inclusive `domain.bbox` plus `point_extraction_policy.halo_cells` complete
source cells on every side — the same `compute_bbox_halo_subset_indices` rule Phase 1
uses, under policy `bbox-halo-subset.v1`. Phase 2 produces values only at the three
approved stations by native-grid bilinear interpolation, which reads one 2x2
neighbourhood, so every source cell any approved extraction can read is inside that
window and nothing outside it is scientifically reachable. Subsetting is applied to the
*full decoded native geometry*, after GFS's `[0, 360)` longitude axis has been reordered
onto the monotonic `[-180, 180)` axis and after GFS dual-parent APCP equivalence has
been validated across the whole array, and every remaining per-point computation
(HRRR/GFS wind rotation, NBM speed/direction-to-U/V, GFS same-bucket differencing,
PoP percent-to-fraction) is pointwise. Retained values, retained coordinates, station
enclosing cells, and interpolation weights are therefore bit-identical to full-grid
processing; `tests/unit/guidance/test_canonical_subset_equivalence.py` proves this per
model by normalizing the same bytes twice and comparing every station and variable. A
domain whose bbox or halo the native grid cannot supply is a terminal normalization
error, never a silently clipped artifact.

Because the retained grid is not the native grid, the artifact must say exactly which
window of which grid it carries. Every `canonical-guidance.v2` artifact declares
`subset_policy_id`, the full source grid shape (`source_grid_ny`/`source_grid_nx`), the
half-open native index bounds `[subset_y_start, subset_y_end) x [subset_x_start,
subset_x_end)`, `subset_halo_cells`, and the four `subset_bbox_*` degrees; validation
rejects a missing, partial, non-integer, out-of-range, or dimension-contradicting
declaration, and rejects a halo below one cell. The corresponding `variable-lineage.v2`
manifest carries the `canonical_retention_policy` the artifact was cut under, and lineage
validation rejects a dataset whose declared window contradicts its manifest's policy.
Full source-grid provenance is unchanged and still complete: source URLs, endpoints,
byte ranges, inventory rows, message numbers, digests, the approved native grid profile
identity and shape, provider publication and local retrieval timestamps, decode
arguments, and the projection CRS all remain retained, so the full native grid can be
reacquired and the retained window reproduced and audited from the artifact alone.
Replay reads the bbox and halo from the run's own persisted run spec and station
snapshot, never from live configuration, so a fresh process retains the identical
window. Retained coordinate arrays, `grid_id`, and every downstream content-addressed
artifact identity differ from full-grid Phase 2 runs; that is expected and approved.

Canonical guidance validation enforces grid and lineage *semantics*, not identifier
syntax. A `canonical-guidance.v2` artifact must carry a grid identifier naming its own
model and no other, and its `variable_lineage_manifest_id` must resolve to a real
`variable-lineage.v2` artifact whose model, grid, cycle, configuration snapshot,
variable set, and lead set all match the dataset. Production creates one such manifest
per model — HRRR, NBM, and GFS alike — recording for every canonical variable at every
source lead the retained index artifact, every selected message artifact (plural for
GFS's dual-parent APCP), the exact inventory rows, message numbers and byte ranges, the
resolved URLs and endpoint, the selector expression, decode backend and arguments, unit
conversion, wind-rotation policy, and source/output grid identity. The GFS-specific
`gfs-qpf-lineage.v1` record remains an additional input to normalization; it is not a
substitute for the variable lineage manifest.

Spatial extraction is native-grid bilinear interpolation under
`bilinear-native-grid.v1`: four finite corners are required, weights sum within
`1e-12`, a one-cell halo may be acquired, and extrapolation is forbidden. The halo
width is also the retention halo `canonical-guidance.v2` is cut with, so the two are
never independent. Winds are earth-relative before blending. Temporal alignment
requires exact target valid times and identical one-hour interval bounds; it does not
interpolate. GFS one-hour QPF is derived from successive accumulations; only the
explicitly bounded floating-point negative tolerance may be floored upstream. No
terrain, elevation, lapse-rate, nearest-neighbor, or temporal fallback is applied.

## Blend equations and invariants

For scalar values and each wind component, with the approved row's ordered
contributors, the unrounded float64 blend is

```text
y = fsum(w_m * x_m),   sum(w_m) = 1 within 1e-12
```

Weights are selected from the exact available-model-set and horizon-band row. They
are never dynamically renormalized. Temperature and dew point use this scalar rule,
and `dew_point <= temperature + 1e-6 K` is required without clamping.

Wind blends U and V separately, then derives

```text
speed = hypot(U, V)
direction_from = degrees(atan2(-U, -V)) mod 360
```

Direction is undefined at exactly zero speed and is never angle-averaged. Before
fallback selection, a source gust more than `0.1 m/s` below its sustained speed
disqualifies that model's coupled `eastward_wind_10m`/`northward_wind_10m`/
`wind_gust_10m` tuple **at that station and target horizon**. U/V and gust are
rejected atomically because the gust operator's convexity invariant is stated
against the *blended* wind, so dropping gust alone would leave a blended gust that
no longer bounds the wind it was checked against. The model's independent variables
at that point, and its wind/gust at every other point, remain valid contributors.
The source values are never clamped or repaired; the point simply loses that
contributor and falls back to the approved row for the models that remain.
A shortfall in `(0, 0.1] m/s` is floored and recorded.
Validated gusts use the scalar/vector row. A final gust shortfall no larger than
`1e-6 m/s` is an explicitly recorded floating-point floor; a larger shortfall fails.
Final gust must be finite and in `[0, 100] m/s`.

Rejection escalates from the coupled point to the whole model cycle when the
failure is not a localized cross-field tension but evidence the cycle is untrustworthy
as a unit: a target horizon with no aligned values, an aligned point missing a
required field, a non-finite source value, or a documented coverage, geometry,
time-identity, lineage, or widespread-quality failure.

QPF uses the separate QPF table and the same weighted equation, only across identical
one-hour intervals. Inputs and output must be finite and nonnegative. PoP is not a
blend: NBM PoP01 is the sole contributor with weight `1.0`, converted to `[0, 1]`.
If NBM PoP is absent, PoP is unavailable; deterministic QPF never synthesizes PoP.

The deterministic correction artifact is `IdentityBiasCorrection`: every correction
is exactly zero. The uncorrected blend, correction, and corrected final baseline are
distinct lineage stages, and correction is applied exactly once.

## Approved fallback tables

Triples are `(HRRR, NBM, GFS)`. Every nonempty model subset has exactly one reviewed
row per band. A missing row is an error.

| Available | Scalar/vector h01-h18 | Scalar/vector h19-h36 | QPF h01-h18 | QPF h19-h36 |
| --- | --- | --- | --- | --- |
| HRRR, NBM, GFS | (.50, .30, .20) | (.35, .40, .25) | (.45, .40, .15) | (.30, .50, .20) |
| HRRR, NBM | (.60, .40, 0) | (.45, .55, 0) | (.55, .45, 0) | (.40, .60, 0) |
| HRRR, GFS | (.70, 0, .30) | (.60, 0, .40) | (.70, 0, .30) | (.60, 0, .40) |
| NBM, GFS | (0, .60, .40) | (0, .60, .40) | (0, .75, .25) | (0, .75, .25) |
| HRRR | (1, 0, 0) | (1, 0, 0) | (1, 0, 0) | (1, 0, 0) |
| NBM | (0, 1, 0) | (0, 1, 0) | (0, 1, 0) | (0, 1, 0) |
| GFS | (0, 0, 1) | (0, 0, 1) | (0, 0, 1) | (0, 0, 1) |

All three usable models yield `complete`. An approved proper subset yields
`degraded`. Missing NBM PoP alone also yields `degraded`. Any deterministic variable
without a usable approved result at any station/horizon makes the run `invalid`.

## Artifact and activity graph

The workflow validates exact role/type/schema/run ownership at every boundary.
Forecast and contribution manifest are registered atomically.

```text
run spec + all selected index/exact-message source roots in canonical model/lead/role order
  -> select-model-cycles
  -> per-model canonical normalization
station snapshot + canonical guidance
  -> align-stations
aligned guidance + selected-cycle evidence
  -> evaluate-availability
aligned guidance + availability
  -> generate-atomic-blend
       -> uncorrected baseline + 7 x 3 x 36 contribution rows
uncorrected baseline
  -> create-identity-correction
uncorrected baseline + zero correction
  -> apply-identity-correction
       -> baseline-forecast.v2
METAR response(s) + station snapshot
  -> normalize-metar-v2
baseline + normalized observations
  -> match-phase2-observations
       -> 3 x 36 matched-pair rows
matched pairs
  -> verify-phase2
       -> verification-report.v2
```

Contribution rows preserve source value, literal weight, weighted contribution,
unrounded sum, fallback row ID/digest, source artifact IDs, and correction identity.
Each row also preserves an ordered `excluded_contributors` list: every model that was
eligible for the run but rejected at that exact `(variable, station, horizon)`, with
the cause, the rejection scope (`coupled-wind-gust-point` or `model-cycle`), the
canonical fields the rejection removed, and the verbatim source gust/sustained values
and tolerance it was judged against. `model-availability-report.v1` aggregates the
same evidence run-wide: `models` names every model contributing to at least one
output, `eligible_models` names the models that survived whole-cycle screening,
`excluded_models` carries whole-cycle rejections, and `point_exclusions` carries every
point-scoped rejection. The run-level summary aggregates that evidence; it never
replaces it.
The verification artifact's ancestor graph reaches every selected index/message,
station snapshot, and METAR response.

## Production composition

The graph above is realized by production code, not by test code. Two concrete
classes in `mesoforge.application.phase2_production` implement the Phase 2
application ports:

- `Phase2ProductionProvider` implements discovery and acquisition. Discovery is
  attempt-based: for each model it walks candidate reference times newest-first
  and selects the first candidate whose complete required lead set acquires and
  satisfies that model's own max-age and completion-deadline policy through
  `guidance.cycle_selection.select_model_cycle`. A model with no viable candidate
  is absent from the run; a partial cycle is never spliced. Acquisition is real
  HRRR/NBM/GFS index-then-byte-range fetching via `guidance.acquisition_v2`, and
  every fetched index and exact selected message is registered as a source
  artifact.
- `Phase2ProductionScience` supplies the eight injected stage callables backing
  `Phase2ArtifactOperations`. It composes the pure domain functions:
  `guidance.normalization_v2` for per-cycle decoding and canonical assembly,
  `alignment.station_frame` for station alignment, `forecasting.availability` and
  the blend modules for availability and atomic blend generation,
  `observations.acquisition`/`observations.normalization_v2` for METAR truth, and
  `verification.matching`/`verification.metrics` for matching and verification.

Normalization performs the physical conversions the canonical contract requires
*before* interpolation: HRRR and GFS winds are asserted through
`uvRelativeToGrid` and rotated to earth-relative components with
`guidance.normalization.rotate_wind_to_earth_relative`; NBM speed/direction is
converted to earth-relative U/V cornerwise on the full native grid with
`guidance.sources.nbm.convert_speed_direction_to_components`. GFS one-hour QPF is
differenced within its own accumulation bucket, and when a lead carries both a
bucket record and a duplicate continuous-total record, both parents plus the
`validate_dual_parent_equivalence` result — comparing arrays, start/end steps,
statistical process, unit, grid shape, and quality mask — are recorded in a
`gfs-qpf-lineage.v1` artifact that is an input to canonical normalization.

The offline acceptance proof substitutes fixture bytes **only** at the real
`HttpTransport` GET/HEAD/range boundary. `tests/support/phase2_provider_transports.py`
serves, per model and lead, a genuine wgrib2-style `.idx` inventory whose offsets are
the true offsets of a concatenated eccodes-generated GRIB2 message stream, a HEAD
carrying the true `Content-Length`, and ranged GETs that honour the exact `Range`
header production sent and answer HTTP 206 with a matching `Content-Range`.
`Phase2ProductionProvider.discover()` and `acquire()` then run unchanged, so candidate
discovery, index parsing, selector matching and ambiguity handling, HEAD/range framing,
GRIB2 boundary and range-integrity validation, retry/deadline policy, and the
information-cutoff check are all genuinely exercised. No test constructs a
`Phase2LeadAcquisition`, `IndexRow`, or `SelectedMessage`. A model is made unavailable
by serving real 404s, not by removing it from the run, so the decision to drop a whole
partial cycle is made by production code.

## Observation, matching, and verification contracts

METAR temperature/dew point convert by `K = degC + 273.15`; wind/gust convert by
`m/s = knots * 0.5144444444444445`. U/V use the meteorological wind-from convention.
Hourly precipitation retains its represented interval and availability/revision
identity. Matching emits all 108 station/horizon rows and uses the nearest report in
an inclusive 15-minute window, with deterministic tie breaking. Missingness is an
explicit status, never an invented value.

Errors are forecast minus observation. Temperature, dew point, U, V, speed, gust,
and QPF report bias, MAE, and RMSE. Direction uses circular absolute error only when
both speeds meet the `1.5 m/s` calm threshold. QPF contingency metrics use 0.254 mm
and 2.54 mm thresholds; PoP reports Brier score and reliability-decile counts.
Reports stratify overall, by target horizon, by station, and by availability state.
Empty or undefined results serialize as JSON `null` with an explicit reason, never
NaN or Infinity.

## Replay and acceptance

Replay is reconstructed from the run's own immutable persisted roots and never
trusts a caller. `Phase2Coordinator.replay` re-reads the `phase2-run-spec.v1` and
`station-catalog-snapshot.v1` artifacts from the repository by artifact ID,
verifies their bytes against the registered content digests, and parses them under
the strict `application.phase2_replay` contracts. Only then is the caller-supplied
request checked *against* that persisted identity: a differing run ID, target
frame, cutoff, horizon set, seed, configuration snapshot/digest, code revision,
environment digest, or lockfile digest fails closed with
`Phase2ReplayIdentityError` before any stage runs. Pinned source roots must match
the run spec's recorded acquisitions exactly in both directions and by exact
artifact identity. The station snapshot and every index/message source are
registered before the run spec, which binds their `artifact_id` and
`content_digest`. Replay reloads each authoritative manifest and verified payload
and reconstructs `Phase2SelectedInputs`; caller manifest fields are never used.
A missing root, extra root, or same-role artifact substitution is a hard failure,
and a missing or incomplete persisted
root raises `Phase2ReplayContractError`. Replay never rediscovers a cycle,
re-queries providers, selects later revisions, or reconstructs inputs by
timestamps.

The registered AviationWeather response uses the run-specific locator
`phase2-metar://<run_id>`. Replay reloads its authoritative manifest and requires
that locator plus exact configuration/code identity, AviationWeather source and
availability authority, valid quality, and availability by the verification
cutoff before any stage executes.

The durable inputs are complete enough to make that real. The
`phase2-run-spec.v1` artifact records both cutoffs, the forecast issue time, the
target horizons and random seed, the cycle-selection policy actually applied, the
per-model required source groups (selected cycle reference time, acquired source
leads, endpoints, deadlines, allowed cycle hours, canonical variable IDs, source
grid profile) *and each acquired lead's own evidence* — resolved index and GRIB
URLs, index/GRIB completion timestamps, and every selected message's canonical
variable, record identity, message number, byte range, and exact inventory row.
That per-lead evidence is what lets normalization and `variable-lineage.v2`
construction run with no process-local acquisition cache: a fresh provider in a
fresh process rebuilds a byte-identical manifest. The run spec additionally
embeds the complete `Phase2Configuration` and the digests it must reproduce, so
the spec is self-verifying and alignment, availability, blending, observation
normalization, matching, and verification all read persisted policy rather than a
mutable live object. The `station-catalog-snapshot.v1` artifact records each
station's exact expected coordinates and elevation, provider ICAO identity, site
name, site types, priority, and exposure/instrument identities, plus the
point-extraction policy — every station field downstream alignment, METAR
normalization, and matching consume.

Replay performs zero network access. Observations are not re-fetched: the
coordinator passes the run's own recorded `aviationweather-metar-response`
artifacts, whose repository-authoritative manifests are validated for artifact
type, schema, source authority, quality state, configuration/code identity, and
availability by the run's verification cutoff, and whose verified stored bytes are
re-normalized. Each response is registered under the run-specific
`phase2-metar://<run-id>` locator with an immutable source identity: a retry with
different bytes for that locator fails closed, so exactly one authoritative
observation artifact can belong to the run. `build_phase2_replay_adapters` composes a replay-only coordinator
that is given no `Phase2Configuration` at all, a `Phase2UnavailableProvider` that
refuses discovery and acquisition, and an `UnavailableHttpTransport` that raises on
every request — so a network call is structurally impossible rather than merely
unused. When a live configuration *is* supplied to `Phase2ProductionScience`, it is
only ever compared against the persisted configuration digest and never read for
policy; a conflicting one fails closed.

Verified content digests protect object reads. Activity idempotency includes
ordered roles, configuration/parameter/code/environment identities, and output
schema. Identical replay, including concurrent replay, reuses the same activities
and artifacts and produces byte-identical verification content. The acceptance
proof demonstrates this independently: it runs the pipeline, deliberately mutates
the live configuration's stations, matching policy, and metric set, forbids every
socket, and then replays through a coordinator that shares no provider, science
instance, or configuration with the original run — asserting both physical
(artifact/activity identity) and logical (byte-identical payload) equality.

`make phase2-offline` is the no-network quality/unit/contract/property/scientific and
default-live-skip proof. `make phase2-acceptance` is the fixture-source acceptance
proof and requires real PostgreSQL and S3-compatible storage. CI runs the latter with
PostgreSQL 16 and MinIO after an Alembic upgrade/downgrade/upgrade round trip, then
runs the repository coverage threshold. Live tests require
`MESOFORGE_LIVE_TESTS=1` plus explicit recent cycles and are never gating.

## NBM 1h PoP versus deterministic QPF at the same valid hour

**Status: mechanics verified and source-backed. The pairing is an advisory
source-product tension only; no value is altered or disqualified.**

The mechanical contract is verified and correct. For every NBM PoP/QPF point in the
live Minnesota run (216 pairings checked):

- both records are `:APCP:surface:{lead-1}-{lead} hour acc fcst:` — a true one-hour
  window, never a 3/6/12-hour bucket;
- the window ends exactly at the target valid time;
- PoP and deterministic QPF are drawn from the *same* interval and the same lead;
- PoP01 is confirmed by GRIB2 PDT 4.9 keys (`probabilityType = 1`,
  `scaledValueOfUpperLimit`/`scaleFactorOfUpperLimit` = 254/10^3 = 0.254 kg m-2),
  i.e. `P(1h accumulation > 0.254 kg m-2)`, converted once from percent;
- deterministic QPF stays in `kg m-2` with no unit conversion.

In the live run, 25 of 108 NBM points carry a deterministic 1h QPF above the
0.254 kg m-2 event threshold while PoP01 for the identical hour is below 20%. The
clearest instance is **KJMR, target horizon 24 (valid 2026-09-03T18:00Z, NBM cycle
17Z lead 25)**:

| field | value |
| --- | --- |
| NBM PoP01 (`P(>0.254 kg m-2)`) | 0.09 (9%) |
| NBM deterministic 1h QPF | 3.6388 kg m-2 |
| interval | 24-25 hour acc fcst, ending exactly at the valid time |

This is not a decoding defect and not a mathematical contradiction. PoP01 is an
exceedance probability, decoded from PDT 4.9 metadata that states exactly what it is.
The captured GRIB2 metadata for the deterministic QPF record states its parameter,
level, interval, and unit — it does **not** state which statistic of the NBM
distribution the value represents. MesoForge therefore makes no claim about that
statistic: an earlier version of this section described it as a central/expected-value
style point estimate, which is not supported by any source MesoForge holds, and that
claim is withdrawn. Any future characterization must cite authoritative NBM product
documentation.

Because the two records are decoded from different, individually valid products, a
low-PoP/high-QPF hour is treated as an **advisory source-product tension** only. Both
values are reported verbatim with their shared interval. Absent an authoritative rule
that defines the relationship between the two statistics, MesoForge does not alter,
suppress, synthesize, or disqualify either value, and the pairing does not affect
availability state. The existing `check_probability_deterministic_tension` continues to
flag only the degenerate endpoints (`PoP == 0` with `QPF >= 0.254`, or `PoP == 1` with
`QPF == 0`); cases like KJMR h24 pass through unflagged and unreconciled.

## Explicit deferrals and limitations

The strict Phase 2 configuration fixes all of these flags to false:

- `rrfs_enabled`: RRFS acquisition, normalization, blending, and claims are disabled.
- `precipitation_type_enabled`: no precipitation-type forecast or verification exists.
- `ai_adjustment_enabled`: no LLM/AI recommendation, adjustment, or rationale exists.
- `learned_weights_enabled`: no learned weights, trained model, or optimization exists.
- Bias is identity-only; there is no learned/local bias estimate or skill attribution.
- `publication_enabled`: no output is eligible for operational publication.

Phase 2 proves deterministic contracts, failure survival, lineage, storage, and
replay—not forecast accuracy, calibration, operational readiness, or meteorological
skill. Fixture acceptance values are synthetic. Live canaries validate narrow source
contract assumptions, not numerical skill. The three stations do not establish
domain-wide representativeness; bilinear gridpoint values and station observations
have different exposure/elevation/support; METAR precision, QC, missingness, and
precipitation reporting constrain verification. No ensemble uncertainty, backtest
engine, leakage-safe training, bias promotion, API/UI, alerting/SLO, or publication
workflow is included.
