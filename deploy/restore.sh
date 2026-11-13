#!/usr/bin/env bash
# Restore a backup made by backup.sh into a FRESH database, then run a smoke check.
# It never overwrites an existing database: switch the app over afterwards (RUNBOOK).
#
# Usage: restore.sh <backup>
#   <backup> is "latest", an object name (homeoa-YYYYmmddTHHMMSSZ.tar.age) in the bucket,
#   or a path to a local .tar.age file.
#
# Environment:
#   PGHOST PGPORT PGUSER PGPASSWORD   server to restore into (user needs CREATEDB)
#   RESTORE_DB                 new database name (default homeoa_restore_<stamp>)
#   BACKUP_AGE_IDENTITY_FILE   the age secret key file (kept OFF the server; see RUNBOOK)
#   RESTORE_MEDIA_DIR          where to unpack media (default: skip media)
#   RESTORE_SECRETS_FILE       write the bundled HOMEOA_FIELD_KEYS/HOMEOA_SECRET_KEY here
#   RESTORE_PRINT_SECRETS=1    or print them instead
#   BACKUP_S3_* / AWS_*        as for backup.sh, when fetching from the bucket
set -euo pipefail

SRC="${1:?usage: restore.sh latest|<object name>|<file.tar.age>}"
: "${BACKUP_AGE_IDENTITY_FILE:?set BACKUP_AGE_IDENTITY_FILE to the age identity (secret key) file}"
PREFIX="${BACKUP_S3_PREFIX:-homeoa/}"

s3() {
  if [ -n "${BACKUP_S3_ENDPOINT:-}" ]; then
    aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 "$@"
  else
    aws s3 "$@"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -f "$SRC" ]; then
  cp "$SRC" "$WORK/backup.tar.age"
else
  : "${BACKUP_S3_BUCKET:?set BACKUP_S3_BUCKET to fetch from the bucket}"
  if [ "$SRC" = "latest" ]; then
    SRC="$(s3 ls "s3://$BACKUP_S3_BUCKET/$PREFIX" | awk '{print $4}' | grep '^homeoa-.*\.tar\.age$' | sort | tail -n1)"
    [ -n "$SRC" ] || { echo "restore: no backups found in s3://$BACKUP_S3_BUCKET/$PREFIX" >&2; exit 1; }
  fi
  s3 cp --only-show-errors "s3://$BACKUP_S3_BUCKET/$PREFIX$SRC" "$WORK/backup.tar.age"
fi
echo "restore: using $SRC"

mkdir "$WORK/backup"
age -d -i "$BACKUP_AGE_IDENTITY_FILE" "$WORK/backup.tar.age" | tar -C "$WORK/backup" -xf -
(cd "$WORK/backup" && sha256sum -c --quiet SHA256SUMS)
cat "$WORK/backup/manifest.txt"

# App secrets bundled by backup.sh (HOMEOA_FIELD_KEYS, HOMEOA_SECRET_KEY).
if [ -f "$WORK/backup/secrets.env" ]; then
  if [ -n "${RESTORE_SECRETS_FILE:-}" ]; then
    (umask 077 && cp "$WORK/backup/secrets.env" "$RESTORE_SECRETS_FILE")
    echo "restore: app secrets written to $RESTORE_SECRETS_FILE (put them in prod.env)"
  elif [ "${RESTORE_PRINT_SECRETS:-0}" = "1" ]; then
    echo "restore: app secrets from the backup (put these in prod.env):"
    cat "$WORK/backup/secrets.env"
  else
    echo "restore: backup contains app secrets; set RESTORE_SECRETS_FILE or RESTORE_PRINT_SECRETS=1"
  fi
else
  echo "restore: WARNING: this backup has no secrets.env; use the field keys from the password manager"
fi

stamp="$(sed -n 's/^stamp=//p' "$WORK/backup/manifest.txt" | tr '[:upper:]' '[:lower:]')"
DB="${RESTORE_DB:-homeoa_restore_${stamp}}"
if [[ ! "$DB" =~ ^[a-z_][a-z0-9_]*$ ]]; then
  echo "restore: invalid database name $DB" >&2
  exit 1
fi
if [ "$(psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$DB'")" = "1" ]; then
  echo "restore: database $DB already exists; refusing to overwrite it" >&2
  exit 1
fi

createdb "$DB"
pg_restore --exit-on-error --no-owner --no-privileges -d "$DB" "$WORK/backup/db.dump"
echo "restore: database restored into $DB"

if [ -n "${RESTORE_MEDIA_DIR:-}" ]; then
  mkdir -p "$RESTORE_MEDIA_DIR"
  tar -C "$RESTORE_MEDIA_DIR" -xf "$WORK/backup/media.tar"
  echo "restore: media unpacked into $RESTORE_MEDIA_DIR"
fi

# Smoke check: the schema is there and every table is readable; print row counts.
tables="$(psql -d "$DB" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [ "$tables" -eq 0 ]; then
  echo "restore: smoke check failed: no tables in $DB" >&2
  exit 1
fi
psql -d "$DB" -At -F ' ' <<'SQL'
SELECT 'table ' || c.relname || ' rows=' ||
       (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I', c.relname),
                                             false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname;
SQL
echo "restore: smoke check passed ($tables tables) -> $DB"
