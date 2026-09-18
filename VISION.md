# MesoForge Vision

MesoForge is an automated digital forecast desk for configured locations.

Its purpose is to continuously ingest and prepare numerical weather guidance,
maintain the best current MesoForge numerical forecast from that guidance,
learn from verification, and eventually use a bounded AI meteorologist to
improve each configured-location forecast before issuance.

MesoForge is not a generic public weather API.

Configured latitude/longitude locations are forecast sites. Each location is
the center of a local forecast domain rather than merely a point at which model
values are averaged.

The system should increasingly behave like an automated meteorologist working
in a digital forecast editor:

- inspect multiple numerical models and ensembles;
- understand how they contributed to the baseline forecast;
- inspect the surrounding weather pattern;
- identify important forecast problems;
- make justified spatial and temporal edits;
- preserve physical and cross-field consistency;
- issue one coherent forecast;
- verify whether those decisions actually improved it.

Improvement must be measured rather than assumed.

---

## North Star

The canonical long-term MesoForge architecture is:

```text
              CONTINUOUS BACKGROUND PROCESSING

                     ┌─ HRRR
                     ├─ RAP
                     ├─ GFS
                     ├─ IFS
                     ├─ ensembles
                     ├─ other native guidance
                     └─ NBM / benchmark evidence
                         ↓
              PREPARED CONTRIBUTOR STATE
                         ↓
                FIELD-SPECIFIC BLENDS
                         ↓
               CROSS-FIELD COHERENCE
                         ↓
          LATEST MESOFORGE BASELINE SNAPSHOT
                         ↓
       refreshed whenever complete eligible guidance changes


              SCHEDULED LOCATION FORECAST

                 configured location
                         ↓
             pin latest baseline snapshot
                         ↓
       derive / extract local forecast domain
                         ↓
          deterministic site/regime correction
                         ↓
 AI sees local baseline + contributors + observations + context
                         ↓
       bounded spatial / temporal forecast edits
                         ↓
               CROSS-FIELD VALIDATION
                         ↓
                final MesoForge grid
                         ↓
          immutable issuance / presentation
                         ↓
                    verification
```

The central principle is:

> **The blend is the forecast.**

MesoForge produces one forecast.

Individual models are contributors, evidence, provenance, benchmarks and
context. They are not competing final forecasts and the system does not simply
choose a model.

---

# 1. Three Core Forecast Artifacts

The architecture distinguishes three fundamentally different artifacts.

## Prepared Contributor State

The prepared contributor state contains normalized numerical-model,
probabilistic and other meteorological evidence that MesoForge may use.

It answers:

> What guidance was available?

Examples include native fields from HRRR, RAP, GFS, IFS and ensembles, plus
benchmark or meta-model evidence such as NBM where useful.

## MesoForge Baseline Snapshot

The baseline snapshot contains MesoForge's own coherent numerical forecast,
constructed from the prepared contributor state through field-specific blend
policies and baseline cross-field coherence.

It answers:

> Given the evidence available at this time, what is MesoForge's current
> numerical forecast?

Each baseline snapshot is immutable and references the exact contributor state,
model cycles, blend-policy identities, code/configuration and provenance from
which it was created.

## Issued Forecast

The issued forecast is the location-specific product created from a pinned
baseline snapshot after applicable deterministic site/regime correction,
bounded AI forecast editing and final validation.

It answers:

> What forecast did MesoForge actually issue for this location?

These three artifacts must remain distinct.

---

# 2. Background Forecast Engine

The expensive numerical forecast work should happen before a scheduled
location forecast begins.

When complete eligible guidance changes, the background system should:

```text
model cycle/product becomes usable
        ↓
discover and validate it
        ↓
download / decode / normalize
        ↓
update prepared contributor state
        ↓
determine which blend fields are affected
        ↓
recompute affected field-specific blends
        ↓
run affected cross-field coherence
        ↓
publish a new immutable baseline snapshot
```

The previous valid contributor state and baseline snapshot remain usable until
their replacements are complete.

A partially prepared update must never replace a known-good published state.

Where practical, the system should update only fields and dependencies affected
by newly available guidance rather than rebuilding unrelated forecast fields.

"Continuous" means responding to complete, usable guidance updates. It does not
mean rebuilding the forecast every time an individual source byte or partial
file arrives.

---

# 3. Guidance Availability and Freshness

Model cycles do not need to share the same initialization time.

A current forecast may legitimately use, for example:

