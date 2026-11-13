"""Local development: DEBUG on, email is printed to the terminal (console backend)."""

import os

from .base import *  # noqa: F403
from .base import HOMEOA_FIELD_KEYS, INSTALLED_APPS, _plugin_apps, env, env_bool, modules_last

# Dev enables the SimpleFIN plugin by default so the demo has a bank feed (sandbox FakeClient);
# set HOMEOA_PLUGINS explicitly (even empty) to override.
if "HOMEOA_PLUGINS" not in os.environ:
    HOMEOA_PLUGINS = ["simplefin", "lob", "stripe", "rulepack_us_wa", "tesseract", "anthropic"]
    INSTALLED_APPS = [*INSTALLED_APPS, *_plugin_apps(HOMEOA_PLUGINS)]
INSTALLED_APPS = modules_last(INSTALLED_APPS)

DEBUG = True
SECRET_KEY = env("HOMEOA_SECRET_KEY", "dev-insecure-secret-key-change-me")
# Dev-only fallback key; never use it for real data.
HOMEOA_FIELD_KEYS = HOMEOA_FIELD_KEYS or ["QBtgYdnlvb1x6iZyMQnez82qsAF6A1r2xxLYmzpbAVA="]
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# No worker under runserver: run on_commit jobs that support it in-process (accounts.tasks).
HOMEOA_JOBS_INLINE = env_bool("HOMEOA_JOBS_INLINE", True)

HOMEOA_ALLOW_DEMO_RESET = True  # seed_demo --reset
HOMEOA_FIXED_TODAY_ALLOWED = True
HOMEOA_STRIPE_CLIENT = "fake"
HOMEOA_ANTHROPIC_CLIENT = "fake"
