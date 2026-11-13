#!/usr/bin/env bash
# Nightly scheduler for the backup container: runs backup.sh once a day at
# BACKUP_HOUR_UTC (default 09 UTC = 2am Pacific). A failed run is logged and retried
# the next night. Each run (here or ad hoc) writes last_success.json / last_failure.json to
# BACKUP_STATUS_DIR (backup.sh does it), which the app's daily ops.backup_check job reads
# and alerts on. Optional extra alerts: BACKUP_FAIL_WEBHOOK (POSTed on failure) and
# BACKUP_OK_WEBHOOK (GET on success, for a dead-man's-switch monitor).
set -uo pipefail
HOUR="${BACKUP_HOUR_UTC:-09}"

while true; do
  now=$(date -u +%s)
  next=$(date -u -d "today ${HOUR}:00" +%s)
  [ "$next" -le "$now" ] && next=$(date -u -d "tomorrow ${HOUR}:00" +%s)
  sleep $((next - now))
  if backup.sh; then
    # Optional dead-man's-switch ping (any "ping URL" monitor) so silence also alerts.
    if [ -n "${BACKUP_OK_WEBHOOK:-}" ]; then
      curl -fsS -m 10 "$BACKUP_OK_WEBHOOK" >/dev/null || true
    fi
  else
    echo '{"level":"ERROR","logger":"homeoa.backup","message":"backup failed"}' >&2
    if [ -n "${BACKUP_FAIL_WEBHOOK:-}" ]; then
      curl -fsS -m 10 -X POST -d 'HomeOA backup failed' "$BACKUP_FAIL_WEBHOOK" || true
    fi
  fi
done
