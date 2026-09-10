> **Historical plan (archived 2026-09-09):** This completed Phase 0-2 development plan is retained for reference. Its agent instructions and delivery checklist are not current work orders. Follow the current owner request and root AGENTS.md; see the [archive index](../README.md). Original path: .hermes/plans/2026-08-30_233217-phase-2-multimodel-baseline.md. The original contents follow unchanged.

# MesoForge Phase 2 Multi-Model Baseline Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Extend the completed Grasston Phase 1 slice into a replayable 36-hour, station-focused deterministic baseline using HRRR, operational NBM, and GFS for temperature, wind, dew point, wind gust, PoP, and one-hour QPF, with explicit alignment, contributor fallbacks, explanations, and verification.

**Architecture:** Preserve the Python modular-monolith and immutable artifact graph from Phase 1. Each provider is acquired and normalized independently; a pure alignment layer maps source-valid fields to one hourly station frame; variable-specific deterministic operators create an `UncorrectedBlendForecast`; an explicit identity correction produces `BaselineForecast.v2`; and every output records exact source bytes, source cycle, valid/interval time, spatial transformation, selected fallback row, and numeric contributions. Per-variable availability is first-class so a source outage produces a deterministic complete or degraded result rather than silent renormalization.

**Tech Stack:** Python 3.12, Pydantic, NumPy, xarray, pyproj, Herbie/cfgrib/eccodes, PyArrow, SQLAlchemy/PostgreSQL, boto3/MinIO, Alembic, pytest/Hypothesis, Ruff, mypy, import-linter, GitHub Actions.

---

## 0. Delivery state and non-negotiable scope

- Plan base: exact merged Phase 1 commit `79f3720b7343ea8d2574d4a8c7b9ab7bf7cedebd`.
- Planning branch: `codex/phase-2-plan`.
- Implementation branch: `claude/phase-2-multimodel-baseline`, based on the exact planning head.
- Retain domain `grasston-minnesota.v1`, center `45.80265, -93.07956`, and stations `station.kcbg`, `station.kjmr`, `station.kros` in that order.
- Retain HRRR; add NBM and GFS. RRFS is explicitly deferred.
- No AI, learned weights, learned bias correction, publication, UI, API expansion, grid product, or orchestration-framework selection.
- Precipitation type is disabled and non-publishable. Phase 2 must not acquire or emit a precipitation-type forecast. A future phase requires a separately approved probability-vector truth contract and atomic temperature/PoP/QPF consistency policy.
- Default tests and CI are network-free. Provider requests occur only in opt-in live contract tests.
- This phase remains a station/small-domain architecture proof, not an operational skill claim or public forecast product.

## 1. Product issue frame, source-cycle selection, and availability

### 1.1 Product frame

`Phase2Request` adds a whole-hour UTC `target_reference_time`, separate from `forecast_issue_time`, every provider reference time, `information_cutoff`, and `verification_cutoff`.

- `forecast_issue_time >= target_reference_time`.
- `information_cutoff <= forecast_issue_time`.
- Target horizons are exactly integers `1..36`.
- `valid_time[h] = target_reference_time + h hours`.
- Instantaneous fields are valid exactly at `valid_time[h]`.
- PoP/QPF interval bounds are exactly `(valid_time[h] - 1 hour, valid_time[h]]`, closure `right`.
- No temporal interpolation, carry-forward, or nearest-time substitution is permitted.
- Do not call the target horizon a source lead. Every contribution separately retains `source_reference_time`, `source_forecast_hour`, source interval, and target horizon.

### 1.2 Deterministic candidate-cycle selection

For each model family, choose one cycle for the entire run before selecting fields. A model cannot use different cycles by variable or target horizon.

1. Consider cycles at the model cadence, newest first, with `source_reference_time <= target_reference_time`.
2. Reject a cycle unless every model-specific required source group and target horizon is complete, valid, and `available_at <= information_cutoff`. HRRR requires T, Td, paired U/V, gust, and QPF; NBM requires T, Td, paired speed/direction, gust, QPF, and PoP01; GFS requires T, Td, paired U/V, gust, and QPF. A wind pair is one availability group but both messages are mandatory. HRRR/GFS are never rejected for lacking PoP because PoP is not in their source contract.
3. Reject a cycle completed after its source-cycle availability deadline, even if acquired before the run cutoff.
4. Select the first remaining cycle whose age is within the configured maximum.
5. If none qualifies, mark that model family unavailable. Never splice cycles.

Configured policy `phase2-cycle-selection.v1`:

| Model | Allowed cycle hours | Max age at target reference | Cycle completion deadline | Required coverage |
|---|---:|---:|---:|---|
| HRRR CONUS surface extended cycles | 00/06/12/18 UTC only | 6 h | reference + 120 min | every target valid time through +36; required source lead <=48 |
| NBM CONUS core | hourly | 3 h | reference + 90 min | every target valid time through +36 |
| GFS 0.25 degree | 00/06/12/18 UTC | 12 h | reference + 360 min | every target valid time through +36 |

The deadline is a product policy, not a claim about provider first publication. Artifact `available_at` remains the first successful, fully received local response completion under the recorded provider/clock policy. Preserve `Last-Modified`, `ETag`, response headers, local request start/end, mirror, URL, and index bytes without treating any one header as authoritative first availability.

### 1.3 Endpoints and immutable identity

- HRRR remains `hrrr.tCCz.wrfsfcfFF.grib2`, product `sfc`, sector `conus`, AWS then NOMADS, using the existing exact-byte range path. Only 00/06/12/18 extended cycles are Phase 2 candidates because ordinary hourly cycles stop at +18; require `source_forecast_hour <=48` after accounting for cycle age.
- NBM uses operational CONUS core `blend.tCCz.core.fFFF.co.grib2` and its `.idx`. Mirror order is NOAA NBM S3 then NOMADS. Pin contract profile `nbm-core-conus-operational.v1`; do not hard-code a marketing version into identity because the operational filename does not carry one. A live canary detects operational version/field drift.
- GFS uses `gfs.tCCz.pgrb2.0p25.fFFF` and `.idx`, product profile `gfs-pgrb2-0p25.v1`, Google Cloud/AWS public archive where configured then NOMADS. Do not substitute `0p50`, `1p00`, `pgrb2b`, `sflux`, OPeNDAP, or transformed third-party data.
- Preserve full index bytes plus exact selected GRIB message bytes. Every selected message records message number, start/end byte, complete inventory row, decoded GRIB identity keys, provider URL, mirror, response revision, digest, cfgrib/eccodes versions, and backend arguments.
- Duplicate or ambiguous inventory matches fail unless an explicit source-specific equivalence rule below requires and validates both records.

