# HomeOA application image: web, worker and the one-shot migrate step all run from it.
# Build from the repository root:  docker build -f deploy/Dockerfile -t homeoa:local .

# --- build: resolve the locked dependencies into /venv with uv ---------------------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY plugins ./plugins
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# --- runtime -------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime
# WeasyPrint (PDF statements, later milestones) needs Pango/HarfBuzz; libpq for pg_isready.
# Tesseract reads scanned documents for search when the tesseract plugin is on.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 fonts-dejavu-core \
      postgresql-client curl tini tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 homeoa \
 && useradd --system --uid 10001 --gid homeoa --home /app --shell /usr/sbin/nologin homeoa \
 && mkdir -p /data/media /app/staticfiles \
 && chown -R homeoa:homeoa /data /app

ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=homeoa.settings.prod \
    HOMEOA_MEDIA_ROOT=/data/media \
    HOMEOA_STATIC_ROOT=/app/staticfiles

COPY --from=build /venv /venv
COPY --from=build --chown=homeoa:homeoa /app /app
WORKDIR /app
USER homeoa
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD curl -fsS -H "Host: ${HOMEOA_ALLOWED_HOSTS%%,*}" http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["tini", "--", "/app/deploy/entrypoint.sh"]
CMD ["web"]
