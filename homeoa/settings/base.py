"""Shared settings. Environment-specific modules import from here."""

import os
from pathlib import Path

from django.utils.translation import gettext_lazy as _

from homeoa.core.security_headers import csp_settings

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _sep, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("HOMEOA_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = [h for h in env("HOMEOA_ALLOWED_HOSTS", "").split(",") if h]

# SimpleFIN demo bridge in sandbox associations. Off: sandboxes always use the
# fake bridge (OPS-05). On: a sandbox may reach the real bridge, but only on these hosts.
# The one-time setup token is claimed by seed_demo only when the flag is on.
HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = env_bool("HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE", False)
HOMEOA_SIMPLEFIN_DEMO_HOSTS = [
    h.strip().lower()
    for h in env("HOMEOA_SIMPLEFIN_DEMO_HOSTS", "beta-bridge.simplefin.org").split(",")
    if h.strip()
]
HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = env("HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN", "")

# Django admin is mounted only when enabled; off in prod unless HOMEOA_ADMIN_ENABLED=1.
HOMEOA_ADMIN_ENABLED = env_bool("HOMEOA_ADMIN_ENABLED", True)

HOMEOA_APPS = [
    "homeoa.core",
    "homeoa.ui",
    "homeoa.accounts",
    "homeoa.access",
    "homeoa.audit",
    "homeoa.events",
    "homeoa.jobs",
    "homeoa.plugins",
    "homeoa.providers",
    "homeoa.rules",
    "homeoa.registry",
    "homeoa.ledger",
    "homeoa.billing",
    "homeoa.payments",
    "homeoa.banking",
    "homeoa.imports",
    "homeoa.dashboard",
    "homeoa.notify",
    "homeoa.portal",
    "homeoa.letters",
    "homeoa.statements",
    "homeoa.documents",
    "homeoa.search",
    "homeoa.assist",
    "homeoa.insurance",
    "homeoa.resale",
    "homeoa.budget",
    "homeoa.compliance",
    "homeoa.vendors",
    "homeoa.ap",
    "homeoa.reports",
    "homeoa.closing",
    "homeoa.community",
    "homeoa.governance",
    "homeoa.changes",
    "homeoa.enforcement",
    "homeoa.meetings",
    "homeoa.votes",
    "homeoa.esign",
    "homeoa.arc",
    "homeoa.maintenance",
    "homeoa.setup",
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "django_htmx",
    "procrastinate.contrib.django",
    *HOMEOA_APPS,
]

# Plugins to load: comma-separated entry-point (plugin id) or distribution names.
HOMEOA_PLUGINS = [p.strip() for p in env("HOMEOA_PLUGINS", "").split(",") if p.strip()]


def _plugin_apps(names):
    from homeoa.plugins.registry import django_apps

    return django_apps(names)


INSTALLED_APPS += _plugin_apps(HOMEOA_PLUGINS)

# Runs the optional modules' ready hooks; it must stay the last installed app.
MODULE_HOOKS_APP = "homeoa.plugins.hooks"


def modules_last(installed):
    return [a for a in installed if a != MODULE_HOOKS_APP] + [MODULE_HOOKS_APP]


INSTALLED_APPS = modules_last(INSTALLED_APPS)

# Optional features shipped inside HomeOA: dotted paths to each module's own manifest.
HOMEOA_MODULES = [
    "homeoa.enforcement.module.MANIFEST",
    "homeoa.arc.module.MANIFEST",
    "homeoa.maintenance.requests_module.MANIFEST",
    "homeoa.maintenance.work_orders_module.MANIFEST",
    "homeoa.community.announcements_module.MANIFEST",
    "homeoa.community.calendar_module.MANIFEST",
    "homeoa.community.polls_module.MANIFEST",
    "homeoa.registry.directory_module.MANIFEST",
    "homeoa.governance.committees_module.MANIFEST",
    "homeoa.payments.autopay_module.MANIFEST",
    "homeoa.reports.packet_module.MANIFEST",
    "homeoa.assist.module.MANIFEST",
]

# With no switch row an optional module is off; only test settings turn this on.
HOMEOA_MODULES_DEFAULT_ON = False

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "homeoa.core.security_headers.PermissionsPolicyMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "homeoa.accounts.middleware.MFAEnrolmentMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "homeoa.core.middleware.CurrentAssociationMiddleware",
    "homeoa.plugins.middleware.ModulePagesMiddleware",
    "homeoa.access.middleware.ResidentGateMiddleware",
    "homeoa.access.middleware.ReadOnlyRoleMiddleware",
    "homeoa.audit.middleware.AuditContextMiddleware",
]

# Content-Security-Policy: report-only until E2E and the live demo show no
# violations; HOMEOA_CSP_REPORT_ONLY=false enforces it. Test and E2E settings enforce.
HOMEOA_CSP_REPORT_ONLY = env_bool("HOMEOA_CSP_REPORT_ONLY", True)
SECURE_CSP, SECURE_CSP_REPORT_ONLY = csp_settings(HOMEOA_CSP_REPORT_ONLY)

