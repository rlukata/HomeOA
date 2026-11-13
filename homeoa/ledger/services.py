"""Ledger posting and query services. The ONLY write path into the general ledger.

Posting
-------
``post_entry(lines, *, date, memo="", source_type="", source_id="", posted_by=None,
association=None) -> JournalEntry``

Each line is a :class:`Line` (or a dict with the same keys)::

    Line(account="1100", fund="operating", debit=Decimal("335.40"), unit_id=3)

``account`` / ``fund`` are model instances or codes. Exactly one of ``debit`` / ``credit``
is a positive ``Decimal`` with at most 2 places. ``unit_id`` / ``ownership_period_id`` tag
the line to the unit subledger. The entry must balance in total *and* within each fund;
interfund transfers therefore go through "Due to/from other fund" lines in both funds.

Errors (all subclass :class:`LedgerError`, a ``ValueError``): ``InvalidLine``,
``UnbalancedEntry``, ``FundUnbalanced``, ``PeriodLocked``. On success the entry is audited
(``ledger.post``) and the ``ledger.entry_posted`` event is published.

``reverse_entry(entry, *, date=None, memo="", posted_by=None)`` posts the mirror image,
dated today in the association's time zone unless ``date`` is given.

Accrual posting conventions
----------------------------------
Unit-subledger lines carry ``unit_id`` and ``ownership_period_id``.

* Assessment billed: Dr 1100 Assessments receivable / Cr 4000 Assessment income.
* Receipt: Dr cash (1000 or 1010) / Cr 1100.
* Overpayment: the excess is Cr 2100 Prepaid assessments (tagged), not a negative A/R line.
* Credit applied to a later charge: Dr 2100 / Cr 1100 (both tagged).

Accounts flagged ``in_unit_subledger`` (1100, 2100) make up ``unit_balance``: positive means
the unit owes, negative means it has a credit (a $50 overpayment shows -50.00).

Sealing
-------
``post_entry`` inserts the entry, its lines, then sets ``sealed_at`` in one transaction. The
DB rejects lines for a sealed entry, any other entry UPDATE, and entries unsealed at commit.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa.audit.services import record
from homeoa.core.context import association_today, get_current_association
from homeoa.core.fields import ZERO

from .events import EntryPosted
from .models import Account, Fund, JournalEntry, JournalLine, LedgerSettings

CENT = Decimal("0.01")


class LedgerError(ValueError):
    """Base for every rejected posting."""


class InvalidLine(LedgerError):
    pass


class UnbalancedEntry(LedgerError):
    pass


class FundUnbalanced(LedgerError):
    pass


class PeriodLocked(LedgerError):
    pass


class EntryAlreadyReversed(LedgerError):
    pass


@dataclass(frozen=True, kw_only=True)
class Line:
    account: Account | str
    fund: Fund | str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    memo: str = ""
    unit_id: int | None = None
    ownership_period_id: int | None = None


def _money(value, what: str) -> Decimal:
    if not isinstance(value, Decimal) or isinstance(value, bool):
        raise InvalidLine(_("%(what)s must be a Decimal amount.") % {"what": what})
    if not value.is_finite() or value != value.quantize(CENT):
        raise InvalidLine(_("%(what)s must have at most two decimal places.") % {"what": what})
    if value < 0:
        raise InvalidLine(_("Amounts cannot be negative."))
    return value.quantize(CENT)


def _resolve(model, value, association):
    if isinstance(value, model):
        if value.association_id != association.pk:
            raise InvalidLine(_("%(obj)s belongs to another association.") % {"obj": value})
        return value
    try:
        return model.all_associations.get(association=association, code=value)
    except model.DoesNotExist:
        raise InvalidLine(
            _("Unknown %(kind)s %(code)s.") % {"kind": model._meta.verbose_name, "code": value}
        ) from None


def get_locked_through(association) -> dt.date | None:
    row = LedgerSettings.all_associations.filter(association=association).first()
    return row.locked_through if row else None


def set_locked_through(association, date: dt.date | None) -> None:
    """Period-lock hook for reconciliation and close."""
    LedgerSettings.all_associations.update_or_create(
        association=association, defaults={"locked_through": date}
    )


def _normalize(lines, association) -> list[JournalLine]:
    out = []
    for raw in lines:
        line = Line(**raw) if isinstance(raw, Mapping) else raw
        if not isinstance(line, Line):
            raise InvalidLine(_("Each line must be a Line."))
        debit = _money(line.debit, _("Debit"))
        credit = _money(line.credit, _("Credit"))
        if (debit > 0) == (credit > 0):
            raise InvalidLine(_("Each line needs exactly one of a debit or a credit."))
        account = _resolve(Account, line.account, association)
        fund = _resolve(Fund, line.fund, association)
        if not account.active:
            raise InvalidLine(_("Account %(a)s is inactive.") % {"a": account})
        if not fund.active:
            raise InvalidLine(_("Fund %(f)s is inactive.") % {"f": fund})
        out.append(
            JournalLine(
                association=association,
                account=account,
                fund=fund,
                debit=debit,
                credit=credit,
                memo=line.memo,
                unit_id=line.unit_id,
                ownership_period_id=line.ownership_period_id,
            )
        )
    return out


def _check_balanced(lines: list[JournalLine]) -> None:
    if len(lines) < 2:
        raise UnbalancedEntry(_("An entry needs at least two lines."))
    total = sum((ln.debit - ln.credit for ln in lines), ZERO)
    if total != 0:
        raise UnbalancedEntry(_("Debits and credits differ by %(d)s.") % {"d": f"{total:,.2f}"})
    per_fund: dict[int, Decimal] = defaultdict(lambda: ZERO)
    names = {}
    for ln in lines:
        per_fund[ln.fund.pk] += ln.debit - ln.credit
        names[ln.fund.pk] = ln.fund.name
    for fund_id, diff in per_fund.items():
        if diff != 0:
            raise FundUnbalanced(
                _("The %(fund)s fund does not balance (off by %(d)s).")
                % {"fund": names[fund_id], "d": f"{diff:,.2f}"}
            )


def post_entry(
    lines: Iterable[Line | Mapping],
    *,
    date: dt.date,
    memo: str = "",
    source_type: str = "",
    source_id: str | int = "",
    posted_by=None,
    association=None,
    _reversal_of: JournalEntry | None = None,
) -> JournalEntry:
    """Validate and post one balanced journal entry atomically. See the module docstring."""
    association = association or get_current_association()
    if association is None:
        raise LedgerError(_("No association to post to."))
    if not isinstance(date, dt.date) or isinstance(date, dt.datetime):
        raise LedgerError(_("An entry needs a date."))
    locked = get_locked_through(association)
    if locked is not None and date <= locked:
        raise PeriodLocked(_("The books are locked through %(d)s.") % {"d": locked.isoformat()})
    built = _normalize(list(lines), association)
    _check_balanced(built)

    with transaction.atomic():
        entry = JournalEntry(
            association=association,
            date=date,
            memo=memo,
            source_type=source_type,
            source_id=str(source_id),
            posted_by=posted_by if getattr(posted_by, "pk", None) else None,
            reversal_of=_reversal_of,
        )
        entry.save()
        for ln in built:
            ln.entry = entry
        JournalLine.all_associations.bulk_create(built)
        # Seal: the DB refuses further lines for this entry, and unsealed entries at commit.
        entry.sealed_at = timezone.now()
        JournalEntry.all_associations.filter(pk=entry.pk).update(sealed_at=entry.sealed_at)
        total = sum((ln.debit for ln in built), ZERO)
        record(
            "ledger.reverse" if _reversal_of else "ledger.post",
            entry,
            actor=posted_by if getattr(posted_by, "pk", None) else None,
            message=f"{total:.2f}",
        )
        from homeoa.events import publish

        publish(
            EntryPosted(
                association_id=association.pk,
                entry_id=entry.pk,
                entry_date=date,
                source_type=source_type,
                source_id=str(source_id),
                total=total,
                reversal_of_id=_reversal_of.pk if _reversal_of else None,
            )
        )
    return entry


def reverse_entry(
    entry: JournalEntry, *, date: dt.date | None = None, memo: str = "", posted_by=None
) -> JournalEntry:
    """Post the mirror image of ``entry`` (debits and credits swapped). Once per entry."""
    if JournalEntry.all_associations.filter(reversal_of=entry).exists():
        raise EntryAlreadyReversed(_("This entry has already been reversed."))
    if entry.reversal_of_id:
        raise EntryAlreadyReversed(_("A reversing entry cannot itself be reversed."))
    lines = [
        Line(
            account=ln.account,
            fund=ln.fund,
            debit=ln.credit,
            credit=ln.debit,
            memo=ln.memo,
            unit_id=ln.unit_id,
            ownership_period_id=ln.ownership_period_id,
        )
        for ln in entry.lines.select_related("account", "fund")
    ]
    return post_entry(
        lines,
        date=date or association_today(entry.association),
        memo=memo or _("Reversal of entry %(n)s") % {"n": entry.pk},
        source_type=entry.source_type,
        source_id=entry.source_id,
        posted_by=posted_by,
        association=entry.association,
        _reversal_of=entry,
    )


# ---------------------------------------------------------------- queries


def _lines(association, *, as_of=None, start=None, fund=None):
    qs = JournalLine.all_associations.filter(association=association)
    if as_of is not None:
        qs = qs.filter(entry__date__lte=as_of)
    if start is not None:
        qs = qs.filter(entry__date__gte=start)
    if fund is not None:
        qs = qs.filter(fund=fund)
    return qs


def _net(qs) -> Decimal:
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


def account_balance(account: Account, *, as_of=None, fund: Fund | None = None) -> Decimal:
    """Balance in the account's normal direction (debit-normal accounts: debits positive)."""
    net = _net(_lines(account.association, as_of=as_of, fund=fund).filter(account=account))
    return net if account.debit_normal else -net


