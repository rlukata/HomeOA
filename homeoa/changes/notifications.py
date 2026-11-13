"""Change-request emails.

* ``changes.awaiting_approval``: to each board member who may approve a new request.
* ``changes.decided``: to the proposer when the request is applied, fails or is rejected.

Internal workflow mail to the person's own login (not an association notice), sent after
the request's transaction commits.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext_lazy

from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

AWAITING_TYPE = "changes.awaiting_approval"
DECIDED_TYPE = "changes.decided"


def register_notification_types() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=AWAITING_TYPE,
            label=gettext_lazy("Change waiting for your approval"),
            default_channels=(ch.EMAIL, ch.INAPP),
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with s=summary %}"
                "Needs your approval: {{ s }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with s=summary p=proposer %}"
                "{{ p }} proposed a change: {{ s }}. It takes effect once the board approves it."
                "{% endblocktranslate %}\n\n{{ url }}"
            ),
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            key=DECIDED_TYPE,
            label=gettext_lazy("Your change was approved or rejected"),
            default_channels=(ch.EMAIL, ch.INAPP),
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with s=summary st=status %}"
                "{{ st }}: {{ s }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with s=summary st=status d=decider %}"
                "{{ s }}: {{ st }} ({{ d }}).{% endblocktranslate %}"
                "{% if reason %}\n{% blocktranslate with r=reason %}Reason: {{ r }}"
                "{% endblocktranslate %}{% endif %}\n\n{{ url }}"
            ),
        )
    )


def _context(change) -> dict:
    url = getattr(settings, "HOMEOA_BASE_URL", "") + reverse("changes:detail", args=[change.pk])
    return {"summary": change.summary, "url": url, "association_name": change.association.name}


def _name(user) -> str:
    return (getattr(user, "full_name", "") or getattr(user, "email", "")) if user else ""


def _send(type_key, users, context, *, change, period) -> None:
    from homeoa.ap.approvals.notifications import person_for
    from homeoa.notify.services import notify_after_commit

    association = change.association
    people = [person_for(u, association) for u in users]
    notify_after_commit(
        type_key, people, context, obj=change, period=period, association=association
    )


def notify_approvers(change, users) -> None:
    users = [u for u in users if u is not None]
    if users:
        context = {**_context(change), "proposer": _name(change.proposed_by)}
        _send(AWAITING_TYPE, users, context, change=change, period=f"change:{change.pk}")


def notify_proposer(change, *, decider=None, reason: str = "") -> None:
    proposer = change.proposed_by
    if proposer is None or not proposer.is_active:
        return
    context = {
        **_context(change),
        "status": str(change.get_status_display()),
        "decider": _name(decider),
        "reason": reason,
    }
    _send(DECIDED_TYPE, [proposer], context, change=change, period=f"change-done:{change.pk}")
