"""Budgets: drafts, adoption, budget vs actual, and the dues schedule they drive (FIN-13, FIN-03).

* A year can have any number of drafts and at most one adopted budget. Adopting a draft
  supersedes the year's previously adopted budget. Only an adopted budget drives dues.
* Budget vs actual reads the accrual ledger per (account, fund) from the fiscal year start
  to ``as_of``. "% of year elapsed" counts completed months (Oct 7 -> 9/12 = 75%).
* :func:`schedule_from_budget` builds an assessment schedule: the operating assessment total
  is the budget's lines on the schedule's income account in the operating fund; the
  reserve and insurance transfers are the lines on their transfer expense accounts.
  Per-unit dues are billing's fixed per-unit amounts. Existing schedules for the year stay untouched
  unless the caller passes ``replace=True`` (the board confirmed).
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.billing import services as billing
from homeoa.billing.models import AssessmentSchedule, BillingRun, Method
from homeoa.core.fields import ZERO
from homeoa.ledger.models import DEBIT_NORMAL, JournalLine

from .models import Budget, BudgetLine, BudgetStatus


class BudgetError(Exception):
    pass


# ------------------------------------------------------------------ dates


def fiscal_start_month(association) -> int:
    return association.fiscal_year_start_month or 1


def year_bounds(association, year: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, fiscal_start_month(association), 1)
    return start, dt.date(year + 1, start.month, 1) - dt.timedelta(days=1)


def months_elapsed(association, year: int, as_of: dt.date) -> int:
    start, end = year_bounds(association, year)
    if as_of < start:
        return 0
    if as_of >= end:
        return 12
    return (as_of.year - start.year) * 12 + as_of.month - start.month


def pct(part: Decimal, whole: Decimal) -> Decimal | None:
    if not whole:
        return None
    return (Decimal(part) * 100 / whole).quantize(Decimal("0.1"), ROUND_HALF_UP)


# ------------------------------------------------------------------ editing


def create_budget(association, year: int, *, name: str = "", copy_from: Budget | None = None):
    """A new draft for ``year``, optionally with the lines of ``copy_from``."""
    budget = Budget.objects.create(
        association=association,
        year=year,
        name=name or _("%(year)s budget (draft)") % {"year": year},
    )
    if copy_from is not None:
        BudgetLine.objects.bulk_create(
            BudgetLine(
                association=association,
                budget=budget,
                group=line.group,
                label=line.label,
                account_id=line.account_id,
                fund_id=line.fund_id,
                amount=line.amount,
                note=line.note,
                sort_order=line.sort_order,
            )
            for line in copy_from.lines.all()
        )
    return budget


def require_draft(budget: Budget) -> None:
    if not budget.is_draft:
        raise BudgetError(_("Only a draft budget can be changed."))


@transaction.atomic
def adopt(budget: Budget, user=None) -> Budget:
    """Adopt a draft; the year's previous adopted budget becomes superseded. Audited."""
    budget = Budget.objects.select_for_update().get(pk=budget.pk)
    require_draft(budget)
    if not budget.lines.exists():
        raise BudgetError(_("Add budget lines before adopting."))
    previous = list(Budget.objects.filter(year=budget.year, status=BudgetStatus.ADOPTED))
    for old in previous:
        old.status = BudgetStatus.SUPERSEDED
        old.save(update_fields=["status"])
    budget.status = BudgetStatus.ADOPTED
    budget.adopted_at = timezone.now()
    budget.adopted_by = user if getattr(user, "is_authenticated", False) else None
    budget.save(update_fields=["status", "adopted_at", "adopted_by"])
    audit.record(
        "budget.adopt",
        budget,
        changes={"year": budget.year, "superseded": [b.pk for b in previous]},
    )
    return budget


def adopted_for(association, year: int) -> Budget | None:
    return Budget.all_associations.filter(
        association=association, year=year, status=BudgetStatus.ADOPTED
    ).first()


# ------------------------------------------------------------------ totals


@dataclass
class Group:
    name: str
    lines: list = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), ZERO)


def grouped(budget: Budget) -> list[Group]:
    groups: OrderedDict[str, Group] = OrderedDict()
    for line in budget.lines.select_related("account", "fund"):
        groups.setdefault(line.group, Group(line.group)).lines.append(line)
    return list(groups.values())


