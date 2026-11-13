"""Rescind an adopted resolution -> its library document
shows the rescinded badge and note, stays visible to owners, and links back."""

import re

from playwright.sync_api import expect

from conftest import OWNER_EMAIL, SECRETARY_EMAIL
from helpers import m2

TITLE = "Resolution 2025-01: Collection policy"


def test_resolution_rescind_library(page, browser, base_url, seed, login, axe):
    m2.residents_enabled(True)
    login(SECRETARY_EMAIL)
    page.goto("/governance/board/")
    page.get_by_test_id("resolutions").get_by_role("link", name=re.compile("2025-01")).click()
    res_url = page.url
    page.get_by_label("Reason").fill("Replaced by new collection policy")
    page.get_by_role("button", name="Rescind resolution").click()
    expect(
        page.get_by_text(re.compile(r"Rescinded .*Replaced by new collection policy"))
    ).to_be_visible()

    page.goto("/documents/")
    page.get_by_role("link", name=TITLE, exact=True).click()
    status = page.get_by_test_id("doc-status")
    expect(status).to_contain_text("Rescinded")
    status.get_by_role("link", name="View record").click()
    expect(page).to_have_url(res_url)
    axe(page)

    ctx = browser.new_context(base_url=base_url)
    owner = ctx.new_page()
    login(OWNER_EMAIL, owner)
    owner.goto("/me/docs/")
    row = owner.get_by_test_id("docs-list").locator("*", has_text=TITLE).last
    expect(owner.get_by_test_id("docs-list")).to_contain_text(TITLE)
    expect(row).to_contain_text("Rescinded")
    ctx.close()
