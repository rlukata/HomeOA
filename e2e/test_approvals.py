"""Bill approvals: rules "<$1,000: 1 approval" and
">= $1,000: 2 approvals"; the person who entered a bill cannot approve it; one approval
per person; reject returns the bill to draft and reverses the accrual."""

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL, manage
from helpers import seed as seed_helpers
from test_bills import MOW, _fill

REPAIR = ("5200 · Repairs & maintenance", "Operating", "1240.00", "Roof repair")
THIRD_EMAIL = "president@cedarrow.example"

_RULES = f"""
import json
from decimal import Decimal
from django.contrib.auth import get_user_model
from homeoa.ap.approvals.models import ApprovalRule
from homeoa.core.context import association_context
from homeoa.core.models import Association
a = Association.objects.order_by("pk").first()
u, _ = get_user_model().objects.get_or_create(email={THIRD_EMAIL!r})
u.is_active = True
u.save()
with association_context(a):
    ApprovalRule.objects.all().delete()
    ApprovalRule.objects.create(association=a, name="Under 1000", priority=10,
        min_amount=Decimal("0"), max_amount=Decimal("1000"), required_approvals=1)
    ApprovalRule.objects.create(association=a, name="1000 and up", priority=20,
        min_amount=Decimal("1000"), required_approvals=2)
print("E2EJSON" + json.dumps({{}}))
"""

_BILL = """
import json
from homeoa.ap.models import Bill
b = Bill.all_associations.get(invoice_number=INV)
print("E2EJSON" + json.dumps({"status": b.status, "entry": b.entry_id}))
"""


def _bill(inv):
    return seed_helpers.run_script(f"INV={inv!r}\n" + _BILL, "bill")


@pytest.fixture
def rules(seed):
    seed_helpers.run_script(_RULES, "rules")
    manage("grant_role", THIRD_EMAIL, "board", "--title", "president")


def _submitted(page, login, inv, lines):
    login(BOARD_EMAIL)
    _fill(page, inv, lines)
    page.get_by_test_id("bill-save").click()
    expect(page.get_by_test_id("bill-status")).to_have_text("Draft")
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).not_to_have_text("Draft")
    # The entrant sees no Approve button.
    expect(page.get_by_test_id("bill-approve")).to_have_count(0)
    url = page.url
    page.context.clear_cookies()
    return url


def test_bill_single_approval(page, rules, login, axe):
    url = _submitted(page, login, "INV-485", (MOW,))
    login(SECRETARY_EMAIL)
    page.goto(url)
    expect(page.get_by_test_id("approval-progress")).to_have_text("0 of 1 approvals")
    axe(page)
    page.get_by_test_id("bill-approve").click()
    expect(page.get_by_test_id("approval-progress")).to_have_text("Approved · pay at bank")
    assert _bill("INV-485")["status"] == "approved"


def test_bill_two_approvals(page, rules, login):
    url = _submitted(page, login, "INV-1240", (REPAIR,))
    login(SECRETARY_EMAIL)
    page.goto(url)
    page.get_by_test_id("bill-approve").click()
    expect(page.get_by_test_id("approval-progress")).to_have_text("1 of 2 approvals")
    # Same person can't approve twice: no button after their approval.
    page.reload()
    expect(page.get_by_test_id("bill-approve")).to_have_count(0)
    page.goto("/money/bills/")
    expect(page.get_by_test_id("bills-waiting")).to_contain_text("INV-1240")
    page.context.clear_cookies()
    login(THIRD_EMAIL)
    page.goto(url)
    page.get_by_test_id("bill-approve").click()
    expect(page.get_by_test_id("approval-progress")).to_have_text("Approved · pay at bank")


@pytest.mark.parametrize(("amount", "needed"), [("999.99", 1), ("1000.00", 2)])
def test_threshold_boundary(page, rules, login, amount, needed):
    url = _submitted(page, login, f"INV-B{amount}", ((MOW[0], MOW[1], amount, MOW[3]),))
    login(SECRETARY_EMAIL)
    page.goto(url)
    expect(page.get_by_test_id("approval-progress")).to_have_text(f"0 of {needed} approvals")


def test_reject_returns_to_draft_and_reverses(page, rules, login):
    url = _submitted(page, login, "INV-REJ", (MOW,))
    assert _bill("INV-REJ")["entry"]
    login(SECRETARY_EMAIL)
    page.goto(url)
    page.get_by_label("Reason").fill("Wrong amount")
    page.get_by_test_id("bill-reject").click()
    expect(page.get_by_text("Bill rejected and returned to draft.")).to_be_visible()
    page.reload()
    expect(page.get_by_test_id("bill-status")).to_have_text("Draft")
    assert _bill("INV-REJ") == {"status": "draft", "entry": None}
    expect(page.get_by_test_id("approval-history")).to_contain_text("Wrong amount")
