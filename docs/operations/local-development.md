# MesoForge local development environment

MesoForge's Phase 0 tests require two real backing services: PostgreSQL
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
