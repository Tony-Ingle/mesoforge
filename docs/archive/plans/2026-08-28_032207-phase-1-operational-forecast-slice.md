> **Historical plan (archived 2026-09-09):** This completed Phase 0-2 development plan is retained for reference. Its agent instructions and delivery checklist are not current work orders. Follow the current owner request and root AGENTS.md; see the [archive index](../README.md). Original path: .hermes/plans/2026-08-28_032207-phase-1-operational-forecast-slice.md. The original contents follow unchanged.

# MesoForge Phase 1 Operational Forecast Slice Implementation Plan

> **For Hermes:** Implement this plan task-by-task with TDD and request Codex review after the implementation branch is pushed. Do not broaden the slice or merge it.

**Goal:** Deliver one replayable, deterministic Grasston, Minnesota forecast-and-verification run from exact NOAA HRRR source messages through an HRRR-only station baseline, revision-aware AviationWeather.gov METAR matching, metrics, immutable artifacts, and complete lineage.

**Architecture:** Extend the Phase 0 modular monolith with narrow `guidance`, `alignment`, `forecasting`, `observations`, and `verification` domains. External bytes enter only as source artifacts; every scientific step is a pure, versioned transformation executed through the existing application artifact service, with exact inputs, configuration, code/environment identity, availability, and activity edges retained. Keep the slice station-first and deterministic: no general blender, scheduler, API, publication workflow, bias model, or AI boundary is introduced.

**Tech stack:** Python 3.12, Pydantic v2, xarray/NumPy, Pint, pyproj, Herbie/cfgrib/eccodes for pinned HRRR inventory and decoding, PyArrow/Parquet for tabular observations and matches, h5netcdf for array artifacts, PostgreSQL, S3/MinIO, pytest/Hypothesis, uv.

**Exact base:** `ae09f063207d4bb952caffb83d9ce6ea4512fbbb` (`origin/main` when this plan was authored).

---

## 1. Scope and fixed decisions

### 1.1 Included

- Domain ID: `grasston-minnesota.v1`.
- Authoritative domain center: latitude `45.80265`, longitude `-93.07956` (WGS84). The configuration validator requires this exact point and verifies it is the arithmetic center of the configured rectangle.
- Bounding box, inclusive and in WGS84 longitude convention `[-180, 180]`:
  - south `45.05265`
  - north `46.55265`
  - west `-94.07956`
  - east `-92.07956`
- Station set is exactly `KCBG`, `KJMR`, and `KROS`, in that canonical lexicographic order. Baseline values are generated for all three configured stations. Observation absence is explicit missingness, not a reason to silently change the station set.
- HRRR CONUS surface product, one explicitly requested hourly cycle, leads exactly `PT0H` through `PT6H` in one-hour increments.
- Source fields only:
  - 2 m temperature
  - 10 m U wind component
  - 10 m V wind component
- AviationWeather.gov decoded METAR/SPECI observations for the same station set, preserving exact response bytes, observation time, report time, provider receipt/availability time, local ingestion time, station metadata, decoded values, opaque provider QC field, raw report, and revision identity.
- Deterministic discovery, acquisition, byte identity, GRIB selection, decoding, unit normalization, grid-to-earth wind rotation when required, spatial subsetting, station interpolation, HRRR-only baseline generation, observation normalization, matching, and verification.
- Verification:
  - temperature bias, MAE, RMSE
  - eastward-wind-component bias, MAE, RMSE
  - northward-wind-component bias, MAE, RMSE
  - wind-speed bias, MAE, RMSE
  - per-pair absolute circular wind-direction error and its mean, only when both observed and forecast speeds meet the calm threshold
  - sample counts and explicit missingness counts for every metric
- Offline deterministic fixtures in normal CI and opt-in live provider smoke tests.

### 1.2 Explicitly excluded

- AI calls, recommendations, adjustments, policy, approval, or publication.
- Learned or rolling bias correction.
- PoP, QPF, precipitation type, dew point, wind gust, diagnostics beyond wind speed/direction needed for verification.
- NBM, GFS, RRFS, ensembles, multi-model blending, arbitrary availability fallbacks.
- Temporal interpolation, vertical interpolation, terrain/lapse-rate correction, generic regridding, PostGIS, a scheduler/orchestrator, API/UI work, or automatic cycle discovery.
- National/full-CONUS retained canonical arrays. The raw selected GRIB messages remain exact source bytes; derived arrays retain only the Grasston bounding box plus interpolation halo.

### 1.3 Material design choices

1. **The baseline is a distinct artifact even though it is an identity single-model baseline.** This preserves the architecture’s separation between raw guidance and a MesoForge forecast layer without inventing a generic blend.
2. **Station interpolation is bilinear in the native HRRR projected grid.** No nearest-neighbor fallback and no extrapolation are permitted. All four corners must exist and be scientifically valid.
3. **HRRR wind is compared to METAR only after conversion to earth-relative east/north components.** The decoder must inspect `GRIB_uvRelativeToGrid`; grid-relative vectors are rotated before interpolation. Treating grid-relative HRRR values as earth-relative is a release-blocking error.
4. **No new relational domain tables are required in Phase 1.** Station catalogs, normalized observations, matched pairs, and verification results are immutable artifacts. PostgreSQL continues to store generic artifact/activity/run metadata. A migration is therefore not planned; implementation must not add one unless a reviewed contract proves artifact storage insufficient.
5. **Parquet is used for observation and matching tables.** Canonical JSON is used for small inventories, lineage manifests, coverage reports, and metric summaries. NetCDF/h5netcdf is used for guidance and baseline arrays.
6. **AviationWeather decoded JSON is the observation payload.** `qcField` is preserved as opaque provider metadata because its bit semantics are not documented in the public OpenAPI contract; MesoForge applies only its own named, deterministic range/completeness checks.
7. **The run pins only forecast-time inputs.** The 14 HRRR index/selected-message source artifacts and one pre-normalized station-catalog snapshot are pinned in `RunManifest.selected_input_artifact_ids`; the snapshot retains the station-response source as its parent. METAR truth arrives after forecast issuance and is linked by verification activities, not retroactively added to run inputs.
8. **No arbitrary cycle fallback.** A request identifies one HRRR cycle. Missing lead/field data eventually fails that run; it never substitutes an older cycle.

### 1.4 Grasston station selection

Station membership is a planning-time, versioned decision, not a dynamic per-run nearest-station query. On 2026-08-28 the AviationWeather station catalog and recent METAR response were inspected for `KCBG`, `KJMR`, `KROS`, `KPNM`, and `KANE`. The selected set is the closest useful three-station geographic bracket around the authoritative center while remaining well inside the bbox:

| Station | AviationWeather site | latitude | longitude | elevation (m) | relation to center |
|---|---|---:|---:|---:|---|
| `KCBG` | Cambridge Muni | 45.55700 | -93.26400 | 285 | southwest |
| `KJMR` | Mora Muni | 45.88854 | -93.26900 | 301 | northwest |
| `KROS` | Rush City Rgnl | 45.69624 | -92.95427 | 282 | southeast |

All three records declare `siteType` containing `METAR`, and all three returned recent temperature and wind reports during the planning check. `KPNM` and `KANE` are valid fallbacks for a future version but are farther from the center and are not members of `grasston-minnesota.v1`. Configuration pins the three IDs and expected metadata; the retained provider response remains authoritative for the effective snapshot. Any membership change requires a new reviewed domain/configuration version and digest, never silent nearest-station reselection.

