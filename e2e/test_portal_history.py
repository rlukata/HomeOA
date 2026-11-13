"""Owner payment history, statements tab and profile (POR-03, POR-05, REG-04)."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, TENANT_EMAIL
from helpers import m2
from helpers import seed as seed_helpers


@pytest.fixture
def residents_on(seed):
    seed_helpers.billing()
    m2.residents_enabled(True)


def test_owner_payments_history(page, residents_on, login, axe):
    login(m2.OWNERC_EMAIL)
    resp = page.goto("/me/payments/")
    assert resp and resp.status == 200, f"/me/payments/ returned {resp and resp.status}"
    expect(page.get_by_test_id("payments-list")).to_be_visible()
    expect(page.get_by_test_id("ledger")).to_contain_text("October", ignore_case=True)
    axe(page)


def test_owner_statements_tab(page, residents_on, login, axe):
    login(m2.OWNERC_EMAIL)
    page.goto("/me/statements/")
    expect(page.get_by_test_id("statements-list")).to_contain_text("No statements yet.")
    axe(page)

    page.context.clear_cookies()
    login(BOARD_EMAIL)
    page.goto("/money/statements/")
    page.get_by_role("button", name="Generate statements for October 2026").click()
    expect(page).to_have_url(re.compile(r"/money/statements/\d+/$"))
    row_c = page.get_by_test_id("statements").locator('tr[data-unit="C"]')
    board_pdf = row_c.locator("a[href$='.pdf']").first.get_attribute("href")
    pk = re.search(r"/(\d+)\.pdf$", board_pdf).group(1)

    # Residents see only SENT statements; previews stay hidden (Manager ruling).
    page.context.clear_cookies()
    login(m2.OWNERC_EMAIL)
    page.goto("/me/statements/")
    lst = page.get_by_test_id("statements-list")
    expect(lst).to_contain_text("No statements yet.")
    expect(lst.get_by_role("link")).to_have_count(0)
    pdf = page.request.get(f"/me/statements/{pk}/pdf/")
    assert pdf.status == 404, f"preview PDF returned {pdf.status}"
    axe(page)


def test_owner_profile_consent(page, residents_on, login, axe):
    login(m2.OWNERC_EMAIL)
    resp = page.goto("/me/profile/")
    assert resp and resp.status == 200, f"/me/profile/ returned {resp and resp.status}"
    for tid in ("contact", "household", "consent", "preferences", "consent-draft"):
        expect(page.get_by_test_id(tid)).to_be_visible()
    axe(page)

    page.locator("#agree").check()
    page.get_by_test_id("consent-give").click()
    expect(page.get_by_text("You get official notices by email.")).to_be_visible()
    expect(page.get_by_test_id("consent-at")).to_have_text(re.compile(r"\w{3} \d{1,2}, \d{4}"))
    axe(page)

    page.get_by_test_id("consent-withdraw").click()
    expect(page.get_by_text("You get official notices by postal mail.")).to_be_visible()


def test_tenant_blocked_from_owner_finance(page, residents_on, login):
    login(TENANT_EMAIL)
    for path in ("/me/payments/", "/me/statements/"):
        resp = page.goto(path)
        assert resp and resp.status == 403, f"{path} returned {resp and resp.status}"
