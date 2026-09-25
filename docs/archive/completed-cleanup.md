> **HISTORICAL DOCUMENT — completed cleanup.** This records the September 2026
> retirement decisions, consumer checks and validation evidence. It is not an active
> checklist or authorization for further cleanup. Current direction, architecture
> and usage are in [VISION](../../VISION.md), [ARCHITECTURE](../../ARCHITECTURE.md)
> and [README](../../README.md). The historical contents follow.

# MesoForge cleanup and selective-rebuild checklist

**Status:** Standalone cleanup is finished and saved. Batch 2 retires Phase 1;
Batch 1 edits remain. Service-backed acceptance remains unrun. Do not search for
further removals under this completed task.

**Goal:** Make the current forecast path understandable, remove demonstrated obsolete
code, and replace only the boundaries needed by the approved V2 work. Preserve
useful science and data. Cleanup must not become another prerequisite platform.

This is a temporary maintenance list, not a second architecture or an autonomous
instruction to clean the entire repository. Follow the owner's current task and
applicable `AGENTS.md`. Use `VISION.md` for direction, `README.md` for current
capabilities, and the revised V2 RFC for design context. Proposed RFC choices do
not become approved merely by appearing here. Escalate material conflicts; do not
revive superseded Phase 3 requirements.

## Saved checkpoint and focused review

- On entry to the checkpoint task, `v2/first-forecast` was already clean at
  `94792c3ad1c2c9d71b6a0f7cc7756b30a28bbb50`. That commit contains the documentation
  consolidation, new documents, archived plans, and both cleanup batches. Its
  history is preserved rather than rewritten to split already-saved work.
- Review was limited to the accumulated changes from `8d0983f`: no accidental
  deletion, unrelated edit, or broken active reference was identified. Archived
  plans retain their original contents after the added notices. Documentation,
  hygiene, and diff-whitespace checks passed again.
- Previously reported results remain **148 retained Phase 2 tests, 197 affected
  tests, and 90 scientific tests passed**, plus nine import contracts, mypy,
  lint/format and the lock check. Those product checks were not rerun for this note.
  The 19 Phase 2 acceptance tests were collected only. Full PostgreSQL/MinIO
  acceptance and the full coverage gate were **not run**; saving these checkpoints
  does not establish that the entire application is verified.
- Earlier dated entries describe their then-uncommitted state. No further cleanup
  or feature implementation is part of this checkpoint review.

## Batch 2: 2026-09-09 — approved standalone Phase 1 retirement

- Owner-approved removal of HRRR-only hours 0–6 generation/verification, including
  hour zero. Branch/HEAD remain `v2/first-forecast` / `8d0983f`; earlier edits are
  preserved. Dated records below describe the state before this retirement.
- Deleted `application/phase1.py`, `application/phase1_adapters.py`, their two
  composition/throttling unit-test files, the Phase 1 acceptance test, and its
  exclusive `tests/fixtures/phase1_expected.json` oracle. Removed only the HRRR
  fixture-builder/transport block from `tests/support/phase1_fixture_transports.py`.
  No remaining runtime/CI consumer of the retired workflow was found.
- Removed its Make target, CI step and coordinator-specific import contract;
  updated necessary active documentation/docstrings. Archived plans and the
  retained Phase 1 contract's historical lifecycle references remain historical.
- Preserved Phase 2 HRRR support, the v1 assembler/schema and their tests,
  configuration overlay, shared scientific functions, readers and migrations.
  The four shared response/METAR/clock/sleeper fixture classes are unchanged.
  No retained assertions or expected values changed; no Phase 2 refactor.
- Locked Python 3.12 environment: **148 retained Phase 2 checks passed** (same
  selection as the baseline), **197 affected composition/wiring/shared checks
  passed**, and **90 marked scientific checks passed**. Import-linter kept all
  **9** remaining contracts; mypy, Ruff lint/format, offline lock check, docs,
  hygiene and whitespace checks passed. Independent diff review found no issues.
- **19 Phase 2 acceptance tests collected successfully, not executed.** Full
  integration/acceptance and the unchanged coverage gate still require dedicated
  PostgreSQL/MinIO services. No service startup, downloads or provider calls occurred.
