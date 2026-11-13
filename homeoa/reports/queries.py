"""Read-only report queries (FIN-14): trial balance, GL detail, AR and AP aging.

Reads ``ledger``, ``ap``, ``billing`` and ``payments``; never writes.

Basis
-----------------------------
The ledger is accrual. The cash view is a *virtual* ledger built only from entries where
a bank or cash account moved, plus opening-balance (and period-close) entries, which carry
over as on accrual. Inside a cash entry, lines on accrual-only accounts are restated:

* Dr Accounts payable (a bill payment) becomes the bill's expense lines, dated the payment.
* Receivable credits allocated to charges become those charges' income lines (scaled to
  each allocation), dated the receipt; a receipt void reverses them the same way.
* Prepaid assessments (an owner prepayment, or its refund) becomes assessment income on
  the day the cash moved.

Everything else is left out: charges, bills, credit memos, write-offs, credit applied to
charges, releases, interfund accruals. Unpaid AR and AP therefore never appear.

Limitation: receivable credits with no ``Charge`` behind them (imported history) stay on
the receivable account. Aging (accrual) counts days past the charge or bill due date.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from homeoa.ap.models import Bill
from homeoa.billing.models import Charge
from homeoa.core.context import association_today
from homeoa.core.fields import ZERO
from homeoa.ledger import services as ledger
from homeoa.ledger.chart import interfund_codes
from homeoa.ledger.models import Account, AccountType, Fund, JournalEntry, JournalLine
from homeoa.payments.models import Allocation

ACCRUAL = "accrual"
CASH = "cash"
BASES = (ACCRUAL, CASH)
CENT = Decimal("0.01")

AP_CODE = "2000"
PREPAID_CODE = "2100"
PREPAID_INCOME_CODE = "4000"  # Prepayments are assessment income when received
CARRY_OVER = ("imports.opening", "seed_demo", "ledger.close")

BUCKETS = (
    ("current", _("Current"), None, 0),
    ("d1", _("1-30 days"), 1, 30),
    ("d31", _("31-60 days"), 31, 60),
    ("d61", _("61-90 days"), 61, 90),
    ("d90", _("Over 90 days"), 91, None),
)

Key = tuple[int, int]  # (account_id, fund_id)


def _bucket(days: int) -> str:
    for key, _label, low, high in BUCKETS:
        if (low is None or days >= low) and (high is None or days <= high):
            return key
    return BUCKETS[0][0]


def _empty_buckets() -> dict[str, Decimal]:
    return {key: ZERO for key, *_rest in BUCKETS}


# ---------------------------------------------------------------- virtual cash ledger


@dataclass
class VLine:
    """A cash-basis line. ``entry`` is the real source entry; amounts may be restated."""

    entry: JournalEntry
    account: Account
    fund: Fund
    debit: Decimal
    credit: Decimal
    memo: str = ""


def cash_accounts(association):
    """Bank and cash accounts: assets outside the unit subledger, except interfund."""
    interfund = set(interfund_codes("operating", "reserve"))
    interfund |= set(interfund_codes("operating", "insurance"))
    return Account.all_associations.filter(
        association=association, type=AccountType.ASSET, in_unit_subledger=False
    ).exclude(code__in=interfund)


def _scaled(lines, amount: Decimal) -> list[tuple[JournalLine, Decimal]]:
    """Split ``amount`` across ``lines`` by their net size; the remainder goes on the last."""
    total = sum((abs(ln.debit - ln.credit) for ln in lines), ZERO)
    out = []
    left = amount
    for i, ln in enumerate(lines):
        if i == len(lines) - 1:
            share = left
        else:
            share = (amount * abs(ln.debit - ln.credit) / total).quantize(CENT, ROUND_HALF_UP)
        left -= share
        out.append((ln, share))
    return out


def _lines_by_entry(entry_ids, keep) -> dict[int, list[JournalLine]]:
    out: dict[int, list[JournalLine]] = defaultdict(list)
    qs = (
        JournalLine.all_associations.filter(entry_id__in=entry_ids)
        .select_related("account", "fund")
        .order_by("pk")
    )
    for ln in qs:
        if keep(ln):
            out[ln.entry_id].append(ln)
    return out


def cash_lines(association, *, as_of=None, start=None, fund: Fund | None = None) -> list[VLine]:
    """The cash-basis virtual ledger (see module doc), oldest first."""
    entries = JournalEntry.all_associations.filter(association=association).filter(
        Q(lines__account__in=cash_accounts(association)) | Q(source_type__in=CARRY_OVER)
    )
    if as_of is not None:
        entries = entries.filter(date__lte=as_of)
    if start is not None:
        entries = entries.filter(date__gte=start)
    entry_ids = set(entries.values_list("pk", flat=True))

    bills = {
        b.payment_entry_id: b for b in Bill.all_associations.filter(payment_entry_id__in=entry_ids)
    }
    bill_lines = _lines_by_entry(
        [b.entry_id for b in bills.values()], lambda ln: ln.account.code != AP_CODE
    )
    allocs = list(
        Allocation.all_associations.filter(
            Q(entry_id__in=entry_ids) | Q(released_entry_id__in=entry_ids)
        )
        .select_related("charge")
        .order_by("pk")
    )
    charge_lines = _lines_by_entry(
        {a.charge.entry_id for a in allocs}, lambda ln: not ln.account.in_unit_subledger
    )
    restated: dict[int, list[tuple[Allocation, int]]] = defaultdict(list)
    for a in allocs:
        if a.entry_id in entry_ids:
            restated[a.entry_id].append((a, 1))
        if a.released_entry_id in entry_ids:
            restated[a.released_entry_id].append((a, -1))
    prepaid_income = Account.all_associations.filter(
        association=association, code=PREPAID_INCOME_CODE
    ).first()

    out: list[VLine] = []

    def add(entry, account, fnd, net, memo):
        if net:
            out.append(VLine(entry, account, fnd, max(net, ZERO), max(-net, ZERO), memo))

    lines = (
        JournalLine.all_associations.filter(entry_id__in=entry_ids)
        .select_related("entry", "account", "fund")
        .order_by("entry__date", "entry_id", "pk")
    )
    grouped: dict[int, list[JournalLine]] = defaultdict(list)
    for ln in lines:
        grouped[ln.entry_id].append(ln)
    for eid, elines in grouped.items():
        entry = elines[0].entry
        if entry.source_type in CARRY_OVER:
            for ln in elines:
                add(entry, ln.account, ln.fund, ln.debit - ln.credit, ln.memo)
            continue
        # Receivable lines: what allocations explain becomes the charges' income.
        recv: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
        recv_lines: dict[tuple, JournalLine] = {}
        for ln in elines:
            if ln.account.in_unit_subledger and ln.account.type == AccountType.ASSET:
                key = (ln.account_id, ln.fund_id)
                recv[key] += ln.debit - ln.credit
                recv_lines.setdefault(key, ln)
        for alloc, sign in restated[eid]:
            c = alloc.charge
            src = charge_lines[c.entry_id]
            if not src:
                continue
            for s, share in _scaled(src, alloc.amount):
                direction = 1 if s.credit > s.debit else -1
                add(entry, s.account, s.fund, -sign * direction * share, c.description)
            key = next(iter(recv), None)
            if key is not None:
                recv[key] += sign * alloc.amount
        for key, rest in recv.items():
            ln = recv_lines[key]
            add(entry, ln.account, ln.fund, rest, ln.memo)
        bill = bills.get(eid)
        for ln in elines:
            net = ln.debit - ln.credit
            if ln.account.in_unit_subledger and ln.account.type == AccountType.ASSET:
                continue
            if ln.account.code == AP_CODE and bill is not None:
                src = [s for s in bill_lines[bill.entry_id] if s.fund_id == ln.fund_id]
                for s, share in _scaled(src, net):
                    sign = 1 if s.debit > s.credit else -1
                    add(entry, s.account, s.fund, sign * share, s.memo or ln.memo)
            elif ln.account.code == PREPAID_CODE and prepaid_income is not None:
                add(entry, prepaid_income, ln.fund, net, ln.memo)
            else:
                add(entry, ln.account, ln.fund, net, ln.memo)
    if fund is not None:
        out = [v for v in out if v.fund.pk == fund.pk]
    return out


def _totals(vlines) -> dict[Key, Decimal]:
    nets: dict[Key, Decimal] = defaultdict(lambda: ZERO)
    for v in vlines:
        nets[(v.account.pk, v.fund.pk)] += v.debit - v.credit
    return nets


def period_activity(
    association, *, start, end, basis: str = CASH, fund: Fund | None = None
) -> dict[str, Decimal]:
    """Net activity (debit positive) per account code from ``start`` to ``end``."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    if basis == CASH:
        for v in cash_lines(association, as_of=end, start=start, fund=fund):
            out[v.account.code] += v.debit - v.credit
    else:
        qs = JournalLine.all_associations.filter(
            association=association, entry__date__gte=start, entry__date__lte=end
        ).select_related("account")
        if fund is not None:
            qs = qs.filter(fund=fund)
        for ln in qs:
            out[ln.account.code] += ln.debit - ln.credit
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- trial balance


