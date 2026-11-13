"""Who serves, and how residents reach them.

Owners and tenants see the board (name and office) and each active committee (names, the
chair), and send a message to the board, a committee or the system admin through a form.
Members' emails and phones are never shown; the message is emailed to them with the
sender's name and email so they can reply, and the board sees every message to the board
and committees in Community > Messages.
"""

from __future__ import annotations

import datetime

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.services import role_holders
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from . import services
from .models import Committee, ContactMessage

MESSAGE_TYPE = "governance.contact_message"


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=MESSAGE_TYPE,
            label=gettext_lazy("Message from a resident or member"),
            default_channels=(ch.EMAIL, ch.INAPP),
            # Internal mail to the board, committee members or the system admin.
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with t=to s=subject %}"
                "[{{ t }}] {{ s }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=sender_name e=sender_email t=to %}"
                "{{ n }} ({{ e }}) wrote to {{ t }}:{% endblocktranslate %}\n\n{{ body }}\n\n"
                "{% blocktranslate %}Reply by email to answer them.{% endblocktranslate %}"
                "{% if url %}\n{{ url }}{% endif %}"
            ),
        )
    )


def directory(association, on: datetime.date) -> dict:
    """The board (officers first) and active committees with their members."""
    return {
        "board": services.roster(association, on),
        "committees": [c for c in services.committees(association, on) if c["members"]],
    }


class ContactForm(forms.Form):
    to = forms.ChoiceField(label=gettext_lazy("To"))
    subject = forms.CharField(label=gettext_lazy("Subject"), max_length=150)
    body = forms.CharField(
        label=gettext_lazy("Message"), max_length=5000, widget=forms.Textarea(attrs={"rows": 6})
    )

    def __init__(self, *args, association=None, on=None, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(ContactMessage.To.BOARD, _("The board"))]
        for c in directory(association, on)["committees"]:
            choices.append((f"committee:{c['committee'].pk}", str(c["committee"])))
        choices.append((ContactMessage.To.SYS_ADMIN, _("The system admin (the app itself)")))
        self.fields["to"].choices = choices


def _recipient_users(association, to: str, committee, on) -> list:
    users = get_user_model().objects
    if to == ContactMessage.To.SYS_ADMIN:
        assigned = role_holders(association, perms.SYS_ADMIN, on)
        supers = list(users.filter(is_superuser=True, is_active=True))
        return list({u.pk: u for u in [*assigned, *supers]}.values())
    return []


def _recipient_people(association, to: str, committee, on) -> list:
    if to == ContactMessage.To.BOARD:
        return [t.person for t in services.roster(association, on)]
    if to == ContactMessage.To.COMMITTEE and committee is not None:
        for c in services.committees(association, on):
            if c["committee"].pk == committee.pk:
                return [m.person for m in c["members"]]
    return []


def send(association, form: ContactForm, *, user, on: datetime.date) -> ContactMessage:
    from homeoa.ap.approvals.notifications import person_for
    from homeoa.notify.services import notify_after_commit

    raw = form.cleaned_data["to"]
    committee = None
    to = raw
    if raw.startswith("committee:"):
        to = ContactMessage.To.COMMITTEE
        committee = Committee.all_associations.get(association=association, pk=int(raw[10:]))
    people = _recipient_people(association, to, committee, on)
    people += [person_for(u, association) for u in _recipient_users(association, to, committee, on)]
    people = list({p.pk: p for p in people if p is not None}.values())
    message = ContactMessage.all_associations.create(
        association=association,
        sender=user,
        to=to,
        committee=committee,
        subject=form.cleaned_data["subject"],
        body=form.cleaned_data["body"],
        recipient_count=len(people),
    )
    url = ""
    if to != ContactMessage.To.SYS_ADMIN:
        url = getattr(settings, "HOMEOA_BASE_URL", "") + reverse("contacts:inbox")
    notify_after_commit(
        MESSAGE_TYPE,
        people,
        {
            "to": message.to_label,
            "subject": message.subject,
            "body": message.body,
            "sender_name": user.full_name or user.email,
            "sender_email": user.email,
            "url": url,
            "association_name": association.name,
        },
        obj=message,
        period=f"message:{message.pk}",
        association=association,
    )
    return message
