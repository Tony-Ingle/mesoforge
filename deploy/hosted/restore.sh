#!/bin/sh
# Restore a backup made by backup.sh into a NEW stack whose volumes are empty.
#
# Usage: sh deploy/hosted/restore.sh BACKUP_DIR
# Validate backup contents and empty destinations before restoring application data.
# Order: PostgreSQL -> bucket and objects -> runtime volume -> migration status. Workers start
# only afterwards, by the operator, once the status is at head.
set -eu
source_dir=${1:?usage: restore.sh BACKUP_DIR}
source_dir=$(cd "$source_dir" && pwd)
cd "$(dirname "$0")"
case "$source_dir" in
    *.incomplete) echo "Refusing an incomplete backup" >&2; exit 1 ;;
esac
for file in postgres.dump runtime.tar.gz objects/objects-manifest.json SHA256SUMS; do
    [ -f "$source_dir/$file" ] || { echo "Missing $source_dir/$file" >&2; exit 1; }
done
(cd "$source_dir" && sha256sum --check --strict SHA256SUMS)
workers_running=$(docker compose ps --all --status running -q guidance-worker forecast-worker)
if [ -n "$workers_running" ]; then
    echo "Refusing to restore while a worker is running; suspend external triggers" >&2
    exit 1
fi

docker compose up -d --wait postgres minio
tables=$(docker compose exec -T postgres sh -c \
    'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = '"'"'public'"'"'"')
if [ "$tables" != "0" ]; then
    echo "Refusing to restore over a database that already has tables" >&2
    exit 1
fi
docker compose exec -T postgres pg_restore --list < "$source_dir/postgres.dump" >/dev/null
# Reject existing runtime data, including dot files, before touching the database.
docker compose run --rm -T --no-deps --entrypoint sh admin -c \
    'entries=$(find /var/lib/mesoforge/runtime -mindepth 1 -maxdepth 1 -print -quit) || exit 1; test -z "$entries" || { echo "Runtime volume is not empty" >&2; exit 1; }'
docker compose run --rm -T --no-deps --entrypoint tar admin -tzf - \
    < "$source_dir/runtime.tar.gz" >/dev/null
# Validate every exported byte before the first restored database row or object.
docker compose run --rm -T --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$source_dir/objects:/backup:ro" admin \
    mesoforge.application.operations validate-export --source /backup
docker compose run --rm -T --no-deps admin mesoforge.application.operations check-empty-storage

echo "1/3 PostgreSQL"
docker compose exec -T postgres sh -c \
    'pg_restore --exit-on-error --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$source_dir/postgres.dump"

echo "2/3 Bucket and objects"
docker compose run --rm -T --no-deps admin mesoforge.application.operations init-storage
docker compose run --rm -T --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$source_dir/objects:/backup:ro" admin \
    mesoforge.application.operations import-objects --source /backup

echo "3/3 Runtime volume"
docker compose run --rm -T --no-deps --entrypoint tar admin \
    -C /var/lib/mesoforge/runtime -xzf - < "$source_dir/runtime.tar.gz"

docker compose run --rm -T --no-deps admin mesoforge.application.operations migration-status
echo "Restored. Start the guidance worker with: docker compose up -d guidance-worker"
