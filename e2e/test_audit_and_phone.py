"""Audit log view, phone layout + axe."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.flows import post_dues, record_payment

FIXTURES = Path(__file__).parent / "fixtures"


def test_audit_log_view(page, seed, login):
    login(BOARD_EMAIL)
    # Generate a payment and a bank match; seed already updated the association settings.
    post_dues(page, "2026-10")
    record_payment(page, "G", "350.86", "1042")
    page.goto("/money/bank/import/")
    page.get_by_label(re.compile("file", re.IGNORECASE)).set_input_files(FIXTURES / "bank_oct.csv")
    page.get_by_role("button", name=re.compile("upload|import", re.IGNORECASE)).click()
    page.goto("/")
    item = page.get_by_role("listitem").filter(has_text="Match deposit: Zelle from RAFAEL ORTIZ")
    item.get_by_role("button", name="Confirm").click()
    expect(item).to_have_count(0)

    for query, text in (
        ("object_type=payments.receipt", "payment"),
        ("action=match", "match"),
        ("object_type=core.association", "Association settings"),
    ):
        page.goto(f"/settings/audit/?{query}")
        log = page.get_by_role("table")
        expect(
            log.get_by_role("row").filter(has_text=re.compile(text, re.IGNORECASE)).first
        ).to_be_visible()
        expect(
            log.get_by_role("button", name=re.compile("edit|delete", re.IGNORECASE))
        ).to_have_count(0)
        expect(
            log.get_by_role("link", name=re.compile("edit|delete", re.IGNORECASE))
        ).to_have_count(0)


@pytest.mark.phone
def test_phone_layout(phone_page, seed, login, axe):
    page = phone_page
    login(BOARD_EMAIL, page)
    page.goto("/")
    tabs = page.get_by_role(
        "navigation", name="Hubs"
    )  # prototype: <nav class="tabbar" aria-label="Hubs">
    for t in ("Home", "Money", "Requests", "Units", "More"):
        expect(tabs.get_by_text(t, exact=True)).to_be_visible()
    tabs.get_by_text("More", exact=True).click()
    sheet = page.get_by_role("dialog", name=re.compile("more", re.IGNORECASE))
    for hub in ("Community", "Governance", "Vendors", "Documents"):
        expect(sheet.get_by_role("link", name=hub)).to_be_visible()
    page.keyboard.press("Escape")

    for path in ("/", "/money/", "/units/"):
        page.goto(path)
        axe(page)
