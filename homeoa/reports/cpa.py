"""Year-end CPA package (FIN-16): one zip of CSVs for a fiscal year.

Every file is built with the same query and ``tables`` builder as its Money > Reports
screen (or the Vendors > 1099 tab), so totals match what the board sees. Files are named
``<stem>-<fy>.csv`` where ``fy`` is the calendar year the fiscal year starts in (as on
Money > Close). The income statement leaves out period-close entries.
"""

from __future__ import annotations

import io
import zipfile

from django.utils.translation import gettext as _

from . import export, queries, statements, tables
from .queries import ACCRUAL, CASH


def _statement_spec(st, *, title, filename, period):
    return tables.statement(
        st, title=title, filename=filename, period=period, scope=_("All funds, by fund"),
        basis=ACCRUAL,
    )  # fmt: skip


def specs(association, fy: int) -> list[export.TableSpec]:
    """The package's tables, in file order."""
    from homeoa.banking.models import Reconciliation
    from homeoa.closing.services import fiscal_year_bounds
    from homeoa.vendors import ten99

    start, end = fiscal_year_bounds(association, fy)
    out = []
    for basis in (ACCRUAL, CASH):
        tb = queries.trial_balance(as_of=end, basis=basis, association=association)
        spec = tables.trial_balance(tb, basis=basis)
        spec.filename = f"trial-balance-{basis}"
        out.append(spec)
    groups = queries.gl_detail(start=start, end=end, association=association)
    out.append(tables.gl_detail(groups, basis=ACCRUAL, start=start, end=end))
    recs = (
        Reconciliation.all_associations.filter(
            association=association,
            completed_at__isnull=False,
            statement_date__gte=start,
            statement_date__lte=end,
        )
        .select_related("bank_account", "completed_by")
        .order_by("bank_account__name", "statement_date", "pk")
    )
    out.append(tables.bank_recs(recs, start=start, end=end))
    ar = queries.ar_aging(as_of=end, association=association)
    out.append(
        tables.aging(
            ar,
            title=_("Receivables aging"),
            who=_("Unit"),
            filename="receivables-aging",
            show_credit=True,
            pii=True,
        )  # fmt: skip
    )
    ap = queries.ap_aging(as_of=end, association=association)
    out.append(
        tables.aging(ap, title=_("Payables aging"), who=_("Vendor"), filename="payables-aging")
    )
    period = f"{start.isoformat()} – {end.isoformat()}"
    is_ = statements.income_statement(association, start=start, end=end, basis=ACCRUAL)
    out.append(
        _statement_spec(
            is_, title=_("Income statement"), filename="income-statement", period=period
        )
    )
    bs = statements.balance_sheet(association, as_of=end, basis=ACCRUAL)
    out.append(
        _statement_spec(
            bs,
            title=_("Balance sheet"),
            filename="balance-sheet",
            period=_("As of %(d)s") % {"d": end.isoformat()},
        )  # fmt: skip
    )
    # 1099s are by calendar year: the one the fiscal year ends in.
    out.append(ten99.table(ten99.totals(association, end.year)))
    from . import tax1120h

    out.append(tax1120h.table(tax1120h.worksheet(association, fy)))
    return out


def file_name(spec: export.TableSpec, fy: int) -> str:
    return f"{export.safe_name(spec.filename or spec.title)}-{fy}.csv"


def build(association, fy: int) -> tuple[str, bytes, list[str]]:
    """``(zip file name, zip bytes, member names)``."""
    buf = io.BytesIO()
    names = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for spec in specs(association, fy):
            name = file_name(spec, fy)
            names.append(name)
            zf.writestr(name, export.to_csv(spec).encode("utf-8"))
    prefix = export.safe_name(association.short_name or association.name)
    zip_name = "-".join(p for p in (prefix, "cpa-package", str(fy)) if p) + ".zip"
    return zip_name, buf.getvalue(), names