def _subledger(association, as_of):
    return _lines(association, as_of=as_of).filter(account__in_unit_subledger=True)


def unit_balance(
    unit_id: int, *, as_of=None, ownership_period_id: int | None = None, association=None
) -> Decimal:
    """What the unit owes (negative = credit/prepaid): net of unit-subledger accounts.

    With ``ownership_period_id`` only that owner's lines count (seller/buyer split).
    """
    association = association or get_current_association()
    qs = _subledger(association, as_of).filter(unit_id=unit_id)
    if ownership_period_id is not None:
        qs = qs.filter(ownership_period_id=ownership_period_id)
    return _net(qs)


def ownership_balance(ownership_period_id: int, *, as_of=None, association=None) -> Decimal:
    """What one ownership period owes across its unit-subledger lines."""
    association = association or get_current_association()
    return _net(_subledger(association, as_of).filter(ownership_period_id=ownership_period_id))


@dataclass
class TrialBalanceRow:
    account: Account
    fund: Fund
    debit: Decimal
    credit: Decimal


@dataclass
class TrialBalance:
    as_of: dt.date | None
    rows: list[TrialBalanceRow] = field(default_factory=list)

    @property
    def total_debit(self) -> Decimal:
        return sum((r.debit for r in self.rows), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), ZERO)

    @property
    def difference(self) -> Decimal:
        return self.total_debit - self.total_credit


