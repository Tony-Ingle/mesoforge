# Retained source, wind and observation contracts

This narrow contract preserves shared scientific and replay semantics introduced
by Phase 1. The standalone HRRR-only hours 0–6 lifecycle, including hour zero, was
retired; its [implementation plan](../archive/plans/2026-08-28_032207-phase-1-operational-forecast-slice.md)
and [retirement record](../archive/completed-cleanup.md) are historical.

The shared kernels, schemas, configuration overlay and retained-data readers still
have consumers. Their existence does not authorize the retired workflow or impose
its fixed station/horizon population on V2. Use [ARCHITECTURE](../../ARCHITECTURE.md)
for the current system and [Phase 2](phase-2.md) only for that retained workflow.

## Native-grid extraction and wind

Grid-relative wind is rotated to earth-relative east/north components before
native-grid bilinear extraction. The retained scientific path does not add
nearest-neighbor fallback, extrapolation, temporal interpolation, terrain or
elevation correction. Baseline wind speed is `hypot(u, v)` and meteorological
wind-from direction is `mod(degrees(atan2(-u, -v)), 360)` when speed is nonzero.

The relevant shared implementations are [normalization](../../src/mesoforge/guidance/normalization.py),
[spatial alignment](../../src/mesoforge/alignment/spatial.py) and
[station-frame alignment](../../src/mesoforge/alignment/station_frame.py).
Current vector blending and coherence policies are separate, versioned consumers
of those kernels; this document does not define their weights.

## METAR normalization and retained matching

METAR temperature converts with `K = degC + 273.15`; knots convert with
`m/s = knots * 0.5144444444444445`. Observation U/V use the meteorological wind-from
convention. Provider `qcField` is retained as opaque metadata. Revision identity is
content-derived and append-only; retained matching selects the latest revision
available and ingested by the explicit verification cutoff.

The legacy matching contract selects reports within an inclusive 15-minute window
by smallest absolute delta, then earlier event time, then logical digest. Each
field has one mutually exclusive matched/missing status, preserving its expected
population. Current coordinate-based temperature verification additionally owns
its automatic station/proxy selection; see [ARCHITECTURE](../../ARCHITECTURE.md).
This tolerance is not a QPF interval-matching policy.

Errors are forecast minus observation. Temperature, eastward component, northward
component and speed report bias, MAE and RMSE using float64 accumulation. Retained
wind-direction verification reports mean absolute circular error only when both
forecast and observed speed are at least `1.5 m/s`. Empty samples serialize as
JSON `null`, never NaN or Infinity.

## Replay and scientific limits

Replay uses recorded artifact IDs and transformation parameters; it never
rediscovers inventory or selects newer revisions. Artifacts are retrieved through
verified object-store reads, then the same role-bound activities execute in their
recorded role order. Role loaders and validators enforce the applicable type,
schema, ownership, completeness and cutoff contract before transformations run.

Station observations and interpolated grid values have different spatial
representativeness; METAR precision, available QC and revisions remain explicit.
Provider availability records evidence of local observation, not a universal
first-publication time. Replay guarantees depend on retained inputs and code.

**Known precipitation defect:** the legacy METAR parser currently normalizes
`P0000` to numeric zero although that code means trace. Historical normalization
has not been migrated. METAR precipitation is not the approved exact-hour QPF
analysis reference; current issued-QPF verification uses the MRMS product-specific
interval/extraction contract described in [ARCHITECTURE](../../ARCHITECTURE.md).