def trial_balance(
    *, as_of=None, fund: Fund | None = None, basis: str = ACCRUAL, association
) -> ledger.TrialBalance:
    """The ledger trial balance, or its cash-basis view. Always totals to zero."""
    if basis != CASH:
        return ledger.trial_balance(as_of=as_of, fund=fund, association=association)
    accounts = {a.pk: a for a in Account.all_associations.filter(association=association)}
    funds = {f.pk: f for f in Fund.all_associations.filter(association=association)}
    out = ledger.TrialBalance(as_of=as_of)
    for (acct, fnd), net in _totals(cash_lines(association, as_of=as_of, fund=fund)).items():
        if net:
            out.rows.append(
                ledger.TrialBalanceRow(
                    account=accounts[acct],
                    fund=funds[fnd],
                    debit=net if net > 0 else ZERO,
                    credit=-net if net < 0 else ZERO,
                )
            )
    out.rows.sort(key=lambda r: (r.account.code, r.fund.sort_order, r.fund.code))
    return out


# ---------------------------------------------------------------- GL detail


def gl_detail(
    *,
    start=None,
    end=None,
    account: Account | None = None,
    fund: Fund | None = None,
    basis: str = ACCRUAL,
    association,
) -> list[ledger.GLAccount]:
    """GL lines per account with opening and running balances, on either basis."""
    if basis != CASH:
        groups = ledger.gl_detail(
            start=start, end=end, account=account, fund=fund, association=association
        )
        return [g for g in groups if g.rows or g.opening]
    vlines = cash_lines(association, as_of=end, fund=fund)
    if account is not None:
        vlines = [v for v in vlines if v.account.pk == account.pk]
    groups: dict[int, ledger.GLAccount] = {}
    for v in vlines:
        g = groups.get(v.account.pk)
        if g is None:
            g = groups[v.account.pk] = ledger.GLAccount(account=v.account, opening=ZERO, rows=[])
        if start and v.entry.date < start:
            g.opening += v.debit - v.credit
            continue
        prev = g.rows[-1].balance if g.rows else g.opening
        g.rows.append(ledger.GLRow(line=v, balance=prev + v.debit - v.credit))
    out = sorted(groups.values(), key=lambda g: g.account.code)
    return [g for g in out if g.rows or g.opening]


