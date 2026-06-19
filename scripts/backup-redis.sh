#!/usr/bin/env bash
# Snapshot the connector's Redis volume and optionally upload off-host.
#
# Designed to run from a host cron (or systemd timer):
#
#     0 */6 * * * cd /path/to/reflow-canvas-lti && ./scripts/backup-redis.sh
#
# What it does:
#   1. Tells Redis to BGSAVE (writes a fresh ``dump.rdb`` inside the
#      ``redis-data`` volume).
#   2. Polls Redis ``LASTSAVE`` until the timestamp advances past the
#      pre-BGSAVE value, confirming the snapshot landed.
#   3. ``docker cp``s the dump file out of the container into
#      ``./backups/redis-<timestamp>.rdb`` on the host.
#   4. If ``BACKUP_S3_BUCKET`` is set, uploads the file via ``aws s3 cp``
#      (operator must have the AWS CLI installed and credentials
#      configured outside of the connector's .env).
#   5. Prunes local backups older than ``BACKUP_RETENTION_DAYS``
#      (default 14 days). S3 lifecycle policies should handle remote
#      retention.
#
# Exit codes:
#   0 - success (or non-fatal warning)
#   1 - BGSAVE never completed within the timeout
#   2 - docker cp failed
#   3 - S3 upload failed
#
# Environment overrides:
#   BACKUP_S3_BUCKET=s3://my-bucket/reflow-redis    (default: empty)
#   BACKUP_RETENTION_DAYS=14                         (default: 14)
#   BACKUP_TIMEOUT_SECONDS=300                       (default: 300 = 5 min)
#   BACKUP_DIR=./backups                             (default: ./backups)
#   REDIS_SERVICE=redis                              (default: redis)

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_TIMEOUT_SECONDS="${BACKUP_TIMEOUT_SECONDS:-300}"
REDIS_SERVICE="${REDIS_SERVICE:-redis}"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out_name="redis-${ts}.rdb"
out_path="${BACKUP_DIR}/${out_name}"

mkdir -p "$BACKUP_DIR"

container_id="$(docker compose ps -q "$REDIS_SERVICE" || true)"
if [[ -z "$container_id" ]]; then
    echo "ERROR: redis service '$REDIS_SERVICE' is not running. Start with 'docker compose up -d redis'." >&2
    exit 1
fi

before_lastsave="$(docker exec "$container_id" redis-cli LASTSAVE | tr -d '\r')"
echo "[$ts] triggering BGSAVE on $container_id (current lastsave: $before_lastsave)"

# BGSAVE returns ``Background saving started`` on accept, OR an error if
# another BGSAVE is already in progress. Either way we wait below.
bgsave_out="$(docker exec "$container_id" redis-cli BGSAVE 2>&1 || true)"
echo "  BGSAVE: $bgsave_out"

elapsed=0
while [[ "$elapsed" -lt "$BACKUP_TIMEOUT_SECONDS" ]]; do
    current="$(docker exec "$container_id" redis-cli LASTSAVE | tr -d '\r')"
    if [[ "$current" -gt "$before_lastsave" ]]; then
        echo "  BGSAVE completed at lastsave=$current (after ${elapsed}s)"
        break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
done

if [[ "$elapsed" -ge "$BACKUP_TIMEOUT_SECONDS" ]]; then
    echo "ERROR: BGSAVE did not complete within ${BACKUP_TIMEOUT_SECONDS}s" >&2
    exit 1
fi

if ! docker cp "$container_id":/data/dump.rdb "$out_path"; then
    echo "ERROR: docker cp failed for $out_path" >&2
    exit 2
fi
size="$(stat -c '%s' "$out_path" 2>/dev/null || stat -f '%z' "$out_path")"
echo "  wrote $out_path ($size bytes)"

if [[ -n "$BACKUP_S3_BUCKET" ]]; then
    if ! command -v aws >/dev/null 2>&1; then
        echo "WARNING: BACKUP_S3_BUCKET set but 'aws' CLI not on PATH; local-only backup." >&2
    else
        s3_dest="${BACKUP_S3_BUCKET%/}/${out_name}"
        echo "  uploading to $s3_dest"
        if ! aws s3 cp "$out_path" "$s3_dest"; then
            echo "ERROR: S3 upload failed" >&2
            exit 3
        fi
    fi
fi

# Prune local backups older than the retention window. S3 lifecycle policies
# handle the remote side.
pruned=0
while IFS= read -r old; do
    rm -f "$old" && pruned=$((pruned + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -name 'redis-*.rdb' -type f -mtime "+${BACKUP_RETENTION_DAYS}" 2>/dev/null || true)

if [[ "$pruned" -gt 0 ]]; then
    echo "  pruned $pruned local backup(s) older than ${BACKUP_RETENTION_DAYS} days"
fi

echo "[$ts] backup complete"
