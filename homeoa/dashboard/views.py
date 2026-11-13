"""Home (board to-do list + Money card + recent activity) and Money > Overview."""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, QueryDict
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.reports import story
from homeoa.ui.nav import get_hub

from . import cards, todos, whats_next

ACTIVITY_LIMIT = 8
SETUP_TODO = "setup-progress"


def _subtabs(hub, slug):
    return [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == slug}
        for i, t in enumerate(hub.subtabs)
    ]


def _can(request, code) -> bool:
    return has_perm(request.user, code, association=request.association)


def _todo_context(request):
    association = request.association
    today = association_today(association)
    items = todos.collect(association, request.user)
    steps = whats_next.pending_steps(association, request.user, today)
    if steps:
        # The card already lists the setup sections, so their to-do would repeat it.
        items = [i for i in items if i.id != SETUP_TODO]
    return {
        "whats_next": steps,
        "today": today,
        "todo_count": len(items),
        "todo_groups": todos.grouped(items, today),
    }


def _money_context(request):
    association = request.association
    if not _can(request, perms.FINANCE_READ):
        return {}
    today = association_today(association)
    return {
        "money": cards.money_summary(association, today),
        "board": cards.board_cards(association, today, compliance=_can(request, perms.BOARD_SHELL)),
        "show_compliance": _can(request, perms.BOARD_SHELL),
    }


# Results worth surfacing above routine job audit rows (e.g. a day's delinquency sweep).
PINNED_ACTIONS = ("payments.autopay_collected",)
# A pinned result older than this is stale news and falls back to normal ordering.
PIN_MAX_AGE_DAYS = 35


def _activity(association):
    """Latest audit events; the newest of each recent pinned result always makes the list."""
    from homeoa.audit.models import AuditEvent

    if association is None:
        return []
    base = AuditEvent.objects.filter(association=association).exclude(object_type="")
    order = ("-created_at", "-pk")
    today = association_today(association)
    cutoff = today - datetime.timedelta(days=PIN_MAX_AGE_DAYS)
    pinned = []
    for action in PINNED_ACTIONS:
        ev = base.filter(action=action).order_by(*order).first()
        # No upper bound: with a pinned clock (HOMEOA_FIXED_TODAY) audit rows can be stamped
        # after "today" by the wall clock; those are the newest news, not stale.
        if ev is not None and cutoff <= _local_date(ev.created_at, association):
            pinned.append(ev)
    pinned_pks = [ev.pk for ev in pinned]
    rest = list(base.exclude(pk__in=pinned_pks).order_by(*order)[: ACTIVITY_LIMIT - len(pinned)])
    return sorted(pinned + rest, key=lambda ev: (ev.created_at, ev.pk), reverse=True)


def _local_date(moment, association):
    from contextlib import suppress
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz = datetime.UTC
    with suppress(ZoneInfoNotFoundError, ValueError, TypeError):
        tz = ZoneInfo(association.timezone)
    return moment.astimezone(tz).date()


@login_required
def index(request):
    if request.association is None:
        # A new install: nothing works until the System admin adds the association.
        if request.user.is_superuser:
            return redirect("new_association")
        return render(request, "core/not_set_up.html", {"current_hub": None, "subtabs": []})
    hub = get_hub("home")
    if not hub.is_visible(request):
        # Residents (and anyone without the board workspace) land on /me/.
        return redirect("portal:home")
    board = _can(request, perms.BOARD_SHELL)
    context = {
        "current_hub": hub,
        "subtabs": [],
        # Read-only roles (the CPA) get the figures only: no to-dos or activity.
        "show_todos": board,
        "today": association_today(request.association),
        "activity": _activity(request.association) if board else [],
        "can_manage": _can(request, perms.FINANCE_MANAGE),
        **(_todo_context(request) if board else {}),
        **_money_context(request),
    }
    return render(request, "dashboard/home.html", context)


@login_required
def todo_list(request):
    """The to-do card alone (htmx refresh)."""
    if not get_hub("home").is_visible(request):
        raise PermissionDenied
    return render(request, "dashboard/_todo_card.html", _todo_context(request))


@require_POST
@login_required
def todo_act(request, item_id: str):
    """Run an item's one-click action (``post_url`` + ``post_data``) inline.

    Only items the providers still list can be acted on, so the browser never chooses the
    target URL. The target view runs with its own permission checks; a redirect means it
    succeeded. Responds with the refreshed to-do card plus the Money card and messages
    out of band.
    """
    if not get_hub("home").is_visible(request):
        raise PermissionDenied
    item = next(
        (i for i in todos.collect(request.association, request.user) if i.id == item_id), None
    )
    if item is None:
        messages.info(request, _("That to-do is already done."))
    elif not item.post_url:
        raise Http404
    else:
        try:
            match = resolve(item.post_url)
        except Resolver404 as exc:
            raise Http404 from exc
        data = QueryDict(mutable=True)
        for key, value in item.post_data.items():
            data[key] = str(value)
        request.POST = data
        response = match.func(request, *match.args, **match.kwargs)
        if response.status_code not in (301, 302, 303):
            messages.error(
                request,
                _("%(title)s could not be done from Home. Open it to see why.")
                % {"title": item.title},
            )
    if not request.headers.get("HX-Request"):
        return redirect("dashboard:index")
    context = {**_todo_context(request), **_money_context(request), "oob": True}
    return render(request, "dashboard/_todo_act.html", context)


@require_perm(perms.FINANCE_READ)
def money_overview(request):
    hub = get_hub("money")
    association = request.association
    today = association_today(association)
    summary = cards.money_summary(association, today)
    sentences = story.money_sentences(association, today.replace(day=1), today)
    deposits = [
        i
        for i in todos.collect(association, request.user)
        if i.source == "banking" or i.id.startswith("bank-")
    ]
    return render(
        request,
        "dashboard/money.html",
        {
            "current_hub": hub,
            "subtabs": _subtabs(hub, "overview"),
            "heading": hub.label,
            "money": summary,
            "story": sentences,
            "deposits": deposits,
            "today": today,
        },
    )