- Exact pre-batch copies, this-batch-only patch and test logs are outside the repo
  in the recorded baseline directory's `phase1-retirement-1789004980788/` folder.
  Changes remain uncommitted for review; no additional cleanup batch or API work.

## Baseline validation: 2026-09-09 — before further retirement

- Rechecked `v2/first-forecast` / `8d0983f0e21977ea15da4a9f112c1266eeafea82`
  and applicable instructions. All pre-existing edits are preserved; no retirement
  was performed. Only this document changed in the repository during validation.
- Installed 82 applicable locked packages in isolated Python 3.12.14 using
  `uv sync --locked --all-groups --no-install-project --no-build --no-python-downloads`
  with explicit interpreter/environment paths. `uv pip check` passed. No global
  Python, lockfile, OS software, services, provider/model downloads, or Hermes changes.
- Ran 12 existing unit-test files covering Phase 2 configuration, canonical
  guidance, normalization, spatial extraction, availability, blends and assembly.
  `python -B -m pytest <files> -q -s -p no:cacheprovider --tb=short`:
  **148 passed on an isolated `git archive HEAD` snapshot (4.71s), and 148 passed
  on the working tree (4.62s); no failures, errors, or skips in either.** Both used
  the same dependency environment and their own source path; no failures or
  environment blockers were observed in this selection.
- Prepared-input checks separately verified 3×3 synthetic guidance extraction
  at `(45.8, -93.1)` (first lead **281 K**), weighted blending
  `280×0.6 + 290×0.4 = 284`, and assembly of seven fields over **36 hours × three
  stations**, including NaN for unavailable PoP. Assembly inputs are dummy `1.0`
  values: this is component/schema coverage, not a realistic joined forecast.
  No existing offline guidance-to-final-output test was identified. Full Phase 2
  acceptance requires PostgreSQL/MinIO; it was not started or claimed verified.
- Environment, exact commands, and logs: local directory
  `C:\Users\Tony\AppData\Local\MesoForge\baselines\20260909-8d0983f-d6c8ced2`
  (`commands.txt`, `phase2-head.log`, `phase2-working.log`, JUnit XML files).
- Proposed Phase 1 retirement remains a separate decision: remove its HRRR-only
  hours 0–6 generation/verification lifecycle (`application/phase1.py`,
  `phase1_adapters.py`), including hour zero. Its consumers are Phase 1 composition,
  throttling and acceptance tests plus Make/CI/import-linter wiring. Keep the
  Phase 2 runner, shared normalization/acquisition/verification functions, Phase 1
  config overlay, shared fixtures, and retained-data readers. Retiring the v1
  assembler additionally requires adapting its baseline-contract/matching tests.

## Batch 1: 2026-09-09 — bounded unused-definition pruning

**Approval and scope:** The owner's subsequent request authorized cleanup using
this checklist and the new vision. This batch removes only definitions with no
identified required consumer. It is optional housekeeping, not a prerequisite for
the first forecast/API milestone or approval of unresolved V2 product choices.

**Starting state:** Rechecked `v2/first-forecast` at
`8d0983f0e21977ea15da4a9f112c1266eeafea82`. The earlier documentation edits,
archive moves, and untracked files described in discovery below were already
present. Root `AGENTS.md` applies; no ancestor/nested override was found. No
instruction conflict blocks this bounded batch. Existing edits are preserved.

### Confirmed removals and consumer evidence

| Changed file | Removed definitions | Evidence and behavior preserved |
| --- | --- | --- |
| [configuration.py](../../src/mesoforge/catalog/configuration.py) | `_H01_H18`, `_H19_H36` | Private tuples appeared only at their definitions. Active horizon-band selection uses `horizon <= 18` and literal band names independently; weight validation, selection, and configuration identity are unchanged. |
| [canonical_v2.py](../../src/mesoforge/guidance/canonical_v2.py) | `_INTERVAL_WIDTH_TOLERANCE_NS` | Private zero-tolerance constant appeared only at its definition. Exact interval validation still compares directly with `_INTERVAL_WIDTH_NS`; no tolerance or scientific policy changed. |
| [hrrr_grib.py](../../tests/fixtures/hrrr_grib.py) | `make_lead_grib_bytes()`, `make_phase2_lead_grib_bytes()` | Unreferenced concatenation wrappers. Phase 1 and Phase 2 fixture transports use the retained temperature/dew-point/wind/gust/QPF message builders directly. |
| [synthetic.py](../../tests/fixtures/synthetic.py) | `build_synthetic_vertical_definition()` and its unused `VerticalDefinition` import | No caller or fixture registration. Retain `SYNTHETIC_VERTICAL_ID`, which is still used, and all active dataset/grid/variable builders. The removed import performs no required registry initialization. |
| [phase1_fixture_transports.py](../../tests/support/phase1_fixture_transports.py) | `NoOpSleeper` | No instantiation or registration. Retain `RecordingSleeper`, clocks, transport behavior, and all test assertions. |

