"""The Anthropic client for an installation: the official SDK, or the fake in tests, dev
and E2E (``HOMEOA_ANTHROPIC_CLIENT = "fake"``), which never touches the network."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

TIMEOUT_SECONDS = 60.0


def fake_allowed() -> bool:
    return bool(getattr(settings, "HOMEOA_FIXED_TODAY_ALLOWED", False) or settings.DEBUG)


def uses_fake() -> bool:
    return getattr(settings, "HOMEOA_ANTHROPIC_CLIENT", "live") == "fake"


def api_key(installation) -> str:
    secrets = (installation.secrets or {}) if installation is not None else {}
    return secrets.get("api_key") or ""


def get_client(installation=None):
    """``None`` when there is no key (live mode): AI is then unavailable."""
    if uses_fake():
        if not fake_allowed():
            raise ImproperlyConfigured("The fake Anthropic client is not allowed in production.")
        from .fake import FakeClaude

        return FakeClaude()
    key = api_key(installation)
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=1)
