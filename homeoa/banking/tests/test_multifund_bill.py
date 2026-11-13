# ruff: noqa: F811  (fixtures imported from test_bill_matching)
"""One bank debit pays a bill whose lines span several funds."""

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse

from homeoa.ap import services as ap
from homeoa.ap.approvals.models import ApprovalRule
from homeoa.ap.models import Bill, BillLine
from homeoa.banking import matching
from homeoa.banking.models import BankAccount, Match, TxnStatus
from homeoa.ledger.models import Account, Fund, JournalEntry, JournalLine
from homeoa.ledger.services import account_balance, trial_balance
from homeoa.reports import queries

from .test_bill_matching import (  # noqa: F401  (fixtures)
    APPROVED_AT,
    PAID,
    _debit,
    assoc,
    city,
    operating,
    treasurer,
)

D = Decimal


def _fund(code):
    return Fund.objects.get(code=code)


def _bal(code, fund):
    return account_balance(Account.objects.get(code=code), fund=_fund(fund))


@pytest.fixture
def reserve_bank(assoc):
    return BankAccount.objects.create(
        name="Reserve savings",
        gl_account=Account.objects.get(code="1010"),
        fund=_fund("reserve"),
    )


def _split_bill(vendor, user, shares=(("operating", "400.00"), ("reserve", "212.34"))):
    ApprovalRule.objects.get_or_create(name="Auto", defaults={"required_approvals": 0})
    bill = Bill.objects.create(
        vendor=vendor, invoice_number="INV-9", bill_date=dt.date(2026, 9, 30)
    )
    for fund, amount in shares:
        BillLine.objects.create(
            bill=bill,
            account=Account.objects.get(code="5000"),
            fund=_fund(fund),
            amount=D(amount),
        )
    bill = ap.submit(bill, user)
    Bill.objects.filter(pk=bill.pk).update(approved_at=APPROVED_AT)
    bill.refresh_from_db()
    assert bill.status == Bill.Status.APPROVED
    return bill


def assoc_of(bill):
    return bill.association


def _fund_tb_balances():
    for fund in Fund.objects.all():
        assert trial_balance(fund=fund).difference == 0, fund.code


def test_suggestion_shows_per_fund_split(operating, city, treasurer):
    bill = _split_bill(city, treasurer)
    (s,) = [s for s in matching.suggestions(_debit(operating)) if s.kind == "bill"]
    assert s.extra["bill"] == bill and s.score == 95
    assert [(f.code, a) for f, a in s.extra["split"]] == [
        ("operating", D("400.00")),
        ("reserve", D("212.34")),
    ]


def test_insurance_only_bill_paid_from_operating(operating, city, treasurer):
    bill = _split_bill(city, treasurer, shares=(("insurance", "612.34"),))
    txn = _debit(operating)
    (s,) = [s for s in matching.suggestions(txn) if s.kind == "bill"]
    assert s.extra["bill"] == bill
    assert [(f.code, a) for f, a in s.extra["split"]] == [("insurance", D("612.34"))]
    matching.confirm_bill(txn, bill, treasurer)
    _fund_tb_balances()
    assert _bal("2000", "insurance") == 0
    assert _bal("1000", "operating") == D("-612.34")
    assert _bal("1920", "operating") == D("612.34")
    assert _bal("2920", "insurance") == D("612.34")
    act = queries.period_activity(assoc_of(bill), start=PAID, end=PAID, fund=_fund("insurance"))
    assert act.get("5000") == D("612.34")


def test_two_fund_bill_paid_from_operating(operating, city, treasurer):
    bill = _split_bill(city, treasurer)
    txn = _debit(operating)
    matching.confirm_bill(txn, bill, treasurer)
    bill.refresh_from_db()
    txn.refresh_from_db()
    assert bill.status == Bill.Status.PAID and txn.status == TxnStatus.MATCHED
    _fund_tb_balances()
    assert _bal("2000", "operating") == 0 and _bal("2000", "reserve") == 0
    assert _bal("1900", "operating") == D("212.34")
    assert _bal("2900", "reserve") == D("212.34")
    assert _bal("1000", "operating") == D("-612.34")
    # AP, bank and expense each posted exactly once.
    pay = JournalLine.objects.filter(entry=bill.payment_entry)
    assert pay.filter(account__code="1000").count() == 1
    assert pay.filter(account__code="2000").count() == 2  # one per fund
    assert sum(ln.debit for ln in pay.filter(account__code="2000")) == D("612.34")
    assert not pay.filter(account__code="5000").exists()
    exp = JournalLine.objects.filter(account__code="5000")
    assert sum(ln.debit - ln.credit for ln in exp) == D("612.34")
    assert JournalEntry.objects.filter(source_type=ap.SOURCE_PAYMENT).count() == 1


