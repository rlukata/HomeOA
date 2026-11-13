"""Bills (Money > Bills): enter with lines and file, submit posts the accrual
(Dr expense / Cr AP per fund), duplicate invoice blocked, no edits after submit, void
reverses, mark paid (Dr AP / Cr cash), locked period blocked, list filter + overdue,
owners refused. Today is 2026-10-07 (fixed clock)."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL, manage
from helpers import seed as seed_helpers

BILLS = "/money/bills/"
MOW = ("5100 · Landscaping", "Operating", "485.00", "October mowing")
GUTTER = ("5200 · Repairs & maintenance", "Reserve", "755.00", "Gutter replacement")

_BALANCES = """
import json
from decimal import Decimal
from django.db.models import Sum
from homeoa.ledger.models import JournalLine
out = {}
for code in ["1000", "1010", "2000", "5100", "5200"]:
    for fund in ["operating", "reserve"]:
        q = JournalLine.all_associations.filter(account__code=code, fund__code=fund)
        d = q.aggregate(d=Sum("debit"), c=Sum("credit"))
        out[f"{code}/{fund}"] = str((d["d"] or Decimal(0)) - (d["c"] or Decimal(0)))
print("E2EJSON" + json.dumps(out))
"""


def _balances() -> dict:
    return seed_helpers.run_script(_BALANCES, "balances")


def _delta(before, after) -> dict:
    from decimal import Decimal

    out = {k: Decimal(after[k]) - Decimal(before[k]) for k in after}
    return {k: str(v) for k, v in out.items() if v}


def _fill(page, invoice, lines, bill_date="2026-10-05", due="2026-11-04"):
    page.goto(BILLS)
    page.get_by_test_id("bill-add").click()
    page.get_by_label("Vendor").select_option(label="Greenway Landscaping")
    page.get_by_label("Invoice number").fill(invoice)
    page.get_by_label("Bill date").fill(bill_date)
    page.get_by_label("Due date").fill(due)
    for i, (account, fund, amount, memo) in enumerate(lines):
        p = f"[name$='-{i}-"
        page.locator(f"{p}account']").select_option(label=account)
        page.locator(f"{p}fund']").select_option(label=fund)
        page.locator(f"{p}amount']").fill(amount)
        page.locator(f"{p}memo']").fill(memo)


def _draft(page, invoice="INV-1001", lines=(MOW,), **kw):
    _fill(page, invoice, lines, **kw)
    page.get_by_test_id("bill-save").click()
    expect(page.get_by_text("Draft saved.")).to_be_visible()
    expect(page.get_by_test_id("bill-status")).to_have_text("Draft")


_APPROVE = """
from django.contrib.auth import get_user_model
from homeoa.ap.approvals import approve
from homeoa.ap.models import Bill
from homeoa.core.context import association_context
b = Bill.all_associations.get(pk=PK)
with association_context(b.association):
    approve(b, get_user_model().objects.get(email=WHO))
print("OK")
"""

PRESIDENT_EMAIL = "president@cedarrow.example"
# The demo's president has no login; the second approval of a reserve bill needs one.
_PRESIDENT_LOGIN = """
import datetime as dt
from django.contrib.auth import get_user_model
from homeoa.access.testing import grant
from homeoa.core.models import Association
a = Association.objects.get()
u, made = get_user_model().objects.get_or_create(email=WHO, defaults={"full_name": "Rafael Ortiz"})
if made:
    grant(u, association=a, start=dt.date(2025, 1, 15))
