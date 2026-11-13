"""Passkey add, sign-in and recovery codes, using Chromium's CDP virtual
authenticator. WebAuthn rejects IP-address RP IDs, so this test drives `localhost`."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL


@pytest.fixture
def lh_page(browser, base_url):
    url = base_url.replace("127.0.0.1", "localhost")
    ctx = browser.new_context(base_url=url)
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    yield page, url
    ctx.close()


def test_passkey_add_and_login(lh_page, seed, axe):
    from helpers.auth import login_as

    page, url = lh_page
    login_as(page, url, BOARD_EMAIL)
    page.goto("/account/passkeys/")
    page.get_by_label("Name for the new passkey").fill("Test key")
    page.get_by_test_id("passkey-add").click()
    codes = page.get_by_test_id("passkey-recovery-codes")
    expect(codes).to_be_visible()
    assert codes.locator("li").count() >= 5
    expect(page.get_by_test_id("passkey-list")).to_contain_text("Test key")
    axe(page)

    # Sign out (fresh cookies), then sign in with the passkey alone.
    page.context.clear_cookies()
    page.goto("/login/")
    page.get_by_test_id("passkey-login").click()
    expect(page).not_to_have_url(re.compile(r"/login/"))
    resp = page.goto("/money/")
    assert resp and resp.status == 200
