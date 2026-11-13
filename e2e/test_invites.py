"""Invitation lifecycle on the unit page (draft, send, resend, revoke, badges)."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.m2 import residents_enabled

NAME = "Priya Raman"  # owner of unit C in the seed


# Seed owners lack e-delivery consent, so send/resend may warn "no email went".
def _unit_page(page) -> None:
    resp = page.goto("/units/C/")
    if not (resp and resp.status == 200):
        page.goto("/units/3/")


def _access(page):
    return page.get_by_test_id("resident-access")


def _badge(page):
    row = _access(page).locator("div", has_text=NAME).last
    return row.get_by_test_id("invitation-status")


def _draft_and_send(page) -> None:
    _access(page).get_by_role("button", name=f"Draft invitation for {NAME}").click()
    expect(page.get_by_text(f"Invitation drafted for {NAME}.")).to_be_visible()
    expect(_badge(page)).to_have_text("Draft")
    _access(page).get_by_role("button", name="Invite drafted residents").click()
    expect(page.get_by_text(re.compile(r"Invitation sent to |Marked as invited"))).to_be_visible()
    expect(_badge(page)).to_have_text("Sent")
    expect(_access(page).get_by_text(re.compile(r"expires \w{3} \d{1,2}, \d{4}"))).to_be_visible()


def test_invite_send_resend_revoke(page, seed, login, axe):
    residents_enabled(True)
    login(BOARD_EMAIL)
    _unit_page(page)
    _draft_and_send(page)
    axe(page)
    _access(page).get_by_role("button", name=f"Resend invitation to {NAME}").click()
    resent = re.compile(r"Invitation resent to |no e-delivery consent")
    expect(page.get_by_text(resent)).to_be_visible()
    expect(_badge(page)).to_have_text("Sent")
    _access(page).get_by_role("button", name=f"Revoke invitation for {NAME}").click()
    expect(page.get_by_text(f"Invitation for {NAME} revoked.")).to_be_visible()
    expect(_badge(page)).to_have_text("Revoked")
    resend = _access(page).get_by_role("button", name=f"Resend invitation to {NAME}")
    expect(resend).to_have_count(0)
    # A revoked invitation can be drafted again.
    expect(_access(page).get_by_role("button", name=f"Draft invitation for {NAME}")).to_be_enabled()
    axe(page)


def test_invite_resend_held_while_residents_off(page, seed, login, axe):
    residents_enabled(True)
    login(BOARD_EMAIL)
    _unit_page(page)
    _draft_and_send(page)
    residents_enabled(False)
    page.reload()
    expect(_access(page).get_by_test_id("invites-held")).to_be_visible()
    resend = _access(page).get_by_role("button", name=f"Resend invitation to {NAME}")
    expect(resend).to_be_disabled()
    axe(page)
    _access(page).get_by_role("button", name=f"Revoke invitation for {NAME}").click()
    expect(page.get_by_text(f"Invitation for {NAME} revoked.")).to_be_visible()
    expect(_badge(page)).to_have_text("Revoked")