print("OK")
"""


def _submit(page):
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_text("Bill submitted and booked to accounts payable.")).to_be_visible()
    # Default rule: one approval by another board member (not the entrant).
    expect(page.get_by_test_id("bill-status")).to_have_text("Pending approval")
    pk = int(re.search(r"/money/bills/(\d+)/", page.url).group(1))
    # A bill with a reserve-fund line needs a second approval (WA: RCW 64.34.372(2)).
    for who in (SECRETARY_EMAIL, PRESIDENT_EMAIL):
        if who == PRESIDENT_EMAIL:
            r = manage("shell", "-c", f"WHO={who!r}\n" + _PRESIDENT_LOGIN, check=False)
            assert r.stdout.strip().endswith("OK"), r.stderr[-1500:]
        r = manage("shell", "-c", f"PK={pk}\nWHO={who!r}\n" + _APPROVE, check=False)
        assert r.stdout.strip().endswith("OK"), r.stderr[-1500:]
        page.reload()
        if page.get_by_test_id("bill-status").inner_text().strip() == "Approved":
            break
    expect(page.get_by_test_id("bill-status")).to_have_text("Approved")


def test_submit_posts_accrual_per_fund(page, seed, login, axe, tmp_path):
    before = _balances()
    login(BOARD_EMAIL)
    _draft(page, lines=(MOW, GUTTER))
    axe(page)
    assert _delta(before, _balances()) == {}  # a draft posts nothing
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    page.get_by_label("Invoice file (PDF or image)").set_input_files(str(pdf))
    page.get_by_role("button", name="Attach").click()
    expect(page.get_by_text("File attached.")).to_be_visible()
    expect(page.get_by_test_id("bill-attachment")).to_contain_text("invoice.pdf")
    _submit(page)
    expect(page.get_by_test_id("bill-amount")).to_have_text("$1,240.00")
    assert _delta(before, _balances()) == {
        "5100/operating": "485.00",
        "2000/operating": "-485.00",
        "5200/reserve": "755.00",
        "2000/reserve": "-755.00",
    }
    expect(page.get_by_role("link", name="Edit", exact=True)).to_have_count(0)
    page.goto(page.url + "edit/")
    expect(
        page.get_by_text("A submitted bill cannot be edited. Void it and enter it again.")
    ).to_be_visible()
    axe(page)


def test_void_reverses(page, seed, login):
    before = _balances()
    login(BOARD_EMAIL)
    _draft(page)
    _submit(page)
    page.get_by_label("Reason").fill("Duplicate of INV-1000")
    page.get_by_test_id("bill-void").click()
    expect(page.get_by_text("Bill voided.")).to_be_visible()
    expect(page.get_by_test_id("bill-status")).to_have_text("Void")
    assert _delta(before, _balances()) == {}
    # A voided invoice number can be entered again.
    _draft(page)


def test_mark_paid_debits_ap_credits_cash(page, seed, login):
    before = _balances()
    login(BOARD_EMAIL)
    _draft(page)
    _submit(page)
    page.get_by_label("Paid on").fill("2026-10-07")
    page.get_by_test_id("bill-mark-paid").click()
    expect(page.get_by_text("Bill marked paid.")).to_be_visible()
    expect(page.get_by_test_id("bill-status")).to_have_text("Paid")
    expect(page.get_by_test_id("bill-void")).to_have_count(0)
    assert _delta(before, _balances()) == {"5100/operating": "485.00", "1000/operating": "-485.00"}


def test_duplicate_invoice_blocked(page, seed, login):
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-2002")
    _fill(page, "inv-2002", (MOW,))
    page.get_by_test_id("bill-save").click()
    expect(
        page.get_by_text("This vendor already has a bill with invoice number inv-2002.")
    ).to_be_visible()


def test_locked_period_blocked(page, seed, login):
    r = manage(
        "shell",
        "-c",
        "import datetime as dt\n"
        "from homeoa.ledger.models import LedgerSettings\n"
        "from homeoa.core.models import Association\n"
        "a = Association.objects.order_by('pk').first()\n"
        "LedgerSettings.all_associations.update_or_create("
        "association=a, defaults={'locked_through': dt.date(2026, 9, 30)})\n",
    )
    assert r.returncode == 0, r.stderr
    before = _balances()
    login(BOARD_EMAIL)
    _draft(page, bill_date="2026-09-15")
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).to_have_text("Draft")
    expect(page.get_by_text(re.compile("lock", re.I)).first).to_be_visible()
    assert _delta(before, _balances()) == {}


def test_list_filter_totals_overdue(page, seed, login, axe):
    login(BOARD_EMAIL)
    _draft(page, invoice="INV-LATE", bill_date="2026-09-01", due="2026-10-01")
    _submit(page)
    _draft(page, invoice="INV-DRAFT")
    page.goto(BILLS)
    rows = page.locator("tr[data-testid^='bill-row-']")
    expect(rows).to_have_count(1)  # default "Open" hides drafts
    late = rows.filter(has_text="INV-LATE")
    expect(late.get_by_test_id("bill-overdue")).to_be_visible()
    expect(page.get_by_test_id("bill-total")).to_contain_text("1 overdue")
    expect(page.get_by_test_id("bill-total")).to_contain_text("$485.00")
    axe(page)
    page.locator("label.chip", has_text="All").click()
    expect(rows).to_have_count(2)
    page.locator("label.chip", has_text="Draft").click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("INV-DRAFT")


def test_owner_refused(page, seed, login):
    login(OWNER_EMAIL)
    resp = page.goto(BILLS)
    assert resp and resp.status == 403, resp and resp.status
