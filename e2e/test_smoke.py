"""Smoke: health endpoint, board shell, phone tabs, axe."""

import re
import urllib.request

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL

HUBS = ["Home", "Money", "Requests", "Community", "Governance", "Units", "Vendors", "Documents"]


def test_healthz(live_server_url):
    with urllib.request.urlopen(live_server_url + "/healthz") as r:
        assert r.status == 200


def test_board_shell_hubs(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/")
    nav = page.get_by_role("navigation", name="Main")
    for hub in HUBS:
        expect(nav.get_by_role("link", name=hub, exact=True)).to_be_attached()
    axe(page)


@pytest.mark.phone
def test_phone_tabs_and_more_sheet(phone_page, seed, login, axe):
    page = phone_page
    login(BOARD_EMAIL, page)
    page.goto("/")
    tabs = page.get_by_role("navigation", name="Hubs")
    for t in ("Home", "Money", "Requests", "Units", "More"):
        expect(tabs.get_by_text(t, exact=True)).to_be_visible()
    axe(page)  # sheet closed
    tabs.get_by_text("More", exact=True).click()
    sheet = page.get_by_role("dialog", name=re.compile("^More$"))
    expect(sheet).to_be_visible()
    for hub in ("Community", "Governance", "Vendors", "Documents"):
        expect(sheet.get_by_role("link", name=hub)).to_be_visible()
    axe(page)
