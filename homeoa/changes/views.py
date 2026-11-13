"""Governance > Change requests: the board's queue, and each proposer's own requests."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import is_board_member

from . import kinds, services
from .models import ChangeRequest
from .replay import change_view

RECENT = 30


def _association(request):
    association = getattr(request, "association", None)
    if association is None:
        raise Http404
    return association


def _visible(request):
    """Board members see every request; anyone else only the ones they proposed."""
    association = _association(request)
    qs = ChangeRequest.all_associations.filter(association=association).select_related(
        "proposed_by"
    )
    if is_board_member(request.user, association):
        return qs
    return qs.filter(proposed_by=request.user)


def _decorate(changes, user):
    for c in changes:
        c.kind_label = kinds.label(c.kind)
        c.mine = services.can_decide(c, user)
    return changes


@login_required
def index(request):
    qs = _visible(request)
    pending = _decorate(list(qs.filter(status=ChangeRequest.Status.PENDING)), request.user)
    closed = _decorate(list(qs.exclude(status=ChangeRequest.Status.PENDING)[:RECENT]), request.user)
    return render(
        request,
        "changes/index.html",
        {
            "waiting": [c for c in pending if c.mine],
            "pending": [c for c in pending if not c.mine],
            "closed": closed,
            "on_board": is_board_member(request.user, request.association),
        },
    )


def _change(request, pk) -> ChangeRequest:
    return get_object_or_404(_visible(request), pk=pk)


@login_required
def detail(request, pk):
    change = _change(request, pk)
    change.kind_label = kinds.label(change.kind)
    return render(
        request,
        "changes/detail.html",
        {
            "change": change,
            "decisions": list(change.decisions.select_related("user")),
            "can_decide": services.can_decide(change, request.user),
            "can_withdraw": change.is_pending and change.proposed_by_id == request.user.pk,
            "waiting_on": services.waiting_on(change),
        },
    )


@login_required
@require_POST
def approve(request, pk):
    change = _change(request, pk)
    try:
        change = services.approve(change, request.user, request.POST.get("comment", ""))
    except PermissionDenied:
        messages.error(request, _("You can't approve this change."))
        return redirect("changes:detail", pk=pk)
    if change.status == ChangeRequest.Status.APPLIED:
        messages.success(request, _("Approved. The change has been made."))
    elif change.status == ChangeRequest.Status.FAILED:
        messages.error(
            request,
            _("Approved, but the change could not be made: %(error)s") % {"error": change.error},
        )
    else:
        messages.success(request, _("Approved. It needs one more approval."))
    return redirect("changes:detail", pk=pk)


@login_required
@require_POST
def reject(request, pk):
    change = _change(request, pk)
    try:
        services.reject(change, request.user, request.POST.get("comment", ""))
    except PermissionDenied:
        messages.error(request, _("You can't reject this change."))
    except services.ChangeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Rejected. Nothing was changed."))
    return redirect("changes:detail", pk=pk)


@login_required
@require_POST
def withdraw(request, pk):
    change = _change(request, pk)
    try:
        services.withdraw(change, request.user)
    except PermissionDenied:
        messages.error(request, _("Only the person who proposed it can withdraw it."))
    else:
        messages.success(request, _("Withdrawn. Nothing was changed."))
    return redirect("changes:detail", pk=pk)


class LimitsForm(forms.Form):
    oneoff_limit = forms.DecimalField(
        label=_lazy("One-off maintenance limit"),
        min_value=0,
        max_digits=12,
        decimal_places=2,
        help_text=_lazy(
            "The board or the Maintenance committee can commission a one-off job estimated "
            "at this amount or less without asking the board first."
        ),
    )
    reason = forms.CharField(label=_lazy("Reason"), max_length=300)


@change_view(
    "policy.limits",
    _lazy("Change the approval limits"),
    manage=perms.POLICY_PROPOSE,
    propose=perms.POLICY_PROPOSE,
    codes={perms.POLICY_PROPOSE},
    always=True,
)
@require_perm(perms.POLICY_PROPOSE)
def limits(request):
    """Settings > Approval limits. Changing them is itself a change request."""
    from homeoa.maintenance import oneoff

    association = _association(request)
    current = oneoff.limit(association)
    form = LimitsForm(request.POST or None, initial={"oneoff_limit": current})
    if request.method == "POST" and form.is_valid():
        oneoff.set_limit(
            association, form.cleaned_data["oneoff_limit"], reason=form.cleaned_data["reason"]
        )
        messages.success(request, _("Approval limits saved."))
        return redirect("changes:limits")
    return render(
        request,
        "changes/limits.html",
        {"current_hub": None, "subtabs": [], "form": form, "current": current},
        status=400 if request.method == "POST" else 200,
    )
