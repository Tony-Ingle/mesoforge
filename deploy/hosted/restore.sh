#!/bin/sh
# Restore a backup made by backup.sh into a NEW stack whose volumes are empty.
#
# Usage: sh deploy/hosted/restore.sh BACKUP_DIR
# Every precondition is checked before anything is written. Order: PostgreSQL ->
# bucket and objects -> runtime volume -> migration status. The workers are started
# only afterwards, by the operator, once the status is at head.
set -eu
source_dir=${1:?usage: restore.sh BACKUP_DIR}
source_dir=$(cd "$source_dir" && pwd)
cd "$(dirname "$0")"
for file in postgres.dump runtime.tar.gz objects/objects-manifest.json; do
    [ -f "$source_dir/$file" ] || { echo "Missing $source_dir/$file" >&2; exit 1; }
done

docker compose up -d --wait postgres minio
tables=$(docker compose exec -T postgres sh -c \
    'psql -tA -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = '"'"'public'"'"'"')
if [ "$tables" != "0" ]; then
    echo "Refusing to restore over a database that already has tables" >&2
    exit 1
fi
# The export must be readable by the restore container and name this stack's bucket
# (stored rows reference their bucket by name).
docker compose run --rm -T --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
    --entrypoint python -v "$source_dir/objects:/backup:ro" admin -c \
    'import json, os, sys; m = json.load(open("/backup/objects-manifest.json")); sys.exit(0 if m["bucket"] == os.environ["MESOFORGE_S3_BUCKET"] else "The backup names bucket " + str(m["bucket"]) + ", not " + os.environ["MESOFORGE_S3_BUCKET"])'

echo "1/3 PostgreSQL"
docker compose exec -T postgres sh -c \
    'pg_restore --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
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
