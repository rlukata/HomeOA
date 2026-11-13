"""The month's money in plain words: a few sentences written by code from the books.

Shown at the top of the monthly board packet and on Money > Overview, for board members
who don't read financial statements. Each sentence puts into words a figure the reports
already show (cash by fund, receivables aging, budget vs actual, bills paid); nothing here
is written by AI.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from homeoa.ledger.models import AccountType
from homeoa.ledger.templatetags.ledger_tags import money

ZERO = Decimal("0.00")
# Aging buckets more than 30 days past due.
BEHIND = ("d31", "d61", "d90")
# At most this many budget lines are named as already over the year's budget.
MAX_OVERRUNS = 3


def money_sentences(association, start: dt.date, end: dt.date) -> list[str]:
    """Cash by fund and how it changed from ``start`` to ``end``, what owners owe at
    ``end``, spending this fiscal year against the adopted budget, and the bills paid from
    ``start`` to ``end``."""
    return [
        *_cash(association, start, end),
        _owed(association, end),
        *_spending(association, end),
        _bills_paid(association, start, end),
    ]


def _cash(association, start, end) -> list[str]:
    from homeoa.dashboard.cards import cash_by_fund

    before = dict(cash_by_fund(association, as_of=start - dt.timedelta(days=1)))
    out = []
    for fund, balance in cash_by_fund(association, as_of=end):
        change = balance - before.get(fund, ZERO)
        values = {"fund": fund.name, "change": money(abs(change)), "balance": money(balance)}
        if change > 0:
            out.append(_("%(fund)s cash rose %(change)s to %(balance)s.") % values)
        elif change < 0:
            out.append(_("%(fund)s cash fell %(change)s to %(balance)s.") % values)
        else:
            out.append(_("%(fund)s cash stayed at %(balance)s.") % values)
    return out


def _owed(association, end) -> str:
    from . import queries

    aging = queries.ar_aging(as_of=end, association=association)
    owed = sum((r.total for r in aging.rows if r.total > 0), ZERO)
    if not owed:
        return _("No owner owes anything.")
    behind = sum(1 for r in aging.rows if any(r.buckets[key] > 0 for key in BEHIND))
    if not behind:
        return _("Owners owe %(owed)s in total; none of it is more than 30 days past due.") % {
            "owed": money(owed)
        }
    return ngettext(
        "Owners owe %(owed)s in total; %(n)s unit is more than 30 days behind.",
        "Owners owe %(owed)s in total; %(n)s units are more than 30 days behind.",
        behind,
    ) % {"owed": money(owed), "n": behind}


def _spending(association, end) -> list[str]:
    from homeoa.budget.services import adopted_for, budget_vs_actual, months_elapsed

    from .statements import fiscal_year_start

    budget = adopted_for(association, fiscal_year_start(association, end).year)
    if budget is None:
        return [_("No budget is adopted for this year, so spending isn't compared with one.")]
    report = budget_vs_actual(budget, end)
    if not report.expense_budget:
        return []
    used = report.expense_used_pct.quantize(Decimal(1), ROUND_HALF_UP)
    # Months fully over by the end of ``end`` (a month-end date counts its own month).
    months = months_elapsed(association, budget.year, end + dt.timedelta(days=1))
    out = [
        _(
            "Spending this year so far is %(actual)s, %(pct)s%% of the %(budget)s budget, "
            "with %(months)s of 12 months gone."
        )
        % {
            "actual": money(report.expense_actual),
            "pct": used,
            "budget": money(report.expense_budget),
            "months": months,
        }
    ]
    over = sorted(
        (
            r
            for _group, rows in report.groups
            for r in rows
            if r.account.type == AccountType.EXPENSE and r.actual > r.budget
        ),
        key=lambda r: -r.variance,
    )[:MAX_OVERRUNS]
    if over:
        lines = ", ".join(
            _("%(line)s by %(amount)s") % {"line": r.label, "amount": money(r.variance)}
            for r in over
        )
        out.append(_("Already over the year's budget: %(lines)s.") % {"lines": lines})
    return out


def _bills_paid(association, start, end) -> str:
    from homeoa.ap.models import Bill

    bills = list(
        Bill.all_associations.filter(
            association=association,
            status=Bill.Status.PAID,
            paid_on__gte=start,
            paid_on__lte=end,
        ).prefetch_related("lines")
    )
    if not bills:
        return _("No bills were paid.")
    total = sum((b.total for b in bills), ZERO)
    return ngettext(
        "%(n)s bill was paid (%(total)s).", "%(n)s bills were paid (%(total)s).", len(bills)
    ) % {"n": len(bills), "total": money(total)}
