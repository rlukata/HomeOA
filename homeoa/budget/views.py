"""Money hub > Budget: drafts and adopted budgets, budget vs actual, dues from budget."""

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.changes.forms import add_resolution_field
from homeoa.changes.replay import money_change
from homeoa.core.context import association_today
from homeoa.ui.nav import get_hub

from . import services
from .forms import LineFormSet, NewBudgetForm
from .models import Budget, BudgetStatus


def _shell(title):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "budget"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "budget"), None)
    return {"current_hub": hub, "subtabs": tabs, "current_tab": current, "heading": title}


def _can_manage(request) -> bool:
    return has_perm(request.user, perms.FINANCE_MANAGE, association=request.association)


@require_perm(perms.FINANCE_READ)
def index(request):
    """One year at a time: the adopted budget and the latest draft side by side."""
    budgets = list(Budget.objects.all())
    years = sorted({b.year for b in budgets}, reverse=True)
    today = association_today(request.association)
    try:
        year = int(request.GET.get("year") or 0)
    except ValueError:
        year = 0
    if year not in years:
        year = today.year if today.year in years else (years[0] if years else today.year)
    in_year = [b for b in budgets if b.year == year]
    adopted = next((b for b in in_year if b.status == BudgetStatus.ADOPTED), None)
    drafts = [b for b in in_year if b.status == BudgetStatus.DRAFT]
    draft = drafts[-1] if drafts else None
    rows = services.compare(adopted, draft)
    groups = []
    for row in rows:
        if not groups or groups[-1]["name"] != row.group:
            groups.append({"name": row.group, "rows": [], "left": None, "right": None})
        g = groups[-1]
        g["rows"].append(row)
        for side in ("left", "right"):
            val = getattr(row, side)
            if val is not None:
                g[side] = (g[side] or 0) + val
    ctx = _shell(_("Budget"))
    ctx.update(
        {
            "years": years,
            "year": year,
            "adopted": adopted,
            "draft": draft,
            "drafts": drafts,
            "others": [b for b in in_year if b not in (adopted, draft)],
            "groups": groups,
            "adopted_totals": services.totals(adopted) if adopted else None,
            "draft_totals": services.totals(draft) if draft else None,
            "can_manage": _can_manage(request),
        }
    )
    return render(request, "budget/index.html", ctx)


@require_perm(perms.FINANCE_MANAGE)
def new(request):
    form = NewBudgetForm(
        request.POST or None, initial={"year": association_today(request.association).year + 1}
    )
    if request.method == "POST" and form.is_valid():
        budget = services.create_budget(
            request.association,
            form.cleaned_data["year"],
            name=form.cleaned_data["name"],
            copy_from=form.cleaned_data["copy_from"],
        )
        messages.success(request, _("Draft budget created."))
        return redirect("budget:lines", pk=budget.pk)
    ctx = _shell(_("New budget"))
    ctx.update({"form": form, "title": _("New budget")})
    return render(request, "budget/new.html", ctx)


@require_perm(perms.FINANCE_READ)
def detail(request, pk):
    budget = get_object_or_404(Budget, pk=pk)
    ctx = _shell(budget.name)
    ctx.update(
        {
            "budget": budget,
            "groups": services.grouped(budget),
            "totals": services.totals(budget),
            "can_manage": _can_manage(request),
        }
    )
    return render(request, "budget/detail.html", ctx)


@require_perm(perms.FINANCE_MANAGE)
def lines(request, pk):
    budget = get_object_or_404(Budget, pk=pk)
    if not budget.is_draft:
        messages.error(request, _("Only a draft budget can be changed."))
        return redirect("budget:detail", pk=budget.pk)
    formset = LineFormSet(request.POST or None, instance=budget)
    if request.method == "POST" and formset.is_valid():
        formset.save()
        messages.success(request, _("Budget lines saved."))
        return redirect("budget:detail", pk=budget.pk)
    ctx = _shell(budget.name)
    ctx.update({"budget": budget, "formset": formset})
    return render(request, "budget/lines.html", ctx)


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def adopt(request, pk):
    budget = get_object_or_404(Budget, pk=pk)
    try:
        services.adopt(budget, request.user)
    except services.BudgetError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Budget adopted."))
    return redirect("budget:detail", pk=budget.pk)


@require_perm(perms.FINANCE_READ)
def actual(request, pk):
    budget = get_object_or_404(Budget, pk=pk)
    report = services.budget_vs_actual(budget, association_today(request.association))
    ctx = _shell(_("Budget vs actual"))
    ctx.update({"budget": budget, "report": report})
    return render(request, "budget/actual.html", ctx)


class DuesForm(forms.Form):
    replace = forms.BooleanField(required=False)

    def __init__(self, *args, association=None, **kwargs):
        super().__init__(*args, **kwargs)
        add_resolution_field(self, association)


def _describe_dues(request, pk):
    budget = Budget.objects.filter(pk=pk).first()
    label = str(budget) if budget else f"#{pk}"
    try:
        plan = services.plan_dues(budget) if budget else None
    except services.BudgetError:
        plan = None
    details = [(_("Budget"), label)]
    if plan is not None:
        details.append((_("Monthly total"), f"${plan.monthly_total:,.2f}"))
        details.append((_("Starts"), str(plan.schedule.effective_start)))
    return _("Set dues from the %(budget)s budget") % {"budget": label}, details


# Dues changes are approved by a board resolution and must follow a ratified budget.
@money_change(
    "money.dues_from_budget", _l("Dues from a budget"), by_resolution=True, describe=_describe_dues
)
@require_perm(perms.FINANCE_MANAGE)
def dues(request, pk):
    """Preview the per-unit dues an adopted budget implies; POST creates the schedule."""
    budget = get_object_or_404(Budget, pk=pk)
    try:
        plan = services.plan_dues(budget)
    except services.BudgetError as exc:
        messages.error(request, str(exc))
        return redirect("budget:detail", pk=budget.pk)
    form = DuesForm(request.POST or None, association=request.association)
    if request.method == "POST" and form.is_valid():
        if budget.ratification != "ratified":
            form.add_error(
                None,
                _(
                    "Owners have not ratified this budget yet. Dues follow a ratified budget: "
                    "record the ratification vote at the annual meeting first."
                ),
            )
        else:
            try:
                services.schedule_from_budget(budget, replace=form.cleaned_data["replace"])
            except services.BudgetError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, _("Dues schedule created from the budget."))
                return redirect("billing:index")
    ctx = _shell(_("Dues from budget"))
    ctx.update({"budget": budget, "plan": plan, "form": form})
    return render(request, "budget/dues.html", ctx)
