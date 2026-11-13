"""Period close services (FIN-15).

* ``checklist(association, start, end)``: the items that must pass before a close.
  - Bank reconciliations: every active bank account has a completed reconciliation whose
    statement date falls on or after ``end`` (covers the whole period).
  - No draft bills dated on or before ``end``.
  - The trial balance as of ``end`` balances, in total and per fund.
* ``close_month`` / ``close_year``: run the checklist, record a :class:`Close`, move the
  ledger lock forward (``set_locked_through``). Closes go forward only.
* ``close_year`` also posts one closing entry (``source_type="ledger.close"``, dated the
  fiscal year end) that zeroes every income and expense account per fund into that fund's
  fund balance account.
* ``reopen``: needs a reason; reopens the close and every later active close, reverses
  their closing entries (dated the period end) and moves the lock back. Audited.

Permission (``finance.manage``) is checked by the views.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.ap.models import Bill
from homeoa.audit.services import record
from homeoa.banking.models import BankAccount, Reconciliation
from homeoa.core.fields import ZERO
from homeoa.events import publish
from homeoa.ledger import services as ledger
from homeoa.ledger.chart import CLOSE_SOURCE, FUND_BALANCE_CODES
from homeoa.ledger.models import AccountType, Fund, JournalLine

from .events import MonthClosed
from .models import Close, CloseKind


class CloseError(ValueError):
    pass


@dataclass
class CheckItem:
    key: str
    label: str
    ok: bool
    detail: str = ""


def month_bounds(year: int, month: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def fiscal_year_bounds(association, year: int) -> tuple[dt.date, dt.date]:
    """The fiscal year that starts in calendar ``year``."""
    month = association.fiscal_year_start_month or 1
    start = dt.date(year, month, 1)
    end = dt.date(year + 1, month, 1) - dt.timedelta(days=1)
    return start, end


def checklist(association, start: dt.date, end: dt.date) -> list[CheckItem]:
    items = []
    missing = [
        acct.name
        for acct in BankAccount.all_associations.filter(association=association, active=True)
        if not Reconciliation.all_associations.filter(
            association=association,
            bank_account=acct,
            completed_at__isnull=False,
            statement_date__gte=end,
        ).exists()
    ]
    items.append(
        CheckItem(
            "bank_recs",
            gettext_lazy("Bank reconciliations done"),
            not missing,
            _("Not reconciled: %(a)s") % {"a": ", ".join(missing)} if missing else "",
        )
    )
    drafts = Bill.all_associations.filter(
        association=association, status=Bill.Status.DRAFT, bill_date__lte=end
    ).count()
    items.append(
        CheckItem(
            "draft_bills",
            gettext_lazy("No draft bills"),
            drafts == 0,
            _("%(n)s draft bill(s) dated in or before the period") % {"n": drafts}
            if drafts
            else "",
        )
    )
    per_fund = (
        JournalLine.all_associations.filter(association=association, entry__date__lte=end)
        .values("fund__name")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    off = [r["fund__name"] for r in per_fund if (r["d"] or ZERO) != (r["c"] or ZERO)]
    items.append(
        CheckItem(
            "trial_balance",
            gettext_lazy("Trial balance balances"),
            not off,
            _("Out of balance: %(f)s") % {"f": ", ".join(off)} if off else "",
        )
    )
    return items


def _active(association):
    return Close.all_associations.filter(association=association, reopened_at__isnull=True)


def _sync_lock(association) -> dt.date | None:
    latest = _active(association).aggregate(m=Max("period_end"))["m"]
    ledger.set_locked_through(association, latest)
    return latest


def _guard(association, start, end):
    locked = ledger.get_locked_through(association)
    if locked is not None and end <= locked:
        raise CloseError(_("The books are already locked through %(d)s.") % {"d": locked})
    if _active(association).filter(period_end__gt=end).exists():
        raise CloseError(_("A later period is closed. Reopen it first."))
    failed = [i for i in checklist(association, start, end) if not i.ok]
    if failed:
        raise CloseError(
            _("The checklist is not complete: %(items)s")
            % {"items": ", ".join(str(i.label) for i in failed)}
        )


def close_month(association, year: int, month: int, *, user=None) -> Close:
    start, end = month_bounds(year, month)
    with transaction.atomic():
        _guard(association, start, end)
        close = Close.all_associations.create(
            association=association,
            kind=CloseKind.MONTH,
            period_start=start,
            period_end=end,
            closed_by=_user(user),
        )
        _sync_lock(association)
        record("closing.close", close, actor=_user(user), message=f"month {end:%Y-%m}")
        closed_by = _user(user)
        publish(
            MonthClosed(
                association_id=association.pk,
                close_id=close.pk,
                year=year,
                month=month,
                closed_by_id=closed_by.pk if closed_by else None,
            )
        )
    return close


def closing_lines(association, start: dt.date, end: dt.date) -> list[ledger.Line]:
    """Lines that zero every income/expense account per fund into the fund's fund balance."""
    rows = (
        JournalLine.all_associations.filter(
            association=association,
            entry__date__gte=start,
            entry__date__lte=end,
            account__type__in=(AccountType.INCOME, AccountType.EXPENSE),
        )
        .values("account__code", "fund__code")
        .annotate(d=Sum("debit"), c=Sum("credit"))
        .order_by("fund__code", "account__code")
    )
    lines: list[ledger.Line] = []
    fund_net: dict[str, object] = defaultdict(lambda: ZERO)  # net debit per fund
    for r in rows:
        net = (r["d"] or ZERO) - (r["c"] or ZERO)
        if net == 0:
            continue
        fund = r["fund__code"]
        fund_net[fund] += net
        if net > 0:
            lines.append(ledger.Line(account=r["account__code"], fund=fund, credit=net))
        else:
            lines.append(ledger.Line(account=r["account__code"], fund=fund, debit=-net))
    funds = {f.code: f for f in Fund.all_associations.filter(association=association)}
    for fund, net in fund_net.items():
        if net == 0:
            continue
        code = FUND_BALANCE_CODES.get(fund)
        if code is None:
            raise CloseError(
                _("No fund balance account is set for the %(f)s fund.") % {"f": funds[fund]}
            )
        if net > 0:  # net loss: expenses exceeded income
            lines.append(ledger.Line(account=code, fund=fund, debit=net))
        else:
            lines.append(ledger.Line(account=code, fund=fund, credit=-net))
    return lines