## 2. Authoritative source field contracts

The selectors are inventory prefilters only. Decoded GRIB keys and temporal semantics are authoritative and must all match.

### 2.1 Canonical variables

| Canonical ID | Unit | Semantics | Vertical/spatial support | Bounds |
|---|---|---|---|---|
| `air_temperature_2m` | K | instantaneous | 2 m, interpolated station point | 180..340 K |
| `dew_point_temperature_2m` | K | instantaneous | 2 m, interpolated station point | 150..340 K and `Td <= T + 1e-6 K` |
| `eastward_wind_10m` | m/s | instantaneous | earth-relative, 10 m, interpolated station point | finite |
| `northward_wind_10m` | m/s | instantaneous | earth-relative, 10 m, interpolated station point | finite |
| `wind_gust_10m` | m/s | instantaneous model gust diagnostic | surface/10 m contract mapped to canonical 10 m gust | 0..100 m/s |
| `probability_of_precipitation_1h` | 1 | probability of liquid-equivalent QPF >= 0.254 kg/m2 in exact hour | interpolated station point | 0..1 |
| `liquid_equivalent_precipitation_amount_1h` | kg/m2 | accumulation over exact hour | interpolated station estimate of source grid-cell depth | >=0 |

`kg/m2` is numerically equal to millimetres of liquid water depth under the standard water-density convention; retain canonical unit `kg/m2` and presentation alias `mm` rather than converting bytes.

### 2.2 HRRR `sfc` messages

Use the existing projection/wind-orientation validation and extend fields through +36:

- `TMP:2 m above ground:<lead> hour fcst` — discipline 0/category 0/number 0, K.
- `DPT:2 m above ground:<lead> hour fcst` — discipline 0/category 0/number 6, K.
- `UGRD:10 m above ground:<lead> hour fcst` — discipline 0/category 2/number 2, m/s.
- `VGRD:10 m above ground:<lead> hour fcst` — discipline 0/category 2/number 3, m/s.
- `GUST:surface:<lead> hour fcst` — discipline 0/category 2/number 22, m/s; map this HRRR surface gust diagnostic to canonical `wind_gust_10m` while retaining source level `surface` in lineage.
- `APCP:surface:<start>-<end> hour acc fcst` — discipline 0/category 1/number 8, `start=end-1`, kg/m2. Select HRRR's direct rolling one-hour record; do not difference the run-total record.
- HRRR has no approved probability field for this contract and does not contribute to PoP.

### 2.3 NBM CONUS `core` messages

Use exact deterministic records, never ensemble standard deviations, percentiles, QMD files, or probability records other than PoP01:

- `TMP:2 m above ground:<lead> hour fcst`, K.
- `DPT:2 m above ground:<lead> hour fcst`, K.
- `WIND:10 m above ground:<lead> hour fcst`, m/s.
- `WDIR:10 m above ground:<lead> hour fcst`, degree meteorological wind-from.
- `GUST:10 m above ground:<lead> hour fcst`, m/s.
- deterministic `APCP:surface:<lead-1>-<lead> hour acc fcst`, kg/m2.
- PoP01: `APCP:surface:<lead-1>-<lead> hour acc fcst:prob >0.254:...:probability forecast`, percent converted once to fraction by dividing by 100.

NBM's speed/direction pair is converted to earth-relative components before spatial interpolation:

`u = -speed * sin(direction_degrees)` and `v = -speed * cos(direction_degrees)`.

Calm speed produces `u=v=0`; direction is ignored at calm. Reject direction outside `[0,360)`, negative speed, missing pair members, or non-matching valid time.

### 2.4 GFS `pgrb2.0p25` messages

- `TMP:2 m above ground:<lead> hour fcst`, K.
- `DPT:2 m above ground:<lead> hour fcst`, K.
- `UGRD:10 m above ground:<lead> hour fcst`, m/s.
- `VGRD:10 m above ground:<lead> hour fcst`, m/s.
- `GUST:surface:<lead> hour fcst`, m/s; map source surface gust to canonical `wind_gust_10m` with lineage.
- bucket accumulation `APCP:surface:<bucket_start>-<lead> hour acc fcst`, kg/m2, where `bucket_start = 6 * floor((lead - 1) / 6)` for the Phase 2 <=48-hour source leads.
- GFS has no approved probability field and does not contribute to PoP.

GFS publishes both six-hour-bucket and continuous accumulations. Phase 2 deliberately uses the bucket series because it yields bounded one-hour differences across every required source lead. Let `f` be the GFS source forecast hour and `b = 6 * floor((f - 1) / 6)`. Select the record whose decoded `startStep=b`, `endStep=f`, and statistical process is accumulation. For `f=1..6`, the bucket and continuous inventory descriptions are duplicates; acquire both candidates and allow canonicalization only when full arrays, units, grids, start/end steps, statistical process, and masks are equivalent, recording both parents and the equivalence result. For `f=6k+1`, one-hour QPF is the `b-f` bucket value itself (the bucket begins at `f-1`). Otherwise it is `bucket[f] - bucket[f-1]`, and both messages must have the same `startStep=b`. Reject a decrease below `-1e-6 kg/m2`; map a difference in `[-1e-6,0)` to zero with a finite-precision quality bit. This contract was checked against operational f001/f005/f006/f007/f008/f012/f013 inventories. Never select APCP by message order or mix bucket and continuous parents.

### 2.5 Grid, longitude, and decode assertions