```text
newer RAP
older HRRR
current GFS
current IFS
latest available ensemble guidance
```

if those are the newest eligible complete inputs according to approved
policies.

The important rule is not that cycle times match.

The important rule is that every input:

* was legitimately available before the applicable information cutoff;
* passed its required completeness and identity checks;
* retains its native valid-time semantics;
* has traceable provenance.

Freshness is evidence, not an automatic winner.

A newer model run should not automatically outweigh another model that has
shown greater skill for the field, lead or situation being forecast.

---

# 4. Configured Locations Are the Product

MesoForge is built around configured forecast locations.

A configured location contains, at minimum:

```text
location identity
latitude
longitude
presentation timezone / metadata as needed
```

The production model is conceptually:

```text
scheduler
    ↓
issue forecast for configured location X
    ↓
MesoForge
```

An external scheduler such as GitHub Actions may eventually decide **when** a
forecast runs.

MesoForge decides **what** that forecast means.

Model selection, blending, meteorology, learning and forecast editing must
remain inside MesoForge rather than workflow YAML or external orchestration.

Internal development tools may accept raw latitude/longitude for testing.

That does not imply MesoForge should become a general public endpoint such as:

```text
GET /forecast?lat=random&lon=random
```

The primary product is scheduled forecasting for explicitly configured
locations.

---

# 5. Local Forecast Domains

A configured coordinate is the center of a spatial forecasting problem.

MesoForge should derive the geography needed to forecast it automatically.

Users should not have to configure:

* model grid cells;
* bounding boxes;
* counties;
* observation stations;
* context regions;
* editable polygons.

The conceptual spatial layers are:

## Source Guidance Domain

Shared numerical guidance containing enough surrounding data to construct local
forecasts.

Nearby locations should reuse prepared source guidance rather than independently
downloading the same model data.

## Context Domain

A larger region the forecast desk may inspect.

This provides information about:

* approaching systems;
* fronts;
* gradients;
* precipitation structures;
* freezing lines;
* temperature boundaries;
* model disagreement;
* surrounding observations;
* weather entering the editable domain.

## Editable Domain

A smaller bounded region in which approved forecast modifications may be made.

The AI may inspect a larger area than it is permitted to modify.

## Forecast Point

The exact configured latitude/longitude.

The delivered point forecast is derived from the final local forecast grid.

It is not independently edited as an isolated point.

Exact grid spacing, projection, context size, editable size and resolution are
implementation choices rather than permanent product assumptions.

---

# 6. The Blend Is the Forecast

MesoForge should construct its own numerical forecast from available guidance.

The end product is not:

```text
HRRR says X
GFS says Y
IFS says Z
```

followed by a model-selection decision.

Instead:

```text
contributors
      ↓
MesoForge blend
      ↓
one forecast
```

Individual contributors remain available underneath the blend as evidence.

---

# 7. Every Field Has Its Own Blend

There is no universal set of model weights.

Different forecast fields require different scientific treatment.

Fields include, among others:

* temperature;
* dew point;
* relative humidity;
* wind;
* gust;
* cloud cover;
* visibility;
* QPF;
* PoP;
* precipitation type;
* snowfall;
* SWE;
* snow ratio;
* ice;
* thunder probability.

A field-specific blend may eventually depend on evidence such as:

```text
field
forecast lead
model availability
guidance freshness
verified historical skill
site performance
weather regime
```

Blend policies must be explicit and versioned.

Long-term weighting and calibration should come from verification evidence
rather than assumptions.

---

# 8. Field Semantics Must Remain Correct

Different field types require different mathematics.

## Scalar Fields

Temperature, dew point and similar fields may use appropriate numerical
blending.

## Relative Humidity

RH should remain physically linked to temperature and dew point rather than
being treated as an unrelated forecast.

## Wind

Wind should be blended using vector components.

Direction degrees should not simply be averaged numerically.

## QPF

QPF retains exact accumulation intervals.

An hourly amount, three-hour amount and cumulative amount are not
interchangeable.

## PoP

Probability of precipitation must come from genuine probabilistic evidence and
calibration.

Deterministic QPF is not itself a probability.

## Precipitation Type

Precipitation type should eventually combine appropriate categorical or
probabilistic evidence for states such as:

```text
rain
snow
sleet
freezing rain
mixed
```

It should not permanently depend on agreement between two specific models.

## Snow and Ice

Snowfall, SWE, snow ratio, freezing-rain liquid and ice accretion are distinct
physical quantities.

They must not be silently substituted for one another.

---