Consumer review covered references/imports, package exports, dynamic/fixture
discovery, scripts, configuration and workflow entry points. These test modules
are helper modules, not collected test functions. No export, retained-data reader,
or migration depends on the removed symbols. An independent review of the actual
diff found only these removals and the orphaned import; surviving code is unchanged.
Unknown external users of undocumented internals/test helpers were not established.

**Replacement prerequisites:** None for these seven definitions: current callers
already use the retained implementations. This conclusion does not authorize
removing entire files, public exports, old schemas, or the Phase 1/2 pipeline.

### Checks and remaining validation

- **Passed:** existing Ruff lint and format checks on the five changed Python
  files (`python -B -m ruff check <files> --no-cache` and
  `python -B -m ruff format --check <files> --no-cache`). Python 3.12 syntax
  compilation also passed without importing modules or writing bytecode.
- **Passed:** `python -B scripts/validate_docs.py`,
  `python -B scripts/check_repository_hygiene.py`, `git diff --check`, and a
  separate whitespace check for this untracked document. Documentation validation
  checks README/docs links, not this root checklist.
- **Passed: 17 tests**, using the available Python 3.14 runtime:
  `python -B -m pytest tests/unit/test_validate_docs.py tests/unit/test_check_repository_hygiene.py tests/unit/test_phase2_repository_wiring.py -q -s -p no:cacheprovider --tb=short`.
  Initial captured invocations failed to start subprocesses on Windows
  (`WinError 6` / `WinError 50`); running in a terminal with capture disabled
  passed unchanged tests. This does not establish scientific-runtime compatibility.
- **Blocked, not passed:** attempted Python 3.12 execution of
  `tests/unit/catalog/test_phase2_configuration.py`,
  `tests/unit/guidance/test_canonical_v2.py`,
  `tests/unit/guidance/test_hrrr_decoding.py`, and
  `tests/contracts/test_canonical_dataset.py` stopped at `No module named pytest`.
  No local project environment/`uv` is available; the global Python has pytest
  but lacks required project packages. Nothing was installed.
- **Still relevant before merge:** run those focused configuration/canonical/GRIB
  checks in the supported project environment, plus
  `tests/unit/application/test_phase1_adapters_throttling.py` and
  `tests/unit/application/test_phase2_composition.py` for retained helpers.
  Required quality gates remain due. Phase 1/2 acceptance tests need dedicated
  PostgreSQL/MinIO services and were not run; no live-provider or expensive tests
  were run. Missing checks have not been waived.

**Disposition:** Changes remain uncommitted for review. No product feature,
forecast policy, dependency, workflow, service, Hermes configuration, or donor
branch changed. Hash comparison confirmed other pre-existing documents/archive
copies were preserved. The supported path below remains intact: live runner →
acquisition → normalization/extraction → blend → persisted/exported station output.
Keep its active implementations and public acquisition clock/sleeper exports;
unobserved external use is not deletion proof. No broader reorganization is
justified before the first feature. Retire or extract further code only when a
concrete replacement and its affected callers are identified.

## Discovery: 2026-09-09 — first forecast/API milestone

Historical discovery-only record; Batches 1 and 2 above record later cleanup.

**Recommendation: no prerequisite cleanup batch.** The inspected path has active
consumers, and useful point-extraction/blend functions already exist outside its
large coordinator. Build the narrow prepared-guidance forecast feature without
first retiring or reorganizing the Phase 1/2 implementation. This is a recommendation
from source inspection, not certification that every component is correct.

### Verified starting state and scope

