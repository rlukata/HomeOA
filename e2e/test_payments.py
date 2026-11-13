"""Record payments (scenario) and axe scans of payments, adjustments, imports."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers
from helpers.flows import record_payment

APPFOLIO = Path(__file__).parent / "fixtures" / "appfolio" / "base"


@pytest.fixture
def billed(seed):
    """seed_demo (Aug-Sep) plus October 2026 dues posted."""
    return seed_helpers.billing()


def test_check_payments_pay_october_and_overpay_to_credit(page, billed, login, axe):
    login(BOARD_EMAIL)
    # seed_demo: unit G still owes September, so the first check pays September.
    alloc = record_payment(page, "G", "350.86", "1041")
    expect(alloc).to_contain_text("September")
    alloc = record_payment(page, "G", "350.86", "1042")
    expect(alloc).to_contain_text("October")
    expect(alloc.locator("tfoot")).to_contain_text("$0.00")
    expect(page.get_by_test_id("unit-balance")).to_have_text("$0.00")
    axe(page)

    # seed_demo: unit H already has a $50.00 credit; paying October's $350.86 keeps it.
    alloc = record_payment(page, "H", "350.86", "2210")
    expect(page.get_by_test_id("unit-balance")).to_have_text(re.compile(r"^(−|-)\$50\.00$"))
    page.get_by_role("link", name="Open payment").click()
    expect(page).to_have_url(re.compile(r"/money/payments/\d+/$"))
    axe(page)


@pytest.mark.parametrize(
    "path", ["/money/payments/", "/money/payments/new/", "/money/adjustments/"]
)
def test_payments_pages_axe(page, billed, login, axe, path):
    login(BOARD_EMAIL)
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"
    axe(page)


def test_import_pages_axe(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/units/import/")
    axe(page)
    page.get_by_label(re.compile("CSV files", re.IGNORECASE)).set_input_files(
        sorted(APPFOLIO.glob("*.csv"))
    )
    page.get_by_role("button", name=re.compile("upload|next", re.IGNORECASE)).click()
    expect(page).to_have_url(re.compile(r"/units/import/\d+/"))
    axe(page)
    run = re.search(r"/units/import/(\d+)/", page.url).group(1)
    resp = page.goto(f"/units/import/{run}/mapping/")
    assert resp and resp.status == 200, f"mapping returned {resp and resp.status}"
    axe(page)
