"""axe scans of the board pages that exist on main. Add a path to PAGES to cover it."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL
from helpers import seed as seed_helpers

UNIT = "G"  # seeded by seed_demo
PAGES = [
    "/",
    "/search/?q=Unit",
    "/money/",
    "/units/",
    f"/units/{UNIT}/",
    "/units/people/",
    "/community/directory/",
    "/money/reports/trial-balance/",
    "/money/reports/gl/",
    "/settings/roles/",
    "/settings/roles/go-live/",
    "/settings/plugins/",
    "/settings/rules/",
    "/settings/rules/law-changes/",
    "/settings/setup/",
    "/settings/setup/features/",
    "/settings/setup/money/",
    "/settings/setup/enforcement/",
    "/settings/audit/",
    "/settings/jobs/",
    "/governance/compliance/",
]


@pytest.fixture
def a11y_unit(seed):
    """The Cedar Row seed has units A-I."""


@pytest.mark.parametrize("path", PAGES)
def test_board_page_axe(page, a11y_unit, login, axe, path):
    login(BOARD_EMAIL)
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"
    assert page.url.endswith(path), f"{path} redirected to {page.url}"
    axe(page)


def test_wide_table_scrolls_by_keyboard_on_a_narrow_screen(page, a11y_unit, login, axe):
    """A table wider than the screen scrolls sideways; its wrapper is then a tab stop."""
    login(BOARD_EMAIL)
    page.set_viewport_size({"width": 700, "height": 800})
    page.goto("/settings/audit/")
    wrap = page.locator(".table-wrap").first
    assert wrap.evaluate("el => el.scrollWidth > el.clientWidth")
    expect(wrap).to_have_attribute("tabindex", "0")
    axe(page)
    page.set_viewport_size({"width": 2400, "height": 800})
    expect(wrap).not_to_have_attribute("tabindex", "0")


# ---------------------------------------------------------------- bank pages
@pytest.fixture
def bank_seed(a11y_unit):
    """One bank account with one imported CSV batch; treasurer has title=treasurer."""
    return seed_helpers.bank()


@pytest.mark.parametrize("kind", ["index", "batch", "account_edit", "account_new", "reserve"])
def test_bank_page_axe(page, bank_seed, login, axe, kind):
    path = {
        "index": "/money/bank/",
        "batch": f"/money/bank/imports/{bank_seed['batch']}/",
        "account_edit": f"/money/bank/accounts/{bank_seed['acct']}/",
        "account_new": "/money/bank/accounts/new/",
        "reserve": "/money/bank/reserve/",
    }[kind]
    login(BOARD_EMAIL)
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"
    axe(page)


def test_roles_money_access_card_axe(page, a11y_unit, login, axe):
    login(BOARD_EMAIL)
    page.goto("/settings/roles/")
    card = page.locator("section, div.card").filter(has=page.locator("#money-h")).last
    expect(card).to_contain_text(BOARD_EMAIL)
    expect(card).to_contain_text("Treasurer")
    expect(
        page.locator("#money-h ~ * .callout.warn, [role=alert]").filter(has_text="No Treasurer")
    ).to_have_count(0)
    axe(page)


def test_simplefin_plugin_page_axe(page, bank_seed, login, axe):
    """Enable the plugin, connect with the fake bridge's setup token, scan each state."""
    from homeoa_simplefin.fake_server import SETUP_TOKEN

    login(BOARD_EMAIL)
    resp = page.goto("/settings/plugins/simplefin/")
    assert resp and resp.status == 200, f"returned {resp and resp.status}"
    axe(page)
    page.get_by_label(re.compile("enabled", re.IGNORECASE)).check()
    page.get_by_role("button", name=re.compile("save", re.IGNORECASE)).click()

    page.goto("/plugins/simplefin/")
    axe(page)
    # seed_demo connects the fake bridge already; connect only when it did not.
    if "not connected" in page.get_by_test_id("simplefin-status").inner_text():
        page.get_by_label(re.compile("token", re.IGNORECASE)).fill(SETUP_TOKEN)
        page.get_by_role("button", name="Connect").click()
    expect(page.get_by_test_id("simplefin-status")).to_contain_text("Connected")
    expect(page.get_by_test_id("simplefin-account").first).to_be_visible()
    axe(page)

    page.goto("/money/bank/")
    expect(page.get_by_test_id("bank-feed")).to_be_visible()
    axe(page)


# ---------------------------------------------------------------- public pages, PWA
@pytest.mark.parametrize("path", ["/login/", "/offline/"])
def test_public_page_axe(page, a11y_unit, axe, path):
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"
    axe(page)


def test_pwa_installable_bits(page, a11y_unit, login):
    login(BOARD_EMAIL)
    page.goto("/")
    href = page.locator("link[rel=manifest]").get_attribute("href")
    manifest = page.request.get(href).json()
    assert manifest["display"] == "standalone"
    scope = page.evaluate("() => navigator.serviceWorker.ready.then(r => r.scope)")
    assert scope.endswith("/")
    # The SW caches only the offline page and its stylesheet, never the signed-in pages.
    cached = page.evaluate(
        """async () => {
            const urls = [];
            for (const k of await caches.keys()) {
                const c = await caches.open(k);
                for (const r of await c.keys()) urls.push(new URL(r.url).pathname);
            }
            return urls;
        }"""
    )
    assert "/offline/" in cached
    assert "/" not in cached and not any(u.startswith("/money") for u in cached)


def test_skip_link_keyboard(page, a11y_unit, login):
    login(BOARD_EMAIL)
    page.goto("/money/")
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to content")
    expect(skip).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#main")).to_be_focused()


# ---------------------------------------------------------------- meeting records
@pytest.mark.parametrize("sub", ["attendance/", "minutes/", "annual/"])
def test_meeting_records_axe(page, a11y_unit, login, axe, sub):
    login(SECRETARY_EMAIL)
    page.goto("/governance/meetings/")
    page.get_by_label("Kind").select_option(label="Annual owners meeting")
    page.get_by_label("Date").fill("2026-11-20")
    page.get_by_role("button", name="Schedule meeting").click()
    expect(page.get_by_test_id("meeting-status")).to_have_text("Draft")
    resp = page.goto(page.url.rstrip("/") + "/" + sub)
    assert resp and resp.status == 200, f"{sub} returned {resp and resp.status}"
    axe(page)
