"""Axe scans of the letter template pages under /settings/letters/."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL


def test_letters_pages_axe(page, seed, login, axe):
    login(BOARD_EMAIL)
    resp = page.goto("/settings/letters/")
    assert resp and resp.status == 200, f"/settings/letters/ returned {resp and resp.status}"
    axe(page)
    page.get_by_role("link", name="New template").click()
    expect(page).to_have_url(re.compile(r"/settings/letters/new/"))
    axe(page)
    page.goto("/settings/letters/")
    first = page.locator("table a").first
    if first.count():
        first.click()
        expect(page).to_have_url(re.compile(r"/settings/letters/\d+/"))
        axe(page)
