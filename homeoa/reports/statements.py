"""Financial statements (FIN-14): income statement, balance sheet, cash flow (indirect).

Built on the ``queries`` layer: accrual reads the ledger, cash reads the virtual cash
ledger (``queries.cash_lines``, strict cash basis). Read-only.

Columns are one per fund plus the all-funds total (or one fund, or the total only), and a
comparison column for the same range a year earlier (balance sheet: the prior fiscal year
end). Interfund balances (1900/1920 against 2900/2920) are shown per fund as one net
"interfund" line; they net to zero in the total.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO
from homeoa.ledger.chart import CLOSE_SOURCE  # year-end closing entries
from homeoa.ledger.models import Account, AccountType, Fund, JournalLine

from .queries import CASH, cash_accounts, cash_lines

TOTAL = "total"
COMPARE = "compare"
ALL_TOTAL = "all"  # fund filter value: all funds, total column only
INTERFUND_CODES = ("1900", "1920", "2900", "2920")

Activity = dict[tuple[str, str], Decimal]  # (account code, fund code) -> net debit


# ---------------------------------------------------------------- shapes


@dataclass
class Column:
    key: str
    label: str


@dataclass
class Row:
    label: str
    values: dict[str, Decimal]
    code: str = ""
    strong: bool = False

    def cells(self, columns) -> list[Decimal]:
        return [self.values.get(c.key, ZERO) for c in columns]


@dataclass
class Section:
    title: str
    rows: list[Row] = field(default_factory=list)
    total: Row | None = None


@dataclass
class Statement:
    columns: list[Column]
    sections: list[Section]
    summary: list[Row] = field(default_factory=list)
    per_fund: bool = False  # per-fund figures shown (drives the cash-basis note)

    def value(self, label_or_code: str, column: str = TOTAL) -> Decimal:
        for row in self._all_rows():
            if label_or_code in (row.code, str(row.label)):
                return row.values.get(column, ZERO)
        return ZERO

    def _all_rows(self):
        for s in self.sections:
            yield from s.rows
            if s.total:
                yield s.total
        yield from self.summary


# ---------------------------------------------------------------- activity


def activity(
    association, *, start=None, end=None, basis: str, without_close: bool = False
) -> Activity:
    """Net debit per (account code, fund code) for dates in [start, end], either basis.

    ``without_close`` leaves out year-end closing entries, so a closed year's income
    statement and cash flow still show its income and expense."""
    out: Activity = defaultdict(lambda: ZERO)
    if basis == CASH:
        for v in cash_lines(association, as_of=end, start=start):
            if without_close and v.entry.source_type == CLOSE_SOURCE:
                continue
            out[(v.account.code, v.fund.code)] += v.debit - v.credit
        return out
    qs = JournalLine.all_associations.filter(association=association)
    if without_close:
        qs = qs.exclude(entry__source_type=CLOSE_SOURCE)
    if start is not None:
        qs = qs.filter(entry__date__gte=start)
    if end is not None:
        qs = qs.filter(entry__date__lte=end)
    for r in qs.values("account__code", "fund__code").annotate(d=Sum("debit"), c=Sum("credit")):
        out[(r["account__code"], r["fund__code"])] += (r["d"] or ZERO) - (r["c"] or ZERO)
    return out


def _by_fund(act: Activity, codes, sign: int = 1) -> dict[str, Decimal]:
    """Sum ``act`` over ``codes`` per fund code and in ``TOTAL``; ``sign`` flips debit-positive."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    codes = set(codes)
    for (code, fund), net in act.items():
        if code in codes:
            out[fund] += sign * net
            out[TOTAL] += sign * net
    return out


def _columns(association, fund_filter: str) -> tuple[list[Column], list[str], bool]:
    """Columns for the fund filter; returns (columns, fund codes shown, per-fund?)."""
    funds = list(Fund.all_associations.filter(association=association))
    if fund_filter == ALL_TOTAL:
        return [Column(TOTAL, _("All funds"))], [], False
    picked = [f for f in funds if f.code == fund_filter]
    if picked:
        return [Column(picked[0].code, picked[0].name)], [picked[0].code], True
    cols = [Column(f.code, f.name) for f in funds] + [Column(TOTAL, _("Total"))]
    return cols, [f.code for f in funds], True