- Branch: `v2/first-forecast`; HEAD:
  `8d0983f0e21977ea15da4a9f112c1266eeafea82`. Comparing committed trees against
  `ce0e0d77e645d31ca33caaac2d20f4f8748dc90e` lists only the V2 RFC as changed.
- Existing edits at discovery: six modified tracked documents (`AGENTS.md`,
  `README.md`, v1 architecture, Phase 3 contract, local-development guide, V2 RFC);
  three deleted old `.hermes/plans/` paths with archive copies already present;
  untracked `VISION.md`, `docs/archive/`, and this newly supplied `CLEANUP.md`.
  These are pre-existing documentation changes, not deletions performed by discovery.
- Root [AGENTS.md](../../AGENTS.md) applies. No ancestor/nested instruction file or
  `AGENTS.override.md` was found; the global Codex `AGENTS.md` is empty. Read
  [VISION.md](../../VISION.md) and [README.md](../../README.md). No instruction conflict blocks
  this discovery-only task; the checklist does not authorize execution of its batches.
- The current request identifies the first forecast/API milestone as the target.
  The entry documents still label its exact support matrix, weights, prepared-input
  format, and access boundary as proposed. This discovery does not infer approval of
  those details or expand the milestone to the whole first release.

### Confirmed supported path and consumers

| Boundary | Inspected behavior and consumers | Disposition now |
| --- | --- | --- |
| [Live runner](../../scripts/run_phase2_live.py), `main()` / `_load_configuration()` | Constructs `Phase2Request`; loads base → Phase 1 → Phase 2 YAML; composes production and replay adapters. No installed console-script entry point is declared in `pyproject.toml`. | **Keep** the working entry point and overlays. |
| [Coordinator](../../src/mesoforge/application/phase2.py), `Phase2Coordinator.run()` / `_run_pinned()` | Discovery/acquisition → normalize → align → availability → blend → identity correction → METAR acquisition/matching → verification. Called by the live runner and Phase 2 acceptance suite. | **Keep**; invoking this entire path from HTTP would bring unwanted acquisition and verification into the request. |
| [Production provider/science](../../src/mesoforge/application/phase2_production.py) | `discover()` actually acquires candidate cycles through `acquire_hrrr_phase2_lead`, `acquire_nbm_lead`, and `acquire_gfs_lead`. `_normalize()` invokes model-specific normalizers, retaining native-grid bbox/halo guidance. `_align()` matches target valid times to source leads and extracts the configured stations. `_evaluate()` / `_forecast_pair()` apply availability, configured weights, field operators, and contributor records. | **Keep**. Provider differences and artifact transactions are not demonstrated duplication. |
| [Phase 2 adapters](../../src/mesoforge/application/phase2_adapters.py) and [replay contracts](../../src/mesoforge/application/phase2_replay.py) | The live/replay builders construct `Phase2ProductionAdapters`. Coordinator, adapters, and production science consume `Phase2PersistedRun`; replay loads verified persisted roots. Acceptance tests exercise these paths. | **Keep**; forwarding methods and old names do not establish dead code. |
| [Phase 1 assembler](../../src/mesoforge/forecasting/baseline.py), `assemble_baseline_forecast()` | Called by `application/phase1_adapters.py`; also used by baseline contract and verification-matching tests. `make phase1-acceptance` remains in CI. | **Keep**; not unused. |
| [Phase 2 assembler](../../src/mesoforge/forecasting/baseline_v2.py), `assemble_baseline_forecast_v2()` | Called by production `_forecast_pair()`. Fixes KCBG/KJMR/KROS, seven variables, and hours 1–36; represents unavailable/inconsistent output as NaN with state codes. `Phase2Request` also requires hours 1–36. | **Keep** for existing callers; this is not an arbitrary-coordinate API response contract. |

Output is still an unpublished station baseline: PostgreSQL metadata and immutable
S3-compatible artifacts, then JSON/CSV/Markdown exports from verified reads. The
runner's `--replay` follows a live run; the coordinator's replay path bypasses
provider discovery/acquisition and uses retained observation responses.
`ArtifactService` is consumed by both Phase 1 and Phase 2 adapters and the runner.
`MesoForgeConfiguration._check_phase2_retains_phase1_domain()` requires Phase 1
configuration and matching domain/stations: its overlay is not obsolete merely
because the requested feature is V2.

