"""Period close (FIN-15), Money > Close: the month checklist blocks the close
until bank recs and draft bills are done; posting into a closed month fails; reopen needs
a reason and moves the lock back; year-end close keeps the year's income statement, moves
net income into fund balance and zeroes income/expense on the trial balance; only
finance.manage can close. Clock is fixed at 2026-10-07; seed_demo posts Aug-Sep 2026."""

import csv
import io
import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL
from helpers.flows import approve_change, expect_sent_for_approval
from helpers.seed import run_script
from test_financial_statements import BS, IS, _money

CLOSE = "/money/close/"
SEP_LOCK = "The books are locked through Sep 30, 2026."
LOCK_ERR = re.compile(r"locked through 2026-09-30|locked through Sep 30, 2026")

_PRE = """
import datetime as dt, json
from homeoa.core.context import association_context
from homeoa.core.models import Association
a = Association.objects.order_by("pk").first()
with association_context(a):
"""

DRAFT_BILL = (
    _PRE
    + """
    from homeoa.ap.models import Bill
    from homeoa.vendors.models import Vendor
    b = Bill.objects.create(association=a, vendor=Vendor.objects.order_by("pk").first(),
        invoice_number="E2E-CLOSE-1", bill_date=dt.date(2026, 9, 20))
    print("E2EJSON" + json.dumps({"bill": b.pk}))
"""
)

# Fix both blockers: drop the draft bill, complete a zero-difference rec per account.
FIX = (
    _PRE
    + """
    from homeoa.ap.models import Bill
    from homeoa.banking.models import BankAccount, Reconciliation
    from django.utils import timezone
    Bill.objects.filter(status=Bill.Status.DRAFT).delete()
    for acct in BankAccount.objects.filter(active=True):
        Reconciliation.objects.create(association=a, bank_account=acct,
            statement_date=STATEMENT_DATE, beginning_balance=0, ending_balance=0,
            completed_at=timezone.now())
    print("E2EJSON" + json.dumps({}))
"""
)

FISCAL_OCT = (
    _PRE
    + """
    Association.objects.filter(pk=a.pk).update(fiscal_year_start_month=10)
    print("E2EJSON" + json.dumps({}))
"""
)


def _month(page):
    return page.get_by_role("region", name="Close a month")


def _fix(date="dt.date(2026, 9, 30)"):
    run_script(FIX.replace("STATEMENT_DATE", date), "close fix")


def _close_september(page):
    page.goto(CLOSE + "?month=2026-09")
    btn = page.get_by_test_id("close-month")
    expect(btn).to_be_enabled()
    btn.click()
    expect(page.get_by_test_id("locked-through")).to_have_text(SEP_LOCK)


def test_checklist_blocks_then_close_month(page, seed, login, axe):
    run_script(DRAFT_BILL, "draft bill")
    login(BOARD_EMAIL)
    page.goto(CLOSE + "?month=2026-09")
    expect(page.get_by_test_id("locked-through")).to_have_text("No period is closed yet.")
    for key in ("bank_recs", "draft_bills"):
        expect(_month(page).get_by_test_id(f"check-{key}")).to_contain_text("Not yet")
    expect(_month(page).get_by_test_id("check-draft_bills")).to_contain_text("1 draft bill")
    expect(page.get_by_test_id("close-month")).to_be_disabled()
    axe(page)

    _fix()
    page.reload()
    for key in ("bank_recs", "draft_bills", "trial_balance"):
        expect(_month(page).get_by_test_id(f"check-{key}")).to_contain_text("Done")
    _close_september(page)
    expect(page.get_by_text("Closed. The books are locked through 2026-09-30.")).to_be_visible()
    expect(page.get_by_test_id("close-row")).to_have_count(1)
    axe(page)


def test_rec_before_period_end_does_not_count(page, seed, login):
    """Tightened rule: a completed rec must have statement_date >= the period end."""
    _fix("dt.date(2026, 9, 15)")
    login(BOARD_EMAIL)
    page.goto(CLOSE + "?month=2026-09")
    expect(_month(page).get_by_test_id("check-bank_recs")).to_contain_text("Not yet")
    expect(page.get_by_test_id("close-month")).to_be_disabled()


def test_posting_into_closed_month_refused(page, seed, login):
    _fix()
    login(BOARD_EMAIL)
    _close_september(page)

    page.goto("/money/payments/new/")
    page.locator("#id_unit").select_option(label="Unit G")
    page.locator("#id_amount").fill("350.86")
    page.locator("#id_received_date").fill("2026-09-15")
    page.locator("#id_method").select_option(label="Check")
    page.locator("#id_reference").fill("9001")
    resp_wait = page.expect_response(lambda r: r.request.method == "POST")
    with resp_wait as info:
        page.get_by_role("button", name="Record payment").click()
    assert info.value.status < 500, info.value.status
    expect(page.get_by_text(LOCK_ERR).first).to_be_visible()
    expect(page.get_by_test_id("allocation")).to_have_count(0)

    # Manual ledger adjustment (no free-form journal UI exists; adjustments post entries).
    page.goto("/money/adjustments/")
    page.locator("#id_unit").select_option(label="Unit G")
    page.locator("#id_amount").fill("10.00")
    page.locator("#id_date").fill("2026-09-15")
    for name in ("memo", "reason", "description"):
        f = page.locator(f"#id_{name}")
        if f.count():
            f.fill("E2E closed-month test")
    with page.expect_response(lambda r: r.request.method == "POST") as info:
        page.locator("form[method=post] [type=submit]").last.click()
    assert info.value.status < 500, info.value.status
    expect(page.get_by_text(LOCK_ERR).first).to_be_visible()


