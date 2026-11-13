"""axe scans of the bank matching and reconciliation pages (/money/bank/...)."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers


@pytest.fixture
def bank_seed(seed):
    return seed_helpers.bank()


def _goto_ok(page, path):
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"


@pytest.mark.parametrize("kind", ["txn", "categorize", "rules", "reconcile"])
def test_matching_page_axe(page, bank_seed, login, axe, kind):
    path = {
        "txn": f"/money/bank/txn/{bank_seed['txn']}/",
        "categorize": f"/money/bank/txn/{bank_seed['txn']}/categorize/",
        "rules": "/money/bank/rules/",
        "reconcile": "/money/bank/reconcile/",
    }[kind]
    login(BOARD_EMAIL)
    _goto_ok(page, path)
    axe(page)


def test_reconciliation_page_axe(page, bank_seed, login, axe):
    login(BOARD_EMAIL)
    _goto_ok(page, "/money/bank/reconcile/")
    page.locator("#id_statement_date").fill("2026-10-31")
    page.locator("#id_ending_balance").fill("0.00")
    page.locator("#id_beginning_balance").fill("0.00")
    page.locator("#id_statement_date").locator("xpath=ancestor::form").locator(
        "button[type=submit]"
    ).first.click()
    expect(page).to_have_url(re.compile(r"/money/bank/reconcile/\d+/$"))
    axe(page)