def trial_balance(*, as_of=None, fund: Fund | None = None, association=None) -> TrialBalance:
    """Net balance per account and fund, as debit or credit column. Always totals to zero."""
    association = association or get_current_association()
    agg = (
        _lines(association, as_of=as_of, fund=fund)
        .values("account", "fund")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    accounts = {a.pk: a for a in Account.all_associations.filter(association=association)}
    funds = {f.pk: f for f in Fund.all_associations.filter(association=association)}
    tb = TrialBalance(as_of=as_of)
    for row in agg:
        net = (row["d"] or ZERO) - (row["c"] or ZERO)
        if net == 0:
            continue
        tb.rows.append(
            TrialBalanceRow(
                account=accounts[row["account"]],
                fund=funds[row["fund"]],
                debit=net if net > 0 else ZERO,
                credit=-net if net < 0 else ZERO,
            )
        )
    tb.rows.sort(key=lambda r: (r.account.code, r.fund.sort_order, r.fund.code))
    return tb


@dataclass
class GLRow:
    line: JournalLine
    balance: Decimal  # running, debit positive


@dataclass
class GLAccount:
    account: Account
    opening: Decimal
    rows: list[GLRow]

    @property
    def closing(self) -> Decimal:
        return self.rows[-1].balance if self.rows else self.opening


def gl_detail(
    *,
    start=None,
    end=None,
    account: Account | None = None,
    fund: Fund | None = None,
    association=None,
) -> list[GLAccount]:
    """Lines per account between ``start`` and ``end`` with opening and running balances."""
    association = association or get_current_association()
    accounts = Account.all_associations.filter(association=association)
    if account is not None:
        accounts = accounts.filter(pk=account.pk)
    out = []
    for acct in accounts.order_by("code"):
        base = _lines(association, fund=fund).filter(account=acct)
        opening = _net(base.filter(entry__date__lt=start)) if start else ZERO
        period = base
        if start:
            period = period.filter(entry__date__gte=start)
        if end:
            period = period.filter(entry__date__lte=end)
        period = period.select_related("entry", "fund").order_by("entry__date", "entry_id", "pk")
        running = opening
        rows = []
        for ln in period:
            running += ln.debit - ln.credit
            rows.append(GLRow(line=ln, balance=running))
        if rows or opening:
            out.append(GLAccount(account=acct, opening=opening, rows=rows))
    return out


__all__ = [
    "EntryAlreadyReversed",
    "FundUnbalanced",
    "InvalidLine",
    "LedgerError",
    "Line",
    "PeriodLocked",
    "UnbalancedEntry",
    "account_balance",
    "ownership_balance",
    "get_locked_through",
    "gl_detail",
    "post_entry",
    "reverse_entry",
    "set_locked_through",
    "trial_balance",
    "unit_balance",
]
