# Phase 2 multi-model baseline contract

This is the authoritative fixed-scope contract for the Phase 2 implementation. The canonical architecture remains
[`docs/architecture/v1.md`](../architecture/v1.md); when its future-state roadmap is
broader than this document, this document governs what Phase 2 actually implements.
The executable configuration is `configs/phase2-grasston.yaml`.

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

Every selected lead retains both the provider index evidence and the exact selected
GRIB message bytes. Inventory selection is by GRIB identity keys, not display names.
Canonical datasets retain source reference/valid/interval times, units, grid identity,
selected-message lineage, decode arguments, configuration snapshot, and source
artifact IDs. Ambiguous, duplicate, missing, or incompatible messages fail closed.

Spatial extraction is native-grid bilinear interpolation under
`bilinear-native-grid.v1`: four finite corners are required, weights sum within
`1e-12`, a one-cell halo may be acquired, and extrapolation is forbidden. Winds are
earth-relative before blending. Temporal alignment requires exact target valid times
and identical one-hour interval bounds; it does not interpolate. GFS one-hour QPF is
derived from successive accumulations; only the explicitly bounded floating-point
negative tolerance may be floored upstream. No terrain, elevation, lapse-rate,
nearest-neighbor, or temporal fallback is applied.

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
disqualifies that model cycle. A shortfall in `(0, 0.1] m/s` is floored and recorded.
Validated gusts use the scalar/vector row. A final gust shortfall no larger than
`1e-6 m/s` is an explicitly recorded floating-point floor; a larger shortfall fails.
Final gust must be finite and in `[0, 100] m/s`.

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

The offline acceptance proof substitutes fixture bytes **only** at the network
boundary: it subclasses `Phase2ProductionProvider` and overrides `discover` to
supply fixture GRIB2 payloads, then inherits the production `acquire` and uses
`Phase2ProductionScience` unchanged. Removing any production science stage breaks
the acceptance test, so the proof cannot drift from the shipped pipeline.

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

Replay receives the recorded `Phase2SelectedInputs`; it does not rediscover a cycle,
re-query providers, select later revisions, or reconstruct inputs by timestamps.
Verified content digests protect object reads. Activity idempotency includes ordered
roles, configuration/parameter/code/environment identities, and output schema.
Identical replay, including concurrent replay, reuses the same activities and
artifacts and produces byte-identical verification content.

`make phase2-offline` is the no-network quality/unit/contract/property/scientific and
default-live-skip proof. `make phase2-acceptance` is the fixture-source acceptance
proof and requires real PostgreSQL and S3-compatible storage. CI runs the latter with
PostgreSQL 16 and MinIO after an Alembic upgrade/downgrade/upgrade round trip, then
runs the repository coverage threshold. Live tests require
`MESOFORGE_LIVE_TESTS=1` plus explicit recent cycles and are never gating.

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
