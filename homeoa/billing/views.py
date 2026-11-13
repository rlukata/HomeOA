"""Money hub > Dues & ledger > Assessments: schedules, preview, post, history (FIN-03)."""

import datetime as dt

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.changes.replay import money_change, money_page
from homeoa.core.context import association_today
from homeoa.ui.nav import get_hub

from .forms import RunForm, ScheduleForm
from .models import AssessmentSchedule, BillingRun, Charge
from .services import (
    BillingError,
    default_schedule_kwargs,
    generate_assessments,
    month_start,
    preview,
)


def _shell(title):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "ledger"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "ledger"), None)
    return {"current_hub": hub, "subtabs": tabs, "current_tab": current, "heading": title}


@require_perm(perms.FINANCE_READ)
def index(request):
    ctx = _shell(_("Assessments"))
    ctx.update(
        {
            "schedules": AssessmentSchedule.objects.all(),
            "runs": BillingRun.objects.select_related("schedule")[:24],
        }
    )
    return render(request, "billing/index.html", ctx)


@money_page
@require_perm(perms.FINANCE_MANAGE)
def run(request):
    """Pick a month, preview per-unit amounts, post (POST goes to ``post``)."""
    association = request.association
    data = request.GET if "period" in request.GET else None
    initial = {"period": month_start(association_today(association)).strftime("%Y-%m")}
    form = RunForm(data, association=association, initial=initial)
    plan = None
    if form.is_bound and form.is_valid():
        plan = preview(form.cleaned_data["schedule"], form.cleaned_data["period"])
    ctx = _shell(_("Run assessment"))
    ctx.update({"form": form, "plan": plan, "has_schedule": form.fields["schedule"].queryset})
    return render(request, "billing/run.html", ctx)


def _describe_post(request):
    schedule = AssessmentSchedule.objects.filter(pk=request.POST.get("schedule") or 0).first()
    period = request.POST.get("period", "")
    try:
        month = date_format(dt.date.fromisoformat(f"{period}-01"), "F Y")
    except ValueError:
        month = period
    return (
        _("Post %(month)s dues") % {"month": month},
        [(_("Schedule"), str(schedule or "")), (_("Month"), month)],
    )


@money_change("money.dues_run", _l("Post monthly dues"), describe=_describe_post)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def post(request):
    form = RunForm(request.POST, association=request.association)
    if not form.is_valid():
        ctx = _shell(_("Run assessment"))
        ctx.update({"form": form, "plan": None, "has_schedule": True})
        return render(request, "billing/run.html", ctx, status=400)
    period = form.cleaned_data["period"]
    month = date_format(period, "F Y")
    try:
        result = generate_assessments(form.cleaned_data["schedule"], period, posted_by=request.user)
    except BillingError as exc:
        messages.error(request, str(exc))
        schedule_pk = form.cleaned_data["schedule"].pk
        return redirect(f"{reverse('billing:run')}?period={period:%Y-%m}&schedule={schedule_pk}")
    if result.created:
        n = result.run.charge_count
        messages.success(
            request,
            ngettext(
                "Posted %(n)s charge for %(month)s.",
                "Posted %(n)s charges for %(month)s.",
                n,
            )
            % {"n": n, "month": month},
        )
    else:
        messages.info(
            request,
            _("%(month)s is already posted; 0 charges created.") % {"month": month},
        )
    return redirect("billing:run_detail", pk=result.run.pk)


@require_perm(perms.FINANCE_READ)
def run_detail(request, pk):
    billing_run = get_object_or_404(BillingRun.objects.select_related("schedule"), pk=pk)
    charges = Charge.objects.filter(run=billing_run).select_related("unit")
    ctx = _shell(_("Assessment run"))
    ctx.update(
        {
            "run": billing_run,
            "charges": sorted(charges, key=lambda c: (c.unit.building, _nat(c.unit.number))),
        }
    )
    return render(request, "billing/run_detail.html", ctx)


def _nat(number):
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _describe_schedule(request, pk=None):
    p = request.POST
    current = AssessmentSchedule.objects.filter(pk=pk).first() if pk else None
    summary = (
        _("Change dues schedule %(name)s") % {"name": current.name}
        if current
        else _("New dues schedule %(name)s") % {"name": p.get("name", "")}
    )
    details = [
        (_("Name"), p.get("name", "")),
        (_("Annual amount"), p.get("annual_amount", "")),
        (_("Per-period amount"), p.get("period_amount", "")),
        (_("Reserve per year"), p.get("reserve_annual_amount", "")),
        (_("Insurance per year"), p.get("insurance_annual_amount", "")),
        (_("Starts"), p.get("effective_start", "")),
        (_("Ends"), p.get("effective_end", "")),
    ]
    if current:
        details.insert(0, (_("Annual amount now"), f"${current.annual_amount or 0:,.2f}"))
    return summary, [(k, v) for k, v in details if v]


def _default_schedule_start(association):
    """This month's 1st; while setting up, the first 1st on or after the go-live date."""
    from homeoa.core import status

    go_live = status.setup_start(association)
    if go_live is None:
        return month_start(association_today(association))
    if go_live.day == 1:
        return go_live
    return month_start(go_live.replace(day=28) + dt.timedelta(days=4))


# Dues changes are approved by a board resolution.
@money_change(
    "money.dues_schedule", _l("Dues schedule"), by_resolution=True, describe=_describe_schedule
)
@require_perm(perms.FINANCE_MANAGE)
def schedule_edit(request, pk=None):
    association = request.association
    instance = get_object_or_404(AssessmentSchedule, pk=pk) if pk else None
    if instance is None:
        instance = AssessmentSchedule(
            association=association,
            effective_start=_default_schedule_start(association),
            **default_schedule_kwargs(association),
        )
    form = ScheduleForm(request.POST or None, instance=instance, association=association)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Schedule saved."))
        return redirect("billing:index")
    ctx = _shell(_("Assessment schedule"))
    ctx.update({"form": form, "title": _("Assessment schedule")})
    return render(request, "billing/schedule_form.html", ctx)
