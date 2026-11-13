"""Form 1120-H worksheet (FIN-19): the homeowners association election for a fiscal year.

From the ledger: income split into exempt-function income (owners' dues and assessments),
other (taxable) income, and interfund transfers that are not income at all; expenses split
into spending on association property and other. Then the two qualification tests, taxable
income after the directly connected deductions and the specific deduction, and the tax.

The thresholds, deduction and rate come from the rule pack (``tax.1120h.*``, federal,
unverified). The Treasurer classifies accounts; defaults follow the HOA chart of accounts
and are a starting point for the CPA, not tax advice. HomeOA never files anything.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, rules
from homeoa.ledger.models import Account, AccountType

from . import export
from .queries import ACCRUAL
from .statements import activity

ZERO = Decimal("0.00")
SETTINGS_KEY = "tax_1120h"

EXEMPT, TAXABLE, EXCLUDED = "exempt", "taxable", "excluded"
INCOME_CLASSES = (
    (EXEMPT, gettext_lazy("Exempt function income")),
    (TAXABLE, gettext_lazy("Other income (taxable)")),
    (EXCLUDED, gettext_lazy("Not income (transfer between funds)")),
)
PROPERTY, OTHER = "property", "other"
EXPENSE_CLASSES = (
    (PROPERTY, gettext_lazy("Association property (management, maintenance, care)")),
    (OTHER, gettext_lazy("Other")),
)
# Defaults for the HOA chart (``ledger.chart``). Anything else starts as taxable income or
# property spending. Late fees and fines start taxable: ask the CPA.
DEFAULT_INCOME = {"4000": EXEMPT, "4050": EXEMPT, "4900": EXCLUDED, "4910": EXCLUDED}


@dataclass
class Line:
    account: Account
    amount: Decimal
    cls: str


@dataclass
class Worksheet:
    fy: int
    start: dt.date
    end: dt.date
    income: list[Line] = field(default_factory=list)
    expenses: list[Line] = field(default_factory=list)
    deductions: Decimal = ZERO
    exempt_min_pct: Decimal | None = None
    expenditure_min_pct: Decimal | None = None
    specific_deduction: Decimal = ZERO
    rate: Decimal | None = None

    def _sum(self, lines, cls):
        return sum((ln.amount for ln in lines if ln.cls == cls), ZERO)

    @property
    def exempt_income(self) -> Decimal:
        return self._sum(self.income, EXEMPT)

    @property
    def other_income(self) -> Decimal:
        return self._sum(self.income, TAXABLE)

    @property
    def gross_income(self) -> Decimal:
        return self.exempt_income + self.other_income

    @property
    def exempt_pct(self) -> Decimal | None:
        if not self.gross_income:
            return None
        return (self.exempt_income * 100 / self.gross_income).quantize(Decimal("0.1"))

    @property
    def property_spending(self) -> Decimal:
        return self._sum(self.expenses, PROPERTY)

    @property
    def total_spending(self) -> Decimal:
        return sum((ln.amount for ln in self.expenses), ZERO)

    @property
    def property_pct(self) -> Decimal | None:
        if not self.total_spending:
            return None
        return (self.property_spending * 100 / self.total_spending).quantize(Decimal("0.1"))

    @property
    def income_test(self) -> bool | None:
        if self.exempt_pct is None or self.exempt_min_pct is None:
            return None
        return self.exempt_pct >= self.exempt_min_pct

    @property
    def spending_test(self) -> bool | None:
        if self.property_pct is None or self.expenditure_min_pct is None:
            return None
        return self.property_pct >= self.expenditure_min_pct

    @property
    def taxable_income(self) -> Decimal:
        return max(ZERO, self.other_income - self.deductions - self.specific_deduction)

    @property
    def tax(self) -> Decimal | None:
        if self.rate is None:
            return None
        return (self.taxable_income * self.rate / 100).quantize(ZERO, rounding=ROUND_HALF_UP)


def _settings(association) -> dict:
    return dict((association.settings or {}).get(SETTINGS_KEY) or {})


def classes(association) -> dict:
    """``{"income": {code: cls}, "expense": {code: cls}, "deductions": {fy: str}}``."""
    s = _settings(association)
    return {
        "income": dict(s.get("income") or {}),
        "expense": dict(s.get("expense") or {}),
        "deductions": dict(s.get("deductions") or {}),
    }


def income_class(association, code: str) -> str:
    return classes(association)["income"].get(code) or DEFAULT_INCOME.get(code, TAXABLE)


def expense_class(association, code: str) -> str:
    return classes(association)["expense"].get(code) or PROPERTY


def save(association, *, fy: int, income: dict, expense: dict, deductions: Decimal, user=None):
    """Store the Treasurer's classification and the year's directly connected deductions."""
    s = _settings(association)
    ok_income = {c for c, _label in INCOME_CLASSES}
    ok_expense = {c for c, _label in EXPENSE_CLASSES}
    s["income"] = {k: v for k, v in income.items() if v in ok_income}
    s["expense"] = {k: v for k, v in expense.items() if v in ok_expense}
    s["deductions"] = {**(s.get("deductions") or {}), str(fy): str(max(ZERO, deductions))}
    settings = dict(association.settings or {})
    settings[SETTINGS_KEY] = s
    association.settings = settings
    association.save(update_fields=["settings"])
    audit.record("reports.tax_1120h_saved", association, actor=user, changes={"fy": fy, **s})


def worksheet(association, fy: int) -> Worksheet:
    from homeoa.closing.services import fiscal_year_bounds

    start, end = fiscal_year_bounds(association, fy)
    act = activity(association, start=start, end=end, basis=ACCRUAL, without_close=True)
    by_code: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for (code, _fund), net in act.items():
        by_code[code] += net
    ws = Worksheet(fy=fy, start=start, end=end)
    for account in Account.all_associations.filter(association=association).order_by("code"):
        net = by_code.get(account.code, ZERO)
        if not net:
            continue
        if account.type == AccountType.INCOME:
            ws.income.append(Line(account, -net, income_class(association, account.code)))
        elif account.type == AccountType.EXPENSE:
            ws.expenses.append(Line(account, net, expense_class(association, account.code)))
    raw = classes(association)["deductions"].get(str(fy))
    ws.deductions = Decimal(raw) if raw else ZERO
    ws.exempt_min_pct = rules.value(association, "tax.1120h.exempt_income_min_pct", end)
    ws.expenditure_min_pct = rules.value(association, "tax.1120h.expenditure_min_pct", end)
    ws.specific_deduction = rules.value(association, "tax.1120h.specific_deduction", end) or ZERO
    ws.rate = rules.value(association, "tax.1120h.rate", end)
    return ws


def _yes_no(value) -> str:
    if value is None:
        return "—"
    return _("Passes") if value else _("Fails")


def table(ws: Worksheet) -> export.TableSpec:
    """The worksheet as one table, for the CPA package."""
    rows = [
        [_("Exempt function income"), ws.exempt_income],
        [_("Other income (taxable)"), ws.other_income],
        [_("Gross income"), ws.gross_income],
        [_("Exempt function income share (%)"), ws.exempt_pct],
        [_("Income test"), _yes_no(ws.income_test)],
        [_("Spending on association property"), ws.property_spending],
        [_("Total spending"), ws.total_spending],
        [_("Association property share (%)"), ws.property_pct],
        [_("Spending test"), _yes_no(ws.spending_test)],
        [_("Deductions directly connected with other income"), ws.deductions],
        [_("Specific deduction"), ws.specific_deduction],
        [_("Taxable income"), ws.taxable_income],
        [_("Tax"), ws.tax],
    ]
    return export.TableSpec(
        title=_("Form 1120-H worksheet"),
        filename="form-1120-h-worksheet",
        subtitle=_("Fiscal year %(start)s to %(end)s · accrual basis · not tax advice")
        % {"start": ws.start.isoformat(), "end": ws.end.isoformat()},
        columns=[export.Column(_("Item")), export.Column(_("Amount or result"))],
        rows=rows,
    )