Planning evidence:

- Station metadata: <https://aviationweather.gov/api/data/stationinfo?ids=KCBG,KJMR,KROS,KPNM,KANE&format=json>
- Recent-report contract check: <https://aviationweather.gov/api/data/metar?ids=KCBG,KJMR,KROS,KPNM,KANE&format=json&hours=1>

---

## 2. Authoritative external contracts

The implementation must encode these values as validated configuration, not scatter string literals across adapters.

### 2.1 HRRR product, endpoints, files, and selectors

Model/template/product:

- Herbie model: `hrrr`
- Herbie product: `sfc`
- sector: `conus`
- cycle frequency: hourly
- forecast hours: integer `0..6`
- file: `hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2`
- index: same URL plus `.idx`

Primary source, auth-free NOAA Open Data on AWS:

```text
https://noaa-hrrr-bdp-pds.s3.amazonaws.com/
  hrrr.{YYYYMMDD}/conus/hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2
https://noaa-hrrr-bdp-pds.s3.amazonaws.com/
  hrrr.{YYYYMMDD}/conus/hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2.idx
```

Secondary source for recent operational data only, after the primary source’s bounded retries are exhausted:

```text
https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/
  hrrr.{YYYYMMDD}/conus/hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2
https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/
  hrrr.{YYYYMMDD}/conus/hrrr.t{HH:02d}z.wrfsfcf{FF:02d}.grib2.idx
```

Never treat endpoint order as implicit Herbie behavior. Configure the order as `aws`, then `nomads`; record the resolved endpoint and URL in the source manifest. Archived/backtest requests may require AWS and must not silently fail over to a source with shorter retention.

Exact wgrib2 inventory expressions:

```text
:TMP:2 m above ground:(anl|[0-9]+ hour fcst):
:UGRD:10 m above ground:(anl|[0-9]+ hour fcst):
:VGRD:10 m above ground:(anl|[0-9]+ hour fcst):
```

Additional semantic assertions after inventory match/GRIB decode:

| Canonical variable | Inventory parameter/level | GRIB discipline/category/number | `typeOfLevel` | level | expected units |
|---|---|---:|---|---:|---|
| `air_temperature_2m` | `TMP:2 m above ground` | `0/0/0` | `heightAboveGround` | `2` | `K` |
| `eastward_wind_10m` source | `UGRD:10 m above ground` | `0/2/2` | `heightAboveGround` | `10` | `m s-1` |
| `northward_wind_10m` source | `VGRD:10 m above ground` | `0/2/3` | `heightAboveGround` | `10` | `m s-1` |

Lead 0 must end in inventory step type `anl`; lead `N > 0` must end in exactly `N hour fcst`. Every field must match exactly one inventory row per file. Zero or duplicate matches are terminal scientific validation errors.

Pinned cfgrib behavior:

- decode only the selected, concatenated GRIB messages; never search the remote inventory during replay
- `engine="cfgrib"`
- `backend_kwargs={"indexpath": "", "errors": "raise", "read_keys": [...]}`
- `read_keys` must include at least `discipline`, `parameterCategory`, `parameterNumber`, `typeOfLevel`, `level`, `stepType`, `uvRelativeToGrid`, `gridType`, `Nx`, `Ny`, `DxInMetres`, `DyInMetres`, `latitudeOfFirstGridPointInDegrees`, `longitudeOfFirstGridPointInDegrees`, `LoVInDegrees`, `Latin1InDegrees`, and `Latin2InDegrees`
- do not persist cfgrib `.idx` caches
- record exact Herbie, cfgrib, eccodes, xarray, NumPy, and pyproj versions in the acquisition/normalization manifest in addition to the environment digest

The plan’s selectors were checked against NOAA’s live index format: f00 uses `anl`, and f01 uses `1 hour fcst` for all three named rows. Source references:

- NOAA NCEP HRRR product inventory: <https://www.nco.ncep.noaa.gov/pmb/products/hrrr/>
- NOAA Open Data AWS HRRR registry: <https://registry.opendata.aws/noaa-hrrr-pds/>
- NOAA NOMADS HRRR filter: <https://nomads.ncep.noaa.gov/gribfilter.php?ds=hrrr_2d>

### 2.2 Byte-range acquisition

Do not download or retain the entire approximately 150 MB surface file for this slice.

For each lead:

1. GET and retain exact `.idx` response bytes as `hrrr-grib-index`.
2. Parse the three exact rows and the following row’s start offset.
3. HEAD the GRIB object to obtain content length when a selected row is last.
4. Issue HTTP Range requests for the exact selected messages.
5. Concatenate selected messages in original source-offset order into one valid multi-message GRIB2 payload.
6. Validate every range length, `Content-Range`, GRIB header/trailer, and decoded key set.
7. Retain the exact concatenated bytes as `hrrr-selected-grib`.
8. Retain full-object ETag, Last-Modified, Content-Length, selected message numbers, byte ranges, exact inventory rows, requested selector, and resolved URL in the acquisition manifest.

The full remote file ETag is provenance metadata, not a SHA-256 content digest. MesoForge’s `content_digest` always hashes the exact retained bytes. If a provider ignores `Range` and returns status 200/full content, reject the response rather than accidentally retaining the full product.

Source identity is logical and stable across mirrors:

```text
source_authority = "noaa.hrrr"
source_locator   = "hrrr://conus/sfc/{YYYYMMDDTHHZ}/f{FF:02d}/{index|selected-grib}"
source_revision  = "etag=<full-object-etag>;length=<full-object-length>"
```

The resolved HTTPS URL remains in artifact attributes. This permits identical logical source registration across an AWS/NOMADS mirror while preserving which endpoint actually served the bytes.

### 2.3 HRRR retry and availability policy

- Connect timeout: 10 seconds.
- Read timeout: 60 seconds.
- Attempts per endpoint: 4.
- Deterministic backoff: 1, 2, 4, and 8 seconds; no random jitter.
- Retry connection/timeouts, HTTP 408/425/429, and 5xx.
- A 404 is retryable only until the configured cycle-availability deadline (`cycle + 90 minutes`); after that it is terminal missing source data.
- Honor integer `Retry-After`, capped at 60 seconds.
- Retry an integrity/range mismatch once from a fresh connection; then fail closed. Never accept differing bytes from repeated requests without recording a distinct source revision.
- After all attempts on AWS fail with a retryable availability/transport failure, try NOMADS under the same rules. Do not fail over on selector ambiguity, decode error, or semantic key mismatch.
- `created_at`: HTTP `Last-Modified` when valid, otherwise response-completion time with the fallback recorded.
- source `available_at`: completion time of the first successful exact-byte response observed by this MesoForge ingestion, authority `mesoforge.observed-provider-availability`, method `first-successful-http-response.v1`.
- `ingested_at`: local response-completion time.
- Preserve response `Date`, `ETag`, `Last-Modified`, and request-attempt history in bounded artifact metadata/manifest JSON, not logs alone.

### 2.4 AviationWeather.gov endpoints and fields

Base: `https://aviationweather.gov`

Station metadata:

```text
GET /api/data/stationinfo?ids=KCBG,KJMR,KROS&format=json
```

METAR batch for a completed six-hour forecast window:

```text
GET /api/data/metar
  ?ids=KCBG,KJMR,KROS
  &format=json
  &date={cycle_plus_6h_plus_15m_as_ISO8601_UTC}
  &hours=6.5
```

