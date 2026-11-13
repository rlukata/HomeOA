"""Owner records requests: Documents > Records requests for the board, More > Records
requests for owners."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.ui.nav import get_hub

from . import records, services
from .forms import RecordsRequestForm
from .models import RecordsRequest


class OwnerRequestForm(forms.Form):
    what = forms.CharField(
        label=gettext_lazy("Which records?"),
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=gettext_lazy(
            "For example: board minutes from 2025, or the latest bank statements."
        ),
    )
    delivery = forms.ChoiceField(
        label=gettext_lazy("How do you want them?"),
        choices=RecordsRequest.Delivery.choices,
        initial=RecordsRequest.Delivery.COPIES,
        widget=forms.RadioSelect,
    )


def _board_page(request, *, form=None, status=200):
    association = request.association
    hub = get_hub("documents")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "records-requests"}
        for i, t in enumerate(hub.subtabs)
    ]
    response = render(
        request,
        "registry/records_requests.html",
        {
            "current_hub": hub,
            "subtabs": tabs,
            "requests": RecordsRequest.objects.select_related("person"),
            "request_form": form
            or RecordsRequestForm(initial={"received_on": services.today_for(association)}),
        },
    )
    response.status_code = status
    return response


@require_perm(perms.UNITS_MANAGE)
def board(request):
    return _board_page(request)


@require_POST
@require_perm(perms.UNITS_MANAGE)
def add(request):
    form = RecordsRequestForm(request.POST)
    if not form.is_valid():
        return _board_page(request, form=form, status=400)
    req = records.log_request(
        request.association,
        received_on=form.cleaned_data["received_on"],
        person=form.cleaned_data["person"],
        requester_name=form.cleaned_data["requester_name"],
        note=form.cleaned_data["note"],
    )
    if req.due_on:
        messages.success(
            request,
            _("Records request logged. Due %(date)s.") % {"date": req.due_on.isoformat()},
        )
    else:
        messages.success(request, _("Records request logged."))
    return redirect("registry:records_requests")


@require_POST
@require_perm(perms.UNITS_MANAGE)
def done(request, pk):
    req = get_object_or_404(RecordsRequest.objects, pk=pk)
    records.mark_fulfilled(req, services.today_for(req.association))
    messages.success(request, _("Records request marked as fulfilled."))
    return redirect("registry:records_requests")


@resident_view(perm=perms.BALANCE_OWN)
def portal(request):
    from homeoa.portal.views.base import _render
    from homeoa.portal.views.mine import my_person

    person = my_person(request)
    form = OwnerRequestForm(request.POST or None)
    if request.method == "POST" and person is not None and form.is_valid():
        req = records.submit(
            request.association,
            person,
            what=form.cleaned_data["what"],
            delivery=form.cleaned_data["delivery"],
            on=services.today_for(request.association),
        )
        if req.due_on:
            messages.success(
                request,
                _("Request sent to the board. The records are due by %(date)s.")
                % {"date": req.due_on.isoformat()},
            )
        else:
            messages.success(request, _("Request sent to the board."))
        return redirect("records_portal:index")
    mine = RecordsRequest.objects.filter(person=person) if person else RecordsRequest.objects.none()
    response = _render(
        request, "more", "registry/portal_records.html", form=form, requests=list(mine)
    )
    if request.method == "POST":
        response.status_code = 400
    return response
