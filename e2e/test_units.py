"""Unit roster; an ownership transfer goes to the board as a change request."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.flows import propose_and_approve


def test_unit_roster(page, seed, login):
    login(BOARD_EMAIL)
    page.goto("/units/")
    rows = page.get_by_role("table").get_by_role("row")
    expect(rows).to_have_count(10)  # header + 9 units
    expect(page.get_by_role("cell", name="11.56%").first).to_be_visible()
    expect(page.get_by_role("cell", name="10.67%").first).to_be_visible()
    expect(page.get_by_text(re.compile(r"100\.00\s*%"))).to_be_visible()

    page.get_by_role("link", name=re.compile(r"^(Unit )?E$")).click()
    expect(page.get_by_text("Chris Doyle").first).to_be_visible()
    expect(page.get_by_text(re.compile("tenant", re.IGNORECASE)).first).to_be_visible()

    page.goto("/units/")
    page.get_by_role("link", name=re.compile(r"^(Unit )?C$")).click()
    prior = page.get_by_test_id("current-owner").inner_text()
    page.get_by_role("button", name=re.compile("transfer", re.IGNORECASE)).click()
    page.get_by_label(re.compile("new owner name", re.IGNORECASE)).fill("Pat Newowner")
    page.get_by_label(re.compile("new owner email", re.IGNORECASE)).fill("pat@cedarrow.example")
    page.get_by_label(re.compile("effective date", re.IGNORECASE)).fill("2026-10-01")
    page.get_by_role("button", name=re.compile("save|transfer", re.IGNORECASE)).click()
    propose_and_approve(page)
    page.get_by_role("link", name="See the result").click()
    expect(page.get_by_test_id("current-owner")).to_contain_text("Pat Newowner")
    history = page.get_by_role("region", name=re.compile("ownership history", re.IGNORECASE))
    expect(history).to_contain_text(prior.strip())
    expect(history).to_contain_text("2026")