# 9. NBM Is Evidence, Not the Product

NBM may remain valuable as:

* benchmark guidance;
* shadow guidance;
* comparison evidence;
* AI context;
* temporary active scaffolding;
* potentially a future weighted meta-model contributor if verification
  justifies it.

NBM should not automatically own a MesoForge field simply because an NBM
product exists.

The target architecture is for MesoForge to produce its own field-specific
forecast, including eventually its own:

* calibrated PoP;
* cloud forecast;
* thunder forecast;
* other probabilistic or categorical fields.

Because NBM itself incorporates information from other guidance, it must not be
treated as statistically independent from those same underlying contributors by
default.

---

# 10. Contributors Are Never Lost

Creating the MesoForge blend must not destroy the evidence from which it was
created.

For a historical forecast, MesoForge should ultimately be able to answer:

```text
What did HRRR say?
What did RAP say?
What did GFS say?
What did IFS say?
What did the ensembles say?
What did NBM say?

What was the original MesoForge baseline?

What did deterministic site correction change?

What did the AI change?

What was finally issued?

Which stage verified best?
```

Contributor fields, policy identities, corrections, AI edits and final states
must remain separately inspectable.

This is essential for:

* verification;
* debugging;
* scientific evaluation;
* learning;
* reproducibility.

---

# 11. Cross-Field Coherence

Field-specific blends cannot behave as unrelated forecast products.

MesoForge must ultimately produce one meteorologically coherent weather
picture.

Important relationships include:

```text
temperature ↔ dew point ↔ RH

wind ↔ gust

QPF ↔ PoP
QPF ↔ precipitation type
QPF ↔ snow / ice
QPF ↔ thunder

precipitation type ↔ thermal structure

snowfall ↔ SWE ↔ snow ratio

freezing rain ↔ thermal structure ↔ ice

visibility ↔ moisture / cloud / precipitation / fog evidence
```

Coherence matters in two places.

## Baseline Coherence

After field-specific blending, the background system reconciles applicable
cross-field relationships before publishing a baseline snapshot.

## Final Validation

After AI editing, affected dependencies are validated again before the forecast
may be issued.

A forecast should not contain contradictory fields simply because those fields
originated from different models or blend methods.

---

# 12. Deterministic Learning Comes Before AI

Persistent measurable forecast bias belongs in deterministic statistical
correction.

The intended order is:

```text
native contributors
        ↓
MesoForge baseline
        ↓
deterministic site/regime correction
        ↓
AI forecast-desk adjustment
        ↓
final forecast
        ↓
verification
```

Simple statistical errors should not require an AI to rediscover them every
forecast cycle.

Corrections must be supported by sufficient comparable verification evidence.

Sparse evidence remains insufficient evidence.

Candidate corrections should progress through a controlled lifecycle such as:

```text
historical evidence
        ↓
candidate correction
        ↓
shadow evaluation
        ↓
future verification
        ↓
versioned promotion if improvement is demonstrated
```

The uncorrected baseline remains preserved for comparison.

---

# 13. The AI Forecast Desk

The AI forecast desk behaves conceptually like a meteorologist using a digital
forecast editor.

It may inspect:

* the local MesoForge baseline;
* every relevant contributor;
* contributor disagreement;
* the larger context domain;
* observations available before the information cutoff;
* verification history;
* deterministic site/regime correction;
* structured site knowledge.

The AI does not choose one model as the forecast.

It reasons about the MesoForge forecast.

For example, if high-resolution guidance strongly supports heavier
precipitation over part of the editable domain while other guidance is lighter,
the AI may conclude:

> The MesoForge QPF baseline is underdone in this region and period.

The resulting action is conceptually:

```text
increase MesoForge QPF here
```

not:

```text
replace the forecast with HRRR
```

The same philosophy applies to every editable field.

---

# 14. Forecast Editing Is Bounded and Structured

Possible forecast operations include:

* warm or cool a region;
* increase or decrease QPF;
* reshape a precipitation feature;
* retime a feature;
* smooth an artifact;
* move a temperature gradient;
* move a rain/snow/freezing-rain boundary;
* strengthen or weaken wind;
* rotate wind coherently;
* adjust cloud extent;
* adjust probabilistic fields;
* modify snowfall or ice fields.

AI changes should preferably be represented as interpretable forecast-editing
operations rather than opaque replacement grids.

The native model data itself remains immutable evidence.

---

# 15. The AI Cannot Run Forever

The forecast desk is a bounded optimization process.

