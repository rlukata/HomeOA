# HomeOA deployment runbook

Production runs as one Docker Compose stack on any VPS; no vendor is assumed.

| Service | What it does |
|---|---|
| `db` | PostgreSQL 17, data in the `pgdata` volume |
| `migrate` | one-shot: waits for the DB, lists pending migrations, applies them. If it fails, `web` and `worker` do not start |
| `web` | gunicorn on :8000 (internal only); `collectstatic` on start; `/healthz` healthcheck |
| `worker` | Procrastinate job worker (`manage.py run_worker`) |
| `backup` | nightly encrypted backup to an S3-compatible bucket (`backup-loop.sh` -> `backup.sh`) |
| `caddy` | TLS on :443 (automatic ACME certificates; Caddy's local CA for `localhost`), proxies to `web` |

Files: `Dockerfile` (app image, non-root uid 10001, WeasyPrint libraries included),
`backup.Dockerfile` (pg_dump 17 + age + AWS CLI), `compose.prod.yaml`, `Caddyfile`,
`entrypoint.sh`, `backup.sh`, `restore.sh`, `drill.sh` + `compose.test.yaml` (restore drill
against MinIO), `prod.env.example`.

## 0. Checkpoint: before loading real association data

Do not import real owner data until all three are chosen and written down:

1. **Host**: a VPS with Docker Compose, a public IP and a DNS name (`HOMEOA_DOMAIN`).
2. **Backup bucket**: any S3-compatible store (AWS S3, Backblaze B2, Cloudflare R2, Wasabi,
   self-hosted MinIO...) in a different provider/region from the host. Create a key that can
   only read/write that bucket.
3. **SMTP**: any provider that can send from the association's domain (SPF/DKIM set up).

Record the three choices (provider names, region, who holds the account logins) with the
association's records before going further. Then work through the two lists below
in order: the platform checklist, then the real-data checklist. Section 9 is the import
itself.

### Go-live checklist (all must be ticked before the real import)

- [ ] Host chosen and the domain's DNS points at it; `HOMEOA_DOMAIN` set, Caddy has a real
      certificate (`curl -fsS https://<domain>/healthz`).
- [ ] Real bucket configured (`BACKUP_S3_*`), and a restore drill (section 5, production
      variant) **passed against that bucket**, including the encrypted-field check.
- [ ] At least 2 age recipients in `BACKUP_AGE_RECIPIENTS`, held by two different board
      members (section 8); `manage.py check --deploy` shows no `homeoa.W002`.
- [ ] Backup staleness check working: `last_success.json` exists in the `backup_status`
      volume after the first backup, and a test alert reached `HOMEOA_ADMINS` (section 3).
- [ ] `HOMEOA_BASE_URL=https://<domain>` set (start-up check `homeoa.E001`).
- [ ] HSTS warnings re-enabled once the domain is final: set
      `HOMEOA_HSTS_INCLUDE_SUBDOMAINS=1` if every subdomain is HTTPS (clears the W005
      silence), consider a longer `HOMEOA_HSTS_SECONDS`, and review the W021 (preload)
      silence in `homeoa/settings/prod.py`.
- [ ] `deploy/prod.env` is gitignored (`git check-ignore deploy/prod.env` prints it), is
      `chmod 600`, and was never committed.
- [ ] SMTP sends from the association's domain (a magic-link email arrives, not in spam).

### Real-data checklist (after the go-live checklist, before section 9)

- [ ] **Backups verified against the real bucket**: at least one nightly backup has landed
      in the production bucket (`aws s3 ls s3://$BACKUP_S3_BUCKET/` with the backup key),
      and `last_success.json` is less than 26 hours old.
- [ ] **Restore drill done** on a scratch host or a throwaway compose project (section 5,
      production variant) from that bucket, with a key holder's own private key, and the
      encrypted-field check passed. Note the date and who ran it in the board minutes.
- [ ] **Field keys stored offline**: `HOMEOA_FIELD_KEYS` and `HOMEOA_SECRET_KEY` are in the
      board's password manager **and** a sealed printed copy is with the association
      records (same handling as the age keys, section 8). Losing `HOMEOA_FIELD_KEYS` makes
      encrypted owner fields unreadable even with a good backup.
- [ ] **MFA on for the board**: every board member has enrolled an authenticator app, then
      Settings -> Roles & access -> "Require MFA for board" is ticked and saved. Sign out
      and back in as one board member to confirm the code is asked for.
- [ ] **Sandbox off**: production was started without `seed_demo`; no sandbox banner shows
      on Home. (Never run `seed_demo` against production.)
- [ ] **Works on a phone**: `https://<domain>/` opens Home in a phone browser with the
      bottom tabs. (No PWA install or native app is required.)
- [ ] **Late fee terms**: the late fee policy under Money -> Assessments -> Late fees
      matches what the board adopted. Until then leave it inactive in production;
      the $25 / 10-day policy in the demo seed is a placeholder only.

## 1. First deploy

```bash
git clone <repo> homeoa && cd homeoa
cp deploy/prod.env.example deploy/prod.env      # gitignored; chmod 600 it
```

Fill in `deploy/prod.env`:

- `HOMEOA_DOMAIN`, `HOMEOA_ALLOWED_HOSTS` (the domain), `HOMEOA_CSRF_TRUSTED_ORIGINS`
  (`https://<domain>`), `HOMEOA_BASE_URL` (`https://<domain>`, required: emailed links are
  built from it, never from the request's Host header; the app refuses to start without an
  https value, check `homeoa.E001`). Passkeys also take their relying-party ID and origin
  from it (not the Host header), so changing the domain invalidates registered passkeys;
  override only with `HOMEOA_WEBAUTHN_RP_ID` / `HOMEOA_WEBAUTHN_ORIGIN`.
- Secrets, generated on your machine:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(50))"                     # HOMEOA_SECRET_KEY
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # HOMEOA_FIELD_KEYS
  python3 -c "import secrets; print(secrets.token_urlsafe(24))"                     # HOMEOA_DB_PASSWORD
  ```
  Store copies of `HOMEOA_FIELD_KEYS` and `HOMEOA_SECRET_KEY` in the board's password manager.
  **Without the field keys, encrypted fields in a restored backup cannot be read.**
- SMTP (`HOMEOA_EMAIL_*`), `HOMEOA_ADMINS` (who gets 500-error emails), optional
  `HOMEOA_SENTRY_DSN` (any Sentry-compatible service, e.g. GlitchTip).
- Backups: **two** key holders (e.g. treasurer and president) each generate their own age
  key pair **on their own computer, never the server** (custody: section 8):
  ```bash
  age-keygen -o homeoa-backup-treasurer.key   # private: personal password manager + sealed print
  age-keygen -y homeoa-backup-treasurer.key   # public key -> send to whoever edits prod.env
  ```
  `BACKUP_AGE_RECIPIENTS="age1...treasurer age1...president"` (space separated, public keys
  only). Fewer than 2 makes `backup.sh` log a warning and `check --deploy` flag `homeoa.W002`.
  plus `BACKUP_S3_BUCKET`, `BACKUP_S3_ENDPOINT` (empty for AWS), `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `BACKUP_RETENTION_DAYS` (default 35), `BACKUP_HOUR_UTC`.
  Optional extras: `BACKUP_OK_WEBHOOK` (pinged after each success, for an external
  dead-man's-switch monitor) and `BACKUP_FAIL_WEBHOOK`. They are not needed for alerting:
  the app checks backups itself (section 3).

The `backup` service is in the compose profile `backup`. `prod.env.example` sets
`COMPOSE_PROFILES=backup`, so production starts it with a plain `up -d`; keep that line.
With Cloudflare Tunnel in production use `COMPOSE_PROFILES=backup,tunnel` (a `--profile`
flag on the command line replaces `COMPOSE_PROFILES`, which would drop backups).

Start:

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env up -d --build
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env ps
curl -fsS https://<domain>/healthz           # {"status": "ok", "database": "ok"}
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env run --rm web \
  python manage.py createsuperuser
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env run --rm backup backup.sh
```

The last line takes a first backup immediately; check it appears in the bucket.

Then sign in with the superuser's email. Home asks for the association's name, initials,
time zone and fiscal year start (only while no association exists), creates it with the
default roles and chart of accounts, and opens Settings: turn on the plugins, choose the
state pack, and add the board in Roles.

Local test of the whole stack: keep `HOMEOA_DOMAIN=localhost`, set `HOMEOA_HTTP_PORT=8080`,
`HOMEOA_HTTPS_PORT=8443`, `HOMEOA_BASE_URL=https://localhost:8443`, then
`curl -k https://localhost:8443/healthz`.

**Why age for encryption:** backups are encrypted to a *public* key, so the server (and anyone
who compromises it or the bucket) cannot decrypt them; only the offline secret key can. age has
no keyring, trust model or configuration to get wrong (unlike gpg), is a single static binary
packaged in Debian, and supports several recipients (give two board members their own keys by
listing both public keys in `BACKUP_AGE_RECIPIENTS`).

## 2. Upgrade

```bash
git pull
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env run --rm backup backup.sh   # pre-upgrade backup
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env up -d --build
```

`migrate` runs first; if a migration fails it exits non-zero and the new `web`/`worker` are
not started. Check `docker compose ... logs migrate`, fix, and run `up` again.

### Rollback

Migrations are forward-only in practice. To roll back a bad release: `git checkout <previous
tag>`, and if the failed release already migrated the schema, restore the pre-upgrade backup
(section 4) and point the app at the restored database. Then `up -d --build`.

## 3. Backups

- Nightly at `BACKUP_HOUR_UTC` (default 09:00 UTC, 2am Pacific): `pg_dump -Fc` + a tarball of
  the `media` volume (`MEDIA_ROOT=/data/media`, which also holds stored documents:
  `HOMEOA_FILE_ROOT=/data/media/files`) + `secrets.env` (the current `HOMEOA_FIELD_KEYS` and
  `HOMEOA_SECRET_KEY`) + a manifest and SHA-256 sums, tarred, encrypted with age to every
  recipient, uploaded as
  `<prefix>homeoa-YYYYmmddTHHMMSSZ.tar.age`. Objects older than `BACKUP_RETENTION_DAYS` are
  deleted after each successful upload. Enable bucket versioning / object lock at the provider
  for protection against a compromised server deleting backups.
  Because the app secrets travel inside the encrypted bundle, **one age private key restores
  everything**, including encrypted fields.
- Alerting (no external service needed): each run writes `last_success.json` or
  `last_failure.json` to the small `backup_status` volume. The worker mounts it read-only and
  the daily job `ops.backup_check` (08:00 association time) emails `HOMEOA_ADMINS` and
  publishes an `ops.backup_stale` event (a board to-do on Home) when the last success is
  older than `HOMEOA_BACKUP_MAX_AGE_HOURS` (default 36), none was ever recorded, or a failure
  is newer than the last success. Test it once:
  `docker compose ... run --rm --entrypoint "" backup rm /status/last_success.json`, then
  `docker compose ... exec worker python manage.py run_scheduled --as-of $(date +%F) --job ops.backup_check --rerun`; an
  email should arrive. Run `backup.sh` again to clear it.
- Logs: `docker compose ... logs backup` (JSON lines).
- Ad hoc: `docker compose ... run --rm backup backup.sh`.

## 4. Restore (real incident)

On a machine that has the age secret key (copy it in only for the restore, then remove it):

```bash
docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env run --rm \
  -v "$PWD/homeoa-backup.key:/run/age.key:ro" -v homeoa-prod_media:/restore-media \
  -e BACKUP_AGE_IDENTITY_FILE=/run/age.key -e RESTORE_DB=homeoa_restored \
  -e RESTORE_MEDIA_DIR=/restore-media -e RESTORE_PRINT_SECRETS=1 --user root \
  backup restore.sh latest            # or an object name, or a local .tar.age path
```

`restore.sh` decrypts, verifies the checksums, creates a **new** database (it refuses to touch
an existing one), restores into it, unpacks media, prints the bundled `HOMEOA_FIELD_KEYS` /
`HOMEOA_SECRET_KEY` (`RESTORE_PRINT_SECRETS=1`; or `RESTORE_SECRETS_FILE=/path` writes them to a
mode-600 file instead), and prints a row count per table as a smoke check. Then switch over:
put those two values in `prod.env` (on a new server they are all you need besides the
bucket credentials), set `HOMEOA_DB_NAME=homeoa_restored`, and `up -d` (the old database
stays available for comparison until you drop it).

## 5. Restore drill

`deploy/drill.sh` runs a full round trip locally against MinIO with throwaway credentials and
removes everything afterwards: migrates the real HomeOA schema, seeds ledger-like rows and a
media file, backs up (encrypt + upload + retention pruning), restores `latest` into a fresh
database, and checks row counts, money totals, a checksum of every row, migrations and the
media file; also that a wrong key fails and an existing database is never overwritten.

```bash
deploy/drill.sh                                   # prints DRILL PASSED
HOMEOA_DEPLOY_DRILL=1 uv run pytest tests/test_deploy.py -k drill
```

Quarterly (and once before the real import, section 0), also drill against production:
run section 4 with `RESTORE_DB=homeoa_drill_<date>` and no `RESTORE_MEDIA_DIR`, compare the
printed row counts with production, then `dropdb homeoa_drill_<date>`. Record the date in
the board minutes.

## 6. Key rotation

- **Field encryption** (`HOMEOA_FIELD_KEYS`):
  1. Take a backup (`run --rm backup backup.sh`).
  2. Generate a new Fernet key and put it **first**, keeping the old one after it
     (`HOMEOA_FIELD_KEYS=new,old`). `up -d`. New writes use the new key, and old values
     still decrypt.
  3. Re-encrypt every stored value under the newest key:
     `docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env run --rm web python manage.py rotate_field_keys`
  4. Drop the old key (`HOMEOA_FIELD_KEYS=new`), `up -d`, and check the app reads
     encrypted values (e.g. a plugin secret).
  5. Keep the old key in the password manager until every backup taken before step 3 has
     aged out of retention: restoring those backups needs it in `HOMEOA_FIELD_KEYS`.
- **Backup age keys** (one per holder, section 8): the holder generates a new pair and sends
  the public key; replace their entry in `BACKUP_AGE_RECIPIENTS` (keep 2+ entries),
  `up -d backup`, and run `backup.sh` once. Older backups are still encrypted to the old key:
  keep the old private key (sealed) until they have aged out of retention, or, when a holder
  left, delete the old backups from the bucket once a new one has been verified.
- **Secrets in backups**: after rotating `HOMEOA_FIELD_KEYS` or `HOMEOA_SECRET_KEY`, run
  `backup.sh` so the newest backup carries the new values.
- **Django secret key**: replace `HOMEOA_SECRET_KEY`; `up -d`. Everyone is signed out and
  outstanding magic links stop working.
- **DB password**: `ALTER USER homeoa PASSWORD '...'` via `docker compose ... exec db psql`,
  update `HOMEOA_DB_PASSWORD`, `up -d`.
- **Bucket / SMTP credentials**: rotate at the provider, update `prod.env`, `up -d`.

## 7. Logs, alerts and client IPs

- All app and worker logs are JSON lines on stdout with a `request_id` (also returned as the
  `X-Request-ID` response header): `docker compose ... logs -f web worker`. Caddy logs JSON too.
- 500 errors email `HOMEOA_ADMINS` over the configured SMTP; `HOMEOA_SENTRY_DSN` additionally
  reports them to any Sentry-compatible service.
- Caddy has a fixed address (`HOMEOA_PROXY_IP`, default 172.31.250.10 on subnet
  `HOMEOA_PROXY_SUBNET`); the app trusts `X-Forwarded-For` only from addresses in
  `HOMEOA_TRUSTED_PROXIES` (set to that IP by the compose file), so audit-log client IPs can't
  be spoofed by clients. Change both if the subnet collides with something on the host.
- `python manage.py check --deploy` inside the `web` container should report no issues.

## 8. Key custody

Backups can be decrypted only with an age private key, and each backup contains the app
secrets, so these keys are the keys to all owner data. The rules:

- **Two or more holders**, normally the treasurer and the president, each with their **own**
  key pair (never a shared key). Either one can restore alone.
- Each holder keeps their private key in their **personal password manager** (a secure note
  with the whole `AGE-SECRET-KEY-1...` line), and a **sealed printed copy** goes into the
  association's records (envelope signed across the seal, dated, stored with the corporate
  records). A broken seal means: rotate that key.