- HRRR: Lambert conformal CONUS grid; assert every existing Phase 1 projection key plus shape, increments, scan flags, and `uvRelativeToGrid`.
- NBM CONUS: assert the operational CONUS grid definition, projection parameters, shape, increments, scan flags, and geographic coverage from decoded keys. Do not infer projection from filename.
- GFS 0.25: regular latitude/longitude global grid, 0.25-degree spacing, expected shape and scan order. Normalize longitudes to `[-180,180)` only for coordinate lookup; retain source longitudes and convention in the canonical artifact.
- For U/V products inspect `uvRelativeToGrid`; rotate to earth-relative east/north before interpolation when required. NBM speed/direction is already earth-relative by contract but is still converted to components before interpolation.
- Every cycle must use one stable grid identity per model. A mid-cycle grid change fails the model family.
- Unit conversion is explicit and recorded. Reject unknown, missing, or merely compatible-but-unapproved units.

## 3. Spatial and temporal alignment

### 3.1 Target frame

Create `aligned-station-guidance.v1` with dimensions `model`, `target_horizon`, and `location`, plus exact target `valid_time`; interval variables carry `(start,end]` bounds. Each value has source artifact/message, source reference, source lead, source interval, source grid, spatial method, source indices/weights, quality bits, and availability.

### 3.2 Spatial method

Phase 2 performs no grid-to-grid regridding. Each model remains on its native grid and is transformed directly to the three station points:

- instantaneous scalar fields: native-grid bilinear interpolation;
- earth-relative U/V components: bilinear interpolation independently, then derive speed/direction;
- NBM speed/direction: convert the four source corners to U/V first, then bilinear interpolation;
- PoP and QPF depth: native-grid bilinear interpolation of bounded probability or grid-cell depth as a station estimate.

Retain the Phase 1 requirements: no extrapolation, four finite unmasked corners, one-cell halo, deterministic weight sum within `1e-12`, and explicit source indices/weights. Probability interpolation must remain within `[0,1]` without clipping. QPF is a depth, not total mass; no conservation claim is made. Documentation must label precipitation results as interpolated grid guidance, not station-gauge estimates.

### 3.3 Temporal method

- Exact valid-time match only for temperature, dew point, wind, and gust.
- Exact interval-bound match only for PoP and QPF.
- GFS same-bucket differencing occurs before spatial interpolation so both complete fields and masks are validated before subtraction.
- No interpolation between source valid times; a missing source hour makes that model unavailable for the entire run under the complete-cycle rule.

## 4. Deterministic blend and fallback policy

### 4.1 Availability state

`model-availability-report.v1` records every considered cycle and rejection reason. `variable-availability-state` is one of:

- `complete`: all configured contributors available;
- `fallback`: an approved subset row used;
- `unavailable`: no approved row exists;
- `inconsistent`: inputs exist but violate scientific constraints. A source-level inconsistency disqualifies that entire model cycle before fallback selection; it does not create a gust-only or variable-only contributor set.

The run result is:

- `complete` when every variable is complete;
- `degraded` when each deterministic variable has an approved result but at least one used fallback, or PoP is unavailable;
- `invalid` when temperature, dew point, U/V wind, gust, or QPF has no result after source-cycle disqualification/fallback, or when a blend-level cross-variable/invariant check fails. A disqualified model with an approved remaining fallback row yields `fallback`, not automatic run invalidity.

A degraded result is retained and verified but is not publishable. There is no publication path in Phase 2.

### 4.2 Approved fallback weights: scalar/vector group

For temperature, dew point, and U/V wind, select the exact row by available model-family set and target horizon. Model order is `(HRRR, NBM, GFS)`.

| Available set | h01-h18 | h19-h36 |
|---|---|---|
| H+N+G | 0.50 / 0.30 / 0.20 | 0.35 / 0.40 / 0.25 |
| H+N | 0.60 / 0.40 / 0 | 0.45 / 0.55 / 0 |
| H+G | 0.70 / 0 / 0.30 | 0.60 / 0 / 0.40 |
| N+G | 0 / 0.60 / 0.40 | 0 / 0.60 / 0.40 |
| H only | 1 / 0 / 0 | 1 / 0 / 0 |
| N only | 0 / 1 / 0 | 0 / 1 / 0 |
| G only | 0 / 0 / 1 | 0 / 0 / 1 |

Weights are literals in immutable configuration, not computed by renormalizing a preferred row. Every row sums to one within `1e-12` and has its own policy digest.

### 4.3 Gust weights and operator

Gust uses the scalar/vector table but a dedicated operator:

1. At each aligned source point calculate source sustained speed from source U/V. If `gust < sustained_speed - 0.1 m/s`, disqualify the entire model cycle for this run before availability/fallback evaluation; variable-only fallback is forbidden. If the shortfall is in `[0,0.1] m/s`, floor that source gust to sustained speed and record `source_gust_floor_applied`.
2. Calculate the configured weighted mean of validated/floored source gusts with float64 intermediates and the same contributor row used for U/V.
3. Calculate blended sustained speed from blended U/V. Convexity should guarantee gust is no lower. If a finite-precision shortfall is <=`1e-6 m/s`, floor to sustained speed and record `final_gust_epsilon_floor`; any larger shortfall is an invariant failure.
4. Enforce `0..100 m/s` without clipping.

### 4.4 QPF weights and operator

QPF uses explicit precipitation rows:

| Available set | h01-h18 | h19-h36 |
|---|---|---|
| H+N+G | 0.45 / 0.40 / 0.15 | 0.30 / 0.50 / 0.20 |
| H+N | 0.55 / 0.45 / 0 | 0.40 / 0.60 / 0 |
| H+G | 0.70 / 0 / 0.30 | 0.60 / 0 / 0.40 |
| N+G | 0 / 0.75 / 0.25 | 0 / 0.75 / 0.25 |
| H only | 1 / 0 / 0 | 1 / 0 / 0 |
| N only | 0 / 1 / 0 | 0 / 1 / 0 |
| G only | 0 / 0 / 1 | 0 / 0 / 1 |

Blend only identical one-hour intervals. Inputs and output must be finite and nonnegative. Do not clip negatives except the explicitly bounded GFS differencing tolerance before blending.

### 4.5 PoP operator

