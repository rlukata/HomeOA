from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date, parse_time
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.documents.models import Visibility
from homeoa.documents.services import UploadRejected
from homeoa.governance.models import Resolution
from homeoa.registry.models import Person, Unit

from . import minutes as msvc
from . import quorum, services
from .models import AgendaItem, Attendance, Meeting, Minutes, Proxy


def _can_manage(request) -> bool:
    return has_perm(request.user, perms.GOVERNANCE_MANAGE, association=request.association)


def _manage(request) -> None:
    if not _can_manage(request):
        raise PermissionDenied


def _fields(post) -> dict:
    date = parse_date(post.get("date", "") or "")
    if date is None:
        raise services.MeetingError(_("Enter a valid date."))
    raw_time = post.get("start_time", "")
    start_time = parse_time(raw_time) if raw_time else None
    if raw_time and start_time is None:
        raise services.MeetingError(_("Enter a valid time."))
    return {
        "title": post.get("title", "").strip()[:200],
        "date": date,
        "start_time": start_time,
        "location": post.get("location", "").strip()[:300],
        "video_url": post.get("video_url", "").strip()[:500],
        "dial_in": post.get("dial_in", "").strip()[:200],
        "description": post.get("description", "").strip(),
    }


@require_perm(perms.GOVERNANCE_READ)
def index(request):
    a = request.association
    today = association_today(a)
    meetings = list(Meeting.objects.all())
    return render(
        request,
        "meetings/index.html",
        {
            "upcoming": [m for m in meetings if m.date >= today][::-1],
            "past": [m for m in meetings if m.date < today],
            "kinds": Meeting.Kind.choices,
            "can_manage": _can_manage(request),
            "live": services.owner_notices_live(a),
            "residents_on": residents_enabled(a),
        },
    )


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def add(request):
    _manage(request)
    try:
        meeting = services.create_meeting(
            request.association,
            kind=request.POST.get("kind", ""),
            user=request.user,
            **_fields(request.POST),
        )
    except services.MeetingError as exc:
        messages.error(request, str(exc))
        return redirect("meetings:index")
    messages.success(request, _("Meeting scheduled. Add the agenda, then send the notice."))
    return redirect("meetings:detail", pk=meeting.pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def live(request):
    _manage(request)
    on = request.POST.get("on") == "1"
    services.set_owner_notices_live(request.association, on, user=request.user)
    messages.success(
        request,
        _("Owner notices are on.") if on else _("Owner notices are off (before cutover)."),
    )
    return redirect("meetings:index")


@require_perm(perms.GOVERNANCE_READ)
def detail(request, pk):
    a = request.association
    meeting = get_object_or_404(Meeting, pk=pk)
    today = association_today(a)
    window = services.notice_window(meeting, today)
    owners_problem = services.mode_problem(a, services.MODE_OWNERS)
    return render(
        request,
        "meetings/meeting.html",
        {
            "meeting": meeting,
            "agenda": list(meeting.agenda.select_related("resolution")),
            "notices": list(meeting.notices.select_related("unit", "person", "batch")),
            "window": window,
            "window_problem": services.window_problem(meeting, today),
            "remote_problem": services.remote_problem(meeting),
            "owner_comment": services.owner_comment_text(meeting),
            "emergency": services.is_emergency(meeting),
            "print_problem": services.mode_problem(a, services.MODE_PRINT, meeting),
            "owners_problem": owners_problem,
            "resolutions": Resolution.objects.all(),
            "can_manage": _can_manage(request),
            "today": today,
        },
    )


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def edit(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    try:
        services.update_meeting(meeting, **_fields(request.POST))
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Meeting saved."))
    return redirect("meetings:detail", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def emergency(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    try:
        services.set_emergency(
            meeting,
            request.POST.get("on") == "1",
            request.POST.get("reason", ""),
            user=request.user,
        )
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Meeting saved."))
    return redirect("meetings:detail", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def status(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    try:
        services.set_status(meeting, request.POST.get("status", ""))
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    return redirect("meetings:detail", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def agenda_add(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    title = request.POST.get("title", "").strip()[:200]
    resolution = None
    if request.POST.get("resolution"):
        resolution = Resolution.objects.filter(pk=request.POST["resolution"]).first()
    if not title and resolution is not None:
        title = str(resolution)[:200]
    if not title:
        messages.error(request, _("Enter an agenda item."))
        return redirect("meetings:detail", pk=pk)
    try:
        services.add_agenda_item(
            meeting, title=title, notes=request.POST.get("notes", ""), resolution=resolution
        )
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    return redirect("meetings:detail", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def agenda_move(request, pk, item):
    _manage(request)
    obj = get_object_or_404(AgendaItem, pk=item, meeting_id=pk)
    if obj.meeting.is_open:
        services.move_agenda_item(obj, -1 if request.POST.get("dir") == "up" else 1)
    return redirect("meetings:detail", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def agenda_remove(request, pk, item):
    _manage(request)
    obj = get_object_or_404(AgendaItem, pk=item, meeting_id=pk)
    try:
        services.remove_agenda_item(obj)
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    return redirect("meetings:detail", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def send(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    try:
        result = services.send_notice(meeting, request.POST.get("mode", ""), user=request.user)
    except services.MeetingError as exc:
        messages.error(request, str(exc))
        return redirect("meetings:detail", pk=pk)
    counts = {
        "email": result.email,
        "mail": result.mail,
        "print": result.printed,
        "board": result.board,
        "skipped": result.skipped,
        "no_consent": result.no_consent,
        "no_email": result.no_email,
    }
    if result.no_email:
        text = _(
            "Notice sent: %(email)s by email, %(mail)s by mail, %(print)s to print, "
            "%(board)s board copies. %(no_email)s units have no email on file; "
            "%(skipped)s already noticed or without owners."
        )
    elif result.no_consent:
        text = _(
            "Notice sent: %(email)s by email, %(mail)s by mail, %(print)s to print, "
            "%(board)s board copies. %(no_consent)s without e-notice consent; "
            "%(skipped)s already noticed or without owners."
        )
    else:
        text = _(
            "Notice sent: %(email)s by email, %(mail)s by mail, %(print)s to print, "
            "%(board)s board copies. %(skipped)s already noticed or without owners."
        )
    messages.success(request, text % counts)
    return redirect("meetings:detail", pk=pk)


# --- minutes (GOV-04) -------------------------------------------------------------------


@require_perm(perms.GOVERNANCE_READ)
def minutes(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk)
    record = Minutes.objects.filter(meeting=meeting).select_related("approved_at").first()
    later = (
        Meeting.objects.filter(date__gte=meeting.date)
        .exclude(pk=meeting.pk)
        .exclude(status=Meeting.Status.CANCELLED)
        .order_by("date")
    )
    return render(
        request,
        "meetings/minutes.html",
        {
            "meeting": meeting,
            "minutes": record,
            "later_meetings": later,
            "visibilities": [
                (v, label) for v, label in Visibility.choices if v in msvc.PUBLISH_VISIBILITIES
            ],
            "can_manage": _can_manage(request),
            "today": association_today(request.association),
        },
    )


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def minutes_action(request, pk, action):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    record = msvc.get_or_create(meeting, user=request.user)
    post = request.POST
    try:
        if action == "save":
            msvc.save_draft(record, post.get("body", ""), user=request.user)
            messages.success(request, _("Draft minutes saved."))
        elif action == "approve":
            at = Meeting.objects.filter(pk=post.get("approved_at") or 0).first()
            on = parse_date(post.get("approved_on", "") or "")
            if at is None or on is None:
                raise services.MeetingError(_("Choose the approving meeting and date."))
            msvc.approve(record, at_meeting=at, on=on, user=request.user)
            messages.success(request, _("Minutes approved. They can no longer be changed."))
        elif action == "publish":
            msvc.publish(
                record, visibility=post.get("visibility", Visibility.OWNERS), user=request.user
            )
            messages.success(request, _("Minutes published to the document library."))
        elif action == "sign":
            msvc.send_for_signature(
                record, user=request.user, base_url=request.build_absolute_uri("/")[:-1]
            )
            messages.success(request, _("Sent to the secretary for signature."))
        else:
            raise services.MeetingError(_("Unknown action."))
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    return redirect("meetings:minutes", pk=pk)


# --- attendance, proxies, quorum (GOV-05) -----------------------------------------------


def _attendance_context(request, meeting) -> dict:
    rows = {(r.unit_id or r.person_id): r.mode for r in Attendance.objects.filter(meeting=meeting)}
    proxies = list(Proxy.objects.filter(meeting=meeting).select_related("unit", "document"))
    active = {p.unit_id: p for p in proxies if p.active}
    units = list(Unit.objects.order_by("number"))
    if meeting.is_owner_meeting:
        roster = [
            {
                "pk": u.pk,
                "label": _("Unit %(n)s") % {"n": u.number},
                "mode": rows.get(u.pk, ""),
                "proxy": active.get(u.pk),
            }
            for u in units
        ]
    else:
        roster = [
            {"pk": p.pk, "label": p.full_name, "mode": rows.get(p.pk, ""), "proxy": None}
            for p in quorum.board_members(meeting)
        ]
    today = association_today(request.association)
    basis, value = quorum.setting_for(request.association, meeting.kind)
    return {
        "meeting": meeting,
        "roster": roster,
        "modes": Attendance.Mode.choices,
        "proxies": proxies,
        "q": quorum.compute(meeting),
        "basis": basis,
        "value": value,
        "can_manage": _can_manage(request),
        "can_revoke": meeting.status != Meeting.Status.HELD and today <= meeting.date,
        "today": today,
        "units": units,
    }


@require_perm(perms.GOVERNANCE_READ)
def attendance(request, pk):
    meeting = get_object_or_404(Meeting, pk=pk)
    return render(request, "meetings/attendance.html", _attendance_context(request, meeting))


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def attendance_set(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    target = request.POST.get("who") or 0
    mode = request.POST.get("mode", "")
    try:
        if meeting.is_owner_meeting:
            msvc.set_attendance(meeting, mode=mode, unit=Unit.objects.filter(pk=target).first())
        else:
            person = Person.objects.filter(pk=target).first()
            msvc.set_attendance(meeting, mode=mode, person=person)
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    if request.htmx:
        return render(request, "meetings/_attendance.html", _attendance_context(request, meeting))
    return redirect("meetings:attendance", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def proxy_add(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    f = request.FILES.get("file")
    try:
        granted = parse_date(request.POST.get("granted_on", "") or "")
        unit = Unit.objects.filter(pk=request.POST.get("unit") or 0).first()
        if granted is None or unit is None:
            raise services.MeetingError(_("Choose the unit and the date of the proxy."))
        msvc.add_proxy(
            meeting,
            unit=unit,
            holder=request.POST.get("holder", ""),
            granted_on=granted,
            upload=(f.name, f.read()) if f is not None else None,
            user=request.user,
        )
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    except UploadRejected as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, _("Proxy recorded."))
    return redirect("meetings:attendance", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def proxy_revoke(request, pk, proxy):
    _manage(request)
    obj = get_object_or_404(Proxy, pk=proxy, meeting_id=pk)
    try:
        msvc.revoke_proxy(obj, on=association_today(request.association), user=request.user)
    except services.MeetingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Proxy revoked."))
    return redirect("meetings:attendance", pk=pk)


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def quorum_setting(request, pk):
    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    try:
        quorum.set_requirement(
            request.association,
            meeting.kind,
            request.POST.get("basis", ""),
            request.POST.get("value", ""),
            user=request.user,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Quorum setting saved for this kind of meeting."))
    return redirect("meetings:attendance", pk=pk)


# --- annual meeting workflow (GOV-03, FIN-13) -------------------------------------------


@require_perm(perms.GOVERNANCE_READ)
def annual(request, pk):
    from homeoa.budget.models import Budget, BudgetStatus

    from . import annual as annual_svc

    meeting = get_object_or_404(Meeting, pk=pk)
    try:
        wf = annual_svc.workflow_for(meeting)
    except annual_svc.AnnualError as exc:
        messages.error(request, str(exc))
        return redirect("meetings:detail", pk=pk)
    today = association_today(request.association)
    return render(
        request,
        "meetings/annual.html",
        {
            "meeting": meeting,
            "wf": wf,
            "steps": annual_svc.steps(wf, today),
            "budgets": Budget.objects.exclude(status=BudgetStatus.SUPERSEDED),
            "can_manage": _can_manage(request),
            "today": today,
        },
    )


@require_POST
@require_perm(perms.GOVERNANCE_READ)
def annual_action(request, pk):
    from homeoa.budget.models import Budget

    from . import annual as annual_svc

    _manage(request)
    meeting = get_object_or_404(Meeting, pk=pk)
    action = request.POST.get("action", "")
    try:
        wf = annual_svc.workflow_for(meeting)
        if action == "budget":
            budget = get_object_or_404(Budget, pk=request.POST.get("budget") or 0)
            annual_svc.set_budget(wf, budget)
        elif action == "sent":
            on = parse_date(request.POST.get("on", "") or "")
            if on is None:
                raise annual_svc.AnnualError(_("Enter a valid date."))
            annual_svc.mark_budget_sent(wf, on, user=request.user)
            messages.success(request, _("Budget summary recorded as sent."))
        elif action == "votes":
            annual_svc.create_votes(wf, user=request.user)
            messages.success(request, _("Votes added to the agenda. Open them from Votes."))
        elif action == "dismiss_rejection":
            annual_svc.dismiss_rejection(wf, user=request.user)
            messages.success(request, _("Follow-up dismissed."))
        elif action == "apply":
            lines = annual_svc.apply_results(wf, user=request.user)
            for line in lines or [_("No closed vote results to record yet.")]:
                messages.info(request, line)
    except (annual_svc.AnnualError, services.MeetingError) as exc:
        messages.error(request, str(exc))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("meetings:annual", pk=pk)