- Private keys never go on the server, in `prod.env`, in email or in chat. Only the public
  `age1...` keys go into `BACKUP_AGE_RECIPIENTS`.
- **When a holder leaves the board** (or a key may be exposed): the incoming holder makes a
  new pair, the leaving holder's public key is removed from `BACKUP_AGE_RECIPIENTS`, a fresh
  backup is taken and verified with the restore drill, the leaving holder's sealed copy is
  destroyed, and backups encrypted to the old key are deleted once they are no longer needed
  (they also contain the app secrets, so consider rotating `HOMEOA_FIELD_KEYS` and
  `HOMEOA_SECRET_KEY` too: section 6).
- Record who holds a key (names and dates only, never the key) in the board minutes.

## 9. Importing from the previous manager (go-live)

Only after every box in section 0 is ticked.

The move is a one-time switch: one snapshot now, one catch-up at cutoff, no monthly parallel run.
The money jobs (dues, late fees, interest, installments, notices, autopay) and resident
access wait until the System admin presses Go live (section 10), so loading and checking
data is safe.

1. Take a manual backup first (`docker compose -f deploy/compose.prod.yaml --env-file deploy/prod.env run --rm backup
   backup.sh`) and confirm it in the bucket. This is the rollback point.
2. Snapshot: the previous manager's exports of every record they hold, all years, through one stated
   "as of" date (unit, owner and tenant directories, owner ledgers, aging, chart of
   accounts, GL detail, trial balance, bank statements and reconciliations, vendors,
   documents). Before loading, check that the trial balance balances, the owner ledgers add
   up to receivables in the GL, and each bank balance matches the reconciled GL cash. Keep
   the files in the board's document store, not on a laptop desktop.