def _compare_key(columns) -> str:
    """The column the comparison mirrors: the total, or the single fund shown."""
    return TOTAL if any(c.key == TOTAL for c in columns) else columns[0].key


def _accounts(association):
    return list(Account.all_associations.filter(association=association).order_by("code"))


def fiscal_year_start(association, day: dt.date) -> dt.date:
    month = association.fiscal_year_start_month or 1
    year = day.year if day.month >= month else day.year - 1
    return dt.date(year, month, 1)


def _year_earlier(day: dt.date) -> dt.date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # Feb 29
        return day.replace(year=day.year - 1, day=28)


# ---------------------------------------------------------------- income statement


def _pl_sections(accounts, act, compare_act, compare_key):
    sections = []
    net = Row(_("Net income (loss)"), defaultdict(lambda: ZERO), code="net_income", strong=True)
    for type_, title, total_label, sign in (
        (AccountType.INCOME, _("Income"), _("Total income"), -1),
        (AccountType.EXPENSE, _("Expenses"), _("Total expenses"), 1),
    ):
        sec = Section(title)
        total = Row(total_label, defaultdict(lambda: ZERO), strong=True)
        for a in accounts:
            if a.type != type_:
                continue
            values = _by_fund(act, [a.code], sign)
            if compare_act is not None:
                values[COMPARE] = _by_fund(compare_act, [a.code], sign)[compare_key]
            if not any(values.values()):
                continue
            sec.rows.append(Row(f"{a.code} {a.name}", values, code=a.code))
            for k, v in values.items():
                total.values[k] += v
                net.values[k] += -sign * v
        sec.total = total
        sections.append(sec)
    return sections, net


def income_statement(
    association, *, start: dt.date, end: dt.date, basis: str, fund_filter: str = "",
    compare: bool = True,
) -> Statement:  # fmt: skip
    """Income and expense per account for [start, end] by fund, total and a year earlier."""
    columns, _codes, per_fund = _columns(association, fund_filter)
    key = _compare_key(columns)
    act = activity(association, start=start, end=end, basis=basis, without_close=True)
    cmp_act = None
    if compare:
        cmp_act = activity(
            association,
            start=_year_earlier(start),
            end=_year_earlier(end),
            basis=basis,
            without_close=True,
        )
        columns = [*columns, Column(COMPARE, _("Prior year"))]
    sections, net = _pl_sections(_accounts(association), act, cmp_act, key)
    return Statement(columns=columns, sections=sections, summary=[net], per_fund=per_fund)


# ---------------------------------------------------------------- balance sheet


def _bs_values(accounts, act):
    """Balance-sheet values for one cumulative activity: dict of row-id -> by-fund values."""
    out = {}
    for a in accounts:
        if a.code in INTERFUND_CODES:
            continue
        if a.type == AccountType.ASSET:
            out[a.code] = _by_fund(act, [a.code], 1)
        elif a.type in (AccountType.LIABILITY, AccountType.EQUITY):
            out[a.code] = _by_fund(act, [a.code], -1)
    out["interfund"] = _by_fund(act, INTERFUND_CODES, 1)
    pl = [a.code for a in accounts if a.type in (AccountType.INCOME, AccountType.EXPENSE)]
    out["net_income"] = _by_fund(act, pl, -1)
    return out


