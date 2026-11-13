"""The month's money in plain words, on the board packet and Money > Overview."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.banking.models import BankAccount
from homeoa.billing.models import ChargeKind
from homeoa.billing.services.adjustments import manual_charge
from homeoa.budget import services as budgets
from homeoa.budget.models import BudgetLine
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.ledger.services import Line, post_entry
from homeoa.registry import services as registry
from homeoa.registry.models import Person, Unit
from homeoa.reports import packet, story
from homeoa.reports.tests.test_board_packet import TREASURER, pilot  # noqa: F401

D = Decimal
SEP1, SEP30 = dt.date(2026, 9, 1), dt.date(2026, 9, 30)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR", timezone="UTC")
    load_chart_of_accounts(a)
    with association_context(a):
        for code, gl in (("operating", "1000"), ("reserve", "1010")):
            BankAccount.objects.create(
                name=f"{code} account",
                gl_account=Account.objects.get(code=gl),
                fund=Fund.objects.get(code=code),
            )
        yield a


def _move(debit, credit, amount, date):
    post_entry(
        [
            Line(account=debit, fund="operating", debit=D(amount)),
            Line(account=credit, fund="operating", credit=D(amount)),
        ],
        date=date,
        memo="test",
    )


def _unit_owing(number, amount, due):
    unit = Unit.objects.create(number=number)
    registry.set_initial_ownership(unit, [Person.objects.create(full_name=f"Owner {number}")], due)
    manual_charge(
        unit, D(amount), "Dues", "Dues", kind=ChargeKind.ASSESSMENT, date=due, due_date=due
    )


def _budget():
    budget = budgets.create_budget(Association.objects.get(), 2026, name="2026 budget")
    for label, code, amount in (("Landscaping", "5100", "1200.00"), ("Repairs", "5200", "6000.00")):
        BudgetLine.objects.create(
            budget=budget,
            group="Expenses",
            label=label,
            account=Account.objects.get(code=code),
            fund=Fund.objects.get(code="operating"),
            amount=D(amount),
        )
    budgets.adopt(budget)


def test_month_in_plain_words(assoc):
    _move("1000", "4000", "5000.00", dt.date(2026, 1, 5))
    _move("5100", "1000", "1500.00", dt.date(2026, 6, 1))
    _move("5200", "1000", "500.00", dt.date(2026, 6, 2))
    _move("1000", "4000", "240.00", dt.date(2026, 9, 10))
    _unit_owing("A", "300.00", dt.date(2026, 8, 1))  # 60 days past due on Sep 30
    _unit_owing("B", "100.00", dt.date(2026, 9, 25))
    _budget()
    assert story.money_sentences(assoc, SEP1, SEP30) == [
        "Operating cash rose $240.00 to $3,240.00.",
        "Reserve cash stayed at $0.00.",
        "Owners owe $400.00 in total; 1 unit is more than 30 days behind.",
        "Spending this year so far is $2,000.00, 28% of the $7,200.00 budget, "
        "with 9 of 12 months gone.",
        "Already over the year's budget: Landscaping by $300.00.",
        "No bills were paid.",
    ]


def test_plain_words_without_budget_or_debts(assoc):
    _move("5200", "1000", "75.00", dt.date(2026, 9, 3))
    assert story.money_sentences(assoc, SEP1, SEP30) == [
        "Operating cash fell $75.00 to -$75.00.",
        "Reserve cash stayed at $0.00.",
        "No owner owes anything.",
        "No budget is adopted for this year, so spending isn't compared with one.",
        "No bills were paid.",
    ]


def test_board_packet_summary_opens_with_the_month_in_plain_words(pilot):  # noqa: F811
    summary = packet.specs(pilot, 2026, 9)[0]
    assert summary.title == "Summary"
    intro = list(summary.intro)
    assert intro[0].startswith("Operating cash ")
    assert any("bill" in s for s in intro)
    # Sentences from the work orders and violations modules, and the next deadline.
    assert any("work order" in s for s in intro)
    assert any("violation" in s for s in intro)
    assert packet.build(pilot, 2026, 9).startswith(b"%PDF")


def test_money_overview_shows_the_month_so_far(pilot, client):  # noqa: F811
    client.force_login(get_user_model().objects.get(email=TREASURER))
    html = client.get(reverse("dashboard:money")).content.decode()
    assert 'data-testid="money-story"' in html
    assert "October so far" in html
    assert "Operating cash " in html