def close_year(association, year: int, *, user=None) -> Close:
    start, end = fiscal_year_bounds(association, year)
    with transaction.atomic():
        if _active(association).filter(kind=CloseKind.YEAR, period_end=end).exists():
            raise CloseError(_("This year is already closed."))
        if _active(association).filter(period_end__gt=end).exists():
            raise CloseError(_("A later period is closed. Reopen it first."))
        failed = [i for i in checklist(association, start, end) if not i.ok]
        if failed:
            raise CloseError(
                _("The checklist is not complete: %(items)s")
                % {"items": ", ".join(str(i.label) for i in failed)}
            )
        close = Close.all_associations.create(
            association=association,
            kind=CloseKind.YEAR,
            period_start=start,
            period_end=end,
            closed_by=_user(user),
        )
        lines = closing_lines(association, start, end)
        if lines:
            # The last month may already be closed: lift the lock just enough to post.
            locked = ledger.get_locked_through(association)
            if locked is not None and locked >= end:
                ledger.set_locked_through(association, end - dt.timedelta(days=1))
            close.entry = ledger.post_entry(
                lines,
                date=end,
                memo=_("Year-end close %(y)s") % {"y": f"{start:%Y-%m}–{end:%Y-%m}"},
                source_type=CLOSE_SOURCE,
                source_id=close.pk,
                posted_by=_user(user),
                association=association,
            )
            close.save(update_fields=["entry"])
        _sync_lock(association)
        record("closing.close", close, actor=_user(user), message=f"year {start}..{end}")
    return close


def reopen(close: Close, *, reason: str, user=None) -> list[Close]:
    """Reopen ``close`` and every later active close. Returns the closes reopened."""
    reason = (reason or "").strip()
    if not reason:
        raise CloseError(_("A reason is required to reopen a period."))
    if not close.is_active:
        raise CloseError(_("This period is already reopened."))
    association = close.association
    with transaction.atomic():
        affected = list(
            _active(association)
            .filter(period_end__gte=close.period_end)
            .order_by("-period_end", "-pk")
        )
        ledger.set_locked_through(association, close.period_start - dt.timedelta(days=1))
        now = timezone.now()
        for c in affected:
            if c.entry_id:
                c.reversal_entry = ledger.reverse_entry(
                    c.entry,
                    date=c.period_end,
                    memo=_("Reopen: reversal of year-end close"),
                    posted_by=_user(user),
                )
            c.reopened_at = now
            c.reopened_by = _user(user)
            c.reopen_reason = reason
            c.save(update_fields=["reversal_entry", "reopened_at", "reopened_by", "reopen_reason"])
            record("closing.reopen", c, actor=_user(user), message=reason[:500])
        _sync_lock(association)
    return affected


def _user(user):
    return user if getattr(user, "pk", None) else None
