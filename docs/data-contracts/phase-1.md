# Phase 1 operational forecast slice

> **Status (2026-09-09):** The standalone hours 0–6 generation/verification workflow
> is retired. Its lifecycle description below is historical. Shared scientific
> contracts, schemas, configuration, and retained-data readers remain in use;
> this path is retained for their references. See [README](../../README.md) and
> [Phase 2](phase-2.md) for the supported workflow.

Phase 1 is one deterministic, replayable HRRR forecast and METAR verification run for
`grasston-minnesota.v1`. The authoritative configuration is
`configs/phase1-grasston.yaml`; this document summarizes its durable contracts rather
than replacing it.

## Lifecycle and lineage

The application request pins an hourly HRRR cycle, forecast issue time, information
cutoff, verification cutoff, configuration/code/environment/lockfile digests, and a
typed run ID. The coordinator invokes injected acquisition and transformation ports;
provider selectors, decoding, wind rotation, interpolation, normalization, matching,
and metric formulas remain outside `application/phase1.py`.

The selected run inputs are ordered `index-f00`, `grib-f00`, through `index-f06`,
`grib-f06`, followed by the pre-run station-catalog snapshot. The snapshot is derived
from the retained AviationWeather station response, so lineage still reaches that
source. METAR truth arrives after forecast issuance and is linked through downstream
activities rather than retroactively becoming a selected run input.

```text
7 index + 7 selected-GRIB roots -> acquisition + variable lineage -> canonical guidance
station response -> station snapshot -------------------------------> point extraction
canonical guidance + station snapshot + extraction report ----------> baseline
METAR response(s) + station snapshot -------------------------------> observations
baseline + observations --------------------------------------------> matched pairs
matched pairs -------------------------------------------------------> verification report
```

Every role-bound transformation supplies a loader and a callable validator for every
declared input role. Role keys must match exactly. Artifact role/type, source-versus-
derived status, validity, run ownership, seven-lead completeness, distinct source IDs,
and cutoff eligibility fail closed before later stages run. Repeating identical source
or transformation identities reuses the existing artifact/activity.

## Scientific contracts

The retained fields are 2 m temperature and 10 m U/V wind for leads 0–6. Grid-relative
wind is rotated to earth-relative east/north components before native-grid bilinear
station interpolation. No nearest-neighbor fallback, extrapolation, temporal
interpolation, terrain correction, or elevation correction is applied. Baseline wind
speed is `hypot(u, v)` and meteorological wind-from direction is
`mod(degrees(atan2(-u, -v)), 360)` when speed is nonzero.

METAR temperature converts with `K = degC + 273.15`; knots convert with
`m/s = knots * 0.5144444444444445`. Observation U/V use the meteorological wind-from
convention. Provider `qcField` is retained as opaque metadata. Revision identity is
content-derived and append-only; matching selects the latest revision available and
ingested by the explicit verification cutoff.

Matching emits all 21 station/lead rows. Reports within an inclusive 15-minute window
are selected by smallest absolute delta, then earlier event time, then logical digest.
Each field has one mutually exclusive status, so matched plus missing counts always
equal the expected population.

Errors are forecast minus observation. Temperature, eastward component, northward
component, and speed report bias, MAE, and RMSE using float64 accumulation. Direction
reports mean absolute circular error only when forecast and observed speed are both at
least `1.5 m/s`. Empty samples serialize as JSON `null`, never NaN or Infinity.

## Replay and limitations

Replay uses recorded artifact IDs and transformation parameters; it never rediscovers
inventory or selects newer revisions. Retrieve each artifact through verified object-
store reads, then execute the same role-bound activities in recorded role order.

This slice proves pipeline behavior, not forecast skill or publication readiness.
Station observations and interpolated grid values have different representativeness;
station elevation is ignored; METAR precision and provider QC semantics are limited;
provider availability records first successful local observation rather than universal
first publication. Phase 1 has no AI adjustment, bias learning, fallback cycle,
publication, or uncertainty interval.

## Provider smoke tests

Normal CI is offline. Manual live checks validate only current provider contracts and
write nothing outside pytest temporary directories:

```bash
MESOFORGE_LIVE_TESTS=1 \
MESOFORGE_LIVE_HRRR_CYCLE=YYYYMMDDTHH \
uv run pytest -m live tests/live -q
```

Use a recent explicit HRRR cycle. These calls obey the configured timeouts and bounded
query sizes; do not loop the command or exceed AviationWeather's documented rate limit.
