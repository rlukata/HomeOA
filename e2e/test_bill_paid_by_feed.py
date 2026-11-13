"""An outgoing bank transaction matching an approved bill's
amount and vendor is suggested as "Pays bill"; confirming marks the bill Paid and links
it; Undo match returns it to Approved. A pending bill only gets a hint (no confirm). A
bill already marked paid by hand is offered as a link (no second expense); categorize
then warns about double counting."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers
from test_bills import MOW, _draft, _submit

_TXN = (
    seed_helpers.APPROVE_ON_DEMO_TODAY
    + """
import json
from decimal import Decimal
from homeoa.banking.models import BankAccount, BankTransaction
acct = BankAccount.all_associations.get(name="Operating checking")
batch = BankTransaction.all_associations.filter(bank_account=acct).latest("pk").batch
t = BankTransaction.all_associations.create(
    association=acct.association, bank_account=acct, batch=batch, date="2026-10-20",
    amount=Decimal("-485.00"), description="GREENWAY LANDSCAPING BILLPAY",
    external_id=REF, sequence=999)
print("E2EJSON" + json.dumps({"pk": t.pk}))
"""
)


def _debit(ref):
    return seed_helpers.run_script(f"REF={ref!r}\n" + _TXN, "debit")["pk"]


@pytest.fixture
def bank(seed):
    seed_helpers.bank()


def test_bill_paid_by_feed_and_unmatch(page, bank, login, axe):
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-FEED")
    _submit(page)  # approved by the secretary
    bill_url = page.url
    txn = _debit("e2e-feed-1")
    page.goto(f"/money/bank/txn/{txn}/")
    sug = page.get_by_test_id("bill-suggestion")
    expect(sug).to_contain_text("Pays bill")
    expect(sug).to_contain_text("485.00")
    axe(page)
    page.get_by_test_id("suggestion").filter(has=sug).get_by_role("button", name="Confirm").click()
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("matched-bill")).to_be_visible()
    page.goto(bill_url)
    expect(page.get_by_test_id("bill-status")).to_have_text("Paid")
    page.goto("/vendors/")  # The paid bill feeds "Paid this year"
    expect(page.get_by_test_id("vendor-paid-greenway-landscaping")).to_have_text("$485.00")

    page.goto(f"/money/bank/txn/{txn}/")
    page.get_by_role("button", name="Undo match").click()
    page.goto(bill_url)
    expect(page.get_by_test_id("bill-status")).to_have_text("Approved")
    page.goto("/vendors/")
    expect(page.get_by_test_id("vendor-paid-greenway-landscaping")).to_have_text("$0.00")


def test_unapproved_bill_never_suggested(page, bank, login):
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-PEND", lines=(MOW,))
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).to_have_text("Pending approval")
    txn = _debit("e2e-feed-2")
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("bill-suggestion")).to_have_count(0)
    hint = page.get_by_test_id("suggestion").filter(has=page.get_by_test_id("bill-pending-hint"))
    expect(hint).to_have_count(1)
    expect(hint.get_by_role("button", name="Confirm")).to_have_count(0)
    expect(hint.get_by_role("link", name="Open bill")).to_be_visible()
    page.goto("/money/bank/")
    expect(page.get_by_text(re.compile(r"pays bill", re.I))).to_have_count(0)


_RULE = """
from homeoa.banking.models import BankAccount, CategorizeRule
from homeoa.ledger.models import Account
a = BankAccount.all_associations.get(name="Operating checking").association
acct = Account.all_associations.filter(association=a, code__startswith="5").order_by("code").first()
CategorizeRule.all_associations.create(association=a, contains="GREENWAY", account=acct)
print("E2EJSON{}")
"""

_EXPENSE = """
import json
from decimal import Decimal
from django.db.models import Sum
from homeoa.ledger.models import JournalLine
q = JournalLine.objects.filter(account__code__startswith="5")
print("E2EJSON" + json.dumps({"debits": str(q.aggregate(s=Sum("debit"))["s"] or 0)}))
"""


def test_manual_payment_then_debit_links_once(page, bank, login):
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-MAN")
    _submit(page)
    bill_url = page.url
    page.get_by_label("Paid on").fill("2026-10-19")
    page.get_by_test_id("bill-mark-paid").click()
    expect(page.get_by_test_id("bill-status")).to_have_text("Paid")
    before = seed_helpers.run_script(_EXPENSE, "exp")["debits"]

    seed_helpers.run_script(_RULE, "rule")  # a categorize rule also matches the debit
    txn = _debit("e2e-feed-3")
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("bill-suggestion")).to_have_count(0)
    link = page.get_by_test_id("suggestion").filter(has=page.get_by_test_id("bill-link-suggestion"))
    expect(link.first).to_be_visible()
    page.goto(f"/money/bank/txn/{txn}/categorize/")
    expect(page.get_by_test_id("categorize-warning")).to_contain_text("counted twice")

    page.goto(f"/money/bank/txn/{txn}/")
    link.first.get_by_role("button", name="Confirm").click()
    expect(page.get_by_text(re.compile("Linked to the recorded payment"))).to_be_visible()
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("matched-bill")).to_be_visible()
    after = seed_helpers.run_script(_EXPENSE, "exp")["debits"]
    assert before == after, (before, after)  # no double-counted expense
    page.goto(bill_url)
    expect(page.get_by_test_id("bill-status")).to_have_text("Paid")
