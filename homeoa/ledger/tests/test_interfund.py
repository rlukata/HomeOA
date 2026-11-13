"""Open interfund balances and the "Settle" Home to-do."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant
from homeoa.ap import services as ap
from homeoa.ap.models import Bill, BillLine
from homeoa.banking import matching
from homeoa.banking import services as banking
from homeoa.banking.models import BankAccount, BankTransaction
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.ledger import interfund
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry
from homeoa.ledger.services import Line, post_entry
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.registry import services as registry
from homeoa.registry.models import Person, Unit
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

D = Decimal
OCT = dt.date(2026, 10, 1)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        from homeoa.ap.approvals.models import ApprovalRule

        ApprovalRule.objects.create(name="Auto", required_approvals=0)
        yield a


@pytest.fixture
def treasurer(assoc):
    user = get_user_model().objects.create_user(email="t@example.org")
    grant(user, association=assoc, start=dt.date(2020, 1, 1), title="treasurer")
    return user


def _bank(name, gl, fund):
    return BankAccount.objects.create(
        name=name, gl_account=Account.objects.get(code=gl), fund=Fund.objects.get(code=fund)
    )


@pytest.fixture
def banks(assoc):
    return {
        "operating": _bank("Operating checking", "1000", "operating"),
        "reserve": _bank("Reserve savings", "1010", "reserve"),
        "insurance": _bank("Insurance savings", "1020", "insurance"),
    }


def _pay_split_bill(treasurer, bank, lines):
    vendor = vendor_services.save_vendor(Vendor(name="Roofer"))
    bill = Bill.objects.create(
        vendor=vendor,
        invoice_number="R-1",
        bill_date=dt.date(2026, 9, 30),
        due_date=dt.date(2026, 10, 22),
        description="Roof",
    )
    for account, fund, amount in lines:
        BillLine.objects.create(
            bill=bill,
            account=Account.objects.get(code=account),
            fund=Fund.objects.get(code=fund),
            amount=D(amount),
        )
    bill = ap.submit(bill, treasurer)
    return ap.pay_from_one_bank(bill, treasurer, paid_on=OCT, bank_account=bank)


def _transfer(src, dst, amount, day, user):
    n = BankTransaction.objects.count()
    for bank, amt in ((src, f"-{amount}"), (dst, amount)):
        banking.import_transactions(
            bank,
            [FeedTxn(external_id=f"x{n}-{bank.pk}", posted=day, amount=D(amt), description="T")],
            source="csv",
        )
    out = BankTransaction.objects.get(external_id=f"x{n}-{src.pk}")
    inn = BankTransaction.objects.get(external_id=f"x{n}-{dst.pk}")
    matching.confirm_interfund(out, inn, user)


def _titles(assoc):
    return [i.title for i in todos.collect(assoc) if i.source == "interfund"]


def test_multifund_bill_then_partial_and_full_transfer(assoc, treasurer, banks):
    _pay_split_bill(
        treasurer,
        banks["operating"],
        [("5200", "operating", "400.00"), ("5200", "reserve", "1250.00")],
    )
    (bal,) = interfund.open_interfund_balances(assoc)
    assert (bal.debtor.code, bal.creditor.code, bal.amount) == ("reserve", "operating", D("1250"))
    assert isinstance(bal.amount, Decimal)
    (item,) = [i for i in todos.collect(assoc) if i.source == "interfund"]
    assert item.title == "Settle $1,250.00: Reserve owes Operating"
    assert item.url == "/money/reports/gl/?account=1900&fund=operating"
    assert item.id == "ledger-interfund-operating-reserve"

    entries = JournalEntry.objects.count()
    interfund.todo_items(assoc)
    assert JournalEntry.objects.count() == entries  # read-only

    _transfer(banks["reserve"], banks["operating"], "1000.00", dt.date(2026, 10, 5), treasurer)
    assert _titles(assoc) == ["Settle $250.00: Reserve owes Operating"]
    _transfer(banks["reserve"], banks["operating"], "250.00", dt.date(2026, 10, 6), treasurer)
    assert _titles(assoc) == []
    assert interfund.open_interfund_balances(assoc) == []


def test_insurance_pair_uses_1920_2920(assoc, treasurer, banks):
    # Monthly insurance transfer accrued: operating owes insurance.
    post_entry(
        [
            Line(account="5910", fund="operating", debit=D("893.08")),
            Line(account="2920", fund="operating", credit=D("893.08")),
            Line(account="1920", fund="insurance", debit=D("893.08")),
            Line(account="4910", fund="insurance", credit=D("893.08")),
        ],
        date=OCT,
    )
    # Reserve paid an insurance-fund bill: insurance owes reserve.
    _pay_split_bill(treasurer, banks["reserve"], [("5300", "insurance", "100.00")])
    rows = {
        (b.debtor.code, b.creditor.code): (b.amount, b.due_from, b.view_fund.code)
        for b in interfund.open_interfund_balances(assoc)
    }
    assert rows == {
        ("operating", "insurance"): (D("893.08"), "1920", "operating"),
        ("insurance", "reserve"): (D("100.00"), "1920", "reserve"),
    }
    assert sorted(_titles(assoc)) == [
        "Settle $100.00: Insurance owes Reserve",
        "Settle $893.08: Operating owes Insurance",
    ]
    _transfer(banks["operating"], banks["insurance"], "893.08", dt.date(2026, 10, 5), treasurer)
    assert _titles(assoc) == ["Settle $100.00: Insurance owes Reserve"]


def test_zero_balance_no_item(assoc, treasurer, banks):
    assert interfund.open_interfund_balances(assoc) == []
    _pay_split_bill(treasurer, banks["operating"], [("5200", "operating", "50.00")])
    assert _titles(assoc) == []


def test_as_of_excludes_later_entries(assoc, treasurer, banks):
    _pay_split_bill(treasurer, banks["operating"], [("5200", "reserve", "75.00")])
    assert interfund.open_interfund_balances(assoc, as_of=OCT - dt.timedelta(days=1)) == []
    assert interfund.open_interfund_balances(assoc, as_of=OCT)[0].amount == D("75.00")


def test_second_association_isolated(assoc, treasurer, banks):
    _pay_split_bill(treasurer, banks["operating"], [("5200", "reserve", "75.00")])
    other = Association.objects.create(name="Other", short_name="O")
    load_chart_of_accounts(other)
    assert interfund.open_interfund_balances(other) == []
    assert interfund.todo_items(other) == []
    with association_context(other):
        post_entry(
            [
                Line(account="1900", fund="operating", debit=D("10.00")),
                Line(account="1000", fund="operating", credit=D("10.00")),
                Line(account="5200", fund="reserve", debit=D("10.00")),
                Line(account="2900", fund="reserve", credit=D("10.00")),
            ],
            date=OCT,
        )
    assert interfund.open_interfund_balances(assoc)[0].amount == D("75.00")
    assert interfund.open_interfund_balances(other)[0].amount == D("10.00")


def test_owner_sees_no_item_board_does(assoc, treasurer, banks, client):
    _pay_split_bill(treasurer, banks["operating"], [("5200", "reserve", "75.00")])
    client.force_login(treasurer)
    assert "Settle $75.00: Reserve owes Operating" in client.get("/").content.decode()

    owner = get_user_model().objects.create_user(email="o@example.org")
    unit = Unit.objects.create(number="A")
    person = Person.objects.create(full_name="Owner A", user=owner)
    registry.set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
    client.force_login(owner)
    resp = client.get("/", follow=True)
    assert "Settle $" not in resp.content.decode()
    assert "/me/" in resp.redirect_chain[-1][0]