def _is_income(line) -> bool:
    return line.account.type == "income"


def totals(budget: Budget) -> dict:
    lines = list(budget.lines.select_related("account"))
    income = sum((ln.amount for ln in lines if _is_income(ln)), ZERO)
    expense = sum((ln.amount for ln in lines if ln.account.type == "expense"), ZERO)
    return {"income": income, "expense": expense, "net": income - expense}


@dataclass
class CompareRow:
    group: str
    label: str
    left: Decimal | None
    right: Decimal | None
    note: str = ""

    @property
    def change(self) -> Decimal | None:
        if self.left is None or self.right is None:
            return None
        return self.right - self.left

    @property
    def change_pct(self):
        return pct(self.change, self.left) if self.change is not None else None


def compare(left: Budget | None, right: Budget | None) -> list[CompareRow]:
    """Side by side by (group, line label): ``left`` (adopted) vs ``right`` (draft)."""
    rows: OrderedDict[tuple, CompareRow] = OrderedDict()
    for side, budget in (("left", left), ("right", right)):
        if budget is None:
            continue
        for ln in budget.lines.all():
            row = rows.setdefault((ln.group, ln.label), CompareRow(ln.group, ln.label, None, None))
            setattr(row, side, ln.amount)
            if side == "right" or not row.note:
                row.note = ln.note or row.note
    return list(rows.values())


# ------------------------------------------------------------------ budget vs actual


@dataclass
class ActualRow:
    group: str
    label: str
    account: object
    fund: object
    budget: Decimal
    actual: Decimal

    @property
    def variance(self) -> Decimal:
        """Actual minus budget, so overspending on expense and extra income are positive."""
        return self.actual - self.budget

    @property
    def variance_pct(self):
        return pct(self.variance, self.budget)

    @property
    def used_pct(self):
        return pct(self.actual, self.budget)


@dataclass
class BudgetVsActual:
    budget: Budget
    start: dt.date
    as_of: dt.date
    months: int
    groups: list[tuple[str, list[ActualRow]]]

    @property
    def elapsed_pct(self) -> Decimal:
        return pct(Decimal(self.months), Decimal(12))

    def _rows(self, kind):
        return [r for _g, rows in self.groups for r in rows if r.account.type == kind]

    @property
    def expense_budget(self) -> Decimal:
        return sum((r.budget for r in self._rows("expense")), ZERO)

    @property
    def expense_actual(self) -> Decimal:
        return sum((r.actual for r in self._rows("expense")), ZERO)

    @property
    def income_budget(self) -> Decimal:
        return sum((r.budget for r in self._rows("income")), ZERO)

    @property
    def income_actual(self) -> Decimal:
        return sum((r.actual for r in self._rows("income")), ZERO)

    @property
    def expense_used_pct(self):
        return pct(self.expense_actual, self.expense_budget)


def _actual(association, account, fund, start, end) -> Decimal:
    agg = JournalLine.all_associations.filter(
        association=association,
        account=account,
        fund=fund,
        entry__date__gte=start,
        entry__date__lte=end,
    ).aggregate(d=Sum("debit"), c=Sum("credit"))
    net = (agg["d"] or ZERO) - (agg["c"] or ZERO)
    return net if account.type in DEBIT_NORMAL else -net


def budget_vs_actual(budget: Budget, as_of: dt.date) -> BudgetVsActual:
    """Per line, per (account, fund): lines sharing an account and fund are combined so the
    ledger actual is counted once."""
    association = budget.association
    start, end = year_bounds(association, budget.year)
    through = min(as_of, end)
    groups: OrderedDict[str, list[ActualRow]] = OrderedDict()
    seen: dict[tuple, ActualRow] = {}
    for line in budget.lines.select_related("account", "fund"):
        key = (line.account_id, line.fund_id)
        if key in seen:
            row = seen[key]
            row.budget += line.amount
            row.label = f"{row.label}, {line.label}"
            continue
        actual = _actual(association, line.account, line.fund, start, through)
        row = ActualRow(line.group, line.label, line.account, line.fund, line.amount, actual)
        seen[key] = row
        groups.setdefault(line.group, []).append(row)
    return BudgetVsActual(
        budget=budget,
        start=start,
        as_of=through,
        months=months_elapsed(association, budget.year, as_of),
        groups=list(groups.items()),
    )