def balance_sheet(
    association, *, as_of: dt.date, basis: str, fund_filter: str = "", compare: bool = True
) -> Statement:
    """Assets = liabilities + fund balance, per fund and total, as of a date."""
    columns, _codes, per_fund = _columns(association, fund_filter)
    key = _compare_key(columns)
    accounts = _accounts(association)
    cur = _bs_values(accounts, activity(association, end=as_of, basis=basis))
    prev = None
    if compare:
        prior_end = fiscal_year_start(association, as_of) - dt.timedelta(days=1)
        prev = _bs_values(accounts, activity(association, end=prior_end, basis=basis))
        columns = [*columns, Column(COMPARE, _("Prior year end"))]

    def row(label, rid, code=""):
        values = dict(cur[rid])
        if prev is not None:
            values[COMPARE] = prev[rid][key]
        return Row(label, values, code=code)

    def section(title, total_label, items):
        sec = Section(title)
        total = Row(total_label, defaultdict(lambda: ZERO), strong=True)
        for r in items:
            if not any(r.values.values()):
                continue
            sec.rows.append(r)
            for k, v in r.values.items():
                total.values[k] += v
        sec.total = total
        return sec

    by_type = defaultdict(list)
    for a in accounts:
        if a.code in cur:
            by_type[a.type].append(row(f"{a.code} {a.name}", a.code, a.code))
    assets = section(
        _("Assets"),
        _("Total assets"),
        [
            *by_type[AccountType.ASSET],
            row(_("Interfund balance (due from / to other funds)"), "interfund", "interfund"),
        ],
    )
    liabilities = section(_("Liabilities"), _("Total liabilities"), by_type[AccountType.LIABILITY])
    equity = section(
        _("Fund balance"),
        _("Total fund balance"),
        [
            *by_type[AccountType.EQUITY],
            row(_("Net income not yet closed"), "net_income", "net_income"),
        ],
    )
    both = Row(
        _("Total liabilities and fund balance"),
        defaultdict(lambda: ZERO),
        code="liab_fb",
        strong=True,
    )
    for k in set(liabilities.total.values) | set(equity.total.values):
        both.values[k] = liabilities.total.values[k] + equity.total.values[k]
    return Statement(
        columns=columns,
        sections=[assets, liabilities, equity],
        summary=[both],
        per_fund=per_fund,
    )


# ---------------------------------------------------------------- cash flow


def cash_flow(
    association, *, start: dt.date, end: dt.date, basis: str, fund_filter: str = ""
) -> Statement:
    """Indirect method: net income, adjusted for balance-sheet changes, is the cash change."""
    columns, _codes, per_fund = _columns(association, fund_filter)
    accounts = _accounts(association)
    act = activity(association, start=start, end=end, basis=basis, without_close=True)
    before = activity(
        association, end=start - dt.timedelta(days=1), basis=basis, without_close=True
    )
    cash = {a.code for a in cash_accounts(association)}
    pl = [a.code for a in accounts if a.type in (AccountType.INCOME, AccountType.EXPENSE)]

    def r(label, values, code="", strong=False):
        return Row(label, defaultdict(lambda: ZERO, values), code=code, strong=strong)

    net_income = r(_("Net income (loss)"), _by_fund(act, pl, -1), "net_income")
    operating = Section(_("Operating activities"), [net_income])
    for a in accounts:
        if a.code in cash or a.code in INTERFUND_CODES:
            continue
        if a.type in (AccountType.ASSET, AccountType.LIABILITY):
            values = _by_fund(act, [a.code], -1)
            if any(values.values()):
                label = _("Change in %(account)s") % {"account": a.name}
                operating.rows.append(r(label, values, a.code))
    interfund = r(_("Interfund transfers (net)"), _by_fund(act, INTERFUND_CODES, -1), "interfund")
    if any(interfund.values.values()):
        operating.rows.append(interfund)
    equity_codes = [a.code for a in accounts if a.type == AccountType.EQUITY]
    fund_bal = r(_("Fund balance entries"), _by_fund(act, equity_codes, -1), "equity")
    financing = Section(
        _("Fund balance and other"), [fund_bal] if any(fund_bal.values.values()) else []
    )

    sections = [operating, financing]
    for sec in sections:
        sec.total = r(_("Net cash from %(s)s") % {"s": str(sec.title).lower()}, {}, strong=True)
        for row in sec.rows:
            for k, v in row.values.items():
                sec.total.values[k] += v
    change = r(_("Net change in cash"), {}, "change", strong=True)
    for sec in sections:
        for k, v in sec.total.values.items():
            change.values[k] += v
    begin = r(_("Cash at beginning of period"), _by_fund(before, cash, 1), "begin")
    end_row = r(_("Cash at end of period"), {}, "end", strong=True)
    for k in set(begin.values) | set(change.values):
        end_row.values[k] = begin.values[k] + change.values[k]
    bank = r(_("Bank and cash movement"), _by_fund(act, cash, 1), "bank")
    return Statement(
        columns=columns,
        sections=sections,
        summary=[change, begin, end_row, bank],
        per_fund=per_fund,
    )
