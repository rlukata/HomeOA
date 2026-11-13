"""Test settings: real Postgres (test_<HOMEOA_DB_NAME>), in-memory email."""

import atexit
import os
import shutil
import tempfile

from .base import *  # noqa: F403
from .base import HOMEOA_PLUGINS, INSTALLED_APPS, modules_last

SECRET_KEY = "test-secret-key"
DEBUG = False
HOMEOA_FIELD_KEYS = ["ZxBFQzV0wh4mnXQim1_pLdCRfc-HXr6s8f-IZO3m-ow="]
ALLOWED_HOSTS = ["testserver", "localhost"]

# The SimpleFIN plugin is loaded so its tests (and models) run; per-association it stays
# off unless a test enables it.
if "simplefin" not in HOMEOA_PLUGINS:
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "simplefin"]
if "lob" not in HOMEOA_PLUGINS:  # postal fake; off per association by default
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "lob"]
if "stripe" not in HOMEOA_PLUGINS:  # ACH fake; off per association by default
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "stripe"]
if "rulepack_us_wa" not in HOMEOA_PLUGINS:  # WA rule pack; demo seed selects it
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "rulepack_us_wa"]
if "tesseract" not in HOMEOA_PLUGINS:  # OCR; off per association by default
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "tesseract"]
if "anthropic" not in HOMEOA_PLUGINS:  # AI assistance (fake client); off by default
    HOMEOA_PLUGINS = [*HOMEOA_PLUGINS, "anthropic"]
INSTALLED_APPS = [*INSTALLED_APPS, "tests.scoped"]
if "homeoa_simplefin" not in INSTALLED_APPS:
    INSTALLED_APPS.append("homeoa_simplefin")
if "homeoa_stripe" not in INSTALLED_APPS:
    INSTALLED_APPS.append("homeoa_stripe")
INSTALLED_APPS = modules_last(INSTALLED_APPS)

WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Stored files go to a throwaway folder per test run, never the repo's media/.
HOMEOA_FILE_ROOT = tempfile.mkdtemp(prefix="homeoa-test-files-")
atexit.register(shutil.rmtree, HOMEOA_FILE_ROOT, ignore_errors=True)
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Unit tests see every optional module on unless a test turns it off.
HOMEOA_MODULES_DEFAULT_ON = True

HOMEOA_ALLOW_DEMO_RESET = True  # seed_demo --reset
HOMEOA_FIXED_TODAY_ALLOWED = True
HOMEOA_STRIPE_CLIENT = "fake"
HOMEOA_ANTHROPIC_CLIENT = "fake"

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

# Law change sources never call the legislature's web service in tests.
HOMEOA_LAW_CHECK_NETWORK = False
