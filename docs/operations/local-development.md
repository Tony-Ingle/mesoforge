# MesoForge local development environment

MesoForge's integration and acceptance tests require two real backing services: PostgreSQL
(metadata/provenance) and an S3-compatible object store (MinIO).

## Option A: Docker Compose (preferred where Docker is available)

```bash
cp .env.example .env   # edit values if needed
docker compose -f deploy/local/compose.yaml up -d --wait
export MESOFORGE_DATABASE_DSN=postgresql+psycopg://mesoforge:mesoforge_local@localhost:55432/mesoforge
export MESOFORGE_TEST_S3_ENDPOINT=http://127.0.0.1:59000
export MESOFORGE_TEST_S3_ACCESS_KEY=mesoforge_local
export MESOFORGE_TEST_S3_SECRET_KEY=mesoforge_local_password
uv run alembic upgrade head
uv run pytest -m integration -q
```

## Option B: no Docker access (this development sandbox)

Integration tests do not require Docker to run. PostgreSQL is provided
by the `pgserver` dev dependency (a real, pip-installable, non-root
PostgreSQL 16+ binary distribution -- see
`tests/integration/conftest.py`); a session-scoped fixture starts and
tears it down automatically. Just running:

```bash
uv run pytest -m integration -q
```

starts real ephemeral PostgreSQL automatically. The S3-compatible
object store still needs a running MinIO (or any S3-compatible)
endpoint reachable at `MESOFORGE_TEST_S3_ENDPOINT`. The standalone MinIO
server binary can be run directly with no root/Docker requirement:

```bash
curl -sfL -o minio https://dl.min.io/server/minio/release/linux-amd64/minio
chmod +x minio
MINIO_ROOT_USER=mesoforge_local MINIO_ROOT_PASSWORD=mesoforge_local_password \
  ./minio server /tmp/mesoforge-minio-data --address :59000 --console-address :59101 &

export MESOFORGE_TEST_S3_ENDPOINT=http://127.0.0.1:59000
export MESOFORGE_TEST_S3_ACCESS_KEY=mesoforge_local
export MESOFORGE_TEST_S3_SECRET_KEY=mesoforge_local_password
uv run pytest -m integration -q
```

## CI

`.github/workflows/ci.yml`'s `integration` job uses real `postgres:16`
and `minio` service containers; no pgserver/standalone-binary fallback
is used there.

## Running the full quality/test suite

See `Makefile` targets (`make quality`, `make test`, `make coverage`) or
the exact command sequence in the Phase 0 implementation plan, Section
"Task 12: Final quality and scope audit".

## Phase 1 offline and live checks

`make phase1-acceptance` runs the PostgreSQL/MinIO-backed offline acceptance proof.
Provider access is never enabled in normal tests or CI. To validate current provider
contracts manually, supply a recent explicit cycle and opt in:

```bash
MESOFORGE_LIVE_TESTS=1 MESOFORGE_LIVE_HRRR_CYCLE=YYYYMMDDTHH make smoke-live
```

The smoke tests use one HRRR lead and one bounded three-station METAR query, assert
contract shape rather than weather values, and retain responses only in temporary test
storage. Avoid repeated invocation and respect provider rate limits.

## Phase 2 checks

The authoritative scope is the [Phase 2 data contract](../data-contracts/phase-2.md).
The default Phase 2 gate is offline:

```bash
make phase2-offline
```

It runs quality checks, unit/contract/property tests, scientific tests, and proves the
live suite skips without `MESOFORGE_LIVE_TESTS=1`. With PostgreSQL and MinIO configured
as above, run the full fixture-source workflow with:

```bash
make phase2-acceptance
```

For the same migration round trip used by CI, run `uv run alembic upgrade head`,
`uv run alembic downgrade base`, then `uv run alembic upgrade head` before acceptance.
CI uses real PostgreSQL 16 and MinIO and follows acceptance with the 90% coverage gate.

Live provider canaries remain manual and non-gating. They require
`MESOFORGE_LIVE_TESTS=1` and explicit recent cycles:

```bash
MESOFORGE_LIVE_TESTS=1 \
MESOFORGE_LIVE_HRRR_CYCLE=YYYYMMDDTHH \
MESOFORGE_LIVE_NBM_CYCLE=YYYYMMDDTHH \
MESOFORGE_LIVE_GFS_CYCLE=YYYYMMDDTHH \
uv run pytest -m live tests/live -q
```

They prove only narrow response contract shape, not forecast skill, operational
availability, or publication readiness.