def test_reopen_needs_reason(page, seed, login):
    _fix()
    login(BOARD_EMAIL)
    _close_september(page)
    row = page.get_by_test_id("close-row").first
    # Bypass the browser's `required` to prove the server refuses an empty reason.
    row.locator("input[name=reason]").evaluate("el => el.removeAttribute('required')")
    row.locator("input[name=reason]").fill("   ")
    row.get_by_test_id("reopen").click()
    expect(page.get_by_text("A reason is required to reopen a period.")).to_be_visible()
    expect(page.get_by_test_id("locked-through")).to_have_text(SEP_LOCK)

    row = page.get_by_test_id("close-row").first
    row.locator("input[name=reason]").fill("Late utility bill for September")
    row.get_by_test_id("reopen").click()
    # Reopening closed books always needs a second board member.
    change_url = expect_sent_for_approval(page)
    expect(page.get_by_test_id("change-details")).to_contain_text("Late utility bill")
    approve_change(page, change_url)
    page.goto(CLOSE)
    expect(page.get_by_test_id("locked-through")).to_have_text("No period is closed yet.")
    expect(page.get_by_test_id("close-row").first).to_contain_text("Reopened")
    expect(page.get_by_test_id("close-row").first).to_contain_text("Late utility bill")


def _row_vals(page, testid):
    return [_money(t) for t in page.get_by_test_id(testid).locator("td").all_inner_texts()]


def _tb_rows(page, as_of):
    page.goto(f"/money/reports/trial-balance/?as_of={as_of}&basis=accrual")
    href = page.locator('a[data-export="csv"]').first.get_attribute("href")
    resp = page.request.get(page.url.split("?")[0] + href)
    return list(csv.reader(io.StringIO(resp.body().decode("utf-8-sig"))))[1:]


def test_year_end_close(page, seed, login):
    # FY Oct 2025 - Sep 2026 so the seeded Aug-Sep activity falls in a finished year.
    run_script(FISCAL_OCT, "fiscal year")
    _fix()
    login(BOARD_EMAIL)
    is_url = f"{IS}?start=2025-10-01&end=2026-09-30&basis=accrual&fund=all"
    bs_url = f"{BS}?as_of=2026-09-30&basis=accrual&fund=all"
    page.goto(is_url)
    net_before = _row_vals(page, "summary-net_income")
    assert any(net_before), net_before
    page.goto(bs_url)
    fb_before = _row_vals(page, "summary-liab_fb")
    assert any(_row_vals(page, "row-net_income")), "expected unclosed net income before close"
    tb = [r for r in _tb_rows(page, "2026-09-30") if r[0][:1] in "456789"]
    assert tb, "expected income/expense activity before close"

    page.goto(CLOSE + "?year=2025")
    btn = page.get_by_test_id("close-year")
    expect(btn).to_be_enabled()
    btn.click()
    done = re.compile("Year closed. Net income moved into fund balance")
    expect(page.get_by_text(done)).to_be_visible()
    expect(page.get_by_test_id("locked-through")).to_have_text(SEP_LOCK)

    page.goto(is_url)
    assert _row_vals(page, "summary-net_income") == net_before
    page.goto(bs_url)
    assert _row_vals(page, "summary-liab_fb") == fb_before
    nrow = page.get_by_test_id("row-net_income")
    assert nrow.count() == 0 or not any(_row_vals(page, "row-net_income"))
    for r in _tb_rows(page, "2026-09-30"):
        if r[0][:1] in "456789":
            assert r[3] in ("", "0", "0.00") and r[4] in ("", "0", "0.00"), r


def test_owner_cannot_close(page, seed, login):
    _fix()
    login(OWNER_EMAIL)
    resp = page.goto(CLOSE)
    assert resp.status == 403, resp.status
    # A POST is refused regardless of the UI, and not merely for a missing CSRF token.
    token = next((c["value"] for c in page.context.cookies() if c["name"] == "csrftoken"), "")
    posts = ((CLOSE + "month/", {"month": "2026-09"}), (CLOSE + "year/", {"year": "2025"}))
    for path, data in posts:
        headers = {"X-CSRFToken": token, "Referer": page.url}
        r = page.request.post(path, form=data, headers=headers)
        assert r.status == 403, (path, r.status)
        assert "CSRF verification failed" not in r.text(), (path, "CSRF refusal")


def test_secretary_proposes_close_treasurer_approves(page, seed, login):
    """Other board members close the books through a change request."""
    _fix()
    login(SECRETARY_EMAIL)
    page.goto(CLOSE + "?month=2026-09")
    page.get_by_test_id("close-month").click()
    change_url = expect_sent_for_approval(page)
    page.goto(CLOSE)
    expect(page.get_by_test_id("locked-through")).to_have_text("No period is closed yet.")
    approve_change(page, change_url, BOARD_EMAIL)
    page.goto(CLOSE)
    expect(page.get_by_test_id("locked-through")).to_have_text(SEP_LOCK)
