# MesoForge

AI-assisted numerical weather forecasting and local forecast optimization platform.

MesoForge is being built to ingest numerical weather guidance, produce deterministic
forecast blends, support bounded AI-assisted recommendations, and verify forecast
performance. The implemented Phase 2 boundary is a deterministic, unpublished
HRRR/NBM/GFS baseline; it makes no AI, learned-bias, or forecast-skill claim.

The Phase 1 Grasston HRRR-to-METAR operational slice and its replay limitations are
documented in [docs/data-contracts/phase-1.md](docs/data-contracts/phase-1.md).
The authoritative fixed scope, equations, fallbacks, lineage, replay guarantees, and
deferrals for the Phase 2 multi-model baseline are documented in
[docs/data-contracts/phase-2.md](docs/data-contracts/phase-2.md).
The owner-gated, documentation-only Phase 3 coordinate identity, observation matching,
as-of replay, evaluation, and resource contract is documented in
[docs/data-contracts/phase-3.md](docs/data-contracts/phase-3.md); no Phase 3
implementation or forecast-skill claim exists yet.