# ---------------------------------------------------------------- aging


@dataclass
class AgingRow:
    label: str
    key: object
    buckets: dict[str, Decimal] = field(default_factory=_empty_buckets)
    credit: Decimal = ZERO  # AR only: unapplied prepaid credit

    @property
    def total(self) -> Decimal:
        return sum(self.buckets.values(), ZERO)

    @property
    def cells(self) -> list[Decimal]:
        return [self.buckets[key] for key, *_rest in BUCKETS]


@dataclass
class Aging:
    as_of: dt.date
    rows: list[AgingRow]

    @property
    def totals(self) -> dict[str, Decimal]:
        out = _empty_buckets()
        for r in self.rows:
            for k, v in r.buckets.items():
                out[k] += v
        return out

    @property
    def total_cells(self) -> list[Decimal]:
        totals = self.totals
        return [totals[key] for key, *_rest in BUCKETS]

    @property
    def total(self) -> Decimal:
        return sum((r.total for r in self.rows), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), ZERO)


def _charge_open(charge: Charge, as_of: dt.date, live: bool) -> Decimal:
    if live:
        return charge.open_balance
    paid = ZERO
    for a in charge.allocations_for_aging:
        if a.entry.date > as_of:
            continue
        if a.released_entry_id and a.released_entry.date <= as_of:
            continue
        paid += a.amount
    return charge.amount - paid


