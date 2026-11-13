"""Settings > Roles: assignments, custom roles and the MFA-for-board toggle (PLT-03)."""

import datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa import audit
from homeoa.accounts.policy import SETTING_KEY as MFA_KEY
from homeoa.audit.models import AuditEvent
from homeoa.changes.models import ChangeRequest
from homeoa.core import status as association_status
from homeoa.core.context import association_today

from . import permissions as perms
from . import role_changes, services
from .decorators import require_perm
from .forms import (
    AssignmentEditForm,
    AssignmentForm,
    CustomRoleForm,
    GoLiveForm,
    ResidentPauseForm,
    SecurityForm,
)
from .models import Role, RoleAssignment


def _association(request):
    association = getattr(request, "association", None)
    if association is None:
        raise Http404
    services.ensure_default_roles(association)
    return association


def _page(request, association, assign_form=None, role_form=None, security_form=None):
    day = services.today_for(association)
    assignments = RoleAssignment.all_associations.filter(association=association).select_related(
        "user", "role", "unit"
    )
    rows = [{"a": a, "active": a.is_active_on(day), "future": a.start > day} for a in assignments]
    roles = Role.all_associations.filter(association=association)
    return render(
        request,
        "access/roles.html",
        {
            "current_hub": None,
            "subtabs": [],
            "rows": rows,
            "roles": [
                {
                    "role": r,
                    "labels": sorted(str(perms.LABELS[c]) for c in r.permission_set),
                    "derived": r.slug in perms.DERIVED_ROLES,
                }
                for r in roles
            ],
            "assign_form": assign_form
            or AssignmentForm(association=association, initial={"start": day}),
            "role_form": role_form or CustomRoleForm(),
            "security_form": security_form
            or SecurityForm(
                initial={"require_mfa_for_board": (association.settings or {}).get(MFA_KEY)}
            ),
            "residents_enabled": services.residents_enabled(association),
            "finance": services.finance_managers(association),
            "pending_changes": ChangeRequest.all_associations.filter(
                association=association,
                status=ChangeRequest.Status.PENDING,
                kind__in=(role_changes.ASSIGN, role_changes.EDIT, role_changes.END),
            ),
        },
    )


def _proposed(request, change, done_message):
    """Tell the person what happened: applied at once (no board yet) or sent for approval."""
    if change.status == ChangeRequest.Status.APPLIED:
        messages.success(request, done_message)
    elif change.status == ChangeRequest.Status.FAILED:
        messages.error(request, change.error)
    else:
        messages.success(
            request,
            _("Sent for board approval. It takes effect once approved (%(n)s needed).")
            % {"n": change.required_approvals},
        )
    return redirect("access:index")


@require_perm(perms.ROLES_MANAGE)
def index(request):
    return _page(request, _association(request))


@require_perm(perms.ROLES_MANAGE)
@require_POST
def assign(request):
    association = _association(request)
    form = AssignmentForm(request.POST, association=association)
    if not form.is_valid():
        return _page(request, association, assign_form=form)
    data = form.cleaned_data
    try:
        change = role_changes.propose_assign(
            association,
            user=request.user,
            email=data["email"].lower(),
            role=data["role"],
            title=data["title"] or "",
            start=data["start"],
            end=data["end"],
        )
    except ValidationError as exc:
        for message in exc.messages:
            form.add_error(None, message)
        return _page(request, association, assign_form=form)
    return _proposed(request, change, _("Role assigned."))


def _history(assignment):
    """The assignment's title and date changes from the audit log, newest first."""
    titles = dict(RoleAssignment.Title.choices)
    labels = {f: RoleAssignment._meta.get_field(f).verbose_name for f in ("title", "start", "end")}

    def show(field, value):
        if value in (None, ""):
            return "—"
        return titles.get(value, value) if field == "title" else value

    events = AuditEvent.all_associations.filter(
        association=assignment.association,
        object_type=RoleAssignment._meta.label_lower,
        object_id=str(assignment.pk),
    ).order_by("-created_at", "-pk")
    rows = []
    for e in events:
        changes = e.changes if isinstance(e.changes, dict) else {}
        items = [
            (labels[f], show(f, v[0]), show(f, v[1]))
            for f, v in changes.items()
            if f in labels and isinstance(v, list) and len(v) == 2
        ]
        rows.append({"event": e, "items": items})
    return rows


@require_perm(perms.ROLES_MANAGE)
def edit(request, pk):
    """Change an assignment's officer title or dates in place."""
    association = _association(request)
    assignment = get_object_or_404(
        RoleAssignment.all_associations.select_related("user", "role"),
        association=association,
        pk=pk,
    )
    if request.method == "POST":
        form = AssignmentEditForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                change = role_changes.propose_edit(
                    association,
                    assignment,
                    user=request.user,
                    title=data["title"] or "",
                    start=data["start"],
                    end=data["end"],
                )
            except ValidationError as exc:
                for message in exc.messages:
                    form.add_error(None, message)
            else:
                return _proposed(request, change, _("Role updated."))
    else:
        form = AssignmentEditForm(
            initial={"title": assignment.title, "start": assignment.start, "end": assignment.end}
        )
    return render(
        request,
        "access/edit_assignment.html",
        {
            "current_hub": None,
            "subtabs": [],
            "assignment": assignment,
            "form": form,
            "history": _history(assignment),
        },
    )