Canonical query ordering is `ids`, `format`, `date`, `hours`. Set a descriptive MesoForge user agent. Do not exceed one request per minute per process; honor the documented global limit of 100 requests/minute. The public API returns at most 400 records for most endpoints; this bounded query is far below that limit.

Required/preserved METAR JSON fields:

- `icaoId`
- `obsTime` (event time, Unix seconds)
- `reportTime`
- `receiptTime` (provider availability for this revision)
- `temp` (degrees Celsius, nullable)
- `wdir` (degrees from true north or string `VRB`, nullable)
- `wspd` (knots, nullable)
- `qcField` (opaque, nullable)
- `metarType`
- `rawOb`
- `lat`, `lon`, `elev`

Reject an unknown station ID, malformed timestamp, non-UTC time, nonfinite numeric value, or schema type mismatch. Preserve unknown extra JSON fields in the raw artifact but ignore them in `metar-observation.v1` until explicitly versioned.

HTTP handling:

- 200: retain exact body even if the JSON array is empty.
- 204: retain a canonical empty-response source artifact with status/headers and produce explicit missingness.
- 400/403/404: terminal request/configuration errors.
- 429/500/502/504 and transport timeouts: bounded retries using the same 1/2/4/8-second policy and `Retry-After` cap.

`receiptTime` is the provider availability time for each record revision. The raw batch artifact’s availability is response completion, because the exact batch bytes did not exist as one MesoForge-selectable object earlier. Local `ingested_at` is separately retained. Source references:

- API/OpenAPI: <https://aviationweather.gov/data/api>
- OpenAPI YAML: <https://aviationweather.gov/data/schema/openapi.yaml>

---

## 3. Scientific and data contracts

### 3.1 Typed identifiers and strictness

Add only the missing domain identity needed by this slice:

- `StationId`, canonical lowercase kebab/dot form (`station.kcbg`, `station.kjmr`, `station.kros`)
- `MatchingPolicyId`, canonical lowercase kebab/dot form
- `MetricSetId`, canonical lowercase kebab/dot form

Continue using existing `ArtifactId`, `ActivityId`, `RunId`, `ConfigurationSnapshotId`, `GridId`, `VariableId`, `VerticalDefinitionId`, and `Digest`. Public functions, Pydantic fields, injected protocols, repositories, and aggregate containers must not downgrade these to unrestricted `str`. Every new Pydantic model uses `extra="forbid"`, `frozen=True`, and `strict=True`.

Observation logical identity and revision identity are digests, not generated UUIDs:

```text
logical_observation_digest = SHA256(JCS([
  "aviationweather.gov", station_id, obs_time
]))
revision_digest = SHA256(JCS([
  logical_observation_digest, receipt_time, canonical_provider_record
]))
```

The same logical observation may have multiple append-only revisions. Exact duplicate revisions deduplicate by digest.

### 3.2 Catalog/configuration additions

Create immutable definitions for:

- `DomainDefinition`: ID, authoritative center, bbox, CRS (`EPSG:4326`), longitude convention, station IDs. Center, bbox, and membership are configuration-digested; validation requires the center to be exactly the rectangle midpoint.
- `StationDefinition`: `StationId`, provider ICAO ID, latitude, longitude, elevation metres, site name/type, provider priority, nullable exposure/instrument identity, station metadata artifact ID, effective start/end. The Phase 1 station snapshot derives these values from the retained stationinfo response and fails if an expected station is absent, lacks `METAR` in `siteType`, or lies outside the bbox. Because AviationWeather does not expose exposure/instrument identity here, both are explicitly `unknown` rather than invented.
- `HrrrSourceSettings`: exact templates/selectors, endpoint order, leads, retry/deadline settings, bbox, field assertions.
- `AviationWeatherSettings`: endpoint, station IDs, lookback, rate/retry settings.
- `PointExtractionPolicy`: `bilinear-native-grid.v1`, no extrapolation, four-corner finite requirement, one-cell halo.
- `ObservationNormalizationPolicy`: units/formulas/range checks.
- `MatchingPolicy`: ID `metar-nearest-15m.v1`, symmetric inclusive tolerance 15 minutes, as-of policy, tie-break rules, calm threshold.
- `MetricSet`: ID `phase1-temperature-wind.v1` and exact formula names.

Extend `MesoForgeConfiguration` with optional Phase 1 settings so Phase 0 configuration remains valid. Add a complete `configs/phase1-grasston.yaml`; do not mutate `configs/base.yaml` into a Phase 1-only file.

Station coordinates are sourced from the effective stationinfo artifact, not duplicated as unchecked mutable constants. The configuration pins expected station IDs, planning-time expected coordinates/elevations, authoritative center, and geographic bounds; the normalized snapshot pins the exact effective coordinates used by the run. A station response is acquired, registered, and normalized before forecast-run creation, with source `available_at` equal to first successful response completion and local `ingested_at` retained separately. The run may select only a normalized station snapshot whose source and derived availability are at or before `information_cutoff`; it never fetches or normalizes station metadata retroactively after the cutoff.

For this prospective Phase 1 slice, a snapshot is effective from its source `available_at` until a later reviewed snapshot supersedes it. Matching requires observation event time inside that interval. Historical backfill before the first retained effective snapshot fails closed. Snapshot coordinates govern HRRR point extraction and station identity; per-record METAR `lat`/`lon`/`elev` are retained as lineage and must differ by no more than `0.02` degree on either coordinate and `30 m` elevation. A larger difference rejects that observation revision with `station_metadata_conflict`; it never moves the forecast point silently.

### 3.3 Variable and vertical definitions

Add/validate:

| Variable | canonical unit | temporal semantics | spatial support | vertical definition | dimensions |
|---|---|---|---|---|---|
| `air_temperature_2m` | `K` | instantaneous | point | `height-agl-2m` | guidance `(lead_time,y,x)`; baseline `(lead_time,location)` |
| `eastward_wind_10m` | `m s-1` | instantaneous | point | `height-agl-10m` | same variants |
| `northward_wind_10m` | `m s-1` | instantaneous | point | `height-agl-10m` | same variants |
| `wind_speed_10m` | `m s-1` | instantaneous | point | `height-agl-10m` | baseline `(lead_time,location)` |
| `wind_from_direction_10m` | `degree` | instantaneous | point | `height-agl-10m` | baseline `(lead_time,location)` |

`height-agl-10m` is an immutable vertical definition. Direction is meteorological “from” direction clockwise from true north in `[0, 360)` and is undefined at exactly zero speed.

If the existing `VariableDefinition` cannot express both permitted dimension variants for one identity, extend its configured `allowed_dimension_variants`; do not create support-suffixed fake meteorological variable IDs.

### 3.4 Variable lineage manifest

The existing canonical dataset requires `variable_lineage_manifest_id`, but Phase 0 has no concrete contract. Add `VariableLineageManifest` (`variable-lineage.v1`) before HRRR normalization.

For every canonical variable and lead it records:

- source index artifact ID
- selected-GRIB artifact ID
- logical source cycle/product/lead
- exact original URL and full-object revision metadata
- inventory message number, byte start/end, exact inventory row
- GRIB identifying keys
- selector expression
- decode backend and arguments
- unit conversion (identity for HRRR’s K and m/s)
- wind rotation policy and grid-relative flag
- source/output grid IDs and spatial subset indices

