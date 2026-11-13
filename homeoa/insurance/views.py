"""Documents > Insurance: the master policy above the insurance documents."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import today_for
from homeoa.documents import services as docs
from homeoa.documents.models import Category
from homeoa.ui.nav import get_hub

from . import services
from .forms import PolicyForm
from .models import MasterPolicy


def _render(request, template, context, status=200):
    hub = get_hub("documents")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "insurance"}
        for i, t in enumerate(hub.subtabs)
    ]
    response = render(
        request,
        template,
        {
            "current_hub": hub,
            "subtabs": tabs,
            "can_manage": services.can_manage(request.user, request.association),
            **context,
        },
    )
    response.status_code = status
    return response


@require_perm(perms.DOCUMENTS_BOARD)
def index(request):
    association = request.association
    policies = list(MasterPolicy.objects.select_related("certificate"))
    return _render(
        request,
        "insurance/index.html",
        {
            "policy": services.current(association, today_for(association)),
            "policies": policies,
            "documents": docs.visible_documents(
                request.user, association, category=Category.INSURANCE
            ),
        },
    )


def _edit(request, policy):
    association = request.association
    if not services.can_manage(request.user, association):
        raise PermissionDenied
    form = PolicyForm(request.POST or None, instance=policy, association=association)
    if request.method == "POST" and form.is_valid():
        form.instance.association = association
        services.save(form.instance)
        messages.success(request, _("Policy saved."))
        return redirect("insurance:index")
    status = 400 if request.method == "POST" else 200
    return _render(request, "insurance/form.html", {"form": form, "editing": policy}, status)


@require_perm(perms.DOCUMENTS_MANAGE)
def add(request):
    return _edit(request, None)


@require_perm(perms.DOCUMENTS_MANAGE)
def edit(request, pk):
    return _edit(request, get_object_or_404(MasterPolicy.objects, pk=pk))