@require_perm(perms.ROLES_MANAGE)
@require_POST
def end(request, pk):
    association = _association(request)
    assignment = get_object_or_404(
        RoleAssignment.all_associations.select_related("user", "role"),
        association=association,
        pk=pk,
    )
    change = role_changes.propose_end(association, assignment, user=request.user)
    return _proposed(request, change, _("Role ended."))


@require_perm(perms.ROLES_MANAGE)
@require_POST
def add_role(request):
    association = _association(request)
    form = CustomRoleForm(request.POST)
    if not form.is_valid():
        return _page(request, association, role_form=form)
    base = slugify(form.cleaned_data["name"])[:40] or "role"
    slug, n = base, 1
    while Role.all_associations.filter(association=association, slug=slug).exists() or (
        slug in perms.DEFAULT_ROLES
    ):
        n += 1
        slug = f"{base}-{n}"
    Role.all_associations.create(
        association=association,
        slug=slug,
        name=form.cleaned_data["name"],
        permissions=sorted(form.cleaned_data["permissions"]),
        member_limit=form.cleaned_data["member_limit"],
    )
    messages.success(request, _("Role created."))
    return redirect("access:index")


@require_perm(perms.SETTINGS_MANAGE)
def association_status_page(request):
    """Association status (Setting up or Live, go-live date) and the System admin's
    emergency pause of resident access, with a required reason, audited."""
    association = _association(request)
    row = association_status.status_for(association)
    paused = row.residents_paused
    if request.method == "POST":
        form = ResidentPauseForm(request.POST)
        if form.is_valid():
            reason = form.cleaned_data["reason"]
            if paused:
                association_status.resume_residents(association, reason=reason, by=request.user)
                messages.success(request, _("The pause on resident access is lifted."))
            else:
                association_status.pause_residents(association, reason=reason, by=request.user)
                messages.success(request, _("Resident access is paused."))
            return redirect("access:status")
    else:
        form = ResidentPauseForm()
    today = association_today(association)
    return render(
        request,
        "access/status.html",
        {
            "current_hub": None,
            "subtabs": [],
            "form": form,
            "live": association_status.is_live(association),
            "status_label": association_status.label(association),
            "go_live_date": row.go_live_date,
            "go_live_future": row.go_live_date is not None and row.go_live_date > today,
            "paused": paused,
            "status_row": row,
            "residents_open": services.residents_enabled(association),
        },
    )


def _gap_dues(association, records_end, today):
    """Months of dues that fell due after the loaded records end and before today and are
    not posted yet: the scheduler will not post them, so the board posts them by hand."""
    from homeoa.billing.models import AssessmentSchedule, BillingRun

    found = []
    for schedule in AssessmentSchedule.objects.filter(association=association, active=True):
        period = records_end.replace(day=1)
        while period <= today:
            due = period.replace(day=schedule.due_day)
            if (
                records_end < due < today
                and schedule.covers(period)
                and not BillingRun.objects.filter(schedule=schedule, period=period).exists()
            ):
                found.append({"schedule": schedule, "period": period, "due": due})
            period = (period + datetime.timedelta(days=32)).replace(day=1)
    return sorted(found, key=lambda d: (d["due"], d["schedule"].pk))


@require_perm(perms.SETTINGS_MANAGE)
def go_live(request):
    """Settings > Go live: what the loaded records cover, what the board must handle by
    hand for the days between them and today, the readiness checks, and the System admin's
    button that makes the association live today."""
    from homeoa.imports.cutover import readiness
    from homeoa.imports.models import ImportRun

    association = _association(request)
    if request.method == "POST":
        form = GoLiveForm(request.POST)
        if form.is_valid():
            try:
                association_status.go_live_today(association, by=request.user)
            except association_status.AlreadyLive as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, _("The association is live from today."))
            return redirect("access:go_live")
    else:
        form = GoLiveForm()
    today = association_today(association)
    live = association_status.is_live(association, today)
    latest = (
        ImportRun.objects.filter(
            association=association,
            mode=ImportRun.Mode.COMMIT,
            status=ImportRun.Status.COMMITTED,
        )
        .order_by("-as_of", "-pk")
        .first()
    )
    records_end = latest.as_of if latest else None
    yesterday = today - datetime.timedelta(days=1)
    gap = records_end is not None and records_end < yesterday
    checks = [] if live else readiness(association, today).checks
    if latest is None:
        checks = [c for c in checks if c.key == "people"]
    row = association_status.status_for(association)
    return render(
        request,
        "access/go_live.html",
        {
            "current_hub": None,
            "subtabs": [],
            "form": form,
            "live": live,
            "go_live_date": row.go_live_date,
            "records_end": records_end,
            "gap": gap and not live,
            "gap_start": records_end + datetime.timedelta(days=1) if gap else None,
            "yesterday": yesterday,
            "gap_dues": _gap_dues(association, records_end, today) if gap and not live else [],
            "checks": checks,
            "paused": row.residents_paused,
        },
    )


@require_perm(perms.ROLES_MANAGE)
@require_POST
def security(request):
    association = _association(request)
    form = SecurityForm(request.POST)
    if not form.is_valid():
        return _page(request, association, security_form=form)
    settings = dict(association.settings or {})
    old = bool(settings.get(MFA_KEY, False))
    new = form.cleaned_data["require_mfa_for_board"]
    if old != new:
        settings[MFA_KEY] = new
        association.settings = settings
        association.save(update_fields=["settings"])
        audit.record("association.mfa_policy", association, changes={MFA_KEY: [old, new]})
    messages.success(request, _("Security settings saved."))
    return redirect("access:index")
