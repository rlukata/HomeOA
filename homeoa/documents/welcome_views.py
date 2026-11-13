"""Welcome packet pages: Documents > Welcome packet (board) and More > Welcome packet
(owners)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.core.context import association_today

from . import services as docs
from . import welcome
from .library_views import _hub_render


class MessageForm(forms.Form):
    message = forms.CharField(
        label=gettext_lazy("Welcome message"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text=gettext_lazy(
            "Shown at the top of the Welcome page, above the documents and the board list."
        ),
    )


@require_perm(perms.DOCUMENTS_BOARD)
def board(request):
    association = request.association
    can_manage = docs.can_manage(request.user, association)
    form = MessageForm(
        request.POST or None, initial={"message": welcome.packet(association).message}
    )
    if request.method == "POST":
        if not can_manage:
            return redirect("documents:welcome")
        if form.is_valid():
            welcome.set_message(association, form.cleaned_data["message"], user=request.user)
            messages.success(request, _("Welcome message saved."))
            return redirect("documents:welcome")
    return _hub_render(
        request,
        "documents/welcome.html",
        {"form": form, "documents": list(welcome.documents(association))},
        tab="welcome",
    )


@resident_view(perm=perms.BALANCE_OWN)
def portal(request):
    from homeoa.governance import contacts
    from homeoa.portal.views.base import _render

    association = request.association
    welcome.mark_seen(request.user, association)
    return _render(
        request,
        "more",
        "documents/portal_welcome.html",
        message=welcome.packet(association).message,
        documents=list(welcome.documents(association)),
        **contacts.directory(association, association_today(association)),
    )
