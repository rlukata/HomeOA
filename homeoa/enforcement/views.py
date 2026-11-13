"""Board pages: violation log, categories and fine schedule versions (VIO-01, VIO-02)."""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date, parse_time
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.documents.models import Category, DocumentVersion
from homeoa.governance.models import Resolution
from homeoa.registry.models import Unit

from . import hearings, ladder, services
from .models import (
    FineSchedule,
    FineScheduleLine,
    Hearing,
    Violation,
    ViolationCategory,
    ViolationStep,
)

MANAGE = perms.VIOLATIONS_MANAGE


def _date(value, *, required=True) -> datetime.date | None:
    d = parse_date(value) if value else None
    if required and d is None:
        raise services.EnforcementError(_("Enter a valid date."))
    return d


def _money(value, *, required=True) -> Decimal | None:
    value = (value or "").strip()
    if not value:
        if required:
            raise services.EnforcementError(_("Enter an amount."))
        return None
    try:
        amount = Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise services.EnforcementError(_("Enter a valid amount.")) from None
    return amount


def _int(value) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _error(request, exc) -> None:
    messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))


def _schedule_banner(association, today):
    current = services.schedule_in_force(association, today)
    return current, (current is not None and not current.is_furnished)


@require_perm(MANAGE)
def index(request):
    a = request.association
    status = request.GET.get("status", "")
    unit_id = _int(request.GET.get("unit"))
    qs = Violation.objects.select_related("unit", "category")
    if status in Violation.Status.values:
        qs = qs.filter(status=status)
    if unit_id:
        qs = qs.filter(unit_id=unit_id)
    today = association_today(a)
    current, unfurnished = _schedule_banner(a, today)
    return render(
        request,
        "enforcement/index.html",
        {
            "violations": qs,
            "statuses": Violation.Status.choices,
            "status": status,
            "unit_id": unit_id,
            "units": Unit.objects.order_by("number"),
            "categories": ViolationCategory.objects.filter(active=True),
            "today": today,
            "schedule": current,
            "unfurnished": unfurnished,
        },
    )


@require_POST
@require_perm(MANAGE)
def violation_add(request):
    a = request.association
    try:
        unit = Unit.objects.filter(pk=_int(request.POST.get("unit"))).first()
        category = ViolationCategory.objects.filter(pk=_int(request.POST.get("category"))).first()
        if unit is None or category is None:
            raise services.EnforcementError(_("Choose a unit and a category."))
        photos = [(f.name, f.read()) for f in request.FILES.getlist("photos")]
        v = services.log_violation(
            a,
            unit=unit,
            category=category,
            observed_on=_date(request.POST.get("observed_on")),
            description=request.POST.get("description", ""),
            photos=photos,
            user=request.user,
        )
    except ValidationError as exc:
        _error(request, exc)
        return redirect("enforcement:index")
    messages.success(request, _("Violation logged."))
    return redirect("enforcement:detail", pk=v.pk)


@require_perm(MANAGE)
def detail(request, pk):
    v = get_object_or_404(Violation.objects.select_related("unit", "category"), pk=pk)
    schedule = services.schedule_in_force(request.association, v.observed_on)
    today = association_today(request.association)
    formal = ladder.formal_step(v)
    line = None
    if schedule:
        line = schedule.lines.filter(category=v.category).first()
    return render(
        request,
        "enforcement/detail.html",
        {
            "v": v,
            "photos": v.photos.select_related("document__current_version"),
            "responses": v.responses.select_related("person").prefetch_related("photos__document"),
            "statuses": Violation.Status.choices,
            "schedule": schedule,
            "line": line,
            "furnished_in_time": bool(schedule and schedule.furnished_by(v.observed_on)),
            "steps": v.steps.select_related("letter", "batch"),
            "notices": _notice_actions(v, today),
            "cure_rule": ladder.cure_days(request.association, today),
            "formal": formal,
            "cure_passed": bool(
                formal
                and formal.cure_by
                and formal.cure_by < today
                and v.status == Violation.Status.OPEN
                and not v.hearings.filter(cancelled_on__isnull=True).exists()
            ),
            "today": today,
            **_hearing_context(request, v, today),
        },
    )


def _notice_actions(v, today):
    return [
        {
            "kind": kind,
            "label": ViolationStep.Kind(kind).label,
            "problem": ladder.notice_problem(v, kind, today),
        }
        for kind in ladder.NOTICE_KINDS
        if kind not in ladder.sent_kinds(v)
    ]


