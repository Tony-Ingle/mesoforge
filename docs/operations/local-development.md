# MesoForge local development environment

These are existing Phase 0–2 operational instructions. Service, installation, live,
and product-test commands were not executed during the 2026-09-09 documentation
consolidation and remain unverified on the local Windows checkout. Examples use a
POSIX shell; see [README.md](../../README.md) for Windows notes and current status.

MesoForge's integration and acceptance tests require two real backing services: PostgreSQL
(metadata/provenance) and an S3-compatible object store (MinIO).

## Option A: Docker Compose (preferred where Docker is available)

```bash
cp .env.example .env   # edit values if needed
docker compose -f deploy/local/compose.yaml up -d --wait
export MESOFORGE_DATABASE_DSN=postgresql+psycopg://mesoforge:mesoforge_local@localhost:55432/mesoforge
export MESOFORGE_TEST_DATABASE_DSN="$MESOFORGE_DATABASE_DSN"
export MESOFORGE_TEST_S3_ENDPOINT=http://127.0.0.1:59000
export MESOFORGE_TEST_S3_ACCESS_KEY=mesoforge_local
export MESOFORGE_TEST_S3_SECRET_KEY=mesoforge_local_password
uv run alembic upgrade head
uv run pytest -m integration -q
```

## Option B: without Docker

Integration tests do not require Docker to run. PostgreSQL is provided
by the `pgserver` dev dependency (a real, pip-installable, non-root
PostgreSQL 16+ binary distribution -- see
[`tests/conftest.py`](../../tests/conftest.py)); a session-scoped fixture starts and
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

[CI](../../.github/workflows/ci.yml)'s `integration` job uses a real `postgres:16`
service container and starts MinIO with `docker run`; no pgserver/standalone-binary
fallback is used there.

## Running the full quality/test suite

See [Makefile](../../Makefile) targets (`make quality`, `make test`, `make coverage`)
and the current [README command guide](../../README.md). The Phase 0 implementation
plan is archived history, not the current command reference. Integration and
acceptance fixtures reset database schemas; use a dedicated test database.

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