PoP is NBM PoP01 passthrough with weight `NBM=1.0`. Do not derive probability from deterministic HRRR/GFS precipitation and do not combine incompatible event definitions.

- NBM available: emit bounded PoP and record NBM as sole contributor.
- NBM unavailable: PoP state is `unavailable`; the run may be `degraded` if every deterministic variable remains valid.
- No previous-run PoP carry-forward beyond the selected NBM cycle policy.

### 4.6 Cross-variable checks

- Use identical contributor rows for temperature and dew point; then require `dew_point <= temperature + 1e-6 K`. Reject rather than clamp.
- Recompute speed/direction only from blended U/V. Never average direction angles.
- Gust policy is Section 4.3.
- PoP must be `[0,1]`; QPF must be nonnegative.
- Flag, but do not alter, `PoP == 0 and QPF >= 0.254` or `PoP == 1 and QPF == 0` as `probability_deterministic_tension`; these are not mathematical contradictions.
- All three locations and all 36 target horizons must have one state for every variable.

### 4.7 Contribution explanation

`blend-contribution-manifest.v1` has one deterministic row per `(variable, location, target_horizon)` with:

- target valid/interval time;
- operator ID/version;
- availability state and exact fallback row ID;
- ordered contributors with model, source cycle/lead, artifact/message, aligned value, configured weight, weighted contribution, and quality bits;
- unrounded float64 sum and serialized output;
- derived U/V-to-speed/direction details;
- GFS QPF differencing parents/tolerance action;
- gust-floor or tension flags;
- configuration, code, environment, and lockfile digests.

The manifest must independently reconstruct every forecast value within a configured `1e-12` absolute tolerance.

## 5. Forecast artifacts and lineage

### 5.1 New/updated schemas

- `model-cycle-selection.v1` — considered cycles, deadlines, completeness, deterministic selection.
- `hrrr-acquisition-manifest.v2`, `nbm-acquisition-manifest.v1`, `gfs-acquisition-manifest.v1`.
- `variable-lineage.v2` — model/cycle/message/interval aware.
- `canonical-guidance.v2` — one provider/cycle native grid with scalar `forecast_reference_time`, one-dimensional `source_lead_time` and `source_valid_time`, native `y/x`, model ID, exact interval bounds, expanded quality bits, and model-specific variables. Add `validate_canonical_guidance_v2`; retain `validate_canonical_dataset` as the unchanged v1 entry point. A new `validate_guidance_dataset` registry dispatches by exact schema version and rejects unknown versions.
- `point-extraction-report.v2` — model-aware interpolation and projection evidence.
- `aligned-station-guidance.v1` — exact dimensions `("model", "target_horizon", "location")`; model order HRRR/NBM/GFS; integer horizons 1..36; target valid time; per-model source reference/forecast hour; data plus quality masks for unavailable/not-provided values. Add `validate_aligned_station_guidance_v1`; this schema never enters either canonical-guidance validator.
- `model-availability-report.v1`.
- `blend-contribution-manifest.v1`.
- `uncorrected-blend-forecast.v1`.
- `identity-bias-correction.v1` — explicit all-zero correction for Phase 2.
- `baseline-forecast.v2` — exact dimensions `("target_horizon", "location")`, target reference/valid/interval coordinates, named forecast variables, and per-variable state. Add `validate_baseline_forecast_v2`; preserve `validate_baseline_forecast` as the unchanged v1 function. A new exact-version dispatcher may route callers without weakening either validator.
- `metar-observations.v2`, `matched-pairs.v2`, `verification-report.v2`.

Do not mutate v1 schemas or reinterpret Phase 1 artifacts.

### 5.2 Artifact types

Add source types `nbm-grib-index`, `nbm-selected-grib`, `gfs-grib-index`, and `gfs-selected-grib`. Add derived types for every schema above. Artifact type remains a validated string in the existing repository; no artifact-type database enum is introduced.

### 5.3 Activity graph

Canonical ordered activities:

1. acquire/register exact source index and message roots per selected model cycle;
2. describe each acquisition;
3. build variable lineage;
4. normalize one canonical grid artifact per model;
5. extract each model to station points;
6. align target valid times/intervals;
7. evaluate model/variable availability;
8. generate uncorrected blend and contribution manifest atomically;
9. apply explicit identity correction once to create `baseline-forecast.v2`;
10. normalize observations after issuance;
11. match by event/interval as of verification cutoff;
12. verify and export lineage.

Every role-bound transformation declares exact role keys and validators. The uncorrected blend and contribution manifest must either both register from one succeeded activity or neither register. The baseline has the uncorrected blend and identity-correction artifact as ancestors. Replay uses recorded artifact IDs/parameters and never rediscovers candidates.

### 5.4 Run and database decision

`RunManifest.v1` remains the authoritative issuance/cutoff record and is not reinterpreted. Before run creation, register a selected immutable `phase2-run-spec.v1` artifact containing `target_reference_time`, `verification_cutoff`, model-specific required groups, cycle-selection policy, target horizons, and all request digests. `selected_input_artifact_ids` must then pin, in canonical order: the run-spec; every selected HRRR raw index/message revision ordered by source lead and field role; every selected NBM raw index/message revision in the same order; every selected GFS raw index/message revision in the same order; and the station-catalog snapshot. Rejected candidate cycles and post-issue observations are not selected inputs but remain reachable through their own activity lineage. Phase 2 replay loads exactly these selected roots and never rediscovers revisions. No migration is expected: existing artifact, activity, edge, run, configuration-snapshot, and content-addressed object tables represent all new state. Availability and contribution data remain immutable artifacts, not mutable relational columns. If implementation discovers a required schema change, stop for Codex design review; do not add an opportunistic migration.

## 6. Observation truth and verification

### 6.1 METAR extension

Continue AviationWeather exact-response retention, revisions, as-of cutoff, station snapshot, and ±15-minute matching. Extend strict decoded/raw retention for:

- dew point (`dewp`) converted C to K;
- explicit wind gust (`wgst`) converted knots to m/s;
- raw routine METAR `Prrrr` hourly liquid precipitation group, where `rrrr` is hundredths of an inch.

