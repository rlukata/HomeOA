"""End-to-end test server settings. Loads only with HOMEOA_E2E=1.

Production-like: DEBUG off, static files served by WhiteNoise (from the finders, so no
collectstatic is needed). Own database (default ``homeoa_e2e``). Email goes to files in
``HOMEOA_E2E_MAIL_DIR`` (default ``.work/e2e-mail``), which the suite reads.
"""

import os

from django.core.exceptions import ImproperlyConfigured

if os.environ.get("HOMEOA_E2E") != "1":
    raise ImproperlyConfigured("homeoa.settings.e2e requires HOMEOA_E2E=1.")

from .base import *  # noqa: E402, F403
from .base import (  # noqa: E402
    BASE_DIR,
    DATABASES,
    HOMEOA_PLUGINS,
    INSTALLED_APPS,
    env,
    env_bool,
    modules_last,
)

DEBUG = False
SECRET_KEY = "e2e-insecure-secret-key"
HOMEOA_FIELD_KEYS = ["C_TXx3WFwpHIul4yAY98GS3nwlP0v42nGYKBLgqcXpQ="]
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

DATABASES = {"default": {**DATABASES["default"], "NAME": env("HOMEOA_E2E_DB_NAME", "homeoa_e2e")}}

EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = env("HOMEOA_E2E_MAIL_DIR", str(BASE_DIR / ".work" / "e2e-mail"))

# runserver has no worker: run on_commit jobs that support it in-process (accounts.tasks).
HOMEOA_JOBS_INLINE = env_bool("HOMEOA_JOBS_INLINE", True)

# SimpleFIN is available (off per association until the seed or a test enables it with
# use_fake, or HOMEOA_SIMPLEFIN_FAKE=1). It never touches the network in E2E.
if "simplefin" not in HOMEOA_PLUGINS:
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "simplefin"]
if "homeoa_simplefin" not in INSTALLED_APPS:
    INSTALLED_APPS = [*INSTALLED_APPS, "homeoa_simplefin"]
os.environ.setdefault("HOMEOA_SIMPLEFIN_FAKE", "1")

# Stripe ACH fake: the seed enables it with a test webhook secret; never live keys.
if "stripe" not in HOMEOA_PLUGINS:
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "stripe"]
if "rulepack_us_wa" not in HOMEOA_PLUGINS:  # WA rule pack; demo seed selects it
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "rulepack_us_wa"]
if "anthropic" not in HOMEOA_PLUGINS:  # AI assistance with the fake client (no network)
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "anthropic"]
if "homeoa_stripe" not in INSTALLED_APPS:
    INSTALLED_APPS = [*INSTALLED_APPS, "homeoa_stripe"]
INSTALLED_APPS = modules_last(INSTALLED_APPS)

HOMEOA_ALLOW_DEMO_RESET = True  # seed_demo --reset
HOMEOA_FIXED_TODAY_ALLOWED = True
HOMEOA_STRIPE_CLIENT = "fake"
HOMEOA_ANTHROPIC_CLIENT = "fake"
HOMEOA_FIXED_TODAY = env("HOMEOA_FIXED_TODAY", "2026-10-07")  # the seeded demo's "today"

# Never claim or reach the real SimpleFIN demo bridge here: the flag is off and the
# one-time token (possibly loaded from .env) is blanked, in settings and the environment.
HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = False
HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = ""
os.environ.pop("HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN", None)
os.environ["HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE"] = "0"

# Enforce the CSP so any violation breaks tests.
from homeoa.core.security_headers import csp_settings  # noqa: E402

HOMEOA_CSP_REPORT_ONLY = False
SECURE_CSP, SECURE_CSP_REPORT_ONLY = csp_settings(False)

# The law check never calls the legislature from E2E runs.
HOMEOA_LAW_CHECK_NETWORK = False