# ------------------------------------------------------------------ dues (FIN-03)


@dataclass
class DuesPlan:
    budget: Budget
    schedule: AssessmentSchedule  # unsaved
    unit_amounts: list  # [(unit, monthly)]
    conflicts: list  # existing active schedules overlapping the year

    @property
    def monthly_total(self) -> Decimal:
        return sum((a for _u, a in self.unit_amounts), ZERO)

    @property
    def yearly_total(self) -> Decimal:
        return self.monthly_total * 12


def _sum_on(budget: Budget, account, fund=None) -> Decimal:
    if account is None:
        return ZERO
    qs = budget.lines.filter(account=account)
    if fund is not None:
        qs = qs.filter(fund=fund)
    return qs.aggregate(t=Sum("amount"))["t"] or ZERO


def _next_month(day: dt.date) -> dt.date:
    return dt.date(day.year + day.month // 12, day.month % 12 + 1, 1)


def plan_dues(budget: Budget) -> DuesPlan:
    """The schedule an adopted budget implies, with fixed per-unit amounts. Writes nothing."""
    if not budget.is_adopted:
        raise BudgetError(_("Only an adopted budget sets dues. Adopt the budget first."))
    association = budget.association
    start, end = year_bounds(association, budget.year)
    kwargs = billing.default_schedule_kwargs(association)
    schedule = AssessmentSchedule(
        association=association,
        name=_("%(year)s dues (from budget)") % {"year": budget.year},
        method=Method.ALLOCATED_INTEREST,
        effective_start=start,
        effective_end=end,
        **kwargs,
    )
    schedule.annual_amount = _sum_on(budget, schedule.income_account, schedule.operating_fund)
    if not schedule.annual_amount:
        raise BudgetError(_("The budget has no assessment income line in the operating fund."))
    schedule.reserve_annual_amount = _sum_on(budget, schedule.reserve_expense_account)
    schedule.insurance_annual_amount = _sum_on(budget, schedule.insurance_expense_account)
    units = billing._units(association)
    dues = billing.unit_dues(schedule, units)
    conflicts = [
        s
        for s in AssessmentSchedule.all_associations.filter(association=association, active=True)
        if s.effective_start <= end and (s.effective_end is None or s.effective_end >= start)
    ]
    # Never re-bill a month: start the first month after the last run of any overlapping
    # schedule in this fiscal year (runs are unique per schedule and period only).
    last_run = (
        BillingRun.all_associations.filter(
            schedule__in=conflicts, period__gte=start, period__lte=end
        )
        .order_by("-period")
        .values_list("period", flat=True)
        .first()
    )
    if last_run is not None:
        start = _next_month(last_run)
        if start > end:
            raise BudgetError(_("Every month of this fiscal year is already billed."))
        schedule.effective_start = start
    return DuesPlan(
        budget=budget,
        schedule=schedule,
        unit_amounts=[(u, dues[u.pk]) for u in units if u.pk in dues],
        conflicts=conflicts,
    )


@transaction.atomic
def schedule_from_budget(budget: Budget, *, replace: bool = False) -> AssessmentSchedule:
    """Save the planned schedule. Overlapping schedules block unless ``replace`` (board
    confirmed): they then end the day before the new one starts, or are deactivated."""
    plan = plan_dues(budget)
    if plan.monthly_total == ZERO:
        raise BudgetError(_("No unit dues could be computed. Check the allocated interests."))
    start = plan.schedule.effective_start
    if plan.conflicts and not replace:
        raise BudgetError(_("A dues schedule already covers this year. Confirm to replace it."))
    for old in plan.conflicts:
        if old.effective_start < start:
            old.effective_end = start - dt.timedelta(days=1)
            old.save(update_fields=["effective_end"])
        elif old.runs.exists():
            raise BudgetError(
                _("Schedule %(name)s has billed months; it cannot be replaced.")
                % {"name": old.name}
            )
        else:
            old.active = False
            old.save(update_fields=["active"])
    schedule = plan.schedule
    schedule.full_clean()
    schedule.save()
    audit.record("budget.dues_schedule", budget, changes={"schedule": schedule.pk})
    return schedule
