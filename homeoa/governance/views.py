from __future__ import annotations

import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.providers import get_provider
from homeoa.registry.models import Person

from . import services
from .models import ActionItem, BoardTerm, Committee, Office, Resolution, Visibility, Vote


def _can_manage(request) -> bool:
    return has_perm(request.user, perms.GOVERNANCE_MANAGE, association=request.association)


def _manage(request) -> None:
    if not _can_manage(request):
        raise PermissionDenied


def _date(value, *, required=True) -> datetime.date | None:
    d = parse_date(value or "") if value else None
    if required and d is None:
        raise services.GovernanceError(_("Enter a valid date."))
    return d


def _person(request, field="person") -> Person:
    try:
        return Person.objects.get(pk=int(request.POST.get(field, "")))
    except ValueError, Person.DoesNotExist:
        raise services.GovernanceError(_("Choose a person.")) from None


def _error(request, exc) -> None:
    if isinstance(exc, ValidationError):
        messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, str(exc))


@require_perm(perms.GOVERNANCE_READ)
def index(request):
    a = request.association
    today = association_today(a)
    roster = services.roster(a, today)
    action_items = ActionItem.objects.select_related("owner", "resolution").order_by(
        "status", "due_date", "pk"
    )
    return render(
        request,
        "governance/index.html",
        {
            "today": today,
            "roster": roster,
            "terms": BoardTerm.objects.select_related("person").order_by("-start", "pk"),
            "committees": services.committees(a, today),
            "resolutions": Resolution.objects.all(),
            "open_items": [i for i in action_items if i.status == ActionItem.Status.OPEN],
            "closed_items": [i for i in action_items if i.status != ActionItem.Status.OPEN],
            "people": Person.objects.order_by("full_name"),
            "board_people": [t.person for t in roster],
            "offices": Office.choices,
            "can_manage": _can_manage(request),
        },
    )


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def term_add(request):
    _manage(request)
    try:
        services.add_term(
            request.association,
            person=_person(request),
            office=request.POST.get("office", ""),
            start=_date(request.POST.get("start")),
            end=_date(request.POST.get("end"), required=False),
            note=request.POST.get("note", "")[:200],
        )
        messages.success(request, _("Board term added."))
    except (services.GovernanceError, ValidationError) as exc:
        _error(request, exc)
    return redirect("governance:index")


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def term_end(request, pk):
    _manage(request)
    term = get_object_or_404(BoardTerm.objects, pk=pk)
    try:
        term.end = _date(request.POST.get("end"))
        term.full_clean(exclude=["association"])
        term.save()
        messages.success(request, _("Term end date saved."))
    except (services.GovernanceError, ValidationError) as exc:
        _error(request, exc)
    return redirect("governance:index")


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def committee_add(request):
    _manage(request)
    name = request.POST.get("name", "").strip()[:120]
    if not name:
        messages.error(request, _("Give the committee a name."))
    elif Committee.objects.filter(name=name).exists():
        messages.error(request, _("A committee with this name already exists."))
    else:
        Committee.objects.create(
            association=request.association,
            name=name,
            purpose=request.POST.get("purpose", "").strip()[:300],
        )
        messages.success(request, _("Committee added."))
    return redirect("governance:index")


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def member_add(request, pk):
    _manage(request)
    committee = get_object_or_404(Committee.objects, pk=pk)
    try:
        services.add_committee_member(
            committee,
            person=_person(request),
            start=_date(request.POST.get("start")),
            end=_date(request.POST.get("end"), required=False),
            is_chair=request.POST.get("is_chair") == "on",
        )
        messages.success(request, _("Committee member added."))
    except (services.GovernanceError, ValidationError) as exc:
        _error(request, exc)
    return redirect("governance:index")


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def resolution_add(request):
    _manage(request)
    title = request.POST.get("title", "").strip()[:200]
    if not title:
        messages.error(request, _("Give the resolution a title."))
        return redirect("governance:index")
    r = services.create_resolution(
        request.association, title=title, text=request.POST.get("text", ""), user=request.user
    )
    messages.success(request, _("Draft resolution %(number)s created.") % {"number": r.number})
    return redirect("governance:resolution", pk=r.pk)


