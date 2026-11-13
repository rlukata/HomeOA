"""Money hub > Reports (FIN-14): TB, GL detail, AR and AP aging. Read-only, ``finance.read``."""

import datetime as dt

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_any
from homeoa.core.context import association_today
from homeoa.ledger.models import Account, Fund
from homeoa.ui.export import export_format, export_response
from homeoa.ui.nav import get_hub

from . import queries, statements, tables
from .queries import ACCRUAL, BUCKETS, CASH


def _date(value):
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _ctx(request, title, report):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "reports"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "reports"), None)
    fund_code = request.GET.get("fund", "").strip()
    basis = request.GET.get("basis", ACCRUAL)
    return {
        "current_hub": hub,
        "subtabs": tabs,
        "current_tab": current,
        "heading": title,
        "report": report,
        "funds": Fund.objects.all(),
        "fund": Fund.objects.filter(code=fund_code).first() if fund_code else None,
        "basis": CASH if basis == CASH else ACCRUAL,
        "bases": ((ACCRUAL, _("Accrual")), (CASH, _("Cash"))),
    }


@require_perm(perms.FINANCE_READ)
def index(request):
    from .module_exports import EXPORTS

    ctx = _ctx(request, _("Reports"), "index")
    ctx["record_exports"] = [
        e
        for e in EXPORTS.values()
        if has_any(request.user, e.perms, association=request.association)
    ]
    return render(request, "reports/index.html", ctx)


@require_perm(perms.FINANCE_READ)
def trial_balance_view(request):
    ctx = _ctx(request, _("Trial balance"), "tb")
    as_of = _date(request.GET.get("as_of"))
    ctx["as_of"] = as_of
    ctx["tb"] = queries.trial_balance(
        as_of=as_of, fund=ctx["fund"], basis=ctx["basis"], association=request.association
    )
    if fmt := export_format(request):
        spec = tables.trial_balance(ctx["tb"], fund=ctx["fund"], basis=ctx["basis"])
        return export_response(request, spec, fmt)
    return render(request, "reports/trial_balance.html", ctx)


@require_perm(perms.FINANCE_READ)
def gl_view(request):
    ctx = _ctx(request, _("General ledger detail"), "gl")
    start = _date(request.GET.get("start"))
    end = _date(request.GET.get("end"))
    code = request.GET.get("account", "").strip()
    account = Account.objects.filter(code=code).first() if code else None
    ctx.update(
        start=start,
        end=end,
        account=account,
        accounts=Account.objects.all(),
        groups=queries.gl_detail(
            start=start,
            end=end,
            account=account,
            fund=ctx["fund"],
            basis=ctx["basis"],
            association=request.association,
        ),
    )
    if fmt := export_format(request):
        spec = tables.gl_detail(
            ctx["groups"], fund=ctx["fund"], basis=ctx["basis"], start=start, end=end
        )
        return export_response(request, spec, fmt)
    return render(request, "reports/gl.html", ctx)


def _aging(request, title, report, fn, template, export):
    ctx = _ctx(request, title, report)
    as_of = _date(request.GET.get("as_of")) or association_today(request.association)
    ctx.update(
        as_of=as_of,
        buckets=BUCKETS,
        aging_cols=len(BUCKETS) + 2,
        aging=fn(as_of=as_of, fund=ctx["fund"], association=request.association),
    )
    if fmt := export_format(request):
        spec = tables.aging(ctx["aging"], title=title, fund=ctx["fund"], **export)
        return export_response(request, spec, fmt)
    return render(request, template, ctx)


@require_perm(perms.FINANCE_READ)
def ar_aging_view(request):
    export = {"who": _("Unit"), "filename": "receivables-aging", "show_credit": True, "pii": True}
    return _aging(
        request,
        _("Receivables aging"),
        "ar",
        queries.ar_aging,
        "reports/ar_aging.html",
        export,
    )


@require_perm(perms.FINANCE_READ)
def ap_aging_view(request):
    export = {"who": _("Vendor"), "filename": "payables-aging"}
    return _aging(
        request, _("Payables aging"), "ap", queries.ap_aging, "reports/ap_aging.html", export
    )


# ---------------------------------------------------------------- statements


def _statement_ctx(request, title, report):
    ctx = _ctx(request, title, report)
    ctx["fund_filter"] = request.GET.get("fund", "").strip()
    ctx["fund_options"] = [
        ("", _("All funds, by fund")),
        (statements.ALL_TOTAL, _("All funds, total only")),
        *[(f.code, f.name) for f in ctx["funds"]],
    ]
    return ctx


def _with_range(request, ctx):
    association = request.association
    end = _date(request.GET.get("end")) or association_today(association)
    ytd = request.GET.get("range") == "ytd"
    if ytd:
        start = statements.fiscal_year_start(association, end)
    else:
        start = _date(request.GET.get("start")) or end.replace(day=1)
    ctx.update(start=min(start, end), end=end, ytd=ytd, with_range=True)
    return ctx


def _render_statement(request, ctx, statement, filename):
    if fmt := export_format(request):
        if ctx.get("with_range"):
            period = f"{ctx['start'].isoformat()} – {ctx['end'].isoformat()}"
        else:
            period = _("As of %(d)s") % {"d": ctx["as_of"].isoformat()}
        scope = dict(ctx["fund_options"]).get(ctx["fund_filter"], ctx["fund_filter"])
        spec = tables.statement(
            statement, title=str(ctx["heading"]), filename=filename, period=str(period),
            scope=str(scope), basis=ctx["basis"],
            note=_(
                "Per-fund cash figures are net of interfund balances; only the all-funds "
                "total ties to bank movement."
            ) if ctx["basis"] == CASH and statement.per_fund else "",
        )  # fmt: skip
        return export_response(request, spec, fmt)
    for row in statement._all_rows():
        row.cell_values = row.cells(statement.columns)
    ctx["statement"] = statement
    ctx["ncols"] = len(statement.columns) + 1
    ctx["show_cash_note"] = ctx["basis"] == CASH and statement.per_fund
    return render(request, "reports/statement.html", ctx)


