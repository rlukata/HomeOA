#!/usr/bin/env bash
# Container entrypoint. Modes:
#   migrate  wait for the DB, show pending migrations, apply them (non-zero exit stops the deploy)
#   web      collect static files, serve with gunicorn
#   worker   run the Procrastinate job worker
#   *        run the given command (e.g. "python manage.py createsuperuser")
set -euo pipefail

wait_for_db() {
  local host="${HOMEOA_DB_HOST:-db}" port="${HOMEOA_DB_PORT:-5432}" tries=60
  until pg_isready -q -h "$host" -p "$port" -U "${HOMEOA_DB_USER:-homeoa}"; do
    tries=$((tries - 1))
    if [ "$tries" -le 0 ]; then
      echo "entrypoint: database at $host:$port did not become ready" >&2
      exit 1
    fi
    sleep 1
  done
}

case "${1:-web}" in
  migrate)
    wait_for_db
    if python manage.py migrate --check >/dev/null 2>&1; then
      echo "entrypoint: no pending migrations"
    else
      echo "entrypoint: pending migrations:"
      python manage.py showmigrations --plan | grep '\[ \]' || true
      python manage.py migrate --noinput
    fi
    ;;
  web)
    wait_for_db
    python manage.py collectstatic --noinput --verbosity 0
    exec gunicorn homeoa.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${HOMEOA_WEB_WORKERS:-3}" \
      --timeout "${HOMEOA_WEB_TIMEOUT:-60}"
    # No gunicorn access log: it would show the Caddy hop. Django's
    # homeoa.core.access_log middleware logs each request with the real client IP.
    ;;
  worker)
    wait_for_db
    exec python manage.py run_worker
    ;;
  *)
    exec "$@"
    ;;
esac