For precipitation truth, accept only a non-SPECI routine report with an explicit syntactically valid `Prrrr` group representing the hour ending at the report time. Convert `rrrr * 0.01 inch * 25.4` to kg/m2. Missing `Prrrr` is missing truth, never zero. Require the observation interval to match the forecast interval within the existing 15-minute end-time tolerance; preserve both exact bounds and report time. Corrected reports remain revisions selected as of cutoff.

### 6.2 Metrics

Metric set `phase2-multimodel-station.v1`:

- temperature/dew point: bias, MAE, RMSE;
- eastward/northward wind and speed: bias, MAE, RMSE;
- direction: mean absolute circular error when forecast and observed speeds are both >=1.5 m/s;
- gust: bias, MAE, RMSE only where an explicit gust is reported; label `conditional_on_reported_gust` and do not use for promotion claims;
- QPF: bias, MAE, RMSE; event contingency counts, CSI, POD, FAR, frequency bias, and ETS at 0.254 and 2.54 kg/m2;
- PoP: Brier score `mean((p-o)^2)` and fixed decile reliability-bin counts for event `observed QPF >=0.254 kg/m2`; bins are `[0,.1)`, ..., `[.8,.9)`, `[.9,1.0]` so probability 1 belongs to the final bin;
- Brier reliability/resolution/uncertainty decomposition only when sample >=100 and both event classes occur: `REL=sum(nk/N*(pbar_k-obar_k)^2)`, `RES=sum(nk/N*(obar_k-obar)^2)`, `UNC=obar*(1-obar)`; otherwise JSON `null` plus reason;
- ROC AUC only when sample >=100 and both classes occur, using the Mann-Whitney definition `P(score_event > score_nonevent) + 0.5*P(tie)`; otherwise `null` plus reason.

For deterministic QPF thresholds, observed and forecast events both use `>= threshold`. Define hits as forecast yes/observed yes, misses as forecast no/observed yes, false alarms as forecast yes/observed no, and correct negatives as both no. `CSI=H/(H+M+F)`, `POD=H/(H+M)`, `FAR=F/(H+F)`, frequency bias `(H+F)/(H+M)`, random hits `Hr=(H+F)*(H+M)/N`, and `ETS=(H-Hr)/(H+M+F-Hr)`. A zero denominator yields JSON `null` with exact reason `zero_denominator:<metric>`; counts are always emitted.

For every continuous scalar, error is `forecast - observation`; bias is `mean(error)`, MAE is `mean(abs(error))`, and RMSE is `sqrt(mean(error**2))`, all with float64 accumulation. Direction signed error is `((forecast_degrees - observed_degrees + 180) % 360) - 180`; the reported direction metric is `mean(abs(signed_error))` after the calm filter. For Brier decomposition, `pbar_k` is the mean of actual forecast probabilities assigned to nonempty bin `k` (not the bin center), `obar_k` is that bin's observed event fraction, and `obar` is the overall event fraction. Empty bins have count zero, serialized `pbar_k=null` and `obar_k=null`, and contribute zero to REL/RES.

Emit overall/by-target-horizon/by-station/by-availability-state strata with matched/missing counts and reasons. Never emit NaN/Infinity. The three-station sample is diagnostic; no metric establishes operational skill or authorizes publication.

## 7. Module boundaries and interfaces

### 7.1 Production modules

Modify or add only at these boundaries:

```text
configs/phase2-grasston.yaml
src/mesoforge/
  common/identifiers.py
  catalog/configuration.py
  catalog/sources.py
  catalog/variables.py
  contracts/datasets.py
  contracts/forecasts.py
  contracts/lineage.py
  contracts/observations.py
  contracts/verification.py
  guidance/
    acquisition.py
    decoding.py
    normalization.py
    validation.py
    cycle_selection.py
    precipitation.py
    sources/hrrr.py
    sources/nbm.py
    sources/gfs.py
  alignment/
    spatial.py
    temporal.py
    reports.py
  forecasting/
    availability.py
    contributions.py
    scalar_blend.py
    vector_blend.py
    gust_blend.py
    precipitation_blend.py
    consistency.py
    baseline.py
    validation.py
  observations/
    normalization.py
    quality.py
  verification/
    matching.py
    metrics.py
    validation.py
  application/
    phase2.py
    phase2_adapters.py
```

Provider URLs, inventory syntax, HTTP transport, and GRIB keys stay in source/acquisition/decoding modules. Scientific transforms stay in pure guidance/alignment/forecasting/verification modules. `application/phase2.py` composes ports and artifact services and contains no selectors, meteorological formulas, interpolation, weights, or metric formulas.

### 7.2 Protocols

```text
GuidanceCycleSource.discover(request) -> CycleInventory
GuidanceCycleSource.acquire(selection) -> AcquiredCycle
GuidanceNormalizer.normalize(acquired, contract) -> CanonicalGuidance
StationAligner.align(guidance, target_frame, policy) -> AlignedStationGuidance
AvailabilityEvaluator.evaluate(aligned, policy) -> AvailabilityReport
MultiModelForecaster.generate(aligned, availability, blend_config)
    -> (UncorrectedBlendForecast, BlendContributionManifest)
BiasCorrector.apply(blend, IdentityCorrection) -> BaselineForecastV2
ObservationMatcher.match(baseline, observations, policy) -> MatchedPairsV2
Verifier.calculate(pairs, metric_set) -> VerificationReportV2
```

Use strict frozen Pydantic request/result models and typed IDs/digests. Domain protocols cannot import concrete storage, PostgreSQL, S3, HTTP clients, API, or orchestration packages.

## 8. TDD implementation sequence

Every task follows RED -> GREEN -> REFACTOR, runs the focused commands, and ends in a focused commit. Exact test names may be split for maintainability but may not weaken the named assertions.

### Task 1: Add Phase 2 configuration and boundary contracts

**Files:** `configs/phase2-grasston.yaml`, `src/mesoforge/catalog/configuration.py`, `sources.py`, `variables.py`, `common/identifiers.py`, `.importlinter`, tests under `tests/unit/catalog/` and `tests/contracts/`.

