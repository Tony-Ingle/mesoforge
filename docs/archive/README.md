# Historical MesoForge documents

This existing index is only a locator for historical material. It is not a fifth
entry point or a maintained roadmap. Current readers should start with
[README](../../README.md), [VISION](../../VISION.md) and
[ARCHITECTURE](../../ARCHITECTURE.md); coding rules are in [AGENTS](../../AGENTS.md).
Archived authority statements, phase restrictions, commands and agent instructions
record their original context and do not govern current work.

| Historical document | Why retained |
| --- | --- |
| [V2 architecture RFC](mesoforge-v2-architecture-rfc.md) | Evolution of the V2 design, scientific contracts, experiments and earlier proposals; superseded as technical authority by ARCHITECTURE. |
| [V1 architecture and implementation plan](mesoforge-v1-architecture.md) | Original architecture reasoning, reviewed phase boundaries and donor-era roadmap. |
| [Phase 3 donor contract](phase-3-contract.md) | Earlier coordinate-verification design, provenance and proof reasoning; not the current V2 contract. |
| [Completed cleanup](completed-cleanup.md) | Approved retirement boundaries, actual consumer checks and their original validation limitations. |
| [Phase 0 foundation plan](plans/2026-08-27_182932-phase-0-foundations.md) | Original foundation implementation and acceptance reasoning. |
| [Phase 1 operational-slice plan](plans/2026-08-28_032207-phase-1-operational-forecast-slice.md) | Original retired HRRR-only workflow and source/scientific design. |
| [Phase 2 multi-model plan](plans/2026-08-30_233217-phase-2-multimodel-baseline.md) | Original multi-model implementation and review history. |

The plans were moved from `.hermes/plans/` in September 2026. The V1/V2
architecture, Phase 3 contract and completed cleanup record were later consolidated
here. Their historical bodies are retained; added status banners and rebased links
do not turn the recorded proposals into current requirements. The old local-development
command guide was merged into README rather than kept as another usage manual.

## Donor identity

The original V2 working baseline was
`8d0983f0e21977ea15da4a9f112c1266eeafea82` on `v2/first-forecast`.
The preserved old Phase 3 implementation was recorded at
`43f56bc0c67ab782c94fb6349d65523793e1a836`.
The RFC's `17968e79749bcd958a69e3a4f0fc03eddea00efd` plus uncommitted-diff description
refers to an earlier donor snapshot. These are historical identities, not statements
about the current checkout or permission to reset it. No donor branch or runtime
artifact is changed by this documentation consolidation.