@require_perm(perms.FINANCE_READ)
def income_statement_view(request):
    ctx = _with_range(request, _statement_ctx(request, _("Income statement"), "is"))
    st = statements.income_statement(
        request.association, start=ctx["start"], end=ctx["end"], basis=ctx["basis"],
        fund_filter=ctx["fund_filter"],
    )  # fmt: skip
    return _render_statement(request, ctx, st, "income-statement")


@require_perm(perms.FINANCE_READ)
def balance_sheet_view(request):
    ctx = _statement_ctx(request, _("Balance sheet"), "bs")
    ctx["as_of"] = _date(request.GET.get("as_of")) or association_today(request.association)
    st = statements.balance_sheet(
        request.association, as_of=ctx["as_of"], basis=ctx["basis"],
        fund_filter=ctx["fund_filter"],
    )  # fmt: skip
    return _render_statement(request, ctx, st, "balance-sheet")


@require_perm(perms.FINANCE_READ)
def cash_flow_view(request):
    ctx = _with_range(request, _statement_ctx(request, _("Cash flow"), "cf"))
    st = statements.cash_flow(
        request.association, start=ctx["start"], end=ctx["end"], basis=ctx["basis"],
        fund_filter=ctx["fund_filter"],
    )  # fmt: skip
    return _render_statement(request, ctx, st, "cash-flow")


# ---------------------------------------------------------------- CPA package


def _fy_choices(association):
    from django.db.models import Min

    from homeoa.ledger.models import JournalEntry

    today = association_today(association)
    current = statements.fiscal_year_start(association, today).year
    first = JournalEntry.all_associations.filter(association=association).aggregate(m=Min("date"))[
        "m"
    ]
    oldest = statements.fiscal_year_start(association, first).year if first else current
    return list(range(current, min(oldest, current) - 1, -1)), current - 1


@require_perm(perms.FINANCE_MANAGE, perms.CPA_PACKAGE)
def cpa_package_view(request):
    """Year-end zip for the CPA; every download is audited."""
    from django.http import HttpResponse

    from homeoa import audit
    from homeoa.closing.services import fiscal_year_bounds

    from . import cpa

    association = request.association
    years, default = _fy_choices(association)
    try:
        fy = int(request.GET.get("year", default))
    except ValueError:
        fy = default
    if request.GET.get("download"):
        name, body, files = cpa.build(association, fy)
        audit.record(
            "reports.cpa_package_downloaded",
            association=association,
            actor=request.user,
            object_type="reports.cpa_package",
            object_repr=name,
            changes={"year": fy, "files": files},
            message=name,
        )
        response = HttpResponse(body, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["Cache-Control"] = "private, no-store"
        return response
    ctx = _ctx(request, _("Year-end CPA package"), "cpa")
    start, end = fiscal_year_bounds(association, fy)
    ctx.update(fy=fy, years=years if fy in years else [fy, *years], start=start, end=end)
    return render(request, "reports/cpa_package.html", ctx)


@require_perm(perms.FINANCE_MANAGE, perms.CPA_PACKAGE)
def tax_1120h_view(request):
    """Form 1120-H worksheet; the Treasurer classifies accounts, the CPA reads it."""
    from decimal import Decimal, InvalidOperation

    from django.contrib import messages
    from django.shortcuts import redirect

    from homeoa.access.services import has_perm

    from . import tax1120h

    association = request.association
    years, default = _fy_choices(association)
    try:
        fy = int(request.GET.get("year") or request.POST.get("year") or default)
    except ValueError:
        fy = default
    can_edit = has_perm(request.user, perms.FINANCE_MANAGE, association=association)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        try:
            deductions = Decimal(request.POST.get("deductions") or "0").quantize(Decimal("0.01"))
        except InvalidOperation:
            deductions = Decimal("0.00")
        tax1120h.save(
            association,
            fy=fy,
            income={k[4:]: v for k, v in request.POST.items() if k.startswith("inc-")},
            expense={k[4:]: v for k, v in request.POST.items() if k.startswith("exp-")},
            deductions=deductions,
            user=request.user,
        )
        messages.success(request, _("Worksheet saved."))
        return redirect(f"{request.path}?year={fy}")
    ctx = _ctx(request, _("Form 1120-H worksheet"), "tax_1120h")
    ctx.update(
        {
            "years": years,
            "fy": fy,
            "ws": tax1120h.worksheet(association, fy),
            "income_classes": tax1120h.INCOME_CLASSES,
            "expense_classes": tax1120h.EXPENSE_CLASSES,
            "can_edit": can_edit,
        }
    )
    return render(request, "reports/tax_1120h.html", ctx)


@login_required
def module_export_view(request, slug):
    """One feature's records as CSV, whether the feature is on or off."""
    from django.http import Http404

    from . import module_exports
    from .export import CSV

    export = module_exports.EXPORTS.get(slug)
    if export is None:
        raise Http404
    if not has_any(request.user, export.perms, association=request.association):
        raise PermissionDenied
    return export_response(request, export.build(request.association), CSV)
