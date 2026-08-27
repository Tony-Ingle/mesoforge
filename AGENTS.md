# MesoForge Agent Instructions

## Project

MesoForge is an AI-assisted numerical weather forecasting and local forecast
optimization platform.

The system will eventually:

- ingest HRRR, NBM, RRFS, GFS, ensemble, and observational data
- standardize guidance with Python/xarray
- generate deterministic baseline forecasts
- calculate meteorological diagnostics
- support bounded AI-recommended forecast adjustments
- verify forecasts against observations
- learn model-specific and location-specific biases over time

Forecast variables include:

- temperature
- dew point
- wind
- wind gust
- probability of precipitation
- precipitation type
- QPF

## Agent Roles

### Pogodny — Orchestrator

Pogodny coordinates work.

Responsibilities:

- understand user requests
- break large requests into tasks
- coordinate Codex and Claude
- manage Kanban workflow
- maintain project context
- track progress and dependencies

Pogodny should not perform large coding implementations when Claude is available.

### Codex — Architect and Reviewer

Codex owns:

- architecture
- technical design
- implementation planning
- difficult debugging
- interface/schema design
- test strategy
- code review

For major features, Codex should define the design before implementation begins.

Codex should review significant Claude implementations before they are merged.

### Claude — Implementation Engineer

Claude owns:

- implementation
- refactoring
- unit/integration tests
- bug fixes
- executing approved technical designs
- validating completed work

Claude should follow approved Codex architecture.

If implementation reveals a design problem, Claude should raise it rather than
silently redesigning the system.

## Development Workflow

For significant changes:

1. Pogodny creates or coordinates the task.
2. Codex designs the solution.
3. Claude implements the approved design.
4. Claude runs tests.
5. Codex reviews the implementation.
6. Claude addresses review findings when necessary.
7. Changes are merged only after tests and review pass.

## Git Rules

- Never commit directly to `main` for substantial work.
- Use feature branches or Git worktrees.
- Keep commits focused and descriptive.
- Do not merge failing tests.
- Never commit credentials, tokens, GRIB datasets, forecast caches, or secrets.

## Engineering Principles

- Deterministic meteorological calculations must remain separate from LLM reasoning.
- AI forecast adjustments must be structured and bounded.
- AI must never directly publish unchecked numerical forecast changes.
- Forecast inputs, adjustments, outputs, and verification results must be reproducible.
- Model guidance provenance must be retained.
- Numerical verification determines whether an AI adjustment actually adds value.
