"""Background jobs for accounts (autodiscovered by procrastinate).

The magic-link email is sent by ``accounts.send_magic_link``, queued after the login
request's transaction commits. The job, not the view, looks the address up and creates
the link, so the raw token never sits in the job queue and the view behaves the same for
known and unknown addresses.

``settings.HOMEOA_JOBS_INLINE`` (default off; on in the ``dev`` and ``e2e`` settings,
env ``HOMEOA_JOBS_INLINE``) runs the job in-process at commit instead of queueing it, so
``runserver`` without a worker still delivers mail.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext as _
from procrastinate.contrib.django import app

from homeoa.audit import audit_context

from . import services

SEND_MAGIC_LINK = "accounts.send_magic_link"


@app.task(name=SEND_MAGIC_LINK, queue="default")
def send_magic_link(email: str, ip: str | None, origin: str) -> None:
    """Email a fresh sign-in link to ``email`` if it belongs to an active user."""
    with audit_context(label=f"job:{SEND_MAGIC_LINK}"):
        user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            return
        from homeoa.access.services import may_request_login

        if not may_request_login(user):
            return  # Residents are not open yet; the login page looks the same

        raw = services.create_magic_link(user, ip)
        # The configured public origin wins over the request's Host header.
        base = getattr(settings, "HOMEOA_BASE_URL", "") or origin
        url = base + reverse("accounts:verify", args=[raw])
        context = {"url": url, "minutes": int(services.LINK_TTL.total_seconds() // 60)}
        send_mail(
            _("Your sign-in link"),
            render_to_string("accounts/email/magic_link.txt", context),
            None,
            [user.email],
        )


def queue_magic_link(*, email: str, ip: str | None, origin: str) -> None:
    """Send the link after the current transaction commits (see module docstring)."""
    kwargs = {"email": email, "ip": ip, "origin": origin}
    if getattr(settings, "HOMEOA_JOBS_INLINE", False):
        transaction.on_commit(lambda: send_magic_link(**kwargs))
    else:
        transaction.on_commit(lambda: send_magic_link.defer(**kwargs))
