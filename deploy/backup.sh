#!/usr/bin/env bash
# One encrypted backup (OPS-02): pg_dump (custom format) + media tarball, packed into one
# tar, encrypted with age to the public recipient(s), uploaded to an S3-compatible bucket,
# then old backups beyond the retention window are deleted.
#
# Environment:
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE   database to dump
#   BACKUP_AGE_RECIPIENTS    one or more age public keys (age1...), space separated
#   BACKUP_S3_BUCKET         bucket name (required unless BACKUP_LOCAL_DIR only)
#   BACKUP_S3_PREFIX         key prefix (default "homeoa/")
#   BACKUP_S3_ENDPOINT       endpoint URL for non-AWS providers (MinIO, B2, Wasabi...)
#   AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION
#   BACKUP_RETENTION_DAYS    delete backups older than this many days (default 35; 0 = keep)
#   BACKUP_MEDIA_DIR         media directory to include (default /data/media)
#   BACKUP_LOCAL_DIR         optional: also keep a copy here
#   BACKUP_STATUS_DIR        optional: write last_success.json / last_failure.json here
#   HOMEOA_FIELD_KEYS HOMEOA_SECRET_KEY   bundled (encrypted) as secrets.env
set -euo pipefail

: "${BACKUP_AGE_RECIPIENTS:?set BACKUP_AGE_RECIPIENTS to the age public key(s)}"
: "${PGDATABASE:?set PGDATABASE}"
PREFIX="${BACKUP_S3_PREFIX:-homeoa/}"
RETENTION="${BACKUP_RETENTION_DAYS:-35}"
MEDIA_DIR="${BACKUP_MEDIA_DIR:-/data/media}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="homeoa-${STAMP}.tar.age"

s3() {
  if [ -n "${BACKUP_S3_ENDPOINT:-}" ]; then
    aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 "$@"
  else
    aws s3 "$@"
  fi
}

log() { printf '{"time":"%s","logger":"homeoa.backup","message":"%s"}\n' "$(date -u +%FT%TZ)" "$*"; }

STATUS_DIR="${BACKUP_STATUS_DIR:-}"
status() {  # status <success|failure> <detail>: read by the app's daily ops.backup_check job
  [ -n "$STATUS_DIR" ] || return 0
  mkdir -p "$STATUS_DIR" 2>/dev/null || true
  printf '{"time":"%s","object":"%s","detail":"%s"}\n' "$(date -u +%FT%TZ)" "$NAME" "$2" \
    >"$STATUS_DIR/last_$1.json.tmp" && mv "$STATUS_DIR/last_$1.json.tmp" "$STATUS_DIR/last_$1.json"
}

WORK="$(mktemp -d)"
finish() {
  rc=$?
  rm -rf "$WORK"
  if [ "$rc" -ne 0 ]; then status failure "exit $rc"; fi
}
trap finish EXIT
mkdir -p "$WORK/backup"

n_recipients=$(wc -w <<<"$BACKUP_AGE_RECIPIENTS")
if [ "$n_recipients" -lt 2 ]; then
  log "WARNING: only $n_recipients age recipient(s); add a second key holder (RUNBOOK: custody)"
fi

log "dumping database $PGDATABASE"
pg_dump --format=custom --no-owner --no-privileges --file "$WORK/backup/db.dump"

if [ -d "$MEDIA_DIR" ]; then
  tar -C "$MEDIA_DIR" -cf "$WORK/backup/media.tar" .
else
  tar -cf "$WORK/backup/media.tar" -T /dev/null
fi
{
  echo "stamp=$STAMP"
  echo "database=$PGDATABASE"
  echo "pg_dump=$(pg_dump --version)"
} >"$WORK/backup/manifest.txt"
# The app secrets travel inside the same age encryption, so one age secret key restores
# everything (encrypted fields need HOMEOA_FIELD_KEYS).
if [ -z "${HOMEOA_FIELD_KEYS:-}" ] || [ -z "${HOMEOA_SECRET_KEY:-}" ]; then
  log "WARNING: HOMEOA_FIELD_KEYS/HOMEOA_SECRET_KEY not set; backup will not include app secrets"
fi
(
  umask 077
  printf 'HOMEOA_FIELD_KEYS=%s\nHOMEOA_SECRET_KEY=%s\n' \
    "${HOMEOA_FIELD_KEYS:-}" "${HOMEOA_SECRET_KEY:-}" >"$WORK/backup/secrets.env"
)
(cd "$WORK/backup" && sha256sum db.dump media.tar secrets.env >SHA256SUMS)

recipients=()
for r in $BACKUP_AGE_RECIPIENTS; do recipients+=(-r "$r"); done
tar -C "$WORK/backup" -cf - . | age "${recipients[@]}" -o "$WORK/$NAME"
log "encrypted $NAME ($(wc -c <"$WORK/$NAME") bytes)"

if [ -n "${BACKUP_LOCAL_DIR:-}" ]; then
  mkdir -p "$BACKUP_LOCAL_DIR"
  cp "$WORK/$NAME" "$BACKUP_LOCAL_DIR/"
fi

if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  s3 cp --only-show-errors "$WORK/$NAME" "s3://$BACKUP_S3_BUCKET/$PREFIX$NAME"
  log "uploaded s3://$BACKUP_S3_BUCKET/$PREFIX$NAME"

  if [ "$RETENTION" -gt 0 ]; then
    cutoff="$(date -u -d "-$RETENTION days" +%Y%m%dT%H%M%SZ)"
    s3 ls "s3://$BACKUP_S3_BUCKET/$PREFIX" | awk '{print $4}' | while read -r key; do
      case "$key" in
        homeoa-*.tar.age)
          ts="${key#homeoa-}"; ts="${ts%.tar.age}"
          if [[ "$ts" < "$cutoff" ]]; then
            s3 rm --only-show-errors "s3://$BACKUP_S3_BUCKET/$PREFIX$key"
            log "pruned $key (older than $RETENTION days)"
          fi
          ;;
      esac
    done
  fi
fi
status success "ok"
log "backup complete: $NAME"
echo "$NAME"
