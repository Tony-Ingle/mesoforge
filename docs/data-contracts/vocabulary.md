# MesoForge Vocabulary

This document defines terms exactly as MesoForge Phase 0 uses them. Where a
term has a common but looser meaning elsewhere in meteorology or software
engineering, the definition here is authoritative for this codebase.

## Storage and identity

**Artifact**
A registered, immutable unit of scientific data: a manifest row
(`ArtifactManifest`) in PostgreSQL referencing a specific set of
content-addressed bytes (a stored object) plus its full provenance
metadata (availability, configuration, code revision, environment,
quality state). An artifact's record identity (`ArtifactId`) is generated
independently of its content; two artifacts may reference the same
stored object.

**Object**
The raw immutable byte payload of an artifact, held in the S3-compatible
object store at a content-addressed key
(`objects/sha256/<first-2-hex>/<remaining-62-hex>`). Objects are
deduplicated by content digest; artifact records referencing them are
not.

**Manifest**
The structured, validated metadata record describing an artifact
(`ArtifactManifest`) or an activity (`ActivityManifest`) or a run
(`RunManifest`). A manifest is immutable once registered/succeeded; no
repository protocol exposes update or delete.

**Activity**
A first-class provenance node representing one execution of a named,
versioned transformation. An activity has a status (`started`,
`succeeded`, `failed`), ordered/uniquely-named input and output artifact
roles, and the configuration/code/environment identity under which it
ran. Activities — not a `parents` field on an artifact — are the unit
that explains how a derived artifact came to exist.

**Run**
A `RunManifest` records the forecast issue time, information cutoff,
configuration snapshot, code revision, environment/lockfile digests,
random seed, and the ordered set of source input artifacts selected for
one forecast cycle. A run's selected inputs are never re-resolved during
replay: replaying a run means re-executing the exact recorded activities
against the exact recorded artifact IDs.

**Content digest**
`sha256:<64 lowercase hex>` over exact serialized bytes. Determines the
object store key and enables deduplication. Distinct from record
identity and from the idempotency digest.

**Idempotency digest**
`sha256:<64 lowercase hex>` computed over an activity's type, version,
ordered role+artifact inputs, configuration digest, parameter digest,
code revision, environment digest, and output schema — not over output
bytes. Determines whether a transformation request is "the same request"
as a previously succeeded one; a repeat returns the original activity and
output rather than re-executing.

## Availability and time