def ar_aging(*, as_of: dt.date, fund: Fund | None = None, association) -> Aging:
    """Open charges by unit, bucketed by days past the charge's due date.

    For a past ``as_of`` the open amounts are rebuilt from allocation history. Receivable
    debits with no charge behind them (imported history) age from their posting date."""
    live = as_of >= association_today(association)
    charges = (
        Charge.all_associations.filter(association=association, charge_date__lte=as_of)
        .exclude(void_entry__date__lte=as_of)
        .select_related("unit")
    )
    if fund is not None:
        charges = charges.filter(
            entry__lines__fund=fund, entry__lines__account__in_unit_subledger=True
        ).distinct()
    allocs: dict[int, list[Allocation]] = defaultdict(list)
    if not live:
        for a in Allocation.all_associations.filter(charge__in=charges).select_related(
            "entry", "released_entry"
        ):
            allocs[a.charge_id].append(a)
    rows: dict[int, AgingRow] = {}
    opened: dict[int, Decimal] = defaultdict(lambda: ZERO)
    charge_entries = set()
    for c in charges:
        charge_entries.add(c.entry_id)
        c.allocations_for_aging = allocs[c.pk]
        amount = _charge_open(c, as_of, live)
        opened[c.unit_id] += amount
        if amount <= 0:
            continue
        row = rows.setdefault(c.unit_id, AgingRow(label=str(c.unit), key=c.unit))
        row.buckets[_bucket((as_of - c.due_date).days)] += amount

    # Receivables with no charge behind them: the unit's residual, by newest posting date.
    recv = JournalLine.all_associations.filter(
        association=association,
        account__in_unit_subledger=True,
        account__type=AccountType.ASSET,
        unit__isnull=False,
        entry__date__lte=as_of,
    ).select_related("entry", "unit")
    if fund is not None:
        recv = recv.filter(fund=fund)
    balance: dict[int, Decimal] = defaultdict(lambda: ZERO)
    newest: dict[int, dt.date] = {}
    units = {}
    for ln in recv:
        balance[ln.unit_id] += ln.debit - ln.credit
        units[ln.unit_id] = ln.unit
        if ln.debit and ln.entry_id not in charge_entries:
            newest[ln.unit_id] = max(newest.get(ln.unit_id, ln.entry.date), ln.entry.date)
    for unit_id, bal in balance.items():
        residual = bal - opened[unit_id]
        if residual > 0 and unit_id in newest:
            unit = units[unit_id]
            row = rows.setdefault(unit_id, AgingRow(label=str(unit), key=unit))
            row.buckets[_bucket((as_of - newest[unit_id]).days)] += residual

    prepaid = JournalLine.all_associations.filter(
        association=association,
        account__in_unit_subledger=True,
        account__type=AccountType.LIABILITY,
        unit__isnull=False,
        entry__date__lte=as_of,
    ).select_related("unit")
    if fund is not None:
        prepaid = prepaid.filter(fund=fund)
    for ln in prepaid:
        row = rows.setdefault(ln.unit_id, AgingRow(label=str(ln.unit), key=ln.unit))
        row.credit += ln.credit - ln.debit
    out = [r for r in rows.values() if r.total or r.credit]
    out.sort(key=lambda r: r.key.sort_key)
    return Aging(as_of=as_of, rows=out)


def _unpaid_bills(association, as_of: dt.date):
    """Bills with a posted accrual on/before ``as_of`` that are not paid or void by then."""
    return (
        Bill.all_associations.filter(association=association, entry__date__lte=as_of)
        .filter(Q(paid_on__isnull=True) | Q(paid_on__gt=as_of))
        .exclude(void_entry__date__lte=as_of)
        .select_related("vendor", "entry")
        .prefetch_related("lines")
    )


def ap_aging(*, as_of: dt.date, fund: Fund | None = None, association) -> Aging:
    """Unpaid bills by vendor, bucketed by days past due (bill date when no due date)."""
    rows: dict[int, AgingRow] = {}
    for bill in _unpaid_bills(association, as_of):
        amount = sum(
            (ln.amount for ln in bill.lines.all() if fund is None or ln.fund_id == fund.pk),
            ZERO,
        )
        if not amount:
            continue
        due = bill.due_date or bill.bill_date
        row = rows.setdefault(bill.vendor_id, AgingRow(label=str(bill.vendor), key=bill.vendor))
        row.buckets[_bucket((as_of - due).days)] += amount
    out = sorted(rows.values(), key=lambda r: r.label.lower())
    return Aging(as_of=as_of, rows=out)