ROOT_URLCONF = "homeoa.urls"
WSGI_APPLICATION = "homeoa.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "homeoa.ui.context_processors.shell",
                "homeoa.core.sandbox.context",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env("HOMEOA_DB_HOST", "localhost"),
        "PORT": env("HOMEOA_DB_PORT", "5432"),
        "NAME": env("HOMEOA_DB_NAME", "homeoa"),
        "USER": env("HOMEOA_DB_USER", "homeoa"),
        "PASSWORD": env("HOMEOA_DB_PASSWORD", "homeoa"),
        "CONN_MAX_AGE": 0,
    }
}

# Field-encryption keys (homeoa.core.crypto), newest first, comma-separated in the env.
HOMEOA_FIELD_KEYS = [k.strip() for k in env("HOMEOA_FIELD_KEYS", "").split(",") if k.strip()]

# Public origin for links in emails (e.g. https://hoa.example.org). Required in prod;
# empty elsewhere means "use the request's origin".
HOMEOA_BASE_URL = env("HOMEOA_BASE_URL").rstrip("/")
HOMEOA_BASE_URL_REQUIRED = False

# Backup health (homeoa.core.ops): status files written by the backup container. Empty
# directory = not configured (dev). Prod also checks the number of age recipients.
HOMEOA_BACKUP_STATUS_DIR = env("HOMEOA_BACKUP_STATUS_DIR")
HOMEOA_BACKUP_MAX_AGE_HOURS = int(env("HOMEOA_BACKUP_MAX_AGE_HOURS", "36"))
HOMEOA_BACKUP_AGE_RECIPIENTS = env("BACKUP_AGE_RECIPIENTS").split()
HOMEOA_BACKUP_MIN_RECIPIENTS = 0

# Reverse proxies (IPs or CIDRs) whose X-Forwarded-For is trusted for the client IP.
HOMEOA_TRUSTED_PROXIES = [
    p.strip() for p in env("HOMEOA_TRUSTED_PROXIES", "").split(",") if p.strip()
]

# Cloudflare Tunnel connector IPs (cloudflared container). CF-Connecting-IP is
# honoured only when the trusted proxy chain ends at one of these. Empty = no tunnel.
HOMEOA_CF_TUNNEL_PROXIES = [
    p.strip() for p in env("HOMEOA_CF_TUNNEL_PROXIES", "").split(",") if p.strip()
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth: passwordless magic links, optional TOTP.
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:index"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14

AUTH_PASSWORD_VALIDATORS: list = []

LANGUAGE_CODE = "en"
LANGUAGES = [("en", _("English"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
TIME_ZONE = "UTC"  # stored in UTC; each association carries its own timezone
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("HOMEOA_EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("HOMEOA_EMAIL_PORT", "587"))
DEFAULT_FROM_EMAIL = env("HOMEOA_DEFAULT_FROM_EMAIL", "homeoa@localhost")
# Reply-To for notices (a board member's own address); empty = none.
HOMEOA_EMAIL_REPLY_TO = env("HOMEOA_EMAIL_REPLY_TO", "")

# seed_demo --reset may wipe sandbox data (OPS-05). Never set in production.
HOMEOA_ALLOW_DEMO_RESET = False

# An association added in the app on a new install starts in sandbox (demo) mode: the demo
# banner shows and the bank feed stays on fakes or SimpleFIN's demo bridge. On for the
# public demo; never in production.
HOMEOA_SANDBOX_NEW_ASSOCIATIONS = env_bool("HOMEOA_SANDBOX_NEW_ASSOCIATIONS", False)

# Pin association_today() to a date (YYYY-MM-DD) for tests, E2E and demos (PLT-07).
# Honored only with DEBUG or HOMEOA_FIXED_TODAY_ALLOWED; production refuses it.
HOMEOA_FIXED_TODAY = env("HOMEOA_FIXED_TODAY")
HOMEOA_FIXED_TODAY_ALLOWED = False

# Stripe client: "live" (real API, keys from the plugin's secrets) or "fake"
# (in-repo fake; only with DEBUG or HOMEOA_FIXED_TODAY_ALLOWED; production refuses it).
HOMEOA_STRIPE_CLIENT = env("HOMEOA_STRIPE_CLIENT", "live")
# Stripe test-mode (sandbox) keys from the environment; only sk_test_/rk_test_ and pk_test_
# are accepted (the plugin refuses to start otherwise). An installation's api_key secret
# takes precedence over HOMEOA_STRIPE_SECRET_KEY.
HOMEOA_STRIPE_SECRET_KEY = env("HOMEOA_STRIPE_SECRET_KEY")
HOMEOA_STRIPE_PUBLISHABLE_KEY = env("HOMEOA_STRIPE_PUBLISHABLE_KEY")

# Anthropic client for AI assistance: "live" (the API key from the plugin's
# secrets) or "fake" (in-repo stand-in; only with DEBUG or HOMEOA_FIXED_TODAY_ALLOWED).
HOMEOA_ANTHROPIC_CLIENT = env("HOMEOA_ANTHROPIC_CLIENT", "live")