1. Write failing tests for exact domain/station retention, source product/cycle/horizon/deadline values, every explicit fallback row, sums, immutable digest changes, invalid overrides, RRFS absence, and PoP NBM-only rule.
2. Implement strict models and configuration. Preserve Phase 1 configuration validation unchanged.
3. Add variable definitions, probability event threshold, exact interval requirements, and units.
4. Run `uv run pytest tests/unit/catalog tests/contracts -q` and `uv run lint-imports`.

**Commit:** `feat: define Phase 2 source and blend contracts`

### Task 2: Generalize exact-byte acquisition without weakening HRRR

**Files:** `guidance/acquisition.py`, `guidance/interfaces.py`, `guidance/cycle_selection.py`, source adapters, focused unit/property tests.

1. Write failing tests for deterministic candidate ordering, completeness, cutoff/deadline/max-age rejection, mirror failover, one-cycle-per-model, and no per-variable cycle splicing.
2. Extract reusable strict range/index/framing behavior while retaining all Phase 1 Content-Range, GRIB2 edition/length, retry, and idempotency tests.
3. Implement NBM/GFS URL/index parsing and exact message registration.
4. Mutation probes: remove cutoff comparison, reverse candidate order, accept partial cycle, accept inconsistent Content-Range total; each must fail tests.
5. Run source/acquisition tests plus the complete existing HRRR suite.

**Commit:** `feat: select and acquire complete multi-model cycles`

### Task 3: Implement NBM decode/normalization contract

**Files:** `guidance/sources/nbm.py`, `decoding.py`, `normalization.py`, `validation.py`, generated eccodes fixtures, contract/unit/property tests.

1. Generate a synthetic NBM-like projected fixture with every deterministic field plus confounding std-dev/percentile/QMD-like records.
2. Test exact message selection, decoded keys, units, intervals, probability threshold/percent conversion, speed/direction conversion, grid identity, and ambiguity rejection.
3. Normalize +36 hours to `canonical-guidance.v2` with exact lineage.
4. Mutation probes: select std dev, use PoP06, omit `/100`, treat wind direction as toward, accept wrong interval/grid.

**Commit:** `feat: normalize NBM core guidance`

### Task 4: Implement GFS decode/normalization and APCP disambiguation

**Files:** `guidance/sources/gfs.py`, `decoding.py`, `precipitation.py`, `normalization.py`, generated fixtures and tests.

1. Test exact 0.25-degree product, cycle cadence, regular lat/lon grid, 0..360 longitude normalization, field keys, and earth/grid-relative winds.
2. Generate duplicate bucket/continuous APCP cases for equivalent f01-f06 records and divergent ambiguous records, plus reset/boundary cases f07/f08/f12/f13, f18/f19, f24/f25, f30/f31, f36/f37, f42/f43, and f47/f48.
3. Require dual-parent equivalence for duplicate early records and exact `startStep=6*floor((f-1)/6)` selection later.
4. Test same-bucket differencing, first-hour-after-reset passthrough, exact interval, negative tolerance, masks, parent lineage, and non-monotonic rejection.
5. Mutation probes: select by record order, subtract after interpolation, silently accept a divergent duplicate, cross a bucket reset by subtraction, select the continuous total, or clamp a material negative.

**Commit:** `feat: normalize GFS point and precipitation guidance`

### Task 5: Extend HRRR to 36 hours and Phase 2 fields

**Files:** `configs/phase2-grasston.yaml`, `guidance/sources/hrrr.py`, normalization/validation modules, fixtures/tests.

1. Test +36 completeness and exact TMP/DPT/U/V/GUST/rolling-APCP selectors.
2. Preserve existing 0..6 Phase 1 behavior and contracts.
3. Select rolling one-hour APCP, rejecting run-total confusion.
4. Add generated precipitation/gust/dew-point cases and projection/wind-orientation regressions.

**Commit:** `feat: extend HRRR guidance for Phase 2`

### Task 6: Align native grids to the station target frame

**Files:** `alignment/spatial.py`, `temporal.py`, `reports.py`, contracts and unit/property/scientific tests.

1. Test exact valid/interval matching and reject interpolation/carry-forward.
2. Test HRRR/NBM projected and GFS regular-grid bilinear point extraction, longitude wrap, descending axes, edge/halo, masks, and no extrapolation.
3. Test NBM vector conversion before interpolation and U/V rotation before interpolation.
4. Emit model-aware extraction/alignment evidence and validate all 3 x 36 keys.
5. Mutation probes: interpolate direction, rotate after interpolation, accept wrong interval, nearest-neighbor fallback.

**Commit:** `feat: align multi-model guidance to Grasston stations`

### Task 7: Implement availability and explicit fallback selection

**Files:** `forecasting/availability.py`, configuration contracts, unit/property tests.

1. Test every table row in both horizon bands and every model outage.
2. Test no implicit renormalization, unsupported sets, model partialness, PoP NBM-only behavior, and run complete/degraded/invalid states.
3. Property-test deterministic ordering and exact weight sum.
4. Mutation probes: delete a row, normalize preferred weights dynamically, classify NBM outage as complete.

**Commit:** `feat: evaluate deterministic model fallbacks`

### Task 8: Implement scalar and vector blend operators

**Files:** `forecasting/scalar_blend.py`, `vector_blend.py`, `consistency.py`, unit/property/scientific tests.

1. Hand-test weighted temperature/dew point and cardinal/opposing wind vectors.
2. Blend U/V before speed/direction; test calm/signed zero/wraparound.
3. Enforce finite bounds and dew-point consistency.
4. Property-test convex scalar bounds and vector reconstruction.

**Commit:** `feat: blend temperature dew point and wind`

### Task 9: Implement gust, QPF, and PoP operators

**Files:** `forecasting/gust_blend.py`, `precipitation_blend.py`, `consistency.py`, unit/property/scientific tests.

1. Test every precipitation fallback row and exact interval equality.
2. Test gust floor with full explanation and material source contradiction rejection.
3. Test NBM-only PoP, unavailable PoP degradation, bounds, and no deterministic-to-probability synthesis.
4. Test probability/QPF tension flags without value mutation.
5. Mutation probes: negative QPF clipping, arbitrary PoP fallback, interval mismatch, average gust without consistency floor.

