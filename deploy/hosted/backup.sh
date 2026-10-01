#!/bin/sh
# Back up a running hosted stack: PostgreSQL, then every object it references, then the
# runtime volume (baselines, pointers, run records, status). Nothing is deleted.
#
# Usage: sh deploy/hosted/backup.sh DEST_DIR [--with-guidance]
#   DEST_DIR must be outside the repository. All runtime data, including prepared
#   guidance, is retained. --with-guidance remains a compatibility alias for this
#   complete default. Suspend external forecast triggers for the backup window.
set -eu
umask 077
dest=${1:?usage: backup.sh DEST_DIR [--with-guidance]}
mode=${2:-}
case "$mode" in
    ""|--with-guidance) ;;
    *) echo "Unknown backup option: $mode" >&2; exit 1 ;;
esac
[ "$#" -le 2 ] || { echo "Too many backup arguments" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd -P)
mkdir -p "$dest"
dest=$(cd "$dest" && pwd -P)
case "$dest/" in
    "$repo"/*) echo "Refusing to write backups inside the repository" >&2; exit 1 ;;
esac
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target=$(mktemp -d --suffix=.incomplete "$dest/mesoforge-$stamp-XXXXXX")
complete=${target%.incomplete}
mkdir "$target/objects"
cd "$here"

forecast_running=$(docker compose ps --all --status running -q forecast-worker)
if [ -n "$forecast_running" ]; then
    echo "Refusing to back up while a forecast worker is running; suspend its scheduler" >&2
    exit 1
fi
was_running=$(docker compose ps --all --status running -q guidance-worker)
finish_backup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ -n "$was_running" ]; then
        docker compose start guidance-worker >/dev/null || status=1
    fi
    if [ "$status" -eq 0 ]; then
        mv "$target" "$complete" || exit 1
        echo "Backup complete: $complete"
    else
        echo "Backup incomplete: $target" >&2
    fi
    exit "$status"
}
trap finish_backup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if [ -n "$was_running" ]; then
    docker compose stop guidance-worker
fi

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

echo "3/3 Complete runtime volume (guidance worker paused)"
# Every nonzero result is a failure, including changed files and a failed Docker run.
docker compose run --rm -T --no-deps --entrypoint tar admin \
    --numeric-owner -C /var/lib/mesoforge/runtime -czf - . \
    > "$target/runtime.tar.gz"

docker compose config --images > "$target/images.txt"
(cd "$target" && sha256sum postgres.dump runtime.tar.gz objects/objects-manifest.json images.txt > SHA256SUMS)