### Proposed batch, preserved behavior, and replacement prerequisites

**First implementation-cleanup batch: none.** No inspected file is approved for
removal, and no demonstrated defect requires a refactor before the feature.

Confirmed reuse seams are [align_station_to_model()](../../src/mesoforge/alignment/station_frame.py)
and [blend_scalar()](../../src/mesoforge/forecasting/scalar_blend.py). The former accepts
latitude/longitude and a requested horizon tuple without a station ID; it uses
exact valid-time/interval matching and bilinear native-grid extraction. Missing
source hours are absent from its result; spatial extraction failures raise an error.
The latter accepts an ordered contributor tuple, rejects non-finite values/invalid
weights, and uses `math.fsum`; it does not choose the fallback policy. Existing
tests already construct temperature-only canonical guidance for hours 1–3.

For the feature, preserve exact time/source-lead relationships, canonical units,
interpolation semantics, approved contributor weights, and explicit missingness.
The new application boundary must translate missing results/errors into the agreed
response reasons, preserve source cycles and cutoffs, and avoid provider access.
Do not force this through the fixed-station assembler or relabel a coordinate as a
METAR station. Leave current station output shapes, scientific policies, immutable
artifact identities, and retained-input replay behavior intact for existing callers.

**Deferred candidate, only if implementation demonstrates a need:** extract the
smallest shared calculation currently trapped in `_forecast_pair()` / `_evaluate()`.
Prerequisite: a concrete approved field/fallback case that existing pure operators
cannot compose cleanly. Name the affected callers and an independent expected result
before editing. Do not split the whole production module or add a new artifact framework.
Any later retirement additionally requires replacement behavior, migrated callers,
and a check of retained-data readers and external users; a working new endpoint alone
does not retire the old runner, assemblers, contracts, or migrations.

### Relevant tests and remaining uncertainty

Existing test sources inspected or identified for the affected boundaries (not run):

- [Station-frame tests](../../tests/unit/alignment/test_station_frame.py): exact valid time,
  absent source hour, exact precipitation interval; plus
  [bilinear properties](../../tests/property/test_bilinear_interpolation.py).
- [Scalar-blend tests](../../tests/unit/forecasting/test_scalar_blend.py): independently
  specified weighted mean, single-source result, invalid weights/values; plus
  [scalar/vector properties](../../tests/property/test_scalar_vector_blend.py).
- [Phase 2 assembly](../../tests/unit/forecasting/test_baseline_v2.py),
  [composition](../../tests/unit/application/test_phase2_composition.py), and
  [replay contracts](../../tests/unit/application/test_phase2_replay_contracts.py): retain
  existing shapes, request restrictions, and persisted-identity behavior if touched.
- [Phase 2 acceptance](../../tests/acceptance/test_phase2_multimodel_baseline.py): fixture
  provider bytes, real PostgreSQL/MinIO, failure matrix, late-cycle cutoffs, and replay.
  This is a heavier later regression check, not part of this discovery.

A focused feature acceptance check should use tiny prepared guidance and independently
specified expected values: repeat a supported non-station coordinate request; verify
values, units, source cycles/leads, and valid times; exercise missing input/fallback
and unsupported coordinates; assert zero provider calls. This is a proposed test,
not existing HTTP coverage or authorization to implement it now.

This was a bounded local consumer trace, not an exhaustive dead-code or retention
audit. External callers, deployed data/readers, runtime/provider health, and readiness
of real prepared guidance were not established. Pure-function reuse is supported
by signatures and current tests, not a guarantee of complete V2 policy compatibility.
No product tests, dependencies, services, models, Hermes, or donor operations were run.
Only this document is updated; all earlier edits remain preserved.
Checks run: `git diff --check`, a separate `git diff --no-index --check` for this
untracked file, and the existing `python -B scripts/validate_docs.py` all passed.
The documentation validator covers README/docs links, not this root checklist;
it is not evidence of scientific or runtime correctness. Hash comparison confirmed
the other pre-existing edited/untracked files were unchanged during discovery.

## 1. Establish the actual starting point

