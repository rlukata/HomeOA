"""axe scans of the late-fee and special-assessment pages (/money/assessments/)."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers

BASE = "/money/assessments/"


@pytest.fixture
def billed(seed):
    return seed_helpers.billing()


def _goto_ok(page, path):
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"


@pytest.mark.parametrize(
    "sub", ["late-fees/", "late-fees/policies/new/", "special/", "special/new/"]
)
def test_latefee_page_axe(page, billed, login, axe, sub):
    login(BOARD_EMAIL)
    _goto_ok(page, BASE + sub)
    axe(page)


def test_special_detail_axe(page, billed, login, axe):
    login(BOARD_EMAIL)
    _goto_ok(page, BASE + "special/new/")
    page.locator("#id_name").fill("Roof repair")
    page.locator("#id_total").fill("11000.00")
    page.locator("#id_installments").fill("2")
    page.locator("#id_first_due").fill("2026-11-01")
    # A special assessment cites the board resolution that approves it; a seeded adopted
    # one applies it at once (the full resolution flow is in test_budget).
    page.locator("#id_change_resolution").select_option(index=1)
    page.locator("#id_name").locator("xpath=ancestor::form").locator(
        "button[type=submit]"
    ).first.click()
    expect(page).to_have_url(re.compile(r"/money/assessments/special/\d+/$"))
    expect(page.get_by_test_id("special-ratification")).to_be_visible()
    axe(page)
