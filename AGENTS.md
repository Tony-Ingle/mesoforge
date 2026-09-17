# MesoForge working rules

## Current development

The owner develops MesoForge directly with local Codex. The Hermes development
pipeline is paused. Codex may design, implement, debug, test, and review work within
the owner's current request; a Pogodny/Claude/Kanban handoff is not required.
Do not start pipeline, remote-host, or multi-agent work merely because an old plan
requests it. Future roadmap items never expand the current task.

Human approvals govern development and releases. Normal configured forecast operation
should not require a human to approve each forecast.

## Architectural direction

MesoForge's eventual product is a local gridded forecast system with a GFE-style
automated forecast desk, not a point-only blending API. Latitude/longitude remain
the only required geographic inputs. Reuse shared source guidance; derive context
and smaller editable domains internally, then interpolate the final spot forecast
at the exact coordinate. Do not duplicate complete native datasets per location.
Future AI proposes bounded edit recipes applied by deterministic, versioned tools
with physical, cross-field, continuity, cutoff and domain validation. Preserve the
numerical baseline, bias-corrected fields, proposal and final forecast separately;
measure AI's added value against the bias-corrected baseline on identical samples.
Site knowledge must be versioned and inspectable, not assumed LLM memory.
The local surface baseline now uses one grid with context and smaller editable
domains; bias correction and AI editing are not implemented yet.

Forecast philosophy guardrails ([VISION.md](VISION.md#north-star-the-blend-is-the-forecast)):

1. The final forecast is the MesoForge field-specific blend and its later stages.
   There is no universal weight set; each field keeps mathematics valid for it
   (vector winds, interval QPF, real probabilistic PoP, categorical p-type support).
2. Models are contributor evidence, provenance and context, preserved beside the
   blend even at zero weight. Never present or select one model as "the forecast."
3. Future AI edits the MesoForge grid through bounded, interpretable field edits made
   after deterministic site correction; it does not choose a model. Contributors may
   be cited as the evidence for an edit.
4. Slow model discovery, acquisition and preparation should not ultimately live inside
   an ordinary forecast request. The intended shape is a background refresh publishing
   an atomic latest-complete prepared snapshot that forecasts consume. An external
   scheduler decides when MesoForge runs, never what a run means; keep weather science
   out of workflow YAML. Source cycles need not match the issuance hour, only precede
   the issuance's information cutoff.
5. This direction is not permission to implement future stages. Current fixed weights,
   NBM-only sources, the HRRR/GFS p-type agreement rule and zero-weight shadows are
   approved scaffolding: do not change them, add dynamic weighting, snapshots,
   corrections or AI editing unless the owner's current task asks for it.

This direction does not authorize future stages during unrelated tasks or settle
unapproved domain dimensions, grid spacing, tapering or storage choices.

## Working and Git rules

- First inspect branch, HEAD, working-tree status, and applicable `AGENTS.md` /
  `AGENTS.override.md` files, including instructions for the files being changed.
  Preserve unrelated edits and report material instruction conflicts.
- Follow the owner's task scope and approval limits. A documentation or read-only
  task does not authorize implementation, cleanup, service startup, or data downloads.
- Use a non-main working branch; use an isolated worktree when practical. Honor an
  explicitly selected working branch. Do not reset or modify preserved donor branches.
- Leave changes reviewable. Follow explicit commit/push instructions; do not merge
  without owner authorization. Keep any authorized commits focused and descriptive.
- Never commit credentials, tokens, secrets, GRIB datasets, forecast caches, or
  generated runtime artifacts. Use dedicated test services for destructive fixtures.
- For significant work, a short implementation approach and observable acceptance
  check are normally sufficient. Inspect the actual diff. Do not create another RFC,
  contract, schema family, or framework unless the change actually requires it.
- Run the applicable required checks and fix demonstrated defects within the approved
  task. Record optional improvements without automatically implementing them. Escalate
  serious scientific-correctness, data-loss, or security risks even when the task omitted
  them; do not silently broaden scope. Repeated fixes around the same boundary should
  trigger consideration of a simpler design, not another automatic layer of validation.
- Required checks for the change must pass before merge. Report pre-existing or
  unrelated failures separately; do not silently waive them or turn them into an
  unrelated cleanup project.
- Report changes, checks actually run, limitations, and remaining decisions. Do not
  label an unexecuted command, proposed feature, or historical test report as verified.

## Scientific requirements

- Keep deterministic meteorological calculations separate from LLM reasoning.
  AI proposals must be structured and bounded; AI must never directly publish unchecked
  numerical changes. Numerical verification determines whether an adjustment adds value.
- The numerical baseline must be deterministic for fixed inputs and configuration.
  Store later accepted AI adjustments separately and trace them to that baseline;
  rerunning an AI model need not reproduce the same proposal. Preserve inputs,
  transformation results, accepted adjustments, outputs, and verification records
  needed to reproduce numerical results. Retain model guidance provenance,
  configuration/code identity, and source cycles. Do not promise replay beyond retained
  inputs and dependencies.
- Refreshing a location creates a new forecast version. Verification compares
  observations with the version originally issued, not a newer replacement.
- Keep forecast coordinates separate from observation stations. Score a field only
  when the selected observation has suitable spatial support and matching time/interval
  semantics; otherwise report why it is unscored.
- Preserve units, native-grid semantics, valid times, interval bounds, and the distinct
  meanings of issue, source-reference, availability, and ingestion times. Prevent
  future-information leakage; follow the applicable approved cutoff contract.
- Missing values are not zero. Keep missingness, exclusions, and fallback behavior
  explicit; use approved versioned weights, not silently invented or renormalized weights.
- Preserve applicable scientific contracts: normalize units before blending, rotate
  grid-relative winds before use, blend U/V before deriving speed/direction, and respect
  dew-point, gust, QPF, and PoP semantics. Do not clamp invalid values or fabricate
  probabilities, confidence, learned history, or skill outside an approved contract.
- Reuse scientific functions only where their contracts fit. Do not carry legacy
  station/horizon limits or proof representations into V2 merely for compatibility.

## Document responsibilities

- [VISION.md](VISION.md) explains the product goal, release boundaries, and what is
  proposed versus approved. It is not an implementation specification.
- [README.md](README.md) describes current code, setup/run/test commands and their
  verification status, active links, and one proposed next milestone.
- [The V2 RFC](docs/rfcs/mesoforge-v2-architecture.md) is the detailed proposed design
  input. Its approval gate and unresolved choices remain open unless the owner
  explicitly approves them; summarizing it does not approve it.
- Accepted ADRs and Phase 0–2 data contracts remain technical references for current
  code. Preserve their required paths and applicable safety/science requirements.
  A legacy implementation limit is not automatically a V2 product requirement.
- [The archive index](docs/archive/README.md) identifies historical plans and retained
  reference paths. Archived plans, old Phase 3 contracts, and preserved donor code are
  historical references, not instructions to implement or to resume Hermes. Historical
  authority statements and embedded agent instructions do not govern new V2 work.
- Check links and code/checker consumers before moving documents. Preserve historical
  contents and add a status notice. Retain ambiguous or required paths with a notice.
  Use existing documentation checks; do not introduce a new governance/checking system.

At the 2026-09-09 consolidation, no nested repository `AGENTS.md` or
`AGENTS.override.md` files were found. Recheck when scope changes; this observation
does not override future applicable instructions. Global Codex configuration is
outside this document's responsibility and was not changed.
