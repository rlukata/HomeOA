"""Demo seed: the demo association's adopted 2026 budget.

Its assessment, reserve and insurance lines match the demo dues schedule, so
``plan_dues`` agrees with the schedule already billing. It creates no schedule.
"""

from __future__ import annotations

from decimal import Decimal

from homeoa.ledger.models import Account, Fund

from . import services
from .models import BudgetLine

# Line label -> (account code, fund code). The negative income transfers become positive
# transfer expenses (5900 / 5910), which is how the schedule reads them.
_INCOME = {
    "Homeowner Assessment": ("4000", "operating"),
    "Transfer to Reserves": ("5900", "operating"),
    "Transfer to Insurance Reserves": ("5910", "operating"),
}
_OPERATING = {
    "General Repair & Maint.": "5200",
    "Landscape – Other": "5100",
    "Pest Control": "5200",
    "Water": "5000",
    "Storm / Drainage": "5000",
    "Garbage": "5000",
}
_OTHER_INCOME = {
    "Major Maintenance Assess (reserve)": ("4900", "reserve"),
    "Insurance Assessments": ("4910", "insurance"),
    "Interest Income – Reserve": ("4300", "reserve"),
    "Special Assessment Income": ("4050", "operating"),
}
_OTHER_EXPENSE = {
    "Major Maintenance Expense": ("5200", "reserve"),
    "Insurance Expense": ("5300", "insurance"),
    "Special Assessment Admin Expense": ("5400", "operating"),
}
# Group names are stored as data in the board's language, like budget lines entered by hand.
GROUPS = {
    "income": "Income",
    "operating_expense": "Operating expenses",
    "other_income": "Other income",
    "other_expense": "Other expenses",
}


def _target(section: str, label: str) -> tuple[str, str]:
    if section == "income":
        return _INCOME[label]
    if section == "operating_expense":
        return _OPERATING.get(label, "5400"), "operating"
    if section == "other_income":
        return _OTHER_INCOME[label]
    return _OTHER_EXPENSE[label]


def seed_budget(association, lines: dict[str, list[tuple[str, Decimal]]], year: int = 2026):
    """Create and adopt the pilot budget from ``core.demo.budget_lines()``."""
    budget = services.create_budget(association, year, name=f"{year} budget")
    order = 0
    for section, rows in lines.items():
        for label, amount in rows:
            code, fund_code = _target(section, label)
            order += 1
            BudgetLine.objects.create(
                association=association,
                budget=budget,
                group=GROUPS.get(section, section),
                label=label,
                account=Account.all_associations.get(association=association, code=code),
                fund=Fund.all_associations.get(association=association, code=fund_code),
                amount=abs(amount),
                sort_order=order,
            )
    return services.adopt(budget)
