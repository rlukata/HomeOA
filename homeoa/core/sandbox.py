"""Sandbox (demo) mode (OPS-05).

A sandbox association is marked by the ``sandbox_mode`` settings key. It shows a banner on
every page, keeps the bank feed on fakes (or SimpleFIN's demo bridge), and is the only kind
of association ``seed_demo`` will wipe and reload. Payments are Stripe test mode and postal
mail is a fake everywhere. Email is not faked: a server with a real mail server sends it.
"""

from __future__ import annotations

SANDBOX_MODE_KEY = "sandbox_mode"


def is_sandbox(association) -> bool:
    return bool(association is not None and (association.settings or {}).get(SANDBOX_MODE_KEY))


def enable(association) -> None:
    settings = dict(association.settings or {})
    settings[SANDBOX_MODE_KEY] = True
    association.settings = settings
    association.save(update_fields=["settings"])


# Mail backends that never deliver (development, tests, E2E). Any other one, like the demo's
# SMTP server, really sends email, sandbox or not.
_NON_SENDING_EMAIL = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
}


def sends_real_email() -> bool:
    from django.conf import settings

    return settings.EMAIL_BACKEND not in _NON_SENDING_EMAIL


def context(request) -> dict:
    """Template context processor: ``sandbox_mode`` drives the banner in ``ui/base.html``,
    and ``sandbox_real_email`` its warning that email still goes out."""
    on = is_sandbox(getattr(request, "association", None))
    return {"sandbox_mode": on, "sandbox_real_email": on and sends_real_email()}
