"""Core reports (Money > Reports): trial balance balances, cash basis differs
from accrual by open AR/AP, AR aging buckets on the fixed clock (2026-10-07),
AP aging by vendor, owners refused. Also the general ledger in both bases."""

import re
from decimal import Decimal

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import seed as seed_helpers
from test_bills import MOW, _fill

REPORTS = "/money/reports/"
TB = REPORTS + "trial-balance/"
GL = REPORTS + "gl/"
AR_AGING = REPORTS + "ar-aging/"
AP_AGING = REPORTS + "ap-aging/"


def _money(text: str) -> Decimal:
    return Decimal(re.sub(r"[^\d.\-]", "", text) or "0")


def _tb_totals(page):
    cells = page.locator("tfoot tr").first.locator("td")
    d, c = (_money(cells.nth(i).inner_text()) for i in (0, 1))
    assert _money(page.get_by_test_id("tb-difference").inner_text()) == 0
    return d, c


@pytest.fixture
def billed(seed):
    seed_helpers.billing()


def test_trial_balance_balances_both_bases(page, billed, login, axe):
    login(BOARD_EMAIL)
    _fill(page, "INV-RPT", (MOW,))
    page.get_by_test_id("bill-save").click()
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).not_to_have_text("Draft")

    page.goto(TB + "?as_of=2026-10-07&basis=accrual")
    acc = _tb_totals(page)
    assert acc[0] == acc[1] and acc[0] > 0, acc
    axe(page)
    page.goto(TB + "?as_of=2026-10-07&basis=cash")
    cash = _tb_totals(page)
    assert cash[0] == cash[1], cash
    assert cash != acc  # cash view drops open AR and the unpaid bill
    expect(page.get_by_test_id("tb-basis")).to_contain_text("Cash basis")
    for name in ("Accounts payable", "Accounts receivable", "Prepaid"):  # strict cash basis
        expect(page.locator("tbody").get_by_text(re.compile(name, re.I))).to_have_count(0)


def test_gl_both_bases(page, billed, login, axe):
    login(BOARD_EMAIL)
    page.goto(GL + "?start=2026-01-01&end=2026-10-07&basis=accrual")
    acc_rows = page.locator("tbody tr").count()
    assert acc_rows > 0
    axe(page)
    page.goto(GL + "?start=2026-01-01&end=2026-10-07&basis=cash")
    expect(page.get_by_text(re.compile("Cash basis"))).to_be_visible()
    assert page.locator("tbody tr").count() < acc_rows  # unpaid assessments drop out


def test_ar_aging_buckets(page, billed, login, axe):
    login(BOARD_EMAIL)
    page.goto(AR_AGING + "?as_of=2026-10-07")
    for b in ("Current", "1-30 days", "31-60 days", "61-90 days", "Over 90 days"):
        expect(page.get_by_role("columnheader", name=re.compile(re.escape(b)))).to_be_visible()
    total = page.get_by_test_id("aging-total")
    expect(total).to_be_visible()
    assert _money(total.inner_text()) > 0
    axe(page)


def test_ap_aging_by_vendor(page, seed, login):
    login(BOARD_EMAIL)
    _fill(page, "INV-AP1", (MOW,))
    page.get_by_test_id("bill-save").click()
    page.get_by_test_id("bill-submit").click()
    page.goto(AP_AGING + "?as_of=2026-10-07")
    row = page.locator(
        "tbody tr", has=page.get_by_role("rowheader", name=re.compile("Greenway", re.I))
    )
    expect(row).to_contain_text("485.00")
    expect(page.get_by_test_id("aging-total")).to_contain_text("485.00")


def test_owner_refused(page, seed, login):
    login(OWNER_EMAIL)
    resp = page.goto(TB)
    assert resp.status in (403, 404) or "/me" in page.url