@require_perm(perms.GOVERNANCE_READ)
def resolution(request, pk):
    r = get_object_or_404(Resolution.objects, pk=pk)
    a = request.association
    today = association_today(a)
    votes = {v.person_id: v.vote for v in r.votes.select_related("person")}
    # One row per person, even when someone holds two current terms (two offices).
    voters, seen = [], set()
    roster_people = [t.person for t in services.roster(a, r.adopted_on or today)]
    for p in roster_people + [v.person for v in r.votes.select_related("person")]:
        if p.pk not in seen:
            seen.add(p.pk)
            voters.append(p)
    return render(
        request,
        "governance/resolution.html",
        {
            "r": r,
            "today": today,
            "voters": [
                {
                    "person": p,
                    "vote": votes.get(p.pk, ""),
                    "label": Vote(votes[p.pk]).label if votes.get(p.pk) else "",
                }
                for p in voters
            ],
            "tally": services.tally(r),
            "vote_choices": Vote.choices,
            "visibilities": Visibility.choices,
            "action_items": r.action_items.select_related("owner"),
            "can_manage": _can_manage(request),
        },
    )


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def resolution_act(request, pk):
    _manage(request)
    r = get_object_or_404(Resolution.objects, pk=pk)
    action = request.POST.get("action", "")
    try:
        if action == "edit":
            services.update_draft(
                r,
                title=request.POST.get("title", "").strip()[:200] or r.title,
                text=request.POST.get("text", ""),
            )
            messages.success(request, _("Draft saved."))
        elif action == "votes":
            votes = {}
            for key, value in request.POST.items():
                if key.startswith("vote-"):
                    person = get_object_or_404(Person.objects, pk=key.removeprefix("vote-"))
                    votes[person] = value
            services.record_votes(r, votes)
            messages.success(request, _("Votes recorded."))
        elif action == "adopt":
            services.adopt(
                r,
                adopted_on=_date(request.POST.get("adopted_on")),
                pdf=request.FILES.get("pdf"),
                visibility=request.POST.get("visibility", Visibility.OWNERS),
                user=request.user,
            )
            messages.success(request, _("Resolution adopted."))
        elif action == "rescind":
            services.rescind(
                r,
                on=_date(request.POST.get("on")),
                reason=request.POST.get("reason", ""),
                user=request.user,
            )
            messages.success(request, _("Resolution rescinded."))
        else:
            raise Http404
    except services.GovernanceError as exc:
        _error(request, exc)
    return redirect("governance:resolution", pk=r.pk)


@require_perm(perms.GOVERNANCE_READ)
def resolution_pdf(request, pk):
    r = get_object_or_404(Resolution.objects, pk=pk)
    if not r.pdf_key:
        raise Http404
    data = get_provider("FileStorage", request.association).get(r.pdf_key)
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="resolution-{r.number}.pdf"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def action_add(request):
    _manage(request)
    title = request.POST.get("title", "").strip()[:200]
    resolution = None
    if request.POST.get("resolution"):
        resolution = get_object_or_404(Resolution.objects, pk=request.POST["resolution"])
    try:
        if not title:
            raise services.GovernanceError(_("Give the action item a title."))
        services.add_action_item(
            request.association,
            title=title,
            owner=_person(request, "owner"),
            due_date=_date(request.POST.get("due_date"), required=False),
            resolution=resolution,
        )
        messages.success(request, _("Action item added."))
    except services.GovernanceError as exc:
        _error(request, exc)
    if resolution is not None:
        return redirect("governance:resolution", pk=resolution.pk)
    return redirect("governance:index")


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def action_status(request, pk):
    item = get_object_or_404(ActionItem.objects.select_related("owner"), pk=pk)
    is_owner = item.owner.user_id is not None and item.owner.user_id == request.user.pk
    if not (is_owner or _can_manage(request)):
        raise PermissionDenied
    try:
        services.set_action_status(
            item, request.POST.get("status", ""), on=association_today(request.association)
        )
        messages.success(request, _("Action item updated."))
    except services.GovernanceError as exc:
        _error(request, exc)
    return redirect("governance:index")
