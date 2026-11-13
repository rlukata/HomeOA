"""Community hub (board): announcements and calendar; resident page; iCal feed.

Board pages need ``announcements.manage`` / ``calendar.manage``. The resident page needs
``calendar.read`` (owners and tenants). The ``.ics`` feed is opened by calendar
apps without a session: its secret token is the only credential.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.ui.nav import get_hub

from . import services
from .forms import AnnouncementForm, CalendarEventForm
from .models import Announcement, CalendarEvent


def _render(request, template, context, tab):
    hub = get_hub("community", request.association)
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == tab}
        for i, t in enumerate(hub.subtabs)
    ]
    return render(request, template, {"current_hub": hub, "subtabs": tabs, **context})


def _feed_url(request, token) -> str:
    return request.build_absolute_uri(reverse("community:ical", args=[token.token]))


# --- announcements --------------------------------------------------------------------


@require_perm(perms.ANNOUNCEMENTS_MANAGE)
def announcements(request):
    today = association_today(request.association)
    rows = list(Announcement.objects.select_related("created_by"))
    for a in rows:
        a.current_state = a.state(today)
    return _render(
        request, "community/announcements.html", {"rows": rows, "today": today}, "announcements"
    )


@require_perm(perms.ANNOUNCEMENTS_MANAGE)
def announcement_edit(request, pk=None):
    obj = get_object_or_404(Announcement, pk=pk) if pk else None
    initial = {} if obj else {"publish_on": association_today(request.association)}
    form = AnnouncementForm(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        ann = services.save_announcement(form.instance, request.user)
        messages.success(request, _("Announcement saved."))
        if ann.notified_on and (obj is None or not obj.notified_on):
            messages.info(request, _("It is being sent to the audience."))
        return redirect("community:announcements")
    title = _("Edit announcement") if obj else _("New announcement")
    return _render(
        request,
        "community/form.html",
        {
            "form": form,
            "title": title,
            "obj": obj,
            "cancel": reverse("community:announcements"),
            "delete_url": reverse("community:announcement_delete", args=[pk]) if obj else "",
        },
        "announcements",
    )


@require_POST
@require_perm(perms.ANNOUNCEMENTS_MANAGE)
def announcement_delete(request, pk):
    get_object_or_404(Announcement, pk=pk).delete()
    messages.success(request, _("Announcement deleted."))
    return redirect("community:announcements")


# --- calendar -------------------------------------------------------------------------


@require_perm(perms.CALENDAR_MANAGE)
def calendar(request):
    assoc = request.association
    today = association_today(assoc)
    rows = services.upcoming(assoc, services.audiences_for(request.user, assoc), today, days=365)
    past = CalendarEvent.objects.filter(start_date__lt=today).exclude(end_date__gte=today)
    token = services.feed_token(request.user, assoc, create=False)
    return _render(
        request,
        "community/calendar.html",
        {
            "rows": rows,
            "past": past.order_by("-start_date")[:10],
            "feed_url": _feed_url(request, token) if token else "",
        },
        "calendar",
    )


@require_perm(perms.CALENDAR_MANAGE)
def event_edit(request, pk=None):
    obj = get_object_or_404(CalendarEvent, pk=pk, source=CalendarEvent.MANUAL) if pk else None
    form = CalendarEventForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Event saved."))
        return redirect("community:calendar")
    title = _("Edit event") if obj else _("New event")
    return _render(
        request,
        "community/form.html",
        {
            "form": form,
            "title": title,
            "obj": obj,
            "cancel": reverse("community:calendar"),
            "delete_url": reverse("community:event_delete", args=[pk]) if obj else "",
        },
        "calendar",
    )


@require_POST
@require_perm(perms.CALENDAR_MANAGE)
def event_delete(request, pk):
    get_object_or_404(CalendarEvent, pk=pk, source=CalendarEvent.MANUAL).delete()
    messages.success(request, _("Event deleted."))
    return redirect("community:calendar")


# --- resident page and feed settings ----------------------------------------------------


@resident_view(perm=perms.CALENDAR_READ)
def resident(request):
    from homeoa.portal.views.base import _render as portal_render

    assoc = request.association
    audiences = services.audiences_for(request.user, assoc)
    token = services.feed_token(request.user, assoc, create=False)
    return portal_render(
        request,
        "home",
        "community/resident.html",
        announcements=services.visible_announcements(assoc, audiences),
        events=services.upcoming(assoc, audiences),
        feed_url=_feed_url(request, token) if token else "",
    )


def _back(request):
    if has_perm(request.user, perms.BOARD_SHELL, association=request.association):
        return redirect("community:calendar")
    return redirect("community:resident")


@login_required
def feed_settings(request):
    return _back(request)


def _may_subscribe(request) -> bool:
    return bool(services.audiences_for(request.user, request.association))


@require_POST
@login_required
def feed_reset(request):
    if not _may_subscribe(request):
        raise PermissionDenied
    token = services.reset_token(request.user, request.association)
    audit.record("community.feed_reset", token, association=request.association)
    messages.success(request, _("Your calendar link is ready. Any older link no longer works."))
    return _back(request)


@require_POST
@login_required
def feed_revoke(request):
    if not _may_subscribe(request):
        raise PermissionDenied
    services.revoke_token(request.user, request.association)
    audit.record("community.feed_revoked", None, association=request.association)
    messages.success(request, _("Your calendar link was turned off."))
    return _back(request)


@require_GET
def ical(request, token):
    row = services.token_owner(token)
    body = services.feed_for(row) if row is not None else None
    if body is None:
        raise Http404
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="calendar.ics"'
    response["Cache-Control"] = "private, max-age=900"
    return response
