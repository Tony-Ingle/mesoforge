#!/bin/sh
# Runs once, when the PostgreSQL volume is first initialized (docker-entrypoint-initdb.d).
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v worker="$MESOFORGE_PG_WORKER_USER" \
    -v worker_password="$MESOFORGE_PG_WORKER_PASSWORD" \
    -v database="$POSTGRES_DB" \
    -f /docker-entrypoint-initdb.d/worker-role.psql
