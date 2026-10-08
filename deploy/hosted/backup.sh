#!/bin/sh
# Complete same-host recovery copy. Scientific history is never deleted here.
# Usage: sh deploy/hosted/backup.sh DEST_DIR [--with-guidance]
# Suspend external triggers. Daily orchestration holds the shared worker lock;
# standalone operators must quiesce writes. --with-guidance is a compatibility alias.
set -eu
umask 077
dest=${1:?usage: backup.sh DEST_DIR [--with-guidance]}
mode=${2:-}
case "$mode" in ""|--with-guidance) ;; *) echo "Unknown backup option: $mode" >&2; exit 1 ;; esac
[ "$#" -le 2 ] || { echo "Too many backup arguments" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd -P)
mkdir -p "$dest"
dest=$(cd "$dest" && pwd -P)
case "$dest/" in "$repo"/*) echo "Refusing to write backups inside the repository" >&2; exit 1 ;; esac
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target=$(mktemp -d --suffix=.incomplete "$dest/mesoforge-$stamp-XXXXXX")
complete=${target%.incomplete}
operation=$(basename "$complete")
active_pid=
was_running=
owned_names=
cd "$here"

# Background + wait is deliberate: POSIX shells handle TERM immediately while
# waiting, instead of postponing the trap until a large foreground copy finishes.
run_owned() {
    "$@" <&0 &
    active_pid=$!
    result=0
    wait "$active_pid" || result=$?
    active_pid=
    return "$result"
}
owned_container() {
    owned_names="$owned_names $1"
    container_name=$1
    shift
    run_owned docker compose run --rm -T --no-deps --name "$container_name" \
        --label "com.mesoforge.backup=$operation" "$@"
}
finish_backup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ -n "$active_pid" ]; then
        kill -TERM "$active_pid" 2>/dev/null || true
        wait "$active_pid" 2>/dev/null || true
    fi
    # Only names allocated to this invocation, never postgres/minio services,
    # another backup, a runner or an unrelated worker.
    if [ "$status" -ne 0 ]; then
        for name in $owned_names; do
            docker stop --time 10 "$name" >/dev/null 2>&1 || true
            docker rm -f "$name" >/dev/null 2>&1 || true
        done
    fi
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

forecast_running=$(docker compose ps --all --status running -q forecast-worker)
if [ -n "$forecast_running" ]; then
    echo "Refusing to back up while a forecast worker is running; suspend its scheduler" >&2
    exit 1
fi
was_running=$(docker compose ps --all --status running -q guidance-worker)
if [ -n "$was_running" ]; then
    docker compose stop guidance-worker
fi

# Size admission includes the uncompressed runtime and exported objects plus the
# database estimate. Do not count compression savings or delete anything to fit.
owned_container "$operation-estimate" --user 10001:10001 admin \
    mesoforge.application.local_backup estimate --runtime-root /var/lib/mesoforge/runtime \
    > "$target/backup-estimate.json"
"${MESOFORGE_BACKUP_PYTHON:-python3}" - "$repo/src" "$dest" "$target/backup-estimate.json" <<'PY'
import json
import shutil
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mesoforge.application.disk_admission import DiskPolicy
estimate = json.loads(Path(sys.argv[3]).read_text())
required = estimate["backup_bytes"]
if type(required) is not int or required < 0:
    raise SystemExit("Backup refused: invalid size estimate")
policy = DiskPolicy.from_environment()
free = shutil.disk_usage(Path(sys.argv[2])).free
if free - required < policy.min_free_bytes:
    raise SystemExit("Backup refused: estimated recovery copy would cross disk reserve")
PY
mkdir "$target/objects"
echo "1/3 PostgreSQL (custom-format dump, verified by listing)"
# These are disposable client containers using the service's existing environment.
# They never run the PostgreSQL server/entrypoint or stop the database service.
owned_container "$operation-dump" --entrypoint sh postgres -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -Fc -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > "$target/postgres.dump"
owned_container "$operation-list" --entrypoint pg_restore postgres --list \
    < "$target/postgres.dump" > "$target/postgres.list"

echo "2/3 Objects referenced by the database (digest-verified)"
owned_container "$operation-objects" --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$target/objects:/backup" admin \
    mesoforge.application.operations export-objects --destination /backup

echo "3/3 Complete runtime volume (guidance worker paused)"
owned_container "$operation-runtime" --user 10001:10001 --entrypoint tar admin \
    --numeric-owner -C /var/lib/mesoforge/runtime -czf - . > "$target/runtime.tar.gz"
docker compose config --images > "$target/images.txt"
if [ -n "${MESOFORGE_BACKUP_RETENTION_PLAN:-}" ]; then
    cp "$MESOFORGE_BACKUP_RETENTION_PLAN" "$target/retention-plan.json"
fi
if [ -n "${MESOFORGE_BACKUP_DAILY_RECEIPTS:-}" ]; then
    cp "$MESOFORGE_BACKUP_DAILY_RECEIPTS" "$target/daily-receipts.json"
fi
if [ -n "${MESOFORGE_BACKUP_DEPLOYMENT:-}" ]; then
    cp "$MESOFORGE_BACKUP_DEPLOYMENT" "$target/deployment.json"
fi
(cd "$target" && sha256sum postgres.dump runtime.tar.gz objects/objects-manifest.json images.txt > SHA256SUMS)
