"""Financial statements (Money > Reports): income statement, balance sheet and cash
flow render on accrual and cash with every funds-filter option; the balance sheet balances
in every column; the per-fund cash note shows only on cash per-fund views (by fund or one
fund); owners refused."""

import re
from decimal import Decimal

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import seed as seed_helpers
from test_bills import MOW, _fill

R = "/money/reports/"
IS, BS, CF = R + "income-statement/", R + "balance-sheet/", R + "cash-flow/"
FUNDS = ("", "all")  # by fund (per-fund columns), all funds total only


def _money(text: str) -> Decimal:
    t = text.strip()
    neg = t.startswith("(") or t.startswith("-") or "−" in t
    v = Decimal(re.sub(r"[^\d.]", "", t) or "0")
    return -v if neg else v


def _row(page, label):
    cells = page.locator("tr", has=page.get_by_role("rowheader", name=label, exact=True))
    return [_money(t) for t in cells.first.locator("td").all_inner_texts()]


@pytest.fixture
def billed(seed):
    seed_helpers.billing()


@pytest.mark.parametrize("basis", ["accrual", "cash"])
def test_statements_render_and_balance(page, billed, login, axe, basis):
    login(BOARD_EMAIL)
    _fill(page, "INV-ST", (MOW,))
    page.get_by_test_id("bill-save").click()
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).not_to_have_text("Draft")

    for url in (IS, BS, CF):
        for fund in FUNDS:
            resp = page.goto(f"{url}?basis={basis}&fund={fund}")
            assert resp.status == 200, (url, fund, resp.status)
            expect(page.get_by_test_id("statement")).to_be_visible()
            label = "Cash basis" if basis == "cash" else "Accrual basis"
            expect(page.get_by_test_id("statement-basis")).to_contain_text(label)
            note = page.get_by_test_id("cash-fund-note")
            if basis == "cash" and fund != "all":
                expect(note).to_be_visible()
            else:
                expect(note).to_have_count(0)
            heads = page.locator("[data-testid=statement] thead th").all_inner_texts()
            if fund == "":
                assert "Total" in heads and len(heads) >= 3, (url, heads)  # funds plus total
            else:
                assert "All funds" in heads and "Total" not in heads, (url, heads)
            if url == BS:
                assets = _row(page, "Total assets")
                both = [
                    _money(t)
                    for t in page.get_by_test_id("summary-liab_fb").locator("td").all_inner_texts()
                ]
                assert assets == both, (basis, fund, assets, both)
                if basis == "accrual":
                    assert any(v != 0 for v in assets), assets
    page.goto(f"{BS}?basis={basis}")
    axe(page)


def test_single_fund_filter(page, billed, login):
    login(BOARD_EMAIL)
    page.goto(BS + "?basis=cash")
    opts = page.locator("select[name=fund] option")
    codes = [
        v
        for v in (opts.nth(i).get_attribute("value") for i in range(opts.count()))
        if v not in FUNDS
    ]
    assert codes, "no single-fund option"
    for code in codes:
        page.goto(f"{BS}?basis=cash&fund={code}")
        expect(page.get_by_test_id("cash-fund-note")).to_be_visible()  # per-fund view
        page.goto(f"{BS}?basis=accrual&fund={code}")
        expect(page.get_by_test_id("cash-fund-note")).to_have_count(0)
        assert _row(page, "Total assets") == [
            _money(t)
            for t in page.get_by_test_id("summary-liab_fb").locator("td").all_inner_texts()
        ]


@pytest.mark.parametrize("url", [IS, BS, CF])
def test_owner_refused(page, seed, login, url):
    login(OWNER_EMAIL)
    resp = page.goto(url)
    assert resp.status == 403, (url, resp.status, page.url)