**Commit:** `feat: blend gust precipitation and probability`

### Task 10: Build contribution and forecast schemas

**Files:** `contracts/forecasts.py`, `contracts/lineage.py`, `forecasting/contributions.py`, `baseline.py`, `validation.py`, contract/unit/property tests.

1. Define and validate all schemas in Section 5.1 without changing v1 semantics.
2. Reconstruct every output from contribution rows and reject missing/duplicate/extra rows, wrong weights, wrong parents, or tolerance mismatch.
3. Generate `uncorrected-blend-forecast.v1`, explicit zero correction, and `baseline-forecast.v2` exactly once.
4. Test atomic dual output and lineage ancestors.

**Commit:** `feat: explain and assemble the Phase 2 baseline`

### Task 11: Extend METAR normalization for dew point, gust, and hourly precipitation

**Files:** `contracts/observations.py`, `observations/normalization.py`, `quality.py`, AviationWeather fixtures/tests.

1. Test strict dew point/gust parsing and conversions, missing gust, raw `P0000`, nonzero amounts, malformed groups, SPECI exclusion, missing-not-zero, corrections, and cutoff selection.
2. Preserve exact provider/raw bytes and record index on every normalized row.
3. Add exact one-hour interval and quality/missing reasons.
4. Mutation probes: treat absent P group as zero, accept SPECI, shift interval, use a post-cutoff correction.

**Commit:** `feat: normalize Phase 2 station truth`

### Task 12: Match and verify every Phase 2 variable

**Files:** `contracts/verification.py`, `verification/matching.py`, `metrics.py`, `validation.py`, unit/property/scientific tests.

1. Extend full-coverage rows to 3 locations x 36 horizons x named fields with mutually exclusive statuses.
2. Test exact interval/end-time precipitation matching and all as-of revision cases.
3. Hand-calculate every metric, threshold contingency table, Brier score, decile bin, and null precondition.
4. Property-test RMSE >= MAE >= abs(bias), bounded Brier/AUC, contingency identities, and JSON finiteness.
5. Stratify by availability state and label conditional gust metrics.

**Commit:** `feat: verify the Phase 2 baseline`

### Task 13: Compose the Phase 2 application use case

**Files:** `application/phase2.py`, `phase2_adapters.py`, focused unit/integration tests.

1. Define strict request/result/port contracts with target reference, issue/cutoff times, config/code/environment/lock digests, and typed IDs.
2. Compose the Section 5.3 lifecycle only from injected ports/pure functions.
3. Test failure at every boundary, no late input selection, atomic blend outputs, identity correction exactly once, replay, and concurrent idempotency.
4. Prove the coordinator contains no provider selectors, formulas, interpolation, weights, or metric logic.

**Commit:** `feat: compose the Phase 2 forecast workflow`

### Task 14: Prove source-failure behavior offline

**Files:** `tests/acceptance/test_phase2_multimodel_baseline.py`, generated fixtures/oracles, real-service integration tests.

Against real PostgreSQL and standalone MinIO, prove:

1. exact configuration, all source roots, cycles, messages, digests, and cutoff eligibility;
2. all three canonical model artifacts and 3 x 36 aligned station values;
3. complete baseline with reconstructable contributions;
4. HRRR failure selects exact NBM+GFS rows;
5. GFS failure selects exact HRRR+NBM rows;
6. NBM failure selects HRRR+GFS for deterministic variables and emits explicit unavailable PoP/degraded run;
7. two-model failures select approved single-model rows where possible and correct invalid/degraded status;
8. partial-cycle data rejects the entire model rather than mixing;
9. observation normalization/matching/metrics and missing truth;
10. lineage reaches exact indexes/messages/station/METAR roots;
11. physical and logical replay equality; concurrent identical requests reuse activities/artifacts;
12. no RRFS, AI, learned bias, publication, or network path is imported/invoked.

**Commit:** `test: prove Phase 2 multi-model failure survival`

### Task 15: Add opt-in live contract canaries

**Files:** `tests/live/test_nbm_contract_live.py`, `test_gfs_contract_live.py`, extend HRRR live test, operations docs.

- Require `MESOFORGE_LIVE_TESTS=1` and explicit recent cycles.
- Fetch one index and minimum ranged messages into pytest temporary directories only.
- Assert product naming, fields/decoded keys/grid/units, NBM PoP01 semantics, GFS duplicate-APCP policy, and one-hour QPF contracts.
- Bound requests, retries, bytes, and time; no meteorological value assertions.
- Default collection skips before network construction.

**Commit:** `test: add live NBM and GFS contract canaries`

### Task 16: Documentation and final hardening

**Files:** create `docs/data-contracts/phase-2.md`; update `docs/architecture/v1.md`, vocabulary, local-development docs, README, Makefile/CI/hygiene only as required.

1. Document exact contracts, equations, tables, graph, replay, source evidence, limitations, and disabled publication/precipitation type.
2. Link Phase 2 status from canonical architecture without duplicating or rewriting it.
3. Keep downloaded GRIB/index/cfgrib caches prohibited from Git.
4. Add offline Phase 2 acceptance/scientific targets to CI real-service jobs.

**Commit:** `docs: document the Phase 2 multi-model baseline`

## 9. Verification gates

Focused tests run after every task. Before push/review, run:

```text
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src scripts
uv run lint-imports
uv run python scripts/validate_docs.py
uv run python scripts/check_repository_hygiene.py
uv run pytest tests/unit tests/contracts tests/property -q
uv run pytest -m scientific -q
uv run pytest --cov=mesoforge --cov-report=term-missing --cov-fail-under=90 -q
```

Real-service gates:

```text
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
uv run pytest -m integration tests/integration tests/acceptance -q
```

Live checks are manual and never gate default CI:

```text
MESOFORGE_LIVE_TESTS=1 \
MESOFORGE_LIVE_HRRR_CYCLE=YYYYMMDDTHH \
MESOFORGE_LIVE_NBM_CYCLE=YYYYMMDDTHH \
MESOFORGE_LIVE_GFS_CYCLE=YYYYMMDDTHH \
uv run pytest -m live tests/live -q
```

