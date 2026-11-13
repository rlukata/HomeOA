"""Paying a two-fund bill from the operating bank leaves the reserve share
owed to operating; board Home shows "Settle $X: Reserve owes Operating" whose Review opens
the GL on 1900 in the operating fund. Matching interfund transfers shrinks, then clears the
to-do. Owners never see it. seed_demo already leaves "Operating owes Reserve" open, so the
bill's reserve share is larger and the expected amount is computed from that baseline."""

import re
from decimal import Decimal

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import seed as seed_helpers
from test_bill_paid_by_feed import bank  # noqa: F401
from test_bills import _draft, _submit

OPS = ("5100 · Landscaping", "Operating", "100.00", "October mowing")
RES = ("5200 · Repairs & maintenance", "Reserve", "2000.00", "Reserve repair")
RESERVE_SHARE = Decimal("2000.00")
SETTLE = re.compile(r"^Settle \$[\d,.]+: Reserve owes Operating$")
BASE = re.compile(r"^Settle \$([\d,.]+): Operating owes Reserve$")

_TXN = (
    seed_helpers.APPROVE_ON_DEMO_TODAY
    + """
import json
from decimal import Decimal
from homeoa.banking.models import BankAccount, BankTransaction
acct = BankAccount.all_associations.get(name=NAME)
batch = BankTransaction.all_associations.filter(
    bank_account__association=acct.association).latest("pk").batch
t = BankTransaction.all_associations.create(
    association=acct.association, bank_account=acct, batch=batch, date="2026-10-21",
    amount=Decimal(AMOUNT), description=DESC, external_id=REF, sequence=SEQ)
print("E2EJSON" + json.dumps({"pk": t.pk}))
"""
)


def _txn(name, amount, ref, seq, desc=None):
    desc = desc or "TRANSFER " + ref
    script = f"NAME={name!r}\nAMOUNT={amount!r}\nREF={ref!r}\nSEQ={seq}\nDESC={desc!r}\n" + _TXN
    return seed_helpers.run_script(script, "transfer txn")["pk"]


def _settle_items(page):
    page.goto("/")
    return page.locator("#todo-card li").filter(has=page.locator(".att-t", has_text=SETTLE))


def _baseline(page):
    """What seed_demo leaves as "Operating owes Reserve" (0 if nothing)."""
    page.goto("/")
    for t in page.locator("#todo-card .att-t").all_inner_texts():
        if m := BASE.match(t.strip()):
            return Decimal(m.group(1).replace(",", ""))
    return Decimal("0")


def _pay_two_fund_bill(page):
    """Returns the expected "Reserve owes Operating" amount after paying the bill."""
    base = _baseline(page)
    assert base < RESERVE_SHARE, base
    _draft(page, invoice="INV-F18", lines=(OPS, RES))
    _submit(page)
    txn = _txn(
        "Operating checking", "-2100.00", "e2e-f18-bill", 990, "GREENWAY LANDSCAPING BILLPAY"
    )
    page.goto(f"/money/bank/txn/{txn}/")
    sug = page.get_by_test_id("bill-suggestion")
    expect(page.get_by_test_id("bill-fund-split")).to_contain_text("Reserve")
    page.get_by_test_id("suggestion").filter(has=sug).get_by_role("button", name="Confirm").click()
    page.goto(f"/money/bank/txn/{txn}/")
    expect(page.get_by_test_id("matched-bill")).to_be_visible()
    return RESERVE_SHARE - base


def _transfer(page, amount, ref, seq):
    out = _txn("Reserve savings", f"-{amount}", ref + "-out", seq)
    _txn("Operating checking", amount, ref + "-in", seq + 1)
    page.goto(f"/money/bank/txn/{out}/")
    sug = page.get_by_test_id("suggestion").filter(has_text="Reserve transfer with")
    expect(sug).to_have_count(1)
    sug.get_by_role("button", name="Confirm").click()
    page.goto(f"/money/bank/txn/{out}/")
    expect(page.get_by_test_id("suggestion").filter(has_text="Reserve transfer")).to_have_count(0)


def test_settle_todo_after_two_fund_bill(page, bank, login, axe):  # noqa: F811
    login(BOARD_EMAIL)
    expect(_settle_items(page)).to_have_count(0)
    owed = _pay_two_fund_bill(page)

    item = _settle_items(page)
    expect(item).to_have_count(1)
    expect(item.locator(".att-t")).to_have_text(f"Settle ${owed:,.2f}: Reserve owes Operating")
    expect(
        page.locator("#todo-card .att-t").filter(has_text="Operating owes Reserve")
    ).to_have_count(0)
    expect(item).to_contain_text("then match the transfer")
    axe(page)
    review = item.get_by_role("link", name="Review")
    href = review.get_attribute("href") or ""
    assert "account=1900" in href and "fund=operating" in href, href
    review.click()
    expect(page).to_have_url(re.compile(r"/money/reports/gl/\?"))
    assert "account=1900" in page.url and "fund=operating" in page.url, page.url


def test_partial_then_full_transfer_clears_todo(page, bank, login):  # noqa: F811
    login(BOARD_EMAIL)
    owed = _pay_two_fund_bill(page)
    part = (owed / 2).quantize(Decimal("0.01"))
    _transfer(page, f"{part}", "e2e-f18-t1", 1001)
    item = _settle_items(page)
    expect(item.locator(".att-t")).to_have_text(
        f"Settle ${owed - part:,.2f}: Reserve owes Operating"
    )
    _transfer(page, f"{owed - part}", "e2e-f18-t2", 1003)
    expect(_settle_items(page)).to_have_count(0)
    other = page.locator("#todo-card .att-t").filter(has_text="Operating owes Reserve")
    expect(other).to_have_count(0)


def test_owner_home_has_no_settle_todo(page, bank, login, browser, base_url):  # noqa: F811
    login(BOARD_EMAIL)
    _pay_two_fund_bill(page)
    expect(_settle_items(page)).to_have_count(1)
    ctx = browser.new_context(base_url=base_url)
    owner = ctx.new_page()
    login(OWNER_EMAIL, owner)
    owner.goto("/")
    expect(owner.get_by_text(re.compile(r"Settle \$|owes Operating"))).to_have_count(0)
    ctx.close()