The lineage manifest is produced first from the 14 source artifacts. Canonical guidance then consumes the lineage artifact plus selected GRIB artifacts and stores that real `ArtifactId` in `variable_lineage_manifest_id`. The manifest maps logical output slices; it does not need to know the future canonical-guidance artifact ID and therefore creates no cycle.

### 3.5 Canonical HRRR guidance artifact

Artifact schema: existing `canonical-guidance.v1` with these tightened Phase 1 requirements:

- `forecast_reference_time`: requested HRRR cycle, scalar UTC-naive `datetime64[ns]` plus dataset `time_encoding="UTC"`
- `lead_time`: exactly `[0h,1h,2h,3h,4h,5h,6h]`
- `valid_time`: exact reference plus lead
- dimensions: `(lead_time,y,x)`
- projected `x`,`y`; 2-D `latitude`,`longitude`
- bbox subset includes all grid points needed for the requested bbox plus exactly one-cell interpolation halo
- variables: temperature, earth-relative U, earth-relative V, each float32 with uint16 quality mask
- source projection/GridDefinition is immutable and digest-validated
- no missing lead, field, coordinate, or interpolation halo is accepted for a valid Phase 1 artifact

Grid-to-earth rotation:

1. Inspect `uvRelativeToGrid` on both U and V and require equality.
2. If already earth-relative, retain values and record identity rotation.
3. If grid-relative, derive local projected-grid unit bases deterministically with pyproj:
   - inverse-project `(x,y)`, `(x+dx,y)`, `(x,y+dy)`
   - use `pyproj.Geod.inv` azimuths clockwise from north
   - the east/north components of a unit basis at azimuth `a` are `(sin(a), cos(a))`
   - `u_east = u_grid * x_basis_east + v_grid * y_basis_east`
   - `v_north = u_grid * x_basis_north + v_grid * y_basis_north`
4. Validate each basis norm with absolute error `<= 1e-12` and absolute basis dot product `<= 1e-3`; nonfinite values or a larger error fail closed. These constants belong to `grid-to-earth-pyproj.v1` configuration and its digest.
5. Rotate every source grid point before bilinear interpolation.

Never infer the rotation flag from product name alone.

### 3.6 Station point extraction and baseline artifact

Bilinear extraction is performed in native projected x/y space:

- transform each station WGS84 coordinate into the source projection
- find the enclosing cell with monotonic-coordinate-safe search
- require station inside the four cell centers’ rectangle and inside retained coverage
- calculate double-precision weights; require each in `[0,1]` and sum to one within `1e-12`
- require all four temperature/U/V values finite and unmasked
- interpolate temperature, earth-relative U, and earth-relative V independently
- record source `(y,x)` indices, coordinates, values, weights, station point, algorithm/library version, and no-extrapolation status in `point-extraction-report.v1`

No terrain adjustment is applied. The report explicitly labels `station_elevation_policy="ignored-in-phase1"`.

The immutable `point-extraction-report.v1` artifact is the architecture-required spatial-weight artifact; its `ArtifactId` is referenced by the baseline and lineage, so a redundant standalone weight file is not created. The retained guidance subset is the smallest source-grid rectangle covering the inclusive domain bbox plus one complete source cell on every side. A station on a bbox edge therefore still requires an enclosing four-corner source cell inside the halo; a station on the outer retained-grid edge, or any point for which such a cell cannot be formed, fails with `outside_interpolation_coverage` rather than extrapolating.

Baseline artifact: `baseline-forecast.v1`, NetCDF, dimensions `(lead_time,location)`:

- locations exactly `station.kcbg`, `station.kjmr`, `station.kros` in canonical order
- values for temperature, U, V
- derived `wind_speed_10m = hypot(u,v)`
- derived wind-from direction `mod(degrees(atan2(-u,-v)),360)` when speed > 0; otherwise NaN with a direction-undefined quality bit
- `forecast_issue_time` from the run
- HRRR source reference time retained as contributor metadata, not substituted for issue time
- contributor artifact ID and extraction-report artifact ID
- every expected station/lead/variable must be present and valid; otherwise baseline generation fails closed

### 3.7 Observation record and Parquet schema

Artifact: `normalized-metar-observations.v1`, sorted by `(station_id,event_time,provider_available_at,revision_digest)`.

Required columns:

- `logical_observation_digest`
- `revision_digest`
- `station_id`
- `provider_station_id`
- `event_time` from `obsTime`
- `report_time`
- `provider_available_at` from `receiptTime`
- `ingested_at`
- `metar_type`
- `raw_observation`
- `raw_record_digest`
- `raw_artifact_id`
- `raw_record_index`
- `station_snapshot_artifact_id`
- `latitude_degrees`, `longitude_degrees`, `elevation_m`
- `temperature_k` nullable
- `wind_speed_m_s` nullable
- `wind_from_direction_degrees` nullable
- `eastward_wind_10m_m_s` nullable
- `northward_wind_10m_m_s` nullable
- `provider_qc_field` nullable
- `mesoforge_qc_state` (`eligible`, `partial`, `rejected`)
- deterministic sorted `quality_flags`

Conversions:

```text
temperature_k = temperature_degC + 273.15
wind_speed_m_s = wind_speed_knots * 0.5144444444444445
u = -wind_speed_m_s * sin(direction_degrees)
v = -wind_speed_m_s * cos(direction_degrees)
```

Rules:

- `wspd == 0`: speed and U/V are zero; direction is null/undefined even if source supplies one.
- `wdir == "VRB"` with positive speed: retain speed, direction/U/V are null, flag `variable_wind_direction`.
- missing direction with positive speed: same partial behavior.
- missing speed: all wind-derived fields null.
- missing temperature: temperature null.
- reject impossible station mismatch, nonfinite numbers, temperature outside `[180,340] K`, wind speed outside `[0,100] m/s`, direction outside `[0,360]`, receipt before event by more than five minutes, or event outside the raw query window.
- do not reject merely because `qcField` is nonzero.
- multiple revisions are retained. Never overwrite an earlier row.

### 3.8 Revision selection and time matching

Matching policy `metar-nearest-15m.v1`:

1. Expected keys are all 21 station/valid-time combinations (`3 stations × 7 leads`).
2. Candidate records must match the exact `StationId` and have `abs(event_time-valid_time) <= 15 minutes`, inclusive.
3. For each logical observation, select the latest revision satisfying:
   - `provider_available_at <= verification_cutoff`
   - and, for operational-latency replay, `ingested_at <= verification_cutoff`
4. Across logical observations, choose the smallest absolute time delta.
5. Tie-break by earlier event time, then lexicographically smaller logical digest. This avoids a hidden preference for a future observation.
6. Do not interpolate observations in time and do not reuse an observation for a different station.
7. A record with `mesoforge_qc_state=rejected` supports no field. `eligible` and `partial` records are evaluated per field: temperature requires non-null `temperature_k`; speed requires non-null `wind_speed_m_s`; U/V require non-null speed, direction, and both components; direction additionally requires defined direction and the calm-threshold rule. Opaque provider `qcField` alone neither accepts nor rejects a field.

`verification_cutoff` is an explicit UTC request parameter and participates in transformation identity. Later corrections create different as-of results, never mutate an earlier matched-pairs artifact.