Required targeted mutation evidence must cover at least: cycle ordering/cutoff, partial-cycle splicing, NBM percent conversion, NBM wind convention, GFS APCP duplicate selection/differencing, wind rotation/interpolation order, interval matching, silent fallback renormalization, PoP synthesis, QPF negative handling, contribution reconstruction, observation absent-P-as-zero, revision cutoff, and correction applied twice.

GitHub Actions must run quality, unit/contract/property/scientific/coverage, real PostgreSQL/MinIO integration/acceptance, and migration round trip against the exact pushed implementation head. Approval requires local head, remote branch head, and every reported CI head SHA to match.

## 10. Release-blocking acceptance criteria

- Exact base/planning/implementation branch and pushed head are reported; no merge occurs.
- Every retained source byte and selected GRIB message has verified content identity and exact index/range/decode lineage.
- Every provider field, level, unit, grid, cycle, valid/interval time, and probability event passes the source contract.
- All 3 stations x 36 horizons are represented with explicit per-variable availability.
- No temporal interpolation, per-variable cycle splice, silent fallback renormalization, or arbitrary surviving-contributor blend occurs.
- Wind is blended as earth-relative U/V; QPF intervals are exact; PoP remains NBM-only.
- Every value is independently reconstructable from contribution explanations.
- Uncorrected blend, identity correction, and baseline are distinct; correction is applied exactly once.
- Simulated failure of each single source yields the exact expected complete/degraded outcome; partial source data never masquerades as a contributor.
- NBM outage leaves PoP explicitly unavailable and does not synthesize probability.
- Verification preserves event-time/availability cutoffs, explicit missing truth, metric preconditions, and finite JSON.
- Phase 1 behavior remains green and replayable.
- Default CI is offline; live checks are opt-in; no downloaded forecast/cache artifact enters Git.
- Coverage is >=90%; quality, scientific, integration, acceptance, migrations, mutation probes, and exact-head CI pass.
- RRFS, precipitation type, AI, learned weights/bias, publication, and operational-skill claims remain disabled.

## 11. Risks and explicit tradeoffs

1. **NBM is itself a blend:** HRRR/GFS information may be correlated with NBM. Fixed weights are transparent but not statistically independent. No skill-optimal claim is made; Phase 3 backtesting must evaluate alternatives.
2. **GFS duplicate precipitation records:** inventory text can be ambiguous. Dual-record equivalence plus decoded start/end-step selection is release-blocking; record order is forbidden.
3. **PoP has one approved contributor:** NBM failure cannot yield PoP. Explicit degraded output is safer than manufacturing probability from deterministic QPF.
4. **Station/grid representativeness:** bilinear source-grid estimates and airport observations differ in scale/elevation/exposure. Results are diagnostic only.
5. **Precipitation truth sparsity:** missing METAR `Prrrr` is common and is not zero. PoP/QPF sample size and selection are reported; no promotion claim follows.
6. **Gust truth selection:** explicit METAR gusts are conditionally reported. Gust metrics are diagnostic and labeled, not unbiased overall gust skill.
7. **Product drift:** operational NBM version and GRIB metadata can change without filename changes. Strict live canaries fail closed and require a contract/configuration revision.
8. **Cycle age and deadlines:** policies balance completeness against freshness and are not provider SLO claims. Every chosen source age and rejection is explained.
9. **Projected/native spatial support:** direct native-grid interpolation avoids a costly common-grid system but produces station estimates, not a gridded blend or conservative remap.
10. **Combinatorial artifacts:** exact per-message roots and explanations increase metadata volume. This is accepted for replayability; large arrays remain in object storage.
11. **Scope pressure:** precipitation type, RRFS, publication, learned blends, and bias correction need independent evidence/contracts. They are not implementation shortcuts in this phase.

## 12. Authoritative evidence used to pin the contract

- NCEP GFS/GDAS product inventory and filename/cycle/horizon contract: `https://www.nco.ncep.noaa.gov/pmb/products/gfs/`.
- GFS documentation: `https://vlab.noaa.gov/web/gfs/documentation`.
- NBM download/product naming: `https://vlab.noaa.gov/web/mdl/nbm-download`.
- NBM weather elements: `https://vlab.noaa.gov/web/mdl/nbm-weather-elements`.
- NBM weather-element definitions: `https://weather.gov/mdl/nbm_elem_def`.
- NBM/NDFD verification definitions for PoP01/QPF01: `https://vlab.noaa.gov/web/mdl/ndfd-verification-help`.
- NOAA GRIB2 parameter table 4.2-0-1 for APCP units/identity: `https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-1.shtml`.
- NOAA CPC GFS duplicate/merged accumulation explanation: `https://www.cpc.ncep.noaa.gov/products/wesley/wgrib2/unmerge_fcst.html`.
- Live operational inventories inspected for 2026-08-29 12Z:
  - NBM `blend.t12z.core.f001.co.grib2.idx` and `f036`;
  - GFS `gfs.t12z.pgrb2.0p25.f001`, `f005`, and `f006` indexes;
  - HRRR `hrrr.t12z.wrfsfcf01.grib2.idx` and `f36`.
- AviationWeather public API schema v4.0: `https://aviationweather.gov/data/schema/openapi.yaml`.

If a live inventory conflicts with this plan at implementation time, fail the live canary and request Codex review. Never silently adjust selectors, thresholds, time intervals, weights, or fallback behavior.

## 13. Implementation review handoff

The implementation handoff must include:

- exact planning commit, implementation base/head, branch, remote head, and pushed-not-merged state;
- changed files and any dependency/lockfile changes;
- pass counts for focused, unit/contract/property, scientific, full coverage, integration, acceptance, migration, and mutation gates;
- full coverage percentage;
- complete/HRRR-failed/NBM-failed/GFS-failed acceptance outcomes;
- exact GitHub Actions run/job URLs and verified head SHA;
- live canary status explicitly run/skipped with reason;
- deviations, scientific risks, and disabled paths.

Any deviation in product family, selector/GRIB identity, probability event, interval semantics, source-cycle selection, spatial method, weights, fallback row, consistency rule, truth source, or metric requires Codex design approval before implementation continues.
