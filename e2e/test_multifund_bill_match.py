"""A debit on the operating bank pays a bill in another fund. The
suggestion shows the per-fund split; confirming marks it Paid; Undo re-opens it. A pending
bill in another fund only gets the "approve it first" hint."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from test_bill_paid_by_feed import _debit, bank  # noqa: F401
from test_bills import _draft, _submit

INS = ("5300 · Insurance", "Insurance", "485.00", "Policy premium")
RES = ("5200 · Repairs & maintenance", "Reserve", "485.00", "Reserve repair")


def test_insurance_bill_paid_from_operating_and_undo(page, bank, login, axe):  # noqa: F811
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-INS", lines=(INS,))
    _submit(page)
    bill_url = page.url
    txn = _debit("e2e-f15-1")
    page.goto(f"/money/bank/txn/{txn}/")
    sug = page.get_by_test_id("bill-suggestion")
    expect(sug).to_contain_text("Pays bill")
    split = page.get_by_test_id("bill-fund-split")
    expect(split).to_contain_text("Insurance")
    expect(split).to_contain_text("485.00")
    expect(split).to_contain_text("owed to the paying fund")
    axe(page)
    page.get_by_test_id("suggestion").filter(has=sug).get_by_role("button", name="Confirm").click()
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("matched-bill")).to_be_visible()
    page.goto(bill_url)
    expect(page.get_by_test_id("bill-status")).to_have_text("Paid")

    page.goto(f"/money/bank/txn/{txn}/")
    page.get_by_role("button", name="Undo match").click()
    page.goto(bill_url)
    expect(page.get_by_test_id("bill-status")).to_have_text("Approved")


def test_pending_bill_other_fund_hint(page, bank, login):  # noqa: F811
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-RES", lines=(RES,))
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).to_have_text("Pending approval")
    txn = _debit("e2e-f15-2")
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("bill-suggestion")).to_have_count(0)
    hint = page.get_by_test_id("suggestion").filter(has=page.get_by_test_id("bill-pending-hint"))
    expect(hint).to_have_count(1)
    expect(hint).to_contain_text("Approve it first")
    expect(hint.get_by_role("button", name="Confirm")).to_have_count(0)
