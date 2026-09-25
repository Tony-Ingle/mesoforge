# MesoForge

MesoForge is an automated digital forecast desk for configured latitude/longitude
locations. Its target is one field-specific MesoForge forecast with measured
verification and later bounded corrections/editing, not a generic public weather API.
The current implementation is described below. Start with the canonical
[VISION.md](VISION.md) for product direction and
[AGENTS.md](AGENTS.md) for working rules.

## Current status

The [background baseline build](#background-mesoforge-baseline-snapshots) produces
real **36-hour surface forecasts** before configured-location jobs: temperature,
dew point, derived RH, vector
wind speed/direction, gust, hourly liquid-equivalent precipitation (QPF) and native
NBM probability of precipitation (PoP).
Background generation builds a small coordinate-derived
[local baseline grid](#local-surface-baseline-grid) with a larger context domain and
smaller editable subset. A location job pins one immutable baseline, reads its saved
domain and exact center, and optionally saves an immutable issuance without blending
again. The earlier [compatibility forward run](#run-verification-and-current-issuance-together)
verifies eligible previous temperature forecasts,
discovers current model cycles, prepares shared guidance for the configured coordinate
collection, and saves new immutable issuances. Failed locations do not stop later ones;
unavailable verification does not prevent a new forecast. Latitude/longitude are the
only required geographic inputs; names are optional display metadata.

- **Science:** HRRR/GFS are active. Temperature retains 70/30 demonstration weights;
  dew point and coupled wind/gust use the applicable retained Phase 2 70/30 rows for
  hours 1–18 and 60/40 for hours 19–36, with explicit approved fallbacks. RH derives
  from temperature/dew point. These are not optimized weights.
- **Precipitation:** HRRR/GFS QPF uses the approved precipitation rows: 70/30 at
  hours 1–18 and 60/40 at 19–36, with explicit approved fallbacks. Every amount
  retains exact accumulation bounds; GFS buckets are differenced on native cells
  before extraction. RAP/IFS precipitation is unavailable in this increment.
- **Probability:** NBM supplies native one-hour PoP for liquid accumulation strictly
  greater than 0.254 kg/m² (0.01 inch), using its approved weight-1 passthrough.
  Thresholds, intervals, native percentages, source evidence and missingness are
  preserved. PoP is separate from HRRR/GFS QPF and does not imply precipitation type.
  NBM-only delivery is the current baseline, not the final product. A bounded
  [multi-source probability shadow path](#native-probability-shadows) retains other
  native events without changing delivered PoP. This path already supports NBM/GEFS
  six-hour comparisons and separate REFS/ECMWF events; it was rechecked with the
  completed p-type grid on September 12 without changing forecast code or policies.
- **Precipitation type:** an explicit [native p-type preparation step](#native-precipitation-type-on-the-local-grid)
  adds HRRR/GFS/RAP flags, native three-hourly IFS categories and NBM conditional
  type probabilities to that same grid. The temporary baseline requires complete
  HRRR/GFS agreement; disagreement remains ambiguous and missing evidence stays
  explicit. This is not yet automatic acquisition in `forward_run` or verified skill.
- **Snowfall water equivalent:** an optional [native accumulation step](#native-snowfall-water-equivalent)
  retains HRRR/RAP hourly amounts and IFS three-hour increments across the same grid.
  These remain zero-weight evidence; the active field is explicitly unavailable
  because no snowfall blend rule is approved. Existing QPF, PoP and p-type are unchanged.
- **Snowfall amount:** an optional [snowfall-amount step](#native-and-kuchera-snowfall-amounts)
  retains HRRR/RAP/NBM native new-snow accumulations, separate NBM model SLR, and
  RAP Kuchera estimates using retained vertical profiles and matching SWE. All remain
  zero-weight evidence; no active snowfall-amount blend or fixed 10:1 rule is introduced.
- **Cloud cover:** an optional [native cloud step](#native-cloud-cover-on-the-local-grid)
  retains HRRR/GFS/RAP/IFS/NBM total-cloud percentages and per-source sky categories
  across the same grid. Native NBM total cloud is now the temporary delivered sky
  baseline; HRRR/GFS/RAP/IFS remain separate zero-weight comparison evidence.
  Missing NBM stays unavailable, with no substitute or invented cloud weights.
- **Visibility:** an optional [native visibility step](#native-visibility-on-the-local-grid)
  retains HRRR/GFS/RAP/NBM surface horizontal visibility across the same grid.
  IFS open-data visibility is explicitly unsupported. Native metres and per-source
  disagreement are preserved; all contributors have zero active weight and delivered
  visibility stays unavailable pending an approved policy. Visibility does not infer fog.
- **Thunder potential:** an optional [native thunder step](#native-thunder-potential-on-the-local-grid)
  adds native NBM hourly probabilities as a temporary baseline, with separate three-
  and six-hour shadow events. Event/spatial-definition uncertainty is explicit; these
  are not exact-point lightning probabilities or a calibrated multi-source forecast.
- **Ice/freezing rain:** an optional [native ice step](#native-ice-and-freezing-rain-liquid-on-the-local-grid)
  retains NBM flat-ice mass-equivalent accumulation and HRRR/RAP freezing-rain liquid
  separately. Both are zero-weight evidence; delivered fields stay unavailable without
  an approved policy. No liquid-to-ice or ice-thickness conversion is applied.
- **Shadows:** real RAP and ECMWF IFS values/provenance accompany issuance with zero
  active weight. IFS preserves native three-hourly gaps and has no compatible
  instantaneous gust. The temporary NBM cloud policy does not change their blend weights.
- **Shared inputs:** [current four-model discovery](#discover-the-current-four-model-set)
  and [selected preparation/issuance](#prepare-and-issue-the-exact-selected-model-set)
  preserve actual provider availability, identities, cycles/leads and acquisition times.
  A [background refresh](#refresh-guidance-and-forecast-from-the-latest-complete-snapshot)
  publishes a complete prepared contributor snapshot (36–42 hours) behind an atomic
  `latest_complete` pointer. A separate provider-free build publishes MesoForge's
  numerical canvas behind `latest_baseline`; configured-location forecasts pin that
  baseline and read its precomputed current-UTC-hour view without running field blends.
  [Coordinate-derived coverage](#automatic-spatial-coverage) reuses native-grid guidance
  across locations; raw messages support offline rebuilding. The separate local
  MesoForge grid reuses these loaded sources and retains its own transformation identity.
- **Storage and reads:** explicit [batch runs](#local-coordinate-batch) save immutable
  PostgreSQL/MinIO versions. [Issued retrieval](#retrieve-one-issued-version) and
  [saved-hour selection](#select-saved-forecast-hours) preserve exact versions.
  Surface `GET /forecast` reads a retained local grid without creating history or
  registration. Legacy temperature-only snapshots keep their prepared calculation path.
  Starting the API without `--data-dir` still selects the labeled synthetic example.
- **Verification:** [automatic METAR station discovery](#discover-and-reuse-nearby-metar-stations)
  and [bounded verification](#automatically-prepare-observations-and-verify) use real
  observations and idempotent facts. [Comparison metrics](#compare-temperature-models-and-blends)
  retain separate issued versions and identical paired samples. Verification/scoring
  currently covers temperature, not the new dew-point/wind fields.

The surface milestone recorded **560 focused offline tests and 19 PostgreSQL/MinIO
integration tests passing**, plus quality checks, real Minneapolis issuance, exact
readback and offline rebuilding. See the [forward-run evidence](#run-verification-and-current-issuance-together).
The context/editable-grid milestone passed **175 offline and 34 PostgreSQL/MinIO
integration tests**, with byte-identical replay and unchanged point values; measurements
are [recorded below](#local-surface-baseline-grid). Full acceptance,
coverage, forecast skill and production reliability are not established by that demonstration.
The QPF increment adds [real interval/conservation and offline replay evidence](#liquid-precipitation-on-the-local-grid);
precipitation verification remains future work. The [PoP increment](#probability-of-precipitation-on-the-local-grid)
adds actual native probabilistic guidance without changing QPF or other surface values.

Known observation-normalization defect: the retained METAR precipitation parser maps
`P0000` to numeric zero, although [NOAA defines it as trace](https://www.weather.gov/media/asos/aum-toc.pdf).
Its assumed report-relative hour and the legacy Phase 2 matcher's ±15-minute tolerance
also do not establish an exact clock-hour QPF verification sample. A future versioned
normalization correction must preserve raw evidence and historical normalized records;
these legacy precipitation records must not be treated as validated QPF truth.

The bounded [MRMS hourly QPE source contract](#prepare-one-mrms-hourly-qpe-reference)
now retains analysed precipitation and quality evidence for future QPF verification.
It does not yet create QPF verification facts, samples or skill scores.

There is **no active snowfall/ice blend, delivered snow depth on the ground, deterministic bias correction,
site learning, AI editing, production deployment or scheduling in the V2 path yet**. Bias/AI
report stages are explicitly unimplemented and final values currently equal the baseline.
Registration services and delivery also remain future work. The retained Phase 2 station
baseline has HRRR/NBM/GFS, QPF and PoP support; its defaults and technical references
remain intact. Existing `_v2` names describe Phase 2 contracts.

**How to read the current policies.** The long-term rule is that *the blend is the
forecast*: one coherent MesoForge grid from field-specific blends, with every model
kept as contributor evidence rather than offered as a competing forecast
([VISION.md](VISION.md#north-star)). The fixed HRRR/GFS
weights, the NBM-only PoP/sky/thunder sources, the HRRR/GFS p-type agreement rule and
the zero-weight RAP/IFS shadows described above are today's scaffolding for that
blend, not its final form, and they stay in force until explicitly replaced. The slow
provider work is now separate from forecast requests: a
[background refresh](#refresh-guidance-and-forecast-from-the-latest-complete-snapshot)
publishes a complete prepared contributor snapshot (up to 42 hours), then a separate
[baseline build](#background-mesoforge-baseline-snapshots) materializes the current
forecast canvas for configured locations and usable reference hours. Location jobs
consume that saved numerical state; the compatibility forward run still prepares inline.
Continuous refresh on the VPS and GitHub Actions scheduling remain future work.
The [coherence framework](src/mesoforge/forecasting/coherence.py) now orchestrates only
the existing approved source checks, T/Td consistency, RH and wind/gust dependencies.
Other meteorological relationships are registered without invented enforcement rules.
The current numerical fields use one [field-policy dispatcher](src/mesoforge/forecasting/field_blend.py):
temperature, dew point, vector wind, gust and interval QPF, with RH derived from
blended temperature/dew point. It reuses the existing recipe, Phase 2 tables and
scientific kernels without changing weights, fallbacks or policy identities.
Prepared evidence, the numerical baseline and an issued location forecast are distinct
artifacts. Current baseline publication is an on-demand background command; a hosted
worker reacting continuously to guidance changes is still future work
([RFC §5.7](docs/rfcs/mesoforge-v2-architecture.md#57-implemented-background-baseline-snapshots)).

Future direction: add bounded spatial editing to the coherent context/editable baseline.
Versioned deterministic tools would validate bounded GFE-style AI edit recipes, keeping
numerical, bias-corrected and final fields separate before exact-point interpolation.
One-off development requests stay untracked. See [VISION.md](VISION.md#4-configured-locations-are-the-product)
and the [active RFC](docs/rfcs/mesoforge-v2-architecture.md). The first local surface grid
and nested domains are implemented; the editing lifecycle remains future work. The
ECMWF six-hour probability assessment remains explicitly incompatible as described
below. Native interval snowfall-water-equivalent evidence is now implemented.
Native snowfall amounts and separate Kuchera estimates are now implemented as evidence.
Native cloud evidence now accompanies an explicit temporary NBM sky baseline;
[cloud checks and real replay evidence](#native-cloud-cover-on-the-local-grid)
are recorded below. Native visibility evidence and temporary native NBM hourly
thunder potential and separate native ice/freezing-rain liquid evidence are implemented
on the same grid. The [complete canvas inventory and proposed condition design](docs/rfcs/mesoforge-v2-architecture.md#67-forecast-canvas-and-deterministic-conditions)
record actual field policies, missingness and verification limits. A [read-only
structured preview](#read-only-structured-condition-preview) now describes approved
saved fields across the grid and its 36-hour center column. Versioned presentation
rules now compose active sky, precipitation, thunder and wind into condition text,
and a [deterministic transition layer](#weather-evolution-and-transitions) records
precipitation onset/ending, endpoint type changes and sky trends between consecutive
hours of one issuance. Intensity, visibility/fog and delivered winter amounts remain gated.
Neither visibility nor thunder potential alone establishes a complete weather-condition string.
Native snowfall, NBM SLR and Kuchera remain separately traceable pending sufficient
verification data; a broad snowfall evaluation campaign is not the next task.

Local Codex development continues; the Hermes development pipeline is paused. The RFC's
unresolved implementation choices remain proposed, not blanket approval of the roadmap.

## Read-only structured condition preview

Using the existing PostgreSQL/MinIO settings, provide the ID of an issuance that
already contains `local_grid_baseline`:

```bash
python -B -m mesoforge.application.weather_conditions --issued-forecast-id SAVED_GRID_ISSUANCE_ID > preview.json
curl "http://127.0.0.1:8765/issued-forecasts/SAVED_GRID_ISSUANCE_ID/conditions"
curl "http://127.0.0.1:8765/issued-forecasts/SAVED_GRID_ISSUANCE_ID/conditions?scope=grid"
```

The ordinary read path is **point-scoped**: by default the CLI and GET return
`center_point.hours[]` for all 36 center hours, including deterministic
`rendering.text`, plus the input, derivation, wording-policy and geometry metadata,
with an empty `cells` list. `--scope editable` / `?scope=editable` adds the cells
whose saved `inside_editable_domain` marker is true, and `--scope grid` /
`?scope=grid` returns the complete context/editable grid as `cells[].hours[]` for
forecast-desk and grid tooling; an unknown scope returns HTTP 422
`invalid_conditions_scope` (CLI usage error) before any storage read. The response
records the `scope` requested, the cells returned and the grid/editable cell counts
(preview schema `mesoforge.weather-condition-preview.v2`). Only the requested cells
and the center are described; every returned cell keeps its exact structured states
and evidence. On the real issuance `9e989662-…`, the default response is **728,712
bytes** (HTTP 728,723) against **35,739,570 bytes** for the previous full response
(2.04%); `editable` is 7,161,366 bytes and `grid` 35,739,683 bytes, whose cells,
center point, input, geometry and policies equal the previous output exactly. Point
and grid responses were byte-identical on repeat, CLI and HTTP JSON agreed, a
forbidden-hook replay reproduced the CLI bytes, and PostgreSQL rows and stored
objects were unchanged. The existing API startup command/settings apply. This endpoint reads an immutable
version; `/forecast` continues to serve the separate prepared calculation/grid path.
Neither the preview nor its renderer prepares data, recalculates fields or writes
forecast/verification history. Missing/older point-only grids return HTTP 409
`conditions_preview_unavailable` (CLI exit 2); unknown IDs return 404.

Components retain their state, unrounded value/units, original policy/weights,
instant or exact interval, probability threshold/event definition, missing reasons
and pointers into the checksummed saved payload. The preview/renderer source hashes
are recorded separately from the saved forecast's code identity. Numeric surface fields, QPF,
hourly PoP, endpoint p-type, qualified native NBM thunder and the temporary active
NBM cloud field are eligible. Sky preserves the saved fraction, unrounded percentage,
category and source policy, using the existing category boundaries. Missing NBM
remains unavailable even when another cloud contributor exists. Visibility/fog
and winter amounts remain unavailable for wording, with evidence references retained.
Known, unknown, ambiguous, unavailable and not-applicable remain distinct; exactly
calm saved wind can make direction not applicable. Intensity and transitions are
not implemented.

The presentation policy is **`mesoforge-condition-wording.v1`**, with ruleset
`saved-active-fields-condition-preview.v3` and renderer
`compositional-conditions-text.v1`. These are initial MesoForge presentation rules,
not hazard/advisory criteria or new numerical forecast policies. Later revisions
should follow verification and product evidence:

| Component | Unrounded active-field thresholds | Wording |
|---|---|---|
| Hourly PoP | <20%; 20–<30%; 30–<60%; 60–<80%; ≥80% | Omit; slight chance; chance; likely; supported type without a probability qualifier |
| Hourly thunder | <10%; 10–<30%; 30–<60%; ≥60% | Omit; thunder possible; chance of thunderstorms; thunderstorms likely |
| Wind | Sustained ≥15 mph **or** gust ≥25 mph | Breezy |
| Wind | Sustained ≥25 mph **or** gust ≥35 mph | Windy; takes precedence over breezy |

Hourly PoP <20% **and** active hourly QPF exactly zero makes precipitation
`not_applicable` **for rendering only**. The original endpoint p-type, including
`unknown`, remains intact. Positive QPF or PoP ≥20% makes precipitation relevant;
a native p-type flag alone does not. Positive QPF does not override the <20% PoP
wording band or manufacture a probability when PoP is missing. For supported
occurrence wording, agreed native type supplies rain, snow, freezing rain, sleet
or mixed precipitation; unknown/ambiguous/unavailable type uses generic
“precipitation,” preserving its distinct state internally.

Only compatible active hourly events qualify; 3/6-hour shadows never fill them.
QPF/PoP retain their exact intervals and thresholds, while p-type retains its
instantaneous endpoint. A type-qualified phrase is a presentation summary, not a
new type-specific probability or a claim that the type persists throughout the
hour. Thunder keeps its native event-definition/footprint uncertainty. Those
caveats live in each hour's structured `presentation` object (`type_scope`,
`event_definition_uncertainty`, reasons and component/evidence references), not
in the text. Missing NBM sky never activates shadow cloud.

The renderer composes sky → precipitation → thunder → wind. A known sky leads
and joins weather wording with "with"; precipitation, thunder and wind join with
"and". When precipitation or thunder wording is at least **likely** (≥60%), the
sky words are omitted from the text and the known sky is retained in
`presentation.sky` with `rendered: false` and an explicit reason. Omitted or
unsafe components keep their structured state; an hour with no supported
component renders `Weather conditions unavailable.` Resulting shapes include
`Clear`, `Mostly cloudy and breezy`, `Partly cloudy with a slight chance of rain`,
`Cloudy with a chance of precipitation`, `Rain likely`, `Snow likely and breezy`,
`Chance of thunderstorms` and `Thunderstorms likely and windy`; none is hard-coded.

In the earlier preview milestone, the retained Minneapolis **unissued grid export** replayed as 49 × 36 results
with identical bytes on repeat and zero provider calls. Its hour 10 illustrates
the separately timed components (abbreviated, not a saved-ID response):

```json
{
  "valid_time": "2026-09-12T04:00:00Z",
  "qpf": {"state": "known", "value": 0.22247206610999942, "unit": "kg/m^2"},
  "pop": {"state": "known", "value": 0.2143886821646931, "unit": "1"},
  "precipitation_type": {"state": "ambiguous", "value": "ambiguous", "interval": null},
  "thunder": {"state": "known", "value": 0.07, "unit": "1"}
}
```

QPF/PoP/thunder each retain `(03:00Z,04:00Z]`; PoP means liquid **>0.254 kg/m²**,
while p-type is the state at 04:00Z. Thunder retains its provider-defined event and
unencoded footprint, not an exact-point lightning claim. Hour 1 had QPF/PoP zero,
p-type unknown and thunder 1%; those native states remain unchanged. The new
presentation rule can suppress precipitation wording without asserting that trace
precipitation is physically impossible.

Earlier preview validation: **148 focused tests passed** (conditions, API/readback, hourly report,
retained surface/thunder); the affected API regression selection also passed.
Successful saved-ID/36-hour preview tests use explicitly synthetic in-memory
issuances. The actual real Minneapolis ID `9588a3d3-41a8-42fa-851f-936079c83743`
predates grids and correctly returned unavailable. Read-only checks against its
retained PostgreSQL/MinIO setup left all table contents and all **21 issuances / 135
objects** unchanged; temporary services were stopped. No real full-grid issuance
was available, and none was created to manufacture the demonstration. External
replay, 36-hour report and storage snapshots are under
`%LOCALAPPDATA%/MesoForge/baselines/20260913-conditions-preview/`. Full application
acceptance and condition-skill verification were not run.

The sky-policy increment uses `nbm-native-total-cloud-baseline.v1`, with the existing
`native-cloud-percentage-display.v1` categories. Preview and minimal-renderer versions
advanced to v2 at that checkpoint; older saved fields are never retroactively given
the new active policy. The current wording layer has its own versions listed above.
NBM-only cloud is an interim delivered baseline, not the intended enterprise cloud
architecture. It enables deterministic ordinary-hour sky descriptions while cloud
skill remains an open evaluation problem. Future multi-source cloud blending or
calibration must follow verification evidence, not temperature weights.

Sky-policy validation: **328 focused/retained offline tests and 18 PostgreSQL/MinIO
integration tests passed**, covering active/shadow gating, native units/times,
missing NBM without substitution, unchanged other fields, grid/point replay and
fresh full-grid issuance/read-only conditions. Ruff, formatting, mypy, import
contracts, locked-dependency consistency, documentation/hygiene and `git diff --check`
also passed. Full application acceptance, cloud-observation skill and calibrated
condition wording were not tested.

Fresh real full-grid demonstration: Minneapolis **44.98859, -93.25557**, issued
`3bd4cada-d4c3-4f1e-94d8-2d5182c61991` at **2026-09-13 23:22:23Z**. Normal
`run_batch` issuance saved the complete calculated **49 × 36** grid through
PostgreSQL/MinIO, after separate current-model preparation and native attachments.
HRRR/GFS 18Z, RAP 21Z and IFS 12Z were selected automatically; reference 23Z gives
valid times September 14 00Z through September 15 11Z. Native NBM **22Z** used the
existing explicit preparation override after actual 23Z AWS404/NOMADS403 and 22Z
AWS200 evidence. No provider defaults were changed. Automatic NBM fallback through
that 403 remains a limitation, not a claim of fully automatic rich-field acquisition.

The actual localhost request was:

```text
GET /issued-forecasts/3bd4cada-d4c3-4f1e-94d8-2d5182c61991/conditions
```

At September 14 **00Z**, sky is `known`, fraction **0.02**, cloud percentage **2**,
category `clear`, policy `nbm-native-total-cloud-baseline.v1`, NBM source lead **2**.
QPF/PoP are zero; endpoint p-type remains `unknown`. At **15Z**, sky is mostly cloudy
(87%) with **0.08296196207611876 kg/m²** QPF over `(14Z,15Z]` and ambiguous endpoint
p-type. At **17Z**, cloud is **92.06306327559248%**, QPF is **1.1699372979056493 kg/m²**
over `(16Z,17Z]`, PoP is **43.39143650808219%** for liquid >0.254 kg/m² over that same
hour, and instantaneous p-type is rain. These remain separate timed components;
the new presentation layer does not modify their original values or timing.

Sky is available at all 49 cells for hours **1–35**. Hour **36** stays unavailable:
the retained cloud adapter stops at NBM source lead 36, whereas this hour needs 37.
Other cloud models do not replace it. PoP has all 36 native hours; hourly thunder
has the same final-hour adapter gap. Unrequested visibility/winter/probability-shadow
attachments were not borrowed from an older run.

Both real HTTP conditions responses were byte-identical, with calculation/acquisition
and write entry points forbidden and PostgreSQL transactions read-only. All table
contents and MinIO key/ETag/size identities stayed unchanged during retrieval:
**22 issued versions / 136 objects**. Issuance added exactly one version/object;
all older rows/objects remained intact, and the older point-only ID still returned
409. API and temporary PostgreSQL/MinIO services were stopped afterward.

Acquisition retained **901,192,329 bytes**, plus 3,112,478 discovery/availability
metadata bytes, outside Git. Existing 120 p-type and 120 cloud lead records were
reused without downloading again. The saved forecast payload is **308,944,233 bytes**;
its grid export compresses to **40,931,549 bytes**. The complete 36-hour center table,
exact JSON, storage snapshots and exercised scripts are under
`%LOCALAPPDATA%/MesoForge/baselines/20260913-nbm-sky-validation/`; preparation/evidence
is under `MesoForge/forward-runs/nbm-sky-current-20260913T230001Z/` and
`MesoForge/baselines/20260913-nbm-sky-current/` beneath the same local-data root.

The wording milestone reuses this exact issuance; it does not acquire new guidance
or create a replacement forecast. Wording validation on September 16: **149 focused
offline tests passed** (`tests/unit/forecasting/test_condition_wording.py`,
`tests/unit/forecasting/test_conditions.py`, `tests/unit/test_weather_conditions_api.py`),
covering every PoP/thunder/wind boundary with unrounded values, precipitation
applicability, unknown versus not-applicable type, ambiguous type, shadow/evidence
gating, composition and sky omission, missing components, byte-identical replay and
unchanged inputs. The broader affected offline selection (forecasting, application,
API, contract and property modules) passed **1,728 tests**; one pre-existing
`test_current_forecast_batch` mock-signature failure, unrelated to conditions,
reproduced unchanged at committed `50d32c1` and is left for a separate task. Ruff,
mypy, import contracts, locked-dependency, documentation, hygiene and whitespace
checks passed. The checks ran in a fresh locked Python 3.12 environment;
`uv run --locked` itself remains unverified here.

The earlier demonstration issuance `3bd4cada-d4c3-4f1e-94d8-2d5182c61991` stayed
unreadable from this session (it lives under the separate Codex sandbox profile), so
the real validation used a **fresh rich-grid issuance in this environment** on
September 16. Automatic discovery at **22:31:54Z** selected **HRRR 18Z / GFS 18Z /
RAP 21Z / IFS 12Z** for reference 22Z (valid September 16 23Z through September 18
10Z); shared preparation with PoP, then the documented p-type, cloud and thunder
attachments and `run_batch` issuance followed, all under 11 minutes. Automatic NBM
PoP discovery rejected the still-publishing **22Z** cycle: the AWS index returned 404,
and on NOMADS the GRIB's Last-Modified (22:37:58Z) followed its index (22:36:21Z)
with no ETag, so object identity could not be validated. That evidence is retained
in the run's `prepared/preparation.json`; the existing `prepared_pop --nbm-cycle`
override then attached NBM **21Z** before the cloud/thunder attachments and issuance.
The first automatic attempt, issued as `5bd637dd-77d4-4f0d-ac92-bd39d73918c4` with
PoP, active sky and thunder explicitly unavailable, remains unchanged history. The
validated issuance is **`9e989662-551e-4918-92d8-77005eb7e474`**, issued **22:48:56Z**
with the complete calculated **49 × 36** grid (300,528,405-byte stored object).

Conditions validation against that issuance: the CLI ran twice and was
**byte-identical** (35,739,570 bytes); `GET /issued-forecasts/9e989662-…/conditions`
ran twice against a listening localhost server and was **byte-identical** (HTTP 200,
the same JSON as the CLI). An in-process replay with point calculation, grid
building, issuance, `requests`, `xarray.open_dataset` and every non-loopback socket
forbidden reproduced the CLI bytes exactly. Two readbacks of the saved payload were
identical and matched both the preview's input digest and the stored object. PostgreSQL
rows (2 issued forecasts, 2 stored objects) and object-store contents (2 objects with
unchanged keys, ETags and sizes) were identical before and after every retrieval. All
36 center hours and all 49 × 36 cell hours rendered. Center states: PoP, QPF and wind
known ×36; p-type unknown ×28, ambiguous ×7, rain ×1; sky and thunder known ×35, each
unavailable at hour 36 (the NBM lead-37 adapter gap).

Actual center wording, with the inputs that produced it: hour 8, September 17 06Z
(1 a.m. CDT) — sky 86.4%, PoP 50.7%, QPF 2.04 mm, HRRR/GFS-agreed rain →
**Mostly cloudy with a chance of rain**; hour 5 — 90.0%, 22.2%, unknown type →
**Cloudy with a slight chance of precipitation**; hour 7 — 86.9%, 55.0%, 2.24 mm,
ambiguous type → **Mostly cloudy with a chance of precipitation**; hour 10 — 82.3%,
19.6%, 0.62 mm, ambiguous → **Mostly cloudy** (relevant, wording omitted below 20%);
hours 18–34 with PoP 0, QPF 0 and native type `unknown` → **Partly cloudy**,
**Mostly clear** or **Mostly cloudy**, with precipitation `not_applicable` for
rendering and the native `unknown` retained; hour 36 — sky and thunder unavailable,
PoP 4.3%, wind below thresholds → **Weather conditions unavailable.** with every
structured state and reason retained. Sustained wind peaked at 7.7 mph, gusts at
18.7 mph and hourly thunder at 4%, so no breezy/windy or thunder wording occurred
naturally; no precipitation reached 60%, so sky omission and the precipitation-plus-
thunder phrasings were not exercised by real data and the wording policy is unchanged.
After validation the **149 focused condition tests passed again**, and **37
integration tests** (batch issuance, forward run, S3 object store and migrations)
passed against an ephemeral test `pgserver` and the same local S3 endpoint using
their own test buckets; the validation database and bucket were unchanged afterwards.

Environment notes for that validation: MinIO's download site now returns HTTP 410
(the open-source server is archived), so the S3-compatible endpoint was a local
`moto` server, which honored the adapter's conditional `If-None-Match` put with 412;
PostgreSQL was the existing `pgserver` script; the API served synthetic `/forecast`
guidance because only the storage-backed conditions route was exercised. Run
directories, raw guidance and evidence remain outside Git under this session's
`MesoForge/forward-runs/rich-20260916T2231Z` and
`MesoForge/baselines/20260916-conditions-real-validation`. Full acceptance/coverage
and live-provider canaries were not run.

### Weather evolution and transitions

`GET /issued-forecasts/{id}/conditions/transitions` and
`python -B -m mesoforge.application.weather_transitions --issued-forecast-id ID
[--display-timezone ZONE]` detect evolution between consecutive hours of one saved
issuance. Both read the exact version once, describe only its point-scoped hours,
and neither recalculates a field, reads a grid cell, calls a provider or writes
history. Structured facts (`mesoforge.weather-transitions.v1`, policy
**`mesoforge-transition-policy.v1`**) are separate from rendered text
(`transition-text.v1`); every fact carries its type, track, status, an explicit
window, both endpoint states, hour references into the conditions preview, evidence
references, reasons and the policy id. Rendered times use the request's
`display_timezone`, else the issuance's saved report zone, else UTC; structured
times stay UTC.

**Timing.** A change is known only within `(previous endpoint, next endpoint]`, the
same left-open, right-closed closure as the hourly intervals; no sub-hourly timing is
inferred, so text says "between 9 PM and 10 PM Wednesday", never a minute.
**Gaps.** An unavailable component at either endpoint breaks that track's sequence;
nothing is inferred across it and the gap is listed. Known, unknown, ambiguous,
unavailable and not-applicable stay distinct throughout.

| Track | Source | Transition types | Rendered |
|---|---|---|---|
| `precipitation_occurrence` | applicability policy (not applicable ↔ applicable) | `precipitation_onset`, `precipitation_ending` | No (structured only; trace amounts are applicable but unworded) |
| `precipitation_wording` | presentation probability band (omitted ↔ slight chance/chance/likely/direct) | `precipitation_wording_onset`, `precipitation_wording_ending` | "Rain developing …", "Precipitation ending …" |
| `precipitation_type` | active endpoint p-type on consecutive applicable hours | `precipitation_type_change` with status known/ambiguous/unknown | Only known→known: "Rain changing to snow …" |
| `sky` | active NBM category on the ordered scale clear → mostly clear → partly cloudy → mostly cloudy → cloudy | `sky_trend` (`clearing`, `increasing_clouds`) | "Becoming mostly clear …" |

`rain → ambiguous → snow` yields two `ambiguous` facts, never a direct rain-to-snow
claim; unknown↔ambiguous changes are not events. A sky trend needs a move of at
least **two categories** from the reference level that persists for **three hours**
(the trend hour and the next two all available and at least one level on that side);
its window runs from the last hour at or beyond the reference on the other side, and
the trend hour becomes the new reference, so one-hour wobbles and one-level changes
never become prose. Thunder and wind stay hourly-only.

Real September 16 demonstration on `9e989662-551e-4918-92d8-77005eb7e474`
(display zone America/Chicago from the saved report): **11 facts, 4 rendered** —
"Precipitation developing between 9 PM and 10 PM Wednesday" (PoP 19.6% → 22.2%,
unknown type), "Precipitation ending between 2 AM and 3 AM Thursday" (33.5% → 19.6%),
"Becoming mostly clear between 2 PM and 5 PM Thursday" (mostly cloudy at 19Z, mostly
clear at 22Z, persisting through 00Z) and "Becoming mostly cloudy between 7 PM and
10 PM Thursday". The 05Z–07Z hours produced `ambiguous → rain` and `rain → ambiguous`
type facts with status `ambiguous`, left unrendered. Applicability onsets/endings at
23Z–00Z, 11Z–12Z, 14Z–15Z, 15Z–16Z and 09Z–10Z (trace QPF with PoP below 20%) are
structured only. Hour 36's unavailable NBM sky is reported as a gap and no trend
crosses it. CLI and HTTP outputs were byte-identical on repeat (37,097 / 37,118
bytes), UTC and Chicago renderings shared identical facts, an unknown zone returned
422 `invalid_display_timezone`, a forbidden-hook replay reproduced the CLI bytes, and
PostgreSQL rows and stored objects were unchanged. The per-hour `components.transitions`
placeholder in the conditions preview is unchanged; evolution lives in this resource.

### Period summaries

`GET /issued-forecasts/{id}/conditions/periods[?display_timezone=ZONE]` and
`python -B -m mesoforge.application.weather_periods --issued-forecast-id ID
[--display-timezone ZONE]` group the transition facts above into local
presentation periods (`mesoforge.period-summary.v1`, policy
**`mesoforge-period-summary.v1`**, renderer `period-text.v1`). They reuse the
transition preview (one saved read, point scope only) and derive **no** new
weather: no period maxima, dominant sky, totals or representative conditions —
only grouping and wording of facts that already exist. Structured output keeps
every period's local and UTC bounds, its hour references, its transition
references with exact UTC windows, omitted facts with their reasons, gaps,
per-track availability counts, and the transition facts themselves.

**Boundaries.** Local 12-hour periods on wall-clock time: day 06:00–18:00, night
18:00–06:00, left-closed/right-open, identified by start date and kind
(`2026-09-16-night`). The first/last periods may be partial; membership and
lengths are computed in UTC, so a period spanning a daylight-saving change is 11
or 13 hours and every hour lands exactly once. This is a presentation convention,
not a daylight definition. **Grouping.** A fact belongs to the period containing
its window end (the first hour holding the new state); a window that starts in an
earlier period is flagged and its exact window is retained. **Labels.** Text uses a
natural local descriptor only when every hourly endpoint inside the window falls
in one band — late … night (01–03), early … morning (04–06), morning (07–12),
afternoon (13–18), evening (19–24), with early/late only when the whole window sits
in that half — otherwise the explicit "between 4 PM and 7 PM Thursday" phrase.
**Combination.** Two rendered facts combine only when their windows are identical
and a listed rule applies (wording onset + increasing clouds; wording ending +
clearing); everything else stays a separate sentence in window order. Ambiguous
and unrendered facts remain omitted with their transition reasons; gaps stay
listed and nothing is inferred across them.

Real September 16 demonstration on `9e989662-551e-4918-92d8-77005eb7e474`
(America/Chicago from the saved report; three complete periods 18:00 Wed –
06:00 Fri): **"Precipitation developing late Wednesday evening. Precipitation
ending late Wednesday night. Becoming mostly clear Thursday afternoon. Clouds
increasing Thursday evening."** Wednesday night holds five facts (two rendered,
one structured-only applicability onset, two `ambiguous` type changes omitted);
Thursday day holds the clearing trend plus three structured-only applicability
facts; Thursday night holds the increasing-clouds trend, a structured-only trace
onset and the hour-36 sky gap (`active_sky_unavailable`). No windows coincided,
so no sentences were combined. The same facts rendered in UTC give four periods
(7 and 5-hour partial ends) and relabelled text. CLI and HTTP outputs were
byte-identical on repeat (31,964 / 32,007 bytes), unknown zones returned 422, a
forbidden-hook replay reproduced the CLI bytes, and PostgreSQL rows and stored
objects were unchanged.

### Issuance payload measurement

A bounded September 16 measurement of the validated issuance
`9e989662-551e-4918-92d8-77005eb7e474` (300,528,405 canonical JSON bytes) explains
the size. `forecast.local_grid_baseline` is **285.7 MB**; the point column
`forecast.hours` is a **5.8 MB** byte-for-byte copy of the center cell; the saved
`hourly_report` is 6.5 MB and the model-set/attachment evidence 2.2 MB. Key names
alone are 113.3 MB (37.7%), values 156.2 MB and JSON punctuation 22.6 MB. Of the
261.7 MB of grid leaves, **forecast values are 3.5 MB (1.3%)** and extraction
geometry 9.0 MB (3.4%); the rest is metadata copied into every cell-hour: GRIB
native keys 63.9 MB, descriptive prose 48.4 MB, timestamps/cycles/leads 36.8 MB,
policy blocks 31.7 MB, hashes 24.7 MB, URLs/files 20.2 MB, other enumerations
23.5 MB. By subtree: thunder guidance 73.1 MB (of which 31.1 MB is the all-pairs
`comparisons` list with only three distinct variants across 1,764 cell-hours),
cloud guidance 50.2 MB, p-type guidance 45.1 MB, per-model surface contributors
47.7 MB, shadow sources 10.0 MB; the `surface.fields` copies of the cloud, thunder
and p-type guidance `field` blocks add 22.9 MB. Storing each (path, hour, value)
once instead of once per cell would need **9.8 MB**; each (path, value) once, 5.2 MB.

Ordinary lossless compression confirms the repetition: gzip-9 **41.4 MB** (7.3×,
its 32 KB window cannot see cross-cell repeats), bz2 16.7 MB, zstd-3 11.6 MB,
zstd-9 9.9 MB, xz-6 2.76 MB and zstd-19 **2.36 MB (127×)**. The 35.7 MB conditions
response is 35.0 MB of `cells` and **0.71 MB** of `center_point`; gzip-9 1.28 MB,
zstd-19 0.44 MB. PostgreSQL holds 184 bytes per issued-forecast row and 232 bytes
per stored-object row (public tables 360 KB including empty index pages; database
8.0 MB against a 7.5 MB empty template) versus 544 MB of objects for two issuances.

At two issuances per day, measured raw sizes project to **219 GB/year for one
location, 2.2 TB for ten and 22 TB for a hundred** (30 GB / 302 GB / 3.0 TB with
gzip-9 at rest; 1.7 GB / 17 GB / 173 GB with zstd-19). Disk is not the first
limit: `select_hours`, window verification and the conditions route read every
full version for a coordinate, so a month of two issuances a day makes one
hour-selection query read about 18 GB. The proposed remedies are recorded in
[RFC §6.7.9](docs/rfcs/mesoforge-v2-architecture.md#679-issuance-payload-measurement-and-proposed-normalization):
a content-addressed shared-metadata table inside the immutable issuance (exact
inflation to today's shape, no change to raw/prepared artifacts or replay), a
center-point default for `GET /issued-forecasts/{id}/conditions` with explicit
editable/grid scopes, and a valid-time prefilter for version selection. The two
read-path items are now implemented (point-scoped conditions above and the
[metadata prefilter](#select-saved-forecast-hours) below) without touching stored
bytes or digests; the immutable issuance format has **not** been normalized and
compression at rest is unchanged, so the measured storage-normalization opportunity
remains future work. Measurement scripts and JSON reports are outside Git under
`MesoForge/baselines/20260916-payload-measurement`.

The learning loop's data source is now in place: [repeatable forward runs](#repeat-forward-runs-and-read-accumulation-status)
accumulate immutable versions and idempotent verification facts for configured
coordinates behind an overlap lock and a decision-window guard, and a read-only
accumulation status reports verified history per coordinate and lead bucket. A
[read-only site verification analysis](#read-only-site-verification-analysis) turns
the stored facts into canonical samples and describes temperature error by lead,
day/night and station; it applies no correction and currently reports
`insufficient_evidence`. New facts also carry [compact analytical attributes](#compact-analytical-attributes-on-new-facts)
so that analysis scales without opening each multi-megabyte evidence payload, while
older facts stay readable without migration. Two owner-approved policies now govern
future learning: [which issuance represents a decision window and what evidence a
correction proposal needs](#decision-window-and-evidence-policies). On-demand background
baseline publication now uses the field-specific dispatcher and generalized coherence
framework with only current approved rules enforced. Broader precipitation, thermal,
winter and visibility coherence still needs scientific policy decisions.
Prepared and baseline publication retain separate concurrency
and cutoff boundaries as described below. More independent verification evidence is still
needed before deriving weights or corrections; visibility/fog needs separate causal
evidence and policy.

## Existing forecast path

The separated coordinate path is [refresh_guidance.py](src/mesoforge/application/refresh_guidance.py)
for provider preparation/publication,
[build_baseline.py](src/mesoforge/application/build_baseline.py) for offline numerical
generation/publication, then
[forecast_from_baseline.py](src/mesoforge/application/forecast_from_baseline.py) for
pinned local-domain extraction and optional immutable issuance. The old
[forecast_from_snapshot.py](src/mesoforge/application/forecast_from_snapshot.py) is an
explicit development/replay/equivalence tool, not the normal configured-location path.
The compatibility
[forward_run.py](src/mesoforge/application/forward_run.py) still combines previous-hour
verification, selected-model preparation and batch issuance in one invocation.
[point_forecast.py](src/mesoforge/application/point_forecast.py)
and [surface_forecast.py](src/mesoforge/application/surface_forecast.py) supply native
extraction to [FieldBlendEngine.blend_field](src/mesoforge/forecasting/field_blend.py)
at each background-built node of
[local_surface_grid.py](src/mesoforge/application/local_surface_grid.py). The surface point
is read from that grid's center. Contributor roles and field semantics follow
[VISION.md](VISION.md#7-every-field-has-its-own-blend); the current adapter capabilities
live in [the contributor catalog](src/mesoforge/catalog/contributors.py).

The immutable field registry binds each migrated field to the existing policy
object and specialized kernel. Temperature remains 70/30 with both sources required;
dew point, wind/gust and QPF retain approved subset rows (70/30 through hour 18,
60/40 afterward when both models qualify). Source wind/gust QC remains coupled;
QPF requires exact hourly intervals and RH stays a diagnostic. Native contributors
are preserved, including shadows. NBM PoP/sky/thunder, p-type and evidence-only
fields retain their separate current policies. No stored schema or historical
forecast is rewritten. See [RFC §5.5](docs/rfcs/mesoforge-v2-architecture.md#55-field-specific-blend-layer).

The 2026-09-24 migration replayed retained Minneapolis snapshot
`20260918T004459Z-fcb1b488`: all 49×36 cells, the exact point, conditions, transitions
and period summaries matched canonically, apart from changed execution-source
hashes and their dependent checksums. Another 2,124 old/new boundary-case comparisons
were exact. Grid/blend time was 26.025 → 25.609 seconds; load time was cache-sensitive.
The 72 PostgreSQL/MinIO integration tests passed, including immutable readback and
historical point-only payloads. The broader offline unit/contract/property run had
3,361 passes and the same three failures independently reproduced at starting
commit `c57233f`: one batch mock-signature expectation and two typed-boundary
inventory checks. They were not changed or waived by this milestone. No provider
acquisition or full expensive acceptance run was performed.

The following is the separate retained **Phase 2 station path**, which supplies
reusable science and contracts; it is not the current coordinate lifecycle's entry point:

1. [The live runner](scripts/run_phase2_live.py), `main()`, builds `Phase2Request`
   from explicit UTC times. `_load_configuration()` merges
   [base](configs/base.yaml), [Phase 1](configs/phase1-grasston.yaml), then
   [Phase 2](configs/phase2-grasston.yaml) configuration and registers a snapshot.
2. [Phase2Coordinator.run()](src/mesoforge/application/phase2.py) composes the
   [production adapters](src/mesoforge/application/phase2_production.py).
   `Phase2ProductionProvider.discover()` tries eligible cycles and acquires selected
   GRIB messages; acquisition is not merely metadata discovery.
3. Model-specific [normalizers](src/mesoforge/guidance/normalization_v2.py) decode
   guidance, standardize units/wind/interval semantics, and retain native-grid bbox
   subsets plus halo. `align_station_to_model()` in
   [station_frame.py](src/mesoforge/alignment/station_frame.py) extracts values at
   exact valid times with bilinear interpolation.
4. Availability and configured fallback rows feed deterministic blend operators.
   [Baseline assembly](src/mesoforge/forecasting/baseline_v2.py) fixes three stations
   and 36 horizons. PostgreSQL stores metadata and S3-compatible storage holds
   immutable artifacts. The coordinator then acquires observations and verifies;
   the runner reads stored results into JSON, CSV, and Markdown exports.

## Setup and commands

Run commands from the repository root. **The general setup, service, and full-suite
commands below remain unverified on this local Windows checkout.** The focused
Python 3.12 checks and prepared-guidance demonstration have separate execution results below.

The project requires Python **3.12** and `uv`; see [pyproject.toml](pyproject.toml)
and [uv.lock](uv.lock). Dependencies include NumPy/xarray, Pint, pyproj,
ecCodes/cfgrib/Herbie, NetCDF/HDF5, PyArrow, Pydantic, SQLAlchemy/psycopg, and boto3.
Development dependencies include pytest, Hypothesis, Ruff, mypy, and pgserver.

```text
uv sync --locked --all-groups
```

This is the dependency setup used by [CI](.github/workflows/ci.yml).
The [Makefile](Makefile) supplies convenience targets; `make sync` uses
`uv sync --all-groups` without `--locked`.

| Check | Existing command | Requirements |
| --- | --- | --- |
| Fast offline tests | `uv run pytest tests/unit tests/contracts tests/property -q` | Development dependencies; no services/providers |
| Scientific tests | `uv run pytest -m scientific tests/unit tests/property -q` | Offline numerical checks |
| Documentation | `uv run python scripts/validate_docs.py` | Existing documentation validator |
| Quality | `make quality` | Lock, formatting, lint, types, imports, docs, hygiene |
| Integration | `uv run pytest -m integration tests/integration -q` | Real PostgreSQL and/or MinIO |
| Phase 2 acceptance | `make phase2-acceptance` | Heavier fixture-GRIB workflow, real PostgreSQL/MinIO, replay/failure scenarios |
| Live canaries | `uv run pytest -m live tests/live -q` | Explicit opt-in and provider cycles |

`make test` runs the fast offline suite. `make phase2-offline` combines quality,
offline tests, scientific checks, and the default live-test skip check. `make test-all`
and `make coverage` include service-backed tests and are not fast offline commands.

For integration/acceptance, follow [local development](docs/operations/local-development.md):

```text
docker compose -f deploy/local/compose.yaml up -d --wait
uv run alembic upgrade head
```

Configure the database and test S3 environment variables before migration/testing.
`MESOFORGE_TEST_DATABASE_DSN` selects the test database; without it,
[tests/conftest.py](tests/conftest.py) starts ephemeral PostgreSQL through pgserver.
MinIO must still be available. These fixtures reset schemas: use a dedicated test
database. Live canaries require `MESOFORGE_LIVE_TESTS=1` and explicit
`MESOFORGE_LIVE_HRRR_CYCLE`, `MESOFORGE_LIVE_NBM_CYCLE`, and
`MESOFORGE_LIVE_GFS_CYCLE` values in `YYYYMMDDTHH` format. They check provider contract
shape, not forecast skill or operational availability.

### Run the existing baseline

The retained Phase 2 entry point is `uv run python scripts/run_phase2_live.py` with
required arguments `--target-reference-time`, `--forecast-issue-time`,
`--information-cutoff`, `--verification-cutoff`, and `--output-dir`.
Supply explicit ISO UTC timestamps and a writable output directory; the runner's
module docstring contains a historical invocation example. Provider retention and
cutoffs determine whether those historical inputs remain available.

It requires migrated PostgreSQL and `MESOFORGE_DATABASE_DSN`,
`MESOFORGE_S3_ENDPOINT`, `MESOFORGE_S3_BUCKET`, `MESOFORGE_S3_ACCESS_KEY`, and
`MESOFORGE_S3_SECRET_KEY`. It downloads live guidance and observations and writes
artifacts. `--replay` adds a zero-network replay **after the live run**; it is not a
standalone offline CLI mode. Keep credentials and generated model/output files out
of Git.

### Windows and validation status

CI runs Ubuntu. Unix `export`, inline environment assignments, shell continuations,
and the Linux MinIO binary example in the operations guide need adaptation on Windows.
Use PowerShell `$env:NAME = 'value'` assignments and direct `uv` commands when Make
or a POSIX shell is unavailable. The lockfile includes Windows pgserver wheels;
that does not establish that the whole stack works on this machine. WSL/Linux offers
an environment closer to CI.

During the September 9 documentation consolidation, checks passed:
`python -B scripts/validate_docs.py`,
`python -B scripts/check_repository_hygiene.py`, and `git diff --check`.
The existing Python check scripts used the available Python 3.14.4 interpreter,
not the project's Python 3.12 runtime; `uv` was not available on PATH. Product setup,
provider access, services, and product tests were not exercised at that checkpoint.
The later Python 3.12 and real-input results below supersede that limited runtime status.

## Approved localhost demonstration

[The HTTP entry point](src/mesoforge/api.py) accepts an exact coordinate and
returns 36 hourly temperatures through [prepared point extraction](src/mesoforge/application/point_forecast.py).
Every JSON response identifies its inputs as real prepared guidance or synthetic
demonstration data. Neither mode claims to be a current live forecast. Invalid
coordinates or points outside the native model domain return HTTP 422. A valid point
outside prepared coverage returns HTTP 409 (`coverage_required`), with instructions
to prepare coverage first. GET never downloads or prepares data.

With the locked dependencies installed, the portable start command is:

```text
uv run --locked python -m mesoforge.api --data-dir PATH_TO_PREPARED_SNAPSHOT
```

The module was exercised directly with the isolated Python 3.12 environment and
`PYTHONPATH=src` on Windows; the `uv run` wrapper above has not been executed.
Open <http://127.0.0.1:8765/forecast?lat=45.8&lon=-93.1>. Stop with Ctrl+C.
The launcher binds only to `127.0.0.1`; `--port` changes the port, not the host.

An explicit `--data-dir` requires existing prepared files; it never generates a
synthetic substitute. Real snapshots contain `HRRR.nc`, `GFS.nc`, `manifest.json`,
and retained source messages/indexes under `raw/`. Startup verifies their hashes,
units, cycles, valid times, and projection metadata, then loads and closes the
prepared datasets. Requests reuse these arrays across coordinates, with no file or
provider I/O. Restart to load different inputs.

New real snapshots declare `target_horizon_hours` as hours 1–36. The API always
returns every declared hour, including explicit nulls where either model is missing.
Older three-hour snapshots without that declaration still serve and rebuild their
original three hours. The default synthetic example also remains three hours.

The latest snapshot exercised on Windows is outside Git at
`%LOCALAPPDATA%\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36`.
This exact PowerShell command starts it from the repository root using the existing
isolated environment; it does not download anything:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe" -B -m mesoforge.api --data-dir "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
```

These real inputs use target reference **2026-09-10 12:00 UTC**, HRRR's **12Z** cycle
at leads **1–36**, and GFS's **06Z** cycle at leads **7–42**. The 36 matching valid
times run hourly from **2026-09-10 13:00 UTC through 2026-09-12 00:00 UTC**.
Existing `align_station_to_model()` and `blend_scalar()` produced all 36 values at
the example coordinate, with empty missing-reason lists throughout. Example values
from the actual HTTP response, rounded to six decimals:

| Horizon | Valid time UTC | Temperature, K |
| --- | --- | --- |
| 1 | 2026-09-10 13:00 | 283.705085 |
| 18 | 2026-09-11 06:00 | 289.244578 |
| 19 | 2026-09-11 07:00 | 289.120690 |
| 36 | 2026-09-12 00:00 | 298.287692 |

Each response includes all 36 hourly rows, source URLs and raw/prepared checksums,
plus the manifest hash. The full captured response is retained locally at
`%LOCALAPPDATA%\MesoForge\baselines\20260910-temperature-36h\actual-response.json`.

Omitting `--data-dir` retains the earlier synthetic example. Its tiny invented grids
live in `mesoforge-synthetic-temperature-demo` under the system temporary directory;
existing files are not overwritten. Its August 30, 2026 inputs still produce
**286.14, 287.14, and 288.14 K** at the example coordinate, labeled synthetic.

Demo weights are **70% HRRR / 30% GFS throughout hours 1–36**, explicitly approved
for this demonstration and **not optimized weights**. They match
`scalar-vector.hg.h01-h18` in [the existing configuration](configs/phase2-grasston.yaml)
for hours 1–18. Phase 2's `scalar-vector.hg.h19-h36` row remains **60/40**;
the endpoint's approved demonstration weights are separate from that policy.
If either required model or hour is missing, that hour is null; weights
are never redistributed. Invalid prepared-file units or time metadata prevent startup.

### Local coordinate batch

[The batch command](src/mesoforge/application/batch_forecast.py) reads one JSON config
and ensures shared coverage from one **36-hour** source snapshot before issuing
forecasts. Save this as `locations.json`:

```json
{
  "locations": [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 45.9, "lon": -93.0}
  ]
}
```

Each location contains only numeric `lat` and `lon`; no station IDs, counties,
bounding boxes, or other geographic configuration is needed. **Batch runs now issue
and persist forecasts**, so they require migrated PostgreSQL and an S3-compatible
object store. One-off `/forecast` requests still need only the prepared files.

Use the existing storage environment variables, supplied outside Git:
`MESOFORGE_DATABASE_DSN`, `MESOFORGE_S3_ENDPOINT`, `MESOFORGE_S3_BUCKET`,
`MESOFORGE_S3_ACCESS_KEY`, and `MESOFORGE_S3_SECRET_KEY`. The DSN uses the
`postgresql+psycopg://` dialect. See [local development](docs/operations/local-development.md)
for existing service options; the command does not install or start them. Apply
migration `0004_issued_forecasts` through the existing Alembic chain before running.
From the repository root with the locked environment configured:

```text
uv run --locked alembic upgrade head
uv run --locked python -m mesoforge.application.batch_forecast --config locations.json --data-dir PATH_TO_PREPARED_SNAPSHOT
```

The portable `uv run` wrappers have not been executed on this Windows checkout.
The command writes JSON to standard output: `batch_run_id` identifies this
invocation; `results` preserves input order and each entry contains its
zero-based `index`, input `location`, and `status`. An `ok` entry has the complete
existing `forecast`, including all 36 hours, units, source cycles/leads, valid times,
weights, missing reasons, source URLs, and checksums. It also has an `issued` header
with a unique `issued_forecast_id`, batch ID, location index, coordinates, issuance
UTC, target reference UTC, and the saved object's content digest. An `error` entry
instead has an error `code` and `message`; no successful issuance is reported for
that location. Missing hourly guidance remains null with reasons inside a saved
forecast; it does not silently change the blend.
An unrepresentably large JSON number is retained as text in its location error.

Every invocation creates new issued versions, even when inputs and numerical values
are identical. Issuance time is the current UTC time when the record is created;
target/reference and source-cycle times retain their prepared-input meanings. This
historical demonstration still makes no operational cutoff or live-forecast claim.
The full forecast, issuance metadata, and current source/dependency identity are
serialized with the existing canonical JSON serializer and saved through the
existing S3 adapter. Verified bytes precede a single PostgreSQL transaction for
stored-object metadata and the `issued_forecasts` header. Scientific payloads are
not stored in PostgreSQL. A new run never updates the old header or overwrites its
object; a database trigger also rejects header UPDATE/DELETE operations.

An unsupported coordinate never reaches storage. Upload, integrity, or database
errors return `issuance_failed` for that location and processing continues. A failed
transaction may leave an unreferenced object; it does not publish partial metadata.
No automatic retries or cleanup are added. Preserve the PostgreSQL data and S3
objects together to retain issued history; saving a record does not copy the original
GRIB guidance into that history or extend its existing retention guarantees.

Exit code **0** means every location succeeded; **1** means at least one location
failed, after all locations were processed. **2** reports an unusable config or
dataset, or unavailable storage setup, on standard error. The command rejects older
three-hour snapshots. It loads and verifies guidance once, then reuses the same arrays
for every coordinate without provider calls or per-location preparation. Network I/O
is limited to the configured persistence services. It starts no HTTP server or location
registration/scheduling process. Models, area, temperature scope, and fixed 70/30
demonstration weights are unchanged. `GET /forecast` calls no issuance service and
does not create history, even for coordinates previously issued by a batch.

The earlier calculation-only demonstration on September 10 used HRRR 12Z / GFS 06Z:
an unsupported coordinate was inserted between the two supported points above.
Its example config and prepared snapshot remain available outside Git. With the
storage environment variables configured and the database migrated, the equivalent
PowerShell issuance command is:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$batchDemo = "$env:LOCALAPPDATA\MesoForge\baselines\20260910-coordinate-batch"
$snapshot = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
& $python -B -m alembic upgrade head
& $python -B -m mesoforge.application.batch_forecast --config "$batchDemo\locations.json" --data-dir $snapshot
$LASTEXITCODE # Historical result was 1; automatic coverage now supports all three points.
```

| Input order | Latitude, longitude | Actual result | Hour 1 / hour 36, K (rounded) |
| --- | --- | --- | --- |
| 0 | 45.8, -93.1 | 36 hours; none missing | 283.705085 / 298.287692 |
| 1 | 44.98, -93.27 | Historical rectangle rejection | Now supported after automatic preparation |
| 2 | 45.9, -93.0 | 36 hours; none missing | 283.629142 / 298.173662 |

The earlier calculation-only output is `actual-batch.json` in the external directory
above. A repeated run with network calls blocked produced identical results, loaded the dataset once
(two prepared-file opens), and left the source snapshot unchanged. The first
coordinate's full forecast exactly matches the earlier captured API response.
These are fixed historical model inputs, not a current live forecast.

Prior calculation-only validation: **27 focused batch tests passed**, plus **262 existing API,
preparation/acquisition, and retained Phase 2 tests**. The new tests independently
calculate expected temperatures and check all 36 hours, provenance, continued
processing after bad coordinates/calculation failures, shared loading without I/O,
repeatable CLI output, and exit codes:

```text
python -B -m pytest tests/unit/application/test_batch_forecast.py -q -p no:cacheprovider
```

Ruff lint/format, mypy, nine import contracts, offline lock validation, documentation,
hygiene, and whitespace checks passed. Full database/storage acceptance, the full
coverage gate, and live-provider canaries were not run. Dependencies were unchanged;
no model data was downloaded and no services were started for that earlier milestone.

Issuance validation on September 10: **376 offline tests passed** (34 batch/issuance, 262
retained API/preparation/Phase 2, 20 serializers, and 60 shared application tests).
The storage unit tests use explicit in-memory doubles. **24 integration tests also
passed against actual PostgreSQL 16.2 and MinIO RELEASE.2025-09-07T16-13-09Z**:
issuance/readback, immutable UPDATE/DELETE rejection, explicit missingness,
transaction rollback with continued processing, read-only GETs, migration roundtrips,
and existing S3 integrity/concurrency checks. Mypy, nine import contracts, offline
lock validation, formatting/lint, documentation, hygiene, and whitespace checks passed.

```text
python -B -m pytest tests/unit/application/test_batch_forecast.py tests/unit/application/test_forecast_issuance.py -q -p no:cacheprovider
python -B -m pytest tests/integration/application/test_batch_issuance.py tests/integration/storage/test_migrations.py tests/integration/storage/test_s3_object_store.py -q -p no:cacheprovider
```

The actual batch CLI was run twice with the real 36-hour snapshot and the three-location
config above, using the isolated Python environment and existing storage variables:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& $python -B -m mesoforge.application.batch_forecast --config "$env:LOCALAPPDATA\MesoForge\baselines\20260910-coordinate-batch\locations.json" --data-dir "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
```

Both runs returned `ok, error, ok` and exit **1**, as expected for the unsupported
middle coordinate. Both supported coordinates retained **two distinct versions**, each
with all 36 hours and no missing values. PostgreSQL held **four issuance headers and
four stored-object metadata rows**; MinIO held **four canonical JSON payloads totaling
134,510 bytes**. Readback through a new issuance service reproduced each complete
forecast and provenance exactly, with verified checksums. Two actual localhost GETs
matched the earlier real API response and left both storage counts unchanged.
The original prepared snapshot was unchanged; no guidance was downloaded.

Evidence, batch output, and readback JSON are outside Git at
`%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-forecasts\demonstration-report.json`
and its containing directory. PostgreSQL and MinIO used separate test/demo storage
outside Git and loopback listeners; both services and the temporary API were stopped.
Data files remain locally. No Docker/WSL or permanent service setup was installed.
Full Phase 2 database/storage acceptance, the coverage gate, live-provider canaries,
backup/restore, and production deployment were not validated by this focused check.

### Retrieve one issued version

`GET /issued-forecasts/{issued_forecast_id}` returns the **complete saved JSON envelope**:
issuance ID/time, batch ID, coordinates, target reference time, stored code/dependency
identity, and the nested `forecast` with all values, units, source cycles/leads, valid
times, weights, checksums, and explicit missingness. It uses the existing PostgreSQL
metadata lookup and checksum-verified MinIO/S3 reader. It does not calculate a forecast,
replace its provenance with current code/input identity, or create rows, buckets, or
objects. It returns the saved version even if `/forecast` has different inputs loaded.

Use the same `MESOFORGE_DATABASE_DSN` and `MESOFORGE_S3_*` variables documented for
batch issuance and the existing API startup command. The existing launcher still loads
prepared guidance for `/forecast`; that calculation route needs no storage connection.
Retrieval requires the already-migrated issuance database and its original object
bucket. No additional migration is needed. Example request using a retained local ID:

```text
GET http://127.0.0.1:8765/issued-forecasts/b80e231a-c6e6-4066-ab4c-1e5d38bc2592
```

This is distinct from `GET /forecast?lat=45.8&lon=-93.1`, which calculates from the
currently loaded prepared guidance and returns no issuance header. An unknown valid
UUID returns **404** with `issued_forecast_not_found`; a malformed UUID returns **422**
with `invalid_issued_forecast_id`. Storage unavailability, missing bytes for a known
version, or a checksum failure returns **500** with `issued_forecast_read_failed`.
Retrieval never reconstructs a damaged version or exposes connection details in errors.

September 10 validation: **156 API/batch/issuance/preparation offline tests** and the
**148-test recorded Phase 2 selection** passed. **29 PostgreSQL/MinIO integration
tests** passed, including exact repeated HTTP readback, no calculation/write calls,
invalid/unknown IDs, corrupted/missing payloads, and no bucket creation. Commands run
through the isolated Python 3.12 environment (`PYTHONPATH=src`):

```text
python -B -m pytest tests/unit/test_issued_forecast_api.py tests/unit/test_forecast_api.py tests/unit/test_real_forecast_api.py tests/unit/application/test_batch_forecast.py tests/unit/application/test_forecast_issuance.py tests/unit/application/test_prepared_temperature.py -q -p no:cacheprovider
python -B -m pytest tests/integration/application/test_batch_issuance.py tests/integration/storage/test_migrations.py tests/integration/storage/test_s3_object_store.py -q -p no:cacheprovider
```

Actual localhost requests retrieved the two prior versions for `(45.8, -93.1)`:
`b80e231a-c6e6-4066-ab4c-1e5d38bc2592` and `cb485961-fd26-4927-a670-38831f76b3b3`.
Both returned 200 and matched the complete original JSON and MinIO checksums. Repeated
retrieval, malformed/unknown ID requests, and a normal `/forecast` GET left **four
issuance rows, four metadata rows, and four objects (134,510 bytes)** unchanged.
No new forecasts or guidance downloads were needed. The API, PostgreSQL, and MinIO
were stopped afterward, preserving their data outside Git. Evidence and captured
responses are in `%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-retrieval`.
Quality checks passed; the full acceptance, coverage, live-provider, backup/restore,
and deployment gaps above remain. The read-only observation preview is documented below;
single-hour verification is also documented below.

### Select saved forecast hours

Use the same API startup and PostgreSQL/S3 settings as saved-version retrieval:

```text
GET http://127.0.0.1:8765/issued-forecast-hours?lat=45.8&lon=-93.1&start_valid_time=2026-09-10T13:00:00Z&end_valid_time=2026-09-10T16:00:00Z
```

Coordinates match the stored latitude/longitude exactly; there is no nearest-location
search. Supply timezone-aware ISO timestamps with **start before end**. Offsets normalize
to UTC; the window includes the start and excludes the end (`[start, end)`). Each
matching saved hour appears separately in `results`, including every issued version
for the same valid time. Results sort by valid time, issuance time, then issuance ID.
An empty window match or a valid coordinate with no history returns HTTP 200 and
`"results": []`. Invalid/missing query parameters return 422 with
`invalid_issued_forecast_hour_query`.

Each result contains `issued` (original ID, issuance time, coordinate, batch ID,
target reference time, and the complete saved object's `content_digest`), the stored
`code_identity`, `forecast_context` (all forecast-level metadata except the hours list),
and one unchanged `hour`. That hour retains its valid time, value/unit, model cycles,
leads, weights, source checksums, and missing reasons. Null values remain explicit.
The digest identifies the complete original saved forecast, not this selected view.

Selection reads each saved version for the exact coordinate through the existing
checksum-verified reader and filters its **actual saved valid times**. It neither
regenerates forecasts nor reads observations, scores error, or writes storage. It
does not apply an issuance cutoff or declare a forecast eligible for verification;
historically issued demonstration records retain their distinct issuance/valid times.
No versions are silently dropped at the repository's usual 100-record listing limit.
Selection lists every version's PostgreSQL metadata for the coordinate, then reads
an object payload only when that version's possible valid times —
`target_reference_time` plus the fixed 1–36 h horizons every issuance is validated to
hold — can intersect the `[start, end)` window; matching still uses each saved hour's
actual valid time, so results are unchanged and versions stay distinct. The response's
`version_scan` reports `versions_for_coordinate`, `versions_read` and the prefilter
name; automatic/window verification and forward runs share this path. On the two
retained real Minneapolis versions, an overlapping window still read 2 of 2 objects,
while windows ending at the first valid hour, starting after the last, or outside
the covered days read **0 of 2** where the earlier implementation read all versions.
A four-version demonstration with three target times read 3, 1, 1, 0 and 4 of 4
objects and matched a full scan every time. The existing coordinate/issuance index
serves the metadata query; no index was added. There is still no pagination or
hour-level query index. A storage/integrity failure returns 500 with
`issued_forecast_hour_selection_failed`, without a partial successful result.

On September 10, the example query returned **six matches**: 13:00, 14:00, and 15:00 UTC
from each of `b80e231a-c6e6-4066-ab4c-1e5d38bc2592` and
`cb485961-fd26-4927-a670-38831f76b3b3`. Every hour and its provenance matched the original
stored JSON. A September 12 01:00–03:00 UTC query returned no matches. Repeated queries
left **four forecast rows, four object-metadata rows, and four MinIO objects (134,510
bytes)** unchanged. PostgreSQL, MinIO, and the localhost API were stopped afterward.
Evidence is outside Git in `%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-hour-selection`.

**179 focused offline API/preparation/batch/issuance tests plus 148 retained Phase 2
tests passed**. **33 real PostgreSQL/MinIO integration tests passed**, including a
101-version completeness check, interval boundaries, exact coordinates, empty results,
unchanged storage, and missing/corrupt payloads. The new unit module is
`tests/unit/test_issued_forecast_hours.py`; it was run alongside the six offline modules
listed above. The same three integration modules above were rerun. Quality checks
passed. Full acceptance, coverage, live-provider, backup/restore, and deployment remain
unvalidated. No observations were fetched or verification performed.

### Preview one observation match

Use the existing API startup and PostgreSQL/S3 settings. The operator additionally
sets `MESOFORGE_OBSERVATIONS_ARTIFACT_ID` to one already-retained
`normalized-metar-observations` artifact with schema `metar-observations.v2` in that
storage. The preview loads its referenced station snapshots and original QC
configuration through existing checksum-verified readers. This selects an input
dataset, not a manually chosen station; the saved forecast supplies latitude/longitude.
There is no observation discovery or acquisition in this endpoint.

```text
GET http://127.0.0.1:8765/issued-forecasts/b80e231a-c6e6-4066-ab4c-1e5d38bc2592/observation-match?valid_time=2026-09-10T13:00:00Z
```

The response preserves the saved hour, forecast context and code identity, and returns
the selected station's METAR identity, coordinates, elevation, WGS84 distance, observation
timestamp, Kelvin temperature, QC and raw/revision provenance. `candidates` explains
exclusions. Both **50 km** and **±15 minutes** are inclusive. Eligible candidates rank
by distance, then absolute time difference, then station ID; remaining ties use earlier
observation time and revision identity. The latest retained revision of each logical
observation is chosen before QC. Temperature-specific failures exclude a candidate;
unrelated missing wind does not. The provider's opaque `qcField` is reported as evidence,
not interpreted as a temperature failure by itself.

No acceptable observation, missing forecast temperature, or an unconfigured dataset
returns HTTP 200 with `status: unavailable`, `selected: null`, and an explicit reason.
An empty observation artifact has no referenced station inventory to list. Invalid
IDs or missing/naive times return 422; an unknown saved ID/hour returns 404; damaged
or unavailable configured storage returns 500, not a successful unavailable match.
This is a proxy preview over the configured snapshot, not an issuance-cutoff eligibility
decision, an observation at the forecast coordinate itself, or a forecast skill score.

September 10 localhost demonstration used the existing real-model forecast at
`(45.8, -93.1)` for 13:00 UTC (**283.70508538821554 K**) and clearly labeled synthetic
observation fixtures registered before the requests:

| Station | Distance | Outcome |
| --- | --- | --- |
| KROS | 16.174 km | Selected: 293.15 K at 13:10 UTC, QC eligible |
| KJMR | 16.407 km | Excluded: temperature failed range QC |
| KCBG | 29.878 km | Eligible at 13:00 UTC, but farther away |

The 15:00 UTC hour returned unavailable because no retained observation met the rules.
Repeated requests preserved every PostgreSQL table's contents and all **seven MinIO
objects**, including **four issued forecasts** and three fixture input artifacts.
No verification/error artifacts or activities were created. The API and temporary
services were stopped. Captured responses and the input manifest are outside Git at
`%LOCALAPPDATA%\MesoForge\baselines\20260910-observation-match-preview`.

Executed in the isolated locked environment: the previously listed API/batch/issuance/
preparation modules, `tests/unit/observations`, `tests/unit/test_observation_preview_api.py`,
and the recorded 148-test Phase 2 selection (**477 offline tests** total). The same
three PostgreSQL/MinIO modules plus `tests/integration/application/test_observation_preview.py`
passed (**37 integration tests**). Ruff, formatting, mypy, import contracts, lockfile,
documentation, hygiene, and whitespace checks passed. Live observation handling,
full acceptance/coverage, backup/restore, and deployment remain unvalidated.

### Verify one issued temperature hour

This is an explicit write command; all existing API GET endpoints remain read-only.
Use the same PostgreSQL/S3 settings and `MESOFORGE_OBSERVATIONS_ARTIFACT_ID` as the
observation preview. With the project environment activated and `PYTHONPATH=src`:

```text
python -B -m mesoforge.application.issued_temperature_verification verify --issued-forecast-id b80e231a-c6e6-4066-ab4c-1e5d38bc2592 --valid-time 2026-09-10T18:00:00Z
python -B -m mesoforge.application.issued_temperature_verification read --verification-id art_4a491043-aa94-49e2-b2b3-cb6b579cd902
```

These commands were demonstrated locally using the isolated locked Python 3.12
environment. The IDs refer to retained local demonstration data. `verify` returns
the saved artifact ID, manifest, and result; `read` returns that exact result without
reselecting observations or recalculating. The existing `ArtifactService` stores the
JSON fact in MinIO and its manifest, activity, and input links in PostgreSQL. No new
tables or migrations were needed. The result retains the full match, issued-forecast
ID and checksum, observation revision, source/QC/proxy metadata, rules, and code identity.

The verification cutoff is fixed to the normalized observation artifact's availability
time, making it a comparison against that retained snapshot. Forecast issuance must
strictly precede both valid time and observation time; those times, observation
availability/ingestion, and selected station metadata must be within the cutoff, which
cannot be in the future. Temperatures must be finite Kelvin values and the match must
pass the existing 50 km / ±15-minute / QC rules. Saved model cycles cannot follow
issuance. **Acquisition timestamps are not embedded in saved forecasts**, so this is
not a complete operational input-cutoff audit or a claim of forecast skill.

Ineligible/unavailable attempts return explicit reasons, a null error and null
verification ID, with no score artifact. Exit codes are 0 for a saved/read result,
1 for an ineligible/unavailable attempt, and 2 for invalid input or storage failure.
Identical forecast version/hour, observation snapshot/revision, cutoff, rules, and
code/environment identity reuse the same result, including concurrent requests.
Changed inputs, policy, or code identity produce a separate artifact; older results
remain intact. Verification facts contain no aggregate performance statistics.

The September 10 demonstration used forecast **296.5107933539454 K** at 18:00 UTC,
issued at 17:09:48 UTC, and a **synthetic** KROS observation of **293.15 K** at 18:10 UTC.
The saved error was **+3.3607933539454393 K**, tied to the issued ID above and observation
revision `sha256:86ea1c81d54e31cce73da4a3d4dcda450dba6cf62fae49e1d491f93275a9e650`.
Repeated execution and readback returned the same artifact. An older observation
snapshot returned unavailable for 18:00 UTC; the 13:00 UTC forecast was ineligible
because issuance followed valid/observation time. Neither attempt wrote a score.
All four issued forecasts remained unchanged. One verification artifact/activity
and one 16,525-byte MinIO object were added after fixture setup. Temporary services
were stopped; evidence is outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-issued-temperature-verification`.

Validation: **680 offline tests** passed across the API/batch/issuance/preparation,
observation, verification, artifact-service, and recorded Phase 2 selections.
**45 PostgreSQL/MinIO integration tests** passed, including concurrent retry safety,
independent issued versions, exact readback, explicit ineligibility, and damaged inputs.
The new modules are `tests/unit/verification/test_issued_temperature.py`,
`tests/unit/application/test_issued_temperature_verification.py`, and
`tests/integration/application/test_issued_temperature_verification.py`.
Quality checks passed. That milestone used synthetic observations; the later bounded
real-observation check is below. Full acceptance/coverage, backup/restore, and deployment
remain unvalidated.

### Verify a coordinate and time window

Use the same environment and retained observation artifact as single-hour verification:

```text
python -B -m mesoforge.application.issued_temperature_verification window --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T20:00:00Z
```

The window includes the start and excludes the end. Existing selection returns every
issued version separately; each then uses unchanged matching, eligibility, and
persistence logic. Results contain issued ID, valid time, verification ID, temperature
error, and reasons. Read the complete saved provenance with the existing `read` command.
Summary categories are exclusive: `verified` means newly saved, `already_existing`
means reused with identical input/policy/code identity; `unavailable`, `ineligible`,
and `errors` retain failures without stopping later hours. An empty selection returns
zero counts. Exit 0 means processing completed (including expected unavailable/ineligible
hours), 1 reports per-hour processing errors, and 2 indicates invalid input or failure
to select/start. GET endpoints remain read-only; no observations are downloaded.

The command above was demonstrated with two retained real-model forecast versions and
one synthetic observation dataset. One eligible result was saved beforehand:

| Run | Newly verified | Already existing | Ineligible | Unavailable | Errors |
| --- | ---: | ---: | ---: | ---: | ---: |
| First window | 3 | 1 | 2 | 0 | 0 |
| Repeated window | 0 | 4 | 2 | 0 | 0 |

Both versions' 18:00 and 19:00 UTC hours were eligible; 17:00 UTC preceded issuance.
Repeated execution/readback changed no PostgreSQL rows or MinIO objects. All four
issued forecasts remained unchanged, and temporary services were stopped. Captured
results are outside Git in `%LOCALAPPDATA%\MesoForge\baselines\20260910-verification-window`.
Only focused coverage was added: six window/CLI unit cases and one integration case;
the existing concurrency check also verifies reuse reporting. **56 offline tests**
(verification application and hour selection) and **13 integration tests** (verification
and observation preview) passed, along with applicable quality checks. The integration
window includes an unavailable hour between eligible hours. Broader suites were not
rerun; operational validation gaps remain. The later real-observation demonstration follows.

### Compare temperature models and blends

New forecast responses and immutable issuances include each source's extracted
`temperature` in Kelvin and `missing_reasons`. The production control remains
**70% HRRR / 30% GFS**. No weights are learned or changed by comparison.

Model definitions in `catalog/contributors.py` describe provider, family/lineage,
domain, supported fields, nominal UTC cycles, adapter-supported leads, grid type,
and lifecycle status. Nominal cycles are capability metadata, not proof of provider
availability. Current defaults describe the existing temperature adapter envelope,
not every product the models publish; unknown lineage is left empty.
`forecasting/recipes.py` defines `temperature_control_v1` and `temperature_equal_v1`
as configurations evaluated by the same scalar function. The existing output keys
`blend_70_30` and `blend_50_50` are retained for compatibility.

An explicit batch can load model/recipe JSON without changing the locations file:

```powershell
python -c "from pathlib import Path; from mesoforge.forecasting.recipes import DEFAULT_CONFIGURATION; Path('contributors.json').write_text(DEFAULT_CONFIGURATION.model_dump_json(indent=2), encoding='utf-8')"
python -B -m mesoforge.application.batch_forecast --config locations.json --data-dir PREPARED_DIRECTORY --contributors-config contributors.json
```

The default export contains only HRRR/GFS. A future adapter registers its capabilities
and provides normalized prepared inputs; its metadata and a comparison recipe can be
added to this configuration. The optional RAP and IFS adapters below are real examples.
Batch entry points reject changes to the approved control. Shadow, evaluated, and
deprecated inputs already prepared as `MODEL_ID.nc` can be read with zero active weight;
retired models are not loaded for new forecasts. A real shadow input must carry the
existing manifest/raw/prepared checksum evidence. Synthetic inputs remain labeled.

New issuances save the configuration snapshot and separate `shadow_sources`, including
values, applied active weights, cycles/leads and provenance, in the existing immutable
payload. Shadow missingness does not change active forecast eligibility. Comparison
reads that saved configuration, retains named recipe definitions, and uses the existing
eligibility checks before scoring shadow sources. Missing required contributors produce
null recipe values without weight redistribution. Mixed history lacking a shadow is
explicitly excluded from the common paired sample; different recipe definitions under
one output key cannot be pooled. No historical forecast is rewritten.

The lifecycle is **shadow → evaluated → active → deprecated → retired**. Status is an
explicit configuration decision, not an automatic promotion or weight-learning policy.
Evaluation provides evidence; activation requires an approved versioned recipe change.
Historical snapshots remain readable after later configuration changes.

With the existing PostgreSQL/MinIO environment, compare an exact saved verification:

```powershell
python -B -m mesoforge.application.model_comparison --verification-id art_dd886879-272b-4c54-b3e9-c3e0fa9eb495 --guidance-root "$env:LOCALAPPDATA\MesoForge\prepared"
```

Repeat `--verification-id` or use `--verification-ids-file ids.json`, containing a JSON
list of saved verification IDs. This explicitly selects the observation revision;
the command never rematches observations, generates issuances, or writes storage.
Repeated identical IDs count once. Different issued versions remain separate samples;
selecting two verification revisions of the same issued hour is rejected to avoid
double counting. Missing contributors remain explicit and cannot be inferred from
the blended value.

To view all 36 prediction comparisons without selecting an observation or scoring:

```text
python -B -m mesoforge.application.model_comparison --issued-forecast-id ISSUED_FORECAST_UUID
```

`--guidance-root` is optional for old issuances that lack contributor values. It searches
retained manifests by the exact issued checksum and reuses the existing prepared-input
reader/extraction. Raw/prepared checksums, recorded scientific code/dependencies, source
provenance and the original control must agree. Recovered contributors are labeled
**reconstructed**, never inserted into old records. Older identity records omit the
temporal-alignment module; that limitation and the current extraction identity are
reported. No recovery data is downloaded or prepared. New issuances need no guidance
directory for comparison because their contributor values are already saved.

Verified output preserves issuance/verification IDs, coordinates, source cycles and
leads, valid times, observation revision/QC, and provenance. Errors are prediction minus
observation. Aggregate metrics use the **same complete paired samples** for all requested
predictions (four by default). Buckets **1–6, 7–18, 19–36** use saved horizons since target reference time;
native model leads and elapsed hours since issuance remain separate. Zero samples
produce null metrics. Counts and descriptive statistics are not claims of forecast skill.

Actual September 10 read-only demonstration: **19** previously verified real-observation
hours, with bucket counts **4 / 15 / 0**. At Fresno 22Z, HRRR **314.459054 K**, GFS
**314.750977 K**, control **314.546631 K**, and 50/50 **314.605015 K** were compared
with **313.15 K** observed. Complete rows and metrics are outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-model-comparison`.
Repeat output was identical; all 10 issued versions and all PostgreSQL/MinIO contents
stayed unchanged. No provider calls occurred. These CLI entry points were exercised
using the isolated locked interpreter; `uv run --locked` remains unverified here.

The contributor generalization was checked against committed `c42e3dd`: all **360 real
hours across 10 saved versions** matched exactly, including control values and source
provenance. All 19 real comparison values/errors and bucket metrics also matched.
A separate synthetic fixture batch retained a third contributor at zero active weight;
the 283 K control stayed unchanged while a named three-model comparison returned
287.5 K. Both issued versions and their verification readbacks remained immutable.
Those synthetic numbers demonstrate mechanics, not forecast skill. Raw numerical
output/configuration is outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-generic-contributors`.

Final focused checks: **314 offline tests** and **14 PostgreSQL/MinIO integration tests**
passed, plus Ruff, formatting, typing, import, documentation, hygiene and diff checks.
An initial integration repeat-check failure passed its isolated and complete reruns;
no assertion or production behavior was weakened. Temporary services were stopped.
No provider downloads occurred. Full acceptance/coverage and broader forecast-skill
evaluation were not run. Missing shadow inputs are isolated; malformed shadow files
(wrong units, times or checksums) intentionally fail loading closed.

### Prepare RAP temperature in shadow mode

RAP adds temperature predictions to **new** issued forecasts without changing the active
HRRR/GFS recipe, its source cycles, its prepared files, or historical issued payloads.
Use an existing real 36-hour control dataset and the same coordinates-only locations JSON.
Preparation is explicit and happens before batch issuance or HTTP requests:

```powershell
$control = "$env:LOCALAPPDATA\MesoForge\prepared\automatic-conus\20260910T221229Z-cc93f80b"
$rap = "$env:LOCALAPPDATA\MesoForge\prepared\rap-shadow-20260910T22Z-aws"
python -B -m mesoforge.application.prepared_rap --config locations.json --data-dir $control --output-dir $rap
python -B -m mesoforge.application.batch_forecast --config locations.json --data-dir $control --contributors-config "$rap\contributors.json" --shadow-data "RAP=$rap"
python -B -m mesoforge.application.model_comparison --issued-forecast-id ISSUED_FORECAST_UUID
```

The generated contributor configuration adds RAP in `shadow` status; its applied active
weight is always zero. PostgreSQL/MinIO settings are required for issuance and saved
comparison, not preparation. `--shadow-data MODEL=PATH` is a generic attachment hook;
shadow manifests never enter the active HRRR/GFS raw-rebuild or cache identity.
Nearby locations share views; distant locations use separate internally derived views
from the same acquired messages. No station IDs or geographic metadata are required.
Each model's prepared view is selected in memory for the exact requested coordinate.

The adapter checks inventories in [NOAA's public RAP mirror](https://registry.opendata.aws/noaa-rap/)
and selects the newest observed complete extended cycle at or before the control reference.
[RAP's documented coverage](https://www.nco.ncep.noaa.gov/pmb/products/rap/) is hourly
through lead 21, with 03/09/15/21 UTC cycles extending through 51. Selection aligns by
actual valid time, not matching HRRR/GFS lead numbers. If no complete cycle is found in
the bounded lookback, a partial shadow remains explicit; it never substitutes a new
control cycle or redistributes weights. An optional `--rap-cycle UTC_TIMESTAMP` fixes
the shadow cycle for reproducibility. Corrupt inventories and provider access/rate
rejections stop discovery rather than scanning more cycles.

Only standalone 2 m temperature GRIB messages and their inventories are acquired.
Raw native messages remain outside Git. Prepared files use the decoded RAP Lambert
projection, temperature in K, and coordinate-derived footprints. Each issued shadow
source retains its cycle, lead, URL, raw/index/prepared/manifest hashes, acquisition and
availability times, capability metadata, and lineage. The scientific system RAPv5 and
the documented NOAA deployment package are recorded separately; no patch version is
inferred from a GRIB message. RAP and HRRR share WRF-ARW/GSI lineage, so RAP is not
treated as independent-family evidence.

Repeating the preparation command reuses that snapshot without provider calls. To
rebuild its values from retained raw messages into a new directory, fully offline:

```powershell
python -B -m mesoforge.application.prepared_rap --config locations.json --data-dir $control --from-raw $rap --output-dir "$rap-rebuilt"
```

New output directories preserve prior snapshots. Rebuilt manifests record the original
source-manifest hash and a new preparation identity; numerical values and acquisition
evidence remain reproducible. Missing RAP hours remain null with reasons. Existing
one-off `/forecast` behavior and all Phase 2 defaults remain unchanged.

September 11 UTC validation selected **RAP September 10 21Z**, source leads **2–37**,
for the retained **22Z control reference**: valid times September 10 23Z through
September 12 10Z. Fresno, Wichita and Raleigh each received **36/36 RAP values**.
The successful AWS acquisition transferred **3,956,932 bytes**, including inventories;
retained temperature messages total **2,525,820 bytes**, and three prepared views total
**765,627 bytes**. Earlier NOMADS metadata attempts exposed compound inventory syntax
and rate limiting; neither acquired a GRIB message. One interrupted metadata retry's
transfer total was not captured; the AWS figure is not an all-attempt network total.

All **108 active hours and HRRR/GFS provenance matched committed `76c0b01` exactly**.
Three new immutable PostgreSQL/MinIO issuances retained RAP values and provenance;
all 10 prior versions stayed unchanged. Repeat preparation, offline raw rebuilding,
and read-only comparison passed with zero provider downloads. Independent interpolation
from the raw RAP cells agreed within **4e-13 K**. For example, read Fresno's saved
comparison with `--issued-forecast-id 5414dbd9-86f2-4a7a-9d8d-957087d84975`.
At issuance, these new RAP versions had **0 eligible observed pairs** in each lead bucket;
MAE, bias and RMSE were null. No RAP was attached retroactively or scored against an old
issuance. The first two valid times precede the new issuance and cannot be verified
as forecasts issued in advance. No skill conclusion follows from this demonstration.

**345 focused offline tests and 14 PostgreSQL/MinIO integration tests passed**, along
with lock, Ruff, formatting, mypy, import, documentation, hygiene and diff checks.
The preparation, batch and comparison command entry points were exercised with the
isolated locked Python environment. Full acceptance/coverage and real-observation
RAP scoring were not run. Temporary services were stopped. Captured all-hour batch,
comparison, acquisition and offline reports remain outside Git under
`%LOCALAPPDATA%\MesoForge\baselines\20260911-rap-shadow`.

### Verify saved RAP shadow guidance

The existing automatic verification and read-only comparison commands handle saved RAP
without new production logic. With the PostgreSQL/MinIO settings above and the existing
Fresno/Wichita/Raleigh locations JSON, these entry points were exercised using the
isolated locked Python environment:

```powershell
python -B -m mesoforge.application.automatic_verification --config locations.json --start-valid-time 2026-09-11T02:00:00Z --end-valid-time 2026-09-11T03:00:00Z
python -B -m mesoforge.application.model_comparison --verification-ids-file "$env:LOCALAPPDATA\MesoForge\baselines\20260911-rap-verification\verification-ids.json"
```

The window is end-exclusive. Station candidates were reused automatically, and the
derived **01:45–02:15 UTC** observation request retained **8,623 response bytes** outside
Git. The nearest QC-eligible stations were KFCH (3.00 km, 01:55 UTC), KIAB (9.08 km,
01:55 UTC), and KRDU (18.03 km, 01:51 UTC). No station IDs were supplied by the user.
The initial 01:00 UTC attempt retained another 11,030 bytes but correctly rejected all
three RAP versions: their selected observations preceded issuance at 00:55:26 UTC.

The 02:00 UTC window saved nine verification facts, keeping overlapping versions separate.
The comparison IDs file selects only the **three RAP-bearing issued versions**. Each
of HRRR, GFS, RAP, 70/30 and 50/50 uses these same three observed samples. All are
target-reference horizon 4 (**1–6 bucket: 3; 7–18: 0; 19–36: 0**); empty buckets have
null metrics. For example, Fresno RAP **309.923567 K** versus **311.15 K** observed
has error **−1.226433 K**. Individual values/errors, metrics, source cycles/leads,
observation revisions, station candidates/QC, raw provenance and checksums are captured
under `%LOCALAPPDATA%\MesoForge\baselines\20260911-rap-verification`.

Independent calculations matched all errors, MAE, mean bias and RMSE. The same-checkout
repeat reused all nine facts with provider calls blocked and no PostgreSQL/MinIO changes;
comparison output was identical. All **13 historical issued payloads** remained exact,
including the active HRRR/GFS values and RAP's zero active weight. No forecast was generated.
**125 focused offline tests and six PostgreSQL/MinIO integration tests passed**, covering
RAP eligibility/missingness, identical sample sets, separate versions and read-only readback.
Formatting/lint, documentation/hygiene and whitespace checks passed. Temporary services
were stopped. Full acceptance/coverage and broader skill evaluation were not run; three
pairs at one valid time cannot establish long-term model rankings or justify new weights.

### Prepare ECMWF IFS temperature in shadow mode

IFS uses the official [ECMWF open-data feed](https://www.ecmwf.int/en/forecasts/datasets/open-data-0)
through its public AWS mirror: deterministic `ifs/0p25/oper/fc`, 2-m temperature only.
It shares RAP's preparation/retention flow and the generic contributor reader, recipes,
issuance and comparison path. Its regular latitude/longitude grid is normalized with
the decoded geometry. No IFS-specific forecasting, verification or storage path was added.
Both RAP and IFS have **zero active weight**; HRRR 70% / GFS 30% remains unchanged.

Using the existing coordinates-only `locations.json` and PostgreSQL/MinIO environment:

```powershell
$control = "$env:LOCALAPPDATA\MesoForge\prepared\automatic-conus\20260910T221229Z-cc93f80b"
$rap = "$env:LOCALAPPDATA\MesoForge\prepared\rap-shadow-20260910T22Z-aws"
$ifs = "$env:LOCALAPPDATA\MesoForge\prepared\ifs-shadow-20260910T22Z"
python -B -m mesoforge.application.prepared_ifs --config locations.json --data-dir $control --output-dir $ifs
python -B -m mesoforge.application.batch_forecast --config locations.json --data-dir $control --contributors-config "$ifs\contributors.json" --shadow-data "RAP=$rap" --shadow-data "IFS=$ifs"
python -B -m mesoforge.application.model_comparison --issued-forecast-id cbf81fca-287a-43a6-8c26-9ab46a3db398
```

Preparation checks actual provider inventories for the newest usable cycle at or before
the control reference time, preferring complete native guidance and falling back to older
cycles when needed. The adapter checks up to 24 hours back and supports native leads
0–90 every three hours. If no complete cycle is found, partial shadow guidance is reported
explicitly. Add `--ifs-cycle 2026-09-10T18:00:00Z` to preparation for an explicit override;
the existing output directory must describe that same cycle or a new directory is needed.
Neither nominal cycle time nor control reference time implies provider availability;
actual availability and acquisition timestamps are retained separately.

IFS stays **natively three-hourly**. Matching uses actual valid times, never equal source
lead numbers. Intervening forecast hours report null IFS temperature with
`IFS: no guidance for this valid time`; they do not interpolate or change active weights.
The decoder checks the documented IFS CY50R1 identity and native grid/time/unit contract.
Manifests and issued shadow rows retain capabilities/lineage, source cycle/lead, URLs,
byte ranges, hashes, acquisition/availability times, and source attribution.

Repeating preparation reuses the snapshot without provider calls. To rebuild offline
from retained raw messages into a new directory:

```powershell
python -B -m mesoforge.application.prepared_ifs --config locations.json --data-dir $control --from-raw $ifs --output-dir "${ifs}-rebuilt"
```

These entry points and offline rebuilding were exercised with the isolated locked
interpreter. The explicit-cycle override was tested offline; `uv run --locked` was not
executed. Preparation remains outside HTTP requests. Raw data stays outside Git.

The September 11 UTC demonstration selected **September 10 18Z IFS**, source leads
**6, 9, …, 39**, aligned to the existing **September 10 22Z** reference. Each location
received 12 native IFS values at **September 11 00Z through September 12 09Z, every
three hours** (forecast hours 2, 5, …, 35), and 24 explicitly missing IFS hours.
Temperature examples at **September 11 03Z**, in K, rounded here only:

| Location | HRRR | GFS | RAP (shadow) | IFS (shadow) | Active 70/30 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fresno | 307.885474 | 309.872959 | 307.311420 | 309.767937 | 308.481720 |
| Wichita | 296.079291 | 297.440890 | 296.246030 | 295.963318 | 296.487770 |
| Raleigh | 299.792906 | 299.133767 | 299.381343 | 298.929347 | 299.595164 |

The bounded preparation received **8,839,654 bytes**, plus a **40,025-byte** metadata
preflight. Retained unique temperature messages occupy **7,875,280 bytes**, indexes
**482,187 bytes**, and three regional prepared views **114,604 bytes**. Each message
was acquired once and shared across regions; retained evidence is copied into regional
snapshots. HRRR/GFS and RAP were reused without downloads. Offline rebuilding reproduced
all values exactly; independent spatial interpolation from raw IFS cells agreed within
**6e-14 K**. No temporal interpolation was performed.

Against committed `855d681`, all **108 active hours and 108 RAP values**, including their
source provenance, were exactly unchanged. Three new immutable issuances were read back
from PostgreSQL/MinIO; all **13 previous issued versions** stayed unchanged. Repeat
comparison created no rows or objects. Existing real RAP comparison results also matched.
The new IFS versions were issued at **02:38 UTC**: native 00Z preceded issuance and the
next native 03Z was still future. Thus there were **zero eligible real IFS/observation
pairs** at validation, zero samples in every lead bucket, and null metrics. No observations
were downloaded or comparison window manufactured. Later comparisons use the same complete
paired native valid times for every included model/recipe and observation.

**287 focused offline tests and five PostgreSQL/MinIO integration tests passed**, including
synthetic four-model paired calculations, native missingness, separate issued versions,
readback and unchanged history. Ruff, formatting, typing, import contracts, documentation,
hygiene and diff checks passed. Full acceptance/coverage and real IFS skill evaluation
were not run. Temporary services were stopped. Full all-hour outputs, immutable IDs,
acquisition evidence and regression/rebuild reports remain outside Git under
`%LOCALAPPDATA%\MesoForge\baselines\20260911-ifs-shadow`.

ECMWF data is used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), with
[ECMWF's attribution terms](https://apps.ecmwf.int/datasets/licences/general/):
**This service is based on data and products of the European Centre for Medium-Range
Weather Forecasts (ECMWF).** MesoForge extracts temperature messages and regional/point
values; it does not temporally interpolate IFS. Source/licence/attribution and these
modifications are recorded with retained provenance. No ECMWF endorsement is implied.

### Bounded historical temperature backtest

`application.historical_backtest` reads existing prepared guidance and immutable
observation artifacts. It calls the existing point forecast, observation selection,
forecast eligibility and comparison functions; it neither downloads data nor writes
issued forecasts or verification facts. It reports all four contributors, the unchanged
70/30 control and 50/50 comparison, selected observation/revision/QC, individual errors,
and identical-sample MAE/bias/RMSE overall, by location and by lead bucket.

The first real experiment used Fresno `(36.7378, -119.7871)`, Wichita
`(37.6872, -97.3301)` and Raleigh `(35.7796, -78.6382)`. The historical decision time
was **September 10, 2026 14Z**; native comparison times were **15Z, 18Z, 21Z and
September 11 00Z**. The original prepared target remains 12Z. Buckets use hours since
the retrospective 14Z decision (1, 4, 7, 10), with original prepared horizons
(3, 6, 9, 12), source leads and cycles preserved separately.

| Model | Fixed source cycle, September 10 UTC | Source leads | Latest required publication |
| --- | --- | --- | --- |
| HRRR | 12Z, retained | 3, 6, 9, 12 | 13:07:51Z |
| GFS | 06Z, retained | 9, 12, 15, 18 | 09:37:43Z |
| RAP | 09Z | 6, 9, 12, 15 | 09:52:51Z |
| IFS | 06Z | 9, 12, 15, 18 | 12:27:05Z |

Publication uses recorded GRIB/index Last-Modified evidence, not nominal run times.
The earlier IFS 18Z demonstration was unsuitable for its 22Z control cutoff: it was
published after midnight. The chosen earlier cycles pass the 14Z publication cutoff.
Actual acquisition and station-metadata timestamps remain later and unchanged. Thus
this is a **provider-available hindcast**, not proof that MesoForge possessed or issued
these inputs at 14Z. Original publication/revision evidence is necessary for larger
archive experiments; a later archive copy's timestamp cannot establish earlier availability.

The existing shadow preparation commands now accept `--hours` to acquire only needed
target hours. Defaults remain all 36 hours. Repeat/offline rebuilding must supply the
same subset; unrequested slots remain explicitly missing. Using the existing locations JSON:

```powershell
$control = "$env:LOCALAPPDATA\MesoForge\prepared\20260910-conus-lifecycle"
$rap = "$env:LOCALAPPDATA\MesoForge\prepared\historical-20260910T14Z-rap"
$ifs = "$env:LOCALAPPDATA\MesoForge\prepared\historical-20260910T14Z-ifs"
python -B -m mesoforge.application.prepared_rap --config locations.json --data-dir $control --output-dir $rap --rap-cycle 2026-09-10T09:00:00Z --hours 3 6 9 12
python -B -m mesoforge.application.prepared_ifs --config locations.json --data-dir $control --output-dir $ifs --ifs-cycle 2026-09-10T06:00:00Z --hours 3 6 9 12
```

Observation preparation reused saved station discovery, then the existing
`prepared_observations.acquire_for_valid_times()` and `prepare_bundle()` functions.
For each coordinate, one bundle covers 15Z–21Z and one covers 00Z, each padded ±15 minutes;
the existing six-hour acquisition bound is unchanged. The equivalent CLI preparation
for the first bundle is below; repeat for the other coordinates with separate raw directories:

```powershell
python -B -m mesoforge.application.prepared_observations --raw-dir NEW_RAW_DIRECTORY --lat 36.7378 --lon -119.7871 --start-valid-time 2026-09-10T15:00:00Z --end-valid-time 2026-09-10T21:00:00Z
python -B -m mesoforge.application.prepared_observations --raw-dir RETAINED_RAW_DIRECTORY --from-raw
```

Save the returned observation artifact IDs as a JSON list. With the existing local
PostgreSQL/MinIO settings, the exercised replay command is:

```powershell
$run = "$env:LOCALAPPDATA\MesoForge\baselines\20260911-historical-backtest"
python -B -m mesoforge.application.historical_backtest --config "$run\locations.json" --data-dir $control --contributors-config "$ifs\contributors.json" --shadow-data "RAP=$rap" --shadow-data "IFS=$ifs" --observation-ids-file "$run\observation-ids.json" --as-of 2026-09-10T14:00:00Z --start-valid-time 2026-09-10T15:00:00Z --end-valid-time 2026-09-11T00:00:00Z --evaluation-cutoff 2026-09-11T03:03:23.997769Z
```

Both valid-window endpoints are **inclusive**. The optional evaluation cutoff selects
retained observation revisions available by that time; preserve it for exact replay.
Each execution has a new report run ID and actual execution time, with a stable input
digest for fixed inputs/code/cutoff. Reports are analysis output, not issued history.
The complete three-hourly intersection has **12 samples**, four per location. Buckets
1–6 and 7–18 have **six each**; 19–36 has **zero** and null metrics. The 18 intervening
location-hours are excluded from every aggregate because no shadow messages were
requested there; no temporal interpolation or weight redistribution occurs.

| Prediction | Samples | MAE (K) | Mean bias (K) | RMSE (K) |
| --- | ---: | ---: | ---: | ---: |
| HRRR | 12 | 1.169261 | 0.976808 | 1.497897 |
| GFS | 12 | 2.262840 | 2.189094 | 2.668673 |
| RAP | 12 | 1.245844 | -0.224209 | 1.537875 |
| IFS | 12 | 1.990748 | 1.029377 | 2.310410 |
| 70/30 control | 12 | 1.428596 | 1.340494 | 1.764522 |
| 50/50 comparison | 12 | 1.666951 | 1.582951 | 1.994016 |

All four Fresno observations used KFCH (3.003 km). Wichita used KICT (9.853 km) at
15Z and KIAB (9.077 km) thereafter; Raleigh used KRDU (18.028 km). Selection followed
the existing QC, 50 km and ±15-minute rules, with no manual station IDs. Complete
candidate explanations, source/observation provenance, all errors, and per-location/
bucket metrics are retained in `backtest.json` and `results.md` under `$run` above.

The experiment acquired **3,391,293 model bytes** plus **141,542 METAR bytes** (311
normalized reports). No HRRR/GFS or station metadata was downloaded. Newly retained
unique model messages total **2,910,471 bytes**, indexes **240,411 bytes**, and regional
prepared shadow views **179,495 bytes**. Including reused guidance, unique retained
model messages total **66,309,771 bytes**; regional copies and storage metadata add
disk usage. Raw observation data and derived artifacts use existing storage.

Validation: **168 focused offline tests passed**. The real PostgreSQL/MinIO replay,
repeat, normalized-observation reuse and raw shadow rebuild passed, with zero provider
calls during repeats. Independent arithmetic matched every error and summary; all
**108 active forecast hours and 16 issued versions remained unchanged**. Read-only
replay created no rows or objects. Quality checks passed and temporary services were
stopped. These commands/functions used the isolated locked interpreter; the standalone
observation CLI example above was not rerun separately, and `uv run --locked` remains
unverified. Full acceptance/coverage was not run.

Twelve pairs from one day do not establish a winner. The existing scientific and
comparison functions are reusable at larger scale; this command is deliberately one
bounded in-memory window, not a bulk backfill engine. Archive completeness, historical
model/product versions (including older IFS contracts), availability evidence, station
history, and independent weather-event/time/location splits must be addressed before
a much larger training dataset. No production weights changed; RAP and IFS remain shadows.

### Discover and reuse nearby METAR stations

The geographic configuration still contains only latitude/longitude. With the existing
PostgreSQL/MinIO environment, discover candidates independently of forecast eligibility:

```text
python -B -m mesoforge.application.station_discovery --config locations.json
python -B -m mesoforge.application.station_discovery --lat 36.7378 --lon -119.7871
```

The [official Aviation Weather Center station-information API](https://aviationweather.gov/data/api/)
supports bounded queries. MesoForge derives a small bounding box internally, queries
`/api/data/stationinfo`, keeps METAR-capable candidates at a WGS84 distance of at most
50 km, and orders them deterministically. It does not download a nationwide catalog.
Dateline footprints use two bounded queries. Failed, oversized, malformed or possibly
truncated provider responses are errors rather than cached empty results.

Successful discovery, including a genuinely empty list, is saved with the existing
artifact service. PostgreSQL retains manifests, coordinate/policy lookup metadata and
lineage; MinIO retains exact raw responses and the immutable candidate snapshot.
The snapshot includes station ID/network, coordinates, elevation when available,
distance, metadata source, acquisition time, query bounds and raw checksums.
A repeated coordinate loads the saved snapshot without a station-discovery request.
A coordinate lock prevents concurrent first runs from rediscovering it independently.
Discovery has a versioned policy and an internal explicit-refresh option, preserving
older snapshots; there is no scheduled refresh or automatic expiry yet.

The existing automatic verification command invokes this lookup only when saved hours
are ready, then reuses the same METAR acquisition, normalization, QC, station ranking,
and verification persistence. With no ready hours it downloads neither station metadata
nor observations. The standalone discovery command can still prepare station candidates
in advance. A new station snapshot cannot silently reuse observations normalized against
an older snapshot. Normal forecast GET requests do not trigger discovery or verification.

A discovered station is a candidate, not a promise of a usable observation. The unchanged
QC requires metadata comparisons, including elevation: a missing elevation is saved as
null and reported as unavailable for that QC, never filled with zero or an invented value.
Station metadata acquired today can reject older reports if their metadata differs beyond
the existing tolerance. Nearest eligible station, ±15-minute matching, QC, and deterministic
ties are unchanged. The retained Phase 2 station catalog and historical readers remain;
the new coordinate workflow no longer selects candidates from Minnesota's fixed list.

Use the existing verification command; station IDs and padded observation windows are
still derived internally:

```text
python -B -m mesoforge.application.automatic_verification --config locations.json --start-valid-time 2026-09-10T20:00:00Z --end-valid-time 2026-09-10T23:00:00Z
```

These command forms use the isolated interpreter documented above; the `uv run --locked`
wrapper remains unverified here. Both discovery CLI forms and the automatic-verification
command were executed on September 10; the discovery CLI reused the saved snapshots.

The four-coordinate demonstration supplied only these coordinates. Candidate distances
below are rounded to 0.1 km; saved metadata retains full precision.

| Coordinate | Discovered METAR candidates and distance (km) |
| --- | --- |
| Fresno: 36.7378, -119.7871 | KFCH 3.0; KFAT 7.6; KO32 30.9; KMAE 39.8; KHJO 49.5 |
| Wichita: 37.6872, -97.3301 | KIAB 9.1; KICT 9.9; KBEC 10.2; KAAO 12.0; K1K1 21.3; K3AU 22.2; KEGT 40.5; KEWK 41.9; KEQA 46.5 |
| Raleigh: 35.7796, -78.6382 | KRDU 18.0; KJNX 34.9; KTQV 36.9; KLHZ 38.5; KHRJ 45.3; KTTA 47.8 |
| Minnesota: 45.8, -93.1 | KROS 16.2; KJMR 16.4; K04W 29.4; KCBG 29.9; KPNM 48.0 |

Four bounded metadata requests downloaded **7,180 bytes** and saved four immutable
snapshots containing all metadata listed above. Repeat discovery made zero provider
calls and created no PostgreSQL rows or MinIO objects. The real verification run used
**30,486 bytes** of METAR observations and saved **9** results: one at each CONUS
coordinate and six across two Minnesota issued versions. Six earlier CONUS hours were
not issued before their valid times; explicit reasons were returned without scores.
Repeat verification reused the observations and all nine results without network calls
or storage changes. All **10** existing issued forecasts stayed unchanged.

Focused checks passed: **109 offline tests** covering discovery, retention, preparation,
automatic verification and existing AWC parsing/acquisition; **16 PostgreSQL/MinIO
integration tests** in the existing verification and observation-preview modules.
The final provenance assertion also passed on rerun. Ruff, mypy, all nine import
contracts, documentation/hygiene and whitespace checks passed. Evidence and raw data
remain outside Git under `%LOCALAPPDATA%\MesoForge\baselines\20260910-station-discovery`
and existing artifact storage. Temporary PostgreSQL and MinIO were stopped.
Full acceptance/coverage, scheduled refresh and provider
reliability remain unverified; current metadata does not prove historical station validity.

### Prepare one MRMS hourly QPE reference

`prepared_mrms` retains one fixed hourly NOAA MRMS QPE grid and its matching
gauge-influence/radar-quality grids, then saves a compact coordinate extraction through
the existing PostgreSQL/MinIO artifact path. MRMS is an **analysis reference**, not
perfect truth or an exact point gauge. The current versioned contract is
`mrms.multisensor-qpe-01h-pass2.v1`, restricted to `MultiSensor_QPE_01H_Pass2`
(GRIB discipline/category/parameter **209/6/37**, mm).

[NOAA's Multi-Sensor QPE documentation](https://vlab.noaa.gov/web/wdtd/-/multi-sensor-qpe)
defines the indicated time as the accumulation end. For this one-hour product,
MesoForge maps indicated time `T` to **`(T-1h, T]`**. The current GRIB template 4.0
does **not** encode those statistical bounds; the documented product contract supplies
them. Contradictory encoded metadata, wrong product identity or unsupported grids fail
validation. NOAA documents hourly updates and approximately one-hour latency; actual
acquisition time is retained separately and is not backdated to that nominal latency.

Extraction policy `mrms.nearest-native-gridpoint.wgs84.v1` selects the nearest native
point on the CONUS 0.01-degree grid without interpolation. It retains row/column,
scanning index, native coordinate, configured coordinate, grid identity and geodesic
separation. It represents analysed QPE associated with the requested coordinate.
Numeric zero, positive amount, native `-1` missing and `-3` no coverage remain distinct;
malformed inputs fail. MRMS has no trace interpretation here. Units and sentinels come
from the [NOAA local-parameter table](https://www.nssl.noaa.gov/projects/mrms/operational/tables.php),
including when the decoder reports the local parameter's units as `unknown`.

`GaugeInflIndex_01H_Pass2` (209/8/17) and `RadarAccumulationQualityIndex_01H`
(209/8/10) are retained as dimensionless support evidence at the same indicated time,
native grid and selected cell. They are not probabilities or complete uncertainty
estimates. No quality cutoff is approved. Their hourly association is retained without
claiming they encode the QPE accumulation bounds. MRMS may use model-based gap filling;
it is not wholly independent of numerical guidance.

With the existing PostgreSQL/MinIO environment configured, acquire **one fixed hour**
into a new directory outside Git, then extract another coordinate from those same files:

```sh
python -m mesoforge.application.prepared_mrms --time 2026-09-24T12:00:00Z --lat 44.98859 --lon -93.25557 --raw-dir /outside-git/mrms-20260924-12
python -m mesoforge.application.prepared_mrms --from-raw --lat 45.016 --lon -94.264 --raw-dir /outside-git/mrms-20260924-12
python -m mesoforge.application.prepared_mrms --replay-artifact <extraction-artifact-id>
```

The fixed acquisition URL has limited operational retention, so the historical command
is illustrative once those files expire. `--from-raw` uses the retained bundle;
`--replay-artifact` reparses the immutable raw objects and checks exact canonical
extraction equality without provider calls or storage writes. Original gzip bytes,
URLs, checksums, response identity, acquisition time and parsed metadata are retained;
PostgreSQL holds manifests/lineage, MinIO holds content-addressed payloads. Different
source bytes create distinct revisions. The extraction references the shared raw grids
instead of embedding millions of grid values. QPF verification/scoring remains the
next separate milestone.

Verified fixed-hour demonstration: the three retained `20260924-120000` files total
**8,222,622 compressed bytes** (QPE 721,721; gauge influence 4,280,177; radar quality
3,220,724). For `(11:00Z,12:00Z]`, coordinate `45.016,-94.264` selects native
`45.015,-94.265` (row 998, column 3573; 136.249 m) with **1.4 mm** QPE,
gauge influence **0.078** and radar quality **1**. Minneapolis `44.98859,-93.25557`
selects `44.985,-93.255` (row 1001, column 3674; 401.487 m) with **0 mm** and both
support values **1**. This establishes the source contract, not a forecast-verification
sample or a skill claim. The two saved extractions are about 17.3 KB each and share
three raw artifacts. Offline reparse took 0.84–0.86 seconds per extraction, reproduced
canonical output exactly, and created no records/objects. The retained-input and
artifact-replay CLI modes were exercised against isolated PostgreSQL/MinIO; the online
CLI acquisition uses a tested transport boundary, but was not rerun over these already
retained real files. **30 focused MRMS tests, 283 retained observation/storage/provenance
tests and 31 storage integration tests passed**, plus Ruff, formatting, mypy, import
contracts, lock consistency, docs and hygiene checks. Missing/no-coverage cases use
actual GRIB fixtures. Full repository/forecast acceptance tests were not run for this
source-only change.

### Prepare one real METAR dataset

[Observation preparation](src/mesoforge/application/prepared_observations.py) reuses the
retained AviationWeather.gov acquisition, strict parser, Phase 2 normalizer, and
PostgreSQL/MinIO artifact path. It now uses coordinate-driven discovery and saves
nearby METAR candidates within 50 km; station IDs come from the official metadata
response, not the locations file. Older retained bundles still read their original
configuration-pinned station snapshot. New discovery captures metadata at acquisition
time; it does not assert historical station metadata validity. Matching, QC, and
verification math are unchanged. An opaque provider `qcField` is preserved alongside existing field QC;
its numeric value is not interpreted as a newly invented pass/fail rule.

With the same configured PostgreSQL/MinIO environment as verification, the acquisition
command is below. `$python` is the isolated interpreter and `PYTHONPATH` points to `src`
as shown earlier. Use a **new directory outside the repository** for a new acquisition;
this demonstrated directory already exists, so use `--from-raw` to reuse it.

```powershell
$observations = "$env:LOCALAPPDATA\MesoForge\observations\20260910T17-1930Z-grasston-metar"
& $python -B -m mesoforge.application.prepared_observations --raw-dir $observations --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T19:30:00Z
# Rebuild/register from retained bytes, without constructing an HTTP transport:
& $python -B -m mesoforge.application.prepared_observations --raw-dir $observations --from-raw
```

The acquisition function was exercised with those exact inputs; the equivalent first
CLI wrapper was not used for the real request. The `--from-raw` CLI was executed twice.
Preparation prints `observations_artifact_id`; set it before running the **unchanged**
window command. These commands were executed against the retained demonstration:

```powershell
$env:MESOFORGE_OBSERVATIONS_ARTIFACT_ID = 'art_0071726d-b60a-4df2-a215-35b341db342e'
& $python -B -m mesoforge.application.issued_temperature_verification window --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T19:30:00Z
```

Preparation accepts up to six hours per explicit request and pads its bounds by
15 minutes; the entire padded interval must be in the past. It uses the official
[METAR API's date and hours query](https://aviationweather.gov/data/api/) through the
existing retry/rate-limit adapter. This example requested **16:45–19:45 UTC** and
received **27 reports (nine each from KCBG, KJMR, KROS), 11,146 response bytes**, at
19:59:25 UTC on September 10. All normalized successfully. Exact response bytes,
URL, headers, status, acquisition time, configuration/code identity, and checksums
remain in `metar.json`, `manifest.json`, and `configuration.json` outside Git.
The existing adapter represents HTTP 204 as canonical `[]`.
Raw and normalized payloads live in MinIO; PostgreSQL holds their manifests,
configuration, transformation lineage, and verification metadata. Observation event,
report, provider-receipt, and ingestion times stay distinct. Original record indices,
logical/revision digests, raw checksums, and station snapshot references are retained.

At 18:00 UTC, the 17:55 and 18:15 reports at all three stations passed temperature QC.
KROS won at **16.174 km**, ahead of KJMR (16.407 km) and KCBG (29.878 km); its 17:55
report was closer in time than its 18:15 report. The other 21 reports were explicitly
outside the time tolerance. The selected real temperature was **296.25 K**;
the saved forecast was **296.510793354 K**, giving **+0.260793354 K** error.
At 19:00 UTC the error was **+0.201772811 K**. Both issued versions were verified
independently at each hour: **four new records**, then **four reused** on repeat.
Both 17:00 hours remained ineligible because issuance followed the valid/observation
time; no scores were fabricated. There were no unavailable hours or processing errors.

Raw readback matched the downloaded bytes. Offline preparation returned the same
artifact, and repeating preparation/verification changed no rows or objects. All four
issued forecast records and payloads remained unchanged. Evidence is outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-real-metar`.
Focused validation passed **70 offline tests** (new preparation plus existing provider
parser/acquisition/normalization tests) and **10 PostgreSQL/MinIO integration tests**
(existing verification module, including one new preparation-to-window case).
The integration fixture covers a missing hour and blocks new observation HTTP transport
construction after acquisition. No dependencies, policies, or forecast calculations changed.
Ruff lint/format, mypy, all nine import contracts, documentation/hygiene checks, and
`git diff --check` passed.
Full acceptance/coverage, operational input-cutoff validation, long-term provider
reliability, and station metadata history remain unverified; this is not a skill claim.
Temporary services were stopped after validation. No polling or scheduling was added.

### Automatically prepare observations and verify

With the existing PostgreSQL/MinIO services and storage environment configured,
provide only latitude, longitude, and the saved forecast valid-time window:

```text
python -B -m mesoforge.application.automatic_verification --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T21:00:00Z
```

This exact command was demonstrated using the isolated interpreter and `PYTHONPATH=src`.
It selects all issued versions in `[start, end)` and reuses the existing forecast-only
eligibility checks. A finite Kelvin forecast must have valid source cycles, no missingness,
and issuance before its valid time. Future hours and incomplete ±15-minute observation
margins are deferred. These are **preflight** checks: station/observation QC, observation
timing relative to issuance, and retained-input eligibility are still decided by the
existing verifier after preparation. No observation is invented for preflight.

For ready valid times, acquisition spans exactly **earliest time − 15 minutes** through
**latest time + 15 minutes**. A single hour requests 30 minutes; duplicate issued versions
do not widen the request. The retained six-hour limit applies to the span between ready
valid times; a wider span fails explicitly before downloading rather than truncating hours.
Station IDs come from the same retained catalog and 50 km rule. Users supply neither
station IDs nor METAR query bounds. No models, forecast fields, weights, or matching rules change.

The command first looks for an existing real observation preparation covering that
coordinate, station set, time range, and configuration. It checks stored source/normalized
checksums and transformation provenance and reuses the first suitable retained snapshot.
Synthetic fixtures do not qualify. Otherwise it uses the existing acquisition/normalization
and artifact storage. Raw bundles default to `%LOCALAPPDATA%\MesoForge\observations` on
Windows, or `$XDG_DATA_HOME/MesoForge/observations` (otherwise `~/.local/share/MesoForge/observations`)
on Linux. `MESOFORGE_OBSERVATIONS_DIR` optionally changes this service-level path; it must
remain outside Git. The result reports the source URL, stations, bounds, bytes, raw directory
when newly acquired, and observation artifact ID. Raw files can still use `--from-raw`.

The existing verification window implementation then processes the original requested
window, preserving all issued versions and explicit reasons. Its results are separate
from preflight: an hour rejected before acquisition can subsequently appear as unavailable
with its issuance/missingness reason because no observation was acquired for it.
When **no hours are ready**, the command returns `status: "nothing_to_verify"`, per-hour
reasons, `verification: null`, and `downloaded_bytes: 0`, without observation acquisition
or preparation. Exit 0 means completed or nothing to verify, 1 means per-hour verification
errors, and 2 means invalid input or orchestration failure.

The real September 10 demonstration found two issued versions at each of 18:00, 19:00,
and 20:00 UTC. It automatically requested **17:45–20:15 UTC** from **KCBG/KJMR/KROS**,
receiving **24 METAR reports (eight each), 9,934 bytes**. Six verification facts were saved.
For one issued version, errors were **+0.260793 K**, **+0.201773 K**, and **+0.365296 K**
at those respective hours; the other version was independently verified with the same values.
Both 17:00 hours failed preflight because issuance was later than valid time. The unchanged
window verifier reported them unavailable with that explicit reason; no scores were saved.
Repeating the command reused the exact observation artifact and all six verification IDs,
with zero downloads and no new rows or objects. This no-eligible command also made no writes
or downloads:

```text
python -B -m mesoforge.application.automatic_verification --lat 45.8 --lon -93.1 --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T18:00:00Z
```

Issued forecasts remained unchanged. Evidence and exact saved results are outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-automatic-verification`.
**75 offline tests passed** in `test_automatic_verification.py`, `test_prepared_observations.py`,
and `tests/unit/verification/test_issued_temperature.py`; **11 PostgreSQL/MinIO integration
tests passed** in the existing verification module. New coverage is limited to nine unit
cases and one integration case: exact bounds, separate versions, no-ready behavior, real
input discovery, synthetic exclusion, overlapping-window reuse, unavailable hours, and
corrupt retained evidence failing without reacquisition. Quality checks passed and temporary
services were stopped.

Repeats intentionally keep a fixed observation snapshot, including missing reports; this
command does not refresh delayed/corrected observations automatically. Verification reuse
continues to require the same inputs, policies, and code identity (including Git HEAD).
Changing code or choosing a different snapshot can create another auditable verification
fact; older facts remain immutable. Full acceptance, coverage, operational cutoff validation,
and provider reliability remain unverified. This remains an on-demand command; the
coordinate-list extension below adds sequential processing without scheduling.

### Verify configured locations sequentially

Use the same locations JSON as batch forecast issuance. For example, `locations.json`:

```json
{
  "locations": [
    {"lat": 45.8, "lon": -93.1},
    {"lat": 44.98, "lon": -93.27},
    {"lat": 45.9, "lon": -93.0}
  ]
}
```

With the existing storage settings/services and isolated interpreter, run:

```text
python -B -m mesoforge.application.automatic_verification --config locations.json --start-valid-time 2026-09-10T17:00:00Z --end-valid-time 2026-09-10T21:00:00Z
```

This command was executed with the example config saved outside Git in
`%LOCALAPPDATA%\MesoForge\baselines\20260910-batch-verification\locations.json`.
Use either `--config` or `--lat/--lon`. The shared time window is validated before
processing. Each supported coordinate calls the existing automatic verification path
in config order; it derives its own eligible hours, stations, and bounded observation
request. No observation geography or model data path belongs in the locations JSON.
No new forecast is generated or issued by this command.

Each result has its original `index` and `location`, a status, the verification summary,
and the complete single-location `result` with provenance. Unsupported/malformed
coordinates and runtime failures have an explicit `error` and do not stop later entries.
Per-hour processing failures also mark that location as an error while preserving its
successful and failed hour results. No-ready locations return `nothing_to_verify` and
perform zero observation downloads. Existing snapshot/reuse rules remain unchanged.
The batch summary counts `completed`, `nothing_to_verify`, and `errors` locations.
Exit 0 means no location errors, 1 means processing finished with location/hour errors,
and 2 means unusable config or global arguments. The former middle-coordinate
rectangle rejection is retired. With no saved eligible hours it now returns
`nothing_to_verify`; eligible hours use saved or newly discovered station candidates.
If no candidates can pass the existing metadata QC, the result is explicitly
`unavailable` with zero METAR observation downloads; the batch counts those separately.

Historical result before automatic spatial coverage removed the fixed rectangle:

| Coordinate | First run | Repeat | Observation bytes: first / repeat |
| --- | --- | --- | ---: |
| 45.8, -93.1 | 6 verified | 6 reused | 0 / 0 |
| 44.98, -93.27 | Historical rectangle rejection | Historical rectangle rejection | No acquisition |
| 45.9, -93.0 | 6 verified | 6 reused | 9,934 / 0 |

The supported coordinates each had two issued versions at 18:00–20:00 UTC; their
17:00 hours remained unscored with explicit late-issuance reasons. The first location
reused its retained real snapshot. The second acquired 24 METAR reports from KCBG,
KJMR, and KROS for **17:45–20:15 UTC**, through the same preparation path. Repeating
preserved all 12 verification IDs and made no PostgreSQL/MinIO writes. A second batch
ending at 18:00 returned `nothing_to_verify` for both supported locations, with zero
downloads/writes, while still reporting the unsupported coordinate. All four issued
forecast records and their complete payloads remained unchanged.

Evidence is in `%LOCALAPPDATA%\MesoForge\baselines\20260910-batch-verification`.
**43 offline tests passed** in the automatic-verification and batch-forecast unit modules;
**2 integration tests passed** using `-k 'automatic_batch or automatic_window'` in the
existing verification integration module. New coverage is six unit cases and one
integration case focused on sequential processing, failure isolation, no-work results,
reuse, and immutable forecasts. Ruff, mypy, import contracts, documentation/hygiene
checks, and `git diff --check` passed. Temporary services were stopped. Broader acceptance,
coverage, and operational reliability were not tested in this increment.

### Automatic spatial coverage

The locations file contains **only lat/lon**. Each point remains exact; MesoForge
internally derives a minimum **50 km preparation buffer** and **150 km context
footprint**, conservatively enclosed in geographic rectangles. It prepares the larger
footprint plus native interpolation cells, merges overlapping footprints, and handles
distant groups separately. These defaults live in
[spatial coverage](src/mesoforge/application/spatial_coverage.py), not in locations JSON.
The observation search stays **50 km**, using discovered/saved stations and existing suitability
rules. That spatial milestone added no station discovery, fields, or weather-dependent sizing.
At a physical model edge, context is clipped to the available domain and the exact
forecast point is checked separately. The 150 km footprint currently retains temperature
only; it is not a new weather-context analysis product.

```json
{"locations": [
  {"lat": 45.8, "lon": -93.1},
  {"lat": 44.98, "lon": -93.27},
  {"lat": 45.9, "lon": -93.0}
]}
```

From the repository root, with the existing locked interpreter and `PYTHONPATH=src`:

```text
python -B -m mesoforge.application.prepared_temperature --config locations.json --from-raw RETAINED_SNAPSHOT --output-dir COVERAGE_DIR
python -B -m mesoforge.api --data-dir COVERAGE_DIR
```

A single point can use `--lat 44.98 --lon -93.27` instead of `--config`.
Open `http://127.0.0.1:8765/forecast?lat=44.98&lon=-93.27`. The coverage directory
contains a small local index of shared prepared snapshots. The API loads those
snapshots before accepting requests. Normal GET requests still create no history.
Existing batch issuance also ensures the entire collection's coverage automatically:

```text
python -B -m mesoforge.application.batch_forecast --config locations.json --data-dir COVERAGE_DIR
```

Batch issuance needs the existing PostgreSQL/MinIO settings and services. Geographic
expansion reuses checksum-verified full raw messages from the selected source cycles;
it does not redownload a model for each coordinate. Original snapshots remain intact.
A repeat reuses sufficient prepared regions; corrupt/incomplete evidence is reported,
not silently replaced with different inputs. Missing hourly temperatures remain explicit
and the fixed 70/30 demonstration weights never change.

For a **new explicit cycle pair**, use the same preparation command with
`--config locations.json`, `--target-reference-time`, `--hrrr-cycle`, and `--gfs-cycle`
instead of `--from-raw`. It plans the collection first, acquires the selected temperature
messages once, and prepares the needed regions before HTTP startup. Repeating the same
command/output/cycles reuses retained data. Omitting all three time arguments now
selects complete current guidance automatically, as described below. There is no scheduling.

The spatial demonstration reused HRRR **2026-09-10 12Z**, GFS **06Z**, target **12Z**:
HRRR leads 1–36 and GFS leads 7–42 share valid times 13Z September 10 through 00Z
September 12. All three points share one derived context region, approximately
**43.62345–47.25655 N, -95.18797–-91.05049 E**. Downloaded for this expansion: **0 bytes**.
Retained raw temperature messages: **63,399,300 bytes**; inventories: **1,861,814 bytes**.
Original acquisition: **65,261,114 bytes**. Raw hashes, source URLs/ranges, retrieval
metadata, cycles/leads, prepared hashes, and preparation code identity are preserved.
The shared HRRR/GFS prepared files total **5,029,082 bytes**. The localhost API
returned **36/36 hours for each point, with zero missing hours**:

| Exact coordinate | First temperature (K) | Last temperature (K) |
| --- | ---: | ---: |
| 45.8, -93.1 | 283.705085 | 298.287692 |
| 44.98, -93.27 | 287.999758 | 300.493731 |
| 45.9, -93.0 | 283.629142 | 298.173662 |

Independent native-grid interpolation and 70/30 arithmetic matched all 108 values.
A repeat/offline run, with network access blocked during preparation, reused the
shared region and reproduced exact forecasts. HTTP repeats also matched. The original
raw/source files remained unchanged, and the localhost server was stopped.
Evidence and complete hourly responses are outside Git under
`%LOCALAPPDATA%\MesoForge\baselines\20260910-spatial-coverage`.

Preparation and API commands were exercised using the isolated Windows interpreter
(substitute actual paths); portable `uv run --locked` wrappers remain unexecuted.
Batch issuance was checked with existing in-memory storage fixtures in this milestone.
No new provider acquisition, database service, observation acquisition, or full acceptance suite was
needed for this spatial demonstration. The focused checks cover footprints, curved
native-grid bounds, shared/offline preparation, actual-point extraction, missingness,
HTTP read-only behavior, issuance/readback, and the retired rectangle's consumers.
**229 focused tests passed** across the spatial geometry/preparation, temperature
preparation/API, batch forecast, automatic verification, observation preparation,
issuance, and saved-version API modules. Ruff check/format, mypy (`src scripts`), all
nine import contracts, documentation/hygiene checks, and `git diff --check` passed.
PostgreSQL/MinIO integration and the full acceptance/coverage gates were not rerun.
The new spatial checks alone can be run with:

```text
python -B -m pytest tests/unit/application/test_spatial_coverage.py tests/unit/application/test_spatial_preparation.py -q -p no:cacheprovider
```

The final shared index is at
`%LOCALAPPDATA%\MesoForge\prepared\20260910-spatial-coverage-final`.
The preparation command and equivalent default-port startup command are:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$locations = "$env:LOCALAPPDATA\MesoForge\baselines\20260910-spatial-coverage\locations.json"
$raw = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
$coverage = "$env:LOCALAPPDATA\MesoForge\prepared\20260910-spatial-coverage-final"
& $python -B -m mesoforge.application.prepared_temperature --config $locations --from-raw $raw --output-dir $coverage
& $python -B -m mesoforge.api --data-dir $coverage
```

The demonstration used an ephemeral localhost port; the startup command's default is
8765. Stop the API with Ctrl+C.

Only fixed demonstration-region restrictions were retired: the hardcoded temperature
crop, the rectangle validator and its consumers, and the redundant observation-prep
rectangle gate. Boundary tests now distinguish native support from insufficient
prepared coverage. Shared scientific functions, synthetic fixtures, Phase 2 defaults,
observation matching policy, historical readers, and storage remain in place.

### Prepare real inputs before serving

[The preparation module](src/mesoforge/application/prepared_temperature.py) reuses
the HRRR/GFS acquisition and strict GRIB decoders plus existing projection/subsetting
functions. It selects only 2 m temperature, retains each complete acquired message
and inventory, and writes small native-grid subsets with a one-cell halo. HRRR stays
on its Lambert grid; GFS retains its geographic grid and north-to-south value order.
The six-field Phase 2 normalizers and Phase 2 defaults are unchanged.

The existing acquisition command already supports different fixed cycles without
code changes. Supply explicit UTC whole-hour timestamps ending in `Z`. The current
source contract permits cycles at **00/06/12/18Z**, at or before the target reference,
with all selected model leads at most **48 hours**. Each source cycle must therefore
be no more than **12 hours** older than the target for this 36-hour window.
For each requested valid time,
the model lead is `target reference + horizon - source cycle`; matching model lead
numbers is not required. These explicit overrides do not substitute different cycles
when a requested pair is unavailable. Omit all three arguments for automatic selection.

The command now downloads **72 selected temperature messages**, one per model per
hour, so run it only when acquiring a new dataset. Choose
a new, empty directory outside Git; occupied directories are refused. The example
directory is already populated on this machine, so use the startup command above
to serve it again. The equivalent portable command is below; its `uv run` wrapper
has not been executed.

```text
uv run --locked python -m mesoforge.application.prepared_temperature --config locations.json --output-dir OUTPUT_DIR --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
```

Equivalent PowerShell preparation and startup with the installed isolated environment:

```powershell
$python = "$env:LOCALAPPDATA\MesoForge\baselines\20260909-8d0983f-d6c8ced2\environment\Scripts\python.exe"
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$snapshot = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36"
& $python -B -m mesoforge.application.prepared_temperature --config locations.json --output-dir $snapshot --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
# Run startup only after preparation succeeds; stop the server with Ctrl+C.
& $python -B -m mesoforge.api --data-dir $snapshot
```

The one acquisition for this expansion retrieved **65,261,114 HTTP body bytes**
(about **65.26 MB / 62.24 MiB**): **63,399,300** bytes in 72 raw temperature messages
and **1,861,814** bytes in 72 inventories. No requested hours were missing.
The earlier three-hour snapshots remain unchanged in `20260910T06Z-hrrr06-gfs00`
and `20260910T12Z-hrrr12-gfs06` under the same external `prepared` directory.
HRRR came from NOAA's AWS archive; GFS from its Google Cloud archive. The streaming
transport caps each inventory at 1 MiB, each selected message at 16 MiB, and the run
at **128 MiB** for the expanded 72-message run, rejecting responses that ignore
byte ranges. No other fields are selected. If acquisition or decoding fails, the
command stops with an error and retains acquired raw files; it does not publish a
completed manifest or invent replacements for unavailable inputs.

The manifest preserves URLs, byte ranges, retrieval and provider timestamps, cycles,
leads, valid times, hashes, configuration/code identity, and decoder versions.
Provider availability and retrieval time are distinct; this manual historical
demonstration does not apply an operational issuance cutoff. Raw messages remain
intact outside Git even though the prepared views use only the small region.

### Discover the current four-model set

Run this once for the entire coordinate collection. No model cycles, target time,
station IDs or geographic metadata are required:

```text
python -B -m mesoforge.application.current_model_set --output-dir EXTERNAL_NEW_SELECTION_DIRECTORY
```

Use the isolated interpreter documented above. This command form was executed on
Windows; the `uv run --locked python` wrapper remains unverified here. The output
directory must be new and outside the repository. The command prints its report
and retains `selection.json` plus the original downloaded inventories there.
An optional `--decision-time` accepts a timezone-aware instant at or before
execution within the current UTC hour; omitting it uses execution time. It is not
a historical replay option.

The UTC whole-hour floor of the decision time defines the reference. HRRR, GFS
and RAP each require **every hourly temperature lead** for hours 1–36. IFS requires
every native three-hourly valid time in that same window: 12 values, with the other
24 hours explicitly listed as native gaps and never interpolated. Cycles are aligned
by actual valid time. RAP and IFS stay zero-weight shadows; the active HRRR/GFS
70/30 demonstration recipe and Phase 2 defaults are unchanged.

The selector considers newest capable cycles first, using the existing model
registrations, URL builders, inventory selectors and HTTP retry behavior. A
candidate's last required lead is checked first, then every interior lead. Each
check downloads an inventory and makes a GRIB **HEAD** request; it downloads no
GRIB body. Both objects must have provider publication timestamps at or before
the fixed decision time. The selected temperature range must fit the GRIB object's
reported length, with a strong ETag retained for later identity checks.

An unpublished or incomplete candidate can yield to an older complete cycle within
the existing adapter lead limits and a bounded lookback, with rejection reasons
recorded. Access/transport failures, malformed inventories, or unprovable object
identity/publication stop selection explicitly. The narrow exception is a final
mirror's 403 after independent 404 evidence: that candidate has no proved usable
endpoint, and the denied mirror remains unknown; an older cycle still needs complete
evidence. Success requires all four models;
failure returns an empty `selected_cycles`, a reason, and exit code 2. Individual
model findings in a failed report are diagnostic evidence, not an approved partial
set. A selection expires at its first valid hour, requiring a new current run.

The retained evidence includes each cycle, lead and valid time; candidate rejection
reasons; request URLs, timestamps and response headers; provider Last-Modified and
ETag values; temperature byte ranges; inventory SHA-256 hashes; and source,
contributor and code configuration identities. This establishes provider-metadata
evidence, not that GRIB contents were decoded or locally ingested by decision time.
The separate preparation step must revalidate these exact identities and availability,
decode units/grids/model versions/valid times, and retain its actual acquisition and
issuance times. Use the selected-set command below to enforce that handoff;
passing cycle arguments to the older preparation commands alone does not pin objects.

Real discovery on **September 11, 2026**, at decision time **16:43:43.475839Z**
selected the following set for reference **16Z**, valid **September 11 17Z through
September 13 04Z**:

| Model | Selected September 11 cycle | Required source leads | Latest selected object publication (UTC) |
| --- | --- | --- | --- |
| HRRR | 12Z | 5–40, hourly | 13:37:48 |
| GFS | 12Z | 5–40, hourly | 15:45:04 |
| RAP | 15Z | 2–37, hourly | 16:09:26 |
| IFS | 06Z | 12–45, every three hours | 12:27:09 |

RAP 16Z lacked the required lead coverage. IFS 12Z's required inventory returned
HTTP 404; the complete 06Z cycle was then checked and accepted. The successful run
retained **120 inventories / 3,059,411 bytes**, with 120 successful GRIB HEAD checks
and no GRIB body downloads. IFS valid times run from September 11 18Z through
September 13 03Z; the intervening 24 hours remain explicit native gaps. All selected
publication timestamps precede the decision cutoff, and discovery finished at
16:46:00Z before the first valid hour.

An earlier attempt at 16:39:39Z stopped safely after three ECMWF HTTP 503 responses,
returning no selected set. Its failure evidence is retained too. Both attempts total
**5,636,268 downloaded inventory bytes**, with zero model-data acquisition. Their
reports and checksum audit are outside Git under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-current-model-set`.

Validation: **55 focused offline tests passed**:

```text
python -B -m pytest tests/unit/application/test_current_model_set.py tests/unit/guidance/test_current_availability.py -q
```

Ruff format/lint, mypy, all nine import contracts, locked dependency consistency,
documentation/hygiene checks and `git diff --check` passed. Tests cover incomplete
interior leads, native-time alignment, bounded fallback, unsafe provider evidence,
decision cutoffs/expiry, retained hashes and unchanged contributor weights/statuses.
The real metadata audit also passed. No preparation, issuance, observations or services
ran for this milestone; GRIB decoding, storage integration and full acceptance were
not rerun.

### Prepare and issue the exact selected model set

With the existing PostgreSQL/MinIO environment settings configured, run these
commands from the repository root using the isolated interpreter:

```text
python -B -m mesoforge.application.current_model_set --output-dir EXTERNAL_NEW_SELECTION_DIRECTORY
python -B -m mesoforge.application.selected_forecast --config locations.json --selection EXTERNAL_NEW_SELECTION_DIRECTORY/selection.json --output-dir EXTERNAL_NEW_PREPARED_DIRECTORY --pop-fields
```

The locations file still contains only coordinates, for example:

```json
{"locations":[{"lat":36.7378,"lon":-119.7871},{"lat":37.6872,"lon":-97.3301},{"lat":35.7796,"lon":-78.6382}]}
```

No source cycles or target reference time are supplied. The second command consumes
the exact successful selection and its retained inventories. It checks completeness,
inventory hashes and expiry, then pins each acquisition to the selected URL, range,
ETag and provider publication time. Range requests use `If-Match`; changed objects,
unselected mirrors or incomplete required native guidance stop the run before
issuance. Retained inputs and a failure report stay outside Git. Use a new discovery
and output directory after a failed or expired selection; the command does not
silently choose replacement cycles.

The acquired HRRR/GFS messages feed the existing normalization and coverage code.
RAP and IFS use their existing shadow adapters at the selected cycles, with shared
validated inventories. The complete coordinate collection determines regional views;
distant locations need no giant shared subset and no separate model downloads.
HRRR/GFS keep the 70/30 control, RAP/IFS have zero active weight, and IFS retains its
12 native slots plus 24 explicit gaps. Phase 2 defaults and forecast calculations
are unchanged.

The command then uses existing batch issuance, returning each coordinate's forecast
and immutable ID, or an explicit error while continuing to later coordinates.
Issuance refuses elapsed first forecast hours. Each saved payload includes the
complete `current_model_set` decision evidence, original selection hash, preparation
module hashes, and validated provider request/response identities, alongside the
existing contributor values, raw hashes and acquisition times. PostgreSQL retains
issuance metadata and the object digest; MinIO retains the complete immutable payload.
The original discovery inventories and raw GRIB messages remain outside Git in the
external preparation directory.

Read an exact version using the existing `GET /issued-forecasts/RETURNED_ID` endpoint
or the existing readback function with the same storage settings:

```text
python -B -c "import json; from uuid import UUID; from mesoforge.application.issuance import read_issued_forecast; print(json.dumps(read_issued_forecast(UUID('RETURNED_ID')), indent=2))"
```

Readback does not recalculate forecasts or create history. Existing prepared/offline
batch commands remain available for deliberate reissuance from retained inputs;
they do not perform a new current-model discovery. This milestone adds no scheduler,
VPS deployment, observations or historical backfill.

Real end-to-end validation on **September 11, 2026** used decision time
**17:10:06Z**, automatically selecting **HRRR 12Z / GFS 12Z / RAP 15Z / IFS 06Z**.
All forecasts cover **September 11 18Z through September 13 05Z** and were issued
around **17:20:48Z**, before the first valid hour. No cycle arguments were supplied.

| Location | Immutable issued-forecast ID | Hours | First / last temperature (K) |
| --- | --- | --- | --- |
| Fresno | `0fd90b7c-feb0-405c-94aa-8782d7badcf5` | 36 | 306.415551 / 296.229429 |
| Wichita | `209f1622-7c60-4c84-9bba-3e9eadd7df1f` | 36 | 302.946247 / 295.827867 |
| Raleigh | `ce8c8491-7727-4af7-bd65-43aa3b021d62` | 36 | 308.146496 / 296.701427 |

An invalid `95.0, -93.0` entry between Fresno and Wichita returned
`unsupported_coordinate`, with no issuance, and both later locations succeeded.
Each successful forecast carries the same selection and validated object evidence,
36 RAP values, 12 native IFS values and 24 explicit IFS gaps. Every active value
matched calculation without shadows exactly. Readback and an offline prepared-data
repeat matched all three saved payloads; the 16 older versions were unchanged.
Readback created no rows or objects. Exactly three new issuance rows, their stored
object metadata, and three MinIO payload objects were created.

The successful preparation made **120 unique temperature range downloads**, one
per selected native model/lead, totaling **76,982,295 bytes** including inventories;
**73,921,510 bytes** are raw temperature messages. All 360 index/HEAD/range checks
matched discovery. Three regional views shared those inputs, totaling **12,440,910
control bytes**, **765,627 RAP bytes**, and **114,604 IFS bytes**. Raw messages and
full evidence remain outside Git; no coordinate caused a duplicate model download.

The first attempt caught a manifest-finalization bug after acquisition and before
issuance. The fix atomically finalizes only the newly created, unpublished manifest;
a focused regression now reproduces the real helper's exclusive file creation.
One bounded rerun used the same unexpired selection in a new output directory.
Including that retained failed attempt and discovery, this demonstration downloaded
**157,025,375 bytes**. Reports and all hourly responses are under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-selected-issuance`; the completed prepared
set is `%LOCALAPPDATA%/MesoForge/prepared/selected-20260911T171801Z`.

**322 focused offline tests and 16 PostgreSQL/MinIO integration tests passed**,
including changed-object rejection, selection-copy races, native missingness,
immutable readback and existing comparison/verification coverage. Ruff, mypy,
all nine import contracts, lock consistency, documentation/hygiene checks and
`git diff --check` passed. Temporary PostgreSQL and MinIO were stopped. Full
repository acceptance/coverage, VPS operation and scheduling were not tested.

### Run verification and current issuance together

The on-demand command uses the same PostgreSQL/MinIO settings as batch issuance
and automatic verification. With those development services running, use the
existing isolated Python environment from the repository root:

```json
{"locations":[{"lat":44.98859,"lon":-93.25557,"name":"Minneapolis"}]}
```

```text
python -B -m mesoforge.application.forward_run --config locations.json --output-dir EXTERNAL_NEW_RUN_DIRECTORY --display-timezone America/Chicago
```

Only `lat` and `lon` are required geographic inputs. `name` is optional display
metadata. `--display-timezone` is also optional: UTC is the default. It controls
report presentation, never model/station selection or forecast values; automatic
coordinate-to-timezone lookup is not implemented. America/Chicago displays this
Minneapolis demonstration in local time, including the correct date and UTC offset.

The command snapshots the coordinate list, selects previously saved hours before
the run time, and applies the existing eligibility and ±15-minute observation rules.
Ready valid times are grouped into bounded windows spanning at most six hours and
passed to existing automatic verification. Exact issued versions stay independent;
retained observations and saved verification facts are reused. No eligible hours
means no observation download. Unavailable observations or a failed verification
window do not prevent new issuance. One failed coordinate does not stop the others.

After verification, one current four-model discovery and one shared preparation
serve the entire collection. Provider identities are checked against that exact
selection. The command does not accept manually specified cycles, stations, or
regions. A missing/expired/changed current set produces an explicit issuance failure,
while completed verification results remain saved. Raw model/observation evidence
stays outside Git. A new run needs a new output directory and intentionally issues
new immutable forecast versions; only verification facts are reused idempotently.

The output directory contains `result.json`, the input snapshot, previous-verification
results, current selection/preparation evidence, and a readable `hourly-report.md`.
Each successful forecast saves `hourly_report` beside its original `hours` through
the existing immutable PostgreSQL/MinIO path. `hours` retains the untouched numerical
baseline; `hours[].surface` adds canonical surface values, per-model contributors,
units, applied weight rows and their hashes, raw-message provenance, and explicit
missingness. The readable report uses °F, %, mph and meteorological compass direction;
stored Kelvin, m/s and degree values stay unrounded. Each row includes:

- UTC and display/local valid time; HRRR, GFS, RAP, IFS and the raw 70/30 blend.
- Dew point, relative humidity, sustained wind speed/direction, and gust, with
  separate contributor tables and explicit missing/fallback reasons.
- `bias_correction.status = not_implemented`, with applied delta **0 K**.
- `ai_adjustment.action = not_run`, applied delta **0 K**, and reason
  **AI forecast-desk stage not implemented yet**.
- Final temperature exactly equal to the raw baseline, including null/missingness.
- `verification.status = not_yet_verified` for this new immutable version and
  `delivery_status = not_delivered`. Earlier versions' verification is reported separately.

Zero here means no correction was applied; it is not an estimated site bias or an
AI judgment. No AI, bias-learning, delivery, scheduler or deployment stage runs.
IFS keeps its native three-hourly values and explicit intervening gaps. RAP/IFS
remain zero-weight shadows, and Phase 2 defaults remain unchanged. Saved reports
describe the state at issuance; later verification does not rewrite them.

Surface policy and availability:

- **Temperature:** unchanged HRRR/GFS 70/30 for all 36 hours; a missing required
  temperature contributor still makes that hour's active temperature unavailable.
- **Dew point and coupled U/V/gust:** use the retained Phase 2 scalar/vector fallback
  table: HRRR/GFS **70/30 for hours 1–18**, **60/40 for hours 19–36** when both are
  eligible. Explicit single-model rows apply only when their eligibility rules allow
  them; weights are never silently renormalized. The policy snapshot, row ID/hash,
  exclusions and actual applied weights are saved. Phase 2 defaults are unchanged.
- **Humidity:** derive `100 × e(Td) / es(T)` from baseline temperature/dew point using
  the [Bolton liquid-water equations documented by UCAR](https://archive.eol.ucar.edu/projects/ceop/dm/documents/refdata_report/eqns.html),
  including below freezing. It is not separately weighted model RH. Inconsistent
  dew point or invalid humidity produces an explicit unavailable result, not clipping.
- **Wind:** rotate native grid U/V to earth-relative components before point extraction;
  blend U/V, then derive speed and meteorological direction. Calm direction is null.
  Retained gust consistency rules reject an invalid source wind/gust tuple or record
  an allowed small source-gust floor. Native contributor values remain separately saved.
- **Shadows:** RAP exposes its available instantaneous fields. IFS exposes native
  three-hourly temperature, dew point and vector winds, with no time interpolation.
  Its published gust is an interval maximum, so it is explicitly unavailable under
  this instantaneous-gust contract. [ECMWF attribution](#prepare-ecmwf-ifs-temperature-in-shadow-mode)
  and source/licence metadata remain attached.
- **Cloud cover:** this core surface step does not acquire it. The separate
  [native cloud attachment](#native-cloud-cover-on-the-local-grid) now enables the
  temporary NBM sky baseline; without it, cloud remains explicitly unavailable.
  Bias correction and AI are absent.
- **Liquid amounts:** [interval-aware QPF](#liquid-precipitation-on-the-local-grid)
  now follows the same grid and issuance path. Amounts do not imply probability or type.

The normal forward command enables these fields automatically. For the separate
discovery/preparation sequence, opt into field evidence when discovering:

```text
python -B -m mesoforge.application.current_model_set --surface-fields --qpf-fields --output-dir EXTERNAL_NEW_SELECTION_DIRECTORY
python -B -m mesoforge.application.selected_forecast --config locations.json --selection EXTERNAL_NEW_SELECTION_DIRECTORY/selection.json --output-dir EXTERNAL_NEW_PREPARED_DIRECTORY
```

Preparation acquires only field ranges recorded in that selection, once for the
coordinate collection; RAP's shared U/V message is downloaded and retained once.
Every range remains pinned to the discovered object identity. Missing optional
fields stay explicit and do not change temperature cycle selection. Raw messages,
inventories and metadata stay outside Git and support offline rebuilding.

The real surface run exposed a provider-discovery edge case: two GFS mirrors
returned 404 for a not-yet-usable candidate, then the final mirror returned 403.
That combination now rejects only that candidate; the 403 stays recorded as
access denied with **unknown** availability, never as proof of absence. An older
candidate must pass every existing completeness/identity check. Access denial
without independent missing-object evidence, rate limits, malformed evidence and
transport failures still stop selection explicitly.

The real **surface** run for Minneapolis completed on **September 11, 2026 at
18:34:28Z**, issuing `9588a3d3-41a8-42fa-851f-936079c83743`. Automatic discovery at
18:27:34Z selected **HRRR 12Z / GFS 12Z / RAP 15Z / IFS 06Z**, covering
**September 11 19Z through September 13 06Z**. All 36 hours have every supported
baseline surface field. HRRR/GFS/RAP supplied 36 native hours each; IFS supplied
12 native temperature/dew-point/wind slots with 24 explicit gaps and no instantaneous
gust. Cloud cover is unavailable throughout. One actual hour, rounded for display:

**September 11, 16:00 CDT / 21:00 UTC**

| Source | Temperature °F | Dew point °F | RH % | Wind mph | From degrees | Gust mph |
| --- | --- | --- | --- | --- | --- | --- |
| HRRR | 84.1 | 50.3 | 31.0 | 18.6 | 188.6 | 32.2 |
| GFS | 88.7 | 50.4 | 27.0 | 14.5 | 194.3 | 27.6 |
| RAP shadow | 83.2 | 54.4 | 37.1 | 18.9 | 188.6 | 31.7 |
| IFS shadow | 86.3 | 50.7 | 29.4 | 16.6 | 185.2 | unavailable |
| Active baseline / final | 85.5 | 50.3 | 29.8 | 17.4 | 190.1 | 30.8 |

The successful model discovery/preparation downloaded **490,482,913 bytes**,
retaining **484,358,119 unique raw GRIB bytes** plus inventories and provenance
outside Git. Its four prepared NetCDF files total **19,219,528 bytes**. There were
**552 distinct GRIB range requests** (HRRR 180, GFS 180, RAP 144, IFS 48), all
matching the selected objects. Raw bytes may also have retained/rebuilt regional
copies on disk; these figures count unique source messages. The initial failed
discovery downloaded another 379,527 metadata bytes and no GRIB.

Exact PostgreSQL/MinIO readback matched the complete saved forecast. All **20 older
issued versions** stayed unchanged; all **35 overlapping temperature hours** matched
the earlier Minneapolis version exactly, including individual HRRR/GFS values.
The real saved-forecast GET returned that exact payload. Repeated prepared-forecast
GETs returned identical baseline surface fields with no PostgreSQL/MinIO changes;
these HTTP checks used FastAPI TestClient, without a listening server.
Offline point replay made zero provider calls. Rebuilding all four datasets from
retained raw messages with HTTP blocked reproduced every array, coordinate, unit,
missingness marker and all 36 point forecasts exactly. Raw provenance and decision
evidence were preserved; newly generated preparation/manifest hashes differ as
expected. The previous Minneapolis 18Z forecast
was verified against a real automatically selected METAR observation, with error
**−0.956694 K** (forecast minus observation). Repeat verification with the same build
reused its saved result and observations with zero downloads. The initial attempt
acquired 4,773 METAR bytes and 2,257 station-metadata bytes; the completed run reused
them. Code identities distinguish the verification versions created while this
milestone was being validated.

All 36 baseline rows, contributor tables and explicit missing reasons are in
`%LOCALAPPDATA%/MesoForge/forward-runs/surface-20260911T182732Z/hourly-report.md`;
the full payload/evidence is in that directory's `result.json`. Validation artifacts
are under `%LOCALAPPDATA%/MesoForge/baselines/20260911-surface-forward`.
Temporary PostgreSQL and MinIO were stopped after validation.

Surface validation used the locked local environment. **560 distinct focused
offline tests and 19 PostgreSQL/MinIO integration tests passed** across the relevant
selections. Coverage includes independent RH/vector calculations, units, missing
contributors, source cycle/lead/grid mismatches, source-gust consistency, exact
temperature preservation, offline field rebuilding, changed provider objects and
immutable readback. The integration fixture compares each coordinate against the
same coordinate's temperature-only baseline; both supported locations succeed
around an invalid middle entry and earlier saved payloads stay unchanged.

New-field scientific/preparation selections (executed in smaller groups):

```text
python -B -m pytest -q tests/unit/forecasting/test_surface.py tests/unit/forecasting/test_scalar_blend.py tests/unit/forecasting/test_vector_blend.py tests/unit/forecasting/test_gust_blend.py tests/unit/forecasting/test_availability.py
python -B -m pytest -q tests/unit/application/test_surface_forecast.py tests/unit/application/test_prepared_temperature.py tests/unit/application/test_prepared_shadow.py tests/unit/application/test_prepared_rap.py tests/unit/application/test_prepared_ifs.py
python -B -m pytest -q tests/unit/guidance/test_current_availability.py tests/unit/guidance/test_selected_objects.py tests/unit/guidance/test_rap_temperature.py tests/unit/guidance/test_ifs_temperature.py
```

Existing forward-run, selected issuance, API, spatial coverage, normalization and
automatic-verification selections were also run, including the storage commands
below. No additional field is scored against observations in this milestone.
Full acceptance/coverage, live-provider canary suites and VPS reliability remain
unverified.
Ruff, mypy, all nine import contracts, lock consistency, documentation/hygiene
checks and `git diff --check` passed.

The earlier **temperature-only** Minneapolis command completed on
**September 11, 2026 at 17:46:15Z**.
Decision time **17:41:36Z** selected **HRRR 12Z / GFS 12Z / RAP 15Z / IFS 06Z**
without cycle arguments. It saved immutable forecast
`b3b32ab2-8da7-4adf-b3e6-0f59bb148051`, covering **September 11 18Z through
September 13 05Z** (September 11 1 PM through September 13 midnight CDT).
All 36 active/RAP values exist; IFS has 12 native values and 24 explicit gaps.
One actual hourly row, rounded only for display:

| Local / UTC valid time | HRRR °F | GFS °F | RAP °F | IFS °F | Raw blend °F | Bias delta °F | AI action / nudge °F | Final °F | Verification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Sept 11 13:00 CDT / 18:00Z | 81.7 | 86.7 | 82.6 | 84.6 | 83.2 | 0.0 (not implemented) | not_run / 0.0 | 83.2 | not_yet_verified |

There were no earlier eligible saved hours at this exact Minneapolis coordinate:
verification returned `nothing_to_verify`, with **zero observation downloads**.
Repeating verification also performed no downloads or writes; no observations were
manufactured. The eligible/idempotent branch was exercised by the storage integration
case below. Exact readback and an offline prepared-guidance calculation matched the
saved forecast. All **19 older issued versions** remained unchanged; one issuance row,
its stored-object metadata, and one MinIO payload were added. Model discovery and
preparation downloaded **80,043,080 bytes** in total, including **73,921,510 raw GRIB
bytes** retained outside Git. Preparation used 120 unique temperature messages and
all 360 provider-identity checks matched the discovery evidence. Offline replay made
zero provider calls. Full JSON and all 36 report rows remain under
`%LOCALAPPDATA%/MesoForge/forward-runs/20260911T174135Z`; demonstration checks are under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-forward-run`. Temporary services were stopped.

Validation on September 11, 2026: **207 focused offline tests and 18 PostgreSQL/MinIO
integration tests passed**. The new integration case uses generated model/observation
fixtures with real storage: two previous versions produce four verification facts
and eight unavailable hour results; repeat execution reuses all four facts without
another observation request. Both supported locations receive 36-hour saved reports
despite an invalid middle coordinate. Four new issuance IDs survive exact readback;
the two original forecasts stay unchanged. The same tests cover preserved native IFS
gaps, separate numerical baselines, and future-hour eligibility. Quality checks use
the existing Ruff, mypy, import, lock, documentation and hygiene commands. Full
acceptance/coverage and operational VPS reliability are not established by these checks.

Focused commands actually run (the integration commands used a dedicated test database
and test buckets, never the retained demonstration database):

```text
python -B -m pytest tests/unit/application/test_forward_run.py tests/unit/application/test_forward_verification.py tests/unit/application/test_hourly_report.py tests/unit/application/test_batch_forecast.py tests/unit/application/test_selected_forecast.py -q
python -B -m pytest tests/unit/application/test_automatic_verification.py tests/unit/application/test_forecast_issuance.py tests/unit/application/test_issued_temperature_verification.py tests/unit/test_forecast_api.py -q
python -B -m pytest tests/integration/application/test_forward_run.py tests/integration/application/test_batch_issuance.py -q
python -B -m pytest tests/integration/application/test_issued_temperature_verification.py::test_automatic_window_acquires_once_reuses_real_snapshot_and_skips_empty_window tests/integration/application/test_issued_temperature_verification.py::test_automatic_batch_isolates_locations_and_reuses_results_without_changing_issuances -q
```

Native probability shadows and the subsequent ECMWF compatibility assessment are
recorded below. [Native precipitation type](#native-precipitation-type-on-the-local-grid)
now preserves categorical guidance and uncertainty without surface-temperature inference.

### Repeat forward runs and read accumulation status

The same forward run is the learning loop's data source for configured coordinates.
It is safe to call repeatedly from an external caller (a person today, a scheduler
later); nothing else was added: no second workflow, queue, VPS or CI deployment.
Locations stay coordinate-only. `display_timezone` is an optional per-location
presentation zone that never selects data or changes values:

```json
{"locations":[
  {"lat":44.98859,"lon":-93.25557,"name":"Minneapolis","display_timezone":"America/Chicago"},
  {"lat":44.9537,"lon":-93.09,"name":"St. Paul"}
]}
```

```text
python -B -m mesoforge.application.forward_run --config locations.json --output-dir EXTERNAL_NEW_RUN_DIRECTORY --display-timezone UTC [--reissue]
python -B -m mesoforge.application.accumulation_status --lat 44.98859 --lon -93.25557
curl "http://127.0.0.1:8765/accumulation-status?lat=44.98859&lon=-93.25557"
```

Each run: verifies every eligible unverified hour of earlier versions through the
existing automatic verification (facts are idempotent; unavailable/ineligible reasons
stay in `previous-verification.json`), discovers the current model set once, then
issues immutable versions. Exit codes: `0` completed, `1` at least one location
failed, `2` the run could not start, `3` another forward run is in progress.

- **Overlap protection.** The whole run holds one process-wide PostgreSQL session
  advisory lock (`pg_try_advisory_lock`, key `sha256:22cd5cd5…` derived from
  `mesoforge.forward-run.v1`). A concurrent run stops immediately with
  `forward_run_overlap` before creating its directory, verifying or issuing; it never
  waits. The lock is released when the run ends or its connection drops.
- **Decision-window guard.** After discovery, a coordinate that already holds a
  version for the discovered `target_reference_time` is reported
  `skipped_already_issued` (with the existing IDs) from issuance metadata alone;
  `--reissue` adds a version deliberately. Only the remaining coordinates reach
  shared preparation (`issuance-locations.json`), so a fully covered retry downloads
  no guidance. Verification still runs and one invalid coordinate still fails alone.
- **Queryable facts.** Saved verification facts now carry searchable attributes
  (`issued_forecast_id`, `valid_time`, `horizon_hours`, `latitude`, `longitude`,
  `verification_status`). They are output metadata only: not part of the idempotency
  digest or the fact payload, so replay still finds the same fact.
- **Only unverified hours enter the observation path.** Before acquiring anything,
  the run indexes the coordinate's saved facts from those attributes and reports
  hours that already hold one as `already_existing` with the fact ID
  (`saved_facts.hours_already_verified`). Without that index a later, wider
  observation acquisition is a new input revision and the idempotent verifier would
  legitimately save a second fact for an already-verified hour (this happened once in
  the demonstration below, before the index existed). If the index cannot be read the
  run says so and falls back to idempotent re-verification.

The accumulation status (`mesoforge.accumulation-status.v1`) reads only issuance
rows, fact attributes and retained observation-source attributes: no forecast
payload, no observation, no provider access, no writes, and no weights, bias, regime
or skill. It reports version count with earliest/latest issuance and target times;
hours `verified`, `pending` (valid time + 15 min still in the future),
`no_retained_observations` (eligible, but no retained METAR acquisition covers
valid time ± 15 min for this coordinate and the current observation configuration)
and `retained_observations_without_fact` (retained inputs cover the hour but no
fact exists: no eligible station match, QC failure or ineligibility; the reasons are
in the run reports, not storage); verified counts by lead bucket 1–6 / 7–18 / 19–36;
fact counts including hours with more than one fact; retained observation coverage;
and the newest station evidence. Repeated reads differ only in `evaluated_at`.

Real demonstration on **September 17, 2026** against the retained local PostgreSQL
and the restored S3 store holding the two Minneapolis versions issued on September 16
(`5bd637dd-77d4-4f0d-ac92-bd39d73918c4`, `9e989662-551e-4918-92d8-77005eb7e474`,
both target 22Z). The configuration listed Minneapolis (America/Chicago), St. Paul
(`44.9537, -93.09`, no zone) and an invalid `95.0, -93.0` entry.

**Cycle 1** (started 02:34:02Z, 719 s, exit 1 because of the invalid entry):
Minneapolis verified **8 real hours** on the two retained versions, 23Z–02Z, after
discovering 8 METAR stations (K21D, KANE, KFCM, KLVN, KMIC, KMSP, KSGS, KSTP) and
downloading **25,436 bytes** of observations; forecast-minus-observation errors were
**+0.19, −0.32, +1.48, +1.01 K** (identical for both versions, which share numerical
hours). St. Paul had nothing to verify; the invalid entry failed with
`invalid_location` before any work. Discovery at 02:36:12Z selected
**HRRR 00Z / GFS 18Z / RAP 21Z / IFS 18Z** for target **02Z** and preparation
downloaded **545,168,677 bytes**. Issued: Minneapolis
`5133bee6-dba6-4000-8313-8e24189553fa` (02:44:40Z, saved report zone
America/Chicago) and St. Paul `1c260243-1a20-40b9-8e36-0d9d30a55380` (02:45:46Z,
UTC), both covering 03Z through September 18 14Z. Exact readback matched the run
report for both. The status for Minneapolis then showed 3 versions over 2 targets,
108 hours (8 verified, 100 pending, all 8 in lead bucket 1–6), 8 facts, one retained
acquisition covering 22:45–02:15Z; St. Paul showed 1 version, 36 pending hours and
no station evidence yet. CLI and HTTP results were equal, repeated reads changed
only `evaluated_at`, an invalid coordinate returned 422, and the reads changed no
row or object.

**Same-window replay** (started 02:46:17Z, 262 s): the identical command in a new
directory re-verified the same 8 hours as `already_existing` with the same 8 fact
IDs, downloaded **0 bytes** (retained observations and station discovery reused),
discovered the same set for target 02Z at 02:48:15Z, and reported both coordinates
`skipped_already_issued` naming cycle 1's IDs; no `issuance-locations.json` or
preparation directory was created and every table count, issuance row, fact and
S3 object was identical before and after. The status was unchanged.

**Overlap** (03:20:41Z): a second command started while cycle 2 held the lock
exited with code **3** after 3 seconds, printed only
`{"error": {"code": "forward_run_overlap", ...}}` on stderr, and created no output
directory; cycle 2 continued unaffected.

**Cycle 2** (started 03:20:16Z, 728 s, exit 1): hour 03Z had become eligible for
every saved version. Minneapolis verified it for the two September 16 versions
(**+1.73 K** each) and for cycle 1's version (**−0.08 K**); St. Paul discovered its
own 9 stations (adds KRNH) and verified cycle 1's 03Z hour (**+0.57 K**, 10,384
observation bytes). Discovery at 03:23:02Z selected the same four cycles for target
**03Z**, preparation downloaded **512,963,380 bytes**, and it issued Minneapolis
`f0f0dd57-cb7e-4c0e-93e3-761efeda4d5c` (03:31:09Z, America/Chicago) and St. Paul
`dd313459-f8ac-40e8-bef1-3c534d64cfad` (03:32:09Z, UTC), covering 04Z through
September 18 15Z; readback matched. This cycle ran **before** the saved-fact index
existed: Minneapolis' wider 23Z–03Z window acquired a new observation revision
(35,982 bytes) and the idempotent verifier therefore saved a second, equally valid
fact for each of the 8 already-verified hours (`verified: 11`, not
`already_existing: 8` + 3). The status shows that honestly: Minneapolis 4 versions
over 3 targets, 144 hours, **11 verified** (all lead bucket 1–6), 133 pending,
**19 facts** with **8 hours holding two facts**; St. Paul 2 versions, 72 hours,
1 verified, 1 fact. The index added afterwards makes such hours `already_existing`
without observation work; the duplicate facts stay immutable and count as one
verified hour each.

**Cycle 3** (started 04:15:41Z, 634 s, exit 1) ran with the saved-fact index.
Minneapolis reported its 11 verified hours as `already_existing` from fact
attributes (`saved_facts.hours_already_verified = 11`, each row naming its fact
ID) and sent only the four newly eligible 04Z hours through one one-hour window
(**5,646 bytes**): **+4.59 K** for both September 16 versions and **+2.24 K** for
the 02Z and 03Z versions. St. Paul reused its 1 fact and verified 04Z for its two
versions (**+1.11 K**, 7,323 bytes). Discovery at 04:16:56Z selected
**HRRR 00Z / GFS 00Z / RAP 03Z / IFS 18Z** for target **04Z**; preparation
downloaded **514,013,712 bytes**; it issued Minneapolis
`67123abe-4350-43e1-94c7-379844596466` (04:25:01Z) and St. Paul
`4ce698ba-c2fe-4f28-9830-3e80c7ea410d` (04:26:01Z), covering 05Z through
September 18 16Z, and readback matched. Exactly 6 facts were added (26 in total;
the 8 double-fact hours are unchanged). Final status: Minneapolis **5 versions over
4 targets, 180 hours, 15 verified (all lead bucket 1–6), 165 pending, 23 facts,
3 retained acquisitions**; St. Paul **3 versions, 108 hours, 3 verified, 3 facts**.
Every status read (CLI and HTTP, repeated) changed no row or object. Retained
evidence for all runs is under
`%LOCALAPPDATA%/MesoForge/baselines/20260917-forward-accumulation` (per-cycle
result directories, `*-summary.json`, `*-db.json`, `*-evidence/`); on this machine
the physical path is the `Packages/Claude_…/LocalCache/Local/MesoForge` mirror of
`%LOCALAPPDATA%`. Temporary PostgreSQL, S3 and API services were stopped afterwards.

Validation on September 17, 2026: **3,242 offline tests** (unit, contract, property;
three pre-existing failures unrelated to this work were deselected: one
current-forecast-batch mock signature and two typed-boundary inventories that fail
identically at the previous commit) and the **forward-run, verification, batch
issuance, storage and advisory-lock integration tests** passed against pgserver and
an S3-compatible moto server. Ruff, mypy, all nine import contracts, lock
consistency, documentation/hygiene checks and `git diff --check` passed.

```text
python -B -m pytest tests/unit/application/test_forward_run.py tests/unit/application/test_forward_verification.py tests/unit/application/test_accumulation_status.py tests/unit/test_accumulation_status_api.py -q
python -B -m pytest tests/integration/application/test_forward_run.py tests/integration/storage/test_idempotency_lock.py tests/integration/application/test_issued_temperature_verification.py tests/integration/application/test_batch_issuance.py tests/integration/storage -q
```

Limitations: unavailable/ineligible attempt reasons are not persisted (they stay in
each run's `previous-verification.json`), so the status can only distinguish retained
observation coverage from a saved fact; hours that stay unverified keep re-entering
acquisition on later runs until their window is retained; facts persisted before the
attributes existed are not indexed; the lock is process-wide, so two forward runs on
different coordinate lists also serialize; and every forward `verify()` still reads
the full issuance payloads (the read-cost limit measured earlier).

### Refresh guidance and forecast from the latest complete snapshot

Provider preparation and numerical-baseline publication are separate commands.
`latest_complete` means prepared contributor evidence; normal location generation
consumes the separate `latest_baseline` described below. The compatibility forward
run above remains an explicit development/replay path.

**Background refresh** (network, roughly 10–15 minutes here):

```text
python -m mesoforge.application.refresh_guidance --config locations.json --root %LOCALAPPDATA%\MesoForge\guidance
```

It discovers required HRRR/GFS and optional RAP/IFS cycles with a **42-hour target
window** (`mesoforge-prepared-coverage-policy.v1`, `--coverage-hours 36..42`; cycle
acceptance still uses the first 36 hours, hours 37–42 are acquired only where the
accepted cycle publishes them, and the window ends at the last hour every selected model
reaches), runs `prepare_selected` with NBM PoP, attaches native p-type, cloud and
thunder, attaches visibility evidence (optional; a failure is recorded, not fatal),
loads the finished preparation offline, computes one validation column per configured
coordinate, writes `snapshots/<id>/snapshot.json` (`mesoforge.prepared-snapshot.v1`)
and only then replaces `latest_complete.json`
(`mesoforge.latest-complete-pointer.v1`) atomically. A failed step does not replace the
current pointer and retains `failure.json`. A stable OS file lock serializes
compare-and-publish across processes sharing the local guidance root; publication
rejects an older reference time even when refreshes overlap. The manifest separates
native deterministic contributors (HRRR/GFS
active; RAP/IFS shadow evidence), the blended meta-model NBM with its
active-current-policy products (hourly PoP, sky, thunder) and evidence-only inputs,
names the current field-policy identities, and references every preparation artifact
by path and digest. NBM PoP selection now rejects a still-publishing newest cycle
(identity/range validation failure), records it under `rejected_candidates` and uses
the next complete cycle; validation is unchanged. The refresh calls `prepare_selected`
with `require_complete_shadows=False`: the active HRRR/GFS objects and each zero-weight
shadow use their own pinned view of the same discovery, a shadow provider failure
(for example an ECMWF open-data 503) stays recorded per object under
`shadow_shortfalls` instead of aborting the preparation, and the snapshot marks that
shadow `partial`/`unavailable`. Discovery uses the same explicit optional-shadow
policy: failure to find or prove a usable RAP/IFS cycle records
`shadow_discovery_shortfalls`, omits its acquisition, and allows valid HRRR/GFS
guidance to publish. HRRR/GFS discovery remains strict; neither weights nor sources
are substituted. A later refresh may restore the shadow normally. The standalone
selection command and compatibility forward run retain their strict defaults.

#### Background MesoForge baseline snapshots

The normal command sequence is now:

```text
refresh_guidance -> prepared contributor state / latest_complete
build_baseline -> immutable numerical baseline / latest_baseline
forecast_from_baseline -> pinned domain and point -> optional immutable issuance
```

After guidance refresh, run the provider-free background build outside Git, then
consume its baseline for the same coordinate-only configuration:

```powershell
python -m mesoforge.application.build_baseline --guidance-root "$env:LOCALAPPDATA\MesoForge\guidance" --baseline-root "$env:LOCALAPPDATA\MesoForge\numerical-baselines" --config locations.json
python -m mesoforge.application.forecast_from_baseline --root "$env:LOCALAPPDATA\MesoForge\numerical-baselines" --config locations.json
# Requires the existing PostgreSQL/MinIO configuration; preserves immutable versions.
python -m mesoforge.application.forecast_from_baseline --root "$env:LOCALAPPDATA\MesoForge\numerical-baselines" --config locations.json --issue
```

The build pins one prepared snapshot, validates its retained inputs and information
cutoff, loads source arrays once, and runs the existing complete canvas for every
configured domain. It writes `baselines/<id>/baseline.json`
(`mesoforge.baseline-snapshot.v1`), compressed domain artifacts, shared metadata and
cutoff evidence before publishing `latest_baseline.json`. Field policies, native
contributors, shadows, missingness and current derivations/checks are unchanged.
The generalized coherence framework runs the existing required checks/derivations
before publication. No provider call or forecast-history write occurs during build.
The manifest records its version, dependency registry and compact per-domain outcomes.
Ordinary missing sources remain explicit unavailable fields; a failed required
operation or an incomplete execution report blocks the new baseline and preserves
the previous pointer. Future relationships such as QPF/PoP/thunder do not modify
fields or fail the build. Location reads neither blend nor rerun baseline coherence.

The spatial representation is a collection of exact configured 7×7 domains using
today's 6 km spacing and nested masks, not a new common/continental grid. Each domain
is still derived only from latitude/longitude. One baseline shares its contributor
state and metadata across locations. New centers require another background build;
being inside a prepared bounding box alone does not make an unbuilt baseline domain
available. Location failures remain explicit and do not stop later locations.

By default the build materializes every usable reference hour in the prepared
36–42-hour envelope, at most seven 36-hour views. This preserves reference-relative
lead bands: shifting a window can move an hour across the existing 18/19-hour policy
boundary, so reading a view must not just slice and relabel a different blend.
`build_baseline --reference-time 2026-09-18T00:00:00Z` limits a bounded replay build;
the flag may be repeated. `forecast_from_baseline --reference-time …` selects a saved
replay view. Normal reads use the current UTC hour; absent temporal coverage returns
`no_current_baseline`, and an unbuilt coordinate returns `coverage_required`. Neither
case triggers blending or acquisition in the location job.

The compact baseline references exact checksummed source JSON objects and pools
repeated metadata once across its views. It reconstructs the existing rich grid
without changing numerical values. The existing issued payload still stores that
rich shape; full issuance-storage normalization is not part of this change.
An issuance retains baseline ID/digest, prepared-state ID, policies, source cycles,
background cutoff, publication, reference and actual issuance times. The baseline is
pinned once for a location collection; a later publication cannot change that run.

The two pointers are independent: a new contributor state may publish even if its
baseline build fails, while the previous baseline remains current. Baseline publication
reuses the persistent OS file lock plus atomic replacement. Ordering compares prepared
reference time, prepared publication time, background analysis cutoff and build start;
a slower older publisher cannot move the pointer backward. Failures preserve previous
immutable baselines and the existing pointer. Retrying a build produces a new artifact.

Current operation is manual/on-demand. A daemon, scheduled hosted worker, incremental
recomputation, broader scientific coherence rules, corrections and AI editing remain future work.
The implementation and current limits are detailed in
[RFC §5.7](docs/rfcs/mesoforge-v2-architecture.md#57-implemented-background-baseline-snapshots).

Coherence validation on 2026-09-24 reused the same retained September 18 contributor
state, without provider access. Two locations × seven reference views reproduced all
**24,696 cell-hours** and exact points at zero tolerance, including every represented
field/evidence record, masks, missingness, policies, conditions, transitions and period
summaries. Only three execution-source identities and dependent digests changed; the
RH registry handler label now names coherence, and baseline manifests add versioned
relationship/report metadata. All 14 saved-view reads ran with both blending and
coherence disabled and still reproduced their forecasts exactly. Historical baselines
without coherence reports remain readable.

The 14-view background build measured 2.53 s for contributor load, 362.22 s for grid
construction, 151.16 s for serialization and 518.84 s overall. Previous numerical
construction was 375.29 s and serialization 153.43 s; load times are cache-sensitive.
The complete coherence boundary, including graph/report collection but excluding
blend kernels, took **0.093 s** in a separate 25.87 s single-grid replay. The build's
`coherence_seconds` counter measures relationship handlers only (0.746 s over all 14
views), excluding graph/report bookkeeping; retained combined vector/gust kernels
are counted in `field_blend_kernel_seconds`. Uncontended location extraction was
16.29 s, with conditions/integrity checks about 23.4 s and transitions/periods under
0.005 s. No serialization or extraction optimization was attempted.

The complete compressed baseline occupies **227.91 MB**, only **19.25 KB** more than
its predecessor including the compact coherence manifests/reports. The existing rich
issued payload format remains unchanged. Peak background working memory was 2.78 GB.
Focused coherence/scientific/artifact checks passed, and **46 PostgreSQL/MinIO tests**
passed, including two-location immutable issuance/readback, historical compatibility,
read-only conditions and concurrent issuance locking. Temporary services were stopped.
The broader offline unit/contract/property selection reported **3,406 passed and the
same three known pre-existing failures**: the explicit-cycle batch mock signature,
typed-identifier inventory and code-revision-validator inventory. These unrelated
failures remain unresolved. Ruff, formatting, mypy, import contracts, lock consistency,
documentation validation, repository hygiene and `git diff --check` passed. Full
expensive acceptance and live-provider tests were not run for this milestone; saving
a local checkpoint does not establish whole-application verification.

**Development/replay from prepared evidence** (the older path; no provider access,
but it still performs local blending and is not the normal location command):

```text
python -m mesoforge.application.forecast_from_snapshot --root %LOCALAPPDATA%\MesoForge\guidance --lat 44.98859 --lon -93.25557 --name Minneapolis --display-timezone America/Chicago
python -m mesoforge.application.forecast_from_snapshot --root … --config locations.json --issue [--reissue] [--output-dir DIR]
```

It resolves the pointer, verifies the manifest and retained-artifact digests, derives
`reference_time = floor(request_time)` (22:37Z → 22Z, 23:05Z → 23Z), checks that HRRR
and GFS hold every valid time R+1…R+36, reads the prepared files as a *reference view*
for that hour (source cycles and leads are exactly what was prepared; the forecast
records `prepared_window` beside its `target_reference_time`), builds the local grid
with the existing policies and returns the 36-hour point forecast plus the hourly
report. NBM-based products report their own covered and missing valid times and never
shorten the forecast. Insufficient coverage returns `no_current_snapshot` (exit 3).
`--issue` reuses the same path under the existing PostgreSQL forward-run advisory
lock, rechecks the decision-window guard before the grid build, and saves the
`prepared_snapshot` provenance (snapshot ID, publication time, request/analysis cutoff
and reference times, per-source information evidence, cycles, field policies,
coverage) inside the immutable issuance. Coordinates outside the refreshed collection's
footprint are refused (`coverage_required`); the snapshot is not extended on demand.

**Real demonstration, September 18, 2026** (evidence under
`%LOCALAPPDATA%\MesoForge\guidance` and `%LOCALAPPDATA%\MesoForge\baselines\20260918-snapshot-demo`,
physically in the `Packages/Claude_…/LocalCache/Local/MesoForge` mirror; the ECMWF
open-data bucket returned 503s/connection failures during the first three attempts,
which failed without publishing anything and retained their `failure.json`):

- **Refresh** (Minneapolis + St. Paul, `--coverage-hours 42`): snapshot
  `20260918T004459Z-fcb1b488`, decision 00:44:59Z, reference 00Z, **819.6 s** total
  (discovery 166.1 s, `prepare_selected` with PoP 397.6 s, p-type 111.5 s, cloud
  58.7 s, thunder 28.1 s, visibility evidence 49.2 s, offline validation 8.4 s,
  publish 0.0 s), **1,171,658,713 bytes** downloaded, 1.78 GB retained. Selected
  cycles: HRRR 18Z (leads 7–48), GFS 18Z (7–48), RAP 21Z (4–45), IFS 18Z (native
  3-hourly 9–48): all four reached hour 42, so the window is
  2026-09-18T01Z–2026-09-19T18Z. NBM PoP: the 00Z cycle was entirely unavailable
  (404), so the newest complete cycle 23Z served all 42 hours; the new
  invalid-evidence rejection path was therefore not exercised in this run. NBM 23Z also
  covered total cloud for all 42 hours, but its native one-hour thunder product ended at
  lead 36 (11Z Sep 19) and visibility at lead 37, both recorded as `partial` with the
  missing valid times. RAP 42/42 and IFS 14/14 native hours were retained (no shadow
  shortfall this time). The manifest is 64 KB and references, not copies, its 1.78 GB
  of preparation artifacts.
- **Ad-hoc forecast, all sockets blocked** (`netguard.py --block-all`): request
  00:59:09Z → reference 00Z, 36 hours 01Z–12Z, `blocked_attempts: []`, **180.8 s**
  (pointer/digest verification 0.02 s, guidance load 2.0 s, **local-grid build
  178.6 s**, hourly report 0.14 s).
- **Issuance from the same snapshot** (loopback only, Minneapolis + St. Paul + an
  invalid entry): request 01:02:49Z → reference 01Z (offset +1 h from the prepared
  window), Minneapolis `8148c74e-4e0f-4660-a923-aa36b94b5100` and St. Paul
  `2fbc62dc-a218-4a9e-957e-cdbb426473a5`, hours 02Z–13Z, guidance loaded once (2.0 s),
  grid 177.9/178.4 s, issuance 18.9/19.1 s, 396.8 s total, the invalid entry isolated as
  `invalid_location`, no provider attempts. Conditions, transitions and period
  summaries then ran unchanged on the Minneapolis issuance (five transitions, no gaps;
  about 26 s each, dominated by reading the issuance object).
- **Later reuse and boundary**: the same snapshot at an explicit reference of 06Z
  (offset +6 h) served 07Z–18Z Sep 19 (176.3 s; thunder partial for 12Z–18Z as
  recorded), and 07Z returned `no_current_snapshot` in 1.2 s with the exact reason
  (`first missing 2026-09-19T19:00:00Z` for HRRR and GFS), exit 3.

Previously reported validation on September 18, 2026: the offline unit/contract/property suites (the
same three pre-existing failures deselected), the forward-run, batch-issuance and
storage integration tests (71) against pgserver and moto, ruff, mypy, the nine import
contracts, documentation checks and `git diff --check` passed.

Limitations: the local-grid build is still the request-time cost and is not
interactive (see [the optimization below](#local-grid-build-cost)); the refresh
must still finish inside its decision hour (existing selection expiry); coordinates
outside the refreshed collection's footprint are refused rather than prepared
offline; the pointer is a local file, not a shared service; superseded and failed
snapshot directories are retained without any pruning policy; ad-hoc (non-issued)
forecasts have no conditions/transitions/period preview because those layers read
saved issuances only.

**Snapshot/issuance correctness (September 24, 2026):** the bounded fixes address the
four findings from the independent audit at `5eb1ae6`; that audit found an unsupported
universal cutoff claim, not demonstrated historical leakage.

- **Publication:** `.latest_complete.lock` is a permanent lock file per local guidance
  root, using Windows byte-range locking or POSIX `flock`. The reference-time comparison
  and atomic `os.replace` occur under that process-shared lock. Reference time remains
  the ordering rule; equal-reference replacements remain allowed. Failed publication
  leaves the current pointer and immutable snapshots intact. Readers need no lock.
- **Information evidence:** additive `source_information` in the existing v1 manifest
  pins each retained source manifest/hash, availability/acquisition timestamps and its
  own discovery cutoff where recorded. Attachments do not inherit the original
  HRRR/GFS/RAP/IFS decision cutoff. If provider publication time is unavailable, an
  actual retrieval time can establish a conservative known-by bound; it is labeled
  accordingly, never presented as a fabricated publication time.
- **Issuance cutoff:** `forecast_analysis_cutoff` is the fixed request time, distinct
  from the floored forecast reference, source cycles, snapshot publication and actual
  issuance. New issuance rejects unproven input timing or inputs, discovery evidence,
  completion or publication after that cutoff. Historical v1 snapshots are still
  readable: the consumer reconstructs information evidence from retained manifests
  and reports any limitations explicitly. Existing immutable issuances are untouched.
- **Location and concurrency isolation:** metadata lookup failure returns
  `issuance_lookup_failed` for that coordinate and later locations continue. Snapshot
  issuance takes the existing `mesoforge.forward-run.v1` PostgreSQL session advisory
  lock through lookup/build/storage. Concurrent snapshot issuers wait, then recheck
  committed versions; a second primary attempt skips the existing coordinate/reference
  version. `--reissue` remains explicit and records `explicit_reissue`. The compatibility
  forward run uses the same lock; different coordinate lists also serialize.

Optional RAP/IFS discovery shortfalls are explicit as described above; NBM per-hour
missingness and all existing active field policies remain unchanged. That correctness
slice preceded the separate [on-demand numerical baseline](#background-mesoforge-baseline-snapshots).
Continuous hosted maintenance remains future work.

Validation for this correctness change: **182 focused offline tests passed**, plus
**72 PostgreSQL/MinIO integration tests** covering snapshot concurrency, forward runs,
batch issuance and storage. Separate spawned processes exercised both pointer ordering
and same-window issuance. The full `tests/unit tests/contracts tests/property` run
reported **3,345 passed and three pre-existing failures**: the current-forecast-batch
mock signature and the two typed-boundary inventories described above. All three were
reproduced at starting commit `6104a36` in a separate temporary checkout; none was
skipped or weakened. Final cutoff edge-case tests also passed after review.
Ruff, formatting, mypy, all nine import contracts, lock consistency, documentation,
repository hygiene and `git diff --check` passed. Checks used the isolated locked
Windows environment and dedicated test storage; temporary services were stopped.
No live provider acquisition or full acceptance run was performed. POSIX locking is
implemented but was not executed on this Windows host; no distributed-filesystem
locking guarantee is claimed.

#### Local-grid build cost

The snapshot-consuming path reuses one transformer per native CRS instead of
rebuilding it for every projection and coverage check, and hands the ephemeral grid
and its freshly built columns to the result instead of copying them a second time.
Neither changes a forecast value. Projection caching applies to existing projection
callers; only the ownership/copy optimization is opt-in, so retained grids and
callbacks that return shared structures keep the previous copies.

Measured on snapshot `20260918T004459Z-fcb1b488` (Minneapolis, 7x7 nodes, 36 hours,
all attachments): the local-grid build fell from **178.6 s to 25.4 s (7.0x)** and the
whole `forecast_from_snapshot` call from 180.8 s to 27.6 s, with 97,021 transformer
constructions replaced by 6. Two coordinates from one loaded snapshot take 53.7 s and
still load guidance once. Peak memory fell from 2,592 MB to 2,209 MB. An in-process
A/B on that snapshot, running both the optimized and the original expressions under
one code identity, produced **identical full canonical bytes** including
`local_grid.sha256`.

What remains, per column (0.28 s) and per build: about 14 s in the 49 columns
(dominated by `copy.deepcopy` over JSON-shaped evidence and per-hour xarray
`Dataset.__getitem__`) and about 10 s in the required canonical digest of the grid.

### Read-only site verification analysis

A descriptive, **temperature-only** view of the verified history that forward
accumulation produces. It applies no correction, derives no weight or regime, and
writes nothing (schema `mesoforge.site-verification-analysis.v1`):

```text
python -B -m mesoforge.application.site_verification_analysis --lat 44.98859 --lon -93.25557 [--display-timezone America/Chicago]
curl "http://127.0.0.1:8765/verification-analysis?lat=44.98859&lon=-93.25557"
```

**Stored facts are evidence, not statistical samples.** Canonicalization
(`mesoforge-verification-canonicalization.v1`) keeps every fact in provenance and
resolves them in two steps:

1. An *opportunity* is one issued version and hour under one verification policy.
   Facts whose evidence is identical — forecast value and issuance digest, station,
   observation time and value, observation revision and logical-observation digests,
   matching policy and error — are the same verification repeated over a re-acquired
   copy of the same observation revision and form one opportunity (earliest fact
   canonical). Facts that disagree, for example a revised observation, make the
   opportunity **ambiguous**: excluded and reported with its reason, never chosen by rule.
2. A *sample* is one target reference time and valid time. Several versions of one
   target are one sample only when their forecast value, observation revision and
   error are identical (a re-issue); versions that differ are ambiguous. Different
   targets verified at one valid time remain separate samples at their own horizons.

Facts are usable only with verified status under `issued-temperature-verification.v1`,
finite kelvin values, a horizon equal to valid − target within 1–36 h, a saved error
equal to forecast − observation and agreement with the issuance metadata row. Facts
saved before identity attributes existed are found with a bounded query and
identified from their immutable payload; nothing is backfilled or modified.

The report gives the inventory (indexed, legacy, usable, excluded by reason,
ambiguous); overall and per lead bucket 1–6 / 7–18 / 19–36 N, bias, MAE, RMSE and
min/max error, with empty buckets left null and nothing extrapolated between them;
local day (06–18) and night (18–06) groups using the period-summary convention in the
requested zone, else the samples' saved report zone, else UTC; per-station proxy
accounting (a station is a proxy for, not identical to, the coordinate); and regime
*readiness* — which saved forecast dimensions (sky, wind, dew point/RH, precipitation,
p-type, thunder, model spread) are reconstructable per sample, as availability and
ranges only. Metrics are labelled `descriptive_only` or `no_samples`.
`correction_readiness` evaluates each lead bucket against the
[evidence policy](#decision-window-and-evidence-policies) (when this section's first
results were produced no policy existed and it reported `evidence_policy: null`). It
lists the observed evidence
and what it does **not** conclude (persistent site bias, lead-dependent correction,
regime bias, preferred model, recommended adjustment) and invents no threshold.

Real result on **September 17, 2026** over the forward-accumulation history above.
Inspecting the payloads showed that cycle 2's eight second facts used the **identical
observation revision** as the first (same KMIC report, time, value, revision and
record digests, forecast, policy, code and error); only acquisition provenance
differs (raw artifact, record index, ingest time, normalized-observation artifact,
cutoff, candidate list). They are re-acquired identical evidence, not revisions.

| Minneapolis `44.98859, -93.25557` | Value |
| --- | --- |
| Stored facts → verified opportunities → samples | **23 → 15 → 9** |
| Opportunities with two identical-evidence facts | 8 (none ambiguous, none excluded) |
| Samples backed by two identical re-issued versions (`5bd637dd…`, `9e989662…`) | 6 |
| Bias / MAE / RMSE | **+1.45 K / 1.54 K / 2.04 K** (+2.6 / 2.8 / 3.7 °F) |
| Min / max error | −0.32 K / +4.59 K (22Z target, hour 6, 04Z) |
| Lead 1–6 | N 9, same metrics; **7–18 and 19–36: N 0, null** |
| Day / night (America/Chicago, saved report zone) | day N 0; night N 9, local hours 18–23 |
| Station proxy | KMIC only, 11.1 km, 263 m; 6 distinct observations shared by 9 samples |
| Targets / valid span | 3 targets, 23Z–04Z (5 h, one evening) |
| Correction readiness | `insufficient_evidence` |
| Reads | 5 issuance metadata rows, 23 fact payloads (180.8 MB), **0 issued forecast objects** |

St. Paul (`44.9537, -93.09`): 3 facts → 3 opportunities → 3 samples, bias +0.93 K,
MAE 0.93 K, RMSE 0.96 K, all lead 1–6, station KSTP (3.6 km, 213 m, 2 distinct
observations), saved report zone UTC (grouped by UTC wall clock, as the note says),
`insufficient_evidence`. Regime readiness for Minneapolis: dew point, RH, wind speed,
direction, gust, precipitation amount and model spread (3–4 models, 0.8–3.2 K) are
saved for all 9 samples; p-type for 6, PoP for 2, cloud fraction and thunder for 0,
because context comes from each sample's canonical (earliest) version. Nine samples
from one evening, one station and short leads describe that evening only; they do not
support a site bias, a lead dependence or any adjustment.

Validation: two CLI runs and two HTTP reads per coordinate were identical outside the
`evaluation` block; a guarded in-process replay that forbids provider requests,
non-loopback connections, forecast calculation, issuance, issuance reads, observation
acquisition, station discovery and verification made zero forbidden calls; and every
PostgreSQL row count (8 issuances, 26 facts), all 50 object identities and the fact
count were unchanged. Invalid coordinates or zones return 422. Evidence is under
`%LOCALAPPDATA%/MesoForge/baselines/20260917-site-verification-analysis`.
**3,265 offline tests** passed (the same three pre-existing failures deselected), as
did the forward-run, verification and storage integration tests against pgserver and
an S3-compatible moto server, including a real-storage analysis and a legacy fact
recovered from its payload. Ruff, mypy, all nine import contracts, lock consistency,
documentation/hygiene checks and `git diff --check` passed.

```text
python -B -m pytest tests/unit/verification/test_site_analysis.py tests/unit/application/test_site_verification_analysis.py tests/unit/test_site_verification_analysis_api.py -q
python -B -m pytest tests/integration/application/test_forward_run.py tests/integration/application/test_issued_temperature_verification.py tests/integration/storage -q
```

Limitations: temperature only; regime context is read from the canonical version
only; and the current history is far from meeting the evidence policy, so nothing
here graduates beyond descriptive.

#### Compact analytical attributes on new facts

A fact payload is 4–9 MB because it embeds the forecast context, candidate list and
hourly report, so the analysis above read 180.8 MB to analyze 23 facts. Every
**newly saved** fact now also carries a small projection of its own payload in
`attributes.analysis` (schema `mesoforge.verification-analytical-attributes.v1`),
beside the unchanged flat index keys:

- identity: issued forecast ID and digest, coordinate, target, horizon, valid time,
  fact schema, verification policy, status;
- forecast and observed temperature, error (forecast − observation) and unit;
- observation identity: station, coordinates, distance, elevation, observation time and
  offset, revision, logical-observation and raw-record digests, acquisition artifacts;
- matching-policy digest, verification cutoff, code commit and saved report zone;
- a compact point context: ten saved surface values with units (cloud fraction, wind
  speed/direction/gust, dew point, RH, QPF, PoP, p-type, thunder) and the per-model
  temperatures. No candidates, provenance blocks, GRIB metadata, URLs, policy prose
  or grid data.

The full payload remains the **authoritative, immutable evidence record**; attributes
are not part of the idempotency digest, so payload bytes and replay are unchanged.
The analysis prefers the block and **falls back to a bounded payload read** for facts
without a usable one (facts saved earlier, unindexed legacy facts, unsupported or
malformed blocks). Both paths run the same projection, so canonicalization and
metrics are identical; `inventory.fact_sources` and `reads` say how many facts and
bytes each path supplied, and `--payload-only` audits that the paths agree. Existing
facts are never rewritten, migrated or backfilled, and the canonicalization policy
is unchanged. The context snapshot is descriptive input for later regime analysis,
not a learned model, weight or correction.

Real measurement on **September 17, 2026**. With the new code the 23 existing
Minneapolis facts still gave exactly 15 opportunities, 9 samples and bias / MAE /
RMSE +1.4541 / 1.5437 / 2.0414 K, all through payload fallback. Forward cycle 4
(started 18:15:29Z) then verified hours 05Z–18Z for every saved version through the
normal path: **70 new Minneapolis facts and 42 St. Paul facts**, each with the block
(the 15 and 3 already-verified hours were answered from saved facts). Its issuance
stage failed honestly on a provider outage (ECMWF open-data HTTP 503 during IFS
discovery; verification results were kept); a retry at 18:31:50Z answered all 130
saved hours from fact attributes in about 10 seconds with no download and issued
Minneapolis `b96f23cd-179c-4ff0-bf1f-e7e7c8740a58` and St. Paul
`2982255e-47cf-40c1-980c-082fe6eccc7b` for target 18Z (HRRR 12Z / GFS 12Z / RAP 15Z /
IFS 06Z).

| Measurement | Value |
| --- | --- |
| Compact block per new fact | 2,408–2,614 bytes (median 2,440); whole attributes median 2,641 |
| Full payload per new fact | 4.20–9.34 MB (median 4.48 MB) — about 1,800× the block |
| New facts whose block equals the projection of their own retained payload | 112 of 112 |
| Historical facts (26): attributes and registration unchanged, none given a block | yes |
| Minneapolis analysis, 93 facts: payloads opened | **23 (180.8 MB)** instead of 93 (601.6 MB); 70 facts from 174.6 KB of attributes |
| St. Paul analysis, 45 facts: payloads opened | **3 (13.3 MB)** instead of 45 (198.3 MB) |
| Compact-preferred versus `--payload-only` analysis | identical for both coordinates |

Mixed legacy/new canonicalization stayed correct: Minneapolis **93 facts → 85
opportunities → 65 samples** (the 8 re-acquired duplicates and the 20 identical
re-issued-version hours still collapse; nothing ambiguous or excluded). Descriptive
metrics: bias +1.43 K, MAE 1.44 K, RMSE 1.65 K; lead 1–6 N 24 (+1.49 K), 7–18 N 39
(+1.32 K), 19–36 N 2 (+2.71 K); day N 32, night N 33; one station (KMIC), 20 distinct
observations over 19 hours, 4 targets. St. Paul: 45 → 45 → 45, bias +0.58 K, MAE
0.61 K, RMSE 0.79 K, station KSTP. Both remain **`insufficient_evidence`**: this is
still one 19-hour weather episode at one station per site. Repeated CLI runs and the
HTTP route were identical outside the `evaluation` block and changed no row or object
(10 issuances, 138 facts, 172 objects). Evidence is under
`%LOCALAPPDATA%/MesoForge/baselines/20260917-compact-attributes`. **3,274 offline
tests** (same three pre-existing failures deselected) and the 72 forward-run,
verification and storage integration tests passed, with Ruff, mypy, nine import
contracts, lock, documentation/hygiene and `git diff --check`.

Remaining limitations: the 26 facts saved before this change keep needing payload
reads (by design, no backfill); the saved cloud fraction is null in plain forward-run
issuances, so sky context is available only where the richer issuance path saved it;
and forward `verify()` itself still reads full issuance payloads.

#### Decision-window and evidence policies

Two owner-approved policies are recorded (full text in RFC §6.7.14). Neither changes a
forecast, a stored issuance or a fact, and **no correction is calculated or applied**.

- **`mesoforge-decision-window-policy.v1`.** The first successful eligible issuance
  for a scheduled decision window is its canonical operational forecast. An identical
  reissue is preserved and collapses analytically; a materially different reissue is
  preserved as an alternate and never silently replaces the primary; older versions
  whose role cannot be determined stay ambiguous. Today a window is the coordinate and
  `target_reference_time`, and the forward run already refuses a second version unless
  `--reissue` is given. The smallest future addition, to arrive with scheduling and not
  before, is three nullable issuance-header fields: `decision_window_id`,
  `issuance_role` (`primary` | `reissue`) and `reissue_of`.
- **`mesoforge-bias-evidence-policy.v1`.** A deterministic temperature-bias correction
  may be *proposed for shadow evaluation* only per lead bucket (1–6, 7–18, 19–36 h,
  never pooled), from canonical samples only, with at least **30 samples** on at least
  **10 distinct decision dates** (UTC date of the target reference time), no more than
  **25% of the samples from one date** (the v1 proxy, with the ten dates, for spanning
  several weather episodes), and a reported date-clustered 95% interval of the mean bias
  that **excludes zero**. These are initial governance thresholds, not claims of
  statistical sufficiency. There are no regime thresholds.
- **Lifecycle.** Verified evidence → deterministic candidate correction → shadow
  correction on future forecasts → identical-sample verification against the unchanged
  baseline → human, versioned promotion only if improvement is shown on at least MAE
  and RMSE. The later AI desk is judged against the bias-corrected baseline.

The analysis (policy `mesoforge-site-verification-analysis.v2`) reports each criterion's
required and observed value per bucket and keeps `candidate_correction` null. Real
evaluation on September 17, 2026:

| Minneapolis bucket | N | Decision dates (windows) | Largest date share | 95% interval of mean bias | Result |
| --- | --- | --- | --- | --- | --- |
| 1–6 h | 24 | 2 (4) | 75% | +1.13 to +1.82 K | insufficient: samples, dates, concentration |
| 7–18 h | 39 | 2 (4) | 69% | −2.37 to +5.25 K | insufficient: dates, concentration, interval includes zero |
| 19–36 h | 2 | 1 (1) | 100% | not computable | insufficient: every criterion |

Thirty-nine samples alone do not satisfy the policy. All 65 samples come from one
roughly 19-hour episode and one station (KMIC). St. Paul (45 samples, one decision
date, KSTP) fails every bucket. Both coordinates are `insufficient_evidence`; the right
next action is continued forward accumulation. Repeated runs were identical outside the
`evaluation` block and changed no row or object; 37 site-analysis unit tests, Ruff,
mypy, import contracts and the documentation/hygiene checks passed.

### Local surface baseline grid

Surface preparation/issuance derives **one 7 × 7 context grid at 6 km spacing**,
centered on the configured latitude/longitude, with a nested **3 × 3 editable subset**.
Its WGS84 azimuthal-equidistant projection spans **36 × 36 km context / 12 × 12 km
editable**, measured between outer node centers. These are replaceable implementation
defaults in `SurfaceGridGeometry`, not permanent product geometry or resolution.
The existing 150 km source-preparation footprint is separate from this measured grid.
No geographic input beyond latitude/longitude is required; `name` remains optional.

Each of the 49 nodes uses the existing native extraction and surface operators for
all 36 hours. HRRR/GFS temperature remains 70/30; dew point and coupled U/V/gust retain
the approved field-specific rows and fallbacks. RH derives from each node's resulting
temperature/dew point, and wind speed/direction derive from earth-relative U/V.
RAP/IFS remain zero-weight shadows with native valid-time gaps. Cells outside prepared
coverage remain explicitly missing; they do not invalidate an available center or
trigger acquisition. Other scientific exclusions remain per field/contributor.

Each cell explicitly carries `inside_editable_domain`, `context_only`,
`is_forecast_point` and `signed_distance_to_editable_boundary_m`. The editable square
includes its boundary: distance is positive inside, zero on the boundary and negative
outside. These projected Euclidean distances and retained geometry permit a future
smooth taper; **no taper weights, edits or corrections are applied**. The 9 editable
nodes and 40 context-only nodes share the same field representation. The point belongs
to the editable subset; it is not a separate third domain.

The configured point is an actual center node and is **extracted from the retained
grid**, not recalculated through a second point-only surface path. Off-node local-grid
interpolation is deferred: it will need explicit handling of diagnostic order and
spatially changing fallback rows. An HTTP request for another coordinate needs its own
prepared local grid and receives `409 coverage_required`, not an unsupported-model claim.

Explicit surface batch/forward issuance automatically includes `local_grid_baseline`
in the existing immutable forecast JSON stored through PostgreSQL/MinIO. It retains
axes, projection, geometry parameters, domain extents/masks, boundary distances,
geographic nodes, all hourly fields, contributor values, applied
weights/row identities, missing reasons, source evidence and transformation/dependency
hashes. The original baseline stays intact for future versioned regional/time edits.
Point-hour selection/verification carries the grid checksum and extraction metadata;
read the exact issuance for the full grid rather than duplicating it for every hour.
This first representation favors inspectability; its JSON packaging is not a permanent
large-grid storage design.
Previously retained v1 3 × 3 grids remain readable with their original bytes and hashes;
they are not rewritten or assigned invented context/editable masks.

For offline preparation and HTTP reads, use an existing selected surface preparation
directory containing `preparation.json`. It supplies the exact contributor configuration,
cycles and shadow paths automatically; no cycle or station arguments are needed:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'MesoForge/baselines/20260909-8d0983f-d6c8ced2/environment/Scripts/python.exe'
$sourceRun = Join-Path $env:LOCALAPPDATA 'MesoForge/forward-runs/surface-20260911T182732Z'
$grids = Join-Path $env:LOCALAPPDATA 'MesoForge/local-grids/20260911-minneapolis-context'
$env:PYTHONPATH = "$PWD/src"
& $python -B -m mesoforge.application.prepared_local_grid --config "$sourceRun/locations.json" --prepared-run "$sourceRun/prepared" --output-dir $grids
& $python -B -m mesoforge.api --data-dir $grids --port 8765
```

The demonstrated config is `{"locations":[{"lat":44.98859,"lon":-93.25557,"name":"Minneapolis"}]}`.
Open `http://127.0.0.1:8765/forecast?lat=44.98859&lon=-93.25557`.
Preparation loads shared guidance once, builds grids sequentially, isolates location
errors, and writes checksummed compressed canonical JSON outside Git. Repeating with
the same retained inputs/code reproduces and reuses the artifact; existing files are
never silently overwritten. A changed input/implementation should use a new output
directory. HTTP loads and verifies retained grids at startup and only reads them during
GET requests; native surface directories receive a preparation-required response.
The offline export creates no new issuance or verification record.

Minneapolis demonstration used retained **September 11, 2026** guidance: HRRR/GFS
12Z, RAP 15Z and IFS 06Z; reference 18Z; valid times September 11 19Z–September 13 06Z.
It did not reacquire current guidance. All eight baseline fields (including U/V and
diagnostics) were present at all **49 × 36 = 1,764 node-hours**. IFS preserved 12 native
temperature times and 24 gaps per cell; instantaneous IFS gust stayed unavailable.
Every center-hour value, source value and provenance entry matched the prior point-only
demonstration exactly; maximum numerical difference was **0**. This demonstrates exact
node extraction, not accuracy of future off-node interpolation or forecast skill.

Three candidates were measured sequentially in fresh processes against those same
retained inputs. MB below are decimal; peak working memory includes serialization and
the point-result grid copy, but not a running API or database. These measurements show
implementation cost and point consistency, not optimal meteorological domain size.

| Context / editable nodes | Spacing | Context / editable width | Editable / context-only nodes | Build | JSON / gzip | Peak memory |
| --- | --- | --- | --- | --- | --- | --- |
| 5 × 5 / 3 × 3 | 6 km | 24 / 12 km | 9 / 16 | 23.06 s | 31.55 / 3.31 MB | 432 MB |
| **7 × 7 / 3 × 3 (default)** | **6 km** | **36 / 12 km** | **9 / 40** | **44.83 s** | **59.86 / 6.25 MB** | **651 MB** |
| 7 × 7 / 3 × 3 | 10 km | 60 / 20 km | 9 / 40 | 45.53 s | 59.86 / 6.25 MB | 651 MB |

The default gives two context rings extending another 12 km beyond the editable
boundary on each side, with finer sampling than the similarly costly 10 km option.
Build-only peak working memory was about 274 MB; loading the four source files took
0.66 seconds, serialization 1.68 seconds and compression 0.67 seconds in that candidate
measurement. The complete preparation command also extracts/copies the point payload.

Geographic extents below describe outer node centers (latitude; longitude):

| Candidate | Context extent | Editable extent |
| --- | --- | --- |
| 5 × 5, 6 km | 44.880508–45.096569; −93.408020–−93.103120 | 44.934574–45.042580; −93.331723–−93.179417 |
| 7 × 7, 6 km | 44.826390–45.150558; −93.484460–−93.026680 | 44.934574–45.042580; −93.331723–−93.179417 |
| 7 × 7, 10 km | 44.718004–45.258534; −93.637773–−92.873367 | 44.898536–45.078573; −93.382572–−93.128568 |

The four prepared NetCDF files occupy **19,219,528 bytes**, shared by both domains.
All three candidates opened each source once, made zero network attempts, left source
files/hashes unchanged and matched all original point hours/provenance exactly.
The chosen default retains **59,859,260 bytes canonical JSON / 6,251,091 bytes gzip**.
The preparation command above was executed using the existing locked environment.
Repeating it with network access blocked reproduced the exact grid/index bytes and
reused the retained artifact. Repeated read-only API requests returned identical data
without source loading or recalculation. The actual prior v1 artifact also read back
with unchanged bytes, checksum and point values. Detailed measurements, the 36-hour
Minneapolis report and replay evidence remain outside Git under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-nested-domains/`.

Focused domain checks cover geometry variation, masks/boundary distance, legacy v1
readback, deterministic replay, analytic spatial fields, wind/RH/units, field weights,
shadow gaps, missing corners, preparation identity, read-only HTTP and storage.
The retained scientific/application selection passed **175 offline tests** in this
milestone. The existing forward-run, batch-issuance and temperature-verification
PostgreSQL/MinIO selection passed **34 integration tests**, including exact immutable
readback of the full context/editable grid and preservation of older forecasts.
Fresh temporary services were stopped afterward. Ruff, formatting, mypy, import
contracts, the locked-dependency check, documentation, repository hygiene and
`git diff --check` passed. Full repository acceptance/coverage and deployed resource limits remain
unverified. That spatial milestone added no bias correction, AI editing or precipitation;
the following QPF increment extends the same representation.

### Liquid precipitation on the local grid

Every context/editable node now carries `liquid_equivalent_precipitation_amount_1h`.
The exact point reads its QPF from the center node, just like the existing surface
fields. Stored values remain unrounded **kg/m² (equivalent mm of liquid water)**,
with `interval_start`, `interval_end` and `(start, end]` closure. Reports display
inches by dividing by 25.4 and show the full UTC accumulation bounds. Valid zero,
small positive amounts and unavailable amounts remain distinct; no trace cleanup,
PoP or precipitation-type inference is performed.

HRRR supplies rolling one-hour APCP. GFS uses the retained six-hour bucket selector
and `compute_one_hour_qpf`: pass through the first hour after a bucket reset,
otherwise subtract the preceding **same-bucket native grid** before spatial
interpolation. The first requested hour may require one additional preceding GFS
message. Early duplicate bucket candidates must be equivalent. Negative parents,
incompatible windows and missing corners are explicit exclusions. The existing
finite-precision difference tolerance of −0.000001 kg/m² may floor tiny negative
differences, with its flag retained; positive amounts are never cleaned away.

Only identical one-hour intervals enter `blend_qpf` and the retained QPF table:
HRRR/GFS 70/30 through hour 18, then 60/40 through hour 36. Approved single-model
rows retain the exclusion reason and applied weight; neither model available means
null. RAP/IFS stay zero-weight surface shadows and have explicit unavailable QPF.
Sum contiguous hourly intervals to obtain longer totals; across hour 18/19, sum
the individually weighted hours rather than applying one weight to the whole period.
This proves temporal accumulation consistency, not area-integrated conservation of
bilinearly interpolated model-grid depths.

New forward runs enable QPF discovery automatically. The separate discovery command
above uses `--surface-fields --qpf-fields`; the exact selected precipitation objects,
including a GFS parent when needed, are checked against discovery identities/cutoffs.
Preparation is shared across coordinates, outside HTTP. Stored parent evidence includes
native bounds/units, cycles/leads, URLs, ranges, hashes, availability/acquisition times,
duplicate equivalence and finite-precision flags. Raw precipitation joins the existing
manifest/raw-file/NetCDF path; there is no separate forecast history system.

For an existing retained selected surface preparation, add only its missing QPF to a
new snapshot, then build the grid. Old snapshots are not overwritten. The original
model-set decision remains historical; later QPF acquisition is identified separately:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'MesoForge/baselines/20260909-8d0983f-d6c8ced2/environment/Scripts/python.exe'
$surface = Join-Path $env:LOCALAPPDATA 'MesoForge/forward-runs/surface-20260911T182732Z/prepared'
$qpf = Join-Path $env:LOCALAPPDATA 'MesoForge/forward-runs/qpf-20260911-retained/prepared'
$replay = "$qpf-replay"
$grids = Join-Path $env:LOCALAPPDATA 'MesoForge/local-grids/20260911-minneapolis-qpf-final'
$env:PYTHONPATH = "$PWD/src"
& $python -B -m mesoforge.application.prepared_qpf --prepared-run $surface --output-dir $qpf
& $python -B -m mesoforge.application.prepared_qpf --prepared-run $qpf --output-dir $replay --from-raw
& $python -B -m mesoforge.application.prepared_local_grid --config "$surface/../locations.json" --prepared-run $replay --output-dir $grids
& $python -B -m mesoforge.api --data-dir $grids --port 8765
```

The first command acquires QPF once; `--from-raw` rebuilds model files with no provider
access. Use new output directories for enrichment/raw rebuilding; the paths above
already contain this demonstration. Grid rebuilding against identical prepared input
is repeatable in place. Preparation/grid commands were exercised (including their Python
entry functions); repeated `/forecast?lat=44.98859&lon=-93.25557` requests were checked
through FastAPI's in-process client. The localhost server startup command is documented,
not re-executed for this increment.

Rebuilding records a new preparation timestamp while retaining original acquisition
evidence. Scientific fields and intervals reproduce; building twice from the same
prepared snapshot also reproduces the grid artifact bytes. The API only reads retained
grids; building this export does not issue a new forecast or create verification history.

**Minneapolis QPF demonstration (retained September 11 guidance):** HRRR/GFS both use
2026-09-11 12Z, source leads 7–42, aligned to reference 18Z. The 36 hourly intervals run
from `(September 11 18Z, 19Z]` through `(September 13 05Z, 06Z]`. Existing RAP 15Z and
IFS 06Z surface shadows were reused. QPF was acquired separately at 20:15Z; this is a
retained-input demonstration, not a newly discovered current forecast or an assertion
that QPF was part of the original 18Z model-set decision.

- 72 APCP messages: **23,261,188 bytes** of raw GRIB, **25,124,548 bytes** including
  inventories. Original provider identities were checked before enriching the snapshot.
  Raw data/provenance remain outside Git. No new RAP/IFS data were downloaded.
- Same 7×7, 6 km grid: 49 context nodes, nine editable nodes. All **1,764 node-hours**
  have QPF; 1,506 are zero and 258 positive. The 324 editable and 1,440 context-only
  values have no missing HRRR/GFS intervals. RAP/IFS QPF remains explicitly unavailable.
- HRRR/GFS prepared files total **21,964,672 bytes**; unchanged shadow NetCDFs total
  **1,344,760 bytes**. The complete local artifact includes the original surface fields,
  raw-parent evidence, weights and accumulation bounds: **73,101,016 bytes** canonical
  JSON, **9,451,023 bytes** compressed. Building/retaining it took **57.9 seconds** in
  this local measurement, plus **0.8 seconds** to load shared guidance.
- Every blended node-hour exactly equals its independently weighted contributors.
  All six GFS six-hour sums exactly equal separately decoded retained bucket-end values
  across all 285 native subset cells: maximum difference **0 kg/m²**. Hourly totals also
  reproduce longer totals across the 18/19-hour weight change.
- The exact center matches its retained grid cell. Existing surface values are unchanged
  (maximum difference **0**). Network-blocked raw rebuilding reproduces QPF arrays and
  interval bounds exactly. Repeated grid builds and API reads are identical, with
  **zero downloads**; HRRR/GFS/RAP/IFS are each opened once per shared grid build.

At the point, HRRR is zero in every interval. The positive GFS/blend intervals below
illustrate that tiny liquid amounts are retained. All other point hours are valid zero.

| UTC interval on September 12 | HRRR mm | GFS mm | Baseline inches |
|---|---:|---:|---:|
| 01–02Z | 0 | 0.0027214463 | 0.0000321431 |
| 02–03Z | 0 | 0.1360843389 | 0.0016072953 |
| 03–04Z | 0 | 0.7415735537 | 0.0087587428 |
| 04–05Z | 0 | 0.3819107852 | 0.0045107573 |
| 05–06Z | 0 | 0.0611075000 | 0.0007217421 |

The 00–06Z total is **0.39701928723 mm / 0.0156306806 inches**, also the 24- and
36-hour point total for this dry example. This table rounds for reading; stored
numerical values do not. The full 36-hour surface/contributor report, grid proof and
independent native-bucket conservation proof are retained under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-qpf/`.

QPF validation on September 11: **527 focused offline tests passed**, with no skips,
across 26 preparation, discovery, grid/report, alignment, blend and retained scientific
modules. The exact selection and JUnit/logs are in that evidence directory under
`final-offline-20260911T202725Z/`. The new checks cover exact/mismatched intervals,
bucket resets and missing parents, unit/display conversion, small positives versus
zero/missing, bitmap/negative native cells, weights, provenance and deterministic replay.

The selected **34 PostgreSQL/MinIO integration checks passed across an initial run
and focused retest**: the first run had 33 passes and exposed a stale active-model
capability guard, which was corrected; its surface forward-run case then passed.
The integration selection was `tests/integration/application/test_forward_run.py`,
`test_batch_issuance.py` and `test_issued_temperature_verification.py`. Generated
guidance fixtures exercise real storage, including exact QPF/grid readback and unchanged
older issued versions. Temporary services were stopped afterward. The real Minneapolis
artifact above was retained/read through the local-grid API, not inserted as a new
historical issuance. Ruff, formatting, mypy, all nine import contracts, the offline
locked-dependency check, documentation, hygiene and `git diff --check` passed.
Full repository acceptance/coverage, precipitation skill and operational resource limits
remain unverified; that QPF-only increment added no precipitation verification, PoP or type.

### Probability of precipitation on the local grid

The same context/editable grid now includes `probability_of_precipitation_1h`.
This is **NBM's probability that liquid accumulation strictly exceeds 0.254 kg/m²
(0.01 inch) during the stated one-hour interval**. The comparator is `>` rather than
`>=`: the native message uses GRIB probability type 1, [above the upper limit](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-9.shtml).
It is separate from QPF, the expected liquid amount; neither field implies precipitation
type or a weather-condition label.

The retained Phase 2 NBM inventory selector, semantic/grid decoder, percentage-to-fraction
conversion and `pop_passthrough` are reused. NBM has its approved PoP-only weight of 1.0;
it supplies no temperature, wind or QPF contribution in this increment. HRRR/GFS rules
and RAP/IFS surface shadows are unchanged. Native percentages are retained alongside
unrounded fractions, source cycles/leads, exact `(start, end]` bounds, threshold/GRIB
event metadata, raw hashes/URLs, provider identity and availability/acquisition times.
Reports display percent with its own interval beside the separate QPF amount/interval.

Only the exact native one-hour event is selected. Longer-period probabilities, other
thresholds and invalid native probability corners are excluded explicitly. Valid zero
probability remains zero; missing guidance is null with a reason. No period splitting,
probability summing, clipping or synthesis from deterministic amounts occurs.

Normal `forward_run` automatically discovers/prepares this bounded NBM field once for
the coordinate collection, before forecasting. It prefers the newest complete compatible
cycle within the existing NBM age limit; if none is complete, it can retain the freshest
cycle's available native hours with explicit gaps. It never splices NBM cycles. NBM
discovery has its own recorded cutoff, separate from the preceding four-model temperature
decision. An unavailable PoP source does not change surface/QPF calculations.
For the separate selected-issuance command, opt in with `--pop-fields` as shown above;
historical prepared snapshots without a PoP attachment retain their original output.

A prepared-run attachment references the existing HRRR/GFS/RAP/IFS directories unchanged.
Only the new NBM raw messages and spatial views are retained. The existing coordinate
footprints determine shared views internally; no station, grid or region input is needed.
The point PoP is read from the exact local-grid center node, and immutable issuance
stores the complete grid and probability evidence through the existing storage path.

To add PoP to an existing surface/QPF preparation and retain a grid (use new output
directories for the first two steps):

```powershell
$python = Join-Path $env:LOCALAPPDATA 'MesoForge/baselines/20260909-8d0983f-d6c8ced2/environment/Scripts/python.exe'
$qpf = Join-Path $env:LOCALAPPDATA 'MesoForge/forward-runs/qpf-20260911-retained/prepared-replay'
$pop = Join-Path $env:LOCALAPPDATA 'MesoForge/forward-runs/pop-20260911-retained/prepared'
$replay = "$pop-replay"
$config = Join-Path $env:LOCALAPPDATA 'MesoForge/forward-runs/surface-20260911T182732Z/locations.json'
$grids = Join-Path $env:LOCALAPPDATA 'MesoForge/local-grids/20260911-minneapolis-pop'
$env:PYTHONPATH = "$PWD/src"
& $python -B -m mesoforge.application.prepared_pop --prepared-run $qpf --output-dir $pop --config $config
& $python -B -m mesoforge.application.prepared_pop --prepared-run $pop --output-dir $replay --from-raw
& $python -B -m mesoforge.application.prepared_local_grid --config $config --prepared-run $replay --output-dir $grids
& $python -B -m mesoforge.api --data-dir $grids --port 8765
```

`--nbm-cycle 2026-09-11T18:00:00Z` is an optional reproduction override, not required
for normal preparation. Offline replay uses retained raw NBM messages without provider
access and leaves the referenced surface/QPF snapshots unchanged. A raw rebuild records
its own preparation time; the probabilities and native intervals reproduce. Repeated
grid builds from identical prepared inputs reproduce the retained artifact bytes.

The bounded Minneapolis demonstration used NBM **2026-09-11 18Z**, native leads
1–36, for intervals ending **September 11 19Z through September 13 06Z**. It reused
the prior HRRR/GFS 12Z, RAP 15Z and IFS 06Z surface/QPF inputs. NBM discovery occurred
at **21:29:59Z**, with acquisition at 21:30Z; this is a later PoP enrichment of a
retained run, not evidence that these probabilities were available at the earlier
18:27Z four-model decision. All 108 provider-object identity checks matched.

Only 36 PoP GRIB messages were acquired: **32,317,484 bytes** of raw guidance plus
455,914 bytes of retained indexes; the preparation downloaded **33,229,312 bytes**
including discovery/revalidation indexes. The regional NBM file is **10,081,917 bytes**
(36 × 132 × 132 native values). Existing model data were referenced, not duplicated.
The unchanged 7×7 / 6 km local geometry contains 9 editable and 40 context-only nodes.
Its complete surface/QPF/PoP artifact is **83,080,612 bytes**, **11,243,436 compressed**;
building and retaining it took about **66 seconds** on this Windows workstation.

All **1,764 node-hours** had valid native PoP (858 zero, 906 positive), covering both
domains with no missing intervals in this sample. Every prior surface/QPF/shadow value
was exactly unchanged. Point PoP came from the grid center; independent reconstruction
from retained native percentage corners differed by at most **5.56e-17** as a fraction.
Five prepared model files were each opened once per build and reused across the grid.
Raw replay reproduced all native values and intervals; repeated grid builds reproduced
identical compressed bytes, and two in-process API reads matched the saved grid exactly.
All replay/build/API checks blocked provider access and made **zero network calls**.

Example point results (unrounded values remain stored):

| UTC interval `(start, end]` | NBM PoP of >0.01 inch | Separate HRRR/GFS QPF |
| --- | ---: | ---: |
| Sep 11 18–19Z | 0% | 0 in |
| Sep 12 03–04Z | 21.438868% | 0.008758743 in |
| Sep 13 05–06Z | 1% | 0 in |

The full 36-hour report, source evidence, reconstruction proof and logs are retained
outside Git under `%LOCALAPPDATA%/MesoForge/baselines/20260911-pop/`
(`minneapolis-pop-hourly-report.md`, `real-grid-proof.json`). Explicit unavailable and
incompatible-interval cases are covered by focused fixtures rather than manufactured
in the real demonstration.

Validation for this increment: **324 focused offline tests** passed, including
probability semantics, zero/missing values, source/time alignment, grid extraction,
shared loading and replay. The affected selected/prepared tests passed again after
the replay byte-accounting fix (38 reruns, not additional unique cases). The existing
surface forward-run PostgreSQL/MinIO integration test also passed with synthetic NBM
GRIB inputs: both new forecasts read back exactly, older history remained unchanged,
and an invalid coordinate did not stop issuance. Temporary services were stopped.
Ruff, mypy, import contracts, the offline lock check, documentation, hygiene and
`git diff --check` passed. Full storage/application acceptance and PoP calibration/skill
were not evaluated; temperature remains the only verified/scored field.

The real-data preparation, raw rebuild and grid commands above were exercised through
their application functions; repeated `/forecast` requests were tested in-process.
The shown standalone API startup command was not executed for this increment.

### Native probability shadows

**September 12 checkpoint:** this architecture was already present when the p-type
milestone completed. The new check reuses it unchanged, including all four retained
native probability events. NBM remains the sole active hourly field source. A matched
native six-hour comparison is available between NBM and GEFS, not between GEFS and
the one-hour delivered field. Different thresholds, comparators, periods or unresolved
spatial support remain explicit rather than being converted to hourly PoP.

Current official [NBM](https://www.nco.ncep.noaa.gov/pmb/products/blend/),
[GEFS six-hour PQPF](https://www.nco.ncep.noaa.gov/pmb/products/gens/),
[REFS](https://www.nco.ncep.noaa.gov/pmb/products/refs/) and
[ECMWF open-data](https://www.ecmwf.int/en/forecasts/datasets/open-data) catalogs were
rechecked. REFS documentation remains preliminary; ECMWF's native `tpg1` event
remains a 24-hour probability of at least 1 mm. No further compatible registered
source or new acquisition was needed to reproduce the bounded Minneapolis comparison.
These catalog checks do not promise availability of every cycle or unacquired period.

Replaying the existing probability attachments from the completed p-type run required
**zero provider calls and zero new downloads**. All four native arrays and their
acquisition records reproduced exactly; only the replay preparation identity changed.
The rebuilt 49-cell grid preserved all **1,764** existing surface/QPF/PoP/p-type
cell-hours. Each of 14 prepared files was loaded once, the configured point matched
its grid center, and two in-process API reads were exact. All 49 grid cells retained
the matching NBM/GEFS event and their own disagreement; unacquired native endpoints
remained explicitly missing. No new provider, recipe, member-fraction calculation or
forecast-history path was necessary.

**150 focused offline tests passed** for probability decoding/comparison/preparation,
p-type, grid/report handling and retained QPF/PoP science. Documentation validation,
repository hygiene and diff checks passed. PostgreSQL/MinIO integration, full
acceptance, probability calibration and precipitation-type skill were not run.
The new replay is outside Git at
`%LOCALAPPDATA%/MesoForge/forward-runs/pop-recheck-20260912/replay`; the matching API
data directory is `%LOCALAPPDATA%/MesoForge/local-grids/20260912-minneapolis-pop-recheck`.
The 36-hour evidence report and replay checks are under
`%LOCALAPPDATA%/MesoForge/baselines/20260912-pop-recheck/`.
The existing replay/grid commands below also accept a p-type-enriched prepared run
and preserve that attachment unchanged. No new command is needed.

**Delivered PoP remains the original NBM hourly field.** NBM-only PoP is the current
implementation baseline, not the intended final product. Long-term PoP should be a
measured/calibrated multi-source probabilistic forecast. Deterministic QPF may later
be a calibration predictor, but rainfall amounts are not probabilities themselves.
Final source weighting/calibration must be selected from verification evidence.

The bounded preparation command registers native probability products separately
from deterministic model contributors. All new products have **zero active weight**.
Each retains its native percentage and unrounded fraction, threshold/comparator,
exact accumulation bounds, spatial support, model cycle/lead/version and raw/object
provenance. No deterministic QPF or ensemble-member fractions are used to manufacture
these probabilities. Existing HRRR/GFS QPF stays unchanged; this step does not enable
additional RAP/IFS amount fields. P-type comes from its separate native-guidance
attachment, which probability replay preserves unchanged.

The September 11, 2026 source inspection found:

| Product | Native event inspected | Use in this increment |
| --- | --- | --- |
| NBM hourly | Point/grid probability >0.254 kg/m² over one hour | Unchanged active baseline. |
| NBM native six-hour | Point/grid probability >0.254 kg/m² over six hours | Zero-weight native comparison reference, acquired directly—not aggregated hourly PoP. |
| GEFS bias-corrected PQPF | Point/grid probability >0.254 kg/m² over six hours | Compatible with the matching native NBM six-hour event; incompatible with hourly control. |
| REFS parallel | Neighborhood probability >12.7 kg/m² over one hour | Native shadow only; threshold/support differ, and the retained message does not encode neighborhood radius. |
| ECMWF IFS ENS `tpg1` | Grid-box-mean precipitation probability ≥1 kg/m² over 24 hours | Native shadow only; threshold, comparator, interval and unnormalized spatial support prevent direct hourly comparison. |

NOAA documents the [GEFS PQPF products](https://www.nco.ncep.noaa.gov/pmb/products/gens/)
and their [six-hour threshold inventory](https://www.nco.ncep.noaa.gov/pmb/products/gens/gepqpf.t00z.pgrb2a.0p50.bc_06hf024.shtml).
The [September 9 NOAA notice](https://www.weather.gov/media/notification/pdf_2026/scn26-048_Updated_RRFS_and_REFS_Implementation_aad.pdf)
describes the REFS parallel feed and planned October 14 operational transition,
weather permitting. Its neighborhood products are not interchangeable with point PoP.
ECMWF's [open-data catalog](https://www.ecmwf.int/en/forecasts/datasets/open-data)
publishes 24-hour probability ranges stepped every 12 hours; the
[`tpg1` definition](https://codes.ecmwf.int/grib/param-db/131060) is precipitation
**1 mm or above**. ECMWF data are © ECMWF, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); normalization and spatial
interpolation here are MesoForge transformations, without ECMWF endorsement.

The bounded Minneapolis experiment reused the existing 18Z-reference surface/QPF/PoP
run and acquired one native message per new product. These are later research
attachments (acquired about 22:48–22:49Z), not claims of availability at the original
forecast decision. The exact source events are:

| Source / source cycle | UTC interval `(start, end]` | Point probability |
| --- | --- | ---: |
| Active NBM hourly / Sep 11 18Z | Sep 11 23Z–Sep 12 00Z, >0.254 kg/m² | 0.000000% |
| NBM native 6h / Sep 11 18Z | Sep 11 18Z–Sep 12 00Z, >0.254 kg/m² | 1.000000% |
| GEFS native 6h / Sep 11 12Z | Same interval and threshold | 3.246593% |
| REFS / Sep 11 12Z | Sep 11 18–19Z, neighborhood >12.7 kg/m² | 0% |
| ECMWF ENS / Sep 11 12Z | Sep 12 00Z–Sep 13 00Z, ≥1 kg/m² | 90.319568% |

GEFS exceeds the **native six-hour** NBM reference by **2.246593 percentage points**.
That is a descriptive disagreement, not a forecast-error score or evidence of skill.
All comparisons require identical threshold/comparator, actual interval/closure and
known spatial support. Incompatible or missing pairs return a null difference with
reasons. Provider population-related GRIB keys are retained without treating them as
reconstructed member lists or inventing a denominator. No member fractions were used.
For these published probabilities, the available member list/count is explicitly
unknown; GRIB probability-product counts are not ensemble-member counts. A future
locally computed fraction would need the expected/available/missing member identities,
qualifying count, denominator and exact threshold/window calculation. That path is
not needed or implemented for this demonstration.

All four products are sampled over the existing context/editable grid. Native periods
appear at their own end times in a separate report table; other hours say no retained
native interval is available. This deliberately bounded experiment does not claim
that unacquired provider periods are unavailable. Hourly NBM is never replaced by
six-hour/daily probabilities, and no probabilities are summed, split or interpolated
in time. Normal forward runs continue with their existing active NBM behavior; broader
automatic shadow-source discovery/collection is not added by this experiment.

The command consumes a bounded **source-event request artifact**, separate from the
unchanged latitude/longitude locations configuration. For the demonstrated events:

```json
[
  {"source_id": "NBM_6H", "cycle": "2026-09-11T18:00:00Z", "start_hour": 0, "end_hour": 6},
  {"source_id": "GEFS_6H", "cycle": "2026-09-11T12:00:00Z", "start_hour": 6, "end_hour": 12},
  {"source_id": "REFS_1H", "cycle": "2026-09-11T12:00:00Z", "start_hour": 6, "end_hour": 7},
  {"source_id": "ECMWF_ENS_24H", "cycle": "2026-09-11T12:00:00Z", "start_hour": 12, "end_hour": 36}
]
```

```text
python -B -m mesoforge.application.prepared_probability_sources --prepared-run EXISTING_SURFACE_POP_RUN --requests native-requests.json --output-dir NEW_SHADOW_RUN
python -B -m mesoforge.application.prepared_probability_sources --prepared-run NEW_SHADOW_RUN --from-raw --output-dir NEW_OFFLINE_REPLAY
python -B -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run NEW_OFFLINE_REPLAY --output-dir NEW_LOCAL_GRID
python -B -m mesoforge.api --data-dir NEW_LOCAL_GRID --port 8765
```

Geographic footprints come from the existing coordinate-derived preparation; no
regions, grids or stations are manually configured. Raw messages are acquired once
per source event and shared across all views/cells. Rebuilding from raw validates
hashes and source events, preserves source provenance, and performs no provider calls.
The acquisition command was covered with fixture transports; the real demonstration
imported the already acquired messages through the same preparation function to avoid
another download. Raw replay, grid preparation and in-process API reads were executed;
the standalone server command was not executed in this increment.

Four raw messages total **1,892,741 bytes**, with **356,717 bytes** of retained
inventory/header evidence. Regional prepared native views total **518,756 bytes**.
Artifacts and the full report are outside Git under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-pop-multisource/`; the usable prepared
run is `%LOCALAPPDATA%/MesoForge/forward-runs/pop-multisource-20260911/replay-validated`.
This is a normalization/comparison demonstration, not PoP verification or calibration.

Validation: **233 focused offline tests passed**, including native product/time/grid
identity, threshold/support mismatches, missing/invalid probabilities, zero shadow
influence, preparation/replay and existing surface/grid/report behavior. The real
Minneapolis check preserved every prior NBM/surface/QPF value over **49 × 36**
node-hours; all four native shadow events were available at all 49 nodes, with the
NBM/GEFS pair comparable at each. Each of nine prepared files loaded once per build.
Independent percentage-corner reconstruction differed by at most **2.23e-16** as a
fraction. Raw replay preserved native values and source provenance; repeated grid
builds and two API reads matched exactly, with provider access blocked throughout.
The grid artifact is **104,680,987 bytes**, **14,516,096 compressed**, built in about
**75 seconds** on this workstation. Code/type/import, documentation, hygiene and
whitespace checks passed. PostgreSQL/MinIO integration and full application acceptance
were not rerun in this increment; no service was started and no PoP skill was measured.

#### ECMWF six-hour normalization outcome

The bounded follow-up checked the exact NBM/GEFS event: **strictly >0.254 kg/m²**
over **2026-09-11 18Z–2026-09-12 00Z**. The outcome is **incompatible / no ECMWF
six-hour probability calculated**. Delivered hourly NBM PoP, all active weights,
deterministic QPF and historical issued forecasts remain unchanged.

There is no matching native probability in the inspected official open-data
catalog/inventory: its precipitation probabilities are 24-hour events starting
at ≥1 mm. The daily probability is not split or converted. Member precipitation
does provide the necessary temporal inputs, but temporal alignment alone does
not establish the same spatial event:

- Actual 12Z-cycle inventories list all **50 perturbed members (1–50)** at both
  leads 6 and 12, without missing or duplicate entries. The complete operational
  ensemble includes a separate control, obtained from `oper/fc` since IFS 50r1;
  it must not be accidentally counted twice. See the
  [official member/control conventions](https://confluence.ecmwf.int/spaces/DAC/pages/272310539/ECMWF+open+data+real-time+forecasts+from+IFS+and+AIFS).
- Only **member 1's two messages** were acquired to check encoding. They contain
  cumulative `tp` in metres over 0–6 and 0–12 hours, on the same published 0.25°
  grid, IFS `cy50r1`. Their difference covers the requested six hours. Members
  2–50 and the separate control were **not acquired**, not declared missing from
  the provider. Control availability was not probed. No partial-population
  denominator or exceedance fraction was invented.
- ECMWF documents raw ENS rainfall as **grid-box-average precipitation**,
  distinct from its calibrated ecPoint product. Published grid spacing is not a
  complete description of the physical event footprint. Bilinear probability
  interpolation onto MesoForge's local grid does not turn that event into a
  point-scale event. The existing application has no established common-support
  normalization for this comparison; adding spatial calibration to force it is
  outside this increment. See the
  [ECMWF spatial-support explanation](https://confluence.ecmwf.int/spaces/FUG/pages/673551197/Section+8.1.7+Point+rainfall).
- The prior generic ECMWF `grid_point` label was too broad. Newly prepared ENS
  metadata now records `grid_box_mean`, published spacing, unknown effective
  footprint and no point downscaling. Even a hypothetical fraction with matching
  threshold/window remains incompatible with the current point-support target.
  Earlier saved artifacts are preserved; replay produces a separate corrected
  metadata version without changing native probabilities. The existing NBM/GEFS
  disagreement remains an exploratory comparison of matched threshold/time
  metadata, not proof of identical physical support or point-verification skill.
- Member subtraction was inspected without clipping: some global cells have
  small negative increments, while inspected Minneapolis cells do not. These
  signed values and packing metadata are retained. No new packing repair,
  missing-value substitution or probability calculation was introduced.

For the requested six-hour window at Minneapolis, NBM remains **1.000000%** and
GEFS **3.246593%**; ECMWF's directly comparable value is **null**, with the
spatial-support reason above. Its separate native daily probability remains
**90.319568% for Sep 12 00Z–Sep 13 00Z**, never a six-hour value.

The probe retained **1,258,844 bytes** of member GRIB messages and **4,000,266
bytes** of provider indexes outside Git under
`%LOCALAPPDATA%/MesoForge/baselines/20260911-ens-six-hour/`. Metadata include URLs,
byte ranges, object identities, acquisition times, hashes, member identities,
source cycle/leads, actual accumulation bounds, units and GRIB version/packing
keys. `offline-member-audit.json` records the explicit unavailable result and
hash-checked replay; `offline_member_audit.py` reproduces the bounded probe from
retained inputs with network access blocked. No all-member download was needed.

Native probability replay uses the existing command unchanged:

```text
python -B -m mesoforge.application.prepared_probability_sources --prepared-run EXISTING_SHADOW_RUN --from-raw --output-dir NEW_SHADOW_REPLAY
```

Validation for this follow-up: **111 focused tests passed** across native
probability decoding/acquisition, contributor comparisons, retained preparation,
local-grid and hourly-report behavior. Ruff, mypy, all nine import contracts,
the offline lock check, documentation, hygiene and `git diff --check` passed.
The member probe replayed with exact hashes/metadata and zero provider calls;
the four existing native probability arrays and source acquisition provenance
also survived raw replay unchanged. A rebuilt Minneapolis grid preserved all
**49 × 36** NBM/surface/QPF cell-hours exactly; repeat grid builds and two API
reads matched exactly with zero provider calls. PostgreSQL/MinIO integration,
full application acceptance and PoP verification/calibration were not run for
this follow-up.

At that checkpoint, the recommendation was **precipitation type from appropriate native categorical and/or
thermodynamic guidance**, retaining model/time availability and explicit missingness
on this same grid. Surface temperature alone is insufficient. Common-support PoP
normalization/calibration can remain a separate future task; no delivered PoP
weight changes are authorized by this experiment.

### Automatic current guidance

Normal preparation needs only the coordinate collection and an external output
folder. The batch command can perform the same preparation before immutable issuance,
using the existing PostgreSQL/MinIO settings. Acquisition never runs inside HTTP GET.
The output includes the actual prepared `directory` to pass to API startup:

```text
python -B -m mesoforge.application.prepared_temperature --config locations.json --output-dir EXTERNAL_PREPARED_ROOT
python -B -m mesoforge.application.batch_forecast --config locations.json --output-dir EXTERNAL_PREPARED_ROOT
python -B -m mesoforge.api --data-dir RETURNED_DIRECTORY
```

Choose either preparation alone or the batch command; the batch command already
prepares once for the whole collection. With an existing snapshot, use
`batch_forecast --config locations.json --data-dir RETURNED_DIRECTORY` to issue
another version without discovery or downloads. A repeated automatic run checks
providers again and creates a separate snapshot; `--data-dir` / `--from-raw` are
explicit offline reuse modes. Distant locations get separate spatial views of the
same retained full model messages, rather than separate model downloads.

Automatic selection snapshots execution time and uses its UTC whole-hour floor as
the target reference. Hours 1–36 therefore start at the next UTC hour. It considers
00/06/12/18Z HRRR extended runs and GFS runs newest first, independently for each
model, within the existing preparation limit of lead 48. Both selected cycles must
cover every requested valid time. Nominal cycle time only enumerates candidates;
it does **not** establish availability. Selection acquires the temperature message
for the final required lead first, then every other required lead, using the existing
provider URLs, inventories, byte-range checks and strict GRIB decoders. A missing
middle hour also rejects a cycle. Provider mirrors are tried before falling back
to an older cycle. Missing, corrupt or incomplete guidance is never filled or
renormalized. No complete usable pair means an explicit failure and no issuance.
If preparation reaches the first valid time, automatic issuance is refused and
requires a new run; it does not silently issue an elapsed hour as a new forecast.

The 128 MiB acquisition body budget still applies, including rejected candidates.
All acquired complete temperature messages and inventories remain outside Git.
`discovery/selection.json` records the candidates examined, failures and selection
reasons; each prepared manifest carries this evidence, exact cycles/leads, source
URLs/ranges, provider availability versus retrieval times, hashes and code identity.
Issued payloads retain the selection summary, per-source acquisition metadata, and
prepared-manifest checksum. Raw
rebuilding preserves the original selection and acquisition evidence. Fixed 70/30
weights remain demonstration weights, and the Phase 2 defaults are unchanged.

For reproducibility, supply **all three** explicit timestamps with either preparation
or `batch_forecast --output-dir`:

```text
python -B -m mesoforge.application.batch_forecast --config locations.json --output-dir EXTERNAL_FIXED_SNAPSHOT --target-reference-time 2026-09-10T12:00:00Z --hrrr-cycle 2026-09-10T12:00:00Z --gfs-cycle 2026-09-10T06:00:00Z
```

These are command forms; the fixed historical example is not a claim of current
forecast availability. Use the isolated interpreter documented above, or the existing
`uv run --locked python` wrapper (wrapper unverified on this Windows checkout).

Automatic-mode demonstration on September 10, 2026: both discovered **18Z** cycles
were complete, so HRRR and GFS selected **2026-09-10 18Z**, leads **5–40**, against
reference **22Z**. Valid times were **September 10 23Z through September 12 10Z**.
All three locations had **36/36 nonmissing hours**, still future at issuance around
22:13Z. One acquisition downloaded **65,282,097 bytes** (63,420,870 raw temperature
bytes plus 1,861,227 inventory bytes). Three separate regions totaled **12,440,910
prepared NetCDF bytes**, reusing the same acquired model messages. No giant
cross-country region was prepared.

| Location | Coordinate | Immutable issued-forecast ID | First / last temperature (K) |
| --- | --- | --- | --- |
| Fresno | 36.7378, -119.7871 | `c347fa99-6200-4b5e-8960-8c0b9fcf380c` | 314.377960 / 294.946714 |
| Wichita | 37.6872, -97.3301 | `89f85924-3764-4792-9155-6c8e7da414a9` | 302.619608 / 296.541069 |
| Raleigh | 35.7796, -78.6382 | `e6456163-ba83-4a1a-b818-a1d56c50f811` | 304.502940 / 296.255264 |

The automatic batch command and actual localhost HTTP requests were executed with
the isolated interpreter. All **108** temperatures matched independent native-grid
interpolation/blending calculations. Issued payloads and acquisition provenance read
back exactly from PostgreSQL/MinIO; the seven older versions remained unchanged.
HTTP calculation and saved-version retrieval created no rows or objects. Offline
coverage reuse downloaded zero bytes; a raw-only rebuild reproduced the source
region's values and selection provenance. An initial demonstration-runner check
requested Fresno from a Raleigh-only rebuilt view and correctly failed for missing
coverage; checking the matching region passed without changing application code.
Evidence and full 36-hour responses remain outside Git under
`%LOCALAPPDATA%/MesoForge/baselines/20260910-automatic-cycles`.

### Rebuild from retained raw messages without downloads

Use the same preparation module with `--from-raw SOURCE_DIR` and a different,
empty `--output-dir`. This mode takes cycles and the target time from the retained
manifest, including its 36-hour declaration (or the earlier three-hour window);
it rejects additional cycle/time arguments. It checks the original source
configuration, raw/index byte counts and checksums, and source times, then reuses the
same decoders, projection, subsetting, and prepared-file writer. It does not create
an HTTP transport. The source `HRRR.nc` and `GFS.nc` files are not needed.

Portable commands (the `uv run` wrapper remains unverified):

```text
uv run --locked python -m mesoforge.application.prepared_temperature --from-raw SOURCE_DIR --output-dir REBUILT_DIR
uv run --locked python -m mesoforge.api --data-dir REBUILT_DIR
```

The equivalent rebuild was exercised through the module's CLI with network calls
blocked, using these arguments and the isolated interpreter:

```powershell
$rebuilt = "$env:LOCALAPPDATA\MesoForge\prepared\20260910T12Z-hrrr12-gfs06-h36-rebuilt"
& $python -B -m mesoforge.application.prepared_temperature --from-raw $snapshot --output-dir $rebuilt
& $python -B -m mesoforge.api --data-dir $rebuilt
```

Both snapshots already exist locally. To serve either again, run only its API startup
command. The rebuild copies raw evidence and the original manifest without changing
the source directory. It records **zero downloaded bytes**, a new preparation time
and code identity, and the original manifest's checksum. It preserves original
acquisition/retrieval timestamps. Prepared arrays, units, coordinates, and forecast
values reproduced exactly in the locked environment; newly serialized files and
manifest identities need not be byte-identical. Replay with changed dependencies
or scientific code has not been established.

No databases are needed. Jobs, registration, history, verification, evaluation, and
AI remain outside this increment.

### Demonstration validation

On 2026-09-10, the isolated locked Python 3.12 environment passed **262 tests** in
two focused runs: the **148-test recorded Phase 2 selection** in [CLEANUP.md](CLEANUP.md)
plus **3 existing acquisition tests**, and **111 API/preparation tests**. The latter consist
of the existing [synthetic API tests](tests/unit/test_forecast_api.py) and new
[GRIB preparation](tests/unit/application/test_prepared_temperature.py) and
[real-input API](tests/unit/test_real_forecast_api.py) tests. To select the four
affected modules (append the recorded selection to run all 262 together):

```text
python -B -m pytest tests/unit/test_forecast_api.py tests/unit/application/test_prepared_temperature.py tests/unit/test_real_forecast_api.py tests/unit/guidance/test_acquisition_v2.py -q -s -p no:cacheprovider
```

These offline tests generate GRIB messages with known values and assert independent
interpolation/blend results, native-grid gradients, actual valid-time alignment,
units, provenance checksums, missing models/hours, unchanged weights, labeled errors,
repeated requests without I/O, and acquisition limits. No retained scientific
assertions were weakened. Independent calculations directly from all 72 downloaded
raw messages matched the 36 actual API temperatures at the example coordinate within
1e-8 K, accounting for the existing cfgrib decoder's float32 precision.

The 15 added extension cases check the default 72-message acquisition, source-age
limits, all 36 independently expected values, UTC date rollover, unchanged Phase 2
late-hour weights, missing models or hours 19/36, repeated requests without I/O,
and exact offline rebuilding. The earlier three-hour tests remain, including old
manifests without horizon metadata and rebuilding without the original NetCDF files.
The actual expanded snapshot was rebuilt with network calls blocked: both native
datasets were identical and every hourly temperature reproduced exactly at two
coordinates. The original snapshot remained unchanged.

Ruff lint/format, mypy, all nine import contracts, lock validation, documentation and
repository hygiene checks, and `git diff --check` passed. Actual localhost requests
returned the real temperatures above; repeated responses matched, another coordinate
worked, and unsupported coordinates returned 422. The listener was confirmed to be
`127.0.0.1` only, then stopped. Full database/storage acceptance, the full coverage
gate, and the live-provider canary suite were not run. This fixed acquisition does
not establish operational provider reliability or forecast skill. No dependencies
or lockfile entries changed for the 36-hour extension.

### Native precipitation type on the local grid

The optional preparation step below adds native type evidence to an existing
surface/QPF/PoP preparation. It reuses that run's selected cycles and coordinate-derived
coverage. One acquisition per model/lead supplies all shared regional views; there
are no per-cell or per-location downloads and no HTTP-time acquisition. The ordinary
`forward_run` command does not yet acquire this attachment automatically.

| Source | Preserved evidence | Temporal support | Role |
| --- | --- | --- | --- |
| HRRR / GFS | Separate instantaneous rain, snow, freezing-rain and ice-pellet flags | Native hourly | Temporary agreement baseline |
| RAP | Same four native flags | Available native hourly leads | Shadow evidence |
| ECMWF IFS | Native category, including wet snow, rain/snow and freezing drizzle | Native three-hourly; gaps explicit | Shadow evidence |
| NBM | Four conditional type probabilities, with original category ranges and percentages | Native valid times | Shadow evidence; no argmax or voting |

Retained Phase 2 explicitly disabled p-type and supplies no active rule to reuse.
The versioned **temporary-hrrr-gfs-native-type-agreement.v1** baseline requires
both complete active classifications. Identical nonempty type sets produce the
named type, or `mixed` for multiple types. Different sets produce `ambiguous`.
Both zero sets produce `unknown` (no type classified, not an assertion of dry
weather). A missing/invalid required source produces `unknown`; neither available
produces `unavailable`. There is no single-source fallback or category weighting.
Shadow disagreement remains visible even when the active sources agree. This is
an interim representation, not a verified final multi-source forecast policy.

**Dry-hour applicability:** the numerical payload keeps native `no_type_classified`
(complete zero flags), missing evidence and contradictory classifications distinct.
It also retains QPF and PoP as separate fields with their own intervals. This preserves
the inputs for the separate [conditions presentation policy](#read-only-structured-condition-preview)
without rewriting original type evidence. Under that policy, hourly PoP <20% plus
exactly zero active hourly QPF makes precipitation not applicable for rendering;
positive QPF or PoP ≥20% makes it relevant. Native type flags alone cannot decide
applicability. This does not change the numerical p-type policy or prove no trace
precipitation exists.
In the September 12 replay, hour 1 retained exactly zero QPF, zero hourly PoP and
zero active type flags with baseline `unknown`. Hour 10 retained 0.22247206611 kg/m²
QPF over its preceding hour, 21.438868% hourly PoP and native type disagreement
(`ambiguous`). Those distinct native facts survive storage and are never relabeled
by the renderer. An accumulation over an hour does not prove
precipitation is occurring at the instantaneous type valid time.

Categories use deterministic nearest-native-cell extraction; categorical codes
and flags are never bilinearly averaged or temporally interpolated. The local-grid
center supplies the exact configured point as before. Each source retains native
values/units, encoding, selected native cell, cycle, lead, valid time, instantaneous
semantics (null accumulation bounds), GRIB metadata, raw/index hashes, byte ranges,
URLs, availability/acquisition times and transformation identity. NBM percentages
remain conditional on precipitation, separate from PoP; snow includes its native
snow/wet-snow category range. Native IFS diagnoses are retained even for tiny rates;
the ECMWF chart's precipitation-rate display mask is not applied.

The implementations follow the [NOAA model inventories](https://www.nco.ncep.noaa.gov/pmb/products/),
[NBM weather elements](https://vlab.noaa.gov/web/mdl/nbm-weather-elements-v4.1),
[ECMWF p-type definition](https://codes.ecmwf.int/grib/param-db/260015) and
[ECMWF category interpretation](https://confluence.ecmwf.int/spaces/FUG/pages/673550833/Section+8.1.10+Types+of+precipitation+-+charts+and+diagrams).
IFS data retain the existing ECMWF open-data licence and attribution. Model profiles
could later help diagnose disagreement: lower-tropospheric temperature/moisture and
pressure/height are needed to resolve melting and refreezing layers. Sparse pressure
levels can miss shallow layers. No custom profile algorithm, 2-m-temperature type
rule, snowfall ratio, or ice-accretion calculation is introduced here.

Commands (preparation/replay/grid command functions and in-process HTTP reads were
exercised locally; this shell sequence and server startup were not executed for this
increment). Substitute external directories appropriate to your machine:

```sh
python -B -m mesoforge.application.prepared_precipitation_type --prepared-run EXISTING_SURFACE_POP_RUN --output-dir NEW_TYPE_RUN
python -B -m mesoforge.application.prepared_precipitation_type --prepared-run NEW_TYPE_RUN --from-raw --output-dir NEW_TYPE_REPLAY
python -B -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run NEW_TYPE_RUN --output-dir NEW_LOCAL_GRIDS
python -B -m mesoforge.api --data-dir NEW_LOCAL_GRIDS
```

Read `http://127.0.0.1:8765/forecast?lat=44.98859&lon=-93.25557` using the existing
localhost API. Each hour includes `surface.fields.precipitation_type` and
`surface.precipitation_type_guidance.contributors`. The hourly report adds a 36-hour
type table and per-source evidence. The saved numerical baseline retains QPF, PoP
and type independently; no type is required merely because an amount/probability is
nonzero, and unknown guidance is not filled from temperature.

The bounded real demonstration enriched the retained **2026-09-11 18Z** reference
forecast (valid 19Z September 11 through 06Z September 13). HRRR/GFS used September
11 12Z, RAP 15Z, IFS 06Z, and NBM 18Z. HRRR/GFS/RAP/NBM each supplied 36 native valid
times; IFS supplied 12, with 24 explicit gaps. Acquisition completed September 12;
this retrospective attachment is not claimed available at the original decision
time and did not alter earlier issued forecasts.

The 36-hour preparation transferred **175,141,132 bytes** (excluding initial adapter
probes); it retained **171,920,292 bytes**
of raw GRIB messages plus **3,517,506 bytes** of indexes outside Git (some initial
NOAA probe messages were reused). The five compressed native regional datasets total
**605,742 bytes**. The existing 7 × 7 / 6 km grid has 9 editable and 40 context-only
cells. Building/retaining it took about **120 seconds** locally; the full baseline
including all earlier provenance is **169,711,333 bytes**, compressed to **24,415,109
bytes**. The large JSON representation remains an implementation measurement.

All **49 × 36** existing surface/QPF/PoP cell-hours remained exactly unchanged.
Each of the 14 prepared files was loaded once. Raw replay reproduced all five native
datasets and acquisition events; repeated grid builds and two in-process API reads
matched exactly with zero provider calls. The Minneapolis point has 33 unknown hours
and 3 ambiguous hours (8–10), rather than manufactured precipitation types.

Validation: **291 focused offline tests passed**, covering native selection/decoding,
category/probability semantics, missingness, nearest-cell extraction, both domains,
raw replay, report rendering, existing preparation/batch/storage tests and retained
surface/QPF/PoP science. Ruff formatting/lint, mypy, all 9 import contracts, existing
documentation/hygiene checks, offline lock validation and `git diff --check` passed.
PostgreSQL/MinIO integration, full application acceptance and precipitation-type
verification/skill scoring were not run for this increment. No package, service,
Phase 2 default or existing weight changes were needed.

Real native cases at September 11 19Z, sampled from the retained messages:

| Case | HRRR | GFS | RAP evidence | Interim result and reason |
| --- | --- | --- | --- | --- |
| 26.31817, -92.99671 (Gulf) | Rain | Rain | Rain | Rain: complete active agreement |
| 47.98259, -112.75259 (Montana) | Snow | Rain | Rain | Ambiguous: active disagreement |
| 48.87410, -113.67456 (Montana) | Snow + ice pellets | No type | Rain | Ambiguous: native mixture retained |
| 47.89070, -112.85033 (Montana) | Rain + snow | Rain | Rain | Ambiguous: mixed evidence is not reduced to rain |
| Minneapolis, first hour | No type | No type | No type | Unknown: zero flags do not assert dry weather |

Those are model diagnoses, not observed or verified precipitation types. The first
retained HRRR hour had no freezing-rain flag. A native GFS freezing-rain case outside
CONUS decoded correctly but is not a supported local forecast demonstration. Focused
fixtures exercise freezing rain, sleet, agreeing snow/mixed types, missing sources,
unknown codes and disagreements; this bounded real window does not establish winter
performance. No extra winter backfill was acquired to force an agreeing case.

Native **interval-aware snowfall water equivalent** is now retained as described
below, followed by separate native and Kuchera-derived snowfall amounts. Inspect native
snowpack and ice-accretion products before adding those fields; do not multiply total QPF
by an instantaneous type flag or assume a universal snow ratio. Derived conditions
should follow these explicit fields and rules later.

## Native snowfall water equivalent

The optional `mesoforge.application.prepared_snowfall` step adds native snowfall
water equivalent to the existing surface preparation and context/editable grid.
This is an interval accumulation, separate from total liquid QPF, PoP, p-type,
snowpack and snowfall depth. **No approved snowfall-water-equivalent blend rule
exists:** `surface.fields.snowfall_water_equivalent_amount` is null with
`policy_unavailable` and no weights. Native contributors remain zero-weight
evidence in `surface.snowfall_guidance`; no delivered field or existing policy changes.

| Source | Retained SWE evidence | Interval treatment |
| --- | --- | --- |
| HRRR | Native accumulated `WEASD`, kg/m² | Exact preceding-hour accumulation; instantaneous snowpack and cycle-total duplicates excluded |
| RAP | Native accumulated `WEASD`, kg/m² | Exact preceding-hour accumulation; source lead limits remain explicit |
| IFS | Native `sf`; metres of water equivalent in the demonstrated feed | Difference compatible cumulative endpoints on native cells, then multiply by 1000 to kg/m²; preserve native three-hour intervals |
| GFS | Unavailable in the inspected `pgrb2.0p25` product | Instantaneous snowpack `WEASD`/`SNOD` is not snowfall accumulation |
| NBM | Unavailable for SWE in the inspected core product | `ASNOW` is newly accumulated snowfall amount; no assumed snow ratio converts it to SWE |

Source definitions follow the [NOAA moisture parameter table](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-1.shtml),
[model inventories](https://www.nco.ncep.noaa.gov/pmb/products/),
[NBM elements](https://vlab.noaa.gov/web/mdl/nbm-weather-elements-v4.1) and
[ECMWF snowfall definition](https://codes.ecmwf.int/grib/param-db/144).
IFS retains its ECMWF attribution and CC-BY-4.0 metadata. Prepared records retain
native units and amounts, exact parent bounds, cycle/lead, provider/product, URLs,
byte ranges, hashes, acquisition/availability times, GRIB version/packing metadata
and transformation identity. IFS parent differencing requires matching native
origin, cycle, grid, units and model identity. Negative/nonfinite parents or increments
remain missing; there is no rounding-to-zero, clipping or hourly interpolation.
HRRR/RAP hourly and IFS three-hour amounts cannot be compared as the same event.
`aggregate_snowfall_intervals` accepts only complete, contiguous, nonoverlapping
periods from one source/cycle/coordinate, preserving component provenance.

Preparation uses the already-selected cycles and coordinate-derived coverage from
an existing surface run. Each native message is acquired once, then shared among
its regional views and all grid cells. This is an explicit attachment, not automatic
`forward_run` snowfall acquisition. Replay verifies retained raw/index checksums
and does not instantiate a provider transport. The exact point is extracted from
the existing local grid; its report separates native SWE amounts and intervals
from the unavailable active baseline, with optional inches of **water equivalent**.

The preparation, replay and grid functions were exercised locally. These shell
examples use placeholders and were not executed verbatim; use the existing locked
environment and new output directories outside Git:

```sh
python -B -m mesoforge.application.prepared_snowfall --prepared-run EXISTING_SURFACE_RUN --output-dir NEW_SNOW_RUN
python -B -m mesoforge.application.prepared_snowfall --prepared-run NEW_SNOW_RUN --from-raw --output-dir NEW_SNOW_REPLAY
python -B -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run NEW_SNOW_REPLAY --output-dir NEW_LOCAL_GRIDS
python -B -m mesoforge.api --data-dir NEW_LOCAL_GRIDS
```

The bounded real experiment reused the **September 11, 2026 18Z** target reference,
with HRRR 12Z, RAP 15Z and IFS 06Z. It acquired 36 hourly HRRR messages, 36 hourly RAP
messages and 13 IFS cumulative endpoints, yielding 12 native three-hour increments
within the 36-hour forecast. Acquisition happened September 12; this is a
retrospective attachment, not a claim that SWE was available at the original decision.
Raw messages total **4,000,217 bytes**, inventories **1,618,777 bytes**; approximately
**5.63 MB** was downloaded including a small retried probe. All are retained outside Git.

A snow-producing Montana point (**47.9825874173, -112.7525884903**) used coordinate-
derived coverage rebuilt from retained surface/QPF guidance with zero downloads.
Minneapolis used its existing prepared coverage. Both retain the current 7×7,
6 km context grid, 9 editable cells and 40 context-only cells, with 36 forecast hours.
These measurements remain implementation defaults, not permanent geographic policy.

| Montana accumulation (UTC) | HRRR SWE, kg/m² | RAP SWE, kg/m² | IFS SWE, kg/m² |
| --- | --- | --- | --- |
| September 11, 18–19Z | 0.605799972576 | 0.001312864207 | Unavailable: no native hourly event |
| September 11, 18–21Z | 0.621219972226, sum of three hourly periods | 0.001312864207, sum of three hourly periods | 0, compatible native-parent difference |
| September 11 18Z–September 12 00Z | 0.643719971236, sum of six hourly periods | 0.001312864207, sum of six hourly periods | 0, sum of two three-hour periods |

This is preserved model disagreement, not evidence of which model is correct.
Minneapolis has valid zero SWE at the demonstrated hour; GFS/NBM remain missing,
and IFS retains 24 hourly gaps rather than invented zero values.

Validation: **81 focused snowfall tests passed**, covering source selection,
unit/window validation, zero versus missing, negative increments, conservation,
disagreement, provenance and raw replay. Existing preparation/batch/API selection
passed **166 tests**; grid/hourly-report selection passed **58 tests**, including
all-cell old-field regression, point extraction, shared loading and immutable
memory-storage readback. Relevant surface/QPF/PoP/p-type tests also passed.
Real raw replay reproduced arrays and native parent provenance with provider access
disabled; new preparation timestamps remain separate from original acquisition times.
Both complete real grids also rebuilt byte-identically with zero provider calls.
Every Minneapolis cell/hour retained exactly the previous surface, QPF, PoP and
p-type payload after removing the new SWE field/evidence. The exact Montana point
matched native contributor extraction for all 36 hours. Retained compressed grids
are approximately 30.02 MB (Minneapolis) and 28.53 MB (Montana); all raw/prepared
guidance and reports remain outside Git. No precipitation verification, winter
skill claim or full PostgreSQL/MinIO
acceptance run is included in this increment.

The following increment adds native snowfall amounts and profile-based Kuchera
estimates without changing this SWE path. Snow depth on the ground remains separate.

## Native and Kuchera snowfall amounts

The optional `mesoforge.application.prepared_snowfall_amount` attachment reuses the
selected cycles, retained SWE and coordinate-derived coverage. It acquires shared
native messages once, outside HTTP, and supplies the existing context/editable grid
and exact center point. These are three distinct quantities:

- **SWE:** liquid-equivalent water associated with snowfall, in kg/m².
- **Snowfall amount:** newly accumulated snowfall depth over an explicit interval,
  stored unrounded in metres and also reported in inches.
- **Snow depth:** total snow already on the ground, such as instantaneous `SNOD`.
  It is not acquired or delivered by this attachment.

`surface.snowfall_amount_guidance` preserves `native_contributors`, `native_slr`
and `derived_contributors` separately. The active `surface.fields.snowfall_amount`
is null with `policy_unavailable` and no weights: **no approved amount blend exists**.
Existing surface, QPF, PoP, p-type and SWE behavior remains unchanged.

| Source | Evidence retained | Meaning and limitations |
| --- | --- | --- |
| HRRR/RAP | Native `ASNOW`, metres | Compatible cycle-total endpoints are differenced on native cells to obtain hourly new-snow amounts; this is the model's native variable-density diagnostic, not Kuchera |
| NBM | Native one-hour `ASNOW`, metres | New snow/sleet amount under the provider's winter definition; distinct hydrometeor scope from HRRR/RAP snow-only guidance |
| NBM | Native `SNOWLR` | Separate instantaneous model ratio with its own valid time and GRIB metadata; not applied to another source's SWE |
| RAP | Kuchera estimate | Derived hourly amount from same-cycle, same-grid SWE and the interval-end air-temperature profile; zero active weight |
| GFS/IFS | Explicitly unavailable for native amount in the inspected feeds | GFS `SNOD`/instantaneous `WEASD`, IFS `sd`/`rsn` are snowpack state; IFS `sf` remains SWE, not new-snow depth |

Native definitions follow the [NOAA moisture table](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-1.shtml),
[NOAA's HRRR/RAP winter diagnostics](https://repository.library.noaa.gov/view/noaa/72271/noaa_72271_DS1.pdf)
and [NBM winter guidance](https://vlab.noaa.gov/documents/6609493/7858320/Blend_Winter_v5.0-Configuration_and_Technical_Details.pdf).
Comparisons require matching intervals, units and hydrometeor scope. NBM snow/sleet
and snow-only estimates are displayed separately with an incompatibility reason.
No daily accumulation is treated as hourly: exact compatible native endpoints are
differenced, including inventories expressing 24 hours as one day. Negative or
nonfinite amounts/parents remain missing, never clipped to zero.

Kuchera is the first derived method, **not an approved delivered forecast or an
assumed truth**. Following the [NOAA Forecast Operations Guide](https://vlab.noaa.gov/web/forecast-guide/fog?page=numerical-methods-for-determining-snow-accumulation),
it uses maximum air temperature from the surface through 500 hPa: SLR is
`12 + 2*(271.16 - Tmax_K)` above 271.16 K, otherwise `12 + (271.16 - Tmax_K)`.
This implementation retains RAP's 21 pressure levels at 25 hPa spacing from
500–1000 hPa, 2 m air temperature and surface pressure. Below-ground levels are
excluded; every required above-ground sample must be finite. The interval-end
profile represents the preceding one-hour SWE interval, an explicit approximation
that cannot resolve subhourly evolution or warm layers between sampled levels.

Kuchera is calculated at each native corner, then its snowfall amounts are
interpolated to the MesoForge grid. Stored evidence includes the profile, mask,
maximum temperature, raw ratio, SWE and derived amount at each corner. The separately
interpolated ratio is diagnostic: multiplying it by interpolated SWE need not
reproduce the interpolated amount. Positive SWE with a nonpositive Kuchera ratio
is unavailable. Complete valid profiles with zero SWE can yield exactly zero amount;
any nonpositive raw ratio is explicitly inapplicable, not clipped or used as a
physical SLR. Missing profiles never receive a surface-only or fixed-ratio fallback.
**Fixed 10:1 is not the preferred method and is not used here.**

Raw ASNOW, SNOWLR and profile messages remain outside Git, with URLs, object
identities, byte ranges, hashes, acquisition times, native GRIB definitions,
cycles/leads and exact accumulation bounds. Native amount, model ratio and Kuchera
remain independently traceable. Native snowfall and SLR must eventually be evaluated
against suitable observations before choosing or calibrating a delivered algorithm.

Use a preparation containing the completed SWE attachment. These placeholder commands
were not executed verbatim; their preparation/replay/grid functions were exercised
with the fixed real case below, using the existing locked local environment:

```sh
python -B -m mesoforge.application.prepared_snowfall_amount --prepared-run EXISTING_SWE_RUN --output-dir NEW_AMOUNT_RUN
python -B -m mesoforge.application.prepared_snowfall_amount --prepared-run NEW_AMOUNT_RUN --from-raw --output-dir NEW_AMOUNT_REPLAY
python -B -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run NEW_AMOUNT_REPLAY --output-dir NEW_LOCAL_GRIDS
python -B -m mesoforge.api --data-dir NEW_LOCAL_GRIDS
```

This explicit attachment is not automatic `forward_run` acquisition. New evidence
acquired retrospectively does not claim availability at the original model-set decision.

The bounded real case reuses the **September 11, 2026 18Z** reference and existing
SWE: HRRR 12Z, RAP 15Z and NBM 18Z. Native amount endpoints cover all 36 requested
hours. The Montana point is **47.9825874173, -112.7525884903**; Minneapolis uses
**44.98859, -93.25557**. Both use the existing nested 7×7 grid at 6 km spacing, with
9 editable and 40 context-only cells. These remain demonstration geometry defaults.

| Montana, September 11 18–19Z | Native snowfall amount, m | Matching native SWE, kg/m² | Kuchera amount, m |
| --- | --- | --- | --- |
| HRRR | 0.00384400039670 | 0.605799972576 | Unavailable: no complete retained HRRR profile in this increment |
| RAP | 0.00000678985685 | 0.001312864207 | 0.00000360277296 |
| NBM | 0 | Unavailable for this product | Not calculated |

RAP native and Kuchera snowfall differ without changing either source. The point's
Kuchera diagnostic ratio is about 1.31748, with complete corner profiles and ratios
retained; it is not a ratio to multiply by the displayed interpolated SWE. NBM's
separate native SLR is about 4.66328 at 19Z, with instantaneous semantics. GFS/IFS
native new-snow amount remains explicitly unsupported in these feeds. The active
snowfall amount remains unavailable. This is real model evidence, not an observed
snowfall validation or a winter-skill ranking.

At that point HRRR has 35 usable native hourly amounts, RAP 34, and NBM 36.
Three intervals have a decreasing native cumulative value at a required corner:
RAP ending September 12 01Z/10Z and HRRR ending 02Z. They remain unavailable rather
than becoming zero or being renormalized across the other corners. Their original
parents are retained; no cause for the decrease is assumed. RAP Kuchera has 36
calculable amounts from its separate SWE/profile evidence, including genuine zero
water intervals. A valid Kuchera value does not repair missing native ASNOW.

New unique raw messages total **75,384,450 bytes**, and inventories **1,581,795 bytes**.
Total network transfer was **76,996,905 bytes**, including the initial inventory
probes and the two corrected 24-hour ASNOW selections. Existing SWE/surface data
was reused. Retained raw messages generated both regional preparations offline;
compressed prepared native arrays total **2,790,625 bytes** for Montana and
**1,450,798 bytes** for Minneapolis. Raw/profile replay reproduced the arrays,
intervals and original acquisition provenance with zero provider calls; replay
preparation timestamps and code identity are separately recorded.

For each real grid, all **1,764 existing cell/hour payloads** exactly match the saved
SWE-milestone baseline after removing only the new snowfall-amount field/evidence.
Both exact point forecasts match the native amount/profile extraction for every hour.
Montana's first six hourly HRRR amounts sum to **0.00412600114676 m**, and RAP to
**0.00000678985685 m**, matching independently extracted cumulative endpoint
differences within 1e-12 m. The complete retained grids occupy **62,968,931 bytes**
(Montana) and **64,216,757 bytes** (Minneapolis) compressed; build-and-retain times
were about **212 s** and **233 s** on this machine. These artifacts include all
previous fields and detailed source/profile evidence, not just snowfall arrays.
Both complete grids then rebuilt **byte-identically with zero provider calls**;
the retained immutable artifacts were reused rather than overwritten.

Validation: **601 focused and retained offline tests passed** (509 scientific,
preparation/grid/report and retained-path checks, plus 92 API/issuance checks),
including 110 new provider/profile/extraction/preparation tests. Tests cover
interval and day/hour alignment, independent Kuchera values, units, zero/missing
amounts, missing profiles, native versus derived disagreement, source corruption,
shared loading, point extraction, replay and immutable memory-storage readback.
Ruff/formatting, mypy, all nine import contracts, documentation/hygiene checks,
the offline lock check and `git diff --check` passed. No real database/MinIO or
full acceptance run, snowfall observations, snowfall verification or calibrated
blend is included in this increment.

## Native cloud cover on the local grid

The optional `mesoforge.application.prepared_cloud` attachment retains native
instantaneous **total cloud cover** from five sources. New V2 surface issuances use
native NBM total sky cover as the temporary active `surface.fields.cloud_area_fraction`
under `nbm-native-total-cloud-baseline.v1`. The canonical fraction (`1`), original
unrounded percentage, category and native provenance are all retained. Only an
eligible NBM value receives weight 1; no alternate source fills an unavailable hour.
This is an explicitly approved interim source policy, not a Phase 2 cloud blend;
retained Phase 2 defaults stay unchanged.
`surface.cloud_guidance` holds each native contributor, unrounded percentage,
original unit/value, sky category, product, source cycle/lead, valid time, raw
hashes and extraction corners/weights. HRRR/GFS/RAP/IFS retain zero active weight
and their descriptive disagreements. Future cloud blending/calibration requires
verification evidence; temperature weights do not apply.
Existing surface, QPF, PoP, p-type, SWE, native snowfall, NBM SLR and Kuchera
values and policies remain unchanged.

| Source | Selected native product | Treatment |
| --- | --- | --- |
| HRRR / RAP | `TCDC:entire atmosphere`, instantaneous forecast | Native percent; retain hourly snapshots |
| GFS | `pgrb2.0p25` instantaneous entire-atmosphere `TCDC` | Exclude averaged and layer-specific clouds |
| NBM | Core deterministic `TCDC:surface` total sky cover | Exclude ensemble spread and low/mid/high cloud fields |
| IFS | Official deterministic `tcc`, parameter 164 | Native fraction converted to percent; preserve three-hourly gaps |

Total and layer clouds have distinct [NOAA parameter definitions](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-6.shtml).
The [NBM inventory](https://www.nco.ncep.noaa.gov/pmb/products/blend/conus/00/blend.t00z.core.f001.co.grib2.shtml)
distinguishes total sky cover from layers and spread. [IFS total cloud cover](https://codes.ecmwf.int/grib/param-db/164)
retains ECMWF open-data attribution/licence metadata in every available contributor
(CC-BY-4.0): this service is based on data and products of the European Centre for
Medium-Range Weather Forecasts (ECMWF). Cloud guidance is extracted and converted
to percent; no temporal interpolation is applied.
Native grids, resolutions and cloud parameterizations remain distinct. Comparisons
are descriptive percentage-point disagreements at the same location and valid time,
not evidence of relative skill. Total cloud is never reconstructed by summing layers.

Sky display policy `native-cloud-percentage-display.v1` uses the **unrounded**
percentage: clear 0–5; mostly clear >5–25; partly cloudy >25–50; mostly cloudy
>50–87; cloudy >87–100. These explicit MesoForge presentation boundaries follow
a [NWS forecast wording reference](https://www.weather.gov/media/pah/ServiceGuide/A-forecast.pdf).
They describe each model's numerical total cover, not an observed opaque-sky amount
or a complete weather-condition string. Zero is clear; missing or out-of-range data
is unavailable, never converted to clear or clipped into range.

Run preparation before building the local grid; it reuses the selected HRRR/GFS/RAP/IFS
cycles and the retained NBM PoP cycle. Each native message is acquired once, then
shared across all regions/cells. Existing prepared meteorological inputs remain
referenced in place. This explicit attachment is not automatic cloud acquisition in
`forward_run`, and HTTP does not acquire guidance. All output directories must be
new and outside Git. Preparation, replay and grid operations were exercised through
these modules' Python functions in the locked environment. These CLI examples were
not run verbatim; uppercase names are placeholders:

```powershell
python -B -m mesoforge.application.prepared_cloud --prepared-run EXISTING_SURFACE_RUN --output-dir NEW_CLOUD_RUN
python -B -m mesoforge.application.prepared_cloud --prepared-run NEW_CLOUD_RUN --from-raw --output-dir NEW_CLOUD_REPLAY
python -B -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run NEW_CLOUD_REPLAY --output-dir NEW_LOCAL_GRID
```

The existing API can read `NEW_LOCAL_GRID` using its documented `--data-dir`
option; no API service was started for this increment. Replay checks retained raw
and index hashes, decodes again and preserves original acquisition metadata.
Original guidance availability and this later cloud acquisition time remain separate;
adding evidence does not claim it was available at the original forecast decision.

Historical evidence-only demonstration (before the temporary active NBM decision):
the September 12, 2026 bounded acquisition used Minneapolis **44.98859, -93.25557**,
reference September 11 18Z and the existing 36-hour window through September 13 06Z.
An actual same-valid-time point sample on September 11 **21Z / 4 p.m. CDT** was:

| Contributor | Source cycle / lead | Cloud percent, rounded here | Category |
| --- | --- | ---: | --- |
| HRRR | September 11 12Z / 9 h | 100 | cloudy |
| GFS | September 11 12Z / 9 h | 100 | cloudy |
| RAP | September 11 15Z / 6 h | 91.404223 | cloudy |
| IFS | September 11 06Z / 15 h | 73.027897 | mostly cloudy |
| NBM | September 11 18Z / 3 h | 43.049961 | partly cloudy |

HRRR/GFS/RAP/NBM each supplied all 36 hours. IFS supplied this one native snapshot;
11 other native leads returned HTTP 503 and 24 intervening hourly times are outside
the native temporal resolution. Both kinds of missingness are retained separately,
without interpolation or substitution. The active cloud field stays unavailable.

Acquisition transferred **134,379,464 bytes**: 131,304,322 bytes of raw GRIB messages
and 3,075,142 bytes of inventories, retained outside Git. Five initial probe messages
were reused in the full preparation. Compressed regional cloud arrays occupy
**2,004,216 bytes**. Raw replay reproduced arrays, events, hashes and original source
provenance exactly, with zero provider calls; all earlier prepared descriptors matched.

The retained grid is still 7×7 nodes at 6 km spacing, with 9 editable and 40
context-only nodes. At 21Z, HRRR varies from 42.269564–100% across this grid, GFS
97.727262–100%, RAP 70.231422–100%, IFS 67.213306–92.301922%, and NBM
24.546602–48.254519%. All four hourly sources are available at all 49 nodes over
36 hours; IFS is available at all 49 nodes for its one acquired native time.
The exact point equals the retained center node and direct native extraction.
Every one of the **1,764 previous cell/hour records matches exactly** after removing
only the new cloud evidence. The completed snowfall and other fields are unchanged.
This full grid, including all prior fields/provenance, occupies **70,369,704 bytes
compressed**, versus 64,216,757 bytes before cloud evidence; building/retaining it
took **260.51 seconds** on this machine. This is an implementation measurement,
not a resource guarantee or a new geometry decision. A second complete build
reused the identical retained grid artifact: byte-for-byte equality and zero
provider calls, with the same original contributor provenance.

Validation: **80 focused cloud tests passed**, including units/ranges, exact category
boundaries, incompatible definitions, native times, missing products, shared loading,
disagreement, provenance, extraction, offline replay and immutable memory-storage
readback. A **488-test retained selection also passed**, covering the affected grid,
report, preparation and batch paths plus surface/winter/precipitation calculations;
these selections overlap and are not additive. The focused command was:

```powershell
python -m pytest -q tests/unit/application/test_cloud_cover.py tests/unit/application/test_prepared_cloud.py tests/unit/forecasting/test_cloud_cover.py tests/unit/guidance/test_cloud.py tests/unit/application/test_local_surface_grid.py tests/unit/application/test_hourly_report.py -k "cloud or missing_peripheral"
```

Ruff, formatting, mypy, all nine import contracts, documentation/hygiene checks,
offline lock validation and `git diff --check` passed. No real PostgreSQL/MinIO
service, full acceptance suite, cloud-observation verification, calibrated blend,
or forecast-skill assessment was run in this increment.

Native visibility evidence is now implemented in the following increment; the cloud
fields and sky categories remain unchanged. The later [conditions preview](#read-only-structured-condition-preview)
adds its separately versioned presentation rules.

## Native visibility on the local grid

The optional `mesoforge.application.prepared_visibility` attachment adds native
instantaneous surface **horizontal visibility** to the same 49-node, 36-hour grid.
`surface.visibility_guidance` preserves contributors and descriptive disagreements;
`surface.fields.visibility` is null with `policy_unavailable` and empty weights.
No approved retained Phase 2 visibility blend or active-source rule exists.
Cloud/sky categories and all earlier surface, QPF, PoP, p-type, SWE, snowfall,
Kuchera and NBM SLR calculations remain unchanged.

| Source | Native evidence | Treatment |
| --- | --- | --- |
| HRRR | `wrfsfc` instantaneous `VIS:surface` | Native metres, hourly |
| GFS | `pgrb2.0p25` instantaneous `VIS:surface` | Native metres, hourly |
| RAP | `awp130pgrb` instantaneous `VIS:surface` | Native metres at supported cycle leads |
| NBM | Core deterministic instantaneous `VIS:surface` | Native metres; exclude adjacent probability products |
| IFS | Absent from the inspected official deterministic open-data product | Explicitly unsupported; no download or reconstruction |

The [NOAA GRIB table](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-19.shtml)
defines visibility as a distance in metres. Source selection excludes averages,
probabilities, ceiling and cause-specific products. The inspected [IFS open-data
catalogue](https://www.ecmwf.int/en/forecasts/datasets/open-data) does not expose this
field; that is a limitation of the selected feed, not a claim about all ECMWF products.

Each local-grid node samples the nearest native cell, preserving its index, location,
native coordinate distance/units and weight 1. Exact ties choose the lower stored
axis index. This avoids smoothing visibility restrictions; [NOAA's field-selected
NBM algorithms](https://vlab.noaa.gov/documents/6609493/7858320/Description_of_Field-Selected_Algorithms_for_National_Blend_of_Models.pdf)
also distinguish nearest-neighbor ceiling/visibility from bilinear field interpolation.
The forecast point comes from the retained local-grid center node. There is no search
for a different valid neighbor when the selected native value is missing.

Store original unrounded metres, native units/value, cycles/leads, valid times,
product/model metadata, raw hashes, acquisition time and extraction provenance.
The report displays statute miles using exactly **1 mile = 1,609.344 metres**.
Zero remains a valid distance; negative/nonfinite values are unavailable. No arbitrary
upper cap or observation-reporting limit is imposed. Provider diagnostic/censoring
notes remain separate: model definitions and native resolution differ, and the
largest value in an array does not establish a censoring threshold. Comparisons only
describe compatible same-time horizontal surface visibility; they do not establish
skill or a delivered blend. Reduced visibility does **not** diagnose fog,
precipitation type/intensity, or a complete weather condition.

Preparation reuses the existing selected HRRR/GFS/RAP cycles and NBM PoP cycle.
One native visibility message per model/lead is shared across regions and cells;
earlier raw/prepared fields remain referenced in place. Run this explicit step before
local-grid construction; `forward_run` does not yet acquire visibility automatically,
and HTTP does not download it. Use new directories outside Git:

```powershell
python -B -m mesoforge.application.prepared_visibility --prepared-run EXISTING_SURFACE_RUN --output-dir NEW_VISIBILITY_RUN
python -B -m mesoforge.application.prepared_visibility --prepared-run NEW_VISIBILITY_RUN --from-raw --output-dir NEW_VISIBILITY_REPLAY
python -B -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run NEW_VISIBILITY_REPLAY --output-dir NEW_LOCAL_GRID
```

These CLI examples use placeholders and were not run verbatim. The module functions
were exercised with real paths in the locked Python environment. The existing API
can read the resulting grid with `--data-dir NEW_LOCAL_GRID`; no service was started
for this increment. Later visibility acquisition is explicitly recorded separately
from the earlier numerical forecast decision time.

The bounded real demonstration uses Minneapolis **44.98859, -93.25557**, reference
September 11, 2026 18Z, and 36 valid hours through September 13 06Z. Native values
at September 11 **19Z / 2 p.m. CDT**, before any display rounding, were:

| Contributor | September 11 source cycle / lead | Native visibility, m |
| --- | --- | ---: |
| HRRR | 12Z / 7 h | 61400 |
| GFS | 12Z / 7 h | 24135.109375 |
| RAP | 15Z / 4 h | 53703.109375 |
| NBM | 18Z / 1 h | 16274 |
| IFS | No native visibility in selected open feed | unavailable |

All four supported sources supplied 36 snapshots. The experiment acquired
**108,565,957 bytes**, including 105,531,028 bytes of GRIB messages and 3,034,929
bytes of inventories. Four initial probe messages were reused. Compressed regional
visibility arrays occupy **1,543,760 bytes**. All raw data remains outside Git.
Raw replay reproduced arrays, event metadata and original acquisition provenance
exactly with zero provider calls; all earlier preparation descriptors, including
cloud, matched unchanged.

All four supported sources are available at all **49 nodes × 36 hours**, including
9 editable and 40 context-only nodes at the unchanged 6 km spacing. At 19Z, HRRR
visibility ranges from 58,000–64,000 m inside the editable domain and 52,200–65,100 m
in context-only nodes; RAP ranges from 50,403.109375–54,703.109375 m and
45,503.109375–55,403.109375 m respectively. GFS and NBM happen to be spatially
constant over these nodes at this hour; no variation is fabricated.
Exact-point extraction equals its retained center node and direct nearest-native
sampling. Every one of the **1,764 earlier cell/hour records matches exactly**
after removing only the new visibility field/evidence, including all cloud and
winter fields. The full retained grid is **75,872,965 bytes compressed**, versus
70,369,704 bytes before visibility; building/retaining it took **291.99 seconds**.
These measurements include all prior fields and their detailed provenance.
A second complete build reproduced the exact retained grid bytes and source
provenance, reused that immutable artifact, and made zero provider calls.

Validation: **435 focused/retained offline tests passed**: 365 source, science,
preparation and application checks plus 70 existing/extended report/grid tests.
These cover native definitions, metre/mile conversion, valid ranges, zero versus
missing/unsupported values, missing neighbors, corruption, times, shared loading,
point extraction, unchanged previous fields, replay and immutable memory-storage
readback. The new visibility modules can be checked with:

```powershell
python -m pytest -q tests/unit/guidance/test_visibility.py tests/unit/forecasting/test_visibility.py tests/unit/application/test_visibility.py tests/unit/application/test_prepared_visibility.py
```

The four-file selection was included in the executed 365-test command. Ruff,
formatting, mypy, all nine import contracts, documentation/hygiene checks, offline
lock validation and `git diff --check` passed. No PostgreSQL/MinIO services, full
acceptance suite, visibility-observation verification, calibrated visibility blend,
or forecast-skill assessment were run in this increment.

The following thunder increment uses those native probabilities. Native ice evidence
and the bounded conditions preview were added later; derived accretion remains future work.

## Native thunder potential on the local grid

The optional [preparation command](src/mesoforge/application/prepared_thunder.py)
attaches native NBM `TSTM` probabilities to the existing surface preparation. It
reuses the selected NBM cycle and shared regional views; no downloads happen per
cell, per domain or inside HTTP requests. The user-approved temporary baseline is
the native **one-hour** NBM probability. Native three- and six-hour events remain
zero-weight shadows at their own endpoints; they are never split into hourly
values, summed, or substituted for missing hourly guidance.

The source retains native percent and normalized fraction, exact interval bounds,
cycle/lead, generating-process and GRIB event keys, URLs, byte ranges, checksums,
availability and acquisition times. The provider encodes thunder probability as
`TSTM`, PDT 8, probability generating-process type 5, with an interval; this is
not accumulated rainfall despite the GRIB `accum` label. Nearest-native-cell
sampling retains the published probability and sampled cell coordinates. The
configured forecast point is still extracted from the local grid's center.

**Definition limitation:** the inspected native NBM messages and current product
references do not establish a precise lightning-count/type threshold or event
neighborhood radius. Both remain explicitly unknown in the payload. Native grid
spacing is not an event radius. This baseline reports the provider-defined NBM
event, not an exact-point flash probability. Unproven event/spatial equivalence
blocks cross-source comparisons. See the official [NBM element definitions](https://vlab.noaa.gov/web/mdl/nbm-weather-elements)
and [native parameter table](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-19.shtml).

The source registry and read path retain distinct probability events rather than
requiring one permanent model. Inspection also found native GLMP lightning and
calibrated HREF thunder products, but no independently bound comparable source was
added here. Retained REFS includes a native probability of its lightning diagnostic
exceeding 0.08; its threshold units/event support are not established as equivalent
to NBM thunder, so it stays explicitly incompatible. HRRR/RAP deterministic lightning
diagnostics are not probabilities. No matching thunder probability was found in the
inspected GFS/IFS/GEFS feeds; ECMWF's separate lightning-probability chart is not an
available field in the inspected open-data ensemble probability inventory. These
are scoped product findings, not claims that the organizations have no thunder products.

```powershell
python -m mesoforge.application.prepared_thunder --prepared-run EXISTING_PREPARATION --output-dir NEW_THUNDER_PREPARATION
python -m mesoforge.application.prepared_thunder --prepared-run NEW_THUNDER_PREPARATION --output-dir OFFLINE_THUNDER_REPLAY --from-raw
python -m mesoforge.application.prepared_local_grid --config locations.json --prepared-run OFFLINE_THUNDER_REPLAY --output-dir NEW_LOCAL_GRID
```

These placeholder commands were not run verbatim; their module functions were
exercised with actual retained paths in the locked environment. All generated data
stays outside Git. The existing API can read `NEW_LOCAL_GRID`; no service was started.
Later thunder acquisition remains separate from the original forecast decision evidence.

The real demonstration uses Minneapolis **44.98859, -93.25557**, NBM cycle September
11, 2026 **18Z**, and the unchanged 36-hour grid through September 13 **06Z**. At the
point, native guidance ending September 12 **06Z / 1 a.m. CDT** is:

| Role/source event | Exact UTC event interval on September 12 | Probability |
| --- | --- | ---: |
| Temporary active NBM one-hour | 05Z–06Z | 4% |
| NBM three-hour shadow | 03Z–06Z | 18% |
| NBM six-hour shadow | 00Z–06Z | 31% |

The different periods explain why these are not a same-event disagreement or a
model ranking. No independent, scientifically comparable contributor disagreement
was demonstrated. The hourly point signal peaks at 7% for 03Z–04Z; this is a real
model thunder-potential case, not observational confirmation of a thunderstorm.
Native zero remains zero, while unsupported periods/products have explicit reasons.

The bounded acquisition retained **54 GRIB messages**: 36 hourly, 12 three-hour and
6 six-hour events. Total transfer was **38,332,503 bytes**, including inventories
from unavailable period requests; retained GRIB is **37,045,317 bytes**, retained
successful inventories **728,745 bytes**, and compressed regional arrays **459,317
bytes**. The three initial probe messages were reused. Raw replay reproduced all
arrays, events and original acquisition provenance exactly with **zero provider calls**.

All **49 cells × 36 hours** have native hourly probabilities, including 9 editable
and 40 context-only cells at unchanged 6 km spacing. At the 06Z endpoint above,
hourly probability varies from 4–5% in both domains; three-hour shadows span 17–19%
and six-hour shadows 27–33% across the full grid. Exact-point extraction equals the
retained center cell and direct nearest-native sampling. All **1,764 previous
cell/hour records match exactly** after removing only the new thunder field/evidence,
including visibility, cloud and all winter fields. The full grid occupies
**86,590,898 bytes compressed** and took **316.59 seconds** to build and retain.
A second complete build reproduced the exact compressed grid and source provenance,
reused the immutable retained artifact, and made **zero provider calls**.

Validation: **483 focused/retained offline tests passed**: 29 native adapter,
35 probability/extraction, 6 preparation, 74 report/grid and 339 retained
application/scientific checks. Focused tests cover bounds, exact event/interval and
spatial-definition preservation, zero versus missing, no longer-period fallback,
shared loading, point extraction and immutable in-memory storage/readback. The new
four-file selection can be run with:

```powershell
python -m pytest -q tests/unit/guidance/test_thunder.py tests/unit/forecasting/test_thunder.py tests/unit/application/test_thunder.py tests/unit/application/test_prepared_thunder.py
```

These files were executed in separate focused selections. Ruff, formatting, mypy,
all nine import contracts, documentation/hygiene checks, offline lock validation and
`git diff --check` passed. No PostgreSQL/MinIO service checks, full acceptance suite,
thunder-observation verification, calibration or forecast-skill assessment ran in
this increment.

Future thunder potential should be evaluated and calibrated from suitable
multi-source probabilities and matching lightning/thunder observations. CAPE,
reflectivity, QPF and deterministic convection can later supply supporting context;
they are not probabilities themselves. No thunder calibration, new meteorological
weights, derived conditions, or changes to earlier fields are introduced here.

The following increment adds that separate native ice/freezing-rain evidence.

## Native ice and freezing-rain liquid on the local grid

The optional `mesoforge.application.prepared_ice` step attaches native accumulated
amounts to the same **49-cell, 36-hour** context/editable grid. `surface.ice_guidance`
retains all contributors, intervals and descriptive disagreements. Two independent
`surface.fields` entries remain null with `policy_unavailable` and empty weights:
`flat_ice_accretion_mass_equivalent` and `freezing_rain_liquid_equivalent_amount`.
No approved retained Phase 2 ice blend or local accretion method was found.

| Source/product | Native physical meaning | Time support |
|---|---|---|
| NBM core `FICEAC` | Provider FRAM elevated flat-surface ice, published mass-equivalent encoding | Separate native 1-hour and 6-hour accumulations |
| HRRR `wrfsfc` / RAP `awp130pgrb` `FRZR` | Liquid-equivalent freezing-rain precipitation | Same-cycle cumulative parents differenced into exact hourly intervals |
| GFS / IFS inspected feeds | No supported native accumulation bound in this adapter | Explicitly unavailable; categorical freezing rain and sea ice are not substitutes |

[NOAA's native parameter table](https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_table4-2-0-1.shtml)
defines `FICEAC` (0/1/228) and `FRZR` (0/1/225) separately in kg/m². Equal units do
**not** make these the same quantity or establish a geometric ice thickness. The
adapter preserves NBM's published units even when the installed ecCodes dictionary
reports its local parameter as unknown: a strict source/parameter/grid/time binding
is required, and original decoded metadata plus the official unit reference remain
stored. [NBM's element definitions](https://vlab.noaa.gov/web/mdl/nbm-weather-elements)
and the [FRAM paper](https://repository.library.noaa.gov/view/noaa/15309) describe the
provider's flat-ice product. This code consumes that native result; it does not
implement FRAM, a density assumption, a 1:1 liquid-to-ice mapping or a new ice blend.

Extraction samples the nearest native cell with a deterministic tie break. Native
parent values, exact source-cycle/lead and accumulation bounds, URLs, hashes,
acquisition/availability times and native geometry remain traceable through the
local grid and its exact center point. Negative/nonfinite increments stay missing;
zero is retained. A missing cumulative parent makes that hourly amount unavailable.
Whole-day inventory spelling (for example `0-1 day`) is accepted only when exactly
equivalent to the requested interval. Native 6-hour NBM amounts are not spread into
hourly values. Comparisons reject unlike physical quantities/windows; they do not
establish skill. A compatible same-source interval aggregation checks contiguous
bounds and conserves amounts without changing the hourly evidence.

```powershell
python -m mesoforge.application.prepared_ice --prepared-run EXISTING_PREPARATION --output-dir NEW_ICE_PREPARATION
python -m mesoforge.application.prepared_ice --prepared-run NEW_ICE_PREPARATION --output-dir OFFLINE_ICE_REPLAY --from-raw
python -m mesoforge.application.prepared_local_grid --config LOCATIONS_JSON --prepared-run OFFLINE_ICE_REPLAY --output-dir NEW_LOCAL_GRIDS
```

All output directories must be outside Git. Preparation acquires one selected native
message per source/lead, shared by regional views and grid cells. Existing prepared
fields are referenced in place. This optional attachment does not yet add automatic
ice acquisition to `forward_run`; HTTP requests perform no downloads. Later ice
acquisition is explicitly separate from original forecast decision-time evidence.
The hourly report retains kg/m² and labels delivered ice/liquid unavailable. It does
not imply road icing, radial ice,
precipitation type, probability or a complete weather condition.

The bounded real case uses September 11, 2026 HRRR **12Z**, RAP **15Z** and NBM
**18Z**, with target 18Z and hourly valid times through September 13 06Z. Retained
surface/snowfall guidance was reused for a point near Glacier National Park
(48.88989256506466, -113.62830155754455); no new surface-model downloads were needed.
This is real forecast guidance, not observational confirmation of an icing event.

| UTC accumulation window | NBM flat-ice mass equivalent kg/m² | HRRR freezing-rain liquid kg/m² | RAP freezing-rain liquid kg/m² |
|---|---:|---:|---:|
| September 12, 08–09Z | 0.005 | 0.007584 | 0 |
| September 12, 09–10Z | 0.014 | 0.073807998 | 0 |
| September 13, 05–06Z | 0.108000003 | 0.002400011 | 0 |

These are distinct quantities and model predictions, not interchangeable estimates
of ice thickness. NBM's separate September 13 **00–06Z** flat-ice product is
0.250999987 kg/m²; it is not an hourly amount or asserted equal to summed native
hourly FRAM outputs. GFS/IFS remain unsupported. At the Glacier point all 36 hourly
NBM/HRRR/RAP values are available, with six native NBM six-hour endpoints. The
compatible HRRR hourly liquid sum is **0.41335999965667725 kg/m²**, matching the
native end-minus-start cumulative amount within 1e-12; RAP's corresponding total
is zero. All underlying values remain unrounded in storage.

Spatial variation exists in both domains. During September 12 08–09Z, HRRR liquid
spans 0–0.007584 kg/m² in the 9 editable cells and 0–0.226543994 in the 40 context-only
cells; NBM hourly flat ice spans 0–0.005 and 0–0.006 respectively. Four HRRR context
cell/hours elsewhere in the window have small negative native cumulative differences
and remain explicitly missing, without clipping. Zero native amounts are preserved.

The experiment transferred **22,215,385 bytes**, including inventories/probes and
the exact whole-day inventory recovery. It retained **116 unique selected messages**
totalling **19,907,170 GRIB bytes**, plus **1,682,319 successful inventory bytes**.
Compressed ice arrays occupy **269,918 bytes** for Minneapolis and **443,255 bytes**
for the Montana view; the latter was cropped from retained raw data with no downloads.
Raw replay reproduced arrays, native events and original acquisition provenance
exactly with **zero provider calls**. Existing preparation descriptors were unchanged.

Both full grids have 49 cells × 36 hours at unchanged 6 km spacing, with exact
point extraction equal to their center cell and direct native evidence sampling.
All **1,764 previous Minneapolis cell/hour payloads match exactly**, including
thunder and every earlier field. Its new full grid is **93,371,728 bytes compressed**
and took **352.92 seconds** to build/retain; the Glacier grid is **71,668,489 bytes**
and took **256.11 seconds**. Complete offline rebuilding reproduced both exact
compressed retained grids and their provenance with zero provider calls.

Validation: **343 focused/retained offline tests passed**: 87 new adapter/science/
preparation tests, 49 hourly-report tests, 3 selected grid/replay tests and 204 retained
application/scientific tests. Coverage includes separate physical quantities, units,
exact intervals and conservation, zero/missing, native parents, provenance, shared
loading, point extraction, unchanged prior fields and immutable in-memory readback.
The new source/science/preparation selection was executed with:

```powershell
python -m pytest -q tests/unit/guidance/test_ice.py tests/unit/forecasting/test_ice.py tests/unit/application/test_ice.py tests/unit/application/test_prepared_ice.py
```

Ruff, formatting, mypy, all nine import contracts, documentation/hygiene, offline lock
validation and `git diff --check` passed. Preparation/grid functions and CLI help were
executed; the command sequence above uses placeholders for the caller's paths.
No PostgreSQL/MinIO services, full acceptance suite, ice-observation verification,
calibration or forecast-skill assessment ran in this increment.

A future derived accretion method needs a validated meteorological algorithm and
suitable thermodynamic, precipitation-rate and wind inputs. FRAM may be evaluated
later; a surface-temperature rule or simple liquid conversion is insufficient.
The [canvas inventory and proposed condition preview](docs/rfcs/mesoforge-v2-architecture.md#67-forecast-canvas-and-deterministic-conditions)
are documented; the [read-only structured preview](#read-only-structured-condition-preview)
now implements the bounded saved-field description and initial presentation policy.
Bounded multi-hour transitions and period grouping are now implemented as described
above; other gated condition rules and derived accretion remain future work.

## References

- [VISION.md](VISION.md): canonical product direction and current/future distinction.
- [AGENTS.md](AGENTS.md): working rules and document responsibilities.
- [V2 architecture RFC](docs/rfcs/mesoforge-v2-architecture.md): technical contracts,
  implemented slices and remaining design proposals.
- [Phase 0](docs/data-contracts/phase-0.md), [Phase 1](docs/data-contracts/phase-1.md),
  [Phase 2](docs/data-contracts/phase-2.md), and [vocabulary](docs/data-contracts/vocabulary.md):
  technical references for the existing implementation. The Phase 1 lifecycle
  description is historical; its shared scientific/schema references remain applicable.
- [Modular monolith ADR](docs/decisions/0001-python-modular-monolith.md) and
  [storage ADR](docs/decisions/0004-postgresql-and-s3-storage.md): accepted foundation decisions.
- [Archive index](docs/archive/README.md): completed implementation plans, retained
  historical references, and donor identities. Historical plans are not work orders.