Artifact `matched-pairs.v1` has one row per expected station/lead even when unavailable. It includes forecast/run/artifact IDs, matching-policy ID/digest, valid time, selected observation IDs/revision/times, delta seconds, forecast and observed values, a row status (`matched_any_field` or `matched_no_fields`), and separate `temperature_status`, `eastward_component_status`, `northward_component_status`, `wind_speed_status`, and `wind_direction_status`.

Each field status is exactly one mutually exclusive value, selected in this precedence order so denominators and missing counts partition the expected keys:

1. `forecast_missing_or_invalid`
2. `no_report_within_tolerance` (no logical observation in the time window)
3. `revision_after_cutoff` (a logical observation exists in-window, but no revision is eligible as-of the cutoff)
4. `station_metadata_conflict`
5. `observation_qc_rejected`
6. field-specific `temperature_missing`, `wind_speed_missing`, or `wind_direction_missing_or_variable`
7. `calm_direction_excluded` (direction only)
8. `matched`

Component status uses `wind_direction_missing_or_variable` when speed exists but direction/components do not. A selected observation may therefore be `matched` for temperature and speed while missing for components/direction. Every metric’s `sample_count` is the count of `matched` statuses for its field; its missing counts contain every other status and sum with `sample_count` to the expected row count for that stratum. This explicit coverage table prevents a metrics report from hiding missing pairs or double-counting reasons.

### 3.9 Verification formulas

For every eligible scalar pair, `error = forecast - observation`.

For temperature, U, V, and speed:

```text
bias = sum(error) / n
mae  = sum(abs(error)) / n
rmse = sqrt(sum(error**2) / n)
```

Use float64 accumulation. Reject nonfinite inputs rather than relying on `nanmean`. When `n == 0`, emit JSON `null` for the metric and sample count zero; never emit NaN.

Wind direction:

- calm threshold: `1.5 m/s`
- eligible only when both forecast speed and observed speed are `>= 1.5 m/s`, and both directions are defined
- signed circular difference: `((forecast_direction - observed_direction + 180) % 360) - 180`
- per-pair direction error: absolute value in degrees
- aggregate: arithmetic mean absolute circular error and `n_direction`
- values exactly at threshold are eligible

Report metrics:

- overall across all eligible station/leads
- by lead
- by station

Every metric row includes unit, sample count, missing count by mutually exclusive reason, baseline artifact ID, matched-pairs artifact ID, matching-policy digest, metric-set ID, and verification cutoff. This three-station Phase 1 acceptance slice reports no uncertainty interval: its purpose is deterministic pipeline proof, not model promotion or a skill claim, and the sample is too small for a defensible interval. This is an explicit Phase 1 exception to the broader architecture recommendation; uncertainty estimation remains required before any verification result is used for operational comparison or promotion.

---

## 4. Artifact and activity graph

### 4.1 Artifact types

| Artifact type | Schema | Media type | Root/derived |
|---|---|---|---|
| `hrrr-grib-index` | `hrrr-index.v1` | `text/plain` | source, one/lead |
| `hrrr-selected-grib` | `hrrr-selected-grib.v1` | `application/x-grib2` | source, one/lead |
| `hrrr-acquisition-manifest` | `hrrr-acquisition-manifest.v1` | canonical JSON | derived |
| `variable-lineage-manifest` | `variable-lineage.v1` | canonical JSON | derived |
| `canonical-guidance` | `canonical-guidance.v1` | NetCDF | derived |
| `aviationweather-station-response` | `aviationweather-station-response.v1` | exact JSON bytes | source |
| `station-catalog-snapshot` | `station-catalog.v1` | canonical JSON | derived |
| `point-extraction-report` | `point-extraction-report.v1` | canonical JSON | derived |
| `baseline-forecast` | `baseline-forecast.v1` | NetCDF | derived |
| `aviationweather-metar-response` | `aviationweather-metar-response.v1` | exact JSON bytes | source |
| `normalized-metar-observations` | `metar-observations.v1` | Parquet | derived |
| `matched-pairs` | `matched-pairs.v1` | Parquet | derived |
| `verification-report` | `verification-report.v1` | canonical JSON | derived |

### 4.2 Activity types and ordered input roles

- `describe-hrrr-acquisition.v1`: `index-f00..f06`, `grib-f00..f06` → acquisition manifest
- `build-variable-lineage.v1`: acquisition manifest + exact source roles → variable lineage
- `normalize-hrrr-guidance.v1`: `grib-f00..f06`, variable lineage → canonical guidance
- `normalize-station-catalog.v1`: station response → station catalog (pre-run, `run_id=None`; selected by the later run)
- `extract-hrrr-stations.v1`: canonical guidance + station catalog → extraction report
- `generate-hrrr-baseline.v1`: canonical guidance + station catalog + extraction report → baseline
- `normalize-metar-observations.v1`: one or more ordered `metar-batch-NNN` + station catalog → observations
- `match-baseline-observations.v1`: baseline + observations → matched pairs
- `verify-phase1-baseline.v1`: matched pairs → verification report

All transformation parameters are strict models serialized through JCS. Ordered input roles, policy/configuration digest, code/environment, and output schema remain in the existing idempotency digest. Repeating an identical request returns the original succeeded activity/output. Any change in selector, source artifact revision, station snapshot, interpolation policy, cutoff, matching policy, calm threshold, metric set, code, or environment produces a distinct identity.

### 4.3 Run lifecycle

1. Resolve/register the exact configuration snapshot.
2. Acquire/register 7 HRRR index and 7 selected-GRIB root artifacts.
3. Acquire/register the stationinfo root artifact and normalize it, before the cutoff, into an immutable station-catalog snapshot with `run_id=None`.
4. Choose `forecast_issue_time` as the coordinator-supplied UTC issue instant and `information_cutoff <= forecast_issue_time`.
5. Call existing `ArtifactService.create_run` with the 14 HRRR roots plus station-catalog snapshot in canonical role order; it must reject any selected artifact whose authoritative availability exceeds cutoff. The station response remains reachable through the snapshot's activity lineage.
6. Produce all forecast-side derived artifacts with that `run_id`.
7. After valid times, acquire/register METAR roots. They are not selected run inputs.
8. Normalize, match, and verify with activities linked to the forecast baseline and observation roots.
9. Export lineage and prove both the HRRR and observation branches are ancestors of the verification report.

A replay uses recorded artifact IDs and parameters. It never re-discovers remote inventory or selects a newer observation revision.

### 4.4 Mixed-codec transformation gap

`ArtifactService.execute_transformation` currently applies one loader/validator to every input. Phase 1 transformations legitimately combine NetCDF, Parquet, and canonical JSON. Add a typed, role-aware entry point rather than magic-byte sniffing:

```text
InputBinding(loader, validator)
ArtifactService.execute_role_bound_transformation(
    request,
    transform,
    serializer,
    input_bindings: Mapping[input_role, InputBinding],
    output_validator,
)
```

Requirements:

- binding keys must equal request input roles exactly
- load/validate in request role order
- `transform` receives an immutable role-to-value mapping or explicitly documented ordered arguments
- existing `execute_transformation` and `execute_raw_transformation` delegate to the same private core and remain backward compatible
- no input can omit validation; raw byte inputs use an explicit raw contract validator
- public signatures retain typed IDs/digests and strict request models

This is the only planned change to the Phase 0 artifact service’s behavior.

---

## 5. Module boundaries and files

### 5.1 Existing files to modify

