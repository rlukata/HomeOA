"""Production settings. All secrets come from the environment."""

from django.core.exceptions import ImproperlyConfigured

from homeoa.core.logging import logging_config

from .base import *  # noqa: F403
from .base import BASE_DIR, DEFAULT_FROM_EMAIL, HOMEOA_FIELD_KEYS, SECRET_KEY, env, env_bool

if env("HOMEOA_FIXED_TODAY"):
    raise ImproperlyConfigured("HOMEOA_FIXED_TODAY must not be set in production.")
HOMEOA_FIXED_TODAY = ""
HOMEOA_FIXED_TODAY_ALLOWED = False

if env("HOMEOA_STRIPE_CLIENT", "live") != "live":
    raise ImproperlyConfigured("HOMEOA_STRIPE_CLIENT must be 'live' in production.")
HOMEOA_STRIPE_CLIENT = "live"
if env("HOMEOA_ANTHROPIC_CLIENT", "live") != "live":
    raise ImproperlyConfigured("HOMEOA_ANTHROPIC_CLIENT must be 'live' in production.")
HOMEOA_ANTHROPIC_CLIENT = "live"

if not SECRET_KEY:
    raise ImproperlyConfigured("HOMEOA_SECRET_KEY must be set in production.")

if not HOMEOA_FIELD_KEYS:
    raise ImproperlyConfigured("HOMEOA_FIELD_KEYS must be set in production.")
try:
    from cryptography.fernet import Fernet

    for _key in HOMEOA_FIELD_KEYS:
        Fernet(_key.encode())
except ValueError as exc:
    raise ImproperlyConfigured("HOMEOA_FIELD_KEYS contains an invalid Fernet key.") from exc

DEBUG = False
HOMEOA_BASE_URL_REQUIRED = True  # system check homeoa.E001 (runs on every start)
# Passkey RP ID/origin come from HOMEOA_BASE_URL, never the request Host header.
HOMEOA_WEBAUTHN_HOST_FALLBACK = False
HOMEOA_BACKUP_MIN_RECIPIENTS = 2  # check --deploy warning homeoa.W002
HOMEOA_ADMIN_ENABLED = env_bool("HOMEOA_ADMIN_ENABLED", False)

# Security headers (behind a TLS-terminating proxy such as Caddy).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("HOMEOA_SSL_REDIRECT", True)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # the container healthcheck speaks plain HTTP
SECURE_HSTS_SECONDS = int(env("HOMEOA_HSTS_SECONDS", "31536000"))  # 1 year, this host only
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("HOMEOA_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = False
# Subdomain HSTS and preload depend on the (not yet chosen) domain: opt in via env.
SILENCED_SYSTEM_CHECKS = [] if SECURE_HSTS_INCLUDE_SUBDOMAINS else ["security.W005"]
SILENCED_SYSTEM_CHECKS += ["security.W021"]
# Access log with the real client IP (client_ip()); gunicorn's own access log is off.
MIDDLEWARE = ["homeoa.core.access_log.AccessLogMiddleware", *MIDDLEWARE]  # noqa: F405
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = [o for o in env("HOMEOA_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# Port 465 (e.g. Resend SMTPS) needs implicit SSL; Django rejects SSL and TLS together.
EMAIL_USE_SSL = env_bool("HOMEOA_EMAIL_USE_SSL", False)
EMAIL_USE_TLS = False if EMAIL_USE_SSL else env_bool("HOMEOA_EMAIL_USE_TLS", True)
EMAIL_HOST_USER = env("HOMEOA_EMAIL_USER")
EMAIL_HOST_PASSWORD = env("HOMEOA_EMAIL_PASSWORD")

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
STATIC_ROOT = env("HOMEOA_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
MEDIA_ROOT = env("HOMEOA_MEDIA_ROOT", "/data/media")
MEDIA_URL = "media/"
# LocalDiskStorage files live inside MEDIA_ROOT: same volume, same backups.
HOMEOA_FILE_ROOT = env("HOMEOA_FILE_ROOT", MEDIA_ROOT.rstrip("/") + "/files")

# Error alerts (OPS-03): email ADMINS on 500s over any SMTP; optional Sentry-compatible DSN.
ADMINS = [a.strip() for a in env("HOMEOA_ADMINS", "").split(",") if a.strip()]
SERVER_EMAIL = env("HOMEOA_SERVER_EMAIL", DEFAULT_FROM_EMAIL)
LOGGING = logging_config(level=env("HOMEOA_LOG_LEVEL", "INFO"), mail_admins=bool(ADMINS))

SENTRY_DSN = env("HOMEOA_SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=False,
        traces_sample_rate=0.0,
        environment=env("HOMEOA_ENVIRONMENT", "production"),
    )
