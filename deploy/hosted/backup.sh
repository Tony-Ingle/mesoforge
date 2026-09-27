#!/bin/sh
# Back up a running hosted stack: PostgreSQL, then every object it references, then the
# runtime volume (baselines, pointers, run records, status). Nothing is deleted.
#
# Usage: sh deploy/hosted/backup.sh DEST_DIR [--with-guidance]
#   DEST_DIR must be outside the repository. Prepared guidance payloads are large and
#   reproducible from providers only while they are current, so by default only each
#   snapshot's manifests are archived (enough for retained baselines to verify);
#   --with-guidance archives the complete snapshots. Avoid the 08:00/20:00 slot
#   windows: a running forecast worker keeps writing run records.
set -eu
dest=${1:?usage: backup.sh DEST_DIR [--with-guidance]}
mode=${2:-}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
mkdir -p "$dest"
dest=$(cd "$dest" && pwd)
case "$dest/" in
    "$repo"/*) echo "Refusing to write backups inside the repository" >&2; exit 1 ;;
esac
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$dest/mesoforge-$stamp"
mkdir "$target" "$target/objects"
cd "$here"

echo "1/3 PostgreSQL (custom-format dump, verified by listing)"
docker compose exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > "$target/postgres.dump"
docker compose exec -T postgres pg_restore --list < "$target/postgres.dump" \
    > "$target/postgres.list"

# Objects after the dump: issuance writes an object before its row commits, so every
# row in the dump has its object by now.
echo "2/3 Objects referenced by the database (digest-verified)"
docker compose run --rm -T --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$target/objects:/backup" admin \
    mesoforge.application.operations export-objects --destination /backup

echo "3/3 Runtime volume (guidance worker paused)"
was_running=$(docker compose ps --status running -q guidance-worker)
restart_worker() {
    if [ -n "$was_running" ]; then
        docker compose start guidance-worker >/dev/null
    fi
}
# Signals exit through the EXIT trap, so an interrupted backup restarts the worker.
trap restart_worker EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -n "$was_running" ]; then
    docker compose stop guidance-worker
fi
if [ "$mode" = "--with-guidance" ]; then
    exclude=""
else
    exclude="--exclude=./guidance/snapshots/*/*/*"
fi
# GNU tar exits 1 when a file changed while being read; that archive is still usable.
status=0
# shellcheck disable=SC2086
docker compose run --rm -T --no-deps --entrypoint tar admin \
    --numeric-owner --warning=no-file-changed --warning=no-file-removed \
    -C /var/lib/mesoforge/runtime $exclude -czf - . \
    > "$target/runtime.tar.gz" || status=$?
if [ "$status" -gt 1 ]; then
    echo "Runtime archive failed (tar exit $status)" >&2
    exit "$status"
fi

docker compose config --images > "$target/images.txt"
echo "Backup complete: $target"