def test_single_fund_bill_unchanged(operating, city, treasurer):
    bill = _split_bill(city, treasurer, shares=(("operating", "612.34"),))
    s = [s for s in matching.suggestions(_debit(operating)) if s.kind == "bill"][0]
    assert s.extra["split"] == []
    matching.confirm_bill(_debit(operating), bill, treasurer)
    bill.refresh_from_db()
    codes = sorted(ln.account.code for ln in bill.payment_entry.lines.all())
    assert codes == ["1000", "2000"]


def test_undo_reverses_cleanly(operating, city, treasurer):
    bill = _split_bill(city, treasurer)
    txn = _debit(operating)
    matching.confirm_bill(txn, bill, treasurer)
    matching.undo(txn, treasurer)
    bill.refresh_from_db()
    assert bill.status == Bill.Status.APPROVED and bill.payment_entry is None
    _fund_tb_balances()
    for code in ("1900", "1000"):
        assert _bal(code, "operating") == 0
    assert _bal("2900", "reserve") == 0
    assert _bal("2000", "operating") == D("400.00")
    assert _bal("2000", "reserve") == D("212.34")
    matching.confirm_bill(txn, bill, treasurer)
    assert Match.objects.filter(bill=bill, undone_at__isnull=True).count() == 1


def test_later_transfer_settles_interfund(operating, reserve_bank, city, treasurer):
    bill = _split_bill(city, treasurer)
    matching.confirm_bill(_debit(operating), bill, treasurer)
    later = PAID + dt.timedelta(days=5)
    out = _debit(reserve_bank, desc="TRANSFER TO CHK", amount="-212.34", date=later)
    into = _debit(operating, desc="TRANSFER FROM SAV", amount="212.34", date=later)
    assert any(s.kind == "interfund" for s in matching.suggestions(out))
    matching.confirm_interfund(out, into, treasurer)
    _fund_tb_balances()
    assert _bal("1900", "operating") == 0 and _bal("2900", "reserve") == 0
    assert _bal("1000", "operating") == D("-400.00")
    assert _bal("1010", "reserve") == D("-212.34")


def test_insurance_share_uses_1920_2920(operating, city, treasurer):
    bill = _split_bill(city, treasurer, shares=(("operating", "400.00"), ("insurance", "212.34")))
    matching.confirm_bill(_debit(operating), bill, treasurer)
    _fund_tb_balances()
    assert _bal("1920", "operating") == D("212.34")
    assert _bal("2920", "insurance") == D("212.34")


def test_cash_basis_expense_per_fund_on_debit_date(assoc, operating, city, treasurer):
    bill = _split_bill(city, treasurer)
    start = dt.date(2026, 9, 1)
    before = queries.period_activity(assoc, start=start, end=PAID, fund=_fund("reserve"))
    assert "5000" not in before  # unpaid AP never appears
    matching.confirm_bill(_debit(operating), bill, treasurer)
    for fund, amount in (("operating", "400.00"), ("reserve", "212.34")):
        act = queries.period_activity(assoc, start=PAID, end=PAID, fund=_fund(fund))
        assert act.get("5000") == D(amount), fund
        assert "2000" not in act
    day_before = queries.period_activity(assoc, start=start, end=PAID - dt.timedelta(days=1))
    assert "5000" not in day_before


def test_txn_page_shows_split(client, operating, city, treasurer):
    _split_bill(city, treasurer)
    txn = _debit(operating)
    client.force_login(treasurer)
    html = client.get(reverse("banking:txn", args=[txn.pk])).content.decode()
    assert 'data-testid="bill-fund-split"' in html
    assert "$400.00" in html and "$212.34" in html


def test_pending_other_fund_bill_gets_hint(operating, city, treasurer):
    bill = Bill.objects.create(vendor=city, invoice_number="INV-P", bill_date=dt.date(2026, 9, 30))
    for fund, amount in (("operating", "400.00"), ("insurance", "212.34")):
        BillLine.objects.create(
            bill=bill,
            account=Account.objects.get(code="5000"),
            fund=_fund(fund),
            amount=D(amount),
        )
    bill = ap.submit(bill, treasurer)  # no approval rule: stays pending
    assert bill.status == Bill.Status.PENDING
    sugg = matching.suggestions(_debit(operating))
    assert [s.extra["bill"] for s in sugg if s.kind == "bill_pending"] == [bill]
    assert not [s for s in sugg if s.kind == "bill"]