3. Load units, owners and opening balances as of that date: Units -> Import
   (`/units/import/`, check the mapping, review the preview, commit) or a throwaway script
. The import is audited and can be compared, not edited, afterwards. Close the
   opening-balance month. File older years under Documents.
4. Money -> Reports -> Parallel run (`/money/reports/parallel-run/`): compare HomeOA with the
   snapshot. Every unit balance and the operating, reserve and insurance cash must match to
   the cent. Explain or fix each difference by restoring step 1 and loading again.
5. Until cutoff the previous manager stays the system of record (owners pay them, they pay bills). Keep
   this gap short: everything in it is loaded again at catch-up.

If anything goes wrong during steps 3-4, restore the backup from step 1 (section 4) and
start again; do not hand-edit imported balances.

## 10. Leaving the previous manager (cutover)

Only after the snapshot load (section 9).

1. Catch-up: request everything since the snapshot's "as of" date (owner ledger
   entries, GL detail, bank statements, roster, vendor and document changes, updated aging),
   take a backup, load it, and run the parallel-run diff once more. It must be clean to the
   cent. Set the clean-months setting on the Cutover page to 1.
   Money -> Reports -> Cutover (`/money/reports/cutover/`) then shows **Ready**: opening data
   imported, opening-balance month closed, latest parallel run clean to the cent, clean
   months ≥ the setting on that page, every board member and ARC reviewer has a login and
   an email, and records delivery requested from the previous manager.
   Request records delivery from the previous manager in writing (in Washington, RCW
   64.34.372(11); **unverified**, have an attorney confirm), then tick the box on the Cutover page.
2. Open the final diff report from that page, save it as PDF with the association records,
   and note in the board minutes the "as of" date it covers.
3. Take a manual backup (section 9, step 1) and confirm it in the bucket.
4. Download the last full set of the previous manager's exports and documents (ledgers, statements, minutes,
   governing documents) and file them in Documents before access ends.
5. The System admin opens Settings -> Go live, reads the gap warning (dues that fell due
   after the records end, owner payments made to the previous manager in those days, late fees on those
   dues) and posts or records anything it lists, then ticks the confirm box and presses
   **Go live**. Going live takes effect today and cannot be undone. Then the board invites
   owners; tell owners how to pay from now on.
   Owners on the previous manager's autopay must link their bank again in HomeOA.
6. Give the previous manager notice per the management contract. Keep using HomeOA only; do not post in
   both systems after the notice date. Update the registered agent with the Secretary of
   State if the previous manager holds it, and tell the insurance agent, vendors and the CPA who to
   contact now.
7. After the first live month: run the month close as usual and confirm owner balances
   moved as expected.