- `pyproject.toml` — add runtime scientific/tabular dependencies and `live` pytest marker.
- `uv.lock` — exact resolved versions.
- `.importlinter` — extend forbidden-infrastructure rules to new pure domains; provider adapters may import network/decoder infrastructure, pure scientific modules may import NumPy/xarray/pyproj but never storage/PostgreSQL/S3/application.
- `Makefile` — add offline scientific/Phase 1 targets and opt-in live smoke target.
- `.github/workflows/ci.yml` — keep default jobs offline; add Phase 1 acceptance to existing PostgreSQL/MinIO job without provider network calls.
- `configs/phase1-grasston.yaml` — full immutable slice configuration.
- `src/mesoforge/common/identifiers.py` — typed station/policy/metric IDs.
- `src/mesoforge/catalog/configuration.py` — optional Phase 1 configuration sections.
- `src/mesoforge/catalog/variables.py` — dimension variants only if required; no provider logic.
- `src/mesoforge/contracts/datasets.py` — share low-level time/quality validation helpers without weakening `canonical-guidance.v1`.
- `src/mesoforge/application/artifacts.py` — role-aware input bindings while retaining current APIs and idempotency/transaction ordering.
- `src/mesoforge/storage/interfaces.py` — type `RunRepository` with `RunManifest`; add serializer protocols only if they remain infrastructure-free.
- `scripts/check_repository_hygiene.py` — recognize generated fixture policy and continue rejecting downloaded GRIB/index/cache artifacts. Do not allow committed `.grib2`/`.idx` files.
- `docs/data-contracts/vocabulary.md` — Phase 1 station observation/revision/matching terms.
- create `docs/data-contracts/phase-1.md` and link it from `docs/architecture/v1.md` Phase 1 section without duplicating the canonical architecture.

### 5.2 New production modules

```text
src/mesoforge/
  contracts/
    serialization.py          # canonical JSON/Parquet schema helpers
    lineage.py                # VariableLineageManifest
    forecasts.py              # baseline dataset validator
    observations.py           # observation contracts/Parquet schema
    verification.py           # matching/metric/report contracts
  catalog/
    domains.py
    stations.py
    sources.py                # strict provider settings, no network
  guidance/
    __init__.py
    interfaces.py
    acquisition.py
    decoding.py
    normalization.py
    validation.py
    sources/
      __init__.py
      hrrr.py
  alignment/
    __init__.py
    spatial.py
    reports.py
  forecasting/
    __init__.py
    baseline.py
    validation.py
  observations/
    __init__.py
    interfaces.py
    acquisition.py
    normalization.py
    quality.py
    sources/
      __init__.py
      aviationweather.py
  verification/
    __init__.py
    matching.py
    metrics.py
    validation.py
  application/
    phase1.py
  storage/
    parquet.py
    json.py
```

Provider/network imports stay in `guidance/sources/hrrr.py` and `observations/sources/aviationweather.py`. Scientific formulas stay in pure domain modules. `application/phase1.py` composes ports and artifact services; it contains no selector regex, GRIB key knowledge, wind formula, interpolation formula, matching rule, or metric formula.

### 5.3 Expected test layout

```text
tests/
  contracts/
    test_variable_lineage_manifest.py
    test_baseline_forecast.py
    test_observations.py
    test_verification_contracts.py
  unit/
    application/test_role_bound_transformations.py
    catalog/test_phase1_configuration.py
    guidance/test_hrrr_source.py
    guidance/test_hrrr_decoding.py
    guidance/test_hrrr_normalization.py
    alignment/test_spatial.py
    forecasting/test_baseline.py
    observations/test_aviationweather.py
    observations/test_normalization.py
    verification/test_matching.py
    verification/test_metrics.py
    storage/test_parquet_serializer.py
    storage/test_json_serializer.py
  property/
    test_wind_conventions.py
    test_bilinear_interpolation.py
    test_observation_revision_selection.py
    test_verification_metrics.py
  integration/
    application/test_phase1_pipeline.py
    storage/test_phase1_artifacts.py
  acceptance/
    test_phase1_grasston_hrrr_metar_verification.py
  live/
    test_hrrr_contract_live.py
    test_aviationweather_contract_live.py
  fixtures/phase1/
    hrrr_inventory_f00.txt
    hrrr_inventory_f01.txt
    aviationweather_station_response.json
    aviationweather_metar_response.json
    expected_phase1_metrics.json
    README.md
```

GRIB2 fixture bytes are generated at test time with eccodes from deterministic keys/values. Do not commit downloaded provider GRIB files or decoder indexes. Fixture JSON is synthetic and clearly labeled; live contract tests prove current provider compatibility separately.

---

## 6. Implementation order (TDD)

Every production task follows RED → GREEN → REFACTOR and ends with the listed focused tests. Commit messages are recommendations; keep commits focused and do not squash away scientifically meaningful steps before review.

### Task 1: Pin dependencies, boundaries, and test markers

**Files:** `pyproject.toml`, `uv.lock`, `.importlinter`, `Makefile`, `.github/workflows/ci.yml`.

1. Add Herbie/cfgrib/eccodes and PyArrow as direct dependencies with compatible bounded ranges; resolve exact versions into `uv.lock`.
2. Add pytest markers `scientific` and `live`.
3. Add `make scientific` and `make phase1-acceptance`; add `make smoke-live` that requires `MESOFORGE_LIVE_TESTS=1`.
4. Ensure normal `make test`, `make test-all`, coverage, and CI either skip live tests by marker or the tests self-skip without the env flag. No CI job sets the flag.
5. Extend import-linter contracts for new package boundaries.
6. Run `uv lock --check`, import-linter, and a collection-only test proving live tests are not executed by default.

**Commit:** `build: add Phase 1 scientific dependencies and boundaries`

### Task 2: Add typed Phase 1 catalogs and configuration

**Files:** `common/identifiers.py`, `catalog/domains.py`, `catalog/stations.py`, `catalog/sources.py`, `catalog/configuration.py`, `configs/phase1-grasston.yaml`; contract/unit/property tests.

1. Write failing strict-model tests for malformed IDs, the exact center/bbox midpoint invariant, bbox order/range, station duplication, station outside domain, expected KCBG/KJMR/KROS metadata drift, lead gaps, endpoint scheme, selectors, retry values, calm threshold, and unknown fields.
2. Implement immutable definitions and optional Phase 1 configuration.
3. Add the exact configuration from Sections 1–3.
4. Prove JCS digest stability under YAML formatting/key order and change under every scientific policy change.
5. Prove existing `configs/base.yaml` still validates unchanged.

**Commit:** `feat: define the Phase 1 Grasston configuration contract`

### Task 3: Add canonical JSON, Parquet, and role-aware transformations

**Files:** `contracts/serialization.py`, `storage/json.py`, `storage/parquet.py`, `storage/interfaces.py`, `application/artifacts.py`; focused unit/integration tests.

1. Write failing tests for deterministic canonical JSON, strict Parquet schema/order/timezone handling, checksum round trip, and malformed payload rejection.
2. Write failing tests that mixed-codec transformations cannot run with missing/extra role bindings and that every input validator executes before the transform.
3. Implement role-aware bindings through the existing private execution core without changing transaction/idempotency ordering.
4. Preserve existing APIs and rerun all Phase 0 artifact-service tests.
5. Prove concurrent identical mixed-codec requests yield one succeeded activity/output.

**Commit:** `feat: support validated role-bound artifact transforms`

### Task 4: Implement HRRR discovery and exact-byte acquisition