Its goal is not:

> keep modifying the forecast until the AI feels satisfied.

Its goal is:

> find the highest-value justified improvements to an already-good numerical
> forecast within a finite analysis budget.

The controller should enforce:

```text
finite task queue
finite analysis/edit passes
finite total edit budget
finite edits per field
target execution time
hard deadline
reserved final-validation time
```

An initial operational direction is approximately:

```text
target completion: ~10 minutes
hard ceiling:      ~15 minutes
```

These are starting operational targets rather than permanent scientific
constants.

The controller owns termination.

There is no unbounded autonomous forecasting loop.

---

# 16. Forecast Prioritization

The AI should not spend equal effort on every weather variable.

Before detailed forecast editing, MesoForge should assess the situation and
rank forecast problems by importance.

Priority may consider:

```text
meteorological relevance
forecast impact
contributor disagreement
baseline uncertainty
```

When precipitation is meteorologically relevant:

> **QPF receives the highest default forecast-desk priority.**

That does not mean QPF always dominates every situation.

Examples:

```text
winter storm:
QPF
→ thermal structure
→ p-type
→ snow / ice
→ wind
→ remaining fields

convective event:
QPF / convective evolution
→ thunder
→ PoP
→ wind
→ temperature / dew point

heat event:
temperature
→ dew point
→ clouds
→ wind
→ precipitation sanity check

quiet dry day:
quick precipitation review
→ temperature
→ clouds
→ wind
```

The weather determines how the remaining analysis budget is spent.

---

# 17. The AI Does Not Need to Edit Everything

Reviewing a field does not imply changing it.

The AI should frequently conclude:

```text
baseline is reasonable
no material change justified
```

Edits should have meaningful forecast value.

MesoForge should discourage meaningless oscillation such as repeatedly moving a
field by tiny amounts without new evidence.

A forecast field may be inspected and accepted unchanged.

---

# 18. One Forecast-Desk Engine

MesoForge should not implement a different AI agent for every field.

The preferred architecture is:

```text
field registry
      +
dependency graph
      +
generic forecast-desk controller
      +
small deterministic edit-tool library
      +
generic validation framework
```

The field registry describes information such as:

```text
field type
native semantics
editable/read-only status
dependencies
allowed edit operations
physical bounds
validation requirements
priority characteristics
```

Reusable deterministic operations may include concepts such as:

```text
adjust
scale
smooth
shift
retime
taper
adjust probability
move categorical boundary
```

Meteorological sophistication should come primarily from evidence and reasoning,
not thousands of bespoke control paths.

---

# 19. Selective AI Context

The AI should not receive every model value, grid cell, hour and field at once
unless necessary.

MesoForge should first generate deterministic summaries such as:

```text
QPF
  relevance: high
  disagreement: high
  timing spread: several hours
  baseline total: X

Temperature
  relevance: moderate
  largest model spread: afternoon

P-type
  meaningful contributors overwhelmingly support rain

Snow
  not applicable
```

The AI can then request detailed spatial or temporal evidence for the fields
that deserve deeper analysis.

This mirrors how a human forecaster decides which guidance deserves closer
inspection.

---

# 20. Dependency-Aware Editing

When one field changes, MesoForge should know which related fields require
review.

Conceptually:

```text
temperature
    ├─ dew point / RH
    └─ precipitation type

QPF
    ├─ PoP
    ├─ precipitation type
    ├─ snow
    ├─ ice
    └─ thunder

wind
    └─ gust
```

The system should re-evaluate affected dependencies rather than blindly
reprocessing every forecast field after every edit.

---

# 21. Preserve the Last Valid State

The AI must never leave MesoForge with a partially modified invalid forecast.

Editing should advance through validated checkpoints:

```text
validated baseline
        ↓
validated edit state 1
        ↓
validated edit state 2
        ↓
validated edit state 3
        ↓
final validation
```

If the AI:

* times out;
* crashes;
* fails;
* reaches its edit limit;
* cannot justify further changes;

MesoForge falls back to the most recent fully validated state.

If the AI produces no acceptable edits, the deterministic baseline remains a
valid forecast.

This is a fundamental reliability requirement.

---

# 22. Forecast Runs Pin Their Baseline

A scheduled location forecast pins one immutable MesoForge baseline snapshot
for the entire analysis job.

That baseline snapshot references the exact prepared contributor state from
which it was constructed, allowing the AI to inspect the underlying evidence.

If new guidance arrives while the forecast desk is working:

