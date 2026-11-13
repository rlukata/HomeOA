"""Statutory notices of delinquency on the WA pack (RCW 64.34.364(17)).

Fixed clock 2026-10-07, residents off. Unit G is 27 days late; the first notice is due
by Oct 10, so the ladder is run for that date with run_scheduled (fakes only).
"""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2
from helpers import seed as seed_helpers
from helpers.flows import propose_and_approve


@pytest.fixture
def billed(seed):
    return seed_helpers.billing()


def _row(page):
    return page.get_by_test_id("delinquency-cases").locator('tr[data-unit="G"]')


def test_statutory_first_notice_timeline_standstill(page, billed, login, mail, axe):
    login(BOARD_EMAIL)
    page.goto("/money/delinquency/")
    expect(page.get_by_test_id("statutory-mode")).to_contain_text("first notice replaces")
    expect(page.get_by_role("note").filter(has_text="contacts are not filled in")).to_be_visible()
    page.get_by_role("button", name="Check the ladder now (Oct 7, 2026)").click()
    row = _row(page)
    expect(row.get_by_test_id("first-notice-deadline")).to_have_text(
        "First notice due by Oct 10, 2026"
    )
    expect(row).to_contain_text("First notice of delinquency")
    axe(page)

    m2.run_scheduled("2026-10-10")
    page.reload()
    row = _row(page)
    records = row.get_by_test_id("preforeclosure-records")
    expect(records).to_have_count(1)
    # One first-class letter per owner address, plus an email copy when known.
    expect(records.locator("li").filter(has_text=re.compile(r"mail", re.I)).first).to_be_visible()
    expect(records.locator("li").filter(has_text="@").first).to_be_visible()
    expect(records.get_by_role("link", name="PDF").first).to_be_visible()
    expect(row.get_by_text("Assistance contacts missing")).to_be_visible()
    expect(row.get_by_test_id("standstill")).to_contain_text(
        re.compile(r"Standstill until \w{3} \d")
    )
    expect(row.get_by_test_id("first-notice-deadline")).to_have_count(1)
    axe(page)
    assert mail.messages() == []  # residents off: previews and draft batches only

    pdf = page.request.get(records.get_by_role("link", name="PDF").first.get_attribute("href"))
    assert pdf.status == 200 and pdf.headers["content-type"].startswith("application/pdf")


def test_statutory_text_locked_and_contacts(page, billed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/settings/delinquency/")
    block = page.get_by_test_id("statutory-notice")
    expect(block).to_contain_text("RCW 64.34.364(17)(a)(iii)")
    expect(block).to_contain_text("cannot be edited")
    expect(block).to_contain_text("A new version of the text applies from Jan 1, 2028")
    expect(block.locator("pre")).to_contain_text("THIS IS A NOTICE OF DELINQUENCY")
    expect(block.locator("textarea, input")).to_have_count(0)
    expect(block.locator(".badge.ok")).to_have_text("Verified")
    expect(page.get_by_test_id("contacts-missing")).to_be_visible()
    axe(page)

    page.get_by_label("Statewide foreclosure hotline: telephone").fill("1-800-555-0100")
    page.get_by_label("Statewide foreclosure hotline: website").fill("https://example.org/hotline")
    page.get_by_label("U.S. Department of Housing and Urban Development (HUD): telephone").fill(
        "1-800-555-0101"
    )
    page.get_by_label("U.S. Department of Housing and Urban Development (HUD): website").fill(
        "https://example.org/hud"
    )
    page.get_by_label("Statewide civil legal aid hotline: telephone").fill("1-800-555-0102")
    page.get_by_label("Statewide civil legal aid hotline: website").fill(
        "https://example.org/legal-aid"
    )
    page.get_by_label("Reason for the change").last.fill("E2E contacts from Commerce")
    page.get_by_role("button", name="Save contacts").click()
    # Collection settings are a policy change: the secretary approves.
    propose_and_approve(page)
    page.goto("/settings/delinquency/")
    expect(page.get_by_test_id("contacts-missing")).to_have_count(0)
    expect(page.get_by_test_id("statutory-notice").locator("pre")).to_contain_text("1-800-555-0100")
    axe(page)

    page.goto("/money/delinquency/")
    expect(page.get_by_text("contacts are not filled in")).to_have_count(0)