**Files:** `guidance/interfaces.py`, `guidance/sources/hrrr.py`, `guidance/acquisition.py`; generated fixtures and unit tests.

1. Test exact URL construction for multiple cycles and f00–f06.
2. Test f00 `anl` and fN `N hour fcst`, zero/duplicate selector rejection, next-offset calculation, unsorted/malformed index rejection, and GRIB last-message handling.
3. Test Range status/headers, truncated/oversized responses, ignored Range, ETag revision, retry classes/backoff, deadline, mirror failover, and no older-cycle fallback with a scripted fake HTTP transport/clock/sleeper.
4. Register exact index and selected-GRIB source artifacts through `ArtifactService.register_source`.
5. Produce/validate `hrrr-acquisition-manifest.v1` with bounded metadata.
6. Prove repeat acquisition of identical source revision returns existing roots and differing ETag/bytes creates or rejects a distinct revision appropriately.

**Commit:** `feat: acquire pinned HRRR messages with exact lineage`

### Task 5: Implement variable lineage and HRRR normalization

**Files:** `contracts/lineage.py`, `guidance/decoding.py`, `guidance/normalization.py`, `guidance/validation.py`; contract/unit/property tests.

1. Define and test `VariableLineageManifest`; reject missing/duplicate variable-lead entries and malformed typed artifact IDs/digests.
2. Generate deterministic three-message × seven-lead GRIB fixtures with eccodes.
3. Decode only selected bytes with cache-disabled cfgrib; assert every GRIB key and lead/cycle invariant.
4. Test both earth-relative and grid-relative wind paths; validate rotation with identity-grid/cardinal-vector cases and speed-preservation properties.
5. Normalize/subset to bbox plus one-cell halo and produce exact canonical coordinates/masks/metadata.
6. Run the existing canonical dataset validator and new Phase 1 completeness checks.
7. Prove malformed unit, projection, rotation flag mismatch, missing lead/field, duplicate message, or changed grid fails before artifact registration.

**Commit:** `feat: normalize replayable HRRR temperature and wind`

### Task 6: Normalize the station catalog and bilinearly extract points

**Files:** `catalog/stations.py`, `alignment/spatial.py`, `alignment/reports.py`; unit/property tests.

1. Test stationinfo parsing for all three required stations, `METAR` site capability, unknown/duplicate/missing IDs, bbox checks, pre-cutoff source/derived availability, effective intervals, explicit unknown exposure/instrument identity, coordinate/elevation conflict limits, and typed identities.
2. Test bilinear exact corner/center/plane values, descending axes, points on each of the four bbox edges with a retained halo, outer-grid-edge rejection, out-of-domain points, nonfinite/masked corners, weight sums, and no extrapolation.
3. Test vector components are rotated before interpolation.
4. Emit deterministic `point-extraction-report.v1` with all source indices/weights and explicit elevation policy.
5. Prove all 21 station/lead outputs are covered.

**Commit:** `feat: extract HRRR guidance at Grasston stations`

### Task 7: Generate and validate the HRRR-only baseline

**Files:** `contracts/forecasts.py`, `forecasting/baseline.py`, `forecasting/validation.py`; tests.

1. Write the strict baseline dataset validator for issue time, source reference, exact station/lead set, dimensions, units, masks, contributors, and valid times.
2. Implement identity baseline assembly from canonical guidance + extraction report.
3. Derive speed/direction with float64 intermediates and documented float output type.
4. Test cardinal/intercardinal vectors, zero calm, signed zero, finite bounds, and missing source rejection.
5. Prove the baseline is a distinct derived artifact with canonical guidance, station catalog, and extraction report as lineage ancestors.

**Commit:** `feat: produce the deterministic Phase 1 baseline`

### Task 8: Implement AviationWeather source acquisition and revisions

**Files:** `observations/interfaces.py`, `observations/sources/aviationweather.py`, `observations/acquisition.py`, `contracts/observations.py`; tests.

1. Test exact stationinfo/METAR URLs, query ordering, user agent, date/window calculation, status handling, request throttling, retries, 204 behavior, and body/headers retention with a fake transport/clock/sleeper.
2. Define strict raw/provider record models that preserve required fields and reject malformed types/times.
3. Register exact response bytes as source artifacts with response-completion availability.
4. Prove no credentials are used and no live request occurs in normal tests.

**Commit:** `feat: ingest revision-aware AviationWeather METAR sources`

### Task 9: Normalize METAR observations

**Files:** `observations/normalization.py`, `observations/quality.py`, `contracts/observations.py`; unit/property tests.

1. Test temperature and knot conversions at known exact cases.
2. Test wind cardinal directions, calm, VRB, missing direction/speed, missing temperature, malformed values, and opaque `qcField` preservation.
3. Test logical/revision digest stability, exact duplicate deduplication, corrected report append, deterministic sort, and Parquet round trip.
4. Test source artifact/record-index/station snapshot lineage on every row.
5. Test range/QC decisions and flags exactly as Section 3.7, including station-snapshot effective-time and coordinate/elevation conflict rejection.

**Commit:** `feat: normalize METAR temperature and wind observations`

### Task 10: Match forecast and observations as-of a cutoff

**Files:** `contracts/verification.py`, `verification/matching.py`, `verification/validation.py`; tests.

1. Test exact, ±15-minute inclusive, ±15-minute-outside, cross-station, earlier/later tie, revision cutoff, local-ingestion cutoff, rejected/partial observations, and no-report cases.
2. Test later corrections change a new as-of artifact but never mutate old matched pairs.
3. Test field-specific eligibility, precedence, mutually exclusive status reasons, and the invariant `matched + missing == expected` for every field and stratum.
4. Produce exactly 21 deterministically sorted rows and reject duplicate expected keys.
5. Include policy ID/digest, cutoff, source artifact IDs, and selected revision on every applicable row.

**Commit:** `feat: match baselines to METAR revisions deterministically`

### Task 11: Calculate verification metrics

**Files:** `verification/metrics.py`, `contracts/verification.py`; unit/property/scientific tests.

1. Use hand-calculated small cases for bias/MAE/RMSE and direction wraparound at 359/1 degrees.
2. Test threshold values below/equal/above 1.5 m/s, undefined calm direction, missing fields, zero samples, and nonfinite rejection.
3. Property-test RMSE ≥ MAE ≥ absolute bias within floating tolerance and nonnegative direction error ≤180 degrees.
4. Produce overall/by-lead/by-station rows with units, counts, missing-reason counts, policy/metric identities, and artifact references.
5. Serialize JSON with `null`, never NaN/Infinity.

**Commit:** `feat: verify Phase 1 temperature and wind baselines`

### Task 12: Compose the application use case

**Files:** `application/phase1.py`; unit/integration tests.

1. Define strict request/result contracts with typed IDs/digests and explicit cycle, issue time, information cutoff, and verification cutoff.
2. Inject source ports, clock/sleeper, artifact service, serializers, and pure functions.
3. Implement the lifecycle in Section 4.3 with no scientific/provider logic in the coordinator.
4. Test failure at every boundary leaves no registered derived output, records failed activity where execution started, and never advances to later stages.
5. Test repeat and concurrent requests reuse succeeded activities/artifacts.

**Commit:** `feat: compose the Phase 1 operational slice`

### Task 13: Build the offline end-to-end acceptance proof

