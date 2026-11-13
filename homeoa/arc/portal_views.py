"""Resident pages (/me/arc/): owners submit and follow architectural requests.

Only current owners may submit (tenants hold no ``architectural.submit``). An owner sees
the requests of the units they own now, with the comments marked visible to them; the
reviewers' internal comments and votes are never shown here.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import resident_view
from homeoa.portal.views.base import _render
from homeoa.registry.models import Person
from homeoa.registry.services import units_owned_by

from . import services
from .models import ArcForm, ArcRequest
from .views import field_rows, file_response, files_from, form_choice


def _owned(request) -> tuple[list[Person], list]:
    people = list(Person.objects.filter(user=request.user))
    units = {u.pk: u for p in people for u in units_owned_by(p)}
    return people, sorted(units.values(), key=lambda u: u.sort_key)


def _mine(request, pk) -> ArcRequest:
    _people, units = _owned(request)
    req = ArcRequest.objects.filter(pk=pk, unit__in=units).select_related("unit", "form").first()
    if req is None:
        raise Http404
    return req


@resident_view(perm=perms.ARCH_SUBMIT)
def mine(request):
    _people, units = _owned(request)
    reqs = ArcRequest.objects.filter(unit__in=units).select_related("unit")
    return _render(request, "requests", "arc/me_list.html", reqs=reqs, owned_units=units)


@resident_view(perm=perms.ARCH_SUBMIT)
def new(request):
    people, units = _owned(request)
    if not units:
        raise PermissionDenied
    services.ensure_default_form(request.association)
    form = form_choice(request)
    errors, values = {}, {}
    unit = units[0] if len(units) == 1 else None
    pk = request.POST.get("unit") or request.GET.get("unit")
    if pk:
        unit = next((u for u in units if str(u.pk) == str(pk)), unit)
    if request.method == "POST" and request.POST.get("submit") and form:
        values = request.POST
        owner = next((p for p in people if unit is not None and unit in units_owned_by(p)), None)
        if unit is None or owner is None:
            messages.error(request, _("Choose your unit."))
        else:
            try:
                req = services.submit(
                    request.association,
                    unit=unit,
                    person=owner,
                    form=form,
                    title=request.POST.get("title", ""),
                    data=request.POST,
                    files=files_from(request),
                    user=request.user,
                )
                messages.success(request, _("Your request was sent to the board."))
                return redirect("arc:mine_detail", req.pk)
            except services.ArcError as exc:
                errors = getattr(exc, "errors", {})
                messages.error(request, str(exc))
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
    return _render(
        request,
        "requests",
        "arc/me_new.html",
        forms=ArcForm.objects.filter(active=True),
        form=form,
        owned_units=units,
        unit=unit,
        rows=field_rows(form, values, errors),
        title_error=errors.get("title", ""),
        values=values,
        action_url=request.path,
    )


@resident_view(perm=perms.ARCH_SUBMIT)
def detail(request, pk):
    req = _mine(request, pk)
    return _render(
        request,
        "requests",
        "arc/me_detail.html",
        req=req,
        comments=req.comments.filter(internal=False),
        attachments=req.attachments.select_related("version"),
    )


@require_POST
@resident_view(perm=perms.ARCH_SUBMIT)
def act(request, pk):
    req = _mine(request, pk)
    action = request.POST.get("action", "")
    try:
        if action == "reply":
            services.provide_info(req, message=request.POST.get("body"), user=request.user)
            messages.success(request, _("Your reply was sent to the board."))
        elif action == "withdraw":
            services.withdraw(req, user=request.user)
            messages.success(request, _("Request withdrawn."))
    except services.ArcError as exc:
        messages.error(request, str(exc))
    return redirect("arc:mine_detail", req.pk)


@resident_view(perm=perms.ARCH_SUBMIT)
def file(request, pk, att):
    req = _mine(request, pk)
    return file_response(get_object_or_404(req.attachments.select_related("version"), pk=att))


@resident_view(perm=perms.ARCH_SUBMIT)
def letter(request, pk):
    """The decision letter of one of the owner's requests."""
    from django.http import HttpResponse

    from homeoa.letters.services import letter_bytes

    req = _mine(request, pk)
    if req.decision_letter_id is None:
        raise Http404
    resp = HttpResponse(letter_bytes(req.decision_letter), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="arc-decision-{req.pk}.pdf"'
    return resp
