# Historical development references

These documents preserve earlier development history. They are not instructions to
implement, resume Hermes, or expand the current V2 task. Start with
[VISION.md](../../VISION.md), [README.md](../../README.md), and
[AGENTS.md](../../AGENTS.md). The [V2 RFC](../rfcs/mesoforge-v2-architecture.md)
remains proposed design input, subject to owner approval.

## Archived plans

The completed Phase 0–2 implementation plans were moved from `.hermes/plans/` on
2026-09-09. Each retains its original contents after an added historical-status
notice. No path-specific consumers were found in code, tests, workflows, or other
tracked documents. Current code and Phase 0–2 data contracts now describe the
implemented baseline; the plans' agent instructions and delivery checklists are obsolete.

- [Phase 0 foundations](plans/2026-08-27_182932-phase-0-foundations.md)
- [Phase 1 operational slice](plans/2026-08-28_032207-phase-1-operational-forecast-slice.md)
- [Phase 2 multi-model baseline](plans/2026-08-30_233217-phase-2-multimodel-baseline.md)

## Retained in place

- [The v1 architecture/implementation plan](../architecture/v1.md): historical,
  retained because accepted ADRs and current data contracts link to it.
- [The old Phase 3 contract](../data-contracts/phase-3.md): historical technical
  reference, retained because the v1 document links to it and selective scientific
  reuse may need its original contract. It does not authorize V2 implementation.
- Phase 0–2 contracts, vocabulary, accepted ADRs, and local-development instructions:
  still needed for current code and existing checks.
- [The V2 RFC](../rfcs/mesoforge-v2-architecture.md): active proposal, not archived.
  No superseded RFC file exists in this checkout; `phase-3-debloat.md` is a
  historical donor reference, not a local file to move or recreate.

## Donor identity

The V2 working baseline inspected for this consolidation was
`8d0983f0e21977ea15da4a9f112c1266eeafea82` on `v2/first-forecast`.
The old Phase 3 implementation is preserved at
`43f56bc0c67ab782c94fb6349d65523793e1a836`, confirmed available locally.
The RFC's `17968e79749bcd958a69e3a4f0fc03eddea00efd` plus uncommitted-diff description
records an earlier donor snapshot; it is not a statement about the current working tree.
The RFC's source-baseline and design-donor hashes are historical references too.
No donor branch, commit, evidence, or runtime configuration was changed.
