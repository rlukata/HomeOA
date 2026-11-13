"""Login, MFA, RBAC."""

import re
import time

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL, manage
from helpers.auth import SIGN_IN_BUTTON, open_link

HUBS = ["Home", "Money", "Requests", "Community", "Governance", "Units", "Vendors", "Documents"]


def request_link(page, email):
    page.goto("/login/")
    page.get_by_label(re.compile("email", re.IGNORECASE)).fill(email)
    page.get_by_role("button", name=re.compile("send|email me|continue", re.IGNORECASE)).click()


def test_magic_link_login(page, seed, mail):
    request_link(page, BOARD_EMAIL)
    expect(page.get_by_text(re.compile("check your email", re.IGNORECASE))).to_be_visible()
    link = mail.wait_for(BOARD_EMAIL).first_link()

    # A GET alone shows the confirm page and must not burn the link.
    page.goto(link)
    expect(page.get_by_role("button", name=SIGN_IN_BUTTON)).to_be_visible()
    page.context.clear_cookies()
    page.goto(link)
    page.get_by_role("button", name=SIGN_IN_BUTTON).click()
    nav = page.get_by_role("navigation", name="Main")
    for hub in HUBS:
        expect(nav.get_by_role("link", name=hub, exact=True)).to_be_attached()

    # Reuse fails with a friendly message.
    page.context.clear_cookies()
    open_link(page, link)
    expect(
        page.get_by_text(re.compile("already been used|no longer valid|expired", re.IGNORECASE))
    ).to_be_visible()

    # Older than 15 minutes fails (age the token).
    mail.clear()
    request_link(page, BOARD_EMAIL)
    old = mail.wait_for(BOARD_EMAIL).first_link()
    manage("e2e_age_magic_links", "--minutes", "16")
    page.context.clear_cookies()
    open_link(page, old)
    expect(page.get_by_text(re.compile("expired", re.IGNORECASE))).to_be_visible()


def require_mfa_for_board(page):
    page.goto("/settings/roles/")
    box = page.get_by_label(re.compile("require MFA for board", re.IGNORECASE))
    box.check()
    page.locator("form").filter(has=box).get_by_role("button", name="Save").click()


def test_mfa_required_for_board(page, browser, base_url, seed, login):
    pyotp = pytest.importorskip("pyotp")
    login(BOARD_EMAIL)
    require_mfa_for_board(page)

    # The (unenrolled) board user is forced to enroll on next sign-in.
    # The seeded secretary enrols, so the treasurer stays unenrolled.
    ctx = browser.new_context(base_url=base_url)
    p2 = ctx.new_page()
    login(SECRETARY_EMAIL, p2)
    expect(p2.get_by_role("heading", name="Set up two-step sign-in")).to_be_visible()
    expect(p2.get_by_role("link", name="Not now")).to_have_count(0)
    secret = "".join(p2.get_by_test_id("totp-secret").inner_text().split())
    p2.get_by_label(re.compile("code", re.IGNORECASE)).fill(pyotp.TOTP(secret).now())
    p2.get_by_role("button", name=re.compile("verify", re.IGNORECASE)).click()
    expect(p2.get_by_role("heading", name="Recovery codes")).to_be_visible()

    # Next sign-in asks for a TOTP code, which is accepted.
    # Codes can't be replayed, so wait for the next 30 s TOTP step.
    p2.wait_for_timeout((30 - time.time() % 30 + 1) * 1000)
    ctx.clear_cookies()
    login(SECRETARY_EMAIL, p2)
    p2.get_by_label(re.compile("code", re.IGNORECASE)).fill(pyotp.TOTP(secret).now())
    p2.get_by_role("button", name=re.compile("verify|continue|sign in", re.IGNORECASE)).click()
    expect(p2.get_by_role("navigation", name="Main")).to_be_visible()
    ctx.close()


def test_mfa_not_required_for_owner(page, browser, base_url, seed, login):
    login(BOARD_EMAIL)
    require_mfa_for_board(page)
    ctx = browser.new_context(base_url=base_url)
    p3 = ctx.new_page()
    login(OWNER_EMAIL, p3)
    expect(p3.get_by_role("heading", name="Set up two-step sign-in")).to_have_count(0)
    ctx.close()


def test_rbac_hub_visibility(page, browser, base_url, seed, login):
    login(BOARD_EMAIL)
    page.goto("/")
    nav = page.get_by_role("navigation", name="Main")
    for hub in HUBS:
        expect(nav.get_by_role("link", name=hub, exact=True)).to_be_attached()

    ctx = browser.new_context(base_url=base_url)
    owner = ctx.new_page()
    login(OWNER_EMAIL, owner)
    owner.goto("/")
    expect(owner).to_have_url(re.compile(r"/me/"))  # residents land on /me/
    resp = owner.goto("/money/")
    assert resp.status == 403
    resp = owner.goto("/units/")
    assert resp.status == 403
    ctx.close()

    ctx = browser.new_context(base_url=base_url)
    tenant = ctx.new_page()
    login(TENANT_EMAIL, tenant)
    tenant.goto("/")
    expect(tenant.get_by_role("link", name="Money", exact=True)).to_have_count(0)
    ctx.close()