The owner's last Git output showed `v2/first-forecast` at `8d0983f`, descended from
the recorded `main` baseline `ce0e0d7`. Its committed tree differed from that
baseline only by the V2 RFC. Documentation consolidation was uncommitted.

Recheck branch, HEAD, working-tree changes, and applicable instructions before each
batch. Do not assume those observations still describe the current checkout.

- Preserve all pre-existing edits and untracked files. Do not reset, clean, stash,
  switch branches, or overwrite them to make the workspace look clean.
- Keep the old Phase 3 donor branch, other worktrees, and evidence untouched.
  Confirm its exact revision only when selecting donor material; older RFC hashes
  may describe earlier snapshots.
- The large unmerged Phase 3 implementation was not brought into this starting
  tree. Leaving unwanted donor code behind is sufficient; do not import it merely
  to delete or refactor it.
- Work locally. This checklist does not authorize Hermes dispatch, remote service
  changes, model downloads, paid jobs, database deletion, or Git history rewriting.

## 2. Classify candidates before changing them

| Decision | Meaning |
|---|---|
| **Keep** | Required by current behavior, an approved replacement, or retained data. |
| **Simplify / replace** | Needed behavior exists, but its boundary or duplicated implementation gets in the way. Name the replacement and preserve required behavior. |
| **Retire after replacement** | Still used today. Remove only after the named replacement works and callers have moved. |
| **Remove when verified** | No remaining required consumer or retention dependency; evidence supports removal and the owner has approved the batch. |
| **Do not port / defer** | Unwanted donor machinery or future work. Keep it outside the active implementation; do not rebuild it in a new form. |

**Old, large, untested, or absent from the new vision does not mean unused.**
A `_v2` suffix may belong to the existing Phase 2 implementation; it does not prove
that the file is part of the proposed product V2.

Inspect imports and callers, but also CLI entry points, configuration-selected or
dynamic loading, registries, package exports, scripts, Make targets, workflows,
migrations, documentation consumers, and historical readers where relevant.
“No search matches” or zero coverage alone is not a deletion proof. Unknown
external consumers mean uncertainty to resolve, not a claim of no consumers.

## 3. Initial candidate list — not a deletion list

Paths below come from the provided design documents and Git listing. Confirm their
existence and current responsibilities locally. No production file below has been
independently established as dead code by this checklist.

| Area to inspect | Intended treatment and timing |
|---|---|
| `VISION.md`, `README.md`, `AGENTS.md`, active architecture/Phase 3 references, `.hermes/plans/`, `docs/archive/` | **First:** review the existing documentation cleanup, resolve authority conflicts, and verify archived plans preserve their contents. Do not overwrite the ongoing cleanup or duplicate it. |
| `scripts/run_phase2_live.py`, `configs/phase1-grasston.yaml`, `configs/phase2-grasston.yaml`, entry points in `pyproject.toml`, `Makefile` | **First:** trace the actual supported forecast path and its checks. Keep working commands until a replacement is demonstrated or their retirement is explicitly approved. |
| `src/mesoforge/application/phase2_production.py`, `phase2.py`, `phase2_adapters.py`, `phase2_replay.py` | **When needed by the first forecast slice:** reuse cohesive calculations; simplify unnecessary forwarding or extract a narrow boundary. Do not rebuild the whole coordinator or merely split it into equally coupled files. |
| `src/mesoforge/forecasting/baseline.py`, `baseline_v2.py`, fixed station/horizon assembly | **After coordinate forecasting works:** identify which legacy entry points can retire. Preserve useful operators and separately decide changes to scientific policies; do not delete every older generation together. |
| `src/mesoforge/guidance/acquisition_v2.py`, normalization paths, `src/mesoforge/application/artifacts.py` | **Only with concrete evidence:** consolidate genuinely duplicated acquisition or transaction behavior. Shared shape does not mean HRRR/NBM/GFS semantics are interchangeable. No replacement artifact framework. |
| Tests, fixtures, scripts, config entries, dependency declarations, documentation for an approved retirement | **With that retirement:** retain behavior tests, remove obsolete-only tests/helpers, and update remaining consumers. Keep historical-schema readers or migrations when retained data or supported installations still depend on them. |
| Branch-only Phase 3 lattices, report/corpus/evidence coordinators, host-certification launchers | **Do not port:** select useful scientific functions and scenarios, not whole commits or proof architecture. Do not turn their absence into cleanup work. |