```text
08:00 baseline A is current
08:01 Minneapolis forecast pins baseline A

08:05 new RAP becomes complete
08:06 contributor state updates
08:07 baseline B becomes current

08:12 Minneapolis forecast finishes using baseline A

next scheduled forecast → baseline B
```

The current forecast never silently changes evidence halfway through its
analysis.

This preserves reproducibility while allowing the background forecast engine to
continue updating independently.

---

# 23. Forecast Latency Is Analysis Time, Not Download Time

MesoForge is not designed around millisecond public-API latency.

A configured forecast job may reasonably take several minutes.

A significant event may intentionally use most of a roughly 10–15 minute AI
forecast-analysis budget.

But that time should be spent on:

```text
local-domain extraction
site/regime correction
meteorological assessment
evidence inspection
forecast editing
cross-field validation
issuance
```

It should not normally be spent waiting for:

```text
model downloads
provider discovery
GRIB decoding
provider retries
construction of the basic MesoForge blend
```

The background forecast engine keeps those prerequisites ready.

---

# 24. Immutable Forecast History

Issued forecasts are historical facts.

An issuance should preserve enough information to reconstruct what happened,
including:

```text
actual issuance time
forecast reference time
baseline snapshot identity
contributor-state identity
source model cycles
field-policy identities
uncorrected baseline
deterministic correction
AI proposal/edit recipe
validation results
final forecast
relevant provenance
```

Later model runs or observations must never rewrite an existing issuance.

Verification is performed against the exact forecast version that was actually
issued.

---

# 25. Verification Is the Judge

MesoForge should never assume that:

```text
more models are better
a newer model is better
AI is better
a local correction is better
a particular edit type is better
```

Those claims require evidence.

Verification should compare, where appropriate:

```text
individual contributors
raw MesoForge baseline
bias-corrected baseline
AI-adjusted forecast
final issued forecast
```

using identical eligible samples whenever possible.

AI performance should primarily be judged against the **bias-corrected
baseline**, not merely against the original uncorrected forecast.

The AI should not receive credit for rediscovering corrections that ordinary
deterministic statistics can make.

---

# 26. Learning Has Separate Layers

MesoForge learning has three distinct meanings.

## Statistical Learning

Deterministic site/regime correction derived from verified forecast error.

## Site Knowledge

Structured, versioned and inspectable information about recurring local
behavior or weather regimes.

This must not depend on an LLM implicitly remembering previous forecasts.

## AI Performance Learning

Measurement of which forecast-editing behaviors improve forecasts and under
which situations.

These mechanisms may inform one another, but they should not collapse into one
opaque learning process.

---

# 27. Reliability and Failure Isolation

Optional evidence should not automatically prevent a forecast from being
created.

For example, unavailable or incomplete zero-weight shadow guidance may be
recorded explicitly without blocking publication if current forecast policy
does not require it.

Required inputs must still satisfy their approved completeness rules.

Missing information remains missing.

MesoForge must not silently fabricate a replacement.

One configured location failing should not prevent unrelated configured
locations from being processed.

A failed background update must not replace the previous known-good published
state.

---

# 28. Current Implementation Is Scaffolding

The current codebase contains temporary policies used to prove the end-to-end
system.

Examples include:

```text
fixed HRRR/GFS temperature weights
retained HRRR/GFS rules for several surface/QPF fields
NBM-only active PoP
NBM-only active cloud/sky
NBM-only active thunder
HRRR/GFS agreement-based precipitation type
zero-weight RAP/IFS shadow evidence
```

These remain valid current behavior until intentionally replaced.

They are not the final architecture.

New generalized systems should replace temporary implementations rather than
permanently stacking another execution layer above them.

For example:

```text
old temperature recipe
        ↓
represent same policy in generalized blend engine
        ↓
prove equivalence
        ↓
retire old execution path
```

The same principle applies throughout the project.

---

# 29. Current Foundation

MesoForge already has substantial working foundations, including:

* real model discovery and acquisition;
* prepared numerical guidance;
* reusable prepared contributor snapshots;
* native contributor evidence;
* local context/editable grids;
* current temporary blend/source policies;
* multiple forecast fields;
* immutable issuance;
* forecast readback;
* verification facts;
* canonical verification analysis;
* site-evidence governance;
* conditions;
* transitions;
* period summaries;
* multi-location operation;
* network-free forecast generation from already-prepared guidance;
* provenance and replay-oriented storage.

These are foundations.

They are not yet the complete MesoForge forecasting intelligence.

