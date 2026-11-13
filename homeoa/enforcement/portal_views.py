"""Resident violation pages (VIO-06): ``/me/violations/<pk>/`` for the unit's owners and
tenants. Owners also get fines and the notice letters."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import resident_view
from homeoa.documents.models import Document

from . import responses
from .models import ResponsePhoto, ViolationPhoto, ViolationResponse, ViolationStep


def _violation(request, pk):
    return get_object_or_404(responses.visible_to(request.user, request.association), pk=pk)


@resident_view(perm=perms.VIOLATIONS_OWN)
def detail(request, pk):
    from homeoa.portal.views.base import _render

    v = _violation(request, pk)
    owner = responses.is_owner(request.user, v)
    return _render(
        request,
        "requests",
        "enforcement/me_detail.html",
        v=v,
        owner=owner,
        photos=v.photos.select_related("document"),
        steps=v.steps.select_related("letter"),
        hearings=v.hearings.all(),
        responses=v.responses.select_related("person").prefetch_related("photos"),
        can_respond=responses.can_respond(v),
        kinds=ViolationResponse.Kind.choices,
    )


@require_POST
@resident_view(perm=perms.VIOLATIONS_OWN)
def respond(request, pk):
    from homeoa.portal.views.mine import my_person

    v = _violation(request, pk)
    photos = [(f.name, f.read()) for f in request.FILES.getlist("photos")]
    try:
        responses.respond(
            v,
            user=request.user,
            person=my_person(request),
            kind=request.POST.get("kind", ""),
            message=request.POST.get("message", ""),
            photos=photos,
        )
        messages.success(request, _("Sent to the board."))
    except ValidationError as exc:
        for m in exc.messages:
            messages.error(request, m)
    return redirect("violations_portal:detail", pk=v.pk)


@resident_view(perm=perms.VIOLATIONS_OWN)
def photo(request, pk, doc):
    """A photo of this violation or of a response to it, whoever's unit it is."""
    from homeoa.documents.services import read_file

    v = _violation(request, pk)
    ok = (
        ViolationPhoto.all_associations.filter(violation=v, document_id=doc).exists()
        or ResponsePhoto.all_associations.filter(response__violation=v, document_id=doc).exists()
    )
    if not ok:
        raise Http404
    version = Document.all_associations.get(pk=doc).current_version
    if version is None:
        raise Http404
    resp = HttpResponse(read_file(version), content_type=version.content_type)
    resp["Content-Disposition"] = "inline"
    resp["Cache-Control"] = "private, no-store"
    return resp


@resident_view(perm=perms.VIOLATIONS_OWN)
def letter(request, pk, step):
    """A notice letter about this violation; owners only (it is addressed to them)."""
    from homeoa.letters.services import letter_bytes

    v = _violation(request, pk)
    if not responses.is_owner(request.user, v):
        raise Http404
    s = get_object_or_404(ViolationStep.objects.select_related("letter"), violation=v, pk=step)
    if s.letter is None:
        raise Http404
    resp = HttpResponse(letter_bytes(s.letter), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="violation-notice-{s.pk}.pdf"'
    return resp