Only the documentation review and forecast-path orientation are immediate. The
other rows are candidates to resolve alongside useful implementation, not a demand
to finish a repository-wide cleanup before shipping a forecast. If that path needs
no cleanup, say so rather than inventing deletion or refactoring work.

## 4. Execute one approved batch at a time

1. **Inspect the selected boundary.** Name the files/symbols, current consumers,
   reason to change, behavior that must survive, and any replacement prerequisite.
   Recommend one small batch, not a comprehensive redesign.
2. **Agree on the batch.** The owner approves its scope and acceptance check. If
   the current request already grants that approval, proceed without asking for
   permission for each file. Discovery of a different major task requires a new
   decision; optional ideas stay deferred.
3. **Implement and test that batch.** Keep unrelated edits separate. Use existing
   focused checks and small behavioral tests. Where practical, compare old and new
   implementations on retained representative inputs with independently specified
   expectations; do not assume the old code is a perfect oracle.
4. **Review the result.** Show the actual diff, remaining callers, checks run and
   results, compatibility/retention effects, and anything uncertain. Check new and
   untracked files too. Commit or push only as authorized; never auto-merge.

Run broader applicable regression checks before merge. Distinguish pre-existing
failures from regressions, but do not silently waive either. If a check cannot run,
report the limitation and obtain the needed decision rather than calling it passed.
No gateway shutdown or all-in-one resource proof is required by this checklist.

For each approved batch, add a brief entry here:

```text
Target and decision:
Consumer evidence / replacement prerequisite:
Required behavior and acceptance check:
Approval and status:
Changed paths; checks actually run; result/commit; remaining limitation:
```

**Current approved implementation-cleanup batch:** Batch 2 above, applied locally
and checked offline. Service-backed validation remains due; no further batch is
authorized by this checklist alone.

## 5. Preserve the behavior, not every historical abstraction

- Preserve model/product selection, native-grid handling, canonical units, wind
  rotation and vector blending, valid times, accumulation intervals, and applicable
  dew-point/gust/QPF/PoP rules. Policy changes are not mechanical cleanup.
- Keep provider availability, local ingestion, issue and observation times distinct;
  preserve applicable cutoff rules, explicit missingness, and contributor/fallback
  provenance. Missing is not zero.
- Keep forecast coordinates separate from observation stations. Do not improve
  apparent scores by dropping unsupported or missing opportunities.
- Preserve issued forecast versions and the promised replay capability for retained
  inputs. Do not rewrite old values, IDs, or stored schemas to resemble V2.
- Do not delete applied migrations, source evidence, databases, object-store data,
  credentials, or donor branches under a source-cleanup task. Those need a separate,
  explicit operational decision.

Tests protecting those behaviors remain useful even if their filenames mention an
old phase. Tests whose only purpose is an explicitly retired representation can
retire with it. Do not retain obsolete APIs solely to keep those tests, and do not
weaken assertions, skip tests, or remove coverage solely to obtain green results.

## 6. Prevent cleanup from becoming the next source of bloat

Prefer direct simplification and deletion over additional wrappers. Do not replace
a large coordinator with a generic workflow engine or one manifest system with
another. Keep arrays/source bytes out of HTTP-heavy work; do not introduce a
report lattice, duplicate error store, or speculative learning/AI/account subsystem
while cleaning current code.

Do not target a percentage reduction, test count, or line quota. Growth is a signal
to examine design, not permission to compress code or remove useful tests. Repeated
patches at the same boundary should prompt a simpler alternative, not an automatic
new validator.

A batch is finished when its agreed behavior works, obsolete consumers are dealt
with, relevant checks are accounted for, and the diff is understandable. Deferred
cleanup does not block an unrelated useful milestone unless it causes a concrete
failure or material risk. When the selected migration work is finished, archive
this checklist; it must not remain a permanent competing roadmap.

## Design basis

The revised [V2 RFC](mesoforge-v2-architecture-rfc.md), especially sections
12–13, proposes selective reuse, exclusion of old Phase 3 proof/report machinery,
behavior-based retirement, and small delivery increments. This checklist adds a
proposed maintenance workflow; it is not a source audit or architecture approval.