In particular, the following remain future architecture:

* continuously maintained generalized MesoForge baseline snapshots;
* generalized field-specific blending;
* generalized cross-field coherence;
* applied deterministic site/regime corrections;
* bounded AI forecast editing;
* AI performance evaluation;
* production background refresh;
* scheduled operational issuance and delivery.

---

# 30. Major Remaining Stages

The intended major progression is:

```text
fast and reusable prepared guidance
        ↓
generalized field-specific MesoForge blend engine
        ↓
continuously maintained coherent baseline snapshots
        ↓
generalized cross-field coherence
        ↓
deterministic site/regime correction
        ↓
bounded AI forecast desk
        ↓
AI verification and controlled promotion
        ↓
operational background refresh
        ↓
scheduled configured-location forecasting
        ↓
delivery / production operation
```

Implementation order may evolve where evidence requires it.

The architectural direction should remain stable.

---

# 31. Refactoring Strategy

The existing repository has accumulated implementation history while the
MesoForge vision was being discovered.

That alone is not a reason to restart the project.

Once generalized systems replace temporary scaffolding, MesoForge may undergo a
major simplification/refactor.

The goal of such a refactor should be to preserve:

* scientific behavior;
* provenance;
* reproducibility;
* forecast semantics;
* provider knowledge;
* verification guarantees;

while removing:

* obsolete compatibility paths;
* duplicate representations;
* superseded temporary policies;
* dead experiments;
* unnecessary abstractions;
* outdated tests;
* outdated documentation.

Behavioral guarantees are more important than preserving historical internal
structure.

The project should become conceptually smaller as permanent architecture
replaces scaffolding.

---

# 32. Development Principles

## Correctness Before Convenience

Units, valid times, accumulation periods, probability-event definitions,
categorical meaning and source semantics must remain explicit.

## Evidence Before Promotion

New models, weights, corrections and AI behavior require measured evaluation.

## Preserve Provenance

Forecast decisions must remain explainable after issuance.

## Prefer Generalized Mechanisms

New fields should plug into shared blend, dependency, editing and validation
frameworks rather than creating parallel systems.

## Replace Scaffolding

When a permanent mechanism replaces a prototype, prove the replacement and
retire the obsolete path.

## Bound Complexity

New architecture should strengthen the core forecast contract rather than add
layers for their own sake.

## Refactor Deliberately

Large cleanup work is appropriate once the stable scientific interfaces are
known.

---

# 33. Non-Goals

MesoForge is not intended to become:

## A Generic Public Weather API

Configured-location forecasting is the product.

## A Model-Selection Engine

The final answer is not:

```text
HRRR wins
GFS wins
IFS wins
```

The blend is the forecast.

## One Universal Weighting Formula

Different fields require different science.

## NBM With Another Name

NBM can provide valuable evidence and benchmarks, but MesoForge should build
its own forecast.

## An LLM Inventing Weather

AI does not create arbitrary forecast numbers.

It edits a rigorous numerical forecast through bounded validated tools.

## An Endlessly Self-Editing Agent

Forecast analysis has finite time, pass and edit budgets.

## Weather Logic in CI/CD

Schedulers decide when forecasts run.

MesoForge owns the meteorology.

## A System That Hides Missing Data

Unavailable, incompatible, ambiguous and not-applicable information remain
explicitly distinct.

---

# 34. Architectural Contract

At the highest level, MesoForge should remain understandable as:

```text
meteorological evidence
        ↓
MesoForge numerical forecast
        ↓
bounded forecast-desk improvement
        ↓
issued forecast
        ↓
verification
```

Everything else exists to make this process:

* scientifically sound;
* operationally reliable;
* reproducible;
* measurable;
* continuously improvable.

The project should resist complexity that does not strengthen one of those
goals.

---

# Final Principle

MesoForge is not trying to predict the weather by asking an AI to invent a
forecast.

It builds a rigorous numerical forecast first.

The background system continuously maintains the best MesoForge numerical
baseline supported by the complete eligible guidance currently available.

When a configured forecast is scheduled, that baseline is pinned.

Deterministic learning handles persistent measurable bias.

Then an AI meteorologist receives the same kind of multi-model evidence,
surrounding weather context and bounded forecast-editing authority that a
skilled human forecaster would use.

The numerical blend provides the foundation.

Deterministic learning handles repeatable error.

AI provides the opportunity to improve the forecast through meteorological
reasoning.

Validation protects the forecast.

Verification decides whether any of it actually works.
