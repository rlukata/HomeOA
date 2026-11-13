"""Scenario 0b: board previews the resident portal as a unit; invitations held."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers


@pytest.fixture
def billed(seed):
    return seed_helpers.billing()


def _hub_links(page):
    # The tab bar is phone-only (hidden by CSS on desktop), so read it from the DOM.
    return [a.get_attribute("href") for a in page.locator('nav[aria-label="Hubs"] a').all()]


def test_preview_as_unit_with_invites_held(page, billed, login, axe):
    login(BOARD_EMAIL)
    u = "C"
    page.goto(f"/units/{u}/")
    access = page.get_by_test_id("resident-access")
    expect(access.get_by_test_id("invites-held")).to_be_visible()
    expect(access.get_by_role("button", name="Invite drafted residents")).to_be_disabled()
    axe(page)

    access.get_by_role("link", name=f"Preview as unit {u}").click()
    expect(page).to_have_url(re.compile(rf"/units/{u}/preview/$"))
    banner = page.get_by_test_id("preview-banner")
    expect(banner).to_contain_text(f"Preview of Unit {u} as its owner")
    expect(page.get_by_test_id("balance")).to_have_text("$396.63")  # unit C
    # Read-only: no pay action anywhere.
    expect(page.get_by_role("button", name=re.compile(r"^Pay", re.I))).to_have_count(0)
    expect(page.get_by_role("link", name=re.compile(r"^Pay \$", re.I))).to_have_count(0)
    axe(page)

    hrefs = _hub_links(page)
    assert hrefs, "preview shows no hub tabs"
    for href in hrefs:
        assert f"/units/{u}/preview/" in href, f"tab leaves preview: {href}"
        resp = page.goto(href)
        assert resp and resp.status == 200, f"{href} returned {resp and resp.status}"
        expect(page.get_by_test_id("preview-banner")).to_be_visible()
        axe(page)

    page.get_by_role("link", name="Exit preview").click()
    expect(page).not_to_have_url(re.compile(r"/preview/"))


def test_preview_as_tenant_has_no_money_tabs(page, billed, login, axe):
    login(BOARD_EMAIL)
    u = "C"
    page.goto(f"/units/{u}/preview/")
    owner_tabs = len(_hub_links(page))
    page.goto(f"/units/{u}/preview/?as=tenant")
    expect(page.get_by_test_id("preview-banner")).to_contain_text("as its tenant")
    tenant_hrefs = _hub_links(page)
    assert len(tenant_hrefs) < owner_tabs, "tenant preview should drop money tabs"
    assert all("as=tenant" in h for h in tenant_hrefs)
    axe(page)
