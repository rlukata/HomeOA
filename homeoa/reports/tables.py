"""Table specs for the Money > Reports pages (RPT-01 exports of FIN-14 reports).

Each builder takes the same query result the page renders, so exports and screens agree.
Statements add their builders here.
"""

from __future__ import annotations

from django.utils.translation import gettext as _

from .export import DATE, MONEY, TEXT, Column, TableSpec
from .queries import BUCKETS, CASH


def _basis_note(basis: str) -> str:
    return _("Cash basis") if basis == CASH else _("Accrual basis")


def _scope(fund, basis=None) -> str:
    parts = [fund.name if fund else _("All funds")]
    if basis:
        parts.append(_basis_note(basis))
    return " · ".join(parts)


def trial_balance(tb, *, fund=None, basis=None) -> TableSpec:
    as_of = tb.as_of.isoformat() if tb.as_of else _("all posted entries")
    return TableSpec(
        title=_("Trial balance"),
        filename="trial-balance",
        subtitle=_("As of %(d)s") % {"d": as_of} + " · " + _scope(fund, basis),
        columns=[
            Column(_("Account")),
            Column(_("Name")),
            Column(_("Fund")),
            Column(_("Debit"), MONEY),
            Column(_("Credit"), MONEY),
        ],
        rows=[
            [r.account.code, r.account.name, r.fund.name, r.debit or None, r.credit or None]
            for r in tb.rows
        ],
        totals=[_("Totals"), "", "", tb.total_debit, tb.total_credit],
    )


def gl_detail(groups, *, fund=None, basis=None, start=None, end=None) -> TableSpec:
    rows = []
    for g in groups:
        if start:
            rows.append(
                [None, g.account.code, g.account.name, _("Opening"), "", None, None, g.opening]
            )
        for r in g.rows:
            ln = r.line
            rows.append(
                [
                    ln.entry.date,
                    g.account.code,
                    g.account.name,
                    ln.memo or ln.entry.memo,
                    ln.fund.name,
                    ln.debit or None,
                    ln.credit or None,
                    r.balance,
                ]
            )
    period = " – ".join(d.isoformat() for d in (start, end) if d)
    return TableSpec(
        title=_("General ledger detail"),
        filename="general-ledger",
        subtitle=" · ".join(x for x in (period, _scope(fund, basis)) if x),
        columns=[
            Column(_("Date"), DATE),
            Column(_("Account")),
            Column(_("Account name")),
            Column(_("Memo")),
            Column(_("Fund")),
            Column(_("Debit"), MONEY),
            Column(_("Credit"), MONEY),
            Column(_("Balance"), MONEY),
        ],
        rows=rows,
        pii=True,  # memos name owners and vendors
    )


def aging(aging, *, title, who, filename, fund=None, show_credit=False, pii=False) -> TableSpec:
    columns = [Column(who, TEXT)]
    columns += [Column(label, MONEY) for _key, label, *_rest in BUCKETS]
    columns.append(Column(_("Total"), MONEY))
    if show_credit:
        columns.append(Column(_("Prepaid credit"), MONEY))
    rows = []
    for r in aging.rows:
        row = [r.label, *[v or None for v in r.cells], r.total]
        if show_credit:
            row.append(r.credit or None)
        rows.append(row)
    totals = [_("Totals"), *aging.total_cells, aging.total]
    if show_credit:
        totals.append(aging.total_credit)
    return TableSpec(
        title=title,
        filename=filename,
        subtitle=_("As of %(d)s") % {"d": aging.as_of.isoformat()} + " · " + _scope(fund),
        columns=columns,
        rows=rows,
        totals=totals,
        pii=pii,
    )


def statement(st, *, title, filename, period, scope, basis, note="") -> TableSpec:
    """Income statement, balance sheet or cash flow as one flat table.

    Section headings become label-only rows, followed by their lines and total; the
    statement's summary rows (net income, totals, ending cash) come last.
    """
    blank = [None] * len(st.columns)
    rows = []
    for sec in st.sections:
        rows.append([sec.title, *blank])
        rows.extend([r.label, *r.cells(st.columns)] for r in sec.rows)
        if sec.total:
            rows.append([sec.total.label, *sec.total.cells(st.columns)])
    rows.extend([r.label, *r.cells(st.columns)] for r in st.summary)
    if note:  # footer row so it reaches the CSV too
        rows.append([note, *blank])
    return TableSpec(
        title=title,
        filename=filename,
        subtitle=" · ".join(x for x in (period, scope, _basis_note(basis)) if x),
        columns=[Column(_("Line")), *[Column(str(c.label), MONEY) for c in st.columns]],
        rows=rows,
    )


def bank_recs(recs, *, start=None, end=None) -> TableSpec:
    """Completed bank reconciliations (CPA package)."""
    period = " – ".join(d.isoformat() for d in (start, end) if d)
    return TableSpec(
        title=_("Bank reconciliations"),
        filename="bank-reconciliations",
        subtitle=period,
        columns=[
            Column(_("Account")),
            Column(_("Statement date"), DATE),
            Column(_("Beginning balance"), MONEY),
            Column(_("Cleared"), MONEY),
            Column(_("Ending balance"), MONEY),
            Column(_("Difference"), MONEY),
            Column(_("Completed"), DATE),
            Column(_("Completed by")),
        ],
        rows=[
            [
                r.bank_account.name,
                r.statement_date,
                r.beginning_balance,
                r.cleared_total,
                r.ending_balance,
                r.difference,
                r.completed_at.date() if r.completed_at else None,
                (r.completed_by.full_name or r.completed_by.email) if r.completed_by else "",
            ]
            for r in recs
        ],
    )