**Files:** synthetic fixtures, `tests/integration/application/test_phase1_pipeline.py`, `tests/acceptance/test_phase1_grasston_hrrr_metar_verification.py`.

Acceptance must prove against real PostgreSQL and MinIO:

1. Exact Phase 1 configuration registers.
2. Seven synthetic index and seven selected-GRIB roots plus the station response register with verified digests; the station response normalizes to one pre-run station-catalog snapshot.
3. `RunManifest` pins the 14 HRRR roots plus station snapshot (15 selected inputs) and rejects any selected artifact available after cutoff; lineage still reaches the raw station response.
4. Exact f00–f06 temperature/U/V decode, rotate, subset, and validate.
5. KCBG/KJMR/KROS bilinear point extraction produces 21 complete locations/times.
6. A distinct baseline artifact is generated.
7. Synthetic AviationWeather bytes register and normalize with at least one correction, one missing field, one VRB wind, and one absent station/time.
8. As-of matching produces exactly 21 coverage rows with expected statuses.
9. All required metrics and counts equal a checked-in expected JSON oracle independently computed from fixture values.
10. Lineage from verification report reaches exact baseline, canonical guidance, all relevant GRIB/index roots, exact METAR roots, and station metadata root.
11. Artifact replay verifies every retained content digest.
12. Logical replay reproduces equal arrays/tables/reports; identical repeat creates no duplicate succeeded activities.
13. No AI/publication/bias package is imported or invoked.

**Commit:** `test: prove the Phase 1 forecast and verification slice`

### Task 14: Add opt-in live contract smoke tests

**Files:** `tests/live/test_hrrr_contract_live.py`, `tests/live/test_aviationweather_contract_live.py`, operations docs.

- Guard every test with `MESOFORGE_LIVE_TESTS=1`; otherwise skip before network construction.
- HRRR smoke: recent explicitly supplied cycle via `MESOFORGE_LIVE_HRRR_CYCLE`, one lead only, assert exact three selectors/keys and no retained file outside a temporary directory.
- METAR smoke: station IDs exactly KCBG/KJMR/KROS, bounded recent window, validate required schema and preserve response only in temp storage.
- Do not assert meteorological values, station availability, byte sizes, or exact record counts.
- Document provider load/rate limits and manual command.

**Commit:** `test: add opt-in live provider contract checks`

### Task 15: Documentation and final hardening

**Files:** `docs/data-contracts/phase-1.md`, `docs/data-contracts/vocabulary.md`, `docs/architecture/v1.md`, `README.md`, `docs/operations/local-development.md`, hygiene/CI scripts as needed.

1. Document every contract/policy/formula/source reference from this plan.
2. Add exact artifact/activity graph and replay instructions.
3. Document operational limitations: point representativeness, no elevation correction, METAR precision/QC opacity, source retention/availability, calm threshold, and no publication.
4. Update architecture Phase 1 status/link only; do not fork or rewrite the canonical architecture.
5. Run docs and repository hygiene tests, including proof that GRIB/index/cfgrib caches cannot be committed.

**Commit:** `docs: document the Phase 1 operational forecast slice`

---

## 7. Verification gates

Run focused tests after each task. Before push/review, run all of the following from the repository root:

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

The migration round trip remains mandatory even though no new migration is expected. Live provider tests are a separate manual gate and never required for default CI:

```text
MESOFORGE_LIVE_TESTS=1 \
MESOFORGE_LIVE_HRRR_CYCLE=YYYYMMDDTHH \
uv run pytest -m live tests/live -q
```

### Release-blocking acceptance criteria

- All default tests run without network access.
- Exact base/source branch state is reported; no merge occurs.
- Every public Phase 1 boundary uses strict typed IDs/digests where a type exists.
- Exactly 7 HRRR leads × 3 fields are selected; ambiguity/missingness fails closed.
- Retained source bytes have verified SHA-256 content identity and exact source/index/range lineage.
- HRRR U/V coordinate convention is inspected and correctly rotated before station comparison.
- Baseline contains all 3 stations × 7 leads and is distinct from canonical guidance.
- Observation revisions are append-only and selected as-of an explicit cutoff.
- Matching emits all 21 expected coverage rows and documented missingness.
- All named metrics, direction threshold behavior, units, and sample counts are independently checked.
- Verification lineage reaches exact HRRR, station, and METAR source artifacts.
- Identical replay is logically equal and idempotently reuses original succeeded activity/output.
- Phase 0 tests, 90% total coverage threshold, real PostgreSQL/MinIO integration, migration round trip, docs, hygiene, lint, typing, and import boundaries pass.
- GitHub Actions for the exact pushed head completes successfully before final implementation approval.

---

## 8. Risks and tradeoffs

1. **Wind orientation (high):** HRRR GRIB may encode grid-relative components. Mitigation: inspect key, deterministic projection-basis rotation, cardinal/property tests, live contract smoke. No comparison is permitted when orientation metadata is missing or contradictory.
2. **Observation representativeness (high):** A station point and NWP grid-point interpolation are not physically identical, and elevation is ignored. Mitigation: explicit support/elevation policy and no claim that Phase 1 scores are operationally calibrated.
3. **Provider availability semantics (medium):** HTTP Last-Modified is not proof of first availability. Mitigation: name the observed-response availability policy, preserve HTTP metadata and local ingestion separately, and avoid retrospective latency claims beyond recorded evidence.
4. **METAR revisions/QC (medium):** Public JSON exposes receipt time and opaque `qcField`, not a documented revision number/QC bit contract. Mitigation: content-derived revision digests, append-only records, as-of selection, raw response retention, deterministic MesoForge QC flags.
5. **Range/index drift (medium):** Provider inventory syntax or Range behavior may change. Mitigation: exact parser/semantic assertions, retained index rows, opt-in live smoke, fail closed.
6. **Decoder/environment drift (medium):** eccodes/cfgrib output metadata can change by version. Mitigation: lockfile/environment digest, recorded backend keys/versions, generated fixtures, logical rather than cross-version bitwise replay promise.
7. **Parquet byte determinism (low/medium):** Metadata/version differences may change bytes. Mitigation: lock PyArrow, deterministic schema/order/options, content-address original output, and define replay equality logically at table level.
8. **Fixture fidelity (medium):** Generated GRIB can miss real-provider quirks. Mitigation: encode all asserted HRRR keys/projection fields and keep live tests opt-in; never make default CI network-dependent.
9. **Artifact-service mixed codecs (medium):** A careless extension could bypass input validation. Mitigation: exact role-binding equality, mandatory validator per role, retain the tested Phase 0 transaction core and old API regression suite.
10. **Scope pressure (medium):** General blending/regridding/orchestration is tempting. Mitigation: single-model identity baseline, native-grid bilinear point extraction, explicit exclusions, and architecture review for any proposed expansion.

---

## 9. Review handoff checklist

The implementation handoff must include:

- branch and exact pushed commit SHA
- exact base SHA
- changed files
- dependency/lockfile changes
- tests and pass counts, full coverage percentage
- PostgreSQL/MinIO and migration round-trip evidence
- Phase 1 acceptance proof output
- exact GitHub Actions run/job URLs for the pushed head
- live smoke status clearly labeled as run/skipped and why
- unresolved risks or deviations from this plan
- explicit confirmation that the branch was pushed and not merged

Any scientific contract deviation (field selector, wind rotation, interpolation, time tolerance, calm threshold, revision policy, units, missingness, or metric formula) requires Codex design review before implementation continues.
