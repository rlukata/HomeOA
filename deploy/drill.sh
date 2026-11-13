#!/usr/bin/env bash
# Single-quoted scripts run inside the container and expand there (SC2016).
# shellcheck disable=SC2016
# Backup -> restore round-trip drill against MinIO (OPS-02). Migrates the real schema,
# seeds ledger-like rows, an encrypted value and a media file, runs backup.sh (two age
# recipients, app secrets bundled, upload, retention, status file), restores the latest
# backup into a fresh database with the SECOND holder's key, and checks that rows, money
# totals, media and the app secrets survive, and that the encrypted value decrypts with
# the keys recovered from the backup. Uses deploy/compose.test.yaml with throwaway
# credentials; tears everything down after.
#
# Usage: deploy/drill.sh            (exit 0 = drill passed)
set -euo pipefail
cd "$(dirname "$0")"

DC=(docker compose -f compose.test.yaml)
cleanup() { "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

run() { "${DC[@]}" run --rm -T tool "$1"; }

# Fresh throwaway app secrets: the restore must recover them from the backup itself.
FIELD_KEY="$(openssl rand 32 | base64 | tr '+/' '-_')"
SECRET_KEY="drill-$(openssl rand -hex 32)"

echo "==> starting Postgres + MinIO"
"${DC[@]}" up -d --wait db minio
"${DC[@]}" build -q tool app

echo "==> migrate the full HomeOA schema (app image) and store an encrypted value"
"${DC[@]}" run --rm -T -e HOMEOA_FIELD_KEYS="$FIELD_KEY" app migrate
"${DC[@]}" run --rm -T -e HOMEOA_FIELD_KEYS="$FIELD_KEY" app python manage.py shell -c "
from django.db import connection
from homeoa.core.crypto import encrypt
with connection.cursor() as c:
    c.execute('CREATE TABLE drill_secret (v text)')
    c.execute('INSERT INTO drill_secret VALUES (%s)', [encrypt('drill-plaintext-42')])
"

echo "==> bucket, two age key holders, seed data"
run '
set -euo pipefail
aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 mb "s3://$BACKUP_S3_BUCKET" >/dev/null
age-keygen -o /keys/age.key 2>/dev/null     # holder 1 (e.g. treasurer)
age-keygen -o /keys/age2.key 2>/dev/null    # holder 2 (e.g. president)
psql -q -v ON_ERROR_STOP=1 <<SQL
CREATE TABLE drill_ledger (id serial PRIMARY KEY, amount numeric(12,2) NOT NULL, memo text);
INSERT INTO drill_ledger (amount, memo)
  SELECT round((random() * 1000)::numeric, 2), md5(g::text) FROM generate_series(1, 2000) g;
SQL
mkdir -p /data/media/statements
head -c 200000 /dev/urandom > /data/media/statements/2026-10.pdf
# An expired backup that retention must prune.
echo old > /tmp/old && aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 cp --only-show-errors /tmp/old \
  "s3://$BACKUP_S3_BUCKET/${BACKUP_S3_PREFIX}homeoa-20000101T000000Z.tar.age"
'

echo "==> backup"
"${DC[@]}" run --rm -T -e HOMEOA_FIELD_KEYS="$FIELD_KEY" -e HOMEOA_SECRET_KEY="$SECRET_KEY" tool '
set -euo pipefail
export BACKUP_AGE_RECIPIENTS="$(age-keygen -y /keys/age.key) $(age-keygen -y /keys/age2.key)"
backup.sh
test -s /status/last_success.json || { echo "DRILL FAILED: no last_success.json" >&2; exit 1; }
cat /status/last_success.json
'

echo "==> restore latest into a fresh database and compare"
run '
set -euo pipefail
# Restored with the SECOND holder key: either key holder can restore alone.
BACKUP_AGE_IDENTITY_FILE=/keys/age2.key RESTORE_DB=homeoa_restored \
  RESTORE_MEDIA_DIR=/restored/media RESTORE_SECRETS_FILE=/restored/secrets.env restore.sh latest
q="SELECT count(*), sum(amount), md5(string_agg(concat_ws(chr(58), id, amount, memo), chr(44) ORDER BY id)) FROM drill_ledger"
src="$(psql -Atc "$q")"
dst="$(psql -d homeoa_restored -Atc "$q")"
echo "source:   $src"
echo "restored: $dst"
[ "$src" = "$dst" ] || { echo "DRILL FAILED: data differs" >&2; exit 1; }
q2="SELECT count(*) FROM django_migrations"
[ "$(psql -Atc "$q2")" = "$(psql -d homeoa_restored -Atc "$q2")" ] \
  || { echo "DRILL FAILED: HomeOA schema/migrations differ" >&2; exit 1; }
echo "migrations: $(psql -d homeoa_restored -Atc "$q2") applied in both"
cmp /data/media/statements/2026-10.pdf /restored/media/statements/2026-10.pdf \
  || { echo "DRILL FAILED: media differs" >&2; exit 1; }
if aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 ls "s3://$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX" \
     | grep -q homeoa-20000101T000000Z; then
  echo "DRILL FAILED: expired backup was not pruned" >&2; exit 1
fi
# Wrong key must not decrypt.
age-keygen -o /tmp/wrong.key 2>/dev/null
if BACKUP_AGE_IDENTITY_FILE=/tmp/wrong.key RESTORE_DB=homeoa_wrongkey restore.sh latest >/dev/null 2>&1; then
  echo "DRILL FAILED: restore succeeded with the wrong key" >&2; exit 1
fi
# Never overwrites an existing database.
if RESTORE_DB=homeoa_restored restore.sh latest >/dev/null 2>&1; then
  echo "DRILL FAILED: restore overwrote an existing database" >&2; exit 1
fi
'

echo "==> encrypted field decrypts with the keys recovered from the backup"
restored_key="$(run 'sed -n "s/^HOMEOA_FIELD_KEYS=//p" /restored/secrets.env')"
restored_secret="$(run 'sed -n "s/^HOMEOA_SECRET_KEY=//p" /restored/secrets.env')"
[ "$restored_key" = "$FIELD_KEY" ] || { echo "DRILL FAILED: field keys not in backup" >&2; exit 1; }
[ "$restored_secret" = "$SECRET_KEY" ] || { echo "DRILL FAILED: secret key not in backup" >&2; exit 1; }
out="$("${DC[@]}" run --rm -T -e HOMEOA_FIELD_KEYS="$restored_key" -e HOMEOA_DB_NAME=homeoa_restored \
  app python manage.py shell -c "
from django.db import connection
from homeoa.core.crypto import decrypt
with connection.cursor() as c:
    c.execute('SELECT v FROM drill_secret')
    print('DECRYPTED=' + decrypt(c.fetchone()[0]))
")"
grep -q '^DECRYPTED=drill-plaintext-42$' <<<"$out" \
  || { echo "DRILL FAILED: encrypted field did not decrypt after restore" >&2; exit 1; }
echo "encrypted field: decrypted OK with the restored keys"
echo "DRILL PASSED"
