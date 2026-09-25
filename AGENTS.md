# MesoForge working rules

## Read the right authority

- [VISION.md](VISION.md) is canonical product direction. Do not casually rewrite it.
- [ARCHITECTURE.md](ARCHITECTURE.md) explains current technical structure and the
  target architecture, with CURRENT and FUTURE distinguished explicitly.
- [README.md](README.md) describes current capabilities, setup and commands.
- Current code is truth for CURRENT behavior; VISION is truth for TARGET direction.
  Resolve discrepancies explicitly in architecture/usage documentation, not by
  treating proposed features as implemented or silently changing science.
- [Detailed contracts](docs/data-contracts/phase-0.md) retain their existing paths
  for shared scientific/data semantics. Read the applicable scope header; legacy
  station limits and phase plans are not current product requirements.
- [Accepted decisions](docs/decisions/0001-python-modular-monolith.md) preserve
  historical reasoning. [Archived material](docs/archive/README.md) is history,
  never an implementation directive. Archive completed/superseded proposals;
  do not let them become a second current architecture.

## Scope, development and Git

- The owner develops directly with local Codex. Hermes is paused. Do not resume
  pipelines, donor work or remote operations because an old plan requests them.
- First inspect branch, HEAD, working-tree status and applicable `AGENTS.md` /
  `AGENTS.override.md`, including nested instructions for changed files.
- Preserve unrelated edits. Honor the selected non-main branch and explicit
  commit/push limits. Do not reset donor branches or merge without authorization.
- The current request bounds the work. Future roadmap items do not expand it.
  Documentation/read-only work does not authorize runtime changes, installations,
  downloads or service startup. Human approvals govern development/releases;
  normal configured forecast operation should not need per-forecast approval.
- Keep changes reviewable. A short implementation approach and observable
  acceptance check normally suffice; do not create another plan/framework without
  a concrete need. Report serious scientific, data-loss or security defects.
- Run applicable checks; report pre-existing failures separately. Do not weaken
  checks, silently fix unrelated failures or call an unexecuted command verified.
  Required checks must pass before merge.
- Never commit credentials, GRIB/model data, caches, database/object-store contents
  or generated forecast artifacts. Destructive integration fixtures require dedicated
  test services. Keep raw evidence and runtime output outside Git.
- Check consumers and links before moving documents. Preserve unique historical
  reasoning with a status banner; use existing documentation tooling. Global
  Codex configuration is outside repository instructions.

## Forecast architecture

- **The blend is the forecast.** Models are contributors, evidence and context,
  including zero-weight shadows; do not select one model as the final forecast.
- Configured locations are the product. Latitude/longitude are the only required
  geographic inputs. Derive source coverage, local context/editable domains and
  observation candidates internally. Reuse guidance across coordinates.
- Background work produces the numerical baseline before location issuance:
  `refresh_guidance` → prepared contributor state / `latest_complete`;
  `build_baseline` → immutable baseline / `latest_baseline`.
- Normal configured jobs use `forecast_from_baseline`, pin one immutable baseline
  and extract its saved configured-domain/reference view. A coverage miss is explicit;
  it must not trigger downloads, reblending or baseline coherence in the location job.
  `forecast_from_snapshot` and older inline workflows are development/replay paths.
- `forecasting/field_blend.py` owns current temperature, dew point, vector wind,
  gust and QPF dispatch through existing policies/kernels. RH is diagnostic.
  Do not introduce a parallel numerical execution path.
- `forecasting/coherence.py` owns finite ordering of current source checks, T/Td,
  RH and wind/gust operations. Registered future relationships are dependencies,
  not approved enforcement rules. Do not invent precipitation-family constraints.
- Fixed weights, NBM active sources, HRRR/GFS p-type agreement and shadow roles
  remain active until explicitly replaced. Generalized machinery does not authorize
  new scientific policies or model promotion. Prove equivalence when replacing
  scaffolding, then remove superseded execution rather than stack another path.
- Current publication is on demand. Continuous workers, scheduling, applied site
  correction, AI editing and delivery remain future work. Schedulers decide WHEN;
  MesoForge owns meteorology. Do not put forecast science in workflow/GHA YAML.

## Scientific and verification guardrails

- Preserve native units, grids, cycles, valid times, accumulation/probability events,
  categorical meanings and provenance. Keep issue/reference/availability/ingestion
  times distinct and enforce applicable cutoffs; never fabricate historical proof.
- Missing is not zero. Keep missing, unavailable, ambiguous and not-applicable
  states explicit. Do not invent weights, renormalize or clamp outside approved rules.
- Rotate grid-relative winds before use; blend U/V rather than compass directions.
  Preserve current T/Td, Bolton RH, gust and interval-QPF behavior. Deterministic QPF
  is not PoP; surface temperature alone does not establish p-type; reduced visibility
  alone does not establish fog. SWE, snowfall amount, ground snow depth,
  freezing-rain liquid and accreted ice are distinct quantities.
- Numerical outputs must be deterministic for fixed retained inputs/configuration.
  Preserve contributor values and policy/code/config identities. Historical artifacts
  and readers must remain usable; never rewrite an earlier issuance with new guidance.
- Verify the exact issued version against a suitable observation/analysis proxy with
  matching spatial and temporal semantics. A forecast coordinate is not a gauge.
  MRMS hourly QPF uses its approved product-specific `(T-1h,T]` contract and native
  gridpoint extraction, not the temperature matcher's time tolerance.
- Opportunity, immutable fact and canonical analytical sample are different.
  Deduplicate evidence, respect reissues/revisions and compare stages/contributors
  on identical eligible samples. Sparse evidence does not justify skill claims.
- Deterministic site/regime learning precedes future AI. Keep numerical baseline,
  corrections, proposals and final fields separately traceable. Future AI edits
  MesoForge fields through bounded deterministic tools, never native contributors.
  Use finite budgets/checkpoints and preserve the last validated forecast state;
  final validation and measured improvement cannot be replaced by LLM confidence.

Recheck nested instructions when the task scope changes.