@require_POST
@require_perm(MANAGE)
def notice_send(request, pk):
    v = get_object_or_404(Violation.objects, pk=pk)
    try:
        step = ladder.send_notice(v, request.POST.get("kind", ""), user=request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        if step.delivery == ViolationStep.Delivery.EMAIL:
            messages.success(request, _("Notice sent by email."))
        else:
            messages.success(
                request,
                _("Letter generated and added to a draft mail batch. Send it from Letters."),
            )
    return redirect("enforcement:detail", pk=v.pk)


@require_POST
@require_perm(MANAGE)
def cured(request, pk):
    v = get_object_or_404(Violation.objects, pk=pk)
    try:
        ladder.mark_cured(
            v,
            cured_on=_date(request.POST.get("cured_on")),
            note=request.POST.get("note", ""),
            user=request.user,
        )
        messages.success(request, _("Marked cured."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=v.pk)


@require_POST
@require_perm(MANAGE)
def violation_status(request, pk):
    v = get_object_or_404(Violation.objects, pk=pk)
    try:
        status = request.POST.get("status", "")
        note = request.POST.get("note", "")
        if status == Violation.Status.CURED and v.status != status:
            ladder.mark_cured(v, note=note, user=request.user)
            messages.success(request, _("Marked cured."))
        elif services.set_status(v, status, note=note, user=request.user):
            messages.success(request, _("Status updated."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=v.pk)


@require_POST
@require_perm(MANAGE)
def photo_add(request, pk):
    v = get_object_or_404(Violation.objects, pk=pk)
    files = request.FILES.getlist("photos")
    if not files:
        messages.error(request, _("Choose a photo."))
    try:
        with transaction.atomic():
            for f in files:
                services.add_photo(v, filename=f.name, content=f.read(), user=request.user)
        if files:
            messages.success(request, _("Photos added."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=v.pk)


def _governing_versions():
    return DocumentVersion.objects.filter(document__category=Category.GOVERNING).select_related(
        "document"
    )


def _apply_category(request, c: ViolationCategory) -> None:
    c.name = request.POST.get("name", "").strip()[:120]
    c.rule_citation = request.POST.get("rule_citation", "").strip()[:300]
    gv = _int(request.POST.get("governing_version"))
    c.governing_version = _governing_versions().filter(pk=gv).first() if gv else None
    c.active = request.POST.get("active", "on") == "on"
    c.full_clean(exclude=["association"])
    c.save()


@require_perm(MANAGE)
def categories(request):
    if request.method == "POST":
        try:
            with transaction.atomic():
                _apply_category(request, ViolationCategory(association=request.association))
            messages.success(request, _("Category added."))
        except IntegrityError:
            messages.error(request, _("A category with this name already exists."))
        except ValidationError as exc:
            _error(request, exc)
        return redirect("enforcement:categories")
    return render(
        request,
        "enforcement/categories.html",
        {
            "categories": ViolationCategory.objects.select_related("governing_version__document"),
            "governing_versions": _governing_versions(),
        },
    )


@require_POST
@require_perm(MANAGE)
def category_edit(request, pk):
    c = get_object_or_404(ViolationCategory.objects, pk=pk)
    try:
        with transaction.atomic():
            _apply_category(request, c)
        messages.success(request, _("Category saved."))
    except IntegrityError:
        messages.error(request, _("A category with this name already exists."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:categories")


def _schedule_fields(request, s: FineSchedule) -> None:
    s.effective_on = _date(request.POST.get("effective_on"))
    s.adopted_on = _date(request.POST.get("adopted_on"))
    s.furnished_on = _date(request.POST.get("furnished_on"), required=False)
    rid = _int(request.POST.get("resolution"))
    s.resolution = Resolution.objects.filter(pk=rid).first() if rid else None
    s.note = request.POST.get("note", "").strip()[:300]
    s.full_clean(exclude=["association"])
    s.save()


@require_perm(MANAGE)
def schedules(request):
    a = request.association
    if request.method == "POST":
        try:
            with transaction.atomic():
                eff = _date(request.POST.get("effective_on"))
                prior = services.schedule_in_force(a, eff)
                s = FineSchedule(association=a, version=services.next_schedule_version(a))
                _schedule_fields(request, s)
                if prior and request.POST.get("copy_lines") == "on":
                    for line in prior.lines.all():
                        line.pk = None
                        line.schedule = s
                        line.save()
            messages.success(request, _("Fine schedule version added."))
            return redirect("enforcement:schedule", pk=s.pk)
        except ValidationError as exc:
            _error(request, exc)
        return redirect("enforcement:schedules")
    today = association_today(a)
    current, unfurnished = _schedule_banner(a, today)
    return render(
        request,
        "enforcement/schedules.html",
        {
            "schedules": FineSchedule.objects.select_related("resolution"),
            "current": current,
            "unfurnished": unfurnished,
            "resolutions": Resolution.objects.all(),
        },
    )


def _save_line(request, s: FineSchedule) -> None:
    category = ViolationCategory.objects.filter(pk=_int(request.POST.get("category"))).first()
    if category is None:
        raise services.EnforcementError(_("Choose a category."))
    line = s.lines.filter(category=category).first() or FineScheduleLine(
        association=s.association, schedule=s, category=category
    )
    line.first_amount = _money(request.POST.get("first_amount"))
    line.repeat_amount = _money(request.POST.get("repeat_amount"))
    line.continuing_amount = _money(request.POST.get("continuing_amount"), required=False)
    line.continuing_period_days = _int(request.POST.get("continuing_period_days") or None)
    line.full_clean(exclude=["association", "schedule"])
    line.save()


@require_perm(MANAGE)
def schedule_detail(request, pk):
    s = get_object_or_404(FineSchedule.objects.select_related("resolution"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            with transaction.atomic():
                if action == "line":
                    _save_line(request, s)
                    messages.success(request, _("Fine line saved."))
                elif action == "remove_line":
                    s.lines.filter(pk=_int(request.POST.get("line"))).delete()
                    messages.success(request, _("Fine line removed."))
                else:
                    _schedule_fields(request, s)
                    messages.success(request, _("Fine schedule saved."))
        except ValidationError as exc:
            _error(request, exc)
        return redirect("enforcement:schedule", pk=s.pk)
    return render(
        request,
        "enforcement/schedule.html",
        {
            "s": s,
            "lines": s.lines.select_related("category"),
            "categories": ViolationCategory.objects.all(),
            "resolutions": Resolution.objects.all(),
        },
    )


def _can_fine(request) -> bool:
    # A fine is a money change on the owner ledger: it needs finance.manage too.
    return has_perm(request.user, perms.FINANCE_MANAGE, association=request.association)


def _hearing_context(request, v, today):
    pending = hearings.pending_hearing(v)
    min_days = hearings.notice_min_days(request.association, today)
    ctx = {
        "hearings": v.hearings.select_related(
            "notice_step__letter", "decision_step__letter", "charge"
        ),
        "pending": pending,
        "hearing_problem": hearings.schedule_problem(v, None, today),
        "hearing_rule": min_days,
        "earliest_hearing": (
            today + datetime.timedelta(days=min_days.value) if min_days.value is not None else None
        ),
        "outcomes": Hearing.Outcome.choices,
        "can_fine": _can_fine(request),
    }
    if pending is not None:
        ctx["fine_problem"] = hearings.fine_problem(pending, today)
        ctx["default_fine"] = hearings.schedule_amount(v)
        ctx["repeat"] = hearings.is_repeat(v)
    return ctx


def _hearing_fields(request) -> dict:
    time_value = request.POST.get("hearing_time") or None
    hearing_time = parse_time(time_value) if time_value else None
    if time_value and hearing_time is None:
        raise services.EnforcementError(_("Enter a valid time."))
    return {
        "hearing_on": _date(request.POST.get("hearing_on")),
        "hearing_time": hearing_time,
        "place": request.POST.get("place", ""),
        "online": request.POST.get("online") == "on",
        "user": request.user,
    }


@require_POST
@require_perm(MANAGE)
def hearing_schedule(request, pk):
    v = get_object_or_404(Violation.objects, pk=pk)
    try:
        hearings.schedule_hearing(v, **_hearing_fields(request))
        messages.success(request, _("Hearing scheduled and notice sent."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=v.pk)


@require_POST
@require_perm(MANAGE)
def hearing_reschedule(request, pk):
    h = get_object_or_404(Hearing.objects.select_related("violation"), pk=pk)
    try:
        hearings.reschedule_hearing(h, **_hearing_fields(request))
        messages.success(request, _("Hearing rescheduled and updated notice sent."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=h.violation_id)


@require_POST
@require_perm(MANAGE)
def hearing_cancel(request, pk):
    h = get_object_or_404(Hearing.objects.select_related("violation"), pk=pk)
    try:
        hearings.cancel_hearing(h, request.POST.get("reason", ""), user=request.user)
        messages.success(request, _("Hearing cancelled."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=h.violation_id)


def _attended(value):
    return {"yes": True, "no": False}.get(value)


@require_POST
@require_perm(MANAGE)
def hearing_decision(request, pk):
    h = get_object_or_404(Hearing.objects.select_related("violation"), pk=pk)
    outcome = request.POST.get("outcome", "")
    try:
        if outcome == Hearing.Outcome.FINE and not _can_fine(request):
            raise services.EnforcementError(
                _("Imposing a fine needs the finance permission (manage finances).")
            )
        amount = (request.POST.get("fine_amount") or "").strip() or None
        if amount is not None:
            amount = _money(amount)
        hearings.record_decision(
            h,
            outcome=outcome,
            attended=_attended(request.POST.get("attended")),
            owner_statement=request.POST.get("owner_statement", ""),
            note=request.POST.get("note", ""),
            fine_amount=amount,
            amount_reason=request.POST.get("amount_reason", ""),
            user=request.user,
        )
        messages.success(request, _("Decision recorded and letter generated."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=h.violation_id)


@require_POST
@require_perm(MANAGE)
def fine_void(request, pk):
    h = get_object_or_404(Hearing.objects, pk=pk)
    try:
        if not _can_fine(request):
            raise services.EnforcementError(
                _("Voiding a fine needs the finance permission (manage finances).")
            )
        hearings.void_fine(h, request.POST.get("reason", ""), user=request.user)
        messages.success(request, _("Fine voided."))
    except ValidationError as exc:
        _error(request, exc)
    return redirect("enforcement:detail", pk=h.violation_id)