**Source reference time**
The nominal time a source dataset (e.g., a guidance cycle) claims to
represent or was produced for, as asserted by the source itself. Distinct
from `forecast_issue_time` (MesoForge's own cycle) and from
`available_at` (when MesoForge could actually obtain it).

**Forecast issue time**
The instant a MesoForge run is defined to represent — the run's own
nominal cycle time. Not the same as any source's reference time; a run
may combine sources with different reference times.

**Information cutoff**
The latest instant at which source data may be considered eligible for
selection into a run. Every selected source input must have authoritative
`available_at <= information_cutoff`. Selecting an input that only became
available after the cutoff is a fail-closed error, not a warning.

**Created at**
The instant a piece of data was produced at its origin (e.g., a source
provider's own generation timestamp), as distinct from when MesoForge
learned about or ingested it.

**Available at (availability)**
The instant at which data is authoritatively considered obtainable, per a
stated `authority` and `method`. For **source** artifacts, this is
caller-supplied (the ingestion code asserts when/how/by whom the data was
established as available). For **derived** artifacts, this is *computed*
as `max(parent.available_at for all parents, activity.completed_at,
registration transaction time)` — a derived artifact can never claim to
be available before its inputs were available, before the activity that
produced it finished, or before its own registration transaction.

**Ingested at**
The instant MesoForge's own ingestion process locally obtained/staged the
data. Optional on `Availability`; distinct from `available_at` (the
authoritative claim) and from `registered_at` (the transactional
database instant).

**Registered at**
The instant the artifact's manifest row was committed to PostgreSQL,
taken from `transaction_timestamp()` inside the registering transaction
— never a caller-supplied or application-clock value. Distinct from all
of the above.

**Valid time**
For a specific lead time within a run, `valid_time = forecast_reference_time
+ lead_time`, exactly, at nanosecond resolution. Not user-overridable
except where a supplied `valid_times` array is validated to already equal
this invariant.

**Lead time**
A nonnegative `timedelta` offset from `forecast_reference_time`
identifying one point (or one interval start, for interval-valued
variables) along a `TimeAxisDefinition`. Lead times within one time axis
are strictly increasing and never repeat.

**Interval**
A `start < end` span with an explicit `IntervalClosure`
(`left_closed_right_open`, `closed`, `open`, `left_open_right_closed`).
Interval-valued temporal semantics (accumulation, average, minimum,
maximum, probability) require exactly one interval per lead time and may
never use `instantaneous` semantics.

## Spatial and scientific

**Spatial support**
How a value relates to the grid cell it is recorded against: `point`
(a value at a specific location), `cell_mean` (an area/volume average
over the cell), `cell_total` (an area/volume integral, e.g.
accumulation), or `categorical_cell` (a discrete classification covering
the cell). Declared per `GridDefinition` and per `VariableDefinition`;
mismatches between a dataset's declared support and its referenced
definitions fail contract validation.

**Quality state**
An artifact-level classification of fitness for use: `valid`, `partial`,
or `invalid`. Distinct from the per-value `quality_mask` on canonical
datasets (Phase 0 bits: `1=missing`, `2=outside_coverage`,
`4=calculated_invalid`).

## Configuration and provenance

**Configuration snapshot**
An immutable, content-addressed record (`ConfigurationSnapshot`) of a
fully validated `MesoForgeConfiguration`. Its `configuration_digest` is a
SHA-256 hash of the RFC 8785/JCS canonical JSON of the validated model —
never of the raw YAML source, so formatting/comments/key order cannot
change identity while semantic content (including list order) always
does.

**Lineage**
The directed graph of activities and the artifacts they consume
(`inputs`, by role) and produce (`outputs`, by role), reconstructible
purely from persisted PostgreSQL rows via `LineageReader.ancestors` /
`.descendants`, and exportable as a deterministic, canonically sorted
`LineageGraph` (`schema_version="lineage-graph.v1"`).

**Logical replay**
Re-executing a run's or activity's recorded transformation using the
exact recorded input artifact IDs, configuration snapshot, code
revision, and parameters, and asserting that the resulting dataset is
*logically* equal (same structure, dtypes, coordinates, values, masks)
to the original — not that the regenerated serialized bytes are
identical. MesoForge Phase 0 promises logical reproducibility and
retained-artifact byte reproducibility (the original stored bytes never
change), not cross-platform bitwise NetCDF regeneration.

**Artifact replay**
Retrieving and checksum-verifying a previously stored artifact's exact
original bytes via `get_verified`. This is a stronger, simpler guarantee
than logical replay: the bytes returned are byte-identical to what was
originally stored, forever, because objects are immutable and
content-addressed.

## Phase 1 terms

- **Station snapshot:** immutable, effective station metadata derived from retained
  AviationWeather station-response bytes before the forecast information cutoff.
- **Logical observation:** a station and observation-event-time identity shared by all
  revisions of one report.
- **Observation revision:** an append-only provider record identified by logical
  observation, provider receipt time, and canonical provider content.
- **Verification cutoff:** the UTC as-of instant limiting both provider availability and
  local ingestion during revision selection.
- **Matched pair:** one explicit station/lead coverage row, including field-specific
  matched or missing status even when no observation value is usable.
- **Calm threshold:** `1.5 m/s`; wind direction is scored only when forecast and observed
  speeds both meet or exceed it.
