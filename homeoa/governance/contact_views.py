"""Board & committees contact pages: the resident page under More, the same form
from Settings for board and committee members, and the board's inbox (Community >
Messages)."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.core.context import association_today

from . import contacts
from .models import ContactMessage

INBOX_LIMIT = 100


def _handle(request, redirect_to):
    association = request.association
    today = association_today(association)
    form = contacts.ContactForm(request.POST or None, association=association, on=today)
    if request.method == "POST" and form.is_valid():
        message = contacts.send(association, form, user=request.user, on=today)
        messages.success(
            request,
            _("Message sent to %(to)s. They will reply to %(email)s.")
            % {"to": message.to_label, "email": request.user.email},
        )
        return None, redirect(redirect_to)
    return form, None


@resident_view(perm=perms.CONTACTS_READ)
def portal_contacts(request):
    from homeoa.portal.views.base import _render

    form, done = _handle(request, "contacts:portal")
    if done:
        return done
    return _render(
        request,
        "more",
        "governance/portal_contacts.html",
        form=form,
        **contacts.directory(request.association, association_today(request.association)),
    )


@require_perm(perms.CONTACTS_READ)
def board_contact(request):
    form, done = _handle(request, "contacts:board")
    if done:
        return done
    return render(
        request,
        "governance/contact.html",
        {
            "current_hub": None,
            "subtabs": [],
            "form": form,
            **contacts.directory(request.association, association_today(request.association)),
        },
    )


@require_perm(perms.GOVERNANCE_READ)
def inbox(request):
    rows = ContactMessage.objects.exclude(to=ContactMessage.To.SYS_ADMIN).select_related(
        "sender", "committee"
    )[:INBOX_LIMIT]
    return render(request, "governance/inbox.html", {"rows": rows})
